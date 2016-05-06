import webob

from wormhole import exception
from wormhole import wsgi

from wormhole.common import log
from wormhole.common import importutils
from wormhole.common import utils
from wormhole.i18n import _
from wormhole.docker_client import DockerHTTPClient
from wormhole.net_util import network

from wormhole.tasks import addtask
from wormhole.tasks import FAKE_SUCCESS_TASK, FAKE_ERROR_TASK

import functools
import uuid
import inspect
import six
import os
import base64
import tempfile
import tarfile
import StringIO
import eventlet

import time
import sys, traceback

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

def check_dev_exist(dev_path):
    """ check /dev/sde exists by `fdisk'. Note `lsblk' can't guarentee that. """
    disk_info, _ignore_err = utils.trycmd('fdisk', '-l', dev_path)
    return disk_info.strip() != ''

class ContainerController(wsgi.Application):

    def __init__(self):
        self._docker = None
        self._container = None
        self._ns_created = False
        vif_class = importutils.import_class(CONF.docker.vif_driver)
        self.vif_driver = vif_class()
        self._setup_volume_mapping()
        super(ContainerController, self).__init__()

    def _setup_volume_mapping(self):
        self._volume_mapping = {}
        self.root_dev_path = os.path.realpath(docker_root_path())

        link_dir = CONF.get('container_volume_link_dir')

        if not os.path.exists(link_dir):
            os.makedirs(link_dir)
            return

        for link in os.listdir(link_dir):
            link_path = volume_link_path(link)
            if os.path.islink(link_path):
                realpath = os.path.realpath(link_path)
                if realpath.startswith("/dev/"):
                    self._volume_mapping[link] = realpath
                    LOG.info("found volume mapping %s ==> %s", 
                            link, self._volume_mapping[link])

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
            # patch socket so that requests to registry is green.
            eventlet.monkey_patch(socket=True)
            self._docker = DockerHTTPClient(CONF.docker.host_url)
        return self._docker

    @property
    def container(self):
        if self._container is None:
            containers = self.docker.containers(all=True)
            if not containers:
                raise exception.ContainerNotFound()
            if len(containers) > 1:
                LOG.warn("Have multiple(%d) containers: %s !", len(containers), containers)
            self._container = { "id" : containers[0]["id"],
                    "name" : (containers[0]["names"] or ["ubuntu-upstart"]) [0] }
        return self._container

    def _attach_bdm(self, block_device_info):
        """ Attach volume, setup symbolic for volume id mapping to device name.
        """
        if block_device_info:
            for bdm in block_device_info.get('block_device_mapping', []):
                LOG.debug("attach block device mapping %s", bdm)
                mount_device = bdm['mount_device']
                volume_id = bdm['connection_info']['data']['volume_id']
                self._add_mapping(volume_id, mount_device, bdm.get('real_device', ''))

    def _update_bdm(self, block_device_info):
        """ Update mapping info. """
        if block_device_info:
            new_volume_mapping = {}
            for bdm in block_device_info.get('block_device_mapping', []):
                LOG.debug("attach block device mapping %s", bdm)
                mount_device = bdm['mount_device']
                size_in_g = bdm['size']
                volume_id = bdm['connection_info']['data']['volume_id']
                new_volume_mapping[volume_id] = {"mount_device" : mount_device, "size": str(size_in_g) + "G"}

            all_devices = utils.list_device()
            to_remove_volumes = set(self._volume_mapping) - set(new_volume_mapping)

            for comm_volume in set(self._volume_mapping).intersection(new_volume_mapping):
                _path = self._volume_mapping[comm_volume]
                _size = new_volume_mapping[comm_volume]['size']
                # If the device not exist or size not match, then remove it.
                if not check_dev_exist(_path) or \
                        any([d['name'] == _path and d['size'] == _size for d in all_devices]):
                    LOG.info("Volume %s doesn't match, update it.", comm_volume)
                    to_remove_volumes.add(comm_volume)

            if to_remove_volumes:
                LOG.info("Possible detach volume when vm is stopped")

                for remove in to_remove_volumes:
                    self._remove_mapping(remove, ensure=False)
            
            to_add_volumes = set(new_volume_mapping) - set(self._volume_mapping)

            if to_add_volumes:
                LOG.info("Possible attach volume when vm is stopped")
                new_devices = [d for d in all_devices if d['name'] not in self._volume_mapping.values()]

                ## group by size
                for size in set([d['size'] for d in new_devices]):
                    _devices = sorted([d['name'] for d in new_devices if d['size'] == size])
                    _to_add_volumes = sorted([v for v in to_add_volumes if new_volume_mapping[v]['size'] == size])
                    LOG.debug("size: %s, new_devices:%s, added_volums:%s", 
                                size, _devices, _to_add_volumes)
                    for add, new_device in zip(_to_add_volumes, _devices):
                        self._add_mapping(add, new_volume_mapping[add]['mount_device'], new_device)

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

    def _get_repository(self, image_name):
        url = CONF.docker.get('registry_url') + '/' + image_name
        return url

    def create(self, request, image_name, image_id, root_volume_id=None, network_info={},
                      block_device_info={}, inject_files=[], admin_password=None):
        """ create the container. """
        if root_volume_id:
            # Create VM from volume, create a symbolic link for the device.
            LOG.info("create new container from volume %s", root_volume_id)
            self._add_root_mapping(root_volume_id)
            pass
        try:
            _ = self.container
            LOG.warn("Already a container exists")
            return  FAKE_SUCCESS_TASK
        except exception.ContainerNotFound:
            repository = self._get_repository(image_name)
            local_image_name = repository + ':' + image_id
            #local_image_name = image_name

            def _do_create_after_download_image(name):
                LOG.debug("create container from image %s", name)
                self.docker.create_container(name, network_disabled=True)
                if admin_password is not None:
                    self._inject_password(admin_password)
                if inject_files:
                    self._inject_files(inject_files, plain=True)
                if block_device_info:
                    try:
                        self._attach_bdm(block_device_info)
                    except Exception as e:
                        LOG.exception(e)

            if self.docker.images(name=local_image_name):
                LOG.debug("Repository = %s already exists", local_image_name)
                _do_create_after_download_image(local_image_name)
                return FAKE_SUCCESS_TASK
            else:
                def _do_pull_image():
                    name = local_image_name
                    try:
                        LOG.debug("starting pull image repository=%s:%s", repository, image_id)
                        resp = self.docker.pull(repository, tag=image_id, insecure_registry=True)
                        LOG.debug("done pull image repository=%s:%s, resp", repository, image_id, resp)
                        if resp.find(image_name + " not found") != -1:
                            LOG.warn("can't pull image, use the local image with name=%s", image_name)
                            name = image_name
                    except Exception as e:
                        name = image_name
                        LOG.exception(e)
                    _do_create_after_download_image(name)
                task = addtask(_do_pull_image)
                LOG.debug("pull image task %s", task)
                return task

    def start(self, request, network_info={}, block_device_info={}):
        """ Start the container. """
        container_id = self.container['id']
        LOG.info("start container %s network_info %s block_device_info %s",
                   container_id, network_info, block_device_info)
        self.docker.start(container_id, privileged=CONF.docker['privileged'], binds=["/dev:/dev"])
        if network_info:
            try:
                self.plug_vifs(network_info)
                self._attach_vifs(network_info)
            except Exception as e:
                msg = _('Cannot setup network for container {}: {}').format(self.container['name'], repr(traceback.format_exception(*sys.exc_info())))
                LOG.debug(msg, exc_info=True)
                raise exception.ContainerStartFailed(msg)
        if block_device_info:
            try:
                self._update_bdm(block_device_info)
            except Exception as e:
                LOG.exception(e)
                raise

    def _stop(self, container_id, timeout=5):
        try:
            self.docker.stop(container_id, max(timeout, 5))
        except dockerErrors.APIError as e:
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
                tar_content = ''
                partial = True
                while partial:
                    partial = os.read(fd, 1<<14)
                    if tar_content: tar_content += partial
                    else: tar_content = partial
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
            raise exception.InjectFailed(path='', reason=repr(e) + str(e.message))
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
        LOG.debug("attach volume %s : device %s, mountpoint %s", volume_id, device, mountpoint)
        if not device:
            link_file = volume_link_path(volume_id)
            if os.path.islink(link_file):
                device = os.path.realpath(link_file)
            else:
                LOG.warn("can't find the device of volume %s when attaching volume", volume_id)
                return
        else:
            if not device.startswith("/dev/"):
                device = "/dev/" + device
            self._volume_mapping[volume_id] = device
            utils.trycmd('ln', '-sf', device, volume_link_path(volume_id))

    def attach_volume(self, request, volume, device, mount_device):
        """ attach volume. """
        self._add_mapping(volume, mount_device, device)
        return None

    def detach_volume(self, request, volume):
        self._remove_mapping(volume)
        return webob.Response(status_int=200)

    def _add_root_mapping(self, volume_id):
        self._add_mapping(volume_id, "/docker", self.root_dev_path)

    def _remove_mapping(self, volume_id, ensure=True):
        link_file = volume_link_path(volume_id)
        if os.path.islink(link_file):
            dev_path = os.path.realpath(link_file)
            # ignore the docker root volume
            if dev_path != self.root_dev_path:
                LOG.debug("dettach volume %s", volume_id)
                if ensure:
                    # ensure the device path is not visible in host/container
                    if not check_dev_exist(dev_path):
                        utils.trycmd('echo', '1', '>', '/sys/block/%s/device/delete' % dev_path.replace('/dev/',''))
                    else: LOG.warn("try to delete device %s, but it seems exist.", dev_path)
                self._volume_mapping.pop(volume_id)
                os.remove(link_file)

    def create_image(self, request, image_name, image_id):
        """ Create a image from the container. """
        repository = self._get_repository(image_name)
        LOG.debug("creating image from repo = %s, tag = %s", repository, image_id)
        def _create_image_cb():
            LOG.debug("pushing image %s", repository)
            self.docker.commit(self.container['id'], repository=repository,
                tag=image_id)
            self.docker.push(repository, tag=image_id, insecure_registry=True)
            LOG.debug("doing image %s", repository)
        task = addtask(_create_image_cb)
        LOG.debug("created image task %s", task)
        return task

    def pause(self, request):
        self.docker.pause(self.container['id'])

    def unpause(self, request):
        self.docker.unpause(self.container['id'])

    def console_output(self, request):
        return { "logs": self.docker.logs(self.container['id']) }

    def status(self, request):
        container = self.docker.containers(all=True)[0]
        return { "status": container['status'] }

    def image_info(self, request):
        image_name = request.GET.get('image_name')
        image_id = request.GET.get('image_id')
        re = self.docker.images(name=self._get_repository(image_name) + ':' + image_id)
        return {"name" : image_name, "id": image_id, "size" : re[0]['size'] if re else 0}

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
    mapper.connect('/container/image-info',
                   controller=controller,
                   action='image_info',
                   conditions=dict(method=['GET']))
