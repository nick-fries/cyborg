#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from keystoneauth1 import loading as ks_loading
from oslo_config import cfg

from cyborg.conf import utils as confutils


PLACEMENT_CONF_SECTION = 'placement'
DEFAULT_SERVICE_TYPE = 'placement'

placement_group = cfg.OptGroup(
    PLACEMENT_CONF_SECTION,
    title='Placement Service Options',
    help="Configuration options for connecting to the placement API service",
)


# Phase 2 (PLAN-amd-v620.md §8.4): operator opt-in for the NUMA-aware
# Placement sub-RP layer. When ``True``, the cyborg conductor reads a
# generic ``numa_node`` DriverAttribute (str(int)) on each driver-side
# deployable and parents the deployable RP under a per-NUMA sub-RP
# named ``<host>_numa_<n>`` instead of directly under the compute
# host root. The intermediate sub-RP is tagged with the standard
# ``HW_NUMA_ROOT`` os-traits trait. When ``False`` (default) the
# conductor produces today's flat tree - behavior is identical to
# stock upstream Cyborg. Driver-agnostic: any driver that emits
# ``numa_node`` benefits.
_phase2_opts = [
    cfg.BoolOpt(
        'numa_aware_subtree',
        default=False,
        help="""
When enabled, the Cyborg conductor parents each deployable's resource
provider under a per-NUMA-node sub-RP (named ``<host>_numa_<n>``)
beneath the compute host root, instead of directly under the host
root. This makes the Placement RP tree NUMA-aware so that Nova's
granular ``same_subtree=`` allocation queries can co-locate a GPU,
NIC, and CPU on the same NUMA node.

This requires driver cooperation: drivers must emit a generic
``numa_node`` ``DriverAttribute`` on each deployable (str integer;
``"-1"`` means no NUMA affinity, which falls through to the host
root). Today the in-tree AMD V620 driver does this; other drivers
may add it incrementally. With the flag disabled the conductor
behaves exactly as today (flat tree).

Marker for unfork: ``DROP WHEN NOVA LANDS
nova-spec-numa-topology-with-rps``.
""",
    ),
]


def register_opts(conf):
    conf.register_group(placement_group)
    conf.register_opts(_phase2_opts, group=placement_group)
    confutils.register_ksa_opts(conf, placement_group, DEFAULT_SERVICE_TYPE)


def list_opts():
    return {
        PLACEMENT_CONF_SECTION: (
            _phase2_opts
            + ks_loading.get_session_conf_options()
            + ks_loading.get_auth_common_conf_options()
            + ks_loading.get_auth_plugin_conf_options('password')
            + ks_loading.get_auth_plugin_conf_options('v2password')
            + ks_loading.get_auth_plugin_conf_options('v3password')
            + confutils.get_ksa_adapter_opts(DEFAULT_SERVICE_TYPE)
        )
    }
