from oslo_config import cfg
from wormhole.i18n import _
from wormhole.common import log
from wormhole.common import utils
from wormhole.common import excutils
from wormhole import exception
from wormhole.net_util import network

import os
import stat

lxc_opts = [
    cfg.StrOpt('vif_driver',
               default='wormhole.net_util.vifs.GenericVIFDriver'),
    cfg.BoolOpt('insecure_registry',
                default=False,
                help='Set true if need insecure registry access.'),
]

CONF = cfg.CONF
CONF.register_opts(lxc_opts, 'lxc')

LOG = log.getLogger(__name__)
LXC_MOUNT_DIR = '/lxc/'
LXC_PATH = '/var/lib/lxc'
LXC_TEMPLATE_SCRIPT = '/var/lib/wormhole/bin/lxc-general'

LXC_NET_CONFIG_TEMPLATE = """# new network
lxc.network.type = veth
lxc.network.link = %(bridge)s
lxc.network.veth.pair = %(tap)s
lxc.network.name = %(name)s
lxc.network.flags = up
lxc.network.hwaddr = %(address)s
lxc.network.mtu = %(mtu)s
"""

def lxc_root(name):
    return LXC_PATH + "/" + name + "/"

def lxc_conf_dir(name):
    return lxc_root(name) + "conf.d/"

def lxc_hook_dir(name):
    return lxc_root(name) + "hooks/"

def lxc_device_conf_file(name, device):
    device_name = os.path.basename(device)
    return lxc_conf_dir(name) + "dev_" + device_name + ".conf"

def lxc_net_conf_file(name, vif="all"):
    return lxc_conf_dir(name) + "net_" + vif + ".conf"

def lxc_autodev_hook_script(name, device):
    device_name = os.path.basename(device)
    return lxc_hook_dir(name) + "autodev_" + device_name + ".sh"

def lxc_net_conf(name, net_name, vif):

    conf = "## START %s\n"%vif['id'][:11]
    conf += LXC_NET_CONFIG_TEMPLATE % {
                "bridge": "qbr%s"%vif['id'][:11],
                "tap": "tap%s"%vif['id'][:11],
                "name": net_name,
                "mtu": str(vif.get('mtu',1300)),
                "address": vif['address']
            }
    gateway = network.find_gateway(name, vif['network'])
    ip =  network.find_fixed_ip(name, vif['network'])
    if net_name == "eth0":
        if ip: conf += "lxc.network.ipv4 = %s\n" % ip
        if gateway: conf += "lxc.network.ipv4.gateway = %s\n" % gateway
    conf += "## END\n\n"
    return conf


