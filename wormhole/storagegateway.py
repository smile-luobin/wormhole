import textwrap
import webob
from wormhole import exception
from wormhole import wsgi
# from wormhole.tasks import addtask
from wormhole.common import utils
from wormhole.common import units

from os_brick.initiator import connector
from oslo_config import cfg
from wormhole.common import log
from wormhole.i18n import _, _LW

import functools
import uuid
import os

CONF = cfg.CONF
LOG = log.getLogger(__name__)

sg_opts = [
    cfg.StrOpt('server_host',
               default="127.0.0.1",
               help='The host of journal server.'),
    cfg.StrOpt('server_port',
               default="9999",
               help='The port of journal server.'),
    cfg.StrOpt('targets_dir',
               default='/etc/tgt/storage-gateway.d/',
               help='The storage-gateway target files dir'),
]

CONF.register_opts(sg_opts, 'sg')

VOLUME_CONF = textwrap.dedent(r"""
    #target-for-%(volume)s
    <target %(target-iqn)s>
        bs-type hijacker
        bsopts "host=%(host)s\\;port=%(port)s\\;volume=%(volume)s\\;device=%(
        device)s"
        backing-store %(device)s
        initiator-address ALL
    </target>
      """)


class SGController(wsgi.Application):
    def __init__(self):
        super(SGController, self).__init__()
        self.volume_device_mapping = {}

    def _do_tgt_update(self, target_iqn):
        (out, err) = utils.execute('tgt-admin', '--update', target_iqn,
                                   run_as_root=True)
        LOG.debug("StdOut from tgt-admin --update: %s", out)
        LOG.debug("StdErr from tgt-admin --update: %s", err)

    def _persist_conf(self, target_iqn, volume_id, sg_device):
        target_info = VOLUME_CONF % {
            "target_iqn": target_iqn,
            "volume": volume_id,
            "host": CONF.sg.server_host,
            "port": CONF.sg.server_host,
            "device": sg_device
        }
        targets_dir = CONF.sg.targets_dir
        target_path = os.path.join(targets_dir, volume_id)
        if os.path.exists(target_path):
            LOG.warning(_LW('Target file already exists for volume, '
                            'found file at: %s'), target_path)
        utils.robust_file_write(targets_dir, volume_id, target_info)

    def enable_sg(self, target_iqn, volume_id, sg_device):
        self._persist_conf(target_iqn, volume_id, sg_device)
        self._do_tgt_update(target_iqn)

    def _remove_target(self, target_iqn):
        # force delete target
        utils.execute('tgt-admin', '--force', '--delete', target_iqn,
                      run_as_root=True)
        if self._get_target(target_iqn):
            utils.execute('tgt-admin',
                          '--delete',
                          target_iqn,
                          run_as_root=True)

    def _get_target(self, target_iqn):
        (out, err) = utils.execute('tgt-admin', '--show', run_as_root=True)
        lines = out.split('\n')
        for line in lines:
            if target_iqn in line:
                parsed = line.split()
                tid = parsed[1]
                return tid[:-1]
        return None

    def _remove_conf(self, volume_id):
        target_path = os.path.join(CONF.sg.targets_dir, volume_id)
        if not os.path.exists(target_path):
            LOG.warning(_LW('Volume path %s does not exist, '
                            'nothing to remove.'), target_path)
            return
        else:
            os.unlink(target_path)

    def disable_sg(self, target_iqn, volume_id):
        self._remove_target(target_iqn)
        self._remove_conf(volume_id)

    def enable_replication(self, **kwargs):
        pass

    def disable_replication(self, **kwargs):
        pass

    def create_snapshot(self, **kwargs):
        pass

    def delete_snapshot(self, **kwargs):
        pass

    def create_backup(self, **kwargs):
        pass

    def delete_backup(self, **kwargs):
        pass


def create_router(mapper):
    controller = SGController()
    mapper.connect('/sg/enable_sg',
                   controller=controller,
                   action='enable_sg',
                   conditions=dict(method=['POST']))
    mapper.connect('/sg/disable_sg',
                   controller=controller,
                   action='disable_sg',
                   conditions=dict(method=['POST']))
    mapper.connect('/sg/enable_replication',
                   controller=controller,
                   action='enable_replication',
                   conditions=dict(method=['POST']))
    mapper.connect('/sg/disable_replication',
                   controller=controller,
                   action='disable_replication',
                   conditions=dict(method=['POST']))
    mapper.connect('/sg/create_snapshot',
                   controller=controller,
                   action='create_snapshot',
                   conditions=dict(method=['POST']))
    mapper.connect('/sg/delete_snapshot',
                   controller=controller,
                   action='delete_snapshot',
                   conditions=dict(method=['POST']))
    mapper.connect('/sg/create_backup',
                   controller=controller,
                   action='create_backup',
                   conditions=dict(method=['POST']))
    mapper.connect('/sg/delete_backup',
                   controller=controller,
                   action='delete_backup',
                   conditions=dict(method=['POST']))
