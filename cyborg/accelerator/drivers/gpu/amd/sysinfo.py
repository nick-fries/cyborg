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
PLAN-amd-v620.md sections 1, 2, 4, 7, and
DESIGN-cyborg-v620-pf-deployable-model.md (repo root) for the
PF-level deployable model implemented here.

The physical card (PF) is always the DriverDevice; discovery mode
decides the deployable's capacity, mirroring the NVIDIA driver's
PGPU/vGPU split:

* PF not bound to gim and ``sriov_numvfs == 0``: PF passthrough mode.
  One deployable, ``num_accelerators = 1``, one AH_TYPE_PCI attach
  handle = the PF itself. A PF bound to gim is NEVER offered for
  passthrough, even if sriov_numvfs momentarily reads 0.
* gim-bound PF or ``sriov_numvfs > 0`` (MxGPU sliced): one deployable
  *per card*, ``num_accelerators = <discovered VF count>``, one AH_TYPE_PCI
  attach handle per VF (sorted by BDF, so handle allocation order is
  deterministic). The card keeps a single Placement RP whose
  inventory is the VF count; VF lineage is structural (handles hang
  off the PF deployable) instead of data (the old ``parent_pf`` JSON
  breadcrumb in ``vendor_board_info``).

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
import re

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
# Phase 3 (PLAN-amd-v620.md §8.5): per-deployable socket trait, mirroring
# the NUMA trait pattern. ``CUSTOM_AMD_V620_SOCKET_NONE`` is emitted when
# the socket cannot be read; ``CUSTOM_AMD_V620_SOCKET<n>`` carries the
# physical_package_id integer for flavor-side filtering. The generic
# ``socket_id`` DriverAttribute (added in ``_generate_attribute_list``)
# is what the Cyborg conductor actually keys on when interposing a
# ``<host>_socket_<n>`` sub-RP above the NUMA sub-RP.
_TRAIT_SOCKET_NONE = "CUSTOM_AMD_V620_SOCKET_NONE"
_TRAIT_SOCKET_PREFIX = "CUSTOM_AMD_V620_SOCKET"

# vendor:device scheduling traits (DESIGN §"vendor:device"). Emitted on
# every deployable so a device profile can pin a card type in a mixed
# fleet without inventing per-model trait names by hand:
#
# * ``CUSTOM_GPU_<vendor>_<product>`` - one per product ID visible on
#   the deployable. PF passthrough mode carries the PF product
#   (CUSTOM_GPU_1002_73A1); VF mode carries the card's PF product AND
#   the VF product (CUSTOM_GPU_1002_73AE), so "any V620 card" and
#   "an MxGPU VF specifically" are both expressible.
# * ``CUSTOM_GPU_MODEL_<sanitized model>`` - the human model name
#   (e.g. CUSTOM_GPU_MODEL_RADEON_PRO_V620), for operators who think
#   in model strings rather than PCI IDs.
#
# Mode pinning (whole card vs slice) stays on the existing
# CUSTOM_AMD_V620_PF / _VF traits - the product traits select the
# silicon, not the carve-up.
_TRAIT_GPU_PRODUCT_FMT = "CUSTOM_GPU_%s_%s"
_TRAIT_GPU_MODEL_PREFIX = "CUSTOM_GPU_MODEL_"

# Product-ID -> canonical model name. lspci's text depends on the
# container image's pci.ids vintage (the deployed agent image renders
# Navi 21 GL-XL as "unknown"); this map makes ``model`` deterministic.
# Falls back to the lspci-captured text for unmapped IDs.
_PRODUCT_NAME_MAP = {
    "73a1": "Radeon PRO V620",
    "73ae": "Radeon PRO V620 MxGPU VF",
}

# Host drivers that own the PF for virtualization. A PF bound to one of
# these must NEVER be emitted as a passthrough deployable, regardless of
# what sriov_numvfs reads at scan time (gim briefly reports 0 during
# init/teardown, and a passthrough bind would hand the vendor host
# driver's device to a guest). Mode selection checks this before
# sriov_numvfs.
_PF_NEVER_PASSTHROUGH_DRIVERS = ("gim",)


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


def _read_pf_driver(bdf):
    """Return the kernel driver bound to a PCI function, or None.

    Resolves the ``/sys/bus/pci/devices/<bdf>/driver`` symlink. An
    unbound device (no symlink) or any read error returns None -
    callers must treat None as "driver unknown".
    """
    path = '/sys/bus/pci/devices/%s/driver' % bdf
    try:
        return os.path.basename(os.readlink(path))
    except OSError as e:
        LOG.debug(
            'No bound driver readable for %s at %s (%s).', bdf, path, e,
        )
        return None


def _sanitize_trait_suffix(text):
    """Turn free text into a Placement-legal trait suffix.

    Placement custom traits must match ``CUSTOM_[A-Z0-9_]+``. Uppercase
    the text and collapse every run of other characters into a single
    underscore: ``"Radeon PRO V620"`` -> ``"RADEON_PRO_V620"``.
    Returns ``None`` if nothing legal survives.
    """
    if not text:
        return None
    out = re.sub(r'[^A-Za-z0-9]+', '_', text).strip('_').upper()
    return out or None


