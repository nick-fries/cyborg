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

from cyborg.accelerator.drivers.gpu.amd.driver import AMDGPUDriver
from cyborg.tests import base


class TestAMDGPUDriver(base.TestCase):
    def test_vendor_constants(self):
        self.assertEqual("amd", AMDGPUDriver.VENDOR)
        self.assertEqual("1002", AMDGPUDriver.VENDOR_ID)

    def test_instantiation(self):
        driver = AMDGPUDriver()
        self.assertEqual("amd", driver.VENDOR)
        self.assertEqual("1002", driver.VENDOR_ID)