class LXCClient(object):
    def __init__(self):
        pass

    def execute(self, container_id, *cmd):
        out, _err = utils.execute('lxc-attach', '-n', container_id, '--',  *cmd, attempts=1)
        return out

    def list(self, all=True):
        containers, _err = utils.execute('lxc-ls', '-f', '-F', 'NAME,STATE')
        if containers:
            # skip the header
            containers = filter(str.strip, containers.split('\n')[1:])
            return [{'id': name, 'status': state, 'name':name}
                    for name, state in map(str.split, containers)]
        return []

    def inspect_container(self, container_id):
        # need to return the container process pid
        # rsp structure rsp['State']['Pid'] = pid
        info, _err = utils.execute('lxc-info', '-p', '-n', container_id)
        return {'State': {'Pid': info.split()[-1]}} if info else {}

    def create_container(self, name, network_disabled=False):
        try:
            utils.execute('lxc-create', '-n', name, '-t', LXC_TEMPLATE_SCRIPT)
        except Exception as ex:
            with excutils.save_and_reraise_exception():
                LOG.error(_('Faild to start container '
                              '%(name)s: %(ex)s'),
                          {'name': name, 'ex': ex.message})
                self.destroy(name, network_info)


    def destroy(self, name, network_info):
        """Destroy the instance on the LXD host

        """
        try:
            utils.execute('lxc-destroy', '-f', '-n', name)
            LOG.info('Destroyed for %s' %name)
        except Exception as ex:
            with excutils.save_and_reraise_exception():
                LOG.error(_('Failed to remove container'
                              ' for %(name)s: %(ex)s'),
                          {'name': name, 'ex': ex.message})

    def images(self, name=None):
        return True

    def pull(self, repository, tag=None, insecure_registry=True):
        pass

    def stop(self, name, timeout):
        containers = self.list()
        status = [c['status'] for c in containers if c['name'] == name]or ['']
        if status and status[0] != 'RUNNING':
            return "Container {} is {}, can't stop it".format(name, status[0])
        try:
            utils.execute('lxc-stop', '-n', name, '-t', timeout)
        except Exception as ex:
            with excutils.save_and_reraise_exception():
                LOG.error(_('Failed to stop container'
                              ' for %(name)s: %(ex)s'),
                          {'name': name, 'ex': ex.message})

    def pause(self, name):
        try:
            utils.execute('lxc-freeze', '-n', name)
        except Exception as ex:
            with excutils.save_and_reraise_exception():
                LOG.error(_('Failed to pause container for %(name)s: %(ex)s'),
                          {'name': name, 'ex': ex.message})

    def unpause(self, name):
        try:
            utils.execute('lxc-unfreeze', '-n', name)
        except Exception as ex:
            with excutils.save_and_reraise_exception():
                LOG.error(_('Failed to unpause container for %(name)s: %(ex)s'),
                          {'name': name, 'ex': ex.message})

    def inject_file(self, name, path, content):

        if os.path.isdir(LXC_MOUNT_DIR + os.path.dirname(path)):
            with open(LXC_MOUNT_DIR + path, 'w') as f: f.write(content)
        else:
            raise exception.DirNotFound(dir=os.path.dirname(path))

    def read_file(self, name, path):
        with open(LXC_MOUNT_DIR + path, 'r') as f: return f.read()

    def _dynamic_attach_or_detach_volume(self, name, device, maj, min, attach=True):

        action = 'add' if attach else 'del'

        utils.execute('lxc-device', '-n', name, action, device)

        cgroup_device_allow = '/sys/fs/cgroup/devices/lxc/%s/devices.%s' \
                                %(name, 'allow' if attach else 'deny')
        for i in range(1, 16):
            with open(cgroup_device_allow, 'w') as f:
                f.write('b %(maj)s:%(min)s rwm\n'%{'maj':maj, 'min':min+i})

    def attach_volume(self, name, device, mount_device, static=True):
        try:
            s = os.stat(device)
            if not stat.S_ISBLK(s.st_mode):
                raise exception.InvalidInput(reason='"%s" is not block device'%device)
            maj, min = os.major(s.st_rdev), os.minor(s.st_rdev)
            if not static:
                # ignore mount_device now
                self._dynamic_attach_or_detach_volume(name, device, maj, min, attach=True)
            else:
                conf_path = lxc_device_conf_file(name, device)
                with open(conf_path, 'w') as f:
                    for i in range(16):
                        f.write('lxc.cgroup.devices.allow = '
                                 'b %(maj)s:%(min)s rwm\n'%{'maj':maj, 'min':min+i})

                LOG.info(_("new config path %(path)s for %(device)s"),
                        {'path': conf_path, 'device': device})
                # autodev hook:
                #  add the partitions of this device into the container when it starts
                with open(lxc_autodev_hook_script(name, device), 'w') as f, \
                      open('/proc/partitions', 'r') as p:
                    for line in p:
                        fields = line.split()
                        if fields and fields[-1].startswith(os.path.basename(device)):
                            f.write("mknod --mode=0660 $LXC_ROOTFS_MOUNT/dev/%(device)s "
                                    "b %(maj)s %(min)s\n" % {
                                    "device": fields[-1], "maj":fields[0], "min":fields[1]})


        except Exception as ex:
            with excutils.save_and_reraise_exception():
                LOG.error(_('Failed to attach device %(device)s '
                              ' for %(name)s: %(ex)s'),
                          {'name': name, 'ex': ex.message, 'device': device})

    def detach_volume(self, name, device, mount_device, static=True):
        try:
            s = os.stat(device)
            if not stat.S_ISBLK(s.st_mode):
                raise exception.InvalidInput(reason='"%s" is not block device'%device)
            maj, min = os.major(s.st_rdev), os.minor(s.st_rdev)
            if not static:
                self._dynamic_attach_or_detach_volume(name, device, maj, min, attach=False)
            for cb in  [lxc_device_conf_file, lxc_autodev_hook_script]:
                path = cb(name, device)
                if path and os.isfile(path):
                    os.remove(path)
                    LOG.info(_("delete path %(path)s for %(device)s"),
                            {'path': path, 'device': device})
        except Exception as ex:
            with excutils.save_and_reraise_exception():
                LOG.error(_('Failed to detach device %(device)s '
                              ' for %(name)s: %(ex)s'),
                          {'name': name, 'ex': ex.message, 'device': device})

    def remove_interfaces(self, name, network_info):
        for vif in network_info:
            if_local_name = 'tap%s' % vif['id'][:11]
            utils.trycmd('ip', 'link', 'del', if_local_name, run_as_root=True)
            _file = lxc_net_conf_file(name, vif['id'][:11])
            LOG.debug("remove net conf %s\n", vif['id'][:11])
            if os.path.isfile(_file):
                os.remove(_file)

    def add_interfaces(self, name, network_info, append=True, net_names=[]):
        network_info = network_info or []
        if not append:
            _dir = lxc_conf_dir(name)
            for _f in os.listdir(_dir):
                if _f.startswith('net_') and _f.endswith('.conf'):
                    _f = _dir + "/" + _f
                    os.remove(_f)
                    LOG.debug("remove file %s",  _f)

        if not net_names:
            net_names = ["eth%d"%i for i in range(len(network_info))]
        for net_name, vif in zip(net_names, network_info):
            conf = lxc_net_conf(name, net_name, vif)
            LOG.debug("new net conf %s, content: %s\n", vif['id'][:11], conf)
            with open(lxc_net_conf_file(name, vif['id'][:11]), "w") as f:
                f.write(conf)

    def start(self, name, network_info=None, block_device_info=None, timeout=10):
        # Start the container
        try:
            self.add_interfaces(name, network_info, append=False)
            utils.execute('lxc-start', '-n', name, '-d', '-l', 'DEBUG')
            utils.execute('lxc-wait', '-n', name, '-s', 'RUNNING', '-t', timeout)
        except Exception as ex:
            with excutils.save_and_reraise_exception():
                LOG.error(_('Failed to start container'
                              ' for %(name)s: %(ex)s'),
                          {'name': name, 'ex': ex.message})
