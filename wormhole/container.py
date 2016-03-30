import webob

from wormhole import exception
from wormhole import wsgi

from wormhole.common import log
from wormhole.common import importutils
from wormhole.common import utils
from wormhole.i18n import _
from wormhole.docker_client import DockerHTTPClient
from wormhole.net_util import network

import functools
import uuid
import inspect
import six
import os
import base64
import tempfile
import tarfile
import StringIO

from oslo.config import cfg

from docker import errors as dockerErrors

container_opts = [
    cfg.StrOpt('container_volume_link_dir', 
        default="/home/.by-volume-id",
        help='The dir containing symbolic files named volume-id targeting device path.')
]

CONF = cfg.CONF
CONF.register_opts(container_opts)

LOG = log.getLogger(__name__)


def volume_link_path(volume_id):
    return os.path.sep.join([CONF.get('container_volume_link_dir'), volume_id])

def docker_root_path():
    DOCKER_LINK_NAME = "docker-data-device-link"
    return volume_link_path(DOCKER_LINK_NAME)

def create_symbolic(device, volume_id):
    if not device.startswith("/dev/"):
        device = "/dev/" + device
    utils.trycmd('ln', '-sf', device, volume_link_path(volume_id))

def remove_symbolic(volume_id):
    link_file = volume_link_path(volume_id)
    if os.path.islink(link_file):
        os.remove(link_file)

