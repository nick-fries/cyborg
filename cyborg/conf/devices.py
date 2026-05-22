# Copyright 2020 Intel, Inc.
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

from oslo_config import cfg


pci_group = cfg.OptGroup(name='pci', title='PCI passthrough options')

pci_opts = [cfg.MultiStrOpt('passthrough_whitelist', default=[], help=" ")]

nic_group = cfg.OptGroup(
    name='nic_devices',
    title='nic device ID options',
    help="""This is used to config specific nic devices.
    """,
)

nic_opts = [cfg.ListOpt('enabled_nic_types', default=[], help=" ")]

gpu_group = cfg.OptGroup(
    name='gpu_devices',
    title='virtual gpu options',
    help="""This is used to config vGPU types for nvidia GPU devices.
    """,
)

vgpu_opts = [
    # TODO(bogdando): After Cyborg ensures the safe removal of Placement
    # resource providers and deployables during upgrades and that can be tested
    # similar to Nova's test_pci_in_placement backed by the Placement
    # sqlite fixture, change this option's default to True.
    cfg.BoolOpt(
        'filter_sriov_vfs',
        default=False,
        help="""
Filter out SR-IOV Virtual Function (VF) devices from GPU discovery.

When enabled, the NVIDIA GPU driver will skip PCI VF devices and only
report Physical Functions (PFs) and mediated devices. Cards like the
A100 expose VFs when SR-IOV is enabled, but these should not be reported
as standalone GPU accelerators.

This option defaults to False because enabling it may cause existing
VF-backed allocations to disappear from Placement without being cleaned
up first. Operators should ensure no instances hold VF allocations before
enabling this option, as Cyborg does not yet have upgrade-safe protection
equivalent to Nova's PCI tracker (which defers removal of allocated
devices until the owning instance is deleted).
""",
    ),
    cfg.ListOpt(
        'enabled_vgpu_types',
        default=[],
        help="""
The vGPU types enabled in the compute node.

Cyborg supports multiple vGPU types in one host. Usually, a single physical
GPU can only set one vgpu type. Some pGPUs (e.g. NVIDIA GRID K1) support
multiple vGPU types.

If more than one single vGPU type are provided, then for each
*vGPU type*, you must add an additional section ``[vgpu_$(VGPU_TYPE)]`` with
a single configuration option ``device_addresses`` to assign this type to
the target physical GPU(s). PGPUs should be configured explicitly now, we will
improve this after we implement the enable/disable interface.

If the same PCI address is provided for two different types, cyborg-agent will
return an InvalidGPUConfig exception at restart.

An example is as the following::

    [gpu_devices]
    enabled_vgpu_types = nvidia-35, nvidia-36

    [vgpu_nvidia-35]
    device_addresses = 0000:84:00.0,0000:85:00.0

    [vgpu_nvidia-36]
    device_addresses = 0000:86:00.0

""",
    ),
    cfg.ListOpt(
        'enabled_amd_pf_product_ids',
        default=["73a1"],
        help="""
PCI product IDs the AMD GPU driver treats as V620 Physical Functions.

The AMD Radeon Pro V620 Physical Function reports PCI ID ``1002:73a1``.
PFs are emitted as Cyborg deployables only when ``sriov_numvfs == 0``
(i.e. the ``gim`` kernel module has not been loaded or no VFs have been
created). Operators may extend this list to support additional V620 SKUs
or future AMD MxGPU-capable boards that share the same discovery shape.
""",
    ),
    cfg.ListOpt(
        'enabled_amd_vf_product_ids',
        default=["73ae"],
        help="""
PCI product IDs the AMD GPU driver treats as V620 Virtual Functions.

The AMD Radeon Pro V620 SR-IOV VF reports PCI ID ``1002:73ae`` ("Navi 21
[Radeon Pro V620 MxGPU]"). Unlike NVIDIA A100, AMD assigns PF and VF
distinct device IDs, so VF identification is by product-ID match alone -
no ``physfn`` sysfs walk is required. Operators may extend this list for
additional V620-class VFs.
""",
    ),
]


