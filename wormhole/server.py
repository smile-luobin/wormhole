import sys
import eventlet

from wormhole import config
from wormhole.common import log as logging
from wormhole import service

def main(servername="wormhole"):
    config.parse_args(sys.argv)
    eventlet.monkey_patch(os=False)
    logging.setup(servername)


    launcher = service.process_launcher()
    server = service.WSGIService(servername, use_ssl=False,
                                         max_url_len=16384)
    launcher.launch_service(server, workers=server.workers or 1)
    launcher.wait()

