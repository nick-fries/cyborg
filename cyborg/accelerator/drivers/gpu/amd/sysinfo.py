# Copyright 2026 V620 Driver Implementation.
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
Cyborg AMD GPU driver implementation - V620 discovery.

Architecture B (Cyborg owns inventory and scheduling) - see
PLAN-amd-v620.md sections 1, 2, 4, 7.

PF and VF representation are mutually exclusive at the source:

* If a PF has ``sriov_numvfs == 0`` (gim not loaded) the PF is emitted
  as a single ``DriverDevice``; no VFs exist.
* If a PF has ``sriov_numvfs > 0`` one ``DriverDevice`` is emitted per
  VF; the PF itself is skipped.

NUMA awareness is surfaced two ways:

* A per-deployable ``CUSTOM_AMD_V620_NUMA<n>`` / ``CUSTOM_AMD_V620_NUMA_NONE``
  trait, for flavor-side filtering ("give me a VF on NUMA 0").
* A generic ``numa_node`` DriverAttribute (Phase 2). The Cyborg
  conductor reads this attribute (driver-agnostic) and parents the
  deployable RP under a per-NUMA sub-RP when
  ``[placement] numa_aware_subtree`` is enabled. See PLAN-amd-v620.md
  §8.4 and the conductor docstring on
  ``_get_or_create_numa_subprovider``.
