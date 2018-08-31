import copy
from kiwipy import rmq
from tornado import gen

from . import loaders
from . import communications
from . import persistence
from . import exceptions

__all__ = [
    'PAUSE_MSG', 'PLAY_MSG', 'KILL_MSG', 'STATUS_MSG',
    'ProcessLauncher', 'create_continue_body', 'create_launch_body'
]

INTENT_KEY = 'intent'
MESSAGE_KEY = 'message'


class Intent(object):
    PLAY = 'play'
    PAUSE = 'pause'
    KILL = 'kill'
    STATUS = 'status'


PAUSE_MSG = {INTENT_KEY: Intent.PAUSE}
PLAY_MSG = {INTENT_KEY: Intent.PLAY}
KILL_MSG = {INTENT_KEY: Intent.KILL}
STATUS_MSG = {INTENT_KEY: Intent.STATUS}

TASK_KEY = 'task'
TASK_ARGS = 'args'
PERSIST_KEY = 'persist'
# Launch
PROCESS_CLASS_KEY = 'process_class'
ARGS_KEY = 'init_args'
KWARGS_KEY = 'init_kwargs'
NOWAIT_KEY = 'nowait'
# Continue
PID_KEY = 'pid'
TAG_KEY = 'tag'
# Task types
LAUNCH_TASK = 'launch'
CONTINUE_TASK = 'continue'
CREATE_TASK = 'create'


def create_launch_body(process_class, init_args=None, init_kwargs=None, persist=False, nowait=True,
                       loader=None):
    if loader is None:
        loader = loaders.get_object_loader()

    msg_body = {
        TASK_KEY: LAUNCH_TASK,
        TASK_ARGS: {
            PROCESS_CLASS_KEY: loader.identify_object(process_class),
            PERSIST_KEY: persist,
            NOWAIT_KEY: nowait,
            ARGS_KEY: init_args,
            KWARGS_KEY: init_kwargs
        }
    }
    return msg_body


def create_continue_body(pid, tag=None, nowait=False):
    msg_body = {
        TASK_KEY: CONTINUE_TASK,
        TASK_ARGS: {
            PID_KEY: pid,
            NOWAIT_KEY: nowait,
            TAG_KEY: tag
        }
    }
    return msg_body


def create_create_body(process_class, init_args=None, init_kwargs=None, persist=False, loader=None):
    if loader is None:
        loader = loaders.get_object_loader()

    msg_body = {
        TASK_KEY: CREATE_TASK,
        TASK_ARGS: {
            PROCESS_CLASS_KEY: loader.identify_object(process_class),
            PERSIST_KEY: persist,
            ARGS_KEY: init_args,
            KWARGS_KEY: init_kwargs
        }
    }
    return msg_body


class RemoteProcessController(object):
    def __init__(self, communicator):
        self._communicator = communicator

    @gen.coroutine
    def get_status(self, pid):
        status_future = yield rmq.kiwi_to_tornado_future(self._communicator.rpc_send(pid, STATUS_MSG))
        result = yield rmq.kiwi_to_tornado_future(status_future)
        raise gen.Return(result)

    @gen.coroutine
    def pause_process(self, pid):
        play_future = yield rmq.kiwi_to_tornado_future(self._communicator.rpc_send(pid, PAUSE_MSG))
        result = yield rmq.kiwi_to_tornado_future(play_future)
        raise gen.Return(result)

    @gen.coroutine
    def play_process(self, pid):
        play_future = yield rmq.kiwi_to_tornado_future(self._communicator.rpc_send(pid, PLAY_MSG))
        result = yield rmq.kiwi_to_tornado_future(play_future)
        raise gen.Return(result)

    @gen.coroutine
    def kill_process(self, pid, msg=None):
        message = copy.copy(KILL_MSG)
        if msg is not None:
            message[MESSAGE_KEY] = msg

        # Wait for the communication to go through
        kill_future = yield rmq.kiwi_to_tornado_future(self._communicator.rpc_send(pid, message))
        # Now wait for the kill to be enacted
        result = yield rmq.kiwi_to_tornado_future(kill_future)

        raise gen.Return(result)

    @gen.coroutine
    def continue_process(self, pid, tag=None, nowait=False):
        message = create_continue_body(pid=pid, tag=tag, nowait=nowait)
        # Wait for the communication to go through
        continue_future = yield rmq.kiwi_to_tornado_future(self._communicator.task_send(message))
        # Now wait for the result of the task
        result = yield rmq.kiwi_to_tornado_future(continue_future)

        raise gen.Return(result)

    @gen.coroutine
    def launch_process(self, process_class, init_args=None, init_kwargs=None, persist=False, nowait=False,
                       loader=None):
        message = create_launch_body(process_class, init_args, init_kwargs, persist, nowait, loader)

        launch_future = yield rmq.kiwi_to_tornado_future(self._communicator.task_send(message))
        result = yield rmq.kiwi_to_tornado_future(launch_future)

        raise gen.Return(result)

    @gen.coroutine
    def execute_process(self, process_class, init_args=None, init_kwargs=None, nowait=False, loader=None):
        message = create_create_body(process_class, init_args, init_kwargs, persist=True, loader=loader)

        create_future = yield rmq.kiwi_to_tornado_future(self._communicator.task_send(message))
        pid = yield rmq.kiwi_to_tornado_future(create_future)

        message = create_continue_body(pid, nowait=nowait)
        continue_future = yield rmq.kiwi_to_tornado_future(self._communicator.task_send(message))
        result = yield rmq.kiwi_to_tornado_future(continue_future)

        raise gen.Return(result)


