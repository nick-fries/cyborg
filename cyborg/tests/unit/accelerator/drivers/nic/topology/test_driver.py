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

"""Tests for the vendor-agnostic NIC topology driver."""

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


class TestNICTopologyDriverContract(base.TestCase):
    """Driver-class contract tests (no Placement involvement)."""

    def test_vendor_string(self):
        self.assertEqual("topology", NICTopologyDriver.VENDOR)

    def test_stevedore_entrypoint(self):
        mgr = DriverManager(
            namespace='cyborg.accelerator.driver',
            name='nic_topology_driver',
            invoke_on_load=False,
        )
        self.assertIs(NICTopologyDriver, mgr.driver)

    def test_discover_disabled_returns_empty(self):
        # Flag default is False.
        d = NICTopologyDriver()
        self.assertEqual([], d.discover())

    def test_discover_swallows_exceptions(self):
        """The driver must never let discovery raise into the agent."""
        d = NICTopologyDriver()
        with mock.patch.object(
            sysinfo, 'discover', side_effect=RuntimeError('boom'),
        ):
            self.assertEqual([], d.discover())

    def test_update_not_implemented(self):
        d = NICTopologyDriver()
        self.assertRaises(
            NotImplementedError, d.update, 'cp', 'img',
        )

    def test_get_stats_empty(self):
        self.assertEqual({}, NICTopologyDriver().get_stats())


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
        """Operator narrows to Infiniband-only (class 0207)."""
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
        """A device whose physfn symlink exists is treated as a VF."""
        mock_listdir.return_value = ['0000:31:00.1']
        mock_is_pf.return_value = False  # has physfn
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:31:00.1/class': '0x020000',
        })
        out = sysinfo._discover_network_pfs(class_prefixes=['02'])
        self.assertEqual([], out)

    @mock.patch('os.listdir', side_effect=OSError('no perm'))
    def test_listdir_oserror_returns_empty(self, _ld):
        self.assertEqual([], sysinfo._discover_network_pfs(['02']))