def _resolve_model_name(info):
    """Canonical model name for a parsed lspci dict.

    Product-ID map first (deterministic, image-independent), then the
    lspci-captured model text, then ``"unknown"``.
    """
    product_id = (info.get('product_id') or '').lower()
    mapped = _PRODUCT_NAME_MAP.get(product_id)
    if mapped:
        return mapped
    captured = (info.get('model') or '').strip()
    return captured or 'unknown'


def _get_traits_and_numa(bdf, role, product_ids=(), model_name=None):
    """Build the trait list AND raw numa_node + socket_id for a V620 deployable.

    :param bdf: PCI BDF of the deployable's device (used to read
                NUMA + socket). In VF mode this is the *PF's* BDF -
                all VFs share the card's locality by construction.
    :param role: ``"PF"`` or ``"VF"``.
    :param product_ids: iterable of PCI product IDs to expose as
                        ``CUSTOM_GPU_<vendor>_<product>`` traits
                        (vendor fixed to 1002 for this driver).
    :param model_name: canonical model name to expose as a
                       ``CUSTOM_GPU_MODEL_<...>`` trait; skipped when
                       None/unknown.
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

    # vendor:device scheduling traits.
    for pid in product_ids:
        suffix = _sanitize_trait_suffix(pid)
        if suffix:
            traits.append(_TRAIT_GPU_PRODUCT_FMT % ("1002", suffix))
    if model_name and model_name != 'unknown':
        suffix = _sanitize_trait_suffix(model_name)
        if suffix:
            traits.append(_TRAIT_GPU_MODEL_PREFIX + suffix)

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


def _generate_attach_handle(bdf):
    """Build one AH_TYPE_PCI attach handle for the given PCI BDF.

    ``attach_info`` is exactly ``{domain, bus, device, function}`` -
    nova's hostdev generation parses it; no extra keys.
    """
    driver_ah = driver_attach_handle.DriverAttachHandle()
    driver_ah.in_use = False
    driver_ah.attach_type = constants.AH_TYPE_PCI
    driver_ah.attach_info = utils.pci_str_to_json(bdf)
    return driver_ah


def _generate_dep_list(gpu):
    """Build the (single-element) DriverDeployable list for a V620 card.

    One deployable per card, named after the device's own BDF (the
    PF). ``gpu["attach_bdfs"]``, when present, lists the allocatable
    functions (the VFs in MxGPU mode); otherwise the device's own BDF
    is the sole handle (PF passthrough). ``num_accelerators`` is the
    handle count, so the deployable's Placement inventory equals its
    real capacity (total = max_unit = N).

    Handles are created sorted by BDF. lspci -D prints fixed-width
    lowercase hex, so lexicographic order == numeric order; combined
    with the allocator's ``ORDER BY id`` this yields
    lowest-free-function-first allocation, deterministically.
    """
    driver_dep = driver_deployable.DriverDeployable()
    driver_dep.attribute_list = _generate_attribute_list(gpu)
    attach_bdfs = gpu.get("attach_bdfs") or [gpu["devices"]]
    driver_dep.attach_handle_list = [
        _generate_attach_handle(bdf) for bdf in sorted(attach_bdfs)
    ]
    driver_dep.name = gpu.get('hostname', '') + '_' + gpu["devices"]
    driver_dep.driver_name = gpu_utils.VENDOR_MAPS.get(
        gpu["vendor_id"], ''
    ).upper()
    driver_dep.num_accelerators = len(driver_dep.attach_handle_list)
    return [driver_dep]


def _generate_controlpath_id(gpu):
    """Build the DriverControlPathID for a V620 card.

    The cpid_info is the card's PF BDF in both modes, so each physical
    card is exactly one device row / one Placement RP.
    """
    driver_cpid = driver_controlpath_id.DriverControlPathID()
    driver_cpid.cpid_type = "PCI"
    driver_cpid.cpid_info = utils.pci_str_to_json(gpu["devices"])
    return driver_cpid


def _generate_driver_device(gpu):
    """Assemble the DriverDevice for one V620 card.

    Normalization policy (DESIGN §5): anything a machine consumes is a
    deployable attribute (``rc``, ``trait<n>``, ``numa_node``,
    ``socket_id``) or a first-class field; ``vendor_board_info`` keeps
    only genuinely unmodeled vendor data under an ``"other"`` bucket
    (empty today). The old ``parent_pf`` breadcrumb is gone - the
    device row *is* the PF, and VF BDFs live in the attach handles.
    """
    driver_device_obj = driver_device.DriverDevice()
    driver_device_obj.vendor = gpu['vendor_id']
    driver_device_obj.model = gpu.get('model_name') or _resolve_model_name(gpu)
    std_board_info = {
        'product_id': gpu.get('product_id'),
        'controller': gpu.get('controller'),
    }
    vendor_board_info = {'other': gpu.get('vendor_other', {})}
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
    """Discover V620 cards on the local host.

    Returns a list of DriverDevice objects, one per physical card
    (PF). A PF that is NOT bound to a virtualization host driver
    (gim) and has sriov_numvfs==0 is a passthrough deployable
    (num_accelerators=1, handle = PF). A gim-bound PF or one with
    sriov_numvfs>0 is a sliced deployable (num_accelerators =
    discovered VF count, one handle per VF, sorted by BDF); if no VFs
    are visible the card is skipped entirely - a gim-owned PF is
    never offered for passthrough.
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

    # 4a. One DriverDevice per known PF. The bound driver is checked
    #     first: a PF owned by a virtualization host driver (gim) is
    #     never passthrough-eligible, whatever sriov_numvfs says.
    #     Otherwise sriov_numvfs picks the mode.
    handled_vfs = set()
    for pf_bdf, pf_info in pfs.items():
        numvfs = _read_sriov_numvfs(pf_bdf)
        pf_driver = _read_pf_driver(pf_bdf)
        never_passthrough = pf_driver in _PF_NEVER_PASSTHROUGH_DRIVERS
        pf_info["rc"] = constants.RESOURCES["PGPU"]
        pf_info["model_name"] = _resolve_model_name(pf_info)
        if numvfs <= 0 and not never_passthrough:
            # PF passthrough mode - one handle, the PF itself.
            pf_info.update(_get_traits_and_numa(
                pf_bdf, role="PF",
                product_ids=[pf_info["product_id"].lower()],
                model_name=pf_info["model_name"],
            ))
            devices.append(_generate_driver_device(pf_info))
        else:
            # MxGPU sliced mode - the card is the deployable, the VFs
            # are its attach handles. Locality (NUMA/socket) is read
            # from the PF; VFs share it by construction.
            vf_bdfs = sorted(vfs_by_pf.get(pf_bdf, []))
            if not vf_bdfs:
                LOG.warning(
                    'PF %s (driver=%s, sriov_numvfs=%d) has no enabled '
                    'VFs discovered via lspci; skipping the card. A PF '
                    'owned by a virtualization host driver is never '
                    'offered for passthrough.',
                    pf_bdf, pf_driver, numvfs,
                )
                continue
            if len(vf_bdfs) != numvfs:
                LOG.warning(
                    'PF %s reports sriov_numvfs=%d but %d VFs were '
                    'discovered; reporting the discovered count.',
                    pf_bdf, numvfs, len(vf_bdfs),
                )
            pf_info["attach_bdfs"] = vf_bdfs
            vf_products = sorted(
                {vfs[b]["product_id"].lower() for b in vf_bdfs}
            )
            pf_info.update(_get_traits_and_numa(
                pf_bdf, role="VF",
                product_ids=(
                    [pf_info["product_id"].lower()] + vf_products
                ),
                model_name=pf_info["model_name"],
            ))
            devices.append(_generate_driver_device(pf_info))
            handled_vfs.update(vf_bdfs)

    # 4b. VFs whose parent PF was not seen via lspci (operator filtered
    #     the PF product ID, or the PF is bound to a driver that hides
    #     it). Group them by sysfs physfn and synthesize a PF-keyed
    #     card device so the model stays one-RP-per-card; fall back to
    #     the legacy per-VF shape only when the parent is unreadable.
    orphans = {}
    for vf_bdf in vfs:
        if vf_bdf in handled_vfs:
            continue
        orphans.setdefault(gpu_utils.get_physfn(vf_bdf), []).append(vf_bdf)

    for parent_bdf, vf_bdfs in orphans.items():
        vf_bdfs = sorted(vf_bdfs)
        if parent_bdf:
            proto = dict(vfs[vf_bdfs[0]])
            proto["devices"] = parent_bdf
            proto["attach_bdfs"] = vf_bdfs
            proto["rc"] = constants.RESOURCES["PGPU"]
            # PF product unknown (not in lspci): model and product
            # traits derive from the VF product - a degraded but
            # honest description of the card.
            proto["model_name"] = _resolve_model_name(proto)
            vf_products = sorted(
                {vfs[b]["product_id"].lower() for b in vf_bdfs}
            )
            proto.update(_get_traits_and_numa(
                parent_bdf, role="VF",
                product_ids=vf_products,
                model_name=proto["model_name"],
            ))
            devices.append(_generate_driver_device(proto))
        else:
            for vf_bdf in vf_bdfs:
                vf_info = vfs[vf_bdf]
                vf_info["rc"] = constants.RESOURCES["PGPU"]
                vf_info["model_name"] = _resolve_model_name(vf_info)
                vf_info.update(_get_traits_and_numa(
                    vf_bdf, role="VF",
                    product_ids=[vf_info["product_id"].lower()],
                    model_name=vf_info["model_name"],
                ))
                devices.append(_generate_driver_device(vf_info))

    return devices


def discover(vendor_id):
    """Entrypoint called by AMDGPUDriver.discover()."""
    return _discover_v620(vendor_id)
