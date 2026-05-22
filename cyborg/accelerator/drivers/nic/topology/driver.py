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


"""
Cyborg vendor-agnostic NIC topology driver.

Phase 3 (PLAN-amd-v620.md §8.5, PRIMER §7). This driver is NOT a
conventional vendor NIC driver - it does NOT enroll Neutron-managed
NICs as Cyborg deployables. Instead it:

1. Discovers every network-class PCI Physical Function on the host
   (PCI class ``0x02xx``; vendor-agnostic).
2. Derives each PF's CPU socket and NUMA node from sysfs.
3. PATCHes ``CUSTOM_TOPO_SOCKET<n>`` and ``CUSTOM_TOPO_NUMA<n>``
   traits onto whatever Placement resource provider Neutron has
   already written for the PF, by matching on the BDF embedded in
   the RP name.

The result is that Neutron's existing flat NIC RPs gain
locality metadata that the companion Nova ``TopologyAffinityFilter``
can read to enforce ``hw:cyborg_locality=socket`` /
``hw:cyborg_locality=numa`` flavor extra-specs. This bridges
Cyborg-managed accelerator topology to Neutron-managed NIC RPs
without modifying Neutron's RP shape or moving NIC RPs into
Cyborg's tree (see PRIMER Part 7 "Option A").

``discover()`` deliberately returns ``[]``: this driver does not
emit Cyborg DriverDevices.
"""

from oslo_log import log as logging

from cyborg.accelerator.drivers.driver import GenericDriver
from cyborg.accelerator.drivers.nic.topology import sysinfo


LOG = logging.getLogger(__name__)


class NICTopologyDriver(GenericDriver):
    """Vendor-agnostic NIC topology trait writer.

    Inherits from :class:`GenericDriver` (the same base the in-tree
    fake driver uses) so the existing stevedore-based agent
    machinery picks it up via the
    ``cyborg.accelerator.driver`` entry-point namespace. ``VENDOR``
    is the sentinel string ``"topology"`` to signal that this driver
    is intentionally cross-vendor: it does not derive from
    :class:`cyborg.accelerator.drivers.nic.base.NICDriver` because
    that base assumes a single-vendor relationship and forces a
    ``VENDOR_MAPS`` lookup by PCI vendor id, neither of which fits
    here.
    """

    VENDOR = "topology"

    def discover(self):
        """Discover network PFs, derive topology, PATCH Neutron RPs.

        :returns: An empty list - this driver does NOT emit Cyborg
                  ``DriverDevice`` instances. Its purpose is purely
                  to PATCH locality traits onto Neutron-written
                  Placement RPs. An empty return is what the agent's
                  ``resource_tracker.update_usage`` expects when a
                  driver contributes no devices.
        """
        try:
            sysinfo.discover()
        except Exception:
            # Never let driver discovery raise into the agent loop -
            # a broken NIC topology pass must not stop other driver
            # discoveries on the same host.
            LOG.exception(
                "NIC topology discovery failed; no traits will be "
                "PATCHed this cycle."
            )
        return []

    def update(self, control_path, image_path):
        """No firmware update path - this driver is read/PATCH only."""
        raise NotImplementedError(
            "NICTopologyDriver does not support firmware updates."
        )

    def get_stats(self):
        """No stats - this driver only PATCHes traits."""
        return {}