class TestSocketAndNumaResolution(base.TestCase):

    @mock.patch('builtins.open')
    def test_read_numa_node_happy(self, mock_open):
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:31:00.0/numa_node': '1',
        })
        self.assertEqual(1, sysinfo._read_numa_node('0000:31:00.0'))

    @mock.patch('builtins.open', side_effect=OSError('boom'))
    def test_read_numa_node_oserror(self, _open):
        self.assertIsNone(sysinfo._read_numa_node('0000:31:00.0'))

    @mock.patch('builtins.open')
    def test_read_numa_node_minus_one(self, mock_open):
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
        """Older Neutron name: <host>:<physnet>:<BDF>."""
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
        """Nova PCI-in-Placement style uses upper-case BDF."""
        self._set_rps([
            {'uuid': 'rp-1', 'name': 'HOST042_0000:31:00.0'},
        ])
        # We pass lower-case BDF, comparison is case-insensitive.
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
        """Tie-breaker: prefer the RP whose name also contains the host."""
        self._set_rps([
            # Both contain the BDF, only the first contains host042.
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


class TestPatchTopologyTraits(base.TestCase):

    def setUp(self):
        super().setUp()
        self.client = mock.MagicMock()

    def test_emits_both_traits(self):
        sysinfo._patch_topology_traits(
            self.client, 'rp-1', socket_id=0, numa_node=0,
        )
        self.client.add_traits_to_rp.assert_called_once()
        args = self.client.add_traits_to_rp.call_args.args
        self.assertEqual('rp-1', args[0])
        self.assertIn('CUSTOM_TOPO_SOCKET0', args[1])
        self.assertIn('CUSTOM_TOPO_NUMA0', args[1])

    def test_socket_only_when_numa_unknown(self):
        sysinfo._patch_topology_traits(
            self.client, 'rp-1', socket_id=1, numa_node=None,
        )
        args = self.client.add_traits_to_rp.call_args.args
        self.assertIn('CUSTOM_TOPO_SOCKET1', args[1])
        self.assertNotIn('CUSTOM_TOPO_NUMA0', args[1])

    def test_no_call_when_both_unknown(self):
        sysinfo._patch_topology_traits(
            self.client, 'rp-1', socket_id=None, numa_node=None,
        )
        self.client.add_traits_to_rp.assert_not_called()

    def test_additive_not_destructive(self):
        """add_traits_to_rp is additive by design: it does not
        clobber existing traits. Tested at unit-level by confirming
        we never call any trait-replacement API and only call
        add_traits_to_rp.
        """
        sysinfo._patch_topology_traits(
            self.client, 'rp-1', socket_id=0, numa_node=0,
        )
        # No PUT, DELETE, or replace call on the client.
        for name in ('put', 'delete', '_put_rp_traits',
                     'delete_trait_by_name'):
            attr = getattr(self.client, name, None)
            if attr is not None and hasattr(attr, 'assert_not_called'):
                attr.assert_not_called()

    def test_add_traits_to_rp_failure_is_swallowed(self):
        """A PATCH failure must not raise."""
        self.client.add_traits_to_rp.side_effect = RuntimeError('boom')
        # Must not raise.
        sysinfo._patch_topology_traits(
            self.client, 'rp-1', socket_id=0, numa_node=0,
        )


class TestDiscoverOrchestration(base.TestCase):
    """End-to-end behavior of ``sysinfo.discover()`` with all I/O mocked."""

    def setUp(self):
        super().setUp()
        from cyborg.conf import CONF
        # Force the topology driver "on" for this test class.
        CONF.set_override('enabled', True, group='nic_topology')
        self.addCleanup(
            CONF.clear_override, 'enabled', group='nic_topology',
        )
        self.set_defaults(host='host042')

    @mock.patch.object(sysinfo, '_patch_topology_traits')
    @mock.patch.object(sysinfo, '_find_neutron_rp_for_bdf')
    @mock.patch.object(sysinfo, '_read_numa_node')
    @mock.patch.object(sysinfo, '_read_socket_id')
    @mock.patch.object(sysinfo, '_discover_network_pfs')
    @mock.patch.object(sysinfo, 'placement_client')
    def test_one_socket_one_pf_end_to_end(
        self, mock_pc, mock_disc, mock_sock, mock_numa,
        mock_find, mock_patch,
    ):
        """1-socket host: socket_id=0, NUMA=0, traits PATCHed."""
        mock_pc.PlacementClient.return_value = mock.MagicMock()
        mock_disc.return_value = [{"bdf": "0000:31:00.0"}]
        mock_sock.return_value = 0
        mock_numa.return_value = 0
        mock_find.return_value = 'rp-1'
        result = sysinfo.discover()
        self.assertEqual([], result)
        mock_patch.assert_called_once()
        call_args = mock_patch.call_args.args
        # client, rp_uuid, socket_id, numa_node
        self.assertEqual('rp-1', call_args[1])
        self.assertEqual(0, call_args[2])
        self.assertEqual(0, call_args[3])

    @mock.patch.object(sysinfo, '_patch_topology_traits')
    @mock.patch.object(sysinfo, '_find_neutron_rp_for_bdf')
    @mock.patch.object(sysinfo, '_read_numa_node')
    @mock.patch.object(sysinfo, '_read_socket_id')
    @mock.patch.object(sysinfo, '_discover_network_pfs')
    @mock.patch.object(sysinfo, 'placement_client')
    def test_no_matching_neutron_rp_skips(
        self, mock_pc, mock_disc, mock_sock, mock_numa,
        mock_find, mock_patch,
    ):
        """When no Neutron RP matches the BDF, skip with INFO; no error."""
        mock_pc.PlacementClient.return_value = mock.MagicMock()
        mock_disc.return_value = [{"bdf": "0000:31:00.0"}]
        mock_sock.return_value = 0
        mock_numa.return_value = 0
        mock_find.return_value = None
        result = sysinfo.discover()
        self.assertEqual([], result)
        mock_patch.assert_not_called()

    @mock.patch.object(sysinfo, '_patch_topology_traits')
    @mock.patch.object(sysinfo, '_find_neutron_rp_for_bdf')
    @mock.patch.object(sysinfo, '_read_numa_node')
    @mock.patch.object(sysinfo, '_read_socket_id')
    @mock.patch.object(sysinfo, '_discover_network_pfs')
    @mock.patch.object(sysinfo, 'placement_client')
    def test_two_pfs_each_get_patched_independently(
        self, mock_pc, mock_disc, mock_sock, mock_numa,
        mock_find, mock_patch,
    ):
        mock_pc.PlacementClient.return_value = mock.MagicMock()
        mock_disc.return_value = [
            {"bdf": "0000:31:00.0"},
            {"bdf": "0000:b1:00.0"},
        ]
        # First PF on socket 0/NUMA 0, second on socket 1/NUMA 1.
        mock_sock.side_effect = [0, 1]
        mock_numa.side_effect = [0, 1]
        mock_find.side_effect = ['rp-1', 'rp-2']

        sysinfo.discover()

        # Both PFs got a patch call.
        self.assertEqual(2, mock_patch.call_count)
        rp_uuids = [c.args[1] for c in mock_patch.call_args_list]
        self.assertEqual(['rp-1', 'rp-2'], rp_uuids)

    @mock.patch.object(sysinfo, '_discover_network_pfs')
    def test_disabled_short_circuit(self, mock_disc):
        from cyborg.conf import CONF
        CONF.set_override('enabled', False, group='nic_topology')
        self.addCleanup(
            CONF.clear_override, 'enabled', group='nic_topology',
        )
        result = sysinfo.discover()
        self.assertEqual([], result)
        mock_disc.assert_not_called()


class TestEmptyReturn(base.TestCase):
    """``discover()`` must always return [] regardless of state."""

    def test_disabled_returns_empty(self):
        from cyborg.conf import CONF
        # default is disabled, but be explicit.
        CONF.set_override('enabled', False, group='nic_topology')
        self.addCleanup(
            CONF.clear_override, 'enabled', group='nic_topology',
        )
        self.assertEqual([], sysinfo.discover())

    @mock.patch.object(sysinfo, '_discover_network_pfs', return_value=[])
    def test_no_pfs_returns_empty(self, _disc):
        from cyborg.conf import CONF
        CONF.set_override('enabled', True, group='nic_topology')
        self.addCleanup(
            CONF.clear_override, 'enabled', group='nic_topology',
        )
        self.assertEqual([], sysinfo.discover())
