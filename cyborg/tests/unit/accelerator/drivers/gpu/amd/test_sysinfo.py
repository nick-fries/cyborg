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

from unittest import mock

from oslo_serialization import jsonutils
from stevedore.driver import DriverManager

from cyborg.accelerator.drivers.gpu.amd import sysinfo
from cyborg.accelerator.drivers.gpu.amd.driver import AMDGPUDriver
from cyborg.tests import base


# Canonical lspci -nnn -D strings for V620 PF and VFs. Captured from a
# real bench; line shape matches GPU_INFO_PATTERN in gpu/utils.py.
V620_PF_INFO = (
    "0000:c1:00.0 Display controller [0380]: Advanced Micro Devices, "
    "Inc. [AMD/ATI] Navi 21 [Radeon Pro V620] [1002:73a1] (rev c0)"
)

V620_PF2_INFO = (
    "0000:e1:00.0 Display controller [0380]: Advanced Micro Devices, "
    "Inc. [AMD/ATI] Navi 21 [Radeon Pro V620] [1002:73a1] (rev c0)"
)

V620_VF_INFO_TEMPLATE = (
    "0000:c1:00.{func} Display controller [0380]: Advanced Micro Devices, "
    "Inc. [AMD/ATI] Navi 21 [Radeon Pro V620 MxGPU] [1002:73ae]"
)

# The lines above are used VERBATIM as captured (class 0380 "Display
# controller"). The AMD driver scans with sysinfo._AMD_GPU_FLAGS, which
# extends the shared GPU_FLAGS (VGA 0300 / 3D 0302) with "Display
# controller" - gim-managed V620 PFs expose no VGA function and the
# MxGPU VFs inherit the 0380 class. Earlier revisions of this file
# rewrote the class token to "VGA compatible controller [0300]" to get
# past the shared filter, which masked the discovery bug; every test
# below is now also a regression test for the 0380 class gate.


def _vf_line(func):
    """Return one VF lspci line for the c1:00.<func> address."""
    return V620_VF_INFO_TEMPLATE.format(func=func)


def _attribute_dict(driver_dev):
    """Flatten a DriverDevice's attribute_list into {key: value}."""
    out = {}
    deps = driver_dev.deployable_list
    assert len(deps) == 1
    for attr in deps[0].attribute_list:
        out[attr.key] = attr.value
    return out


def _trait_values(attr_dict):
    return [v for k, v in attr_dict.items() if k.startswith("trait")]


class _SysfsMock:
    """Helper to build an ``open()`` side-effect for sysfs reads.

    Pass a mapping of ``{path: content_or_OSError}``.
    """

    def __init__(self, files):
        self.files = files

    def __call__(self, path, *args, **kwargs):
        if path not in self.files:
            raise FileNotFoundError(path)
        entry = self.files[path]
        if isinstance(entry, OSError):
            raise entry
        return mock.mock_open(read_data=entry).return_value


