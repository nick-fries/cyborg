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


"""
Vendor-agnostic NIC topology discovery + Placement trait PATCH.

See ``driver.py`` for the high-level rationale. This module owns:

* ``_discover_network_pfs()`` -- scan ``/sys/bus/pci/devices`` for
  every device whose PCI class word starts with one of the operator-
  configured ``[nic_topology] pci_class_prefixes`` (default
  ``["02"]``, "Network controller").
* ``_read_socket_id(bdf)`` / ``_read_numa_node(bdf)`` -- single-file
  sysfs reads, both delegate to the shared GPU utils where
  ``get_socket_id`` already implements the local_cpulist ->
  physical_package_id walk.
* ``_find_neutron_rp_for_bdf(...)`` -- query Placement for any RP
  whose name contains the BDF (case-insensitive) and pick the best
  match.
* ``_patch_topology_traits(...)`` -- additive PATCH of
  ``CUSTOM_TOPO_SOCKET<n>`` and ``CUSTOM_TOPO_NUMA<n>`` onto the
  matched RP via ``placement_client.add_traits_to_rp`` (which is
  itself additive, not destructive).
* ``discover()`` -- orchestrates the above and returns an empty list.

The driver is **not** instantiated unless the operator opts in via
``[nic_topology] enabled = True``. When disabled, ``discover()`` is
a fast no-op (the agent still invokes it, but nothing happens).
"""

import os

from oslo_context import context as oslo_context
from oslo_log import log as logging

from cyborg.accelerator.drivers.gpu import utils as gpu_utils
from cyborg.common import placement_client
from cyborg.conf import CONF


LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sysfs scan
# ---------------------------------------------------------------------------

_PCI_DEVICES_ROOT = '/sys/bus/pci/devices'


def _read_class_code(bdf):
    """Return the PCI class word (e.g. ``"0200"``) for a BDF, or None.

    sysfs's ``class`` file contains a 24-bit hex value like
    ``"0x020000"`` (base class + sub-class + interface). We strip
    the ``0x`` prefix and return the leading 4 hex chars (base +
    sub-class) so callers can match on either the broad class
    (``"02"`` = network) or a specific sub-class (``"0200"`` =
    Ethernet, ``"0207"`` = Infiniband).
    """
    path = os.path.join(_PCI_DEVICES_ROOT, bdf, 'class')
    try:
        with open(path) as f:
            raw = f.read().strip()
    except OSError as e:
        LOG.debug('class read failed for %s (%s): %s', bdf, path, e)
        return None
    # Expected shape: ``"0x020000"``.
    if raw.startswith('0x') or raw.startswith('0X'):
        raw = raw[2:]
    raw = raw.lower()
    # Return the leading 4 hex chars; callers compare prefixes.
    return raw[:4] if len(raw) >= 4 else raw


def _is_physical_function(bdf):
    """Heuristic: return True for a PF (not a VF).

    A VF has a ``physfn`` symlink in its sysfs directory. The
    topology driver only operates on PFs; the matching Neutron RP
    is also keyed by PF BDF in every observed naming convention.
    """
    physfn = os.path.join(_PCI_DEVICES_ROOT, bdf, 'physfn')
    return not os.path.exists(physfn)


