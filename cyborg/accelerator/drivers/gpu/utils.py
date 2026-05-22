# Modifications Copyright (C) 2021 ZTE Corporation
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
Utils for GPU driver.
"""

import os
import re

from oslo_concurrency import processutils
from oslo_log import log as logging

import cyborg.common.exception as exception
import cyborg.conf
import cyborg.privsep


LOG = logging.getLogger(__name__)

GPU_FLAGS = ["VGA compatible controller", "3D controller"]
GPU_INFO_PATTERN = re.compile(
    r"(?P<devices>[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:"
    r"[0-9a-fA-F]{2}\.[0-9a-fA-F]) "
    r"(?P<controller>.*) [\[].*]: (?P<model>.*) .*"
    r"[\[](?P<vendor_id>[0-9a-fA-F]"
    r"{4}):(?P<product_id>[0-9a-fA-F]{4})].*"
)

VENDOR_MAPS = {"10de": "nvidia", "102b": "matrox", "1002": "amd"}
PRODUCT_ID_MAPS = {"1eb8": "T4", "15f7": "P100_PCIE_12GB"}


@cyborg.privsep.sys_admin_pctxt.entrypoint
def lspci_privileged():
    cmd = ['lspci', '-nnn', '-D']
    return processutils.execute(*cmd)


@cyborg.privsep.sys_admin_pctxt.entrypoint
def create_mdev_privileged(pci_addr, mdev_type, ah_uuid):
    """Instantiate a mediated device."""
    if ah_uuid is None:
        raise exception.AttachHandleUUIDNeeded()
    fpath = '/sys/class/mdev_bus/{0}/mdev_supported_types/{1}/create'
    fpath = fpath.format(pci_addr, mdev_type)
    with open(fpath, 'w') as f:
        f.write(ah_uuid)
    return ah_uuid


@cyborg.privsep.sys_admin_pctxt.entrypoint
def remove_mdev_privileged(physical_device, mdev_type, medv_uuid):
    fpath = (
        '/sys/class/mdev_bus/{0}/mdev_supported_types/{1}/devices/{2}/remove'
    )
    fpath = fpath.format(physical_device, mdev_type, medv_uuid)
    with open(fpath, 'w') as f:
        f.write("1")


def get_pci_devices(pci_flags, vendor_id=None):
    device_for_vendor_out = []
    all_device_out = []
    lspci_out = lspci_privileged()[0].split('\n')
    for pci in lspci_out:
        if any(x in pci for x in pci_flags):
            all_device_out.append(pci)
            if vendor_id and vendor_id in pci:
                device_for_vendor_out.append(pci)
    return device_for_vendor_out if vendor_id else all_device_out


def discover_vendors():
    vendors = set()
    gpus = get_pci_devices(GPU_FLAGS)
    for gpu in gpus:
        m = GPU_INFO_PATTERN.match(gpu)
        if m:
            vendor_id = m.groupdict().get("vendor_id")
            vendors.add(vendor_id)
    return vendors


def get_physfn(bdf):
    """Return the parent PF BDF for a given VF BDF, or None.

    Reads /sys/bus/pci/devices/<bdf>/physfn (a symlink) and resolves
    the target's basename, which is the parent PF's BDF. Returns None
    if the device is not a VF (no physfn symlink) or if the symlink
    cannot be read.
    """
    physfn_path = '/sys/bus/pci/devices/%s/physfn' % bdf
    try:
        target = os.readlink(physfn_path)
    except OSError:
        return None
    return os.path.basename(target)


def get_socket_id(bdf):
    """Return the CPU socket (physical_package_id) for a PCI device, or None.

    Phase 3 (PLAN-amd-v620.md §8.5): generic helper used by any
    driver (GPU, NIC, future accelerators) that needs to surface the
    PCI device's containing CPU socket so the Cyborg conductor can
    parent the deployable RP under a ``<host>_socket_<n>`` anchor.

    Implementation:

    1. Read ``/sys/bus/pci/devices/<bdf>/local_cpulist`` (a comma-
       separated, optionally hyphen-ranged list like ``"0-15,32-47"``
       that names the host CPUs local to the device).
    2. Parse out the first CPU id from the first range.
    3. Read ``/sys/devices/system/cpu/cpu<N>/topology/physical_package_id``
       and return that integer.

    Returns ``None`` on any I/O or parse error - callers must treat
    None as "socket unknown" (graceful degradation: deployable
    parents directly under the host root, not under a socket anchor).
    """
    cpulist_path = '/sys/bus/pci/devices/%s/local_cpulist' % bdf
    try:
        with open(cpulist_path) as f:
            raw = f.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    # First entry can be "0", "0-15", "0-15,32-47", etc. Take the
    # leading integer of the first range.
    first_token = raw.split(',', 1)[0].strip()
    first_cpu_str = first_token.split('-', 1)[0].strip()
    try:
        first_cpu = int(first_cpu_str)
    except ValueError:
        return None
    pkg_path = (
        '/sys/devices/system/cpu/cpu%d/topology/physical_package_id'
        % first_cpu
    )
    try:
        with open(pkg_path) as f:
            raw = f.read().strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
