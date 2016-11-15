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


class Task(object):
    TASK_DOING = 0
    TASK_SUCCESS = 1
    TASK_ERROR = 2
    FORMAT_MAP = {
        TASK_DOING: "doing",
        TASK_SUCCESS: "successful",
        TASK_ERROR: "error with {}"
    }

    def __init__(self, tid, callback, *args, **kwargs):
        self.tid = str(tid)
        self.callback = callback
        self.args = args
        self.kwargs = kwargs
        self._code = self.TASK_DOING
        self._msg = ''

    def start(self):

        def _inner():
            """Read data from the input and write the same to the output
            until the transfer completes.
            """
            try:
                LOG.debug("starting doing task")
                self.callback(*self.args, **self.kwargs)
                self._code = self.TASK_SUCCESS
                LOG.debug("ending doing task")
            except Exception as e:
                LOG.exception(e)
                self._code = self.TASK_ERROR
                self._msg = str(e.message)

        greenthread.spawn(_inner)
        return self

    def status(self):
        return {"code": self._code,
                "message": "Task %s is " % self.tid +
                           self.FORMAT_MAP.get(self._code, '').format(
                               self._msg),
                "task_id": self.tid
                }

    @staticmethod
    def success_task():
        t = Task("-1", None)
        t._code = t.TASK_SUCCESS
        return t.status()

    @staticmethod
    def error_task():
        t = Task("-1", None)
        t._code = t.TASK_ERROR
        return t.status()


FAKE_SUCCESS_TASK = Task.success_task()
FAKE_ERROR_TASK = Task.error_task()


class TaskManager(object):
    _task_mapping = {}
    _free_id = 0

    def add_task(self, callback, *args, **kwargs):
        task_id = str(self._free_id)
        t = Task(task_id, callback, *args, **kwargs)
        self._task_mapping[task_id] = t
        t.start()
        self._free_id += 1
        return t.status()

    def query_task(self, task_id):
        task = self._task_mapping.get(task_id)
        if not task:
            raise exception.TaskNotFound(id=task_id)
        return task.status()


_tmanger = TaskManager()

addtask = _tmanger.add_task


class TaskController(wsgi.Application):
    def query(self, request, task):
        return _tmanger.query_task(task)


def create_router(mapper):
    controller = TaskController()

    mapper.connect('/tasks/{task}',
                   controller=controller,
                   action='query',
                   conditions=dict(method=['GET']))
