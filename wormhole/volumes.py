import webob
from wormhole import exception
from wormhole import wsgi
from wormhole.tasks import addtask
from wormhole.common import utils
from wormhole.common import units

from oslo.config import cfg
from wormhole.common import log

import functools
import uuid
import os


CONF = cfg.CONF
LOG = log.getLogger(__name__)

volume_opts = [
    cfg.StrOpt('device_symbolic_directory',
               default='/home/.by-volume-id',
               help='Path to use as the volume mapping.'),
    cfg.StrOpt('volume_dd_blocksize',
               default='1M',
               help='The default block size used when copying volume'),
]

CONF.register_opts(volume_opts)

LINK_DIR = "/home/.by-volume-id"
DOCKER_LINK_NAME = "docker-data-device-link"
DEV_DIRECTORY = "/dev/"

def volume_link_path(volume_id):
    return os.path.sep.join([LINK_DIR, volume_id])

def create_symbolic(dev_path, volume_id):
    utils.trycmd('ln', '-sf', dev_path, volume_link_path(volume_id))

def remove_symbolic(volume_id):
    link_file = volume_link_path(volume_id)
    if os.path.islink(link_file):
        os.remove(link_file)

def check_for_odirect_support(src, dest, flag='oflag=direct'):

    # Check whether O_DIRECT is supported
    try:
        utils.execute('dd', 'count=0', 'if=%s' % src, 'of=%s' % dest,
                      flag, run_as_root=True)
        return True
    except processutils.ProcessExecutionError:
        return False

class VolumeController(wsgi.Application):

    def __init__(self):
        super(VolumeController, self).__init__()
        self.volume_device_mapping = {}
        self.setup_volume_mapping()
        
    def setup_volume_mapping(self):
        if self.volume_device_mapping:
            return

        if not os.path.exists(LINK_DIR):
            os.makedirs(LINK_DIR)
            return

        for link in os.listdir(LINK_DIR):
            link_path = volume_link_path(link)
            if os.path.islink(link_path):
                realpath = os.path.realpath(link_path)
                if realpath.startswith(DEV_DIRECTORY):
                    self.volume_device_mapping[link] = realpath
                    LOG.info("found volume mapping %s ==> %s", 
                            link, self.volume_device_mapping[link])

    def list_host_device(self):
        dev_out, _err = utils.trycmd('lsblk', '-dn', '-o', 'NAME,TYPE')
        dev_list = []
        for dev in dev_out.strip().split('\n'):
            name, type = dev.split()
            if type == 'disk' and not name.endswith('da'):
                dev_list.append(DEV_DIRECTORY + name)

        LOG.debug("scan host devices: %s", dev_list)
        return { "devices" : dev_list }

    def list(self, request, scan=True):
        if scan:
            LOG.debug("scaning host scsi devices")
            utils.trycmd("bash", "-c", "for f in /sys/class/scsi_host/host*/scan; do echo '- - -' > $f; done")
        return self.list_host_device()

    def add_mapping(self, volume_id, mountpoint, device=''):
        if not device:
            link_file = volume_link_path(volume_id)
            if os.path.islink(link_file):
                device = os.path.realpath(link_file)
            else:
                LOG.warn("can't find the device of volume %s when attaching volume", volume_id)
                return
        else:
            if not device.startswith(DEV_DIRECTORY):
                device = DEV_DIRECTORY + device
            create_symbolic(device, volume_id)
        self.volume_device_mapping[volume_id] = device

    def get_device(self, volume_id):
        device = self.volume_device_mapping.get(volume_id)
        if not device:
            LOG.warn("can't found mapping for volume %s", volume_id)
            link_path = volume_link_path(volume_id)
            if os.path.islink(link_path):
                realpath = os.path.realpath(link_path)
                if realpath.startswith(DEV_DIRECTORY):
                    self.volume_device_mapping[volume_id] = realpath
                    device = realpath
        if not device:
            raise exception.VolumeNotFound(id=volume_id)
        LOG.debug("found volume mapping: %s ==> %s", volume_id, device)
        return device

    def add_root_mapping(self, volume_id):
        root_dev_path = os.path.realpath(volume_link_path(DOCKER_LINK_NAME))
        self.add_mapping(volume_id, "/docker", root_dev_path)

    def remove_mapping(self, volume):
        if volume in self.volume_device_mapping:
            remove_symbolic(volume)
            del self.volume_device_mapping[volume]

    def attach_volume(self, request, volume, device, mount_device):
        """ attach volume. """
        LOG.debug("attach volume %s : device %s, mountpoint %s", volume, device, mount_device)
        self.add_mapping(volume, mount_device, device)
        return None

    def detach_volume(self, request, volume):
        LOG.debug("dettach volume %s, current volume mapping: %s", volume, self.volume_device_mapping)
        self.remove_mapping(volume)
        return webob.Response(status_int=200)

    def clone_volume(self, request, volume, src_vref):
        LOG.debug("clone volume %s, src_vref %s", volume, src_vref)
        srcstr = self.get_device(src_vref["id"])
        dststr = self.get_device(volume["id"])
        size_in_g = min(int(src_vref['size']), int(volume['size']))

        clone_callback = functools.partial(utils.copy_volume, srcstr, dststr, size_in_g*units.Ki, CONF.volume_dd_blocksize)
        task_id = addtask(clone_callback)

        return {"task_id": task_id }


def create_router(mapper):
    global controller
    
    mapper.connect('/volumes',
                   controller=controller,
                   action='list',
                   conditions=dict(method=['GET']))
    mapper.connect('/volumes/detach',
                   controller=controller,
                   action='detach_volume',
                   conditions=dict(method=['POST']))
    mapper.connect('/volumes/clone',
                   controller=controller,
                   action='clone_volume',
                   conditions=dict(method=['POST']))
    mapper.connect('/volumes/attach',
                   controller=controller,
                   action='attach_volume',
                   conditions=dict(method=['POST']))
    
controller = VolumeController()
