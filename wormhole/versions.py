from wormhole import wsgi
import webob.dec


def version_string():
    return '2015.11'


class Versions(wsgi.Application):
    def index(self, req):
        return {
            "versions":
                [{"status": "CURRENT", "id": "v1.0"}]
        }

    @webob.dec.wsgify()
    def __call__(self, request):
        # TODO
        return wsgi.render_response(body=self.index(request))