class ContainerController(wsgi.Application):

    def __init__(self):
        self._docker = None
        self._container = None
        self._ns_created = False
        vif_class = importutils.import_class(CONF.docker.vif_driver)
        self.vif_driver = vif_class()
        super(ContainerController, self).__init__()

    def _discovery_use_eth(self):
        net_prefix = 'eth'
        exec_id = self.docker.exec_create(self.container['id'], 'ifconfig -a')
        res = self.docker.exec_start(exec_id)
        _found_dev = set()
        for line in res.split('\n'):
            if line.startswith(net_prefix):
                _found_dev.add(line.split()[0])
        return _found_dev

    def _available_eth_name(self):
        net_prefix = 'eth'
        used_eths = self._discovery_use_eth()
        i = 0
        while 1:
            name = net_prefix + str(i)
            if name not in used_eths:
                LOG.debug("available net name ==> %s", name)
                return name
            i += 1

    @property
    def docker(self):
        if self._docker is None:
            self._docker = DockerHTTPClient(CONF.docker.host_url)
        return self._docker

    @property
    def container(self):
        if self._container is None:
            containers = self.docker.containers(all=True)
            if not containers:
                LOG.error("No containers exists!")
                raise exception.ContainerNotFound()
            if len(containers) > 1:
                LOG.warn("Have multiple(%d) containers: %s !", len(containers), containers)
            self._container = { "id" : containers[0]["id"], 
                    "name" : (containers[0]["names"] or ["ubuntu-upstart"]) [0] }
        return self._container

    def _attach_bdm(self, block_device_info):
        if block_device_info:
            for bdm in block_device_info.get('block_device_mapping', []):
                LOG.debug("attach block device mapping %s", bdm)
                mount_device = bdm['mount_device']
                volume_id = bdm['connection_info']['data']['volume_id']
                self._add_mapping(volume_id, mount_device, bdm.get('real_device', ''))

    def plug_vifs(self, network_info):
        """Plug VIFs into networks."""
        instance = self.container['id']
        for vif in network_info:
            LOG.debug("plug vif %s", vif)
            self.vif_driver.plug(vif, instance)

    def _find_container_pid(self, container_id):
        n = 0
        while True:
            # NOTE(samalba): We wait for the process to be spawned inside the
            # container in order to get the the "container pid". This is
            # usually really fast. To avoid race conditions on a slow
            # machine, we allow 10 seconds as a hard limit.
            if n > 20:
                return
            info = self.docker.inspect_container(container_id)
            if info:
                pid = info['State']['Pid']
                # Pid is equal to zero if it isn't assigned yet
                if pid:
                    return pid
            time.sleep(0.5)
            n += 1

    def _create_ns(self):
        if self._ns_created:
            return
        container_id = self.container['id']
        netns_path = '/var/run/netns'
        if not os.path.exists(netns_path):
            utils.execute(
                'mkdir', '-p', netns_path, run_as_root=True)
        nspid = self._find_container_pid(container_id)
        if not nspid:
            msg = _('Cannot find any PID under container "{0}"')
            raise RuntimeError(msg.format(container_id))
        netns_path = os.path.join(netns_path, container_id)
        utils.execute(
            'ln', '-sf', '/proc/{0}/ns/net'.format(nspid),
            '/var/run/netns/{0}'.format(container_id),
            run_as_root=True)
        utils.execute('ip', 'netns', 'exec', container_id, 'ip', 'link',
                      'set', 'lo', 'up', run_as_root=True)
        self._ns_created = True


    def _attach_vifs(self, network_info):
        """Plug VIFs into container."""
        if not network_info:
            return
        self._create_ns()
        container_id = self.container['id']
        instance = container_id

        for idx, vif in enumerate(network_info):
            new_remote_name = self._available_eth_name()
            self.vif_driver.attach(vif, instance, container_id, new_remote_name)

    def create(self, request, image_name, volume_id=None, network_info={}, block_device_info={}, inject_files=[], admin_password=None):
        """ create the container. """
        if volume_id:
            # Create VM from volume, create a symbolic link for the device.
            LOG.info("create new container from volume %s", volume_id)
            self._add_root_mapping(volume_id)
            pass
        try:
            _ = self.container
            LOG.warn("Already a container exists")
            return None
        except exception.ContainerNotFound:
            self.docker.create_container(image_name, network_disabled=True)
            if admin_password is not None:
                self._inject_password(admin_password)
            if inject_files:
                self._inject_files(inject_files, plain=True)
            return None

    def start(self, request, network_info={}, block_device_info={}):
        """ Start the container. """
        container_id = self.container['id']
        LOG.info("start container %s network_info %s block_device_info %s", 
                     container_id, network_info, block_device_info)
        self.docker.start(container_id, privileged=True)
        if network_info:
            try:
                self.plug_vifs(network_info)
                self._attach_vifs(network_info)
            except Exception as e:
                msg = _('Cannot setup network for container %s: %s').format(self.container['name'], e)
                LOG.debug(msg, exc_info=True)
                raise exception.ContainerStartFailed(msg)
        if block_device_info:
            try: 
                self._attach_bdm(block_device_info)
            except Exception as e:
                pass

    def _stop(self, container_id, timeout=5):
        try:
            self.docker.stop(container_id, max(timeout, 5))
        except errors.APIError as e:
            if 'Unpause the container before stopping' not in e.explanation:
                LOG.warning(_('Cannot stop container: %s'),
                            e, instance=container_id, exc_info=True)
                raise
            self.docker.unpause(container_id)
            self.docker.stop(container_id, timeout)

    def stop(self, request):
        """ Stop the container. """
        container_id = self.container['id']
        LOG.info("stop container %s", container_id)
        return self._stop(container_id)

    def _extract_dns_entries(self, network_info):
        dns = []
        if network_info:
            for net in network_info:
                subnets = net['network'].get('subnets', [])
                for subnet in subnets:
                    dns_entries = subnet.get('dns', [])
                    for dns_entry in dns_entries:
                        if 'address' in dns_entry:
                            dns.append(dns_entry['address'])
        return dns if dns else None

    def unplug_vifs(self, network_info):
        """Unplug VIFs from networks."""
        instance = self.container['id']
        for vif in network_info:
            self.vif_driver.unplug(instance, vif)

    def restart(self, request, network_info={}, block_device_info={}):
        """ Restart the container. """
        # return webob.Response(status_int=204)
        container_id = self.container['id']
        LOG.info("restart container %s", container_id)
        self._stop(container_id)
        try:
            network.teardown_network(container_id)
            self._ns_created = False
            if network_info:
                self.unplug_vifs(network_info)
                netns_file = '/var/run/netns/{0}'.format(container_id)
                # if os.path.exists(netns_file):
                    # os.remove(netns_file)
        except Exception as e:
            LOG.warning(_('Cannot destroy the container network'
                          ' during reboot {0}').format(e),
                        exc_info=True)
            return

        dns = self._extract_dns_entries(network_info)
        self.docker.start(container_id, dns=dns)
        try:
            if network_info:
                self.plug_vifs(network_info)
                self._attach_vifs(network_info)
        except Exception as e:
            LOG.warning(_('Cannot setup network on reboot: %s'), e,
                        exc_info=True)
            return

    def detach_interface(self, request, vif):
        if vif:
            LOG.debug("detach network info %s", vif)
            instance = self.container['id']
            self.vif_driver.unplug(instance, vif)
        return webob.Response(status_int=200)

    def attach_interface(self, request, vif):
        if vif:
            LOG.debug("attach network info %s", vif)
            container_id = self.container['id']
            instance = container_id
            self.vif_driver.plug(vif, instance)
            new_remote_name = self._available_eth_name()
            self._create_ns()
            self.vif_driver.attach(vif, instance, container_id, new_remote_name)
        return webob.Response(status_int=200)

    def _inject_files(self, inject_files, plain=False):
        container_id = self.container['id']

        # docker client API just accept tar data
        fd, name = tempfile.mkstemp(suffix=".tar")
        try:
            for (path, content_base64) in inject_files:
                # Ensure the parent dir of injecting file exists
                dirname = os.path.dirname(path)
                if not dirname:
                    dirname = '/'

                filename = os.path.basename(path)

                content = content_base64 if plain else base64.b64decode(content_base64)
                LOG.debug("inject file %s, content: len = %d, partial = %s", path, len(content), content[:30])

                # ugly but works
                _tarinfo =  tarfile.TarInfo(filename)
                _tarinfo.size = len(content)
                _tar = tarfile.TarFile(name, "w")
                _tar.addfile(_tarinfo, StringIO.StringIO(content))
                _tar.close()

                os.lseek(fd, 0, os.SEEK_SET)
                tar_content = os.read(fd, 1<<30)
                # TODO: file already exists in the container, need to backup?
                self.docker.put_archive(container_id, dirname, tar_content)

        except TypeError as e: # invalid base64 encode
            LOG.exception(e)
            raise exception.InjectFailed(path=path, reason="contents %s" % e.message)
        except dockerErrors.NotFound as e:
            LOG.exception(e)
            raise exception.InjectFailed(path=path, reason="dir " + dirname + " not found")
        except Exception as e:
            LOG.exception(e)
            raise exception.InjectFailed(path='', reason=str(e.message))
        finally:
            LOG.debug("clean temp tar name %d %s", fd, name)
            os.close(fd)
            os.remove(name)

    def inject_files(self, request, inject_files):
        self._inject_files(inject_files, plain=True)
        return webob.Response(status_int=200)


    def _read_file(self, path):
        """ Read container path content. """
        archive = self.docker.get_archive(self.container['id'], path)
        resp = archive[0]
        tar = tarfile.TarFile(fileobj=StringIO.StringIO(resp.data))
        fileobj = tar.extractfile(os.path.basename(path))
        tar.close()
        if fileobj:
            content = fileobj.read()
            fileobj.close()
            return content
        return ''


    def _inject_password(self, admin_password):
        """Set the root password to admin_passwd
        """
        # The approach used here is to copy the password and shadow
        # files from the instance filesystem to local files, make any
        # necessary changes, and then copy them back.

        LOG.debug("Inject admin password admin_passwd=<SANITIZED>")
        admin_user = 'root'

        passwd_path = os.path.join('/etc', 'passwd')
        shadow_path = os.path.join('/etc', 'shadow')

        passwd_data = self._read_file(passwd_path)
        shadow_data = self._read_file(shadow_path)

        new_shadow_data = utils.set_passwd(admin_user, admin_password,
                                      passwd_data, shadow_data)
        self._inject_files([(shadow_path, new_shadow_data)], plain=True)

    def inject_password(self, request, admin_password):
        """ Modify root password. """
        admin_password = base64.b64decode(admin_password)
        self._inject_password(admin_password)

    def _add_mapping(self, volume_id, mountpoint, device=''):
        if not device:
            link_file = volume_link_path(volume_id)
            if os.path.islink(link_file):
                device = os.path.realpath(link_file)
            else:
                LOG.warn("can't find the device of volume %s when attaching volume", volume_id)
                return
        else:
            create_symbolic(device, volume_id)

    def attach_volume(self, request, volume, device, mount_device):
        """ attach volume. """
        LOG.debug("attach volume %s : device %s, mountpoint %s", volume, device, mount_device)
        self._add_mapping(volume, mount_device, device)
        return None

    def detach_volume(self, request, volume):
        LOG.debug("dettach volume %s", volume)
        self._remove_mapping(volume)
        return webob.Response(status_int=200)

    def _add_root_mapping(self, volume_id):
        root_dev_path = os.path.realpath(docker_root_path())
        self._add_mapping(volume_id, "/docker", root_dev_path)

    def _remove_mapping(self, volume):
        remove_symbolic(volume)

    def create_image(self, request, image_id):
        """ Create a image from the container. """
        return None

    def pause(self, request):
        self.docker.pause(self.container['id'])

    def unpause(self, request):
        self.docker.unpause(self.container['id'])

    def console_output(self, request):
        return { "logs": self.docker.logs(self.container['id']) }

    def status(self, request):
        container = self.docker.containers(all=True)[0]
        return { "status": container['status'] }

