from wormhole import wsgi
#from wormhole import container
from wormhole import host
from wormhole import volumes
from wormhole import tasks
from wormhole import storagegateway


class Router(wsgi.ComposableRouter):
    def add_routes(self, mapper):
        for r in [host, volumes, tasks, storagegateway]:
            r.create_router(mapper)
