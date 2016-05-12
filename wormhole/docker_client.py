from docker import client
from docker import tls
from docker import errors as dockerErrors

import inspect
import six
import functools
from config import cfg

CONF = cfg.CONF

DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_DOCKER_API_VERSION = '1.21'

docker_opts = [
    cfg.StrOpt('root_directory',
               default='/var/lib/docker',
               help='Path to use as the root of the Docker runtime.'),
    cfg.StrOpt('host_url',
               default='unix:///var/run/docker.sock',
               help='tcp://host:port to bind/connect to or '
                    'unix://path/to/socket to use'),
    cfg.BoolOpt('api_insecure',
                default=False,
                help='If set, ignore any SSL validation issues'),
    cfg.StrOpt('ca_file',
               help='Location of CA certificates file for '
                    'securing docker api requests (tlscacert).'),
    cfg.StrOpt('cert_file',
               help='Location of TLS certificate file for '
                    'securing docker api requests (tlscert).'),
    cfg.StrOpt('key_file',
               help='Location of TLS private key file for '
                    'securing docker api requests (tlskey).'),
    cfg.StrOpt('vif_driver',
               default='wormhole.net_util.vifs.DockerGenericVIFDriver'),
    cfg.BoolOpt('inject_key',
                default=False,
                help='Inject the ssh public key at boot time'),
    cfg.StrOpt('shared_directory',
               default=None,
               help='Shared directory where glance images located. If '
                    'specified, docker will try to load the image from '
                    'the shared directory by image ID.'),
    cfg.BoolOpt('privileged',
                default=False,
                help='Set true can own all root privileges in a container.'),

    cfg.StrOpt('registry_url',
        default='162.3.119.15:5000',
               help='Registry url to pull/push images.'),
    cfg.BoolOpt('insecure_registry',
                default=False,
                help='Set true if need insecure registry access.'),
]

CONF.register_opts(docker_opts, 'docker')

def filter_data(f):
    """Decorator that post-processes data returned by Docker.

     This will avoid any surprises with different versions of Docker.
    """
    @functools.wraps(f, assigned=[])
    def wrapper(*args, **kwds):
        attempts = kwds.pop('attempts', 5)
        while 1:
            try:
                out = f(*args, **kwds)
                break
            except dockerErrors.NotFound as e:
                attempts -= 1
                # bug '404 Client Error: Not Found for url: http+docker://localunixsocket/v1.21/containers/create '
                if attempts > 0 and 'Not Found for url:' in str(e):
                    sleep(1)
                else: raise

        def _filter(obj):
            if isinstance(obj, list):
                new_list = []
                for o in obj:
                    new_list.append(_filter(o))
                obj = new_list
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(k, six.string_types):
                        obj[k.lower()] = _filter(v)
            return obj
        return _filter(out)
    return wrapper

class DockerHTTPClient(client.Client):
    def __init__(self, url='unix://var/run/docker.sock'):
        if (CONF.docker.cert_file or
                CONF.docker.key_file):
            client_cert = (CONF.docker.cert_file, CONF.docker.key_file)
        else:
            client_cert = None
        if (CONF.docker.ca_file or
                CONF.docker.api_insecure or
                client_cert):
            ssl_config = tls.TLSConfig(
                client_cert=client_cert,
                ca_cert=CONF.docker.ca_file,
                verify=CONF.docker.api_insecure)
        else:
            ssl_config = False
        super(DockerHTTPClient, self).__init__(
            base_url=url,
            version=DEFAULT_DOCKER_API_VERSION,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            tls=ssl_config
        )
        self._setup_decorators()

    def _setup_decorators(self):
        for name, member in inspect.getmembers(self, inspect.ismethod):
            if not name.startswith('_'):
                setattr(self, name, filter_data(member))

    def pause(self, container_id):
        url = self._url("/containers/{0}/pause".format(container_id))
        res = self._post(url)
        return res.status_code == 204

    def unpause(self, container_id):
        url = self._url("/containers/{0}/unpause".format(container_id))
        res = self._post(url)
        return res.status_code == 204

    def load_repository_file(self, name, path):
        with open(path) as fh:
            self.load_image(fh)

    def get_container_logs(self, container_id):
        return self.attach(container_id, 1, 1, 0, 1)

