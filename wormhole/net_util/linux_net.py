# Copyright (c) 2011 X.commerce, a business unit of eBay Inc.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
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

"""Implements vlans, bridges, and iptables rules using linux utilities."""

import binascii
import calendar
import inspect
import itertools
import os
import re

from oslo_config import cfg

import six

from wormhole import exception
from wormhole.i18n import _
from wormhole.common import excutils
from wormhole.common import importutils
from wormhole.common import jsonutils
from wormhole.common import log as logging
from wormhole.common import processutils
from wormhole.common import timeutils
from wormhole import paths
from wormhole.common import utils

LOG = logging.getLogger(__name__)


linux_net_opts = [
    cfg.IntOpt('ovs_vsctl_timeout',
               default=120,
               help='Amount of time, in seconds, that ovs_vsctl should wait '
                    'for a response from the database. 0 is to wait forever.'),
    ]

CONF = cfg.CONF
CONF.register_opts(linux_net_opts)


def clean_conntrack(fixed_ip):
    try:
        _execute('conntrack', '-D', '-r', fixed_ip, run_as_root=True,
                 check_exit_code=[0, 1])
    except processutils.ProcessExecutionError:
        LOG.exception(_('Error deleting conntrack entries for %s'), fixed_ip)


def _enable_ipv4_forwarding():
    sysctl_key = 'net.ipv4.ip_forward'
    stdout, stderr = _execute('sysctl', '-n', sysctl_key)
    if stdout.strip() is not '1':
        _execute('sysctl', '-w', '%s=1' % sysctl_key, run_as_root=True)


def _execute(*cmd, **kwargs):
    """Wrapper around utils._execute for fake_network."""
    return utils.execute(*cmd, **kwargs)

def _dnsmasq_pid_for(dev):
    """Returns the pid for prior dnsmasq instance for a bridge/device.

    Returns None if no pid file exists.

    If machine has rebooted pid might be incorrect (caller should check).

    """
    pid_file = _dhcp_file(dev, 'pid')

    if os.path.exists(pid_file):
        try:
            with open(pid_file, 'r') as f:
                return int(f.read())
        except (ValueError, IOError):
            return None


def _ra_pid_for(dev):
    """Returns the pid for prior radvd instance for a bridge/device.

    Returns None if no pid file exists.

    If machine has rebooted pid might be incorrect (caller should check).

    """
    pid_file = _ra_file(dev, 'pid')

    if os.path.exists(pid_file):
        with open(pid_file, 'r') as f:
            return int(f.read())


def _ip_bridge_cmd(action, params, device):
    """Build commands to add/del ips to bridges/devices."""
    cmd = ['ip', 'addr', action]
    cmd.extend(params)
    cmd.extend(['dev', device])
    return cmd


def _set_device_mtu(dev, mtu=None):
    """Set the device MTU."""

    if not mtu:
        mtu = CONF.network_device_mtu
    if mtu:
        utils.execute('ip', 'link', 'set', dev, 'mtu',
                      mtu, run_as_root=True,
                      check_exit_code=[0, 2, 254])


def _create_veth_pair(dev1_name, dev2_name):
    """Create a pair of veth devices with the specified names,
    deleting any previous devices with those names.
    """
    for dev in [dev1_name, dev2_name]:
        delete_net_dev(dev)

    utils.execute('ip', 'link', 'add', dev1_name, 'type', 'veth', 'peer',
                  'name', dev2_name, run_as_root=True)
    for dev in [dev1_name, dev2_name]:
        utils.execute('ip', 'link', 'set', dev, 'up', run_as_root=True)
        utils.execute('ip', 'link', 'set', dev, 'promisc', 'on',
                      run_as_root=True)
        _set_device_mtu(dev)


def _ovs_vsctl(args):
    full_args = ['ovs-vsctl', '--timeout=%s' % CONF.ovs_vsctl_timeout] + args
    try:
        return utils.execute(*full_args, run_as_root=True)
    except Exception as e:
        LOG.error(_("Unable to execute %(cmd)s. Exception: %(exception)s"),
                  {'cmd': full_args, 'exception': e})
        raise e

def _ovs_ofctl(args):
    full_args = ['ovs-ofctl', '--timeout=%s' % CONF.ovs_vsctl_timeout] + args
    try:
        return utils.execute(*full_args, run_as_root=True)
    except Exception as e:
        LOG.error(_("Unable to execute %(cmd)s. Exception: %(exception)s"),
                  {'cmd': full_args, 'exception': e})
        raise e


def create_ovs_bridge(bridge_name):
    bridge_args = ['--', '--may-exist', 'add-br', bridge_name]
    _ovs_vsctl(bridge_args)
    set_db_attribute("Port", bridge_name, "tag", "4095")
    _set_device_mtu(bridge_name)