def _discover_network_pfs(class_prefixes=None):
    """Return a list of ``{"bdf": str}`` for each network-class PF.

    :param class_prefixes: iterable of lower-case hex strings that
        the PCI class word must start with (e.g. ``["02"]``). When
        None, falls back to ``CONF.nic_topology.pci_class_prefixes``.

    The default ``"02"`` matches every network controller class -
    Ethernet (0200), Token Ring (0201), FDDI (0202), ATM (0203),
    ISDN (0204), WorldFip (0205), PICMG (0206), Infiniband (0207),
    Fabric (0208). Operators with unusual hardware (e.g. an
    Infiniband-only deployment) may narrow this to ``["0207"]``.
    """
    if class_prefixes is None:
        class_prefixes = list(CONF.nic_topology.pci_class_prefixes)
    class_prefixes = [p.lower() for p in class_prefixes]

    try:
        entries = sorted(os.listdir(_PCI_DEVICES_ROOT))
    except OSError as e:
        LOG.warning(
            'NIC topology: cannot list %s: %s; no NICs will be '
            'PATCHed this cycle.', _PCI_DEVICES_ROOT, e,
        )
        return []

    pfs = []
    for entry in entries:
        # Each entry is a BDF like "0000:31:00.0".
        if ':' not in entry:
            continue
        class_word = _read_class_code(entry)
        if class_word is None:
            continue
        if not any(class_word.startswith(p) for p in class_prefixes):
            continue
        if not _is_physical_function(entry):
            # VFs are skipped - they share their PF's locality and
            # the PF's RP is what Neutron tracks.
            continue
        pfs.append({"bdf": entry})
    return pfs


def _read_socket_id(bdf):
    """Thin wrapper around ``gpu_utils.get_socket_id`` so test code
    can patch ``sysinfo._read_socket_id`` without reaching across
    the module boundary."""
    return gpu_utils.get_socket_id(bdf)


def _read_numa_node(bdf):
    """Return the NUMA node id for a PCI device, or None.

    Mirrors the AMD driver's ``_read_numa_node`` but lives here so
    the NIC topology driver doesn't import driver-specific code.
    """
    path = os.path.join(_PCI_DEVICES_ROOT, bdf, 'numa_node')
    try:
        with open(path) as f:
            raw = f.read().strip()
    except OSError as e:
        LOG.debug('numa_node read failed for %s: %s', bdf, e)
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


# ---------------------------------------------------------------------------
# Placement RP matching
# ---------------------------------------------------------------------------

def _find_neutron_rp_for_bdf(client, context, host_name, bdf):
    """Find the Placement RP that Neutron has written for ``bdf``.

    Matching algorithm (from broadest to narrowest):

    1. Query Placement for all RPs (``GET /resource_providers``)
       and walk the response.
    2. Filter to RPs whose ``name`` contains ``bdf`` case-
       insensitively. Nova's PCI-in-Placement style uses upper-case
       BDF in RP names (``HOST_0000:31:00.0``); older Neutron
       implementations use lower-case
       (``host:physnet:0000:31:00.0``). We compare both lowered.
    3. If ``host_name`` is non-empty, prefer matches whose RP name
       *also* contains the host name (case-insensitive); fall
       through to any BDF match if none does. This avoids
       cross-host trait pollution in multi-host Placement
       deployments.
    4. If multiple still tie, pick the deterministically first
       one (sorted by name) and log a warning.

    :returns: the RP UUID (str) or None if no candidate was found.
    """
    try:
        resp = client.get('/resource_providers')
    except Exception as e:
        LOG.warning(
            'NIC topology: GET /resource_providers failed: %s; '
            'cannot match BDF %s', e, bdf,
        )
        return None
    if resp is None or getattr(resp, 'status_code', 500) != 200:
        LOG.debug(
            'NIC topology: /resource_providers returned %s for BDF %s',
            getattr(resp, 'status_code', None), bdf,
        )
        return None
    try:
        body = resp.json()
    except Exception:
        return None

    bdf_lc = bdf.lower()
    host_lc = (host_name or '').lower()

    bdf_matches = []
    for rp in body.get('resource_providers', []):
        name = (rp.get('name') or '').lower()
        if bdf_lc in name:
            bdf_matches.append(rp)
    if not bdf_matches:
        return None
    # Prefer matches that also contain the host name.
    host_qualified = [
        rp for rp in bdf_matches
        if host_lc and host_lc in (rp.get('name') or '').lower()
    ]
    pool = host_qualified or bdf_matches
    if len(pool) > 1:
        LOG.warning(
            'NIC topology: multiple Placement RPs match BDF %s on '
            'host %s: %r; picking the first.',
            bdf, host_name, sorted(rp.get('name') for rp in pool),
        )
    pool.sort(key=lambda rp: (rp.get('name') or ''))
    return pool[0].get('uuid')


