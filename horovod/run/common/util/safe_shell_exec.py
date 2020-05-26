# Copyright 2019 Uber Technologies, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import multiprocessing
import os
import re
import signal
import subprocess
import sys
import threading
import time

from horovod.run.util.threads import in_thread, on_event

GRACEFUL_TERMINATION_TIME_S = 5


def terminate_executor_shell_and_children(pid):
    # Terminate the whole process group of pid
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        return

    # Wait GRACEFUL_TERMINATION_TIME_S for pid's termination
    start = time.time()
    while True:
        if time.time() - start > GRACEFUL_TERMINATION_TIME_S:
            break

        try:
            if os.waitpid(pid, os.WNOHANG) != (0, 0):
                return
        except OSError:
            return

        time.sleep(0.1)

    # Kill the whole process group of pid
    os.killpg(os.getpgid(pid), signal.SIGKILL)


def forward_stream(src_stream, dst_stream, prefix, index):
    def prepend_context(line, rank, prefix):
        localtime = time.asctime(time.localtime(time.time()))
        return '{time}[{rank}]<{prefix}>:{line}'.format(
            time=localtime,
            rank=str(rank),
            prefix=prefix,
            line=line
        )

    def write(text):
        if index is not None:
            text = prepend_context(text, index, prefix)
        dst_stream.write(text)
        dst_stream.flush()

    line_buffer = ''
    while True:
        text = os.read(src_stream.fileno(), 1000)
        if text is None:
            break

        if not isinstance(text, str):
            text = text.decode('utf-8')

        if not text:
            break

        for line in re.split('([\r\n])', text):
            line_buffer += line
            if line == '\r' or line == '\n':
                write(line_buffer)
                line_buffer = ''

    # flush the line buffer if it is not empty
    if len(line_buffer):
        write(line_buffer)

    src_stream.close()


def _exec_middleman(command, env, exit_event, stdout, stderr):
    parent_pid = os.getppid()
    if parent_pid == 1:
        # parent terminated already
        return

    stdout_r, stdout_w = stdout
    stderr_r, stderr_w = stderr

    # Close unused file descriptors to enforce PIPE behavior.
    stdout_r.close()
    stderr_r.close()
    os.setsid()

    executor_shell = subprocess.Popen(command, shell=True, env=env,
                                      stdout=stdout_w, stderr=stderr_w)

    stop = threading.Event()
    cleanup_threads = []
    cleanup_threads.append(on_event(exit_event,
                                    terminate_executor_shell_and_children,
                                    args=(executor_shell.pid,),
                                    stop=stop))

    def kill_executor_children_if_parent_dies():
        # The parent pid changes when the parent terminates to init's pid (1)
        while not stop.is_set():
            if os.getppid() != parent_pid:
                print('parent pid changed {} -> {}'.format(parent_pid, os.getppid()))
                break
            time.sleep(0.1)

        print('parent terminated, terminating shell')
        cleanup_threads.append(in_thread(terminate_executor_shell_and_children,
                                         args=(executor_shell.pid,)))

    in_thread(kill_executor_children_if_parent_dies)

    exit_code = executor_shell.wait()

    # wait for all cleanup threads so they get a chance to finish
    stop.set()
    for thread in cleanup_threads:
        thread.join()

    if exit_code < 0:
        # See: https://www.gnu.org/software/bash/manual/html_node/Exit-Status.html
        exit_code = 128 + abs(exit_code)

    sys.exit(exit_code)


def _create_event(ctx):
    # We need to expose this method for internal testing purposes, so we can mock it out to avoid
    # leaking semaphores.
    return ctx.Event()


def execute(command, env=None, stdout=None, stderr=None, index=None, events=None):
    ctx = multiprocessing.get_context('spawn')

    # When this event is set, signal to middleman to terminate its children and exit.
    exit_event = _create_event(ctx)

    # Make a pipe for the subprocess stdout/stderr.
    (stdout_r, stdout_w) = ctx.Pipe()
    (stderr_r, stderr_w) = ctx.Pipe()

    middleman = ctx.Process(target=_exec_middleman, args=(command, env, exit_event,
                                                          (stdout_r, stdout_w),
                                                          (stderr_r, stderr_w)))
    middleman.start()

    # Close unused file descriptors to enforce PIPE behavior.
    stdout_w.close()
    stderr_w.close()

    # Redirect command stdout & stderr to provided streams or sys.stdout/sys.stderr.
    # This is useful for Jupyter Notebook that uses custom sys.stdout/sys.stderr or
    # for redirecting to a file on disk.
    if stdout is None:
        stdout = sys.stdout
    if stderr is None:
        stderr = sys.stderr

    stdout_fwd = in_thread(target=forward_stream, args=(stdout_r, stdout, 'stdout', index))
    stderr_fwd = in_thread(target=forward_stream, args=(stderr_r, stderr, 'stderr', index))

    # TODO: Currently this requires explicitly declaration of the events and signal handler to set
    #  the event (gloo_run.py:_launch_jobs()). Need to figure out a generalized way to hide this behind
    #  interfaces.
    stop = threading.Event()
    events = events or []
    for event in events:
        on_event(event, exit_event.set, stop=stop, silent=True)

    try:
        middleman.join()
    except:
        # interrupted, send middleman TERM signal which will terminate children
        exit_event.set()
        while True:
            try:
                middleman.join()
                break
            except:
                # interrupted, wait for middleman to finish
                pass
    finally:
        stop.set()

    stdout_fwd.join()
    stderr_fwd.join()

    return middleman.exitcode