def create_ovs_vif_port(bridge, dev, iface_id, mac, instance_id,
                        internal=False):
    delete_ovs_vif_port(bridge, dev)
    #interface_args = ['--', '--if-exists', 'del-port', dev, '--',
    interface_args = ['--', 'add-port', bridge, dev,
                      '--', 'set', 'Interface', dev,
                      'external-ids:iface-id=%s' % iface_id,
                      'external-ids:iface-status=active',
                      'external-ids:attached-mac=%s' % mac,
                      'external-ids:vm-uuid=%s' % instance_id]

    if internal:
        interface_args.append("type=internal")

    _ovs_vsctl(interface_args)
    _set_device_mtu(dev)

def create_ovs_patch_port(bridge_name, local_name, remote_name):
    interface_args = ['--', '--may-exist', 'add-port', bridge_name, local_name,
                    '--', 'set', 'Interface', local_name,
                    'type=patch', 'options:peer=%s' % remote_name]

    _ovs_vsctl(interface_args)

def get_ovs_port_ofport(port_name):
    interface_args = (["get", "Interface", port_name, "ofport"])
    output = _ovs_vsctl(interface_args)
    if output:
        return output[0].rstrip("\n\r")

def delete_ovs_bridge(bridge_name):
    bridge_args = ['--', '--if-exists', 'del-br', bridge_name]
    _ovs_vsctl(bridge_args)

def delete_ovs_vif_port(bridge, dev):
    _ovs_vsctl(['--', '--if-exists', 'del-port', bridge, dev])
    delete_net_dev(dev)

def delete_ovs_flows(bridge, ofport):
    flow_args = ['del-flows', bridge, 'in_port=%s' % ofport]
    _ovs_ofctl(flow_args)


def create_evs_dpdk_br(bridge):
    _ovs_vsctl(['add-br', bridge, '--', 'set', 'bridge', bridge, 'datapath_type=dpdk'])
    _ovs_vsctl(['set', "port", bridge, 'tag=4095'])


def create_evs_patch_port(bridge, port, patch_port):
    _ovs_vsctl(['--', 'add-port', bridge, port, '--', 'set', 'interface', port, 'type=patch', 'options:peer=%s' % patch_port])

def create_evs_virtio_port(bridge, dev, iface_id, mac, instance_id,
                        internal=False):

    sc_type = None
    sf_port_id = None
    list_args = ['--', '--if-exists', 'list', 'port', dev]
    if str(_ovs_vsctl(list_args)).find('sf_port_id') != -1:
        columns_args = ['--', '--columns=other_config', 'list', 'port', dev]
        result = str(_ovs_vsctl(columns_args)).split('(')[1].split(')')[0]

        re_sf_port_id= re.compile('.*sf_port_id="(.*?)".*', re.M | re.X)
        match_sf_port_id = re_sf_port_id.search(result)
        if match_sf_port_id:
            sf_port_id = match_sf_port_id.group(1)

        re_sc_type= re.compile('.*sc_type=(.*?),.*', re.M | re.X)
        match_sc_type = re_sc_type.search(result)
        if match_sc_type:
            sc_type = match_sc_type.group(1)


    interface_args = ['--', '--if-exists', 'del-port', dev, '--',
                      'add-port', bridge, dev,
                      '--',  'set', 'port', dev,
                      'other_config:port_type=virtio',
                      '--', 'set', 'Interface', dev,
                      'external-ids:iface-id=%s' % iface_id,
                      'external-ids:iface-status=active',
                      'external-ids:attached-mac=%s' % mac,
                      'external-ids:vm-uuid=%s' % instance_id]

    if internal:
        interface_args.append("type=internal")

    _ovs_vsctl(interface_args)

    sc_interface_args = ['set', 'port', dev]
    if sf_port_id:
        sc_interface_args.append('other_config:sf_port_id=%s' % sf_port_id)
    if sc_type:
        sc_interface_args.append('other_config:sc_type=%s' % sc_type)
    if sf_port_id:
        _ovs_vsctl(sc_interface_args)