def create_router(mapper):
    controller = ContainerController()
    mapper.connect('/container/create',
                   controller=controller,
                   action='create',
                   conditions=dict(method=['POST']))
    mapper.connect('/container/start',
                   controller=controller,
                   action='start',
                   conditions=dict(method=['POST']))
    mapper.connect('/container/stop',
                   controller=controller,
                   action='stop',
                   conditions=dict(method=['POST']))
    mapper.connect('/container/restart',
                   controller=controller,
                   action='restart',
                   conditions=dict(method=['POST']))

    mapper.connect('/container/attach-interface',
                   controller=controller,
                   action='attach_interface',
                   conditions=dict(method=['POST']))
    mapper.connect('/container/detach-interface',
                   controller=controller,
                   action='detach_interface',
                   conditions=dict(method=['POST']))

    mapper.connect('/container/inject-files',
                   controller=controller,
                   action='inject_files',
                   conditions=dict(method=['POST']))
    mapper.connect('/container/admin-password',
                   controller=controller,
                   action='inject_password',
                   conditions=dict(method=['POST']))

    mapper.connect('/container/detach-volume',
                   controller=controller,
                   action='detach_volume',
                   conditions=dict(method=['POST']))
    mapper.connect('/container/attach-volume',
                   controller=controller,
                   action='attach_volume',
                   conditions=dict(method=['POST']))

    mapper.connect('/container/create-image',
                   controller=controller,
                   action='create_image',
                   conditions=dict(method=['POST']))

    mapper.connect('/container/pause',
                   controller=controller,
                   action='pause',
                   conditions=dict(method=['POST']))
    mapper.connect('/container/unpause',
                   controller=controller,
                   action='unpause',
                   conditions=dict(method=['POST']))

    mapper.connect('/container/console-output',
                   controller=controller,
                   action='console_output',
                   conditions=dict(method=['GET']))
    mapper.connect('/container/status',
                   controller=controller,
                   action='status',
                   conditions=dict(method=['GET']))