# Phase 3 (PLAN-amd-v620.md §8.5): NIC topology driver options.
# The driver is vendor-agnostic and writes locality traits onto
# Neutron-written Placement RPs. Operators opt in by setting
# ``[nic_topology] enabled = True``. The defaults are tuned for
# the common case (Ethernet NICs, the standard
# ``CUSTOM_TOPO_SOCKET<n>`` / ``CUSTOM_TOPO_NUMA<n>`` trait names
# used by the companion Nova ``TopologyAffinityFilter``); operators
# with unusual hardware or naming conventions can override.
nic_topology_group = cfg.OptGroup(
    name='nic_topology',
    title='NIC topology driver',
    help=(
        "Configuration for the vendor-agnostic NIC topology driver "
        "(Cyborg V620 / topology-aware-scheduling fork, PLAN "
        "§8.5). Discovers network-class PCI Physical Functions on "
        "the local host and PATCHes locality traits onto the "
        "corresponding Neutron-written Placement resource providers."
    ),
)

nic_topology_opts = [
    cfg.BoolOpt(
        'enabled',
        default=False,
        help="""
Enable the NIC topology driver. When False (default) the driver is
loaded by stevedore but its ``discover()`` returns immediately
without touching Placement. Operators must explicitly opt in.
""",
    ),
    cfg.ListOpt(
        'pci_class_prefixes',
        default=['02'],
        help="""
PCI class-word prefixes that the topology driver treats as network
controllers. Default ``['02']`` matches every network controller
sub-class (Ethernet 0200, Infiniband 0207, etc). Narrow this to
``['0207']`` for an Infiniband-only deployment or expand it for
unusual hardware. Values are case-insensitive hex without ``0x``.
""",
    ),
    cfg.StrOpt(
        'socket_trait_prefix',
        default='CUSTOM_TOPO_SOCKET',
        help="""
Trait-name prefix for the per-socket locality trait. The driver
appends the integer socket id (e.g. ``CUSTOM_TOPO_SOCKET0``).
Must match what the Nova ``TopologyAffinityFilter`` reads.
""",
    ),
    cfg.StrOpt(
        'numa_trait_prefix',
        default='CUSTOM_TOPO_NUMA',
        help="""
Trait-name prefix for the per-NUMA-node locality trait. The driver
appends the integer NUMA node id (e.g. ``CUSTOM_TOPO_NUMA0``).
Must match what the Nova ``TopologyAffinityFilter`` reads.
""",
    ),
]


def register_opts(conf):
    conf.register_group(nic_group)
    conf.register_opts(nic_opts, group=nic_group)
    conf.register_group(gpu_group)
    conf.register_opts(vgpu_opts, group=gpu_group)
    conf.register_group(pci_group)
    conf.register_opts(pci_opts, group=pci_group)
    conf.register_group(nic_topology_group)
    conf.register_opts(nic_topology_opts, group=nic_topology_group)


def register_dynamic_opts(conf):
    """Register dynamically-generated options and groups.

    This must be called by the service that wishes to use the options **after**
    the initial configuration has been loaded.
    """
    opts = [
        cfg.ListOpt(
            'physical_device_mappings',
            default=[],
            item_type=cfg.types.String(),
        ),
        cfg.ListOpt(
            'function_device_mappings',
            default=[],
            item_type=cfg.types.String(),
        ),
    ]

    # Register the '[nic_type]/physical_device_mappings' and
    # '[nic_type]/function_device_mappings' opts, implicitly
    # registering the '[nic_type]' groups in the process
    for nic_type in conf.nic_devices.enabled_nic_types:
        conf.register_opts(opts, group=nic_type)
    # Register the '[vgpu_$(VGPU_TYPE)]/device_addresses' opts, implicitly
    # registering the '[vgpu_$(VGPU_TYPE)]' groups in the process
    opt = cfg.ListOpt(
        'device_addresses', default=[], item_type=cfg.types.String()
    )
    for vgpu_type in conf.gpu_devices.enabled_vgpu_types:
        conf.register_opt(opt, group='vgpu_%s' % vgpu_type)


def list_opts():
    return {
        nic_group: nic_opts,
        gpu_group: vgpu_opts,
        nic_topology_group: nic_topology_opts,
    }
