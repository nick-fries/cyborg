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
Cyborg AMD GPU driver implementation.
"""

from cyborg.accelerator.drivers.gpu.amd import sysinfo
from cyborg.accelerator.drivers.gpu.base import GPUDriver


class AMDGPUDriver(GPUDriver):
    """Class for AMD GPU drivers.

    Supports the AMD Radeon Pro V620 (PF device ID ``73a1``) and its
    SR-IOV Virtual Functions (VF device ID ``73ae``) created by the
    out-of-tree ``gim`` kernel module. PF and VF have distinct PCI
    device IDs, so identification is by pure product-ID match -- there
    is no need to consult sysfs ``physfn`` as the NVIDIA driver does.
    """

    VENDOR = "amd"
    VENDOR_ID = "1002"

    def discover(self):
        return sysinfo.discover(self.VENDOR_ID)
