import webob

from wormhole import exception
from wormhole import wsgi
from wormhole.i18n import _
from wormhole.common import utils
from wormhole.common import jsonutils
from wormhole.common import processutils
from wormhole.common import excutils
from wormhole.common import log
from eventlet import greenthread

LOG = log.getLogger(__name__)

TASK_DOING = 0
TASK_SUCCESS = 1
TASK_ERROR = 2

class Task(object):
    def __init__(self, tid, callback, *args, **kwargs):
        self.tid = tid
        self.callback = callback
        self.args = args
        self.kwargs = kwargs
        self._status = TASK_DOING

    def start(self):

        def _inner():
            """Read data from the input and write the same to the output
            until the transfer completes.
            """
            try:
                self.callback(*self.args, **self.kwargs)
                self._status = TASK_SUCCESS
            except Exception as e:
                LOG.exception(e)
                self._status = TASK_ERROR

        greenthread.spawn(_inner)
        return self

    def status(self):
        return self._status

class TaskManager(object):
    _task_mapping = {}
    _free_id = 0

    def add_task(self, callback, *args, **kwargs):
        task_id = str(self._free_id)
        t = Task(task_id, callback, *args, **kwargs)
        self._task_mapping[task_id] = t
        t.start()
        self._free_id += 1
        return task_id

    def query_task(self, task_id):
        task = self._task_mapping.get(task_id)
        if not task:
            raise exception.TaskNotFound(id=task_id)
        return task.status()

_tmanger = TaskManager()

addtask = _tmanger.add_task

class TaskController(wsgi.Application):
    def query(self, request, task):
        return { "status" : _tmanger.query_task(task)}

def create_router(mapper):
    controller = TaskController()

    mapper.connect('/tasks/{task}',
                   controller=controller,
                   action='query',
                   conditions=dict(method=['GET']))
def test_001():
    callback = utils.execute

    t = Thread(callback, "sleep", "10")
    t.start()
    print "good", t.done()
    callback("sleep", "1")
    print "good 0", t.done()
    callback("sleep", "10")
    print "good 0", t.done()
