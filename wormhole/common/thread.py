from wormhole import exception
from wormhole.i18n import _
from wormhole.common import utils
from wormhole.common import jsonutils
from wormhole.common import processutils
from wormhole.common import excutils
from wormhole.common import log as logging
from eventlet import greenthread

class Thread(object):
    """Class that reads chunks from the input file and writes them to the
    output file till the transfer is completely done.
    """

    def __init__(self, callback, *args, **kwargs):
        self.callback = callback
        self.args = args
        self.kwargs = kwargs
        self._done = False

    def start(self):

        def _inner():
            """Read data from the input and write the same to the output
            until the transfer completes.
            """
            callback(*self.args, **self.kwargs)
            self._done = True

        greenthread.spawn(_inner)
        return self

    def done(self):
        return self._done

def test_001():
    callback = utils.execute

    t = Thread(callback, "sleep", "10")
    t.start()
    print "good", t.done()
    callback("sleep", "1")
    print "good 0", t.done()
    callback("sleep", "10")
    print "good 0", t.done()
