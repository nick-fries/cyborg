# Copyright 2026 Phase 3 Topology Fork.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Tests for the vendor-agnostic NIC topology driver.

Test layout (AAA, one concept per test):

* ``TestNICTopologyDriverContract`` -- driver-class surface.
* ``TestDiscoverNetworkPFs``         -- sysfs scan.
* ``TestSocketAndNumaResolution``    -- sysfs leaf reads.
* ``TestFindNeutronRP``              -- BDF -> RP lookup.
* ``TestReconcileTopologyTraits``    -- Fix 1: atomic reconcile.
* ``TestStripTopologyTraits``        -- Fix 2 inner helper.
* ``TestSweepOrphanTopologyTraits``  -- Fix 2: orphan sweep.
* ``TestDiscoverOrchestration``      -- discover() end-to-end.
* ``TestEmptyReturn``                -- discover() return shape.
"""

from unittest import mock

from stevedore.driver import DriverManager

from cyborg.accelerator.drivers.nic.topology.driver import NICTopologyDriver
from cyborg.accelerator.drivers.nic.topology import sysinfo
from cyborg.tests import base


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _SysfsMock:
    """Helper to build an ``open()`` side-effect for sysfs reads."""

    def __init__(self, files):
        self.files = files

    def __call__(self, path, *args, **kwargs):
        if path not in self.files:
            raise FileNotFoundError(path)
        entry = self.files[path]
        if isinstance(entry, OSError):
            raise entry
        return mock.mock_open(read_data=entry).return_value


def _fake_placement_client(traits_by_rp=None, rps=None, generation=42):
    """Build a MagicMock PlacementClient with the methods sysinfo
    exercises wired to in-memory state.

    :param traits_by_rp: dict of rp_uuid -> list[str] of traits.
        ``_get_rp_traits`` returns ``{"traits": traits_by_rp[uuid]}``;
        ``_put_rp_traits`` mutates ``traits_by_rp`` in place.
    :param rps: list of dicts for ``GET /resource_providers`` to
        return.
    :param generation: ignored on the read path (we don't simulate
        generation conflicts here - the production client handles
        the read-modify-write).
    """
    client = mock.MagicMock()
    traits_by_rp = dict(traits_by_rp or {})
    rps = list(rps or [])

    def _get(url, *a, **kw):
        if url == '/resource_providers':
            return _Resp(200, {'resource_providers': rps})
        return _Resp(404, {})
    client.get.side_effect = _get

    def _get_traits(rp_uuid):
        return {'traits': list(traits_by_rp.get(rp_uuid, []))}
    client._get_rp_traits.side_effect = _get_traits

    def _put_traits(rp_uuid, traits_json):
        traits_by_rp[rp_uuid] = list(traits_json.get('traits', []))
    client._put_rp_traits.side_effect = _put_traits

    def _ensure_traits(traits):
        # idempotent; nothing to record beyond "was called"
        return None
    client._ensure_traits.side_effect = _ensure_traits

    client._state = traits_by_rp
    return client


# ---------------------------------------------------------------------------
# Driver class
# ---------------------------------------------------------------------------

class TestNICTopologyDriverContract(base.TestCase):
    """Driver-class contract tests (no Placement involvement)."""

    def test_vendor_string_is_topology(self):
        self.assertEqual("topology", NICTopologyDriver.VENDOR)

    def test_stevedore_entrypoint_loads_class(self):
        mgr = DriverManager(
            namespace='cyborg.accelerator.driver',
            name='nic_topology_driver',
            invoke_on_load=False,
        )
        self.assertIs(NICTopologyDriver, mgr.driver)

    def test_discover_disabled_returns_empty(self):
        d = NICTopologyDriver()
        self.assertEqual([], d.discover())

    def test_discover_swallows_exceptions(self):
        d = NICTopologyDriver()
        with mock.patch.object(
            sysinfo, 'discover', side_effect=RuntimeError('boom'),
        ):
            self.assertEqual([], d.discover())

    def test_update_raises_not_implemented(self):
        d = NICTopologyDriver()
        self.assertRaises(
            NotImplementedError, d.update, 'cp', 'img',
        )

    def test_get_stats_returns_empty_dict(self):
        self.assertEqual({}, NICTopologyDriver().get_stats())


# ---------------------------------------------------------------------------
# Sysfs scan
# ---------------------------------------------------------------------------

class TestDiscoverNetworkPFs(base.TestCase):
    """``_discover_network_pfs`` sysfs-scanning behavior."""

    @mock.patch.object(sysinfo, '_is_physical_function')
    @mock.patch('os.listdir')
    @mock.patch('builtins.open')
    def test_ethernet_pf_is_discovered(
        self, mock_open, mock_listdir, mock_is_pf,
    ):
        mock_listdir.return_value = ['0000:31:00.0']
        mock_is_pf.return_value = True
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:31:00.0/class': '0x020000',
        })
        out = sysinfo._discover_network_pfs(class_prefixes=['02'])
        self.assertEqual([{"bdf": '0000:31:00.0'}], out)

    @mock.patch.object(sysinfo, '_is_physical_function')
    @mock.patch('os.listdir')
    @mock.patch('builtins.open')
    def test_vga_class_is_not_discovered(
        self, mock_open, mock_listdir, mock_is_pf,
    ):
        mock_listdir.return_value = ['0000:01:00.0']
        mock_is_pf.return_value = True
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:01:00.0/class': '0x030000',
        })
        out = sysinfo._discover_network_pfs(class_prefixes=['02'])
        self.assertEqual([], out)

    @mock.patch.object(sysinfo, '_is_physical_function')
    @mock.patch('os.listdir')
    @mock.patch('builtins.open')
    def test_infiniband_only_filter(
        self, mock_open, mock_listdir, mock_is_pf,
    ):
        mock_listdir.return_value = [
            '0000:31:00.0',  # Ethernet (0200) - excluded
            '0000:32:00.0',  # Infiniband (0207) - included
        ]
        mock_is_pf.return_value = True
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:31:00.0/class': '0x020000',
            '/sys/bus/pci/devices/0000:32:00.0/class': '0x020700',
        })
        out = sysinfo._discover_network_pfs(class_prefixes=['0207'])
        self.assertEqual([{"bdf": '0000:32:00.0'}], out)

    @mock.patch.object(sysinfo, '_is_physical_function')
    @mock.patch('os.listdir')
    @mock.patch('builtins.open')
    def test_vf_is_skipped(
        self, mock_open, mock_listdir, mock_is_pf,
    ):
        mock_listdir.return_value = ['0000:31:00.1']
        mock_is_pf.return_value = False
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:31:00.1/class': '0x020000',
        })
        out = sysinfo._discover_network_pfs(class_prefixes=['02'])
        self.assertEqual([], out)

    @mock.patch('os.listdir', side_effect=OSError('no perm'))
    def test_listdir_oserror_returns_empty(self, _ld):
        self.assertEqual([], sysinfo._discover_network_pfs(['02']))


# ---------------------------------------------------------------------------
# Sysfs leaf reads
# ---------------------------------------------------------------------------

class TestSocketAndNumaResolution(base.TestCase):

    @mock.patch('builtins.open')
    def test_read_numa_node_happy(self, mock_open):
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:31:00.0/numa_node': '1',
        })
        self.assertEqual(1, sysinfo._read_numa_node('0000:31:00.0'))

    @mock.patch('builtins.open', side_effect=OSError('boom'))
    def test_read_numa_node_oserror_returns_none(self, _open):
        self.assertIsNone(sysinfo._read_numa_node('0000:31:00.0'))

    @mock.patch('builtins.open')
    def test_read_numa_node_minus_one_returns_none(self, mock_open):
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:31:00.0/numa_node': '-1',
        })
        self.assertIsNone(sysinfo._read_numa_node('0000:31:00.0'))

    @mock.patch('builtins.open')
    def test_read_socket_id_delegates_to_gpu_utils(self, mock_open):
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:31:00.0/local_cpulist': '0-15',
            '/sys/devices/system/cpu/cpu0/topology/physical_package_id': '0',
        })
        self.assertEqual(0, sysinfo._read_socket_id('0000:31:00.0'))


# ---------------------------------------------------------------------------
# BDF -> RP lookup
# ---------------------------------------------------------------------------

class TestFindNeutronRP(base.TestCase):

    def setUp(self):
        super().setUp()
        self.client = mock.MagicMock()
        self.context = mock.sentinel.context

    def _set_rps(self, rps):
        self.client.get.return_value = _Resp(
            status_code=200, body={'resource_providers': rps},
        )

    def test_bdf_match_neutron_style(self):
        self._set_rps([
            {'uuid': 'rp-1', 'name': 'host042:hpc:0000:31:00.0'},
            {'uuid': 'rp-2', 'name': 'host042:hpc:0000:32:00.0'},
            {'uuid': 'rp-x', 'name': 'unrelated'},
        ])
        out = sysinfo._find_neutron_rp_for_bdf(
            self.client, self.context, 'host042', '0000:31:00.0',
        )
        self.assertEqual('rp-1', out)

    def test_bdf_match_nova_pci_style_uppercase(self):
        self._set_rps([
            {'uuid': 'rp-1', 'name': 'HOST042_0000:31:00.0'},
        ])
        out = sysinfo._find_neutron_rp_for_bdf(
            self.client, self.context, 'host042', '0000:31:00.0',
        )
        self.assertEqual('rp-1', out)

    def test_no_matching_rp_returns_none(self):
        self._set_rps([
            {'uuid': 'rp-x', 'name': 'unrelated'},
        ])
        out = sysinfo._find_neutron_rp_for_bdf(
            self.client, self.context, 'host042', '0000:31:00.0',
        )
        self.assertIsNone(out)

    def test_prefers_host_qualified_match(self):
        self._set_rps([
            {'uuid': 'rp-1', 'name': 'host042:hpc:0000:31:00.0'},
            {'uuid': 'rp-2', 'name': 'host999:hpc:0000:31:00.0'},
        ])
        out = sysinfo._find_neutron_rp_for_bdf(
            self.client, self.context, 'host042', '0000:31:00.0',
        )
        self.assertEqual('rp-1', out)

    def test_placement_get_error_returns_none(self):
        self.client.get.side_effect = RuntimeError('boom')
        out = sysinfo._find_neutron_rp_for_bdf(
            self.client, self.context, 'host042', '0000:31:00.0',
        )
        self.assertIsNone(out)


# ---------------------------------------------------------------------------
# Fix 1: atomic trait reconciliation
# ---------------------------------------------------------------------------

class TestReconcileTopologyTraits(base.TestCase):
    """Set-reconciliation flow: GET, compute kept|desired, conditional PUT.

    These tests assert externally-observable behavior:
    * What does the final trait set on the RP look like?
    * Was _ensure_traits called for new traits?
    * Was _put_rp_traits called only when needed?
    """

    def _client_with(self, current):
        return _fake_placement_client(traits_by_rp={'rp-1': list(current)})

    def test_reconcile_adds_both_traits_on_empty_rp(self):
        # Arrange
        client = self._client_with([])
        # Act
        sysinfo._reconcile_topology_traits(client, 'rp-1', 0, 0)
        # Assert
        self.assertEqual(
            {'CUSTOM_TOPO_SOCKET0', 'CUSTOM_TOPO_NUMA0'},
            set(client._state['rp-1']),
        )

    def test_reconcile_preserves_unrelated_neutron_traits(self):
        client = self._client_with(
            ['CUSTOM_PHYSNET_HPC', 'CUSTOM_VNIC_TYPE_DIRECT'],
        )
        sysinfo._reconcile_topology_traits(client, 'rp-1', 1, 1)
        self.assertEqual(
            {
                'CUSTOM_PHYSNET_HPC',
                'CUSTOM_VNIC_TYPE_DIRECT',
                'CUSTOM_TOPO_SOCKET1',
                'CUSTOM_TOPO_NUMA1',
            },
            set(client._state['rp-1']),
        )

    def test_reconcile_strips_stale_socket_trait_on_move(self):
        """A NIC that moved from socket 0 -> socket 1 must lose
        CUSTOM_TOPO_SOCKET0 on the same PUT that adds SOCKET1."""
        client = self._client_with([
            'CUSTOM_PHYSNET_HPC',
            'CUSTOM_TOPO_SOCKET0',
            'CUSTOM_TOPO_NUMA0',
        ])
        sysinfo._reconcile_topology_traits(client, 'rp-1', 1, 1)
        self.assertEqual(
            {
                'CUSTOM_PHYSNET_HPC',
                'CUSTOM_TOPO_SOCKET1',
                'CUSTOM_TOPO_NUMA1',
            },
            set(client._state['rp-1']),
        )

    def test_reconcile_strips_both_topology_traits_when_unknown(self):
        """Both socket_id and numa_node unreadable -> strip all
        topology traits but keep neighbors."""
        client = self._client_with([
            'CUSTOM_PHYSNET_HPC',
            'CUSTOM_TOPO_SOCKET0',
            'CUSTOM_TOPO_NUMA0',
        ])
        sysinfo._reconcile_topology_traits(client, 'rp-1', None, None)
        self.assertEqual(
            {'CUSTOM_PHYSNET_HPC'},
            set(client._state['rp-1']),
        )

    def test_reconcile_socket_known_numa_unknown(self):
        client = self._client_with([])
        sysinfo._reconcile_topology_traits(client, 'rp-1', 1, None)
        self.assertEqual(
            {'CUSTOM_TOPO_SOCKET1'},
            set(client._state['rp-1']),
        )

    def test_reconcile_noop_when_traits_already_match(self):
        """If the RP already has exactly the desired topology traits,
        we MUST NOT call _put_rp_traits (no unnecessary writes)."""
        client = self._client_with([
            'CUSTOM_TOPO_SOCKET0', 'CUSTOM_TOPO_NUMA0',
        ])
        sysinfo._reconcile_topology_traits(client, 'rp-1', 0, 0)
        client._put_rp_traits.assert_not_called()

    def test_reconcile_noop_skips_ensure_traits_too(self):
        client = self._client_with([
            'CUSTOM_TOPO_SOCKET0', 'CUSTOM_TOPO_NUMA0',
        ])
        sysinfo._reconcile_topology_traits(client, 'rp-1', 0, 0)
        client._ensure_traits.assert_not_called()

    def test_reconcile_strips_two_socket_traits_after_swap(self):
        """The bug-mode-A scenario: NIC swap leaves both SOCKET0 and
        SOCKET1 lingering. Reconcile MUST collapse to the current
        socket only."""
        client = self._client_with([
            'CUSTOM_TOPO_SOCKET0',
            'CUSTOM_TOPO_SOCKET1',
            'CUSTOM_TOPO_NUMA0',
            'CUSTOM_PHYSNET_HPC',
        ])
        sysinfo._reconcile_topology_traits(client, 'rp-1', 1, 1)
        self.assertEqual(
            {
                'CUSTOM_PHYSNET_HPC',
                'CUSTOM_TOPO_SOCKET1',
                'CUSTOM_TOPO_NUMA1',
            },
            set(client._state['rp-1']),
        )

    def test_reconcile_get_failure_skips_put(self):
        client = _fake_placement_client(traits_by_rp={})
        client._get_rp_traits.side_effect = RuntimeError('boom')
        sysinfo._reconcile_topology_traits(client, 'rp-1', 0, 0)
        client._put_rp_traits.assert_not_called()

    def test_reconcile_put_failure_swallowed(self):
        client = self._client_with(['CUSTOM_PHYSNET_HPC'])
        client._put_rp_traits.side_effect = RuntimeError('boom')
        # Must not raise.
        sysinfo._reconcile_topology_traits(client, 'rp-1', 0, 0)

    def test_reconcile_ensure_traits_failure_skips_put(self):
        client = self._client_with([])
        client._ensure_traits.side_effect = RuntimeError('boom')
        sysinfo._reconcile_topology_traits(client, 'rp-1', 0, 0)
        client._put_rp_traits.assert_not_called()

    def test_reconcile_negative_values_treated_as_unknown(self):
        client = self._client_with(['CUSTOM_TOPO_SOCKET2'])
        sysinfo._reconcile_topology_traits(client, 'rp-1', -1, -1)
        # All topology traits gone, no negative-id traits created.
        self.assertEqual(set(), set(client._state['rp-1']))

    def test_reconcile_honors_configured_socket_prefix(self):
        """Operator-customized prefix must drive both the desired
        additions and the strip filter."""
        from cyborg.conf import CONF
        CONF.set_override(
            'socket_trait_prefix', 'CUSTOM_OPERATOR_SOC', group='nic_topology',
        )
        self.addCleanup(
            CONF.clear_override,
            'socket_trait_prefix', group='nic_topology',
        )
        client = self._client_with([
            'CUSTOM_OPERATOR_SOC0',
            'CUSTOM_PHYSNET_HPC',
        ])
        sysinfo._reconcile_topology_traits(client, 'rp-1', 1, None)
        self.assertIn('CUSTOM_OPERATOR_SOC1', client._state['rp-1'])
        self.assertNotIn('CUSTOM_OPERATOR_SOC0', client._state['rp-1'])
        self.assertIn('CUSTOM_PHYSNET_HPC', client._state['rp-1'])

    def test_reconcile_single_round_trip_when_change_needed(self):
        """Exactly one GET + one PUT (single _ensure_traits batch)
        when traits change."""
        client = self._client_with(['CUSTOM_PHYSNET_HPC'])
        sysinfo._reconcile_topology_traits(client, 'rp-1', 0, 0)
        self.assertEqual(1, client._get_rp_traits.call_count)
        self.assertEqual(1, client._put_rp_traits.call_count)


# ---------------------------------------------------------------------------
# Fix 2 inner helper: _strip_topology_traits
# ---------------------------------------------------------------------------

class TestStripTopologyTraits(base.TestCase):

    def test_strip_removes_only_topology_traits(self):
        client = _fake_placement_client(traits_by_rp={
            'rp-1': [
                'CUSTOM_TOPO_SOCKET0',
                'CUSTOM_TOPO_NUMA1',
                'CUSTOM_PHYSNET_HPC',
            ],
        })
        n = sysinfo._strip_topology_traits(client, 'rp-1')
        self.assertEqual(2, n)
        self.assertEqual(['CUSTOM_PHYSNET_HPC'], client._state['rp-1'])

    def test_strip_noop_when_no_topology_traits_present(self):
        client = _fake_placement_client(traits_by_rp={
            'rp-1': ['CUSTOM_PHYSNET_HPC'],
        })
        n = sysinfo._strip_topology_traits(client, 'rp-1')
        self.assertEqual(0, n)
        client._put_rp_traits.assert_not_called()

    def test_strip_get_failure_returns_zero(self):
        client = _fake_placement_client(traits_by_rp={})
        client._get_rp_traits.side_effect = RuntimeError('boom')
        self.assertEqual(0, sysinfo._strip_topology_traits(client, 'rp-1'))

    def test_strip_put_failure_returns_zero(self):
        client = _fake_placement_client(traits_by_rp={
            'rp-1': ['CUSTOM_TOPO_SOCKET0'],
        })
        client._put_rp_traits.side_effect = RuntimeError('boom')
        self.assertEqual(0, sysinfo._strip_topology_traits(client, 'rp-1'))


# ---------------------------------------------------------------------------
# Fix 2: orphan sweep
# ---------------------------------------------------------------------------

class TestSweepOrphanTopologyTraits(base.TestCase):

    def test_sweep_with_no_rps_is_zero(self):
        client = _fake_placement_client(rps=[])
        out = sysinfo._sweep_orphan_topology_traits(
            client, 'host042', {'0000:31:00.0'},
        )
        self.assertEqual((0, 0), out)

    def test_sweep_with_no_pfs_strips_all_host_topology_traits(self):
        """All NICs removed from host -> every host-qualified
        BDF-bearing RP gets its topology traits stripped."""
        client = _fake_placement_client(
            traits_by_rp={
                'rp-1': ['CUSTOM_TOPO_SOCKET0', 'CUSTOM_PHYSNET_HPC'],
                'rp-2': ['CUSTOM_TOPO_NUMA1', 'CUSTOM_PHYSNET_HPC'],
            },
            rps=[
                {'uuid': 'rp-1', 'name': 'host042:hpc:0000:31:00.0'},
                {'uuid': 'rp-2', 'name': 'host042:hpc:0000:b1:00.0'},
            ],
        )
        rps_swept, traits_stripped = (
            sysinfo._sweep_orphan_topology_traits(client, 'host042', set())
        )
        self.assertEqual(2, rps_swept)
        self.assertEqual(2, traits_stripped)
        self.assertEqual(['CUSTOM_PHYSNET_HPC'], client._state['rp-1'])
        self.assertEqual(['CUSTOM_PHYSNET_HPC'], client._state['rp-2'])

    def test_sweep_present_bdf_is_not_swept(self):
        """RPs whose BDF is currently present on the host must be
        left alone by the sweep."""
        client = _fake_placement_client(
            traits_by_rp={
                'rp-1': ['CUSTOM_TOPO_SOCKET0', 'CUSTOM_PHYSNET_HPC'],
            },
            rps=[
                {'uuid': 'rp-1', 'name': 'host042:hpc:0000:31:00.0'},
            ],
        )
        rps_swept, traits_stripped = (
            sysinfo._sweep_orphan_topology_traits(
                client, 'host042', {'0000:31:00.0'},
            )
        )
        self.assertEqual(0, rps_swept)
        self.assertEqual(0, traits_stripped)
        # Untouched.
        self.assertIn('CUSTOM_TOPO_SOCKET0', client._state['rp-1'])

    def test_sweep_different_host_rps_are_ignored(self):
        """Host-scoping: an RP on host999 must NOT be swept by host042."""
        client = _fake_placement_client(
            traits_by_rp={
                'rp-other': ['CUSTOM_TOPO_SOCKET0'],
            },
            rps=[
                {'uuid': 'rp-other', 'name': 'host999:hpc:0000:31:00.0'},
            ],
        )
        rps_swept, _ = sysinfo._sweep_orphan_topology_traits(
            client, 'host042', set(),
        )
        self.assertEqual(0, rps_swept)
        # Untouched.
        self.assertIn('CUSTOM_TOPO_SOCKET0', client._state['rp-other'])

    def test_sweep_non_bdf_named_rps_are_skipped(self):
        """An RP without a BDF substring in its name is not in
        scope. (Cyborg-managed sub-RPs, host roots, etc.)"""
        client = _fake_placement_client(
            traits_by_rp={
                'rp-root': ['CUSTOM_TOPO_SOCKET0'],
            },
            rps=[
                {'uuid': 'rp-root', 'name': 'host042_socket_0'},
            ],
        )
        rps_swept, _ = sysinfo._sweep_orphan_topology_traits(
            client, 'host042', set(),
        )
        self.assertEqual(0, rps_swept)

    def test_sweep_mixed_outcomes(self):
        """Two RPs: one currently-present (kept), one orphan (swept)."""
        client = _fake_placement_client(
            traits_by_rp={
                'rp-keep': ['CUSTOM_TOPO_SOCKET0', 'CUSTOM_PHYSNET_HPC'],
                'rp-strip': ['CUSTOM_TOPO_SOCKET1', 'CUSTOM_PHYSNET_HPC'],
            },
            rps=[
                {'uuid': 'rp-keep', 'name': 'host042:hpc:0000:31:00.0'},
                {'uuid': 'rp-strip', 'name': 'host042:hpc:0000:b1:00.0'},
            ],
        )
        rps_swept, traits_stripped = (
            sysinfo._sweep_orphan_topology_traits(
                client, 'host042', {'0000:31:00.0'},
            )
        )
        self.assertEqual(1, rps_swept)
        self.assertEqual(1, traits_stripped)
        # Kept RP untouched.
        self.assertIn('CUSTOM_TOPO_SOCKET0', client._state['rp-keep'])
        # Strip RP cleaned.
        self.assertEqual(['CUSTOM_PHYSNET_HPC'], client._state['rp-strip'])

    def test_sweep_rp_with_no_topology_traits_skipped(self):
        """An orphan RP that has no topology traits left is a no-op
        (no spurious PUT)."""
        client = _fake_placement_client(
            traits_by_rp={
                'rp-clean': ['CUSTOM_PHYSNET_HPC'],
            },
            rps=[
                {'uuid': 'rp-clean', 'name': 'host042:hpc:0000:31:00.0'},
            ],
        )
        rps_swept, _ = sysinfo._sweep_orphan_topology_traits(
            client, 'host042', set(),
        )
        self.assertEqual(0, rps_swept)
        client._put_rp_traits.assert_not_called()

    def test_sweep_empty_host_name_is_no_op(self):
        """Refuse to sweep when host_name is empty - would over-match."""
        client = _fake_placement_client(
            traits_by_rp={
                'rp-1': ['CUSTOM_TOPO_SOCKET0'],
            },
            rps=[
                {'uuid': 'rp-1', 'name': 'host042:hpc:0000:31:00.0'},
            ],
        )
        out = sysinfo._sweep_orphan_topology_traits(client, '', set())
        self.assertEqual((0, 0), out)

    def test_sweep_placement_get_error_returns_zero(self):
        client = _fake_placement_client()
        client.get.side_effect = RuntimeError('boom')
        out = sysinfo._sweep_orphan_topology_traits(client, 'host042', set())
        self.assertEqual((0, 0), out)

    def test_sweep_placement_500_returns_zero(self):
        client = _fake_placement_client()
        client.get.side_effect = None
        client.get.return_value = _Resp(500, {})
        out = sysinfo._sweep_orphan_topology_traits(client, 'host042', set())
        self.assertEqual((0, 0), out)

    def test_sweep_uppercase_bdf_in_rp_name_matched(self):
        """Nova-PCI-in-Placement-style names use upper-case BDF.
        BDF regex is case-insensitive via .lower() pre-normalize."""
        client = _fake_placement_client(
            traits_by_rp={
                'rp-1': ['CUSTOM_TOPO_SOCKET0'],
            },
            rps=[
                {'uuid': 'rp-1', 'name': 'HOST042_0000:31:00.0'},
            ],
        )
        rps_swept, _ = sysinfo._sweep_orphan_topology_traits(
            client, 'host042', set(),
        )
        self.assertEqual(1, rps_swept)


# ---------------------------------------------------------------------------
# discover() orchestration
# ---------------------------------------------------------------------------

class TestDiscoverOrchestration(base.TestCase):
    """End-to-end behavior of ``sysinfo.discover()``."""

    def setUp(self):
        super().setUp()
        from cyborg.conf import CONF
        CONF.set_override('enabled', True, group='nic_topology')
        self.addCleanup(
            CONF.clear_override, 'enabled', group='nic_topology',
        )
        self.set_defaults(host='host042')

    @mock.patch.object(sysinfo, '_sweep_orphan_topology_traits')
    @mock.patch.object(sysinfo, '_reconcile_topology_traits')
    @mock.patch.object(sysinfo, '_find_neutron_rp_for_bdf')
    @mock.patch.object(sysinfo, '_read_numa_node')
    @mock.patch.object(sysinfo, '_read_socket_id')
    @mock.patch.object(sysinfo, '_discover_network_pfs')
    @mock.patch.object(sysinfo, 'placement_client')
    def test_one_pf_reconciles_and_sweep_runs(
        self, mock_pc, mock_disc, mock_sock, mock_numa,
        mock_find, mock_reconcile, mock_sweep,
    ):
        # Arrange
        mock_pc.PlacementClient.return_value = mock.MagicMock()
        mock_disc.return_value = [{"bdf": "0000:31:00.0"}]
        mock_sock.return_value = 0
        mock_numa.return_value = 0
        mock_find.return_value = 'rp-1'
        # Act
        result = sysinfo.discover()
        # Assert
        self.assertEqual([], result)
        mock_reconcile.assert_called_once()
        args = mock_reconcile.call_args.args
        self.assertEqual('rp-1', args[1])
        self.assertEqual(0, args[2])
        self.assertEqual(0, args[3])
        mock_sweep.assert_called_once()
        # Sweep got the BDF set.
        sweep_args = mock_sweep.call_args.args
        self.assertEqual({'0000:31:00.0'}, sweep_args[2])

    @mock.patch.object(sysinfo, '_sweep_orphan_topology_traits')
    @mock.patch.object(sysinfo, '_reconcile_topology_traits')
    @mock.patch.object(sysinfo, '_find_neutron_rp_for_bdf')
    @mock.patch.object(sysinfo, '_read_numa_node')
    @mock.patch.object(sysinfo, '_read_socket_id')
    @mock.patch.object(sysinfo, '_discover_network_pfs')
    @mock.patch.object(sysinfo, 'placement_client')
    def test_no_matching_neutron_rp_skips_reconcile(
        self, mock_pc, mock_disc, mock_sock, mock_numa,
        mock_find, mock_reconcile, mock_sweep,
    ):
        mock_pc.PlacementClient.return_value = mock.MagicMock()
        mock_disc.return_value = [{"bdf": "0000:31:00.0"}]
        mock_sock.return_value = 0
        mock_numa.return_value = 0
        mock_find.return_value = None
        result = sysinfo.discover()
        self.assertEqual([], result)
        mock_reconcile.assert_not_called()
        # Sweep still runs - BDF is "present" on host even if Neutron
        # hasn't tracked it.
        mock_sweep.assert_called_once()

    @mock.patch.object(sysinfo, '_sweep_orphan_topology_traits')
    @mock.patch.object(sysinfo, '_reconcile_topology_traits')
    @mock.patch.object(sysinfo, '_find_neutron_rp_for_bdf')
    @mock.patch.object(sysinfo, '_read_numa_node')
    @mock.patch.object(sysinfo, '_read_socket_id')
    @mock.patch.object(sysinfo, '_discover_network_pfs')
    @mock.patch.object(sysinfo, 'placement_client')
    def test_two_pfs_each_get_reconciled_independently(
        self, mock_pc, mock_disc, mock_sock, mock_numa,
        mock_find, mock_reconcile, mock_sweep,
    ):
        mock_pc.PlacementClient.return_value = mock.MagicMock()
        mock_disc.return_value = [
            {"bdf": "0000:31:00.0"},
            {"bdf": "0000:b1:00.0"},
        ]
        mock_sock.side_effect = [0, 1]
        mock_numa.side_effect = [0, 1]
        mock_find.side_effect = ['rp-1', 'rp-2']
        sysinfo.discover()
        self.assertEqual(2, mock_reconcile.call_count)
        rp_uuids = [c.args[1] for c in mock_reconcile.call_args_list]
        self.assertEqual(['rp-1', 'rp-2'], rp_uuids)
        # Sweep gets both BDFs in the present set.
        sweep_args = mock_sweep.call_args.args
        self.assertEqual(
            {'0000:31:00.0', '0000:b1:00.0'}, sweep_args[2],
        )

    @mock.patch.object(sysinfo, '_sweep_orphan_topology_traits')
    @mock.patch.object(sysinfo, '_discover_network_pfs')
    @mock.patch.object(sysinfo, 'placement_client')
    def test_no_pfs_still_runs_sweep(
        self, mock_pc, mock_disc, mock_sweep,
    ):
        """Mode-A bug-fix: all NICs removed -> empty PF list -> sweep
        MUST still run so stale topology traits are stripped."""
        mock_pc.PlacementClient.return_value = mock.MagicMock()
        mock_disc.return_value = []
        sysinfo.discover()
        mock_sweep.assert_called_once()
        sweep_args = mock_sweep.call_args.args
        self.assertEqual(set(), sweep_args[2])

    @mock.patch.object(sysinfo, '_discover_network_pfs')
    def test_disabled_short_circuit_no_sweep(self, mock_disc):
        from cyborg.conf import CONF
        CONF.set_override('enabled', False, group='nic_topology')
        self.addCleanup(
            CONF.clear_override, 'enabled', group='nic_topology',
        )
        result = sysinfo.discover()
        self.assertEqual([], result)
        mock_disc.assert_not_called()

    @mock.patch.object(
        sysinfo, '_sweep_orphan_topology_traits',
        side_effect=RuntimeError('boom'),
    )
    @mock.patch.object(sysinfo, '_reconcile_topology_traits')
    @mock.patch.object(sysinfo, '_find_neutron_rp_for_bdf')
    @mock.patch.object(sysinfo, '_read_numa_node', return_value=0)
    @mock.patch.object(sysinfo, '_read_socket_id', return_value=0)
    @mock.patch.object(sysinfo, '_discover_network_pfs')
    @mock.patch.object(sysinfo, 'placement_client')
    def test_sweep_exception_is_swallowed(
        self, mock_pc, mock_disc, _sock, _numa, mock_find,
        _reconcile, _sweep,
    ):
        mock_pc.PlacementClient.return_value = mock.MagicMock()
        mock_disc.return_value = [{"bdf": "0000:31:00.0"}]
        mock_find.return_value = 'rp-1'
        # Must not raise.
        self.assertEqual([], sysinfo.discover())


class TestEmptyReturn(base.TestCase):
    """``discover()`` must always return [] regardless of state."""

    def test_disabled_returns_empty(self):
        from cyborg.conf import CONF
        CONF.set_override('enabled', False, group='nic_topology')
        self.addCleanup(
            CONF.clear_override, 'enabled', group='nic_topology',
        )
        self.assertEqual([], sysinfo.discover())

    @mock.patch.object(sysinfo, '_sweep_orphan_topology_traits')
    @mock.patch.object(sysinfo, 'placement_client')
    @mock.patch.object(sysinfo, '_discover_network_pfs', return_value=[])
    def test_no_pfs_returns_empty(self, _disc, _pc, _sweep):
        from cyborg.conf import CONF
        CONF.set_override('enabled', True, group='nic_topology')
        self.addCleanup(
            CONF.clear_override, 'enabled', group='nic_topology',
        )
        self.assertEqual([], sysinfo.discover())
