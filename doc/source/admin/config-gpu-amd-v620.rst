================================================
AMD Radeon Pro V620 (gim VFs) — operator guide
================================================

This guide covers operator setup for the AMD Radeon Pro V620 GPU
driver shipped under ``cyborg.accelerator.drivers.gpu.amd``. The
driver supports the V620 Physical Function (PCI ID ``1002:73a1``) and
SR-IOV Virtual Functions (PCI ID ``1002:73ae``) produced by AMD's
out-of-tree ``gim`` (MxGPU-Virtualization) kernel module.

Overview
========

Two representation modes are possible per V620 PF on a given host:

* **PF mode.** ``sriov_numvfs == 0`` (``gim`` not loaded, or ``vf_num``
  set to 0). The PF is emitted as a single Cyborg deployable with
  inventory ``PGPU=1`` and traits ``CUSTOM_AMD_V620``,
  ``CUSTOM_AMD_V620_PF``.
* **VF mode.** ``sriov_numvfs > 0``. The PF itself is *not* a
  deployable; instead each VF is reported as a distinct deployable
  with inventory ``PGPU=1`` and traits ``CUSTOM_AMD_V620``,
  ``CUSTOM_AMD_V620_VF``, ``CUSTOM_AMD_MXGPU``.

Each deployable additionally carries a per-device NUMA trait read
from ``/sys/bus/pci/devices/<BDF>/numa_node``:

* ``CUSTOM_AMD_V620_NUMA<n>`` (e.g. ``CUSTOM_AMD_V620_NUMA0``) for a
  valid NUMA node.
* ``CUSTOM_AMD_V620_NUMA_NONE`` when ``numa_node`` is ``-1`` or
  unreadable.

The driver does not invoke ``gim``. Operators load ``gim`` via
``modprobe`` and the driver reflects whatever state ``gim`` has
established.

Hardware prerequisites
======================

* Server with SBIOS supporting AMD V620 SR-IOV (revision >= 1.2a).
* SBIOS settings: SR-IOV enabled, ARI enabled, IOMMU enabled
  (``VT-d``/``AMD-Vi``).
* Kernel command line on the host:

  .. code-block:: ini

     intel_iommu=on iommu=pt   # Intel host CPUs
     amd_iommu=on   iommu=pt   # AMD host CPUs

* Reboot after BIOS / cmdline changes and confirm with::

     dmesg | grep -i -e DMAR -e IOMMU
     lspci -nn -d 1002:73a1

Loading the ``gim`` kernel module
=================================

The ``gim`` source lives at
https://github.com/amd/MxGPU-Virtualization. Build per the upstream
README and install the resulting ``gim.ko``. Configure the desired VF
count:

.. code-block:: console

   # cat /etc/modprobe.d/gim.conf
   options gim vf_num=4

   # modprobe gim
   # ls -la /sys/bus/pci/devices/0000:c1:00.0/virtfn*
   lrwxrwxrwx ... virtfn0 -> ../0000:c1:00.1
   lrwxrwxrwx ... virtfn1 -> ../0000:c1:00.2
   lrwxrwxrwx ... virtfn2 -> ../0000:c1:00.3
   lrwxrwxrwx ... virtfn3 -> ../0000:c1:00.4

The host must own the V620 PF via ``gim`` only; the in-tree
``amdgpu`` driver should *not* be bound to the PF when ``gim`` is in
use.

Binding VFs to ``vfio-pci``
===========================

V620 VFs carry the distinct device ID ``73ae`` (vs PF ``73a1``), so a
single ``new_id`` write claims every VF on the host:

.. code-block:: console

   # echo "1002 73ae" > /sys/bus/pci/drivers/vfio-pci/new_id

Persist this in a systemd unit or ``/etc/modprobe.d`` snippet for
reboot survival.

After binding, each VF directory under ``/sys/bus/pci/devices/`` shows
``driver -> ../../../bus/pci/drivers/vfio-pci``.

Cyborg configuration
====================

Enable the driver in ``cyborg.conf`` on each compute node hosting
V620 hardware:

.. code-block:: ini

   [agent]
   enabled_drivers = amd_gpu_driver

   [gpu_devices]
   # Default values; uncomment and edit only to support additional
   # SKUs.
   # enabled_amd_pf_product_ids = 73a1
   # enabled_amd_vf_product_ids = 73ae

Restart ``cyborg-agent``. After the next discovery tick the conductor
publishes one Placement RP per VF (or per PF if ``gim`` has not been
loaded) under the compute host RP.

Verify with::

   openstack accelerator device list --host <hostname>

Nova flavor examples
====================

VF passthrough — single VF, NUMA-affined
-----------------------------------------

.. code-block:: console

   openstack flavor create v620.vf \
     --vcpus 4 --ram 16384 --disk 50

   openstack flavor set v620.vf \
     --property hw:cpu_policy=dedicated \
     --property hw:numa_nodes=1 \
     --property "resources:PGPU=1" \
     --property "trait:CUSTOM_AMD_V620_VF=required" \
     --property "trait:CUSTOM_AMD_MXGPU=required"

To pin to NUMA node 0 specifically (e.g. to co-locate with a
NUMA-aligned NIC VF on the same socket):

.. code-block:: console

   openstack flavor set v620.vf.numa0 \
     --property hw:cpu_policy=dedicated \
     --property hw:numa_nodes=1 \
     --property "resources:PGPU=1" \
     --property "trait:CUSTOM_AMD_V620_VF=required" \
     --property "trait:CUSTOM_AMD_V620_NUMA0=required"

PF passthrough (gim not loaded)
--------------------------------

When SR-IOV is not in use, the whole PF is exposed:

.. code-block:: console

   openstack flavor set v620.pf \
     --property hw:cpu_policy=dedicated \
     --property "resources:PGPU=1" \
     --property "trait:CUSTOM_AMD_V620_PF=required"

Known limitations
=================

* Live migration of V620-attached instances is unsupported (a
  Cyborg-wide limitation, not specific to this driver).
* The PF/VF mode is decided at discovery time. Changing
  ``sriov_numvfs`` on a host with bound instances requires draining
  the host first; ``gim`` will refuse to drop ``sriov_numvfs`` while
  any VF is in use.
* Cross-product NUMA co-location ("GPU VF + NIC VF + pinned vCPUs on
  the same socket") relies on operator-side discipline plus the
  per-VF NUMA trait. Full ``same_subtree=`` allocation requests
  require Nova-side work outside the scope of this driver.
