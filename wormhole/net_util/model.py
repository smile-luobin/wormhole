# Copyright 2011 OpenStack Foundation
# All Rights Reserved.
#
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

# Constants for the 'vif_type' field in VIF class
VIF_TYPE_OVS = 'ovs'
VIF_TYPE_IVS = 'ivs'
VIF_TYPE_DVS = 'dvs'
VIF_TYPE_IOVISOR = 'iovisor'
VIF_TYPE_BRIDGE = 'bridge'
VIF_TYPE_802_QBG = '802.1qbg'
VIF_TYPE_802_QBH = '802.1qbh'
VIF_TYPE_HW_VEB = 'hw_veb'
VIF_TYPE_MLNX_DIRECT = 'mlnx_direct'
VIF_TYPE_MIDONET = 'midonet'
VIF_TYPE_VHOSTUSER = 'vhostuser'
VIF_TYPE_OTHER = 'other'

# Constants for dictionary keys in the 'vif_details' field in the VIF
# class
VIF_DETAILS_PORT_FILTER = 'port_filter'
VIF_DETAILS_OVS_HYBRID_PLUG = 'ovs_hybrid_plug'
VIF_DETAILS_OVS_TRUNK_PLUG = 'ovs_trunk_plug'
VIF_DETAILS_PHYSICAL_NETWORK = 'physical_network'

# The following two constants define the SR-IOV related fields in the
# 'vif_details'. 'profileid' should be used for VIF_TYPE_802_QBH,
# 'vlan' for VIF_TYPE_HW_VEB
VIF_DETAILS_PROFILEID = 'profileid'
VIF_DETAILS_VLAN = 'vlan'

# Define supported virtual NIC types. VNIC_TYPE_DIRECT and VNIC_TYPE_MACVTAP
# are used for SR-IOV ports
VNIC_TYPE_NORMAL = 'normal'
VNIC_TYPE_DIRECT = 'direct'
VNIC_TYPE_MACVTAP = 'macvtap'
VNIC_TYPE_VHOSTUSER = 'vhostuser'

# Constants for the 'vif_model' values
VIF_MODEL_VIRTIO = 'virtio'
VIF_MODEL_NE2K_PCI = 'ne2k_pci'
VIF_MODEL_PCNET = 'pcnet'
VIF_MODEL_RTL8139 = 'rtl8139'
VIF_MODEL_E1000 = 'e1000'
VIF_MODEL_E1000E = 'e1000e'
VIF_MODEL_NETFRONT = 'netfront'
VIF_MODEL_SPAPR_VLAN = 'spapr-vlan'

# Constant for max length of network interface names
# eg 'bridge' in the Network class or 'devname' in
# the VIF class
NIC_NAME_LEN = 14

