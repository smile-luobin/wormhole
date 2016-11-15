import os
import random
import sys

from oslo_config import cfg
import oslo_messaging as messaging

from wormhole import exception
from wormhole.i18n import _
from wormhole.common import log as logging
from wormhole.common import service
from wormhole import wsgi

CONF = cfg.CONF


class WSGIService(object):
    """Provides ability to launch API from a 'paste' configuration."""

    def __init__(self, name, loader=None, use_ssl=False, max_url_len=None):
        """Initialize, but do not start the WSGI server.

        :param name: The name of the WSGI server given to the loader.
        :param loader: Loads the WSGI application using the given name.
        :returns: None

        """
        self.name = name
        self.manager = self._get_manager()
        self.loader = loader or wsgi.Loader()
        self.app = self.loader.load_app(name)
        self.host = '0.0.0.0'
        self.port = CONF.get('port', 7127)
        self.workers = 1
        if self.workers and self.workers < 1:
            worker_name = '%s_workers' % name
            msg = (_("%(worker_name)s value of %(workers)s is invalid, "
                     "must be greater than 0") %
                   {'worker_name': worker_name,
                    'workers': str(self.workers)})
            raise exception.InvalidInput(msg)
        self.use_ssl = use_ssl
        self.server = wsgi.Server(name,
                                  self.app,
                                  host=self.host,
                                  port=self.port,
                                  use_ssl=self.use_ssl,
                                  max_url_len=max_url_len)
        # Pull back actual port used
        self.port = self.server.port
        self.backdoor_port = None

    def reset(self):
        """Reset server greenpool size to default.

        :returns: None

        """
        self.server.reset()

    def _get_manager(self):
        """Initialize a Manager object appropriate for this service.

        Use the service name to look up a Manager subclass from the
        configuration and initialize an instance. If no class name
        is configured, just return None.

        :returns: a Manager instance, or None.

        """
        return None

    def start(self):
        """Start serving this service using loaded configuration.

        Also, retrieve updated port number in case '0' was passed in, which
        indicates a random port should be used.

        :returns: None

        """
        if self.manager:
            self.manager.init_host()
            self.manager.pre_start_hook()
            if self.backdoor_port is not None:
                self.manager.backdoor_port = self.backdoor_port
        self.server.start()
        if self.manager:
            self.manager.post_start_hook()

    def stop(self):
        """Stop serving this API.

        :returns: None

        """
        self.server.stop()

    def wait(self):
        """Wait for the service to stop serving this API.

        :returns: None

        """
        self.server.wait()


def process_launcher():
    return service.ProcessLauncher()


# NOTE(vish): the global launcher is to maintain the existing
#             functionality of calling service.serve +
#             service.wait
_launcher = None


def serve(server, workers=None):
    global _launcher
    if _launcher:
        raise RuntimeError(_('serve() can only be called once'))

    _launcher = service.launch(server, workers=workers)


def wait():
    _launcher.wait()


import routes
import routers


def public_app_factory(global_conf, **local_conf):
    return wsgi.ComposingRouter(routes.Mapper(),
                                [routers.Router()])