# ---------------------------------------------------------------------------
# Trait PATCH
# ---------------------------------------------------------------------------

def _trait_name(prefix, value):
    """Compose ``<PREFIX><value>``. Returns None for negative values."""
    if value is None or int(value) < 0:
        return None
    return "%s%d" % (prefix, int(value))


def _patch_topology_traits(client, rp_uuid, socket_id, numa_node):
    """Additively PATCH the topology traits onto an RP.

    Uses ``placement_client.add_traits_to_rp`` which:

    * Ensures the trait exists in Placement (creating if needed).
    * GETs the RP's current traits.
    * Unions the new traits with the existing set.
    * PUTs the combined list back via ``_put_rp_traits``.

    Crucially, ``add_traits_to_rp`` is **additive** - it does not
    clobber unrelated traits like Neutron-written
    ``CUSTOM_PHYSNET_*`` or ``CUSTOM_VNIC_TYPE_*``. That property
    is what makes the topology driver safe to run alongside Neutron
    on the same RP.
    """
    socket_trait = _trait_name(
        CONF.nic_topology.socket_trait_prefix, socket_id,
    )
    numa_trait = _trait_name(
        CONF.nic_topology.numa_trait_prefix, numa_node,
    )
    traits = [t for t in (socket_trait, numa_trait) if t]
    if not traits:
        LOG.info(
            'NIC topology: no topology traits to PATCH on RP %s '
            '(socket=%r, numa=%r).', rp_uuid, socket_id, numa_node,
        )
        return
    try:
        client.add_traits_to_rp(rp_uuid, traits)
    except Exception as e:
        LOG.warning(
            'NIC topology: failed to PATCH traits %r onto RP %s: %s',
            traits, rp_uuid, e,
        )
        return
    LOG.info(
        'NIC topology: PATCHed %r onto RP %s.', traits, rp_uuid,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def discover():
    """Discover NICs, derive topology, PATCH Neutron RPs.

    The agent calls this once per resource-tracker cycle. Steps:

    1. ``[nic_topology] enabled`` gate. When False, return [].
    2. Enumerate network PFs via ``_discover_network_pfs``.
    3. For each PF, read socket_id + numa_node via sysfs.
    4. Match against a Placement RP by BDF substring.
    5. PATCH the locality traits (additive).

    Always returns ``[]`` so the agent's
    ``resource_tracker.update_usage`` treats the call as
    "no new Cyborg-managed devices".
    """
    if not getattr(CONF.nic_topology, 'enabled', False):
        LOG.debug('NIC topology driver disabled; nothing to do.')
        return []

    pfs = _discover_network_pfs()
    if not pfs:
        LOG.debug('NIC topology: no network PFs discovered this cycle.')
        return []

    client = placement_client.PlacementClient()
    context = oslo_context.get_current() or oslo_context.RequestContext(
        user_id=None, project_id=None, overwrite=False,
    )
    host_name = CONF.host

    for pf in pfs:
        bdf = pf["bdf"]
        socket_id = _read_socket_id(bdf)
        numa_node = _read_numa_node(bdf)
        pf["socket_id"] = socket_id
        pf["numa_node"] = numa_node

        rp_uuid = _find_neutron_rp_for_bdf(client, context, host_name, bdf)
        if not rp_uuid:
            LOG.info(
                'NIC topology: no Placement RP found for PF %s (host %s); '
                'skipping. This is normal if the PF is not managed by '
                'Neutron or Neutron has not reported it yet.',
                bdf, host_name,
            )
            continue

        _patch_topology_traits(client, rp_uuid, socket_id, numa_node)

    return []
