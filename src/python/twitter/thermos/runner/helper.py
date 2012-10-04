from contextlib import closing
import errno
import os
import signal
import time

from twitter.common import log
from twitter.common.dirutil import safe_mkdir, lock_file
from twitter.common.process import ProcessProviderFactory
from twitter.common.quantity import Amount, Time
from twitter.common.recordio import ThriftRecordWriter
from twitter.thermos.base.ckpt import CheckpointDispatcher
from twitter.thermos.base.path import TaskPath

from gen.twitter.thermos.ttypes import (
  ProcessState,
  ProcessStatus,
  RunnerCkpt,
  TaskState,
  TaskStatus)


class TaskKiller(object):
  """
    Task killing interface.
  """
  def __init__(self, task_id, checkpoint_root):
    self._task_id = task_id
    self._checkpoint_root = checkpoint_root

  def kill(self, force=True):
    TaskRunnerHelper.kill(self._task_id, self._checkpoint_root, force=force,
                          terminal_status=TaskState.KILLED)

  def lose(self, force=True):
    TaskRunnerHelper.kill(self._task_id, self._checkpoint_root, force=force,
                          terminal_status=TaskState.LOST)


# TODO(wickman) Clock is used haphazardly.
#
# TaskRunnerHelper is sort of a mishmash of "checkpoint-only" operations and
# the "Process Platform" stuff that started to get pulled into process.py
#
# This really needs some hard design thought to see if it can be extracted out
# even further.