"""

import os

from oslo_log import log as logging
from oslo_serialization import jsonutils

from cyborg.accelerator.common import utils
from cyborg.accelerator.drivers.gpu import utils as gpu_utils
from cyborg.common import constants
from cyborg.conf import CONF
from cyborg.objects.driver_objects import driver_attach_handle
from cyborg.objects.driver_objects import driver_attribute
from cyborg.objects.driver_objects import driver_controlpath_id
from cyborg.objects.driver_objects import driver_deployable
from cyborg.objects.driver_objects import driver_device


LOG = logging.getLogger(__name__)


# Default V620 product IDs. These are also the defaults of the
# ``[gpu_devices] enabled_amd_pf_product_ids`` and
# ``enabled_amd_vf_product_ids`` ListOpts; defined here so the discovery
# logic has a sane default even if the operator wipes the config keys.
_DEFAULT_PF_PRODUCT_IDS = ["73a1"]
_DEFAULT_VF_PRODUCT_IDS = ["73ae"]

# Trait constants. ``CUSTOM_AMD_V620`` is emitted on every V620
# deployable (PF or VF); the *_PF / *_VF / *_MXGPU traits differentiate.
_TRAIT_OWNER_CYBORG = "OWNER_CYBORG"
_TRAIT_AMD_V620 = "CUSTOM_AMD_V620"
_TRAIT_AMD_V620_PF = "CUSTOM_AMD_V620_PF"
_TRAIT_AMD_V620_VF = "CUSTOM_AMD_V620_VF"
_TRAIT_AMD_MXGPU = "CUSTOM_AMD_MXGPU"
_TRAIT_NUMA_NONE = "CUSTOM_AMD_V620_NUMA_NONE"
_TRAIT_NUMA_PREFIX = "CUSTOM_AMD_V620_NUMA"
# Phase 3 (PLAN-amd-v620.md §8.5): per-deployable socket trait, mirroring
# the NUMA trait pattern. ``CUSTOM_AMD_V620_SOCKET_NONE`` is emitted when
# the socket cannot be read; ``CUSTOM_AMD_V620_SOCKET<n>`` carries the
# physical_package_id integer for flavor-side filtering. The generic
# ``socket_id`` DriverAttribute (added in ``_generate_attribute_list``)
# is what the Cyborg conductor actually keys on when interposing a
# ``<host>_socket_<n>`` sub-RP above the NUMA sub-RP.
_TRAIT_SOCKET_NONE = "CUSTOM_AMD_V620_SOCKET_NONE"
_TRAIT_SOCKET_PREFIX = "CUSTOM_AMD_V620_SOCKET"


def _read_numa_node(bdf):
    """Return the NUMA node id (int) for a PCI device, or None.

    Reads ``/sys/bus/pci/devices/<bdf>/numa_node``. A value of -1 in
    sysfs (meaning "no NUMA affinity") is normalized to the host's
    sole NUMA node when exactly one
    ``/sys/devices/system/node/node<N>`` exists (single-socket
    firmware routinely omits ACPI ``_PXM``, leaving -1 on every
    device), and to None otherwise. Any OSError is caught and logged
    - this function never raises.
    """
    path = '/sys/bus/pci/devices/%s/numa_node' % bdf
    try:
        with open(path) as f:
            raw = f.read().strip()
    except OSError as e:
        LOG.warning(
            'Failed to read numa_node for device %s at %s: %s',
            bdf, path, e,
        )
        return None
    try:
        value = int(raw)
    except ValueError:
        LOG.warning(
            'Unexpected numa_node content for device %s: %r',
            bdf, raw,
        )
        return None
    if value < 0:
        # Gated single-NUMA fallback: unambiguous only when the host
        # exposes exactly one NUMA node; otherwise stay "unknown".
        return gpu_utils.get_sole_numa_node()
    return value


def _read_socket_id(bdf):
    """Return the CPU socket id (int) for a PCI device, or None.

    Phase 3: thin wrapper around ``gpu_utils.get_socket_id`` so the
    AMD driver matches the structure of ``_read_numa_node`` while
    leaving the actual sysfs walk in the shared GPU utils module
    (where the NIC topology driver also uses it).

    Reads ``/sys/bus/pci/devices/<bdf>/local_cpulist`` to find the
    first local CPU and resolves its ``physical_package_id``.
    Returns None on any failure - caller must treat None as
    "socket unknown".
    """
    try:
        return gpu_utils.get_socket_id(bdf)
    except Exception as e:
        LOG.warning(
            'Failed to read socket_id for device %s: %s', bdf, e,
        )
        return None


def _read_sriov_numvfs(bdf):
    """Return the current SR-IOV VF count of a PF, or 0 on error.

    Reads ``/sys/bus/pci/devices/<bdf>/sriov_numvfs``. A missing or
    unreadable file is treated as zero VFs (gim not loaded, or the
    device is not SR-IOV-capable).
    """
    path = '/sys/bus/pci/devices/%s/sriov_numvfs' % bdf
    try:
        with open(path) as f:
            raw = f.read().strip()
    except OSError as e:
        LOG.debug(
            'sriov_numvfs not readable for %s at %s (%s); '
            'treating as zero VFs.',
            bdf, path, e,
        )
        return 0
    try:
        return int(raw)
    except ValueError:
        LOG.warning(
            'Unexpected sriov_numvfs content for %s: %r', bdf, raw,
        )
        return 0


def _get_traits_and_numa(bdf, role):
    """Build the trait list AND raw numa_node + socket_id for a V620 deployable.

    :param bdf: PCI BDF of the deployable's device (used to read
                NUMA + socket).
    :param role: ``"PF"`` or ``"VF"``.
    :returns: dict with ``"traits"`` (list[str]), ``"numa_node"``
              (``str(int)``; ``"-1"`` for no NUMA affinity), and
              ``"socket_id"`` (``str(int)``; ``"-1"`` if unreadable).

    The generic ``numa_node`` and ``socket_id`` values are exposed as
    DriverAttributes (see ``_generate_attribute_list``) so the Cyborg
    conductor's topology-aware sub-RP layer (PLAN §8.4 + §8.5) can
    parent the deployable RP under a ``<host>_socket_<n>`` ->
    ``<host>_numa_<n>`` chain. Driver-agnostic by contract: any future
    driver that emits these keys participates.

    The ``CUSTOM_AMD_V620_NUMA<n>`` and ``CUSTOM_AMD_V620_SOCKET<n>``
    traits are retained for flavor-side filtering (operator can pin
    by trait); the generic attributes are for conductor consumption.
    """
    traits = [_TRAIT_OWNER_CYBORG, _TRAIT_AMD_V620]
    if role == "PF":
        traits.append(_TRAIT_AMD_V620_PF)
    elif role == "VF":
        traits.append(_TRAIT_AMD_V620_VF)
        traits.append(_TRAIT_AMD_MXGPU)

    numa_node = _read_numa_node(bdf)
    if numa_node is None:
        traits.append(_TRAIT_NUMA_NONE)
        numa_value = "-1"
    else:
        traits.append("%s%d" % (_TRAIT_NUMA_PREFIX, numa_node))
        numa_value = str(numa_node)

    socket_id = _read_socket_id(bdf)
    if socket_id is None or socket_id < 0:
        traits.append(_TRAIT_SOCKET_NONE)
        socket_value = "-1"
    else:
        traits.append("%s%d" % (_TRAIT_SOCKET_PREFIX, socket_id))
        socket_value = str(socket_id)

    return {
        "traits": traits,
        "numa_node": numa_value,
        "socket_id": socket_value,
    }


# Backwards-compatible alias - existing callers / tests that imported
# _get_traits before Phase 2 still work, though no in-tree callers use
# it after the Phase 2 rewrite.
def _get_traits(bdf, role):
    out = _get_traits_and_numa(bdf, role)
    return {"traits": out["traits"]}


def _generate_attribute_list(gpu):
    """Build the DriverAttribute list from a gpu_dict.

    Mirrors NVIDIA's _generate_attribute_list - emits ``rc`` and one
    ``trait<n>`` per trait. Phase 2 additionally emits a single
    ``numa_node`` attribute (str(int); ``"-1"`` for no NUMA affinity)
    when present in the gpu_dict. The Cyborg conductor reads this
    generic attribute (driver-agnostic key) to decide which per-NUMA
    sub-RP a deployable RP should be parented under; see
    ``cyborg/conductor/manager.py::_get_or_create_numa_subprovider``.
    """
    attr_list = []
    index = 0
    for k, v in gpu.items():
        if k == "rc":
            driver_attr = driver_attribute.DriverAttribute()
            driver_attr.key, driver_attr.value = k, v
            attr_list.append(driver_attr)
        if k == "traits":
            for val in gpu.get(k, []):
                driver_attr = driver_attribute.DriverAttribute(
                    key="trait" + str(index), value=val
                )
                index += 1
                attr_list.append(driver_attr)
        if k == "numa_node":
            driver_attr = driver_attribute.DriverAttribute(
                key="numa_node", value=str(v),
            )
            attr_list.append(driver_attr)
        if k == "socket_id":
            # Phase 3: generic socket_id DriverAttribute (str(int); "-1"
            # for unknown). Read by the conductor's socket-anchor layer
            # to parent the deployable under <host>_socket_<n>.
            driver_attr = driver_attribute.DriverAttribute(
                key="socket_id", value=str(v),
            )
            attr_list.append(driver_attr)
    return attr_list


def _generate_attach_handle(gpu):
    """Build a single AH_TYPE_PCI attach handle for a V620 deployable."""
    driver_ah = driver_attach_handle.DriverAttachHandle()
    driver_ah.in_use = False
    driver_ah.attach_type = constants.AH_TYPE_PCI
    driver_ah.attach_info = utils.pci_str_to_json(gpu["devices"])
    return driver_ah


def _generate_dep_list(gpu):
    """Build the (single-element) DriverDeployable list for a V620 device.

    AMD V620 is straight PCI passthrough - one deployable, one attach
    handle, no mdev-like multiplexing.
    """
    driver_dep = driver_deployable.DriverDeployable()
    driver_dep.attribute_list = _generate_attribute_list(gpu)
    driver_dep.attach_handle_list = [_generate_attach_handle(gpu)]
    driver_dep.name = gpu.get('hostname', '') + '_' + gpu["devices"]
    driver_dep.driver_name = gpu_utils.VENDOR_MAPS.get(
        gpu["vendor_id"], ''
    ).upper()
    driver_dep.num_accelerators = 1
    return [driver_dep]


def _generate_controlpath_id(gpu):
    """Build the DriverControlPathID for a V620 device.

    The cpid_info is the device's own BDF (the VF's BDF for a VF, the
    PF's BDF for a PF), so each device is a distinct Placement
    resource provider keyed by its own BDF.
    """
    driver_cpid = driver_controlpath_id.DriverControlPathID()
    driver_cpid.cpid_type = "PCI"
    driver_cpid.cpid_info = utils.pci_str_to_json(gpu["devices"])
    return driver_cpid


def _generate_driver_device(gpu):
    """Assemble the DriverDevice for one V620 PF or VF."""
    driver_device_obj = driver_device.DriverDevice()
    driver_device_obj.vendor = gpu['vendor_id']
    driver_device_obj.model = gpu.get('model', 'miss model info')
    std_board_info = {
        'product_id': gpu.get('product_id'),
        'controller': gpu.get('controller'),
    }
    vendor_board_info = {
        'vendor_info': gpu.get('vendor_info', 'gpu_vb_info'),
    }
    # Surface the parent PF's BDF on a VF so downstream consumers can
    # express PF->VF topology. PFs leave this absent.
    if gpu.get('parent_pf'):
        vendor_board_info['parent_pf'] = gpu['parent_pf']
    driver_device_obj.std_board_info = jsonutils.dumps(std_board_info)
    driver_device_obj.vendor_board_info = jsonutils.dumps(vendor_board_info)
    driver_device_obj.type = constants.DEVICE_GPU
    driver_device_obj.stub = gpu.get('stub', False)
    driver_device_obj.controlpath_id = _generate_controlpath_id(gpu)
    driver_device_obj.deployable_list = _generate_dep_list(gpu)
    return driver_device_obj


def _config_product_ids(opt_name, default):
    """Read a configured product-ID list, falling back to the default."""
    try:
        configured = getattr(CONF.gpu_devices, opt_name)
    except Exception:
        configured = None
    return list(configured) if configured else list(default)


def _discover_v620(vendor_id):
    """Discover V620 PFs and VFs on the local host.

    Returns a list of DriverDevice objects. PF and VF are mutually
    exclusive at the source: a PF with sriov_numvfs==0 is reported as
    a single PF DriverDevice; a PF with sriov_numvfs>0 contributes
    one DriverDevice per VF (the PF is omitted).
    """
    pf_product_ids = set(
        _config_product_ids(
            'enabled_amd_pf_product_ids', _DEFAULT_PF_PRODUCT_IDS,
        )
    )
    vf_product_ids = set(
        _config_product_ids(
            'enabled_amd_vf_product_ids', _DEFAULT_VF_PRODUCT_IDS,
        )
    )

    # 1. lspci-driven raw discovery, filtered to vendor 1002.
    raw_lines = gpu_utils.get_pci_devices(gpu_utils.GPU_FLAGS, vendor_id)

    # 2. Parse and bucket into PF / VF dicts by product ID.
    pfs = {}  # pf_bdf -> gpu_dict
    vfs = {}  # vf_bdf -> gpu_dict
    for line in raw_lines:
        m = gpu_utils.GPU_INFO_PATTERN.match(line)
        if not m:
            continue
        info = m.groupdict()
        product_id = info["product_id"].lower()
        bdf = info["devices"]
        if product_id in pf_product_ids:
            info['hostname'] = CONF.host
            pfs[bdf] = info
        elif product_id in vf_product_ids:
            info['hostname'] = CONF.host
            vfs[bdf] = info
        else:
            LOG.debug(
                'Skipping AMD device %s with product_id %s '
                '(not a V620 PF or VF).', bdf, product_id,
            )

    # 3. Group VFs by parent PF (sysfs physfn). Treat missing physfn
    #    as "no known parent" - the VF is still discovered and reported.
    vfs_by_pf = {}
    for vf_bdf in vfs:
        pf_bdf = gpu_utils.get_physfn(vf_bdf)
        vfs_by_pf.setdefault(pf_bdf, []).append(vf_bdf)

    devices = []

    # 4a. For each known PF, decide PF-mode vs VF-mode by sriov_numvfs.
    handled_vfs = set()
    for pf_bdf, pf_info in pfs.items():
        numvfs = _read_sriov_numvfs(pf_bdf)
        if numvfs <= 0:
            # PF-only mode - emit the PF, no VFs.
            pf_info["rc"] = constants.RESOURCES["PGPU"]
            pf_info.update(_get_traits_and_numa(pf_bdf, role="PF"))
            devices.append(_generate_driver_device(pf_info))
        else:
            # VF mode - emit one DriverDevice per VF; skip the PF.
            for vf_bdf in vfs_by_pf.get(pf_bdf, []):
                vf_info = vfs[vf_bdf]
                vf_info['parent_pf'] = pf_bdf
                vf_info["rc"] = constants.RESOURCES["PGPU"]
                vf_info.update(_get_traits_and_numa(vf_bdf, role="VF"))
                devices.append(_generate_driver_device(vf_info))
                handled_vfs.add(vf_bdf)

    # 4b. VFs whose parent PF was not seen via lspci (operator filtered
    #     the PF product ID, or the PF is bound to a different driver
    #     that hides it). Still emit them.
    for vf_bdf, vf_info in vfs.items():
        if vf_bdf in handled_vfs:
            continue
        parent = gpu_utils.get_physfn(vf_bdf)
        if parent:
            vf_info['parent_pf'] = parent
        vf_info["rc"] = constants.RESOURCES["PGPU"]
        vf_info.update(_get_traits_and_numa(vf_bdf, role="VF"))
        devices.append(_generate_driver_device(vf_info))

    return devices


def discover(vendor_id):
    """Entrypoint called by AMDGPUDriver.discover()."""
    return _discover_v620(vendor_id)