class TestAMDSysinfo(base.TestCase):
    def setUp(self):
        super().setUp()
        self.set_defaults(host='compute-amd-01', debug=True)

    # --- Test 1: PF-only discovery ------------------------------------
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_pf_only_discovery(self, mock_lspci, mock_open, mock_physfn):
        mock_lspci.return_value = (V620_PF_INFO + '\n', '')
        mock_physfn.return_value = None
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '0',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '0',
            # Phase 3: socket discovery walks local_cpulist -> physical_package_id.
            '/sys/bus/pci/devices/0000:c1:00.0/local_cpulist': '0-15,32-47',
            '/sys/devices/system/cpu/cpu0/topology/physical_package_id': '0',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual(1, len(devs))
        attrs = _attribute_dict(devs[0])
        traits = _trait_values(attrs)
        self.assertEqual('PGPU', attrs['rc'])
        self.assertIn('OWNER_CYBORG', traits)
        self.assertIn('CUSTOM_AMD_V620', traits)
        self.assertIn('CUSTOM_AMD_V620_PF', traits)
        self.assertIn('CUSTOM_AMD_V620_NUMA0', traits)
        self.assertNotIn('CUSTOM_AMD_V620_VF', traits)
        self.assertNotIn('CUSTOM_AMD_MXGPU', traits)
        # vendor:device scheduling traits (PF product + model name).
        self.assertIn('CUSTOM_GPU_1002_73A1', traits)
        self.assertIn('CUSTOM_GPU_MODEL_RADEON_PRO_V620', traits)
        self.assertNotIn('CUSTOM_GPU_1002_73AE', traits)
        # Normalization: model from the product-name map, and
        # vendor_board_info reduced to the unmodeled-only bucket.
        self.assertEqual('Radeon PRO V620', devs[0].model)
        self.assertEqual(
            {'other': {}}, jsonutils.loads(devs[0].vendor_board_info)
        )
        self.assertEqual(1, devs[0].deployable_list[0].num_accelerators)
        # Phase 2: generic numa_node DriverAttribute is emitted alongside
        # the CUSTOM_AMD_V620_NUMA<n> trait for the conductor sub-RP layer.
        self.assertEqual('0', attrs['numa_node'])
        # Phase 3: socket_id DriverAttribute and CUSTOM_AMD_V620_SOCKET<n>
        # trait emitted alongside numa_node.
        self.assertEqual('0', attrs['socket_id'])
        self.assertIn('CUSTOM_AMD_V620_SOCKET0', traits)
        self.assertNotIn('CUSTOM_AMD_V620_SOCKET_NONE', traits)
        # cpid is the PF's own BDF
        cpid = jsonutils.loads(devs[0].controlpath_id.cpid_info)
        self.assertEqual('c1', cpid['bus'])
        self.assertEqual('0', cpid['function'])

    # --- Test 2: VF mode = one PF-level deployable --------------------
    # DESIGN-cyborg-v620-pf-deployable-model.md §6: one DriverDevice
    # per card, cpid = PF BDF, num_accelerators = VF count, one
    # AH_TYPE_PCI handle per VF sorted by BDF.
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_vf_mode_pf_level_deployable(self, mock_lspci, mock_open,
                                         mock_physfn):
        lines = '\n'.join(
            [V620_PF_INFO] + [_vf_line(i) for i in range(1, 5)]
        ) + '\n'
        mock_lspci.return_value = (lines, '')

        # Every VF's parent is the PF.
        mock_physfn.return_value = '0000:c1:00.0'

        # Locality is read from the PF (VFs inherit it structurally).
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '4',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '0',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual(1, len(devs))
        dev = devs[0]

        # Device row is the card: cpid = PF BDF.
        cpid = jsonutils.loads(dev.controlpath_id.cpid_info)
        self.assertEqual('c1', cpid['bus'])
        self.assertEqual('00', cpid['device'])
        self.assertEqual('0', cpid['function'])
        self.assertEqual('Radeon PRO V620', dev.model)
        self.assertEqual(
            '73a1', jsonutils.loads(dev.std_board_info)['product_id']
        )
        # parent_pf breadcrumb is gone; only the unmodeled bucket stays.
        self.assertEqual(
            {'other': {}}, jsonutils.loads(dev.vendor_board_info)
        )

        dep = dev.deployable_list[0]
        self.assertEqual('compute-amd-01_0000:c1:00.0', dep.name)
        self.assertEqual(4, dep.num_accelerators)

        # One handle per VF, sorted by BDF (lowest-first determinism).
        funcs = []
        for ah in dep.attach_handle_list:
            self.assertFalse(ah.in_use)
            info = jsonutils.loads(ah.attach_info)
            self.assertEqual(
                ['bus', 'device', 'domain', 'function'],
                sorted(info.keys()),
            )
            funcs.append(info['function'])
        self.assertEqual(['1', '2', '3', '4'], funcs)

        attrs = _attribute_dict(dev)
        traits = _trait_values(attrs)
        self.assertEqual('PGPU', attrs['rc'])
        self.assertIn('OWNER_CYBORG', traits)
        self.assertIn('CUSTOM_AMD_V620', traits)
        self.assertIn('CUSTOM_AMD_V620_VF', traits)
        self.assertIn('CUSTOM_AMD_MXGPU', traits)
        self.assertNotIn('CUSTOM_AMD_V620_PF', traits)
        # vendor:device traits: card product AND VF product + model.
        self.assertIn('CUSTOM_GPU_1002_73A1', traits)
        self.assertIn('CUSTOM_GPU_1002_73AE', traits)
        self.assertIn('CUSTOM_GPU_MODEL_RADEON_PRO_V620', traits)
        # Locality comes from the PF BDF.
        self.assertIn('CUSTOM_AMD_V620_NUMA0', traits)
        self.assertEqual('0', attrs['numa_node'])

    # --- Test 2b: handle order is BDF order regardless of lspci order -
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_vf_handles_sorted_by_bdf(self, mock_lspci, mock_open,
                                      mock_physfn):
        # lspci emits the VFs shuffled; handles must still come out
        # ascending so handle-row id order == function order.
        lines = '\n'.join(
            [V620_PF_INFO, _vf_line(4), _vf_line(2), _vf_line(3),
             _vf_line(1)]
        ) + '\n'
        mock_lspci.return_value = (lines, '')
        mock_physfn.return_value = '0000:c1:00.0'
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '4',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '0',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual(1, len(devs))
        funcs = [
            jsonutils.loads(ah.attach_info)['function']
            for ah in devs[0].deployable_list[0].attach_handle_list
        ]
        self.assertEqual(['1', '2', '3', '4'], funcs)

    # --- Test 2c: sriov_numvfs vs discovered-VF mismatch --------------
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_vf_count_is_discovered_not_declared(self, mock_lspci,
                                                 mock_open, mock_physfn):
        # sriov_numvfs says 4 but only 2 VFs are visible: report the
        # discovered capacity (never fake handles).
        lines = '\n'.join(
            [V620_PF_INFO, _vf_line(1), _vf_line(2)]
        ) + '\n'
        mock_lspci.return_value = (lines, '')
        mock_physfn.return_value = '0000:c1:00.0'
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '4',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '0',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual(1, len(devs))
        dep = devs[0].deployable_list[0]
        self.assertEqual(2, dep.num_accelerators)
        self.assertEqual(2, len(dep.attach_handle_list))

    # --- Test 2d: numvfs>0 but zero VFs visible -> card skipped -------
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_vf_mode_no_vfs_skips_card(self, mock_lspci, mock_open,
                                       mock_physfn):
        # gim owns the PF (numvfs>0) but no VF made it through the
        # product filter: the card has no allocatable capacity and the
        # gim-owned PF must not be offered for passthrough.
        mock_lspci.return_value = (V620_PF_INFO + '\n', '')
        mock_physfn.return_value = None
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '2',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '0',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual([], devs)

    # --- Test 2g: gim-bound PF is NEVER passthrough, even numvfs=0 ----
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo'
                '._read_pf_driver')
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_gim_pf_numvfs_zero_never_passthrough(self, mock_lspci,
                                                  mock_open, mock_physfn,
                                                  mock_driver):
        # gim owns the PF but sriov_numvfs reads 0 (init/teardown
        # window). The PF must NOT fall back to passthrough mode; with
        # no VFs visible the card is skipped outright.
        mock_lspci.return_value = (V620_PF_INFO + '\n', '')
        mock_physfn.return_value = None
        mock_driver.return_value = 'gim'
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '0',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '0',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual([], devs)

    # --- Test 2h: gim-bound PF, numvfs=0 but VFs visible -> VF mode ---
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo'
                '._read_pf_driver')
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_gim_pf_numvfs_zero_with_vfs_uses_vf_mode(self, mock_lspci,
                                                      mock_open,
                                                      mock_physfn,
                                                      mock_driver):
        # Stale numvfs=0 while lspci still shows VFs: discovered truth
        # wins - the card is a sliced deployable, never passthrough.
        lines = '\n'.join([V620_PF_INFO, _vf_line(1), _vf_line(2)]) + '\n'
        mock_lspci.return_value = (lines, '')
        mock_physfn.return_value = '0000:c1:00.0'
        mock_driver.return_value = 'gim'
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '0',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '0',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual(1, len(devs))
        dep = devs[0].deployable_list[0]
        self.assertEqual(2, dep.num_accelerators)
        traits = _trait_values(_attribute_dict(devs[0]))
        self.assertIn('CUSTOM_AMD_V620_VF', traits)
        self.assertNotIn('CUSTOM_AMD_V620_PF', traits)

    # --- Test 2e: orphan VFs with readable physfn group into a card ---
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_orphan_vfs_grouped_by_physfn(self, mock_lspci, mock_open,
                                          mock_physfn):
        # The PF is hidden from lspci (filtered product ID / foreign
        # driver) but sysfs physfn still names it: synthesize the
        # PF-keyed card device instead of per-VF devices.
        lines = '\n'.join([_vf_line(1), _vf_line(2)]) + '\n'
        mock_lspci.return_value = (lines, '')
        mock_physfn.return_value = '0000:c1:00.0'
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '0',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual(1, len(devs))
        dev = devs[0]
        cpid = jsonutils.loads(dev.controlpath_id.cpid_info)
        self.assertEqual('0', cpid['function'])
        dep = dev.deployable_list[0]
        self.assertEqual('compute-amd-01_0000:c1:00.0', dep.name)
        self.assertEqual(2, dep.num_accelerators)
        traits = _trait_values(_attribute_dict(dev))
        # PF product unknown here - only the VF product trait.
        self.assertIn('CUSTOM_GPU_1002_73AE', traits)
        self.assertNotIn('CUSTOM_GPU_1002_73A1', traits)
        # Degraded-but-honest model from the VF product map.
        self.assertEqual('Radeon PRO V620 MxGPU VF', dev.model)

    # --- Test 2f: orphan VFs with unreadable physfn -> legacy per-VF --
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_orphan_vfs_no_physfn_legacy_per_vf(self, mock_lspci,
                                                mock_open, mock_physfn):
        lines = '\n'.join([_vf_line(1), _vf_line(2)]) + '\n'
        mock_lspci.return_value = (lines, '')
        mock_physfn.return_value = None
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.1/numa_node': '0',
            '/sys/bus/pci/devices/0000:c1:00.2/numa_node': '0',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual(2, len(devs))
        funcs = sorted(
            jsonutils.loads(d.controlpath_id.cpid_info)['function']
            for d in devs
        )
        self.assertEqual(['1', '2'], funcs)
        for dev in devs:
            self.assertEqual(
                1, dev.deployable_list[0].num_accelerators
            )

    # --- Test 3: NUMA -1 (no NUMA affinity, ambiguous host) -----------
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.get_sole_numa_node')
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_numa_node_minus_one(self, mock_lspci, mock_open, mock_physfn,
                                 mock_sole):
        # Host exposes zero or 2+ NUMA nodes: -1 must stay "unknown".
        mock_sole.return_value = None
        mock_lspci.return_value = (V620_PF_INFO + '\n', '')
        mock_physfn.return_value = None
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '0',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '-1',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual(1, len(devs))
        attrs = _attribute_dict(devs[0])
        traits = _trait_values(attrs)
        self.assertIn('CUSTOM_AMD_V620_NUMA_NONE', traits)
        for t in traits:
            self.assertFalse(
                t.startswith('CUSTOM_AMD_V620_NUMA')
                and t != 'CUSTOM_AMD_V620_NUMA_NONE',
                'Unexpected NUMA trait %s' % t,
            )
        # Phase 2: numa_node attribute is "-1" when no NUMA affinity.
        self.assertEqual('-1', attrs['numa_node'])

    # --- Test 3b: NUMA -1 on a single-NUMA host normalizes to node 0 --
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.get_sole_numa_node')
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_numa_node_minus_one_single_numa_host(self, mock_lspci,
                                                  mock_open, mock_physfn,
                                                  mock_sole):
        # Single-socket firmware omitting ACPI _PXM: sysfs says -1 but
        # exactly one /sys/devices/system/node/node<N> exists, so the
        # deployable is normalized onto the sole node.
        mock_sole.return_value = 0
        mock_lspci.return_value = (V620_PF_INFO + '\n', '')
        mock_physfn.return_value = None
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '0',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '-1',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual(1, len(devs))
        attrs = _attribute_dict(devs[0])
        traits = _trait_values(attrs)
        self.assertIn('CUSTOM_AMD_V620_NUMA0', traits)
        self.assertNotIn('CUSTOM_AMD_V620_NUMA_NONE', traits)
        self.assertEqual('0', attrs['numa_node'])

    # --- Test 4: NUMA OSError (sysfs read fails) ----------------------
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_numa_node_oserror(self, mock_lspci, mock_open, mock_physfn):
        mock_lspci.return_value = (V620_PF_INFO + '\n', '')
        mock_physfn.return_value = None
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '0',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node':
                OSError('device busy'),
        })

        # Must not raise.
        devs = AMDGPUDriver().discover()

        self.assertEqual(1, len(devs))
        attrs = _attribute_dict(devs[0])
        traits = _trait_values(attrs)
        self.assertIn('CUSTOM_AMD_V620_NUMA_NONE', traits)
        # Phase 2: numa_node attribute is "-1" on sysfs OSError too.
        self.assertEqual('-1', attrs['numa_node'])

    # --- Test 5: Multi-NUMA (2 PFs on different sockets) --------------
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_multi_numa_pf_only(self, mock_lspci, mock_open, mock_physfn):
        lines = '\n'.join([V620_PF_INFO, V620_PF2_INFO]) + '\n'
        mock_lspci.return_value = (lines, '')
        mock_physfn.return_value = None
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '0',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '0',
            '/sys/bus/pci/devices/0000:e1:00.0/sriov_numvfs': '0',
            '/sys/bus/pci/devices/0000:e1:00.0/numa_node': '1',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual(2, len(devs))
        traits_by_bdf = {}
        numa_by_bdf = {}
        for dev in devs:
            cpid = jsonutils.loads(dev.controlpath_id.cpid_info)
            bdf_key = cpid['bus']
            attrs = _attribute_dict(dev)
            traits_by_bdf[bdf_key] = _trait_values(attrs)
            numa_by_bdf[bdf_key] = attrs.get('numa_node')
        self.assertIn('CUSTOM_AMD_V620_NUMA0', traits_by_bdf['c1'])
        self.assertIn('CUSTOM_AMD_V620_NUMA1', traits_by_bdf['e1'])
        # Phase 2: per-BDF numa_node attribute consistent with the trait.
        self.assertEqual('0', numa_by_bdf['c1'])
        self.assertEqual('1', numa_by_bdf['e1'])

    # --- Test 6: Stevedore entry-point resolves ----------------------
    def test_stevedore_entrypoint(self):
        mgr = DriverManager(
            namespace='cyborg.accelerator.driver',
            name='amd_gpu_driver',
            invoke_on_load=False,
        )
        self.assertIs(AMDGPUDriver, mgr.driver)

    # --- Test 7: legacy VGA class still discovered --------------------
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_vga_class_still_discovered(self, mock_lspci, mock_open,
                                        mock_physfn):
        # A hypothetical V620 enumerating as VGA 0300 (no gim, primary
        # display function) must remain discoverable: _AMD_GPU_FLAGS
        # extends GPU_FLAGS, it does not replace it.
        vga_line = V620_PF_INFO.replace(
            "Display controller [0380]", "VGA compatible controller [0300]"
        )
        mock_lspci.return_value = (vga_line + '\n', '')
        mock_physfn.return_value = None
        mock_open.side_effect = _SysfsMock({
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '0',
            '/sys/bus/pci/devices/0000:c1:00.0/numa_node': '0',
        })

        devs = AMDGPUDriver().discover()

        self.assertEqual(1, len(devs))
        traits = _trait_values(_attribute_dict(devs[0]))
        self.assertIn('CUSTOM_AMD_V620_PF', traits)

    # --- Test 8: the extra class is scoped to the AMD driver ----------
    def test_shared_gpu_flags_not_widened(self):
        # The 0380 fix must not leak into the shared GPU_FLAGS used by
        # NVIDIA discovery and discover_vendors().
        from cyborg.accelerator.drivers.gpu import utils as gpu_utils

        self.assertNotIn('Display controller', gpu_utils.GPU_FLAGS)
        self.assertEqual(
            gpu_utils.GPU_FLAGS + ['Display controller'],
            sysinfo._AMD_GPU_FLAGS,
        )