class TaskRunnerHelper(object):
  """
    TaskRunner helper methods that can be operated directly upon checkpoint
    state.  These operations do not require knowledge of the underlying
    task.
  """
  class PermissionError(Exception): pass
  PS = ProcessProviderFactory.get()

  # Maximum drift between when the system says a task was forked and when we checkpointed
  # its fork_time (used as a heuristic to determine a forked task is really ours instead of
  # a task with coincidentally the same PID but just wrapped around.)
  MAX_START_TIME_DRIFT = Amount(10, Time.SECONDS)

  @staticmethod
  def get_actual_user():
    import getpass, pwd
    try:
      pwd_entry = pwd.getpwuid(os.getuid())
    except KeyError:
      return getpass.getuser()
    return pwd_entry[0]

  @staticmethod
  def process_from_name(task, process_name):
    if task.has_processes():
      for process in task.processes():
        if process.name().get() == process_name:
          return process
    return None

  @classmethod
  def this_is_really_our_pid(cls, process_handle, current_user, start_time, clock=time):
    """
      A heuristic to make sure that this is likely the pid that we own/forked.  Necessary
      because of pid-space wrapping.  We don't want to go and kill processes we don't own,
      especially if the killer is running as root.
    """
    if process_handle.user() != current_user:
      log.info("Expected pid %s to be ours but the pid user is %s and we're %s" % (
        process_handle.pid(), process_handle.user(), current_user))
      return False

    estimated_start_time = clock.time() - process_handle.wall_time()
    if abs(start_time - estimated_start_time) >= cls.MAX_START_TIME_DRIFT.as_(Time.SECONDS):
      log.info("Time drift from the start of %s is %s, real: %s, pid wall: %s, estimated: %s" % (
        process_handle.pid(), abs(start_time - estimated_start_time), start_time,
        process_handle.wall_time(), estimated_start_time))
      return False

    return True

  @classmethod
  def scan_process(cls, state, process_name, clock=time, ps=None):
    """
      Given a process_run and its owner, return the following:
        (coordinator pid, process pid, process tree)
    """
    process_run = state.processes[process_name][-1]
    process_owner = state.header.user

    if ps is None:
      ps = cls.PS
      ps.collect_all()

    coordinator_pid, pid, tree = None, None, set()

    if process_run.coordinator_pid:
      if process_run.coordinator_pid in ps.pids() and cls.this_is_really_our_pid(
          ps.get_handle(process_run.coordinator_pid), process_owner, process_run.fork_time):
        coordinator_pid = process_run.coordinator_pid
      else:
        log.info('  Coordinator %s [pid: %s] completed.' % (process_run.process,
            process_run.coordinator_pid))

    if process_run.pid:
      if process_run.pid in ps.pids() and cls.this_is_really_our_pid(
          ps.get_handle(process_run.pid), process_owner, process_run.start_time, clock=clock):
        pid = process_run.pid
        subtree = ps.children_of(process_run.pid, all=True)
        if subtree:
          tree = set(subtree)
      else:
        log.info('  Process %s [pid: %s] completed.' % (process_run.process, process_run.pid))

    return (coordinator_pid, pid, tree)

  @classmethod
  def scantree(cls, state, clock=time):
    """
      Scan the process tree associated with the provided task state.

      Returns a dictionary of process name => (coordinator pid, pid, pid children)
      If the coordinator is no longer active, coordinator pid will be None.  If the
      forked process is no longer active, pid will be None and its children will be
      an empty set.
    """
    cls.PS.collect_all()
    return dict((process_name, cls.scan_process(state, process_name, ps=cls.PS, clock=clock)) for
                 process_name in state.processes)

  @classmethod
  def safe_signal(cls, pid, sig=signal.SIGTERM):
    try:
      os.kill(pid, sig)
    except OSError as e:
      if e.errno not in (errno.ESRCH, errno.EPERM):
        log.error('Unexpected error in os.kill: %s' % e)
    except Exception as e:
      log.error('Unexpected error in os.kill: %s' % e)

  @classmethod
  def terminate_pid(cls, pid):
    cls.safe_signal(pid, signal.SIGTERM)

  @classmethod
  def kill_pid(cls, pid):
    cls.safe_signal(pid, signal.SIGKILL)

  @classmethod
  def kill_group(cls, pgrp):
    cls.safe_signal(-pgrp, signal.SIGKILL)

  @classmethod
  def _get_process_tuple(cls, state, process_name):
    assert process_name in state.processes and len(state.processes[process_name]) > 0
    cls.PS.collect_all()
    return cls.scan_process(state, process_name, ps=cls.PS)

  @classmethod
  def _get_coordinator_group(cls, state, process_name):
    assert process_name in state.processes and len(state.processes[process_name]) > 0
    return state.processes[process_name][-1].coordinator_pid

  @classmethod
  def terminate_process(cls, state, process_name):
    log.debug('TaskRunnerHelper.terminate_process(%s)' % process_name)
    _, pid, _ = cls._get_process_tuple(state, process_name)
    if pid:
      log.debug('   => SIGTERM pid %s' % pid)
      cls.terminate_pid(pid)
    return bool(pid)

  @classmethod
  def kill_process(cls, state, process_name):
    log.debug('TaskRunnerHelper.kill_process(%s)' % process_name)
    coordinator_pgid = cls._get_coordinator_group(state, process_name)
    coordinator_pid, pid, tree = cls._get_process_tuple(state, process_name)
    # This is super dangerous.  TODO(wickman)  Add a heuristic that determines
    # that 1) there processes that currently belong to this process group
    #  and 2) those processes have inherited the coordinator checkpoint filehandle
    # This way we validate that it is in fact the process group we expect.
    if coordinator_pgid:
      log.debug('   => SIGKILL coordinator group %s' % coordinator_pgid)
      cls.kill_group(coordinator_pgid)
    if coordinator_pid:
      log.debug('   => SIGKILL coordinator %s' % coordinator_pid)
      cls.kill_pid(coordinator_pid)
    if pid:
      log.debug('   => SIGKILL pid %s' % pid)
      cls.kill_pid(pid)
    for child in tree:
      log.debug('   => SIGKILL child %s' % child)
      cls.kill_pid(child)
    return bool(coordinator_pid or pid or tree)

  @classmethod
  def kill_runner(cls, state):
    log.debug('TaskRunnerHelper.kill_runner()')
    assert state, 'Could not read state!'
    assert state.statuses
    pid = state.statuses[-1].runner_pid
    assert pid != os.getpid(), 'Unwilling to commit seppuku.'
    try:
      os.kill(pid, signal.SIGKILL)
      return True
    except OSError as e:
      if e.errno == errno.EPERM:
        # Permission denied
        return False
      elif e.errno == errno.ESRCH:
        # pid no longer exists
        return True
      raise

  @classmethod
  def open_checkpoint(cls, filename, force=False, state=None):
    """
      Acquire a locked checkpoint stream.
    """
    safe_mkdir(os.path.dirname(filename))
    fp = lock_file(filename, "a+")
    if fp in (None, False):
      if force:
        log.info('Found existing runner, forcing leadership forfeit.')
        state = state or CheckpointDispatcher.from_file(filename)
        if cls.kill_runner(state):
          log.info('Successfully killed leader.')
          # TODO(wickman)  Blocking may not be the best idea here.  Perhaps block up to
          # a maximum timeout.  But blocking is necessary because os.kill does not immediately
          # release the lock if we're in force mode.
          fp = lock_file(filename, "a+", blocking=True)
      else:
        log.error('Found existing runner, cannot take control.')
    if fp in (None, False):
      raise cls.PermissionError('Could not open locked checkpoint: %s, lock_file = %s' %
        (filename, fp))
    ckpt = ThriftRecordWriter(fp)
    ckpt.set_sync(True)
    return ckpt

  @classmethod
  def kill(cls, task_id, checkpoint_root, force=False,
           terminal_status=TaskState.KILLED, clock=time):
    """
      An implementation of Task killing that doesn't require a fully
      hydrated TaskRunner object.  Terminal status must be either
      KILLED or LOST state.
    """
    assert terminal_status in (TaskState.KILLED, TaskState.LOST)
    pathspec = TaskPath(root=checkpoint_root, task_id=task_id)
    checkpoint = pathspec.getpath('runner_checkpoint')
    state = CheckpointDispatcher.from_file(checkpoint)
    ckpt = cls.open_checkpoint(checkpoint, force=force, state=state)
    if state is None or state.header is None or state.statuses is None:
      log.error('Cannot update states in uninitialized TaskState!')
      return

    def write_task_state(state):
      update = TaskStatus(state=state, timestamp_ms=int(clock.time() * 1000),
                          runner_pid=os.getpid(), runner_uid=os.getuid())
      ckpt.write(RunnerCkpt(task_status=update))

    def write_process_status(status):
      ckpt.write(RunnerCkpt(process_status=status))

    if cls.is_task_terminal(state.statuses[-1].state):
      log.info('Task is already in terminal state!  Finalizing.')
      cls.finalize_task(pathspec)
      return

    with closing(ckpt):
      write_task_state(TaskState.ACTIVE)
      for process, history in state.processes.items():
        process_status = history[-1]
        if not cls.is_process_terminal(process_status.state):
          if cls.kill_process(state, process):
            write_process_status(ProcessStatus(process=process,
              state=ProcessState.KILLED, seq=process_status.seq + 1, return_code=-9,
              stop_time=clock.time()))
          else:
            if process_status.state is not ProcessState.WAITING:
              write_process_status(ProcessStatus(process=process,
                state=ProcessState.LOST, seq=process_status.seq + 1))
      write_task_state(terminal_status)
    cls.finalize_task(pathspec)

  @classmethod
  def reap_children(cls):
    pids = set()

    while True:
      try:
        pid, status, rusage = os.wait3(os.WNOHANG)
        if pid == 0:
          break
        pids.add(pid)
        log.debug('Detected terminated process: pid=%s, status=%s, rusage=%s' % (
          pid, status, rusage))
      except OSError as e:
        if e.errno != errno.ECHILD:
          log.warning('Unexpected error when calling waitpid: %s' % e)
        break

    return pids

  TERMINAL_PROCESS_STATES = frozenset([
    ProcessState.SUCCESS,
    ProcessState.KILLED,
    ProcessState.FAILED,
    ProcessState.LOST])

  TERMINAL_TASK_STATES = frozenset([
    TaskState.SUCCESS,
    TaskState.FAILED,
    TaskState.KILLED,
    TaskState.LOST])

  @classmethod
  def is_process_terminal(cls, process_status):
    return process_status in cls.TERMINAL_PROCESS_STATES

  @classmethod
  def is_task_terminal(cls, task_status):
    return task_status in cls.TERMINAL_TASK_STATES

  @staticmethod
  def initialize_task(spec, task):
    active_task = spec.given(state='active').getpath('task_path')
    finished_task = spec.given(state='finished').getpath('task_path')
    is_active, is_finished = map(os.path.exists, [active_task, finished_task])
    assert not is_finished
    if not is_active:
      safe_mkdir(os.path.dirname(active_task))
      with open(active_task, 'w') as fp:
        fp.write(task)

  @staticmethod
  def finalize_task(spec):
    active_task = spec.given(state='active').getpath('task_path')
    finished_task = spec.given(state='finished').getpath('task_path')
    is_active, is_finished = map(os.path.exists, [active_task, finished_task])
    assert is_active and not is_finished
    safe_mkdir(os.path.dirname(finished_task))
    os.rename(active_task, finished_task)
    os.utime(finished_task, None)
