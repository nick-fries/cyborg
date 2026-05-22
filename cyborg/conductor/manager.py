# Copyright 2017 Huawei Technologies Co.,LTD.
# All Rights Reserved.
#
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

import uuid

import oslo_messaging as messaging

from oslo_log import log as logging

from cyborg.common import data_migrations
from cyborg.common import exception
from cyborg.common import placement_client
from cyborg.conf import CONF
from cyborg.objects.attach_handle import AttachHandle
from cyborg.objects.attribute import Attribute
from cyborg.objects.control_path import ControlpathID
from cyborg.objects.deployable import Deployable
from cyborg.objects.device import Device
from cyborg.objects.driver_objects.driver_device import DriverDeployable
from cyborg.objects.driver_objects.driver_device import DriverDevice
from cyborg.objects.ext_arq import ExtARQ


LOG = logging.getLogger(__name__)


class ConductorManager:
    """Cyborg Conductor manager main class."""

    RPC_API_VERSION = '1.0'
    target = messaging.Target(version=RPC_API_VERSION)

    def __init__(self, topic, host=None):
        super().__init__()
        self.topic = topic
        self.host = host or CONF.host
        self.placement_client = placement_client.PlacementClient()

    def init_host(self):
        """Hook called on service startup. Heals NULL project_id ARQs."""
        try:
            count = data_migrations.heal_arq_project_ids()
            if count:
                LOG.info(
                    'Conductor startup: healed project_id on %d ARQ(s).', count
                )
        except Exception:
            LOG.exception(
                'Conductor startup: failed to heal ARQ project_ids. '
                'Run cyborg-dbsync online_data_migrations manually.'
            )

    def periodic_tasks(self, context, raise_on_error=False):
        pass

    def device_profile_create(self, context, obj_devprof):
        """Signal to conductor service to create a device_profile.

        :param context: request context.
        :param obj_devprof: a created (but not saved) device_profile object.
        :returns: created device_profile object.
        """
        obj_devprof.create(context)
        return obj_devprof

    def device_profile_delete(self, context, obj_devprof):
        """Signal to conductor service to delete a device_profile.
        :param context: request context.
        :param obj_devprof: a device_profile object to delete.
        """
        obj_devprof.destroy(context)

    def arq_create(self, context, obj_extarq, devprof_id):
        """Signal to conductor service to create an accelerator requests.

        :param context: request context.
        :param obj_extarq: a created (but not saved) accelerator_requests
        object
        :param devprof_id: a device profile id
        :returns: saved accelerator_requests object.
        """
        obj_extarq.create(context, devprof_id)
        return obj_extarq

    def arq_delete_by_uuid(self, context, arqs):
        """Signal to conductor service to delete accelerator requests by
        ARQ UUIDs.

        :param context: request context.
        :param arqs: ARQ UUIDs joined with ','
        """
        arqlist = arqs.split(',')
        ExtARQ.delete_by_uuid(context, arqlist)

    def arq_delete_by_instance_uuid(self, context, instance):
        """Signal to conductor service to delete accelerator requests by
        instance UUID.

        :param context: request context.
        :param instance: UUID of instance whose ARQs need to be deleted
        """
        ExtARQ.delete_by_instance(context, instance)

    def arq_apply_patch(self, context, patch_list, valid_fields):
        """Signal to conductor service to apply patch accelerator requests.

        :param context: request context.
        :param patch_list: A map from ARQ UUIDs to their JSON patches
        :param valid_fields: Dict of valid fields
        """
        ExtARQ.apply_patch(context, patch_list, valid_fields)

    def report_data(self, context, hostname, driver_device_list):
        """Update the Cyborg DB in one hostname according to the
        discovered device list.
        :param context: request context.
        :param hostname: agent's hostname.
        :param driver_device_list: a list of driver_device object
        discovered by agent in the host.
        """
        # TODO(): Every time get from the DB?
        # First retrieve the old_device_list from the DB.
        old_driver_device_list = DriverDevice.list(context, hostname)
        # TODO(wangzhh): Remove invalid driver_devices without controlpath_id.
        # Then diff two driver device list.
        self.drv_device_make_diff(
            context, hostname, old_driver_device_list, driver_device_list
        )

    def drv_device_make_diff(
        self, context, host, old_driver_device_list, new_driver_device_list
    ):
        """Compare new driver-side device object list with the old one in
        one host.
        """
        LOG.info("Start differing devices.")
        # TODO(): The placement report will be implemented here.
        # Use cpid.cpid_info to identify whether the device is the same.
        stub_cpid_list = [
            driver_dev_obj.controlpath_id.cpid_info
            for driver_dev_obj in new_driver_device_list
            if driver_dev_obj.stub
        ]
        new_cpid_list = [
            driver_dev_obj.controlpath_id.cpid_info
            for driver_dev_obj in new_driver_device_list
        ]
        old_cpid_list = [
            driver_dev_obj.controlpath_id.cpid_info
            for driver_dev_obj in old_driver_device_list
        ]
        same = set(new_cpid_list) & set(old_cpid_list) - set(stub_cpid_list)
        added = set(new_cpid_list) - same - set(stub_cpid_list)
        deleted = set(old_cpid_list) - same - set(stub_cpid_list)
        host_rp = self._get_root_provider(context, host)
        # device is deleted.
        for d in deleted:
            old_driver_dev_obj = old_driver_device_list[old_cpid_list.index(d)]
            for driver_dep_obj in old_driver_dev_obj.deployable_list:
                rp_uuid = self.get_rp_uuid_from_obj(driver_dep_obj)
                self._delete_provider_and_sub_providers(context, rp_uuid)
            old_driver_dev_obj.destroy(context, host)
        # device is added
        for a in added:
            new_driver_dev_obj = new_driver_device_list[new_cpid_list.index(a)]
            try:
                new_driver_dev_obj.create(context, host)
            except Exception as exc:
                LOG.exception(
                    "Failed to add device %(device)s. Reason: %(reason)s",
                    {'device': new_driver_dev_obj, 'reason': exc},
                )
                new_driver_dev_obj.destroy(context, host)
            # TODO(All): If report device data to Placement raise exception,
            # we should revert driver device created in Cyborg and resources
            # created in Placement to reduce the risk of data inconsistency
            # here between Cyborg and Placement.
            cleanup_inconsistency_resources = False
            for driver_dep_obj in new_driver_dev_obj.deployable_list:
                try:
                    self.get_placement_needed_info_and_report(
                        context, driver_dep_obj, host_rp,
                        host_name=host,
                    )
                except Exception as exc:
                    LOG.info(
                        "Failed to add device %(device)s. Reason: %(reason)s",
                        {'device': new_driver_dev_obj, 'reason': exc},
                    )
                    cleanup_inconsistency_resources = True
                    break
            if cleanup_inconsistency_resources:
                new_driver_dev_obj.destroy(context, host)
                for driver_dep_obj in new_driver_dev_obj.deployable_list:
                    rp_uuid = self.get_rp_uuid_from_obj(driver_dep_obj)
                    self._delete_provider_and_sub_providers(context, rp_uuid)
        for s in same:
            # get the driver_dev_obj, diff the driver_device layer
            new_driver_dev_obj = new_driver_device_list[new_cpid_list.index(s)]
            old_driver_dev_obj = old_driver_device_list[old_cpid_list.index(s)]
            # First, get dev_obj_list from hostname
            device_obj_list = Device.get_list_by_hostname(context, host)
            # Then, use controlpath_id.cpid_info to identify one Device.
            cpid_info = new_driver_dev_obj.controlpath_id.cpid_info
            for dev_obj in device_obj_list:
                # get cpid_obj, could be empty or only one value.
                cpid_obj = ControlpathID.get_by_device_id_cpidinfo(
                    context, dev_obj.id, cpid_info
                )
                # find the one cpid_obj with cpid_info
                if cpid_obj is not None:
                    break

            changed_key = [
                'std_board_info',
                'vendor',
                'vendor_board_info',
                'model',
                'type',
            ]
            for c_k in changed_key:
                if getattr(new_driver_dev_obj, c_k) != getattr(
                    old_driver_dev_obj, c_k
                ):
                    setattr(dev_obj, c_k, getattr(new_driver_dev_obj, c_k))
            dev_obj.save(context)
            # diff the internal layer: driver_deployable
            self.drv_deployable_make_diff(
                context,
                dev_obj.id,
                cpid_obj.id,
                old_driver_dev_obj.deployable_list,
                new_driver_dev_obj.deployable_list,
                host_rp,
                host_name=host,
            )

    def drv_deployable_make_diff(
        self,
        context,
        device_id,
        cpid_id,
        old_driver_dep_list,
        new_driver_dep_list,
        host_rp,
        host_name=None,
    ):
        """Compare new driver-side deployable object list with the old one in
        one host.
        """
        # use name to identify whether the deployable is the same.
        LOG.info("Start differing deploybles.")
        new_name_list = [
            driver_dep_obj.name for driver_dep_obj in new_driver_dep_list
        ]
        old_name_list = [
            driver_dep_obj.name for driver_dep_obj in old_driver_dep_list
        ]
        same = set(new_name_list) & set(old_name_list)
        added = set(new_name_list) - same
        deleted = set(old_name_list) - same
        # name is deleted.
        for d in deleted:
            old_driver_dep_obj = old_driver_dep_list[old_name_list.index(d)]
            rp_uuid = self.get_rp_uuid_from_obj(old_driver_dep_obj)
            old_driver_dep_obj.destroy(context, device_id)
            self._delete_provider_and_sub_providers(context, rp_uuid)
        # name is added.
        for a in added:
            new_driver_dep_obj = new_driver_dep_list[new_name_list.index(a)]
            new_driver_dep_obj.create(context, device_id, cpid_id)
            try:
                self.get_placement_needed_info_and_report(
                    context, new_driver_dep_obj, host_rp,
                    host_name=host_name,
                )
            except Exception as exc:
                LOG.info(
                    "Failed to add deployable %(deployable)s. "
                    "Reason: %(reason)s",
                    {'deployable': new_driver_dep_obj, 'reason': exc},
                )
                new_driver_dep_obj.destroy(context, device_id)
                rp_uuid = self.get_rp_uuid_from_obj(new_driver_dep_obj)
                # TODO(All): If report deployable data to Placement raise
                # exception, we should revert driver deployable created in
                # Cyborg and resources created in Placement to reduce the risk
                # of data inconsistency here between Cyborg and Placement.
                self._delete_provider_and_sub_providers(context, rp_uuid)
        for s in same:
            # get the driver_dep_obj, diff the driver_dep layer
            new_driver_dep_obj = new_driver_dep_list[new_name_list.index(s)]
            old_driver_dep_obj = old_driver_dep_list[old_name_list.index(s)]
            # get dep_obj, it won't be None because it stored before.
            dep_obj = Deployable.get_by_name_deviceid(context, s, device_id)
            # update the driver_dep num_accelerators field
            if dep_obj.num_accelerators != new_driver_dep_obj.num_accelerators:
                dep_obj.num_accelerators = new_driver_dep_obj.num_accelerators
                dep_obj.save(context)
                rp_uuid = self.get_rp_uuid_from_obj(new_driver_dep_obj)
                attrs = new_driver_dep_obj.attribute_list
                resource_class = [i.value for i in attrs if i.key == 'rc'][0]
                inv_data = _gen_resource_inventory(
                    resource_class, dep_obj.num_accelerators
                )
                self.placement_client.update_inventory(rp_uuid, inv_data)
            # diff the internal layer: driver_attribute_list
            new_attribute_list = []
            if hasattr(new_driver_dep_obj, 'attribute_list'):
                new_attribute_list = new_driver_dep_obj.attribute_list
            self.drv_attr_make_diff(
                context,
                dep_obj.id,
                old_driver_dep_obj.attribute_list,
                new_attribute_list,
            )
            # diff the internal layer: driver_attach_hanle_list
            self.drv_ah_make_diff(
                context,
                dep_obj.id,
                cpid_id,
                old_driver_dep_obj.attach_handle_list,
                new_driver_dep_obj.attach_handle_list,
            )

    def drv_attr_make_diff(
        self, context, dep_id, old_driver_attr_list, new_driver_attr_list
    ):
        """Diff new driver-side Attribute Object lists with the old one."""
        LOG.info("Start differing attributes.")
        dep_obj = Deployable.get_by_id(context, dep_id)
        driver_dep = DriverDeployable.get_by_name(context, dep_obj.name)
        rp_uuid = self.get_rp_uuid_from_obj(driver_dep)
        new_key_list = [
            driver_attr_obj.key for driver_attr_obj in new_driver_attr_list
        ]
        old_key_list = [
            driver_attr_obj.key for driver_attr_obj in old_driver_attr_list
        ]
        same = set(new_key_list) & set(old_key_list)
        # key is deleted.
        deleted = set(old_key_list) - same
        for d in deleted:
            old_driver_attr_obj = old_driver_attr_list[old_key_list.index(d)]
            self.placement_client.delete_trait_by_name(
                context, rp_uuid, old_driver_attr_obj.value
            )
            old_driver_attr_obj.delete_by_key(context, dep_id, d)
        # key is added.
        added = set(new_key_list) - same
        for a in added:
            new_driver_attr_obj = new_driver_attr_list[new_key_list.index(a)]
            new_driver_attr_obj.create(context, dep_id)
            self.placement_client.add_traits_to_rp(
                rp_uuid, [new_driver_attr_obj.value]
            )
        # key is same, diff the value.
        for s in same:
            # value is not same, update
            new_driver_attr_obj = new_driver_attr_list[new_key_list.index(s)]
            old_driver_attr_obj = old_driver_attr_list[old_key_list.index(s)]
            if new_driver_attr_obj.value != old_driver_attr_obj.value:
                attr_obj = Attribute.get_by_dep_key(context, dep_id, s)
                attr_obj.value = new_driver_attr_obj.value
                attr_obj.save(context)
                # Update traits here.
                if new_driver_attr_obj.key.startswith("trait"):
                    self.placement_client.delete_trait_by_name(
                        context, rp_uuid, old_driver_attr_obj.value
                    )
                    self.placement_client.add_traits_to_rp(
                        rp_uuid, [new_driver_attr_obj.value]
                    )
                # Update resource classes here.
                if new_driver_attr_obj.key.startswith("rc"):
                    self.placement_client.ensure_resource_classes(
                        context, [new_driver_attr_obj.value]
                    )
                    inv_data = _gen_resource_inventory(
                        new_driver_attr_obj.value, dep_obj.num_accelerators
                    )
                    self.placement_client.update_inventory(rp_uuid, inv_data)
                    self.placement_client.delete_rc_by_name(
                        context, old_driver_attr_obj.value
                    )

    @classmethod
    def drv_ah_make_diff(
        cls, context, dep_id, cpid_id, old_driver_ah_list, new_driver_ah_list
    ):
        """Diff new driver-side AttachHandle Object lists with the old one."""
        LOG.info("Start differing attach_handles.")
        new_info_list = [
            driver_ah_obj.attach_info for driver_ah_obj in new_driver_ah_list
        ]
        old_info_list = [
            driver_ah_obj.attach_info for driver_ah_obj in old_driver_ah_list
        ]
        same = set(new_info_list) & set(old_info_list)
        LOG.info('new info list %s', new_info_list)
        LOG.info('old info list %s', old_info_list)
        # attach_info is deleted.
        deleted = set(old_info_list) - same
        for d in deleted:
            old_driver_ah_obj = old_driver_ah_list[old_info_list.index(d)]
            old_driver_ah_obj.destroy(context, dep_id)
        # attach_info is added.
        added = set(new_info_list) - same
        for a in added:
            new_driver_ah_obj = new_driver_ah_list[new_info_list.index(a)]
            new_driver_ah_obj.create(context, dep_id, cpid_id)
        # attach-info is same
        for s in same:
            # get attach_handle obj
            new_driver_ah_obj = new_driver_ah_list[new_info_list.index(s)]
            old_driver_ah_obj = old_driver_ah_list[old_info_list.index(s)]
            changed_key = ['attach_type']
            ah_obj = AttachHandle.get_ah_by_depid_attachinfo(
                context, dep_id, s
            )
            for c_k in changed_key:
                if getattr(new_driver_ah_obj, c_k) != getattr(
                    old_driver_ah_obj, c_k
                ):
                    setattr(ah_obj, c_k, getattr(new_driver_ah_obj, c_k))
            ah_obj.save(context)

    def _get_root_provider(self, context, hostname):
        try:
            provider = self.placement_client.get(
                "resource_providers?name=" + hostname
            ).json()
            pr_uuid = provider["resource_providers"][0]["uuid"]
            return pr_uuid
        except (IndexError, KeyError):
            raise exception.PlacementResourceProviderNotFound(
                resource_provider=hostname
            )

    def _get_sub_provider(self, context, parent, name):
        old_sub_pr_uuid = str(uuid.uuid3(uuid.NAMESPACE_DNS, str(name)))
        new_sub_pr_uuid = self.placement_client.ensure_resource_provider(
            context, old_sub_pr_uuid, name=name, parent_provider_uuid=parent
        )
        if old_sub_pr_uuid == new_sub_pr_uuid:
            return new_sub_pr_uuid
        else:
            raise exception.Conflict()

    # ---- Phase 2 (PLAN-amd-v620.md §8.4): NUMA-aware sub-RP layer ----
    # When [placement] numa_aware_subtree is True, deployable RPs are
    # parented under a per-NUMA sub-RP named "<host>_numa_<n>" so that
    # Nova's same_subtree= queries can co-locate GPU + NIC + CPU on the
    # same NUMA node. Driver-agnostic: any driver that emits a generic
    # ``numa_node`` DriverAttribute participates.
    #
    # DROP WHEN NOVA LANDS nova-spec-numa-topology-with-rps.

    @staticmethod
    def _numa_aware_enabled():
        return bool(getattr(CONF.placement, 'numa_aware_subtree', False))

    @staticmethod
    def _extract_numa_node(obj):
        """Read the generic ``numa_node`` DriverAttribute from a dep obj.

        Returns the int value when present and parseable, else None.
        Value ``-1`` is normalized to None ("no NUMA affinity").
        """
        for attr in getattr(obj, 'attribute_list', []) or []:
            if getattr(attr, 'key', None) == 'numa_node':
                try:
                    n = int(attr.value)
                except (TypeError, ValueError):
                    return None
                return n if n >= 0 else None
        return None

    @staticmethod
    def _extract_socket_id(obj):
        """Read the generic ``socket_id`` DriverAttribute from a dep obj.

        Phase 3 (PLAN-amd-v620.md §8.5): mirrors ``_extract_numa_node``
        for the new per-CPU-socket anchor layer. ``-1`` normalizes to
        None ("socket unknown"); callers must treat None as "no
        socket anchor, parent under host root directly".
        """
        for attr in getattr(obj, 'attribute_list', []) or []:
            if getattr(attr, 'key', None) == 'socket_id':
                try:
                    n = int(attr.value)
                except (TypeError, ValueError):
                    return None
                return n if n >= 0 else None
        return None

    @staticmethod
    def _numa_subprovider_name(host_name, numa_node):
        """Return the canonical "<host>_numa_<n>" sub-RP name."""
        return "%s_numa_%d" % (host_name, int(numa_node))

    @staticmethod
    def _socket_subprovider_name(host_name, socket_id):
        """Return the canonical "<host>_socket_<n>" sub-RP name."""
        return "%s_socket_%d" % (host_name, int(socket_id))

    def _get_or_create_numa_subprovider(
        self, context, parent_rp_uuid, host_name, numa_node,
    ):
        """Idempotently return the UUID of the per-NUMA sub-RP.

        For ``numa_node == -1`` (no NUMA affinity) returns
        ``parent_rp_uuid`` unchanged - the deployable then parents
        directly under whatever was passed in (host root, or in Phase
        3 the new ``<host>_socket_<n>`` anchor when one was created),
        matching today's degraded behavior.

        Phase 3 change: the second positional argument was renamed
        from ``host_rp_uuid`` to ``parent_rp_uuid`` because the parent
        is no longer always the host root. When the new socket
        anchor layer is active, callers pass the socket sub-RP UUID
        here so NUMA sub-RPs become children of the socket anchor
        rather than direct children of the host root. The host_name
        argument is still used to construct the deterministic
        sub-RP name (and therefore its deterministic UUID) so the
        NUMA RP UUID is stable across socket/no-socket configurations.

        Otherwise computes a deterministic UUID from the sub-RP name
        (uuid3 of "<host>_numa_<n>"), ensures the RP exists under the
        passed-in parent via placement_client.ensure_resource_provider,
        and tags it with the standard ``HW_NUMA_ROOT`` os-traits trait
        so that downstream services know this RP represents a NUMA node.
        """
        if numa_node is None or int(numa_node) < 0:
            return parent_rp_uuid
        sub_name = self._numa_subprovider_name(host_name, numa_node)
        deterministic_uuid = str(
            uuid.uuid3(uuid.NAMESPACE_DNS, sub_name)
        )
        sub_uuid = self.placement_client.ensure_resource_provider(
            context,
            deterministic_uuid,
            name=sub_name,
            parent_provider_uuid=parent_rp_uuid,
        )
        # ensure_resource_provider returns the existing UUID if a RP
        # with that UUID already exists. If the deterministic UUID we
        # computed clashes with something else (unlikely - we hash the
        # sub-RP's name) treat it as a hard conflict.
        if sub_uuid != deterministic_uuid:
            raise exception.Conflict()
        # Tag the sub-RP as a NUMA root. add_traits_to_rp is idempotent.
        try:
            self.placement_client.add_traits_to_rp(
                sub_uuid, ['HW_NUMA_ROOT'],
            )
        except Exception as exc:
            # Trait tagging failure is non-fatal - the RP exists and is
            # usable. Log and move on; tag will retry on next report.
            LOG.warning(
                "Failed to tag NUMA sub-RP %(uuid)s (%(name)s) with "
                "HW_NUMA_ROOT: %(err)s",
                {'uuid': sub_uuid, 'name': sub_name, 'err': exc},
            )
        return sub_uuid

    def _get_or_create_socket_subprovider(
        self, context, host_rp_uuid, host_name, socket_id,
    ):
        """Idempotently return the UUID of the per-socket sub-RP.

        Phase 3 (PLAN-amd-v620.md §8.5): the socket anchor sits
        between the compute host root and the NUMA sub-RPs from
        Phase 2. The result is a host -> socket -> NUMA -> device
        tree that lets Nova co-locate accel+NIC at either socket or
        NUMA granularity via Placement ``same_subtree=`` queries.

        For ``socket_id == -1`` (unreadable) returns ``host_rp_uuid``
        unchanged - the NUMA sub-RP (or the deployable, when
        ``numa_node == -1`` too) then parents directly under the
        host root, matching the pre-Phase-3 degraded shape.
        Single-socket hosts (``socket_id == 0``) get a single
        ``<host>_socket_0`` anchor - we do NOT special-case the 1P
        case, because that would mean 1P and 2P hosts have
        different tree shapes for no operational benefit.

        Computes a deterministic UUID from the sub-RP name
        (uuid3 of "<host>_socket_<n>"), ensures the RP exists
        under the host root via
        ``placement_client.ensure_resource_provider``, and tags it
        with the ``CUSTOM_SOCKET_ROOT`` trait so downstream services
        (including the Nova ``TopologyAffinityFilter`` in the
        companion patch) can walk the tree and recognize the anchor.
        """
        if socket_id is None or int(socket_id) < 0:
            return host_rp_uuid
        sub_name = self._socket_subprovider_name(host_name, socket_id)
        deterministic_uuid = str(
            uuid.uuid3(uuid.NAMESPACE_DNS, sub_name)
        )
        sub_uuid = self.placement_client.ensure_resource_provider(
            context,
            deterministic_uuid,
            name=sub_name,
            parent_provider_uuid=host_rp_uuid,
        )
        if sub_uuid != deterministic_uuid:
            raise exception.Conflict()
        # Tag the socket anchor. CUSTOM_SOCKET_ROOT is not (yet) part
        # of os-traits proper; using a CUSTOM_ prefix is the
        # documented escape hatch for site-defined traits and is
        # idempotent through ``add_traits_to_rp``.
        try:
            self.placement_client.add_traits_to_rp(
                sub_uuid, ['CUSTOM_SOCKET_ROOT'],
            )
        except Exception as exc:
            LOG.warning(
                "Failed to tag socket sub-RP %(uuid)s (%(name)s) "
                "with CUSTOM_SOCKET_ROOT: %(err)s",
                {'uuid': sub_uuid, 'name': sub_name, 'err': exc},
            )
        return sub_uuid

    def provider_report(
        self,
        context,
        name,
        resource_class,
        traits,
        total,
        parent,
    ):
        self.placement_client.ensure_resource_classes(
            context, [resource_class]
        )
        sub_pr_uuid = self._get_sub_provider(context, parent, name)
        result = _gen_resource_inventory(resource_class, total)
        self.placement_client.update_inventory(sub_pr_uuid, result)
        # traits = ["CUSTOM_FPGA_INTEL", "CUSTOM_FPGA_INTEL_ARRIA10",
        #           "CUSTOM_FPGA_INTEL_REGION_UUID",
        #           "CUSTOM_FPGA_FUNCTION_ID_INTEL_UUID",
        #           "CUSTOM_PROGRAMMABLE",
        #           "CUSTOM_FPGA_NETWORK"]
        self.placement_client.add_traits_to_rp(sub_pr_uuid, traits)
        return sub_pr_uuid

    def get_placement_needed_info_and_report(
        self, context, obj, parent_uuid=None, host_name=None,
    ):
        pr_name = obj.name
        attrs = obj.attribute_list
        resource_class = [i.value for i in attrs if i.key == 'rc'][0]
        traits = [i.value for i in attrs if str(i.key).startswith("trait")]
        total = obj.num_accelerators

        # Phase 2 + Phase 3: optionally interpose a per-socket and
        # per-NUMA sub-RP between the host root and the deployable RP.
        # Gated on the config flag and on the driver having emitted
        # usable socket_id / numa_node attributes. The shape is:
        #
        #   host_root
        #     └── <host>_socket_<s>   (CUSTOM_SOCKET_ROOT)   <- Phase 3
        #           └── <host>_numa_<n>  (HW_NUMA_ROOT)      <- Phase 2
        #                 └── deployable
        #
        # On 1P hosts the socket anchor is still created (socket_0) -
        # we intentionally do NOT special-case 1P, so the tree shape
        # is identical regardless of socket count. ``socket_id == -1``
        # (unreadable) gracefully degrades to today's flat-or-NUMA
        # shape because ``_get_or_create_socket_subprovider`` returns
        # the host root unchanged for negative socket ids.
        effective_parent = parent_uuid
        if (
            self._numa_aware_enabled()
            and parent_uuid is not None
            and host_name is not None
        ):
            socket_id = self._extract_socket_id(obj)
            if socket_id is not None:
                effective_parent = self._get_or_create_socket_subprovider(
                    context, effective_parent, host_name, socket_id,
                )
            numa_node = self._extract_numa_node(obj)
            if numa_node is not None:
                effective_parent = self._get_or_create_numa_subprovider(
                    context, effective_parent, host_name, numa_node,
                )

        rp_uuid = self.provider_report(
            context, pr_name, resource_class, traits, total,
            effective_parent,
        )
        dep_obj = Deployable.get_by_name(context, pr_name)
        dep_obj["rp_uuid"] = rp_uuid
        dep_obj.save(context)

    def get_rp_uuid_from_obj(self, obj):
        return str(uuid.uuid3(uuid.NAMESPACE_DNS, str(obj.name)))

    def _has_allocations(self, context, rp_uuid):
        """Return True if any consumer currently holds resources on rp_uuid.

        Phase 2 (PLAN-amd-v620.md §8.4): used by the deferred-RP-delete
        guard. We never delete an RP that has live allocations - doing
        so would orphan a Nova instance's hold. Returns False on any
        client error (caller falls through to the legacy delete path,
        which itself surfaces 409 if Placement rejects).
        """
        try:
            resp = self.placement_client.get(
                "/resource_providers/%s/allocations" % rp_uuid,
            )
        except Exception as exc:
            LOG.warning(
                "Failed to query allocations for RP %(uuid)s: %(err)s; "
                "assuming none.",
                {'uuid': rp_uuid, 'err': exc},
            )
            return False
        if resp is None or getattr(resp, 'status_code', 500) != 200:
            return False
        try:
            body = resp.json()
        except Exception:
            return False
        return bool(body.get('allocations'))

    # In-memory set of RP UUIDs whose delete was deferred because they
    # had live allocations. The agent re-issues report_data periodically;
    # next pass the conductor will retry. A future improvement (out of
    # scope for Phase 2) would persist this in the DB; the in-memory
    # variant is sufficient because all deferred RPs are also still
    # present in Cyborg's device/deployable tables and naturally
    # re-attempt deletion on subsequent reconciles.
    _deferred_delete_rp_uuids: set = set()

    def _delete_provider_and_sub_providers(self, context, rp_uuid):
        rp_in_tree = self.placement_client.get_providers_in_tree(
            context, rp_uuid
        )
        # Phase 2: identify potential NUMA-sub-RP parents that may
        # become eligible for garbage collection once this rp_uuid and
        # its children are gone. We compute the set BEFORE delete by
        # looking at the parent_provider_uuid of rp_uuid (the entry
        # whose ``uuid == rp_uuid``).
        parent_uuid_of_target = None
        if self._numa_aware_enabled():
            for rp in rp_in_tree:
                if rp["uuid"] == rp_uuid:
                    parent_uuid_of_target = rp.get("parent_provider_uuid")
                    break

        deferred_any = False
        for rp in rp_in_tree[::-1]:
            if rp["parent_provider_uuid"] == rp_uuid or rp["uuid"] == rp_uuid:
                # Deferred-delete guard: never delete an RP with live
                # allocations. We log + record + skip, then continue
                # the iteration so we still try to delete other (non-
                # allocated) RPs in the subtree. The next reconcile
                # cycle naturally retries because the device/deployable
                # remains in the Cyborg DB.
                if self._has_allocations(context, rp["uuid"]):
                    LOG.warning(
                        "Deferred RP delete for %(uuid)s: allocations "
                        "present; will retry on next reconcile.",
                        {'uuid': rp["uuid"]},
                    )
                    self._deferred_delete_rp_uuids.add(rp["uuid"])
                    deferred_any = True
                    if rp["uuid"] == rp_uuid:
                        # Can't proceed any further up the chain since
                        # the target itself can't go.
                        break
                    continue
                self.placement_client.delete_provider(rp["uuid"])
                LOG.info(
                    "Successfully delete resource provider %(rp_uuid)s",
                    {"rp_uuid": rp["uuid"]},
                )
                self._deferred_delete_rp_uuids.discard(rp["uuid"])
                if rp["uuid"] == rp_uuid:
                    break

        # Phase 2: garbage-collect the parent NUMA sub-RP when it is
        # now childless. Only applies when (a) the flag is on, (b)
        # the immediate parent is not the host root (host roots are
        # owned by nova-compute and must never be deleted by Cyborg),
        # and (c) no allocations remain on the parent. We detect "is
        # a host root" heuristically: a host root has no parent of
        # its own. We compare via a fresh in_tree fetch rooted at the
        # parent.
        if (
            self._numa_aware_enabled()
            and parent_uuid_of_target
            and not deferred_any
        ):
            # Phase 2: gc the NUMA sub-RP if it is now empty.
            self._maybe_gc_numa_subprovider(context, parent_uuid_of_target)
            # Phase 3: if the NUMA sub-RP got gc'd, its own parent (the
            # socket sub-RP) may now be empty too. Chain the gc up one
            # level. ``_maybe_gc_socket_subprovider`` is safe to call
            # unconditionally - it short-circuits if the candidate is
            # not actually a socket anchor or still has children.
            self._maybe_gc_socket_subprovider(
                context, parent_uuid_of_target,
            )

    def _maybe_gc_socket_subprovider(self, context, child_uuid):
        """Garbage-collect an empty socket sub-RP after a NUMA gc.

        Phase 3 helper. ``child_uuid`` is the UUID of an RP that was
        a child of the socket sub-RP we want to gc. We look up its
        current row in Placement to find its parent (the socket
        anchor) and, if the parent is in fact a socket anchor with
        no remaining children and no allocations, delete it.

        The function is intentionally lenient: any failure - the
        child has already been deleted, the parent is not a socket
        anchor, the parent still has siblings - is a silent no-op.
        The next reconcile naturally retries if needed.
        """
        # First find the socket-anchor candidate by looking up the
        # child's parent_provider_uuid via the in-tree fetch.
        try:
            in_tree = self.placement_client.get_providers_in_tree(
                context, child_uuid,
            )
        except Exception as exc:
            LOG.debug(
                "Socket sub-RP gc skipped: in_tree for %(uuid)s "
                "failed: %(err)s",
                {'uuid': child_uuid, 'err': exc},
            )
            return
        # If the child still exists, its parent is the candidate.
        # If it doesn't (NUMA gc succeeded), look up the parent we
        # captured during the original in_tree at the top of
        # _delete_provider_and_sub_providers - but we don't have it
        # here, so fall through: the parent candidate is the entry
        # in in_tree whose ``children == 0`` and whose name matches
        # the socket pattern. We probe each candidate.
        candidates = set()
        for rp in in_tree:
            if rp["uuid"] == child_uuid:
                pp = rp.get("parent_provider_uuid")
                if pp:
                    candidates.add(pp)
        # Independently scan: any RP in the tree whose name matches
        # the socket pattern and has no children is also a candidate
        # (covers the case where the NUMA gc already removed the
        # child by the time we got here).
        children_by_parent = {}
        for rp in in_tree:
            pp = rp.get("parent_provider_uuid")
            if pp:
                children_by_parent.setdefault(pp, 0)
                children_by_parent[pp] += 1
        for rp in in_tree:
            name = rp.get("name") or ""
            if "_socket_" not in name:
                continue
            if children_by_parent.get(rp["uuid"], 0) == 0:
                candidates.add(rp["uuid"])

        for candidate in candidates:
            self._maybe_gc_anchor_subprovider(
                context, candidate, name_marker="_socket_",
            )

    def _maybe_gc_anchor_subprovider(
        self, context, candidate_uuid, name_marker,
    ):
        """Generic empty-anchor RP gc used by both NUMA + socket layers.

        Phase 3 refactor of the Phase 2 NUMA gc. Safe to call on any
        UUID: short-circuits if the RP is not an anchor (no parent,
        or name doesn't contain ``name_marker``), still has children,
        or has live allocations. Failures are logged and swallowed.
        """
        try:
            in_tree = self.placement_client.get_providers_in_tree(
                context, candidate_uuid,
            )
        except Exception as exc:
            LOG.debug(
                "Anchor sub-RP gc skipped for %(uuid)s: in_tree "
                "fetch failed: %(err)s",
                {'uuid': candidate_uuid, 'err': exc},
            )
            return

        target = None
        children = []
        for rp in in_tree:
            if rp["uuid"] == candidate_uuid:
                target = rp
            elif rp.get("parent_provider_uuid") == candidate_uuid:
                children.append(rp)
        if target is None:
            return
        # Never gc a host root (parent_provider_uuid is None).
        if not target.get("parent_provider_uuid"):
            return
        # Never gc if it still has any children.
        if children:
            return
        # Never gc if it has live allocations.
        if self._has_allocations(context, candidate_uuid):
            LOG.info(
                "Skipping anchor sub-RP gc for %(uuid)s: live "
                "allocations.",
                {'uuid': candidate_uuid},
            )
            return
        # Belt-and-suspenders: name marker must match (avoids gc'ing
        # an unrelated sub-RP whose UUID happened to be passed in).
        name = target.get("name") or ""
        if name_marker not in name:
            return
        try:
            self.placement_client.delete_provider(candidate_uuid)
            LOG.info(
                "Garbage-collected empty anchor sub-RP %(uuid)s "
                "(%(name)s).",
                {'uuid': candidate_uuid, 'name': name},
            )
        except Exception as exc:
            LOG.warning(
                "Failed to gc anchor sub-RP %(uuid)s: %(err)s",
                {'uuid': candidate_uuid, 'err': exc},
            )

    def _maybe_gc_numa_subprovider(self, context, candidate_uuid):
        """Delete a NUMA sub-RP if it is empty and not a host root.

        Phase 2 helper, Phase 3 refactor: now delegates to the generic
        ``_maybe_gc_anchor_subprovider`` with ``name_marker="_numa_"``.
        Kept as a separate method so test code and future callers can
        target the NUMA layer explicitly.
        """
        self._maybe_gc_anchor_subprovider(
            context, candidate_uuid, name_marker="_numa_",
        )


def _gen_resource_inventory(resource_class, total):
    return {
        resource_class: {
            'total': total,
            'max_unit': total,
        },
    }
