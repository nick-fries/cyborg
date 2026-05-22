==========================
Topology-Aware Scheduling
==========================

This page documents the *topology-aware subtree* feature added by
the V620 / topology-aware-scheduling fork. It composes three
moving pieces:

1. A **socket+NUMA anchor tree** in Cyborg (this document).
2. A **vendor-agnostic NIC topology driver** in Cyborg (this
   document).
3. A **Nova ``TopologyAffinityFilter``** in the companion Nova
   patch (see Nova release notes).

Operators opt in per host. The feature is OFF by default and
backwards-compatible with prior Cyborg deployments.

.. contents:: :local:

Overview
========

When ``[placement] numa_aware_subtree = True`` is set in
``cyborg.conf``, the Cyborg conductor stops parenting accelerator
deployable resource providers (RPs) directly under the compute
host root. Instead it interposes two anchor RPs that mirror the
host's CPU topology:

.. code-block:: text

    host_root
      └── <host>_socket_<s>          (trait CUSTOM_SOCKET_ROOT)
            └── <host>_numa_<n>      (trait HW_NUMA_ROOT)
                  └── deployable     (e.g. an AMD V620 VF)

The companion ``nic_topology_driver`` does NOT add NIC RPs to this
tree. Neutron's flat NIC RPs stay where they are. Instead the
driver PATCHes ``CUSTOM_TOPO_SOCKET<n>`` and ``CUSTOM_TOPO_NUMA<n>``
traits onto each Neutron-written NIC RP, leaving Neutron's
existing traits intact.

The Nova ``TopologyAffinityFilter`` reads
``hw:cyborg_locality`` from the flavor extra-spec:

* ``socket`` -- accelerator and NIC must share a socket anchor.
* ``numa`` -- accelerator and NIC must share a NUMA anchor.
* ``none`` (default) -- no constraint.

The filter walks the Placement tree for each allocation candidate's
accelerator RP to find its anchor, and reads the candidate's NIC
RP traits to find its socket/NUMA. If they don't match, the
candidate is pruned.

Enabling the Cyborg topology tree
==================================

On each Cyborg-running compute (or the controller for legacy
single-node deployments):

.. code-block:: ini

   [placement]
   numa_aware_subtree = True

Restart the Cyborg conductor and agent. On the next reconcile
pass, every existing deployable RP gets re-parented under a
new ``<host>_socket_<n>`` / ``<host>_numa_<n>`` chain. Garbage
collection is automatic: when the last child of a NUMA or socket
anchor is removed, the anchor itself is deleted (best-effort, gated
on no live allocations).

The flag controls both the socket and NUMA layers, because the
two layers form a single conceptual feature. Tearing down only
one of them is not supported.

Enabling the NIC topology driver
================================

The driver is a stevedore-loadable accelerator driver. Add it to
``[agent] enabled_drivers`` in ``cyborg.conf`` AND enable it:

.. code-block:: ini

   [agent]
   enabled_drivers = amd_gpu_driver, nic_topology_driver

   [nic_topology]
   enabled = True
   # Defaults below; override only if you have unusual hardware
   # or non-default trait naming.
   # pci_class_prefixes = 02
   # socket_trait_prefix = CUSTOM_TOPO_SOCKET
   # numa_trait_prefix = CUSTOM_TOPO_NUMA

Restart the Cyborg agent. On the next reconcile, the driver walks
``/sys/bus/pci/devices``, identifies every network-class PCI PF,
derives its socket/NUMA from sysfs, and PATCHes the two locality
traits onto whatever Placement RP Neutron has written for that PF.

The driver returns NO Cyborg deployables -- it is read/PATCH only.
Neutron continues to own its NIC RPs; Cyborg only annotates them.

Sample Nova flavor
==================

.. code-block:: bash

   openstack flavor create gpu-nic-same-socket \
       --vcpus 8 --ram 16384 --disk 40
   openstack flavor set gpu-nic-same-socket \
       --property hw:cpu_policy=dedicated \
       --property hw:numa_nodes=1 \
       --property accel:device_profile=v620-vf \
       --property hw:cyborg_locality=socket

Boot the instance with a vNIC of any type (``direct``, ``vdpa``,
``direct-physical``, ``virtio-forwarder``); the filter is
vnic-type-agnostic. The Nova scheduler will only consider hosts
whose chosen V620 VF and NIC RP share a socket anchor.

Use ``hw:cyborg_locality=numa`` for the stricter "same NUMA node"
constraint, useful when your V620 hosts have multiple NUMA nodes
per socket (SNC / chiplet layouts).

Behavior on different host shapes
=================================

* **1P (single socket) hosts.** All deployables sit under a
  single ``<host>_socket_0`` anchor. A ``hw:cyborg_locality=socket``
  flavor matches trivially -- the filter walk finds socket_0 on
  every accel RP and ``CUSTOM_TOPO_SOCKET0`` on every NIC RP.
  We do *not* special-case 1P: the tree shape is identical to 2P,
  just with fewer siblings.

* **2P (two socket) hosts.** Each socket gets its own anchor.
  Cross-socket allocations are filtered out for
  ``hw:cyborg_locality=socket`` requests.

* **2P with 2 NUMA per socket (SNC, chiplet).** Four NUMA anchors,
  two per socket. ``hw:cyborg_locality=socket`` allows NUMA
  movement within a socket; ``hw:cyborg_locality=numa`` does not.

* **4P+ hosts.** Same shape, more siblings. No upper bound on
  socket count.

* **Hosts where socket cannot be read.** If
  ``/sys/bus/pci/devices/<bdf>/local_cpulist`` is unreadable, the
  socket_id attribute is ``-1`` and the deployable parents
  directly under whatever the next-up layer is (NUMA anchor if
  numa_node is known, host root otherwise). The Nova filter
  treats missing anchors as "passing with a warning" -- it does
  not fail closed, because operator visibility into anchor
  presence is via Placement queries, not via failed bookings.

Troubleshooting
===============

**Symptom:** The socket trait was never PATCHed onto my NIC RP.

* Confirm the PF actually has a Placement RP. Neutron's PCI-in-
  Placement reporting runs on Nova compute -- if the host hasn't
  reported, there is no RP to PATCH.
* Confirm the RP name actually contains the PF's BDF as a
  substring. The driver matches by substring; if your Neutron
  fork uses an unusual naming convention, set the
  ``[nic_topology]`` config keys to match.
* Check the agent log for
  ``NIC topology: no Placement RP found for PF <BDF>``.
  This is an INFO-level message, not an error.

**Symptom:** The Nova ``TopologyAffinityFilter`` rejects every
host.

* Confirm at least one of the matched NIC RPs has a
  ``CUSTOM_TOPO_SOCKET<n>`` trait. If not, the topology driver
  isn't running or matched no RP -- see above.
* Confirm the accelerator RP's parent chain leads to a
  ``CUSTOM_SOCKET_ROOT``-tagged anchor. If not,
  ``numa_aware_subtree`` isn't enabled or the driver didn't emit
  a ``socket_id`` attribute.
* The filter is permissive on missing data: it logs a warning and
  passes the candidate rather than failing closed. If you see
  hosts pass that shouldn't, check the scheduler log for those
  warnings.

**Symptom:** Existing flavors don't get topology-aware
scheduling.

* The default for ``hw:cyborg_locality`` is ``none``. The
  filter must be explicitly opted into per flavor; it is also
  inactive on flavors that don't set this extra-spec.
* The filter must be present in
  ``[filter_scheduler] enabled_filters`` (Nova).
