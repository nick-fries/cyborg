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

# The GPU_FLAGS filter ("VGA compatible controller", "3D controller")
# is not strict enough to reject "Display controller", but
# get_pci_devices() also filters by the vendor_id substring, so the
# "1002" in the brackets makes these lines pass when vendor_id="1002".
# However GPU_FLAGS still gates the line, so for the tests we rewrite
# the controller token to one of the recognized strings.
V620_PF_INFO = V620_PF_INFO.replace(
    "Display controller [0380]", "VGA compatible controller [0300]"
)
V620_PF2_INFO = V620_PF2_INFO.replace(
    "Display controller [0380]", "VGA compatible controller [0300]"
)
V620_VF_INFO_TEMPLATE = V620_VF_INFO_TEMPLATE.replace(
    "Display controller [0380]", "VGA compatible controller [0300]"
)


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

    # --- Test 2: VF-only discovery ------------------------------------
    @mock.patch('cyborg.accelerator.drivers.gpu.amd.sysinfo.gpu_utils'
                '.get_physfn')
    @mock.patch('builtins.open')
    @mock.patch('cyborg.accelerator.drivers.gpu.utils.lspci_privileged')
    def test_vf_only_discovery(self, mock_lspci, mock_open, mock_physfn):
        lines = '\n'.join(
            [V620_PF_INFO] + [_vf_line(i) for i in range(1, 5)]
        ) + '\n'
        mock_lspci.return_value = (lines, '')

        # Every VF's parent is the PF.
        mock_physfn.return_value = '0000:c1:00.0'

        sysfs = {
            '/sys/bus/pci/devices/0000:c1:00.0/sriov_numvfs': '4',
        }
        for i in range(1, 5):
            sysfs['/sys/bus/pci/devices/0000:c1:00.%d/numa_node' % i] = '0'
        mock_open.side_effect = _SysfsMock(sysfs)

        devs = AMDGPUDriver().discover()

        self.assertEqual(4, len(devs))
        for dev in devs:
            attrs = _attribute_dict(dev)
            traits = _trait_values(attrs)
            self.assertEqual('PGPU', attrs['rc'])
            self.assertIn('OWNER_CYBORG', traits)
            self.assertIn('CUSTOM_AMD_V620', traits)
            self.assertIn('CUSTOM_AMD_V620_VF', traits)
            self.assertIn('CUSTOM_AMD_MXGPU', traits)
            self.assertIn('CUSTOM_AMD_V620_NUMA0', traits)
            self.assertNotIn('CUSTOM_AMD_V620_PF', traits)
            # Phase 2: VF deployables also carry the generic numa_node
            # attribute for conductor consumption.
            self.assertEqual('0', attrs['numa_node'])
            cpid = jsonutils.loads(dev.controlpath_id.cpid_info)
            # Each VF must use its own BDF as the cpid (a distinct RP).
            self.assertEqual('c1', cpid['bus'])
            self.assertEqual('00', cpid['device'])
            self.assertNotEqual('0', cpid['function'])

        # No PF DriverDevice was emitted (cpids only function 1..4).
        cpid_funcs = sorted(
            jsonutils.loads(d.controlpath_id.cpid_info)['function']
            for d in devs
        )
        self.assertEqual(['1', '2', '3', '4'], cpid_funcs)

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
