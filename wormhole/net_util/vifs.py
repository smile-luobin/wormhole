# Copyright (C) 2013 VMware, Inc
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


from wormhole.common import processutils
from wormhole.common import log as logging
from wormhole import exception
from wormhole.common import utils
from wormhole.i18n import _

from oslo_config import cfg

from . import linux_net
from . import model as network_model
from . import network

import random

network_opts = [
    cfg.IntOpt('network_device_mtu',
               default=9000,
               help='DEPRECATED: THIS VALUE SHOULD BE SET WHEN CREATING THE '
                    'NETWORK. MTU setting for network interface.'),
]

CONF = cfg.CONF

CONF.register_opts(network_opts)

LOG = logging.getLogger(__name__)

class GenericVIFDriver(object):

    def plug(self, vif, instance):
        vif_type = vif['type']

        LOG.debug('plug vif_type=%(vif_type)s instance=%(instance)s '
                  'vif=%(vif)s',
                  {'vif_type': vif_type, 'instance': instance,
                   'vif': vif})

        if vif_type is None:
            raise exception.WormholeException(
                _("Vif_type parameter must be present "
                  "for this vif_driver implementation"))

        # bypass vif check
        self.plug_ovs_hybrid(instance, vif)

    def plug_ovs_hybrid(self, instance, vif):
        """Plug using hybrid strategy

        Create a per-VIF linux bridge, then link that bridge to the OVS
        integration bridge via an ovs internal port device. Then boot the
        VIF on the linux bridge using standard net_util mechanisms.
        """
        if_local_name = 'tap%s' % vif['id'][:11]
        if_remote_name = 'ns%s' % vif['id'][:11]
        iface_id = self.get_ovs_interfaceid(vif)
        br_name = self.get_br_name(vif['id'])
        vm_port_name = self.get_vm_ovs_port_name(vif['id'])
        # Device already exists so return.
        if linux_net.device_exists(if_local_name):
            return
        undo_mgr = utils.UndoManager()

        try:
            if not linux_net.device_exists(br_name):
                utils.execute('brctl', 'addbr', br_name, run_as_root=True)
                undo_mgr.undo_with(lambda: utils.execute('brctl', 'delbr', br_name, run_as_root=True))
                utils.execute('brctl', 'setfd', br_name, 0, run_as_root=True)
                utils.execute('brctl', 'stp', br_name, 'off', run_as_root=True)
                utils.execute('tee',
                              ('/sys/class/net/%s/bridge/multicast_snooping' %
                               br_name),
                              process_input='0',
                              run_as_root=True,
                              check_exit_code=[0, 1])

            #fix bridge's state is down after host reboot.
            
            linux_net.create_ovs_vif_port(self.get_bridge_name(vif),
                                          vm_port_name, iface_id,
                                          vif['address'], instance,
                                          internal=True)
            undo_mgr.undo_with(
                lambda: utils.execute('ovs-vsctl', 'del-port',
                                       self.get_bridge_name(vif),
                                       vm_port_name, run_as_root=True))
            utils.execute('ip', 'link', 'set', self.get_bridge_name(vif), 'up', run_as_root=True)
            utils.execute('ip', 'link', 'set', vm_port_name, 'up', run_as_root=True)
            utils.execute('ip', 'link', 'set', br_name, 'up', run_as_root=True)

            utils.execute('brctl', 'addif', br_name, vm_port_name,
                                run_as_root=True)

        except Exception:
            msg = "Failed to configure Network." \
                " Rolling back the network interfaces %s %s" % (
                    br_name, vm_port_name)
            undo_mgr.rollback_and_reraise(msg=msg, instance=instance)

    def unplug(self, instance, vif):
        vif_type = vif['type']

        LOG.debug('vif_type=%(vif_type)s instance=%(instance)s '
                  'vif=%(vif)s',
                  {'vif_type': vif_type, 'instance': instance,
                   'vif': vif})

        if vif_type is None:
            raise exception.WormholeException(
                _("Vif_type parameter must be present "
                  "for this vif_driver implementation"))

        self.unplug_ovs_hybrid(instance, vif)

    def unplug_ovs_hybrid(self, instance, vif):
        """UnPlug using hybrid strategy

        Unhook port from OVS, unhook port from bridge, delete
        bridge, and delete both veth devices.
        """
        try:
            br_name = self.get_br_name(vif['id'])
            vm_port_name = self.get_vm_ovs_port_name(vif['id'])

            if linux_net.device_exists(br_name):
                utils.execute('brctl', 'delif', br_name, vm_port_name,
                              run_as_root=True)
                utils.execute('ip', 'link', 'set', br_name, 'down',
                              run_as_root=True)
                utils.execute('brctl', 'delbr', br_name,
                              run_as_root=True)

            linux_net.delete_ovs_vif_port(self.get_bridge_name(vif), vm_port_name)
        except processutils.ProcessExecutionError:
            LOG.exception(_("Failed while unplugging vif for %s"), instance)

    def attach(self, vif, instance, container_id, new_remote_name):
        vif_type = vif['type']
        if_local_name = 'tap%s' % vif['id'][:11]
        if_remote_name = 'ns%s' % vif['id'][:11]
        br_name = self.get_br_name(vif['id'])
        gateway = network.find_gateway(instance, vif['network'])
        ip = network.find_fixed_ip(instance, vif['network'])

        LOG.debug('attach vif_type=%(vif_type)s instance=%(instance)s '
                  'vif=%(vif)s',
                  {'vif_type': vif_type, 'instance': instance,
                   'vif': vif})

        try:
            if linux_net.device_exists(if_local_name):
                linux_net.delete_net_dev(if_local_name)

            # veth
            utils.execute('ip', 'link', 'add', 'name', if_local_name, 'type',
                          'veth', 'peer', 'name', if_remote_name,
                          run_as_root=True)

            # Deleting/Undoing the interface will delete all
            # associated resources (remove from the bridge, its pair, etc...)
            utils.execute('brctl', 'addif', br_name, if_local_name,
                          run_as_root=True)
            utils.execute('ip', 'link', 'set', if_local_name, 'up',
                          run_as_root=True)

            utils.execute('ip', 'link', 'set', if_remote_name, 'netns',
                          container_id, run_as_root=True)
            utils.execute('ip', 'netns', 'exec', container_id, 'ip', 'link',
                          'set', 'dev', if_remote_name, 'name', new_remote_name,
                          run_as_root=True)

            utils.execute('ip', 'netns', 'exec', container_id, 'ip', 'link',
                          'set', new_remote_name, 'address', vif['address'],
                          run_as_root=True)
            utils.execute('ip', 'netns', 'exec', container_id, 'ip', 'addr',
                          'add', ip, 'dev', new_remote_name, run_as_root=True)
            utils.execute('ip', 'netns', 'exec', container_id, 'ip', 'link',
                          'set', new_remote_name, 'up', run_as_root=True)

            # Setup MTU on new_remote_name is required if it is a non
            # default value
            #mtu = CONF.network_device_mtu
            mtu = 1300
            if vif.get('mtu') is not None:
                mtu = vif.get('mtu')

            if mtu is not None:
                utils.execute('ip', 'netns', 'exec', container_id, 'ip',
                              'link', 'set', new_remote_name, 'mtu', mtu,
                              run_as_root=True)

            if gateway is not None:
                utils.execute('ip', 'netns', 'exec', container_id,
                              'ip', 'route', 'replace', 'default', 'via',
                              gateway, 'dev', new_remote_name, run_as_root=True)
                # utils.execute('ip', 'netns', 'exec', container_id,
                              # 'ip', 'route', 'add', '0.0.0.0/0', 'via',
                              # gateway,  run_as_root=True)

            # Disable TSO, for now no config option
            utils.execute('ip', 'netns', 'exec', container_id, 'ethtool',
                          '--offload', new_remote_name, 'tso', 'off',
                          run_as_root=True)

        except Exception as e:
            LOG.exception(_("Failed to attach vif: %s"), str(e.message))

    def get_bridge_name(self, vif):
        return 'br-int'
        #return vif['network']['bridge']

    def get_ovs_interfaceid(self, vif):
        return vif.get('ovs_interfaceid') or vif['id']

    def get_br_name(self, iface_id):
        return ("qbr" + iface_id)[:network_model.NIC_NAME_LEN]

    def get_veth_pair_names(self, iface_id):
        return (("qvb%s" % iface_id)[:network_model.NIC_NAME_LEN],
                ("qvo%s" % iface_id)[:network_model.NIC_NAME_LEN])

    def get_hybrid_plug_enabled(self, vif):
        if vif.get('details'):
            return vif['details'].get('ovs_hybrid_plug', False)
        return False

    def get_vm_ovs_port_name(self, iface_id):
        return ("qvm%s" % iface_id)[:network_model.NIC_NAME_LEN]

