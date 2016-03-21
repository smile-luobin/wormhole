import webob
from wormhole import exception
from wormhole import wsgi
from wormhole.common import utils

from oslo.config import cfg
from wormhole.common import log

import functools
import uuid
import os

CONF = cfg.CONF
LOG = log.getLogger(__name__)

volume_opts = [
    cfg.StrOpt('device_symbolic_directory',
               default="/home/.by-volume-id",
               help='Path to use as the volume mapping.'),
]

CONF.register_opts(volume_opts)

LINK_DIR = "/home/.by-volume-id"
DOCKER_LINK_NAME = "docker-data-device-link"
DEV_DIRECTORY = "/dev/"

def create_symbolic(dev_path, volume_id):
    utils.trycmd('ln', '-sf', dev_path, LINK_DIR + os.path.sep + volume_id)

def remove_symbolic(volume_id):
    link_file = LINK_DIR + os.path.sep + volume_id
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


def copy_volume(srcstr, deststr, size_in_m, blocksize, sync=False,
                execute=utils.execute, ionice=None):
    # Use O_DIRECT to avoid thrashing the system buffer cache
    extra_flags = []
    if check_for_odirect_support(srcstr, deststr, 'iflag=direct'):
        extra_flags.append('iflag=direct')

    if check_for_odirect_support(srcstr, deststr, 'oflag=direct'):
        extra_flags.append('oflag=direct')

    # If the volume is being unprovisioned then
    # request the data is persisted before returning,
    # so that it's not discarded from the cache.
    if sync and not extra_flags:
        extra_flags.append('conv=fdatasync')

    blocksize, count = _calculate_count(size_in_m, blocksize)

    cmd = ['dd', 'if=%s' % srcstr, 'of=%s' % deststr,
           'count=%d' % count, 'bs=%s' % blocksize]
    cmd.extend(extra_flags)

    if ionice is not None:
        cmd = ['ionice', ionice] + cmd


    # Perform the copy
    start_time = timeutils.utcnow()
    execute(*cmd, run_as_root=True)
    duration = timeutils.delta_seconds(start_time, timeutils.utcnow())

    # NOTE(jdg): use a default of 1, mostly for unit test, but in
    # some incredible event this is 0 (cirros image?) don't barf
    if duration < 1:
        duration = 1
    mbps = (size_in_m / duration)
    mesg = ("Volume copy details: src %(src)s, dest %(dest)s, "
            "size %(sz).2f MB, duration %(duration).2f sec")
    LOG.debug(mesg % {"src": srcstr,
                      "dest": deststr,
                      "sz": size_in_m,
                      "duration": duration})
    mesg = _("Volume copy %(size_in_m).2f MB at %(mbps).2f MB/s")
    LOG.info(mesg % {'size_in_m': size_in_m, 'mbps': mbps})


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
            link_path = LINK_DIR + os.path.sep + link
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

    def add_mapping(self, volume, mountpoint, device=''):
        if not device:
            link_file = LINK_DIR + os.path.sep + volume
            if os.path.islink(link_file):
                device = os.path.realpath(link_file)
            else:
                LOG.warn("can't find the device of volume %s when attaching volume", volume)
                return
        else:
            if not device.startswith(DEV_DIRECTORY):
                device = DEV_DIRECTORY + device
            create_symbolic(device, volume)
        self.volume_device_mapping[volume] = device

    def add_root_mapping(self, volume_id):
        root_dev_path = os.path.realpath(LINK_DIR + os.path.sep + DOCKER_LINK_NAME)
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
    mapper.connect('/volumes/attach',
                   controller=controller,
                   action='attach_volume',
                   conditions=dict(method=['POST']))
    
controller = VolumeController()
