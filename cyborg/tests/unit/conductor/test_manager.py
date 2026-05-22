#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
from unittest import mock

import fixtures

from oslo_utils.fixture import uuidsentinel as uuids

from cyborg.common import exception
from cyborg.conductor import manager
from cyborg.tests import base
from cyborg.tests.unit import fake_driver_device


class ConductorManagerTest(base.TestCase):
    def setUp(self):
        super().setUp()
        self.placement_mock = self.useFixture(
            fixtures.MockPatch(
                'cyborg.common.placement_client.PlacementClient'
            )
        ).mock.return_value
        self.cm = manager.ConductorManager(
            mock.sentinel.topic, mock.sentinel.host
        )
        self.fake_driver_devices = (
            fake_driver_device.get_fake_driver_devices_objs()
        )
        self.fake_driver_depolyables = (
            fake_driver_device.get_fake_driver_deployable_objs()
        )

    def test__gen_resource_inventory(self):
        expected = {
            'CUSTOM_FOO': {
                'total': 42,
                'max_unit': 42,
            },
        }
        actual = manager._gen_resource_inventory('CUSTOM_FOO', 42)
        self.assertEqual(expected, actual)

    @mock.patch('cyborg.conductor.manager.ConductorManager._get_sub_provider')
    def test_provider_report(self, mock_get_sub):
        rc = 'CUSTOM_ACCELERATOR'
        traits = [
            "CUSTOM_FPGA_INTEL",
            "CUSTOM_FPGA_INTEL_ARRIA10",
            "CUSTOM_FPGA_INTEL_REGION_UUID",
            "CUSTOM_FPGA_FUNCTION_ID_INTEL_UUID",
            "CUSTOM_PROGRAMMABLE",
            "CUSTOM_FPGA_NETWORK",
        ]
        total = 42
        expected_inv = {
            rc: {
                'total': total,
                'max_unit': total,
            },
        }
        self.placement_mock.ensure_resource_classes.return_value = None
        actual = self.cm.provider_report(
            mock.sentinel.context,
            mock.sentinel.name,
            rc,
            traits,
            total,
            mock.sentinel.parent,
        )

        self.placement_mock.ensure_resource_classes.assert_called_once_with(
            mock.sentinel.context, [rc]
        )
        mock_get_sub.assert_called_once_with(
            mock.sentinel.context, mock.sentinel.parent, mock.sentinel.name
        )
        sub_pr_uuid = mock_get_sub.return_value
        self.placement_mock.update_inventory.assert_called_once_with(
            sub_pr_uuid, expected_inv
        )
        self.placement_mock.add_traits_to_rp.assert_called_once_with(
            sub_pr_uuid, traits
        )
        self.assertEqual(sub_pr_uuid, actual)

    def test_get_root_provider(self):
        self.placement_mock.get.return_value.json.return_value = {
            'resource_providers': [{'uuid': mock.sentinel.uuid}],
        }
        uuid = self.cm._get_root_provider(mock.sentinel.context, 'foo')
        self.assertEqual(mock.sentinel.uuid, uuid)

    def test_get_root_provider_not_found(self):
        self.placement_mock.get.return_value.json.return_value = {
            'resource_providers': [],
        }
        self.assertRaises(
            exception.PlacementResourceProviderNotFound,
            self.cm._get_root_provider,
            mock.sentinel.context,
            'foo',
        )

    def test_get_root_provider_unavailable(self):
        self.placement_mock.get.side_effect = exception.PlacementServerError(
            "Placement Server has some error at this time."
        )
        self.assertRaises(
            exception.PlacementServerError,
            self.cm._get_root_provider,
            mock.sentinel.context,
            'foo',
        )

    @mock.patch(
        'cyborg.conductor.manager.ConductorManager.'
        '_delete_provider_and_sub_providers'
    )
    @mock.patch(
        'cyborg.conductor.manager.ConductorManager.'
        'get_placement_needed_info_and_report'
    )
    @mock.patch(
        'cyborg.objects.driver_objects.driver_device.DriverDevice.destroy'
    )
    @mock.patch(
        'cyborg.objects.driver_objects.driver_device.DriverDevice.create'
    )
    def test_drv_device_make_diff(
        self,
        mock_create_driver_device,
        mock_destroy_driver_device,
        mock_placement_report,
        mock_placement_delete,
    ):
        old_driver_attr_list = []
        new_driver_attr_list = self.fake_driver_devices[:1]
        self.placement_mock.get.return_value.json.return_value = {
            'resource_providers': [{'uuid': mock.sentinel.uuid}],
        }

        mock_placement_report.side_effect = (
            exception.ResourceProviderCreationFailed(name=uuids.compute_node)
        )

        self.cm.drv_device_make_diff(
            mock.sentinel.context,
            'foo',
            old_driver_attr_list,
            new_driver_attr_list,
        )

        mock_destroy_driver_device.assert_called_once()
        mock_placement_delete.assert_called_once()

    @mock.patch(
        'cyborg.conductor.manager.ConductorManager.'
        '_delete_provider_and_sub_providers'
    )
    @mock.patch(
        'cyborg.conductor.manager.ConductorManager.'
        'get_placement_needed_info_and_report'
    )
    @mock.patch(
        'cyborg.objects.driver_objects.driver_deployable.'
        'DriverDeployable.destroy'
    )
    @mock.patch(
        'cyborg.objects.driver_objects.driver_deployable.'
        'DriverDeployable.create'
    )
    def test_drv_deployable_make_diff(
        self,
        mock_create_driver_deployable,
        mock_destroy_driver_deployable,
        mock_placement_report,
        mock_placement_delete,
    ):
        old_driver_dep_list = []
        new_driver_dep_list = self.fake_driver_depolyables[:1]
        self.placement_mock.get.return_value.json.return_value = {
            'resource_providers': [{'uuid': mock.sentinel.uuid}],
        }

        mock_placement_report.side_effect = (
            exception.ResourceProviderCreationFailed(name=uuids.compute_node)
        )

        self.cm.drv_deployable_make_diff(
            mock.sentinel.context,
            '1',
            '2',
            old_driver_dep_list,
            new_driver_dep_list,
            'foo',
        )

        mock_destroy_driver_deployable.assert_called_once()
        mock_placement_delete.assert_called_once()

    @mock.patch(
        'cyborg.common.data_migrations.heal_arq_project_ids', autospec=True
    )
    def test_init_host_heals_null_project_ids(self, mock_heal):
        mock_heal.return_value = 3
        self.cm.init_host()
        mock_heal.assert_called_once()

    @mock.patch(
        'cyborg.common.data_migrations.heal_arq_project_ids', autospec=True
    )
    def test_init_host_heal_handles_failure(self, mock_heal):
        mock_heal.side_effect = Exception('Nova unavailable')
        self.cm.init_host()
        mock_heal.assert_called_once()


