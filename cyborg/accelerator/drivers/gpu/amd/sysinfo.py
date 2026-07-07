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

NUMA awareness is surfaced via per-deployable traits
(``CUSTOM_AMD_V620_NUMA<n>`` or ``CUSTOM_AMD_V620_NUMA_NONE``) so that
Nova flavors can request a specific NUMA-affined VF without Cyborg
needing to model a NUMA sub-RP layer.
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

# PCI class strings the AMD lspci scan accepts. gim-managed V620
# functions enumerate with PCI class 0380 ("Display controller"): the
# PF exposes no VGA function and the MxGPU VFs inherit the class, so
# the shared ``gpu_utils.GPU_FLAGS`` (VGA 0300 / 3D 0302) alone drops
# every V620 line before the vendor/product match runs. The extra
# class is deliberately scoped to this driver rather than added to
# ``GPU_FLAGS`` itself so NVIDIA discovery and ``discover_vendors()``
# are not widened.
_AMD_GPU_FLAGS = gpu_utils.GPU_FLAGS + ["Display controller"]

# Trait constants. ``CUSTOM_AMD_V620`` is emitted on every V620
# deployable (PF or VF); the *_PF / *_VF / *_MXGPU traits differentiate.
_TRAIT_OWNER_CYBORG = "OWNER_CYBORG"
_TRAIT_AMD_V620 = "CUSTOM_AMD_V620"
_TRAIT_AMD_V620_PF = "CUSTOM_AMD_V620_PF"
_TRAIT_AMD_V620_VF = "CUSTOM_AMD_V620_VF"
_TRAIT_AMD_MXGPU = "CUSTOM_AMD_MXGPU"
_TRAIT_NUMA_NONE = "CUSTOM_AMD_V620_NUMA_NONE"
_TRAIT_NUMA_PREFIX = "CUSTOM_AMD_V620_NUMA"


def _read_numa_node(bdf):
    """Return the NUMA node id (int) for a PCI device, or None.

    Reads ``/sys/bus/pci/devices/<bdf>/numa_node``. A value of -1 in
    sysfs (meaning "no NUMA affinity") is normalized to None. Any
    OSError is caught and logged - this function never raises.
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
        return None
    return value


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


def _get_traits(bdf, role):
    """Build the trait list for a V620 deployable.

    :param bdf: PCI BDF of the deployable's device (used to read NUMA).
    :param role: ``"PF"`` or ``"VF"``.
    :returns: ``{"traits": [...]}`` ready to merge into the gpu_dict.
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
    else:
        traits.append("%s%d" % (_TRAIT_NUMA_PREFIX, numa_node))

    return {"traits": traits}


def _generate_attribute_list(gpu):
    """Build the DriverAttribute list from a gpu_dict.

    Mirrors NVIDIA's _generate_attribute_list - emits ``rc`` and one
    ``trait<n>`` per trait.
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

    # 1. lspci-driven raw discovery, filtered to vendor 1002. Uses the
    #    AMD-scoped class list so class 0380 V620 functions are seen.
    raw_lines = gpu_utils.get_pci_devices(_AMD_GPU_FLAGS, vendor_id)

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
            pf_info.update(_get_traits(pf_bdf, role="PF"))
            devices.append(_generate_driver_device(pf_info))
        else:
            # VF mode - emit one DriverDevice per VF; skip the PF.
            for vf_bdf in vfs_by_pf.get(pf_bdf, []):
                vf_info = vfs[vf_bdf]
                vf_info['parent_pf'] = pf_bdf
                vf_info["rc"] = constants.RESOURCES["PGPU"]
                vf_info.update(_get_traits(vf_bdf, role="VF"))
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
        vf_info.update(_get_traits(vf_bdf, role="VF"))
        devices.append(_generate_driver_device(vf_info))

    return devices


def discover(vendor_id):
    """Entrypoint called by AMDGPUDriver.discover()."""
    return _discover_v620(vendor_id)