def create_evs_virtio_port_bind_numa(bridge, dev, numa_id, iface_id, mac, instance_id,
                        internal=False):
    sc_type = None
    sf_port_id = None
    list_args = ['--', '--if-exists', 'list', 'port', dev]
    if str(_ovs_vsctl(list_args)).find('sf_port_id') != -1:
        columns_args = ['--', '--columns=other_config', 'list', 'port', dev]
        result = str(_ovs_vsctl(columns_args)).split('(')[1].split(')')[0]

        re_sf_port_id= re.compile('.*sf_port_id="(.*?)".*', re.M | re.X)
        match_sf_port_id = re_sf_port_id.search(result)
        if match_sf_port_id:
            sf_port_id = match_sf_port_id.group(1)

        re_sc_type= re.compile('.*sc_type=(.*?),.*', re.M | re.X)
        match_sc_type = re_sc_type.search(result)
        if match_sc_type:
            sc_type = match_sc_type.group(1)

    interface_args = ['--', '--if-exists', 'del-port', dev, '--',
                      'add-port', bridge, dev,
                      '--',  'set', 'port', dev,
                      'other_config:port_type=virtio',
                      'other_config:numa_id=%s' % numa_id,
                      '--', 'set', 'Interface', dev,
                      'external-ids:iface-id=%s' % iface_id,
                      'external-ids:iface-status=active',
                      'external-ids:attached-mac=%s' % mac,
                      'external-ids:vm-uuid=%s' % instance_id]

    if internal:
        interface_args.append("type=internal")

    _ovs_vsctl(interface_args)


    sc_interface_args = ['set', 'port', dev]
    if sf_port_id:
        sc_interface_args.append('other_config:sf_port_id=%s' % sf_port_id)
    if sc_type:
        sc_interface_args.append('other_config:sc_type=%s' % sc_type)
    if sf_port_id:
        _ovs_vsctl(sc_interface_args)


def bridge_exists(bridge_name):
    try:
        _ovs_vsctl(['br-exists', bridge_name])
    except RuntimeError as e:
        with excutils.save_and_reraise_exception() as ctxt:
            if 'Exit code: 2\n' in str(e):
                ctxt.reraise = False
                return False
    return True

def get_evs_port_ofport(port_name):
    interface_args = (["get", "Interface", port_name, "ofport"])
    output = _ovs_vsctl(interface_args)
    if output:
        return output[0].rstrip("\n\r")


def device_exists(device):
    """Check if ethernet device exists."""
    return os.path.exists('/sys/class/net/%s' % device)


def delete_evs_flows(bridge, ofport):
    flow_args = ['del-flows', bridge, 'in_port=%s' % ofport]
    _ovs_ofctl(flow_args)

def delete_evs_port(bridge, port):
    _ovs_vsctl(['--', '--if-exists', 'del-port', bridge, port])

def delete_evs_bridge(bridge ):
    _ovs_vsctl(['--', '--if-exists', 'del-br', bridge])

def create_ivs_vif_port(dev, iface_id, mac, instance_id):
    utils.execute('ivs-ctl', 'add-port',
                   dev, run_as_root=True)


def delete_ivs_vif_port(dev):
    utils.execute('ivs-ctl', 'del-port', dev,
                  run_as_root=True)
    utils.execute('ip', 'link', 'delete', dev,
                  run_as_root=True)


def create_tap_dev(dev, mac_address=None):
    if not device_exists(dev):
        try:
            # First, try with 'ip'
            utils.execute('ip', 'tuntap', 'add', dev, 'mode', 'tap',
                          run_as_root=True, check_exit_code=[0, 2, 254])
        except processutils.ProcessExecutionError:
            # Second option: tunctl
            utils.execute('tunctl', '-b', '-t', dev, run_as_root=True)
        if mac_address:
            utils.execute('ip', 'link', 'set', dev, 'address', mac_address,
                          run_as_root=True, check_exit_code=[0, 2, 254])
        utils.execute('ip', 'link', 'set', dev, 'up', run_as_root=True,
                      check_exit_code=[0, 2, 254])


def delete_net_dev(dev):
    """Delete a network device only if it exists."""
    if device_exists(dev):
        try:
            utils.execute('ip', 'link', 'delete', dev, run_as_root=True,
                          check_exit_code=[0, 2, 254])
            LOG.debug(_("Net device removed: '%s'"), dev)
        except processutils.ProcessExecutionError:
            with excutils.save_and_reraise_exception():
                LOG.error(_("Failed removing net device: '%s'"), dev)


# Similar to compute virt layers, the Linux network node
# code uses a flexible driver model to support different ways
# of creating ethernet interfaces and attaching them to the network.
# In the case of a network host, these interfaces
# act as gateway/dhcp/vpn/etc. endpoints not VM interfaces.
interface_driver = None


def _get_interface_driver():
    global interface_driver
    if not interface_driver:
        interface_driver = importutils.import_object(
                CONF.linuxnet_interface_driver)
    return interface_driver


def plug(network, mac_address, gateway=True):
    return _get_interface_driver().plug(network, mac_address, gateway)


def unplug(network):
    return _get_interface_driver().unplug(network)


def get_dev(network):
    return _get_interface_driver().get_dev(network)


class LinuxNetInterfaceDriver(object):
    """Abstract class that defines generic network host API
    for all Linux interface drivers.
    """

    def plug(self, network, mac_address):
        """Create Linux device, return device name."""
        raise NotImplementedError()

    def unplug(self, network):
        """Destroy Linux device, return device name."""
        raise NotImplementedError()

    def get_dev(self, network):
        """Get device name."""
        raise NotImplementedError()