class _FakeAttr:
    def __init__(self, key, value):
        self.key = key
        self.value = value


class _FakeDep:
    """Lightweight driver-side deployable for NUMA/socket sub-RP tests."""

    def __init__(self, name, numa_node=None, rc='PGPU', traits=None,
                 total=1, socket_id=None):
        self.name = name
        self.num_accelerators = total
        self.attribute_list = [_FakeAttr('rc', rc)]
        for i, t in enumerate(traits or []):
            self.attribute_list.append(_FakeAttr('trait%d' % i, t))
        if numa_node is not None:
            self.attribute_list.append(
                _FakeAttr('numa_node', str(numa_node))
            )
        # Phase 3: optional socket_id DriverAttribute. Conductor reads
        # this to interpose a <host>_socket_<n> anchor above the NUMA
        # sub-RP.
        if socket_id is not None:
            self.attribute_list.append(
                _FakeAttr('socket_id', str(socket_id))
            )


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class NumaAwareSubRPTest(base.TestCase):
    """Phase 2: tests for the conductor NUMA-aware sub-RP layer.

    Cases:
    1. Flag off (default): no NUMA sub-RP interposed; behavior matches
       today's flat tree.
    2. Flag on, deployable with numa_node=0: deployable RP parented
       under "<host>_numa_0" sub-RP; HW_NUMA_ROOT trait tagged.
    3. Flag on, deployable with numa_node=-1: no sub-RP; parents
       directly under host root.
    4. Flag on, two deployables on different NUMA nodes: two
       separate sub-RPs created.
    5. Delete with allocations: deferred-RP-delete guard fires; RP
       is NOT deleted.
    6. Delete without allocations: RP and empty NUMA sub-RP parent
       both garbage-collected.
    """

    def setUp(self):
        super().setUp()
        self.placement_mock = self.useFixture(
            fixtures.MockPatch(
                'cyborg.common.placement_client.PlacementClient'
            )
        ).mock.return_value
        self.cm = manager.ConductorManager(
            mock.sentinel.topic, mock.sentinel.host
        )
        # ensure_resource_provider in the real client returns the UUID
        # it was passed in when the RP already exists / on create. The
        # mock should pass it through.
        self.placement_mock.ensure_resource_provider.side_effect = (
            lambda ctx, uuid_, name=None, parent_provider_uuid=None: uuid_
        )
        # Patch Deployable.get_by_name so get_placement_needed_info_and_report
        # doesn't try to touch the DB.
        self.dep_obj = mock.MagicMock()
        get_by_name = self.useFixture(
            fixtures.MockPatch(
                'cyborg.conductor.manager.Deployable.get_by_name'
            )
        ).mock
        get_by_name.return_value = self.dep_obj

    def _set_flag(self, on):
        # Phase 2 flag lives in CONF.placement.numa_aware_subtree.
        from cyborg.conf import CONF
        CONF.set_override('numa_aware_subtree', on, group='placement')
        self.addCleanup(
            CONF.clear_override, 'numa_aware_subtree', group='placement'
        )

    # ---- Case 1: flag off ----
    def test_flag_off_keeps_flat_tree(self):
        self._set_flag(False)
        dep = _FakeDep('host01_devA', numa_node=0)
        self.cm.get_placement_needed_info_and_report(
            mock.sentinel.context, dep, parent_uuid='HOST-RP',
            host_name='host01',
        )
        # The parent passed into ensure_resource_provider should be the
        # host root, not a NUMA sub-RP. We expect exactly one
        # ensure_resource_provider call (for the deployable itself).
        calls = self.placement_mock.ensure_resource_provider.call_args_list
        self.assertEqual(1, len(calls))
        # The deployable's parent_provider_uuid kwarg must be HOST-RP.
        kwargs = calls[0].kwargs
        self.assertEqual('HOST-RP', kwargs['parent_provider_uuid'])

    # ---- Case 2: flag on, numa_node=0 ----
    def test_flag_on_creates_numa_subprovider(self):
        self._set_flag(True)
        dep = _FakeDep('host01_devA', numa_node=0)
        self.cm.get_placement_needed_info_and_report(
            mock.sentinel.context, dep, parent_uuid='HOST-RP',
            host_name='host01',
        )
        # Two ensure_resource_provider calls: one for the NUMA sub-RP,
        # one for the deployable. The deployable's parent must be the
        # NUMA sub-RP's UUID.
        calls = self.placement_mock.ensure_resource_provider.call_args_list
        self.assertEqual(2, len(calls))
        numa_call = calls[0]
        dep_call = calls[1]
        self.assertEqual('host01_numa_0', numa_call.kwargs['name'])
        self.assertEqual('HOST-RP', numa_call.kwargs['parent_provider_uuid'])
        numa_uuid = numa_call.args[1]
        self.assertEqual(numa_uuid, dep_call.kwargs['parent_provider_uuid'])
        # HW_NUMA_ROOT must be tagged on the NUMA sub-RP.
        tag_calls = self.placement_mock.add_traits_to_rp.call_args_list
        self.assertTrue(
            any(c.args == (numa_uuid, ['HW_NUMA_ROOT']) for c in tag_calls),
            'HW_NUMA_ROOT not tagged on NUMA sub-RP; calls=%r' % tag_calls,
        )

    # ---- Case 3: flag on, numa_node=-1 ----
    def test_flag_on_numa_minus_one_no_subprovider(self):
        self._set_flag(True)
        dep = _FakeDep('host01_devA', numa_node=-1)
        self.cm.get_placement_needed_info_and_report(
            mock.sentinel.context, dep, parent_uuid='HOST-RP',
            host_name='host01',
        )
        calls = self.placement_mock.ensure_resource_provider.call_args_list
        # No NUMA sub-RP - just the deployable.
        self.assertEqual(1, len(calls))
        self.assertEqual('HOST-RP', calls[0].kwargs['parent_provider_uuid'])

    # ---- Case 4: flag on, multi-NUMA ----
    def test_flag_on_multi_numa(self):
        self._set_flag(True)
        dep_a = _FakeDep('host01_devA', numa_node=0)
        dep_b = _FakeDep('host01_devB', numa_node=1)
        self.cm.get_placement_needed_info_and_report(
            mock.sentinel.context, dep_a, parent_uuid='HOST-RP',
            host_name='host01',
        )
        self.cm.get_placement_needed_info_and_report(
            mock.sentinel.context, dep_b, parent_uuid='HOST-RP',
            host_name='host01',
        )
        names = [
            c.kwargs.get('name')
            for c in (
                self.placement_mock.ensure_resource_provider.call_args_list
            )
        ]
        # Two distinct NUMA sub-RPs created.
        self.assertIn('host01_numa_0', names)
        self.assertIn('host01_numa_1', names)
        # Sub-RP UUIDs differ.
        numa_uuids = [
            c.args[1]
            for c in (
                self.placement_mock.ensure_resource_provider.call_args_list
            )
            if c.kwargs.get('name', '').startswith('host01_numa_')
        ]
        self.assertEqual(2, len(numa_uuids))
        self.assertEqual(2, len(set(numa_uuids)))

    # ---- Case 5: delete deferred when allocations present ----
    def test_delete_deferred_when_allocated(self):
        self._set_flag(True)
        # in_tree shape: NUMA sub-RP root + the deployable child.
        rp_in_tree = [
            {
                'uuid': 'NUMA-RP', 'name': 'host01_numa_0',
                'parent_provider_uuid': 'HOST-RP',
            },
            {
                'uuid': 'DEP-RP', 'name': 'host01_devA',
                'parent_provider_uuid': 'NUMA-RP',
            },
        ]
        self.placement_mock.get_providers_in_tree.return_value = rp_in_tree
        # The dep-rp has an allocation.
        self.placement_mock.get.return_value = _Resp(
            status_code=200,
            body={'allocations': {'consumer-uuid': {'resources': {'PGPU': 1}}}},
        )
        self.cm._delete_provider_and_sub_providers(
            mock.sentinel.context, 'DEP-RP',
        )
        # delete_provider must NOT have been called.
        self.placement_mock.delete_provider.assert_not_called()
        self.assertIn('DEP-RP', self.cm._deferred_delete_rp_uuids)

    # ---- Case 6: delete succeeds + NUMA gc ----
    def test_delete_succeeds_and_gcs_empty_numa_sub_rp(self):
        self._set_flag(True)
        rp_in_tree = [
            {
                'uuid': 'NUMA-RP', 'name': 'host01_numa_0',
                'parent_provider_uuid': 'HOST-RP',
            },
            {
                'uuid': 'DEP-RP', 'name': 'host01_devA',
                'parent_provider_uuid': 'NUMA-RP',
            },
        ]
        # First call: in_tree for DEP-RP delete. Second call: in_tree
        # for the gc check on NUMA-RP. After DEP-RP is gone, NUMA-RP
        # has no children.
        self.placement_mock.get_providers_in_tree.side_effect = [
            rp_in_tree,
            [
                {
                    'uuid': 'NUMA-RP', 'name': 'host01_numa_0',
                    'parent_provider_uuid': 'HOST-RP',
                },
            ],
        ]
        # No allocations on either RP.
        self.placement_mock.get.return_value = _Resp(
            status_code=200, body={'allocations': {}},
        )
        self.cm._delete_provider_and_sub_providers(
            mock.sentinel.context, 'DEP-RP',
        )
        # DEP-RP deleted; NUMA-RP also gc'd.
        delete_calls = [
            c.args[0]
            for c in self.placement_mock.delete_provider.call_args_list
        ]
        self.assertIn('DEP-RP', delete_calls)
        self.assertIn('NUMA-RP', delete_calls)

    def test_gc_skips_host_root(self):
        """The NUMA gc must never delete a host root RP."""
        self._set_flag(True)
        # Simulate a target whose immediate parent IS the host root,
        # i.e. there is no NUMA sub-RP. Garbage collection should be
        # called on the host-root candidate and refuse.
        host_root = {
            'uuid': 'HOST-RP', 'name': 'host01',
            'parent_provider_uuid': None,
        }
        self.placement_mock.get_providers_in_tree.return_value = [host_root]
        self.placement_mock.get.return_value = _Resp(
            status_code=200, body={'allocations': {}},
        )
        self.cm._maybe_gc_numa_subprovider(
            mock.sentinel.context, 'HOST-RP',
        )
        self.placement_mock.delete_provider.assert_not_called()