class ProcessLauncher(object):
    """
    Takes incoming task messages and uses them to launch processes.

    Expected format of task:
    For launch:
    {
        'task': [LAUNCH_TASK]
        'process_class': [Process class to launch]
        'args': [tuple of positional args for process constructor]
        'kwargs': [dict of keyword args for process constructor].
        'nowait': True or False
    }

    For continue
    {
        'task': [CONTINUE_TASK]
        'pid': [Process ID]
        'nowait': True or False
    }
    """

    def __init__(self, loop=None, persister=None, load_context=None, loader=None):
        self._loop = loop
        self._persister = persister
        self._load_context = load_context if load_context is not None else persistence.LoadSaveContext()

        if loader is not None:
            self._loader = loader
            self._load_context = self._load_context.copyextend(loader=loader)
        else:
            self._loader = loaders.get_object_loader()

    @gen.coroutine
    def __call__(self, communicator, task):
        """
        Receive a task.
        :param task: The task message
        """
        task_type = task[TASK_KEY]
        if task_type == LAUNCH_TASK:
            raise gen.Return((yield self._launch(communicator, **task.get(TASK_ARGS, {}))))
        elif task_type == CONTINUE_TASK:
            raise gen.Return((yield self._continue(communicator, **task.get(TASK_ARGS, {}))))
        elif task_type == CREATE_TASK:
            raise gen.Return((yield self._create(communicator, **task.get(TASK_ARGS, {}))))
        else:
            raise communications.TaskRejected

    @gen.coroutine
    def _launch(self, _communicator, process_class, persist, nowait, init_args=None, init_kwargs=None):
        if persist and not self._persister:
            raise communications.TaskRejected("Cannot persist process, no persister")

        if init_args is None:
            init_args = ()
        if init_kwargs is None:
            init_kwargs = {}

        proc_class = self._loader.load_object(process_class)
        proc = proc_class(*init_args, **init_kwargs)
        if persist:
            self._persister.save_checkpoint(proc)

        if nowait:
            raise gen.Return(proc.pid)

        yield proc.step_until_terminated()
        raise gen.Return(proc.future().result())

    @gen.coroutine
    def _continue(self, _communicator, pid, nowait, tag=None):
        if not self._persister:
            raise communications.TaskRejected("Cannot continue process, no persister")

        try:
            saved_state = self._persister.load_checkpoint(pid, tag)
        except exceptions.PersistenceError as exception:
            raise communications.TaskRejected("Cannot continue process: {}".format(exception))

        proc = saved_state.unbundle(self._load_context)

        if nowait:
            raise gen.Return(proc.pid)

        yield proc.step_until_terminated()
        raise gen.Return(proc.future().result())

    @gen.coroutine
    def _create(self, _communicator, process_class, persist, init_args=None, init_kwargs=None):
        if persist and not self._persister:
            raise communications.TaskRejected("Cannot persist process, no persister")

        if init_args is None:
            init_args = ()
        if init_kwargs is None:
            init_kwargs = {}

        proc_class = self._loader.load_object(process_class)
        proc = proc_class(*init_args, **init_kwargs)
        if persist:
            self._persister.save_checkpoint(proc)

        raise gen.Return(proc.pid)
