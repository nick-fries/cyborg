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
Vendor-agnostic NIC topology discovery + Placement trait reconcile.

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
* ``_reconcile_topology_traits(...)`` -- set-reconcile
  ``CUSTOM_TOPO_SOCKET<n>`` / ``CUSTOM_TOPO_NUMA<n>`` against the
  RP's existing trait set in a single GET + (conditional) PUT.
* ``_sweep_orphan_topology_traits(...)`` -- after each cycle, scan
  every host-qualified BDF-bearing RP in Placement and strip stale
  topology traits whose BDF is no longer present on this host.
* ``discover()`` -- orchestrates the above and returns an empty list.

The driver is **not** instantiated unless the operator opts in via
``[nic_topology] enabled = True``. When disabled, ``discover()`` is
a fast no-op (the agent still invokes it, but nothing happens).
"""

import os
import re

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

# BDF substring pattern (case-insensitive). Matches the canonical
# ``0000:31:00.0`` form embedded in RP names by both Neutron and
# Nova-PCI-in-Placement. We only care about the *presence* of a
# BDF-shaped substring in the orphan sweep, not at which character
# offset it appears.
_BDF_RE = re.compile(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]')


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
            'reconciled this cycle.', _PCI_DEVICES_ROOT, e,
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
# Trait reconciliation (Fix 1)
# ---------------------------------------------------------------------------

def _trait_name(prefix, value):
    """Compose ``<PREFIX><value>``. Returns None for negative values."""
    if value is None or int(value) < 0:
        return None
    return "%s%d" % (prefix, int(value))


def _is_topology_trait(trait, socket_prefix, numa_prefix):
    """Return True if ``trait`` is a Cyborg-written topology trait.

    We identify topology traits by exact-prefix match against the
    operator-configured prefixes (``CONF.nic_topology.socket_trait_prefix``
    and ``CONF.nic_topology.numa_trait_prefix``). This is deliberately
    not a hardcoded ``CUSTOM_TOPO_`` check: operators who customize
    the prefixes must still see clean reconciliation.
    """
    return trait.startswith(socket_prefix) or trait.startswith(numa_prefix)


def _reconcile_topology_traits(client, rp_uuid, socket_id, numa_node):
    """Atomically reconcile topology traits on a Placement RP.

    Single GET + (conditional) single PUT. Steps:

    1. Read the operator-configured socket/NUMA trait prefixes once.
    2. GET the RP's current trait list via ``_get_rp_traits``.
    3. Build the **desired** set of topology traits from the sysfs
       reads (empty if both ``socket_id`` and ``numa_node`` are
       unreadable).
    4. Build the **kept** set = (current traits) minus any trait
       matching either prefix. This preserves Neutron's
       ``CUSTOM_PHYSNET_*`` / ``CUSTOM_VNIC_TYPE_*`` and any other
       unrelated traits.
    5. Compute the **final** set = kept | desired.
    6. If final == current, do nothing (no PUT). This makes the
       cycle a true no-op for steady-state hosts.
    7. Otherwise ``_ensure_traits`` the desired traits (idempotent)
       and ``_put_rp_traits`` the final set in one round-trip.

    Crucially this replaces the additive ``add_traits_to_rp`` flow:
    by replacing the full topology subset, a NIC that moved sockets
    has its **old** socket trait stripped on the same PUT that adds
    the new one, with no transient no-trait window.

    Failures during the GET or PUT are logged at WARN and swallowed;
    the next cycle retries.
    """
    socket_prefix = CONF.nic_topology.socket_trait_prefix
    numa_prefix = CONF.nic_topology.numa_trait_prefix

    socket_trait = _trait_name(socket_prefix, socket_id)
    numa_trait = _trait_name(numa_prefix, numa_node)
    desired = {t for t in (socket_trait, numa_trait) if t}

    # Read current traits.
    try:
        traits_json = client._get_rp_traits(rp_uuid)
    except Exception as e:
        LOG.warning(
            'NIC topology: GET traits failed for RP %s: %s; skipping '
            'reconcile this cycle.', rp_uuid, e,
        )
        return
    current = set(traits_json.get('traits', []))

    # Strip any trait whose prefix is one we own.
    kept = {
        t for t in current
        if not _is_topology_trait(t, socket_prefix, numa_prefix)
    }
    final = kept | desired

    if final == current:
        LOG.debug(
            'NIC topology: RP %s already has the desired traits %r; '
            'no PUT needed.', rp_uuid, sorted(desired),
        )
        return

    if desired:
        try:
            client._ensure_traits(list(desired))
        except Exception as e:
            LOG.warning(
                'NIC topology: failed to ensure traits %r exist: %s; '
                'skipping reconcile of RP %s.', sorted(desired), e,
                rp_uuid,
            )
            return

    traits_json['traits'] = sorted(final)
    try:
        client._put_rp_traits(rp_uuid, traits_json)
    except Exception as e:
        LOG.warning(
            'NIC topology: PUT traits failed for RP %s: %s', rp_uuid, e,
        )
        return
    added = sorted(final - current)
    removed = sorted(current - final)
    LOG.info(
        'NIC topology: reconciled RP %s (added=%r, removed=%r).',
        rp_uuid, added, removed,
    )


# ---------------------------------------------------------------------------
# Orphan trait sweep (Fix 2)
# ---------------------------------------------------------------------------

def _strip_topology_traits(client, rp_uuid):
    """Strip every Cyborg-written topology trait from ``rp_uuid``.

    One GET + (conditional) one PUT. Used by the orphan sweep for
    RPs whose BDF is no longer present on this host. Returns the
    number of traits stripped (0 if no change).

    Failures are logged at WARN and the function returns 0 so the
    sweep can continue with the next RP.
    """
    socket_prefix = CONF.nic_topology.socket_trait_prefix
    numa_prefix = CONF.nic_topology.numa_trait_prefix

    try:
        traits_json = client._get_rp_traits(rp_uuid)
    except Exception as e:
        LOG.warning(
            'NIC topology orphan sweep: GET traits failed for RP %s: '
            '%s', rp_uuid, e,
        )
        return 0
    current = list(traits_json.get('traits', []))
    kept = [
        t for t in current
        if not _is_topology_trait(t, socket_prefix, numa_prefix)
    ]
    stripped = len(current) - len(kept)
    if stripped == 0:
        return 0
    traits_json['traits'] = kept
    try:
        client._put_rp_traits(rp_uuid, traits_json)
    except Exception as e:
        LOG.warning(
            'NIC topology orphan sweep: PUT traits failed for RP %s: '
            '%s', rp_uuid, e,
        )
        return 0
    LOG.info(
        'NIC topology orphan sweep: stripped %d topology trait(s) '
        'from RP %s.', stripped, rp_uuid,
    )
    return stripped


def _sweep_orphan_topology_traits(client, host_name, present_bdfs):
    """Strip topology traits from RPs whose BDF is no longer present.

    1. GET ``/resource_providers``.
    2. Filter to RPs whose name contains the host name (case-
       insensitive) AND contains a BDF-shaped substring. The host
       qualification matches ``_find_neutron_rp_for_bdf`` and
       prevents multi-host deployments from racing.
    3. Extract the BDF substring from each name; if it is NOT in
       ``present_bdfs``, strip topology traits via the atomic
       GET/PUT in ``_strip_topology_traits``.
    4. We never DELETE the RP itself - Neutron owns the RP
       lifecycle. We only strip Cyborg-written traits.

    Logs a summary at INFO. Returns a ``(rps_swept, traits_stripped)``
    tuple for tests and operators.
    """
    if not host_name:
        # A blank host name would over-match every RP - refuse.
        LOG.debug(
            'NIC topology orphan sweep: empty host name; skipping.',
        )
        return (0, 0)
    try:
        resp = client.get('/resource_providers')
    except Exception as e:
        LOG.warning(
            'NIC topology orphan sweep: GET /resource_providers '
            'failed: %s', e,
        )
        return (0, 0)
    if resp is None or getattr(resp, 'status_code', 500) != 200:
        LOG.debug(
            'NIC topology orphan sweep: /resource_providers returned '
            '%s; skipping cycle.',
            getattr(resp, 'status_code', None),
        )
        return (0, 0)
    try:
        body = resp.json()
    except Exception:
        return (0, 0)

    host_lc = host_name.lower()
    present_bdfs_lc = {b.lower() for b in present_bdfs}

    rps_swept = 0
    traits_stripped = 0
    for rp in body.get('resource_providers', []):
        name = (rp.get('name') or '')
        name_lc = name.lower()
        if host_lc not in name_lc:
            continue
        m = _BDF_RE.search(name_lc)
        if not m:
            continue
        bdf = m.group(0)
        if bdf in present_bdfs_lc:
            continue
        rp_uuid = rp.get('uuid')
        if not rp_uuid:
            continue
        n = _strip_topology_traits(client, rp_uuid)
        if n > 0:
            rps_swept += 1
            traits_stripped += n
    LOG.info(
        'NIC topology: orphan sweep stripped %d topology traits '
        'from %d RPs.', traits_stripped, rps_swept,
    )
    return (rps_swept, traits_stripped)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def discover():
    """Discover NICs, derive topology, reconcile Neutron RPs.

    The agent calls this once per resource-tracker cycle. Steps:

    1. ``[nic_topology] enabled`` gate. When False, return [].
    2. Enumerate network PFs via ``_discover_network_pfs``.
    3. For each PF, read socket_id + numa_node via sysfs.
    4. Match against a Placement RP by BDF substring.
    5. Reconcile the locality traits (atomic GET + conditional PUT).
    6. Run an orphan sweep against every host-qualified RP whose
       BDF is no longer present on this host, stripping stale
       topology traits.

    Always returns ``[]`` so the agent's
    ``resource_tracker.update_usage`` treats the call as
    "no new Cyborg-managed devices".
    """
    if not getattr(CONF.nic_topology, 'enabled', False):
        LOG.debug('NIC topology driver disabled; nothing to do.')
        return []

    pfs = _discover_network_pfs()

    client = placement_client.PlacementClient()
    context = oslo_context.get_current() or oslo_context.RequestContext(
        user_id=None, project_id=None, overwrite=False,
    )
    host_name = CONF.host

    present_bdfs = set()
    for pf in pfs:
        bdf = pf["bdf"]
        present_bdfs.add(bdf)
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

        _reconcile_topology_traits(client, rp_uuid, socket_id, numa_node)

    # Orphan sweep: always run, even when ``pfs`` is empty (handles
    # the "all NICs removed from host" case so prior topology traits
    # are stripped). Cheap when nothing changes (one GET, no PUTs).
    try:
        _sweep_orphan_topology_traits(client, host_name, present_bdfs)
    except Exception:
        # The sweep must never raise into the agent loop.
        LOG.exception(
            'NIC topology: orphan sweep raised; continuing.'
        )

    return []