class TestAMDSysinfoHelpers(base.TestCase):
    """Direct-call coverage of the small sysfs helpers."""

    def test_read_numa_node_positive(self):
        m = mock.mock_open(read_data='2\n')
        with mock.patch('builtins.open', m):
            self.assertEqual(2, sysinfo._read_numa_node('0000:c1:00.0'))

    def test_read_numa_node_negative(self):
        m = mock.mock_open(read_data='-1\n')
        with mock.patch('builtins.open', m), mock.patch(
            'cyborg.accelerator.drivers.gpu.utils.get_sole_numa_node',
            return_value=None,
        ):
            self.assertIsNone(sysinfo._read_numa_node('0000:c1:00.0'))

    def test_read_numa_node_negative_single_numa_fallback(self):
        m = mock.mock_open(read_data='-1\n')
        with mock.patch('builtins.open', m), mock.patch(
            'cyborg.accelerator.drivers.gpu.utils.get_sole_numa_node',
            return_value=0,
        ):
            self.assertEqual(0, sysinfo._read_numa_node('0000:c1:00.0'))

    def test_read_numa_node_oserror(self):
        with mock.patch(
            'builtins.open', side_effect=OSError('boom'),
        ):
            # Must not raise.
            self.assertIsNone(sysinfo._read_numa_node('0000:c1:00.0'))

    def test_read_sriov_numvfs_positive(self):
        m = mock.mock_open(read_data='4\n')
        with mock.patch('builtins.open', m):
            self.assertEqual(4, sysinfo._read_sriov_numvfs('0000:c1:00.0'))

    def test_read_sriov_numvfs_missing(self):
        with mock.patch(
            'builtins.open', side_effect=OSError('no such file'),
        ):
            self.assertEqual(0, sysinfo._read_sriov_numvfs('0000:c1:00.0'))

    def test_read_pf_driver(self):
        with mock.patch(
            'os.readlink',
            return_value='../../../../bus/pci/drivers/gim',
        ):
            self.assertEqual(
                'gim', sysinfo._read_pf_driver('0000:c1:00.0'),
            )

    def test_read_pf_driver_unbound(self):
        with mock.patch(
            'os.readlink', side_effect=OSError('no such file'),
        ):
            self.assertIsNone(sysinfo._read_pf_driver('0000:c1:00.0'))


    def test_read_socket_id_happy_path(self):
        """Phase 3: socket_id resolves via local_cpulist -> package_id."""
        files = {
            '/sys/bus/pci/devices/0000:c1:00.0/local_cpulist':
                '0-15,32-47',
            '/sys/devices/system/cpu/cpu0/topology/physical_package_id': '1',
        }

        def _open(path, *a, **kw):
            if path not in files:
                raise FileNotFoundError(path)
            return mock.mock_open(read_data=files[path]).return_value

        with mock.patch('builtins.open', side_effect=_open):
            self.assertEqual(
                1, sysinfo._read_socket_id('0000:c1:00.0'),
            )

    def test_read_socket_id_single_cpu(self):
        """A 1-socket / 1-core test mock: cpulist is just '0'."""
        files = {
            '/sys/bus/pci/devices/0000:c1:00.0/local_cpulist': '0',
            '/sys/devices/system/cpu/cpu0/topology/physical_package_id': '0',
        }

        def _open(path, *a, **kw):
            if path not in files:
                raise FileNotFoundError(path)
            return mock.mock_open(read_data=files[path]).return_value

        with mock.patch('builtins.open', side_effect=_open):
            self.assertEqual(
                0, sysinfo._read_socket_id('0000:c1:00.0'),
            )

    def test_read_socket_id_unreadable(self):
        """OSError on either sysfs file -> None; never raises."""
        with mock.patch(
            'builtins.open', side_effect=OSError('boom'),
        ):
            self.assertIsNone(sysinfo._read_socket_id('0000:c1:00.0'))

    def test_sanitize_trait_suffix(self):
        self.assertEqual(
            'RADEON_PRO_V620',
            sysinfo._sanitize_trait_suffix('Radeon PRO V620'),
        )
        self.assertEqual(
            'A_B_C', sysinfo._sanitize_trait_suffix('a-b(c)'),
        )
        self.assertIsNone(sysinfo._sanitize_trait_suffix(''))
        self.assertIsNone(sysinfo._sanitize_trait_suffix('***'))
        self.assertIsNone(sysinfo._sanitize_trait_suffix(None))

    def test_resolve_model_name_map_hit(self):
        self.assertEqual(
            'Radeon PRO V620',
            sysinfo._resolve_model_name(
                {'product_id': '73A1', 'model': 'whatever lspci said'}
            ),
        )

    def test_resolve_model_name_fallback_to_lspci(self):
        with mock.patch.dict(sysinfo._PRODUCT_NAME_MAP, clear=True):
            self.assertEqual(
                'Navi 21 GL-XL',
                sysinfo._resolve_model_name(
                    {'product_id': '73a1', 'model': 'Navi 21 GL-XL'}
                ),
            )

    def test_resolve_model_name_unknown(self):
        with mock.patch.dict(sysinfo._PRODUCT_NAME_MAP, clear=True):
            self.assertEqual(
                'unknown',
                sysinfo._resolve_model_name({'product_id': '9999'}),
            )
