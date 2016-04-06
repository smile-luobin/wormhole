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
    cfg.StrOpt('volume_dd_blocksize',
               default='1M',
               help='The default block size used when copying volume'),
]

CONF.register_opts(volume_opts)
CONF.import_opt('container_volume_link_dir', 'wormhole.container')

def volume_link_path(volume_id):
    return os.path.sep.join([CONF.get('container_volume_link_dir'), volume_id])

class VolumeController(wsgi.Application):

    def __init__(self):
        super(VolumeController, self).__init__()
        self.volume_device_mapping = {}

    def list(self, request, scan=True):
        """ List all host devices. """
        if scan:
            LOG.debug("scaning host scsi devices")
            utils.trycmd("bash", "-c", "for f in /sys/class/scsi_host/host*/scan; do echo '- - -' > $f; done")
        return { "devices" : [d['name'] for d in utils.list_device()] }

    def _get_device(self, volume_id):
        device = self.volume_device_mapping.get(volume_id)
        if not device:
            LOG.warn("can't found mapping for volume %s", volume_id)
            link_path = volume_link_path(volume_id)
            if os.path.islink(link_path):
                realpath = os.path.realpath(link_path)
                if realpath.startswith("/dev/"):
                    self.volume_device_mapping[volume_id] = realpath
                    device = realpath
        if not device:
            raise exception.VolumeNotFound(id=volume_id)
        LOG.debug("found volume mapping: %s ==> %s", volume_id, device)
        return device

    def clone_volume(self, request, volume, src_vref):
        LOG.debug("cloning volume %s, src_vref %s", volume, src_vref)
        srcstr = self._get_device(src_vref["id"])
        dststr = self._get_device(volume["id"])
        size_in_g = min(int(src_vref['size']), int(volume['size']))

        clone_callback = functools.partial(utils.copy_volume, srcstr, dststr,
                                            size_in_g*units.Ki, CONF.volume_dd_blocksize)
        task = addtask(clone_callback)
        LOG.debug("clone volume task %s", task)

        return task


def create_router(mapper):
    controller = VolumeController()

    mapper.connect('/volumes',
                   controller=controller,
                   action='list',
                   conditions=dict(method=['GET']))
    mapper.connect('/volumes/clone',
                   controller=controller,
                   action='clone_volume',
                   conditions=dict(method=['POST']))

