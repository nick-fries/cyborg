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

"""Conductor-service options.

Phase 3 (PLAN-amd-v620.md §8.5): the conductor runs a periodic
reconcile of the host RP subtree against the Cyborg DB to catch
zombie RPs that the per-cycle ``drv_device_make_diff`` may have
missed - e.g. an agent that crashed mid-discovery on a previous
cycle, or a duplicate cpid row caused by BDF reassignment after a
PCI rescan.
"""

from oslo_config import cfg


conductor_group = cfg.OptGroup(
    name='conductor',
    title='Conductor service options',
)

conductor_opts = [
    cfg.IntOpt(
        'periodic_rp_sweep_interval',
        default=3600,
        min=0,
        help="""
Minimum interval (in seconds) between two consecutive runs of the
periodic RP-subtree sweep that detects and removes orphan Placement
resource providers whose Cyborg DB row has already been deleted.

The duplicate-cpid detection (which runs on every ``report_data``
cycle) is cheap; the subtree sweep is more expensive because it
enumerates the host RP tree. Default is once per hour. Set to 0 to
run the sweep on every reconcile cycle. The sweep is deferred-delete
safe: RPs with live allocations are skipped.
""",
    ),
]


def register_opts(conf):
    conf.register_group(conductor_group)
    conf.register_opts(conductor_opts, group=conductor_group)


def list_opts():
    return {conductor_group: conductor_opts}