class SocketAnchorSubRPTest(base.TestCase):
    """Phase 3: tests for the socket-anchor sub-RP layer above NUMA.

    Tree shape after Phase 3 (when both attrs present, flag on):

        host_root
          └── <host>_socket_<s>  (CUSTOM_SOCKET_ROOT)
                └── <host>_numa_<n>  (HW_NUMA_ROOT)
                      └── deployable
    """

    def setUp(self):
        super().setUp()
        self.placement_mock = self.useFixture(
            fixtures.MockPatch(
                'cyborg.common.placement_client.PlacementClient'
            )
        ).mock.return_value
        self.cm = manager.ConductorManager(
            mock.sentinel.topic, mock.sentinel.host
        )
        self.placement_mock.ensure_resource_provider.side_effect = (
            lambda ctx, uuid_, name=None, parent_provider_uuid=None: uuid_
        )
        self.dep_obj = mock.MagicMock()
        get_by_name = self.useFixture(
            fixtures.MockPatch(
                'cyborg.conductor.manager.Deployable.get_by_name'
            )
        ).mock
        get_by_name.return_value = self.dep_obj

    def _set_flag(self, on):
        from cyborg.conf import CONF
        CONF.set_override('numa_aware_subtree', on, group='placement')
        self.addCleanup(
            CONF.clear_override, 'numa_aware_subtree', group='placement'
        )

    def _ensure_calls_by_name(self):
        out = {}
        for c in self.placement_mock.ensure_resource_provider.call_args_list:
            name = c.kwargs.get('name')
            uid = c.args[1]
            parent = c.kwargs.get('parent_provider_uuid')
            out[name] = (uid, parent)
        return out

    # ---- Case 1: 1P host (single socket, single NUMA) ----
    def test_single_socket_single_numa(self):
        """1P host gets <host>_socket_0 -> <host>_numa_0 -> dep.

        Spec: do NOT special-case 1P. Same tree shape as multi-socket.
        """
        self._set_flag(True)
        dep = _FakeDep('host01_devA', numa_node=0, socket_id=0)
        self.cm.get_placement_needed_info_and_report(
            mock.sentinel.context, dep, parent_uuid='HOST-RP',
            host_name='host01',
        )
        calls = self._ensure_calls_by_name()
        self.assertIn('host01_socket_0', calls)
        self.assertIn('host01_numa_0', calls)
        socket_uuid, socket_parent = calls['host01_socket_0']
        numa_uuid, numa_parent = calls['host01_numa_0']
        # Socket parents under host root, NUMA parents under socket.
        self.assertEqual('HOST-RP', socket_parent)
        self.assertEqual(socket_uuid, numa_parent)
        # CUSTOM_SOCKET_ROOT tagged on socket sub-RP.
        tag_calls = self.placement_mock.add_traits_to_rp.call_args_list
        self.assertTrue(
            any(
                c.args == (socket_uuid, ['CUSTOM_SOCKET_ROOT'])
                for c in tag_calls
            ),
            'CUSTOM_SOCKET_ROOT not tagged; calls=%r' % tag_calls,
        )

    # ---- Case 2: 2P host, 1 NUMA per socket ----
    def test_two_socket_one_numa_each(self):
        self._set_flag(True)
        dep_a = _FakeDep('host01_devA', numa_node=0, socket_id=0)
        dep_b = _FakeDep('host01_devB', numa_node=1, socket_id=1)
        for dep in (dep_a, dep_b):
            self.cm.get_placement_needed_info_and_report(
                mock.sentinel.context, dep, parent_uuid='HOST-RP',
                host_name='host01',
            )
        calls = self._ensure_calls_by_name()
        # Two distinct socket anchors.
        self.assertIn('host01_socket_0', calls)
        self.assertIn('host01_socket_1', calls)
        # Each NUMA anchor parents under its socket.
        s0_uuid, _ = calls['host01_socket_0']
        s1_uuid, _ = calls['host01_socket_1']
        n0_uuid, n0_parent = calls['host01_numa_0']
        n1_uuid, n1_parent = calls['host01_numa_1']
        self.assertEqual(s0_uuid, n0_parent)
        self.assertEqual(s1_uuid, n1_parent)

    # ---- Case 3: 2P host, 2 NUMA per socket (SNC / chiplet) ----
    def test_two_socket_two_numa_per_socket(self):
        self._set_flag(True)
        deps = [
            _FakeDep('host01_devA', numa_node=0, socket_id=0),
            _FakeDep('host01_devB', numa_node=1, socket_id=0),
            _FakeDep('host01_devC', numa_node=2, socket_id=1),
            _FakeDep('host01_devD', numa_node=3, socket_id=1),
        ]
        for dep in deps:
            self.cm.get_placement_needed_info_and_report(
                mock.sentinel.context, dep, parent_uuid='HOST-RP',
                host_name='host01',
            )
        calls = self._ensure_calls_by_name()
        # Four NUMA sub-RPs total, two per socket.
        self.assertIn('host01_numa_0', calls)
        self.assertIn('host01_numa_1', calls)
        self.assertIn('host01_numa_2', calls)
        self.assertIn('host01_numa_3', calls)
        s0_uuid, _ = calls['host01_socket_0']
        s1_uuid, _ = calls['host01_socket_1']
        # NUMA 0, 1 -> socket 0
        for n_name in ('host01_numa_0', 'host01_numa_1'):
            self.assertEqual(s0_uuid, calls[n_name][1])
        # NUMA 2, 3 -> socket 1
        for n_name in ('host01_numa_2', 'host01_numa_3'):
            self.assertEqual(s1_uuid, calls[n_name][1])

    # ---- Case 4: 4P host ----
    def test_four_socket_host(self):
        self._set_flag(True)
        for i in range(4):
            dep = _FakeDep(
                'host01_devS%d' % i, numa_node=i, socket_id=i,
            )
            self.cm.get_placement_needed_info_and_report(
                mock.sentinel.context, dep, parent_uuid='HOST-RP',
                host_name='host01',
            )
        names = [
            c.kwargs.get('name')
            for c in (
                self.placement_mock.ensure_resource_provider.call_args_list
            )
        ]
        for i in range(4):
            self.assertIn('host01_socket_%d' % i, names)
            self.assertIn('host01_numa_%d' % i, names)

    # ---- Case 5: socket_id=-1 (unreadable) -> graceful degradation ----
    def test_socket_unknown_falls_back_to_host_root(self):
        """When socket_id is -1, deployable parents under host root.

        NUMA is also -1 in this case (matches the AMD driver's
        invariant: if you can't read CPU topology you usually can't
        read NUMA either). Verify no socket/numa sub-RPs are created.
        """
        self._set_flag(True)
        dep = _FakeDep('host01_devA', numa_node=-1, socket_id=-1)
        self.cm.get_placement_needed_info_and_report(
            mock.sentinel.context, dep, parent_uuid='HOST-RP',
            host_name='host01',
        )
        calls = self.placement_mock.ensure_resource_provider.call_args_list
        # Only one ensure_resource_provider call (the deployable itself).
        self.assertEqual(1, len(calls))
        self.assertEqual('HOST-RP', calls[0].kwargs['parent_provider_uuid'])

    # ---- Case 5b: socket_id=-1 but numa_node valid (mixed) ----
    def test_socket_unknown_numa_known(self):
        """Defensive: socket -1 but NUMA 0 -> only NUMA sub-RP, no socket.

        Less common but theoretically possible if local_cpulist is
        unreadable but numa_node is. NUMA parents under host root.
        """
        self._set_flag(True)
        dep = _FakeDep('host01_devA', numa_node=0, socket_id=-1)
        self.cm.get_placement_needed_info_and_report(
            mock.sentinel.context, dep, parent_uuid='HOST-RP',
            host_name='host01',
        )
        calls = self._ensure_calls_by_name()
        # NUMA sub-RP created, no socket sub-RP.
        self.assertNotIn('host01_socket_0', calls)
        self.assertIn('host01_numa_0', calls)
        # NUMA parents directly under host root (since socket is -1).
        _, numa_parent = calls['host01_numa_0']
        self.assertEqual('HOST-RP', numa_parent)

    # ---- Case 6: garbage-collect empty socket sub-RP ----
    def test_gc_empty_socket_sub_rp_after_numa_gc(self):
        """When the last NUMA child is gone, the socket sub-RP is gc'd."""
        self._set_flag(True)
        rp_in_tree = [
            {
                'uuid': 'SOCKET-RP', 'name': 'host01_socket_0',
                'parent_provider_uuid': 'HOST-RP',
            },
            {
                'uuid': 'NUMA-RP', 'name': 'host01_numa_0',
                'parent_provider_uuid': 'SOCKET-RP',
            },
            {
                'uuid': 'DEP-RP', 'name': 'host01_devA',
                'parent_provider_uuid': 'NUMA-RP',
            },
        ]
        # in_tree sequence:
        #   1. delete DEP-RP -> in_tree includes DEP-RP + children of DEP-RP
        #   2. gc NUMA-RP candidate -> in_tree includes NUMA-RP + (no children)
        #   3. gc SOCKET-RP candidate (chained) -> in_tree includes SOCKET-RP
        #
        # The candidate-finding logic in _maybe_gc_socket_subprovider
        # also makes a second in_tree fetch.
        self.placement_mock.get_providers_in_tree.side_effect = [
            rp_in_tree,
            # Post DEP-RP gc: NUMA-RP candidate has no children.
            [
                {
                    'uuid': 'NUMA-RP', 'name': 'host01_numa_0',
                    'parent_provider_uuid': 'SOCKET-RP',
                },
            ],
            # Socket gc candidate-find pass: looks up the NUMA-RP
            # tree to find its parent.
            [
                {
                    'uuid': 'SOCKET-RP', 'name': 'host01_socket_0',
                    'parent_provider_uuid': 'HOST-RP',
                },
            ],
            # Second pass (anchor gc): isolated socket RP with no children.
            [
                {
                    'uuid': 'SOCKET-RP', 'name': 'host01_socket_0',
                    'parent_provider_uuid': 'HOST-RP',
                },
            ],
        ]
        self.placement_mock.get.return_value = _Resp(
            status_code=200, body={'allocations': {}},
        )
        self.cm._delete_provider_and_sub_providers(
            mock.sentinel.context, 'DEP-RP',
        )
        delete_calls = [
            c.args[0]
            for c in self.placement_mock.delete_provider.call_args_list
        ]
        self.assertIn('DEP-RP', delete_calls)
        self.assertIn('NUMA-RP', delete_calls)
        self.assertIn('SOCKET-RP', delete_calls)

    # ---- Case 7: socket sub-RP NOT gc'd while sibling NUMA exists ----
    def test_socket_sub_rp_not_gcd_when_other_numa_child_remains(self):
        self._set_flag(True)
        rp_in_tree = [
            {
                'uuid': 'SOCKET-RP', 'name': 'host01_socket_0',
                'parent_provider_uuid': 'HOST-RP',
            },
            {
                'uuid': 'NUMA-RP-A', 'name': 'host01_numa_0',
                'parent_provider_uuid': 'SOCKET-RP',
            },
            {
                'uuid': 'NUMA-RP-B', 'name': 'host01_numa_1',
                'parent_provider_uuid': 'SOCKET-RP',
            },
            {
                'uuid': 'DEP-RP', 'name': 'host01_devA',
                'parent_provider_uuid': 'NUMA-RP-A',
            },
        ]
        # Sequence:
        #   1. delete DEP-RP
        #   2. NUMA gc candidate (NUMA-RP-A) - empty, gets gc'd
        #   3. socket gc candidate-find - sees both A (gone) and B still there
        #
        # The mock just returns the same tree minus the removed
        # entries each time; we simulate by returning sequences.
        self.placement_mock.get_providers_in_tree.side_effect = [
            rp_in_tree,
            # NUMA-A gc: only itself, no children.
            [
                {
                    'uuid': 'NUMA-RP-A', 'name': 'host01_numa_0',
                    'parent_provider_uuid': 'SOCKET-RP',
                },
            ],
            # Socket gc candidate-find: socket has NUMA-RP-B still as a child.
            [
                {
                    'uuid': 'SOCKET-RP', 'name': 'host01_socket_0',
                    'parent_provider_uuid': 'HOST-RP',
                },
                {
                    'uuid': 'NUMA-RP-B', 'name': 'host01_numa_1',
                    'parent_provider_uuid': 'SOCKET-RP',
                },
            ],
        ]
        self.placement_mock.get.return_value = _Resp(
            status_code=200, body={'allocations': {}},
        )
        self.cm._delete_provider_and_sub_providers(
            mock.sentinel.context, 'DEP-RP',
        )
        delete_calls = [
            c.args[0]
            for c in self.placement_mock.delete_provider.call_args_list
        ]
        self.assertIn('DEP-RP', delete_calls)
        self.assertIn('NUMA-RP-A', delete_calls)
        # Socket NOT deleted because NUMA-RP-B is still a child.
        self.assertNotIn('SOCKET-RP', delete_calls)
