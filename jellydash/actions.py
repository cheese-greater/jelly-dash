"""Operator actions: run a configured command in a pseudo-terminal and buffer its output
so any number of browsers can follow it live or replay it after a reload."""

import codecs
import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time

MAX_CHUNKS = 5000
MAX_BUFFER_BYTES = 2 * 1024 * 1024
TERMINAL_ROWS = 40
TERMINAL_COLUMNS = 120


class ActionRun:
    def __init__(self):
        self.chunks = []
        self.dropped = 0
        self.buffered_bytes = 0
        self.condition = threading.Condition()
        self.started_at = time.time()
        self.finished_at = None
        self.exit_code = None
        self.pid = None
        self.master = None
        self.process = None

    @property
    def running(self):
        return self.finished_at is None

    def write_input(self, text):
        """Feed keystrokes to the command's terminal, as if typed."""
        with self.condition:
            master = self.master
        if master is None or not self.running:
            raise RuntimeError('not running')
        os.write(master, text.encode('utf-8'))

    def signal_group(self, signal_number):
        with self.condition:
            process = self.process
        if process is None or not self.running:
            raise RuntimeError('not running')
        try:
            os.killpg(os.getpgid(process.pid), signal_number)
        except ProcessLookupError:
            pass

    def append(self, text):
        with self.condition:
            self.chunks.append(text)
            self.buffered_bytes += len(text)
            while self.chunks and (len(self.chunks) > MAX_CHUNKS or self.buffered_bytes > MAX_BUFFER_BYTES):
                self.buffered_bytes -= len(self.chunks.pop(0))
                self.dropped += 1
            self.condition.notify_all()

    def finish(self, exit_code):
        with self.condition:
            self.exit_code = exit_code
            self.finished_at = time.time()
            self.condition.notify_all()

    def wait_from(self, offset, timeout):
        """Return (next_offset, chunks, finished), waiting up to timeout for new output."""
        with self.condition:
            end = self.dropped + len(self.chunks)
            if offset >= end and self.finished_at is None:
                self.condition.wait(timeout)
                end = self.dropped + len(self.chunks)
            offset = max(offset, self.dropped)
            return end, self.chunks[offset - self.dropped:], self.finished_at is not None

    def describe(self):
        return {
            'running': self.running,
            'exit_code': self.exit_code,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'pid': self.pid,
            'chunks': self.dropped + len(self.chunks)
        }


class ActionManager:
    def __init__(self, actions_config):
        self.actions = {}
        for entry in actions_config or []:
            name = str(entry.get('name') or '').strip()
            if not name or not entry.get('command'):
                continue
            self.actions[name] = {
                'name': name,
                'label': entry.get('label') or name,
                'description': entry.get('description') or '',
                'command': entry['command'],
                'confirm': bool(entry.get('confirm', True))
            }
        self.runs = {}
        self._lock = threading.Lock()

    def get(self, name):
        return self.actions.get(name)

    def run_for(self, name):
        return self.runs.get(name)

    def start(self, name):
        action = self.actions[name]
        with self._lock:
            current = self.runs.get(name)
            if current is not None and current.running:
                raise RuntimeError('already running')
            run = ActionRun()
            self.runs[name] = run
        threading.Thread(target=self._execute, args=(action, run), name=f'action-{name}', daemon=True).start()
        return run

    def send_input(self, name, text):
        run = self.runs.get(name)
        if run is None:
            raise RuntimeError('not running')
        run.write_input(text)

    def stop(self, name):
        """SIGTERM the whole process group, then SIGKILL anything still alive a few seconds later."""
        run = self.runs.get(name)
        if run is None:
            raise RuntimeError('not running')
        run.signal_group(signal.SIGTERM)
        run.append('\r\n[stop requested: SIGTERM sent]\r\n')

        def escalate():
            time.sleep(5)
            if run.running:
                try:
                    run.signal_group(signal.SIGKILL)
                    run.append('[still running after 5s: SIGKILL sent]\r\n')
                except RuntimeError:
                    pass
        threading.Thread(target=escalate, daemon=True).start()

    def status(self):
        entries = []
        for name, action in self.actions.items():
            entry = {key: action[key] for key in ('name', 'label', 'description', 'command', 'confirm')}
            run = self.runs.get(name)
            entry['run'] = run.describe() if run else None
            entries.append(entry)
        return entries

    @staticmethod
    def _execute(action, run):
        master, slave = pty.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', TERMINAL_ROWS, TERMINAL_COLUMNS, 0, 0))
        environment = dict(os.environ, TERM='xterm-256color', COLUMNS=str(TERMINAL_COLUMNS), LINES=str(TERMINAL_ROWS))
        environment.setdefault('LANG', 'C.UTF-8')
        run.append(f'$ {action["command"]}\r\n')
        # util-linux setsid --ctty makes the pty the controlling terminal, so Ctrl-C reaches the
        # command and scripts that open /dev/tty for prompts work. Fall back to a plain new session.
        if shutil.which('setsid'):
            arguments = ['setsid', '--ctty', '/bin/sh', '-c', action['command']]
            new_session = False
        else:
            arguments = ['/bin/sh', '-c', action['command']]
            new_session = True
        try:
            process = subprocess.Popen(arguments, stdin=slave, stdout=slave, stderr=slave,
                                       start_new_session=new_session, env=environment, cwd='/')
        except OSError as error:
            os.close(master)
            os.close(slave)
            run.append(f'failed to start: {error}\r\n')
            run.finish(127)
            return
        os.close(slave)
        with run.condition:
            run.pid = process.pid
            run.process = process
            run.master = master
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        exited_at = None
        try:
            while True:
                readable, _, _ = select.select([master], [], [], 0.5)
                if readable:
                    try:
                        data = os.read(master, 8192)
                    except OSError:
                        break
                    if not data:
                        break
                    text = decoder.decode(data)
                    if text:
                        run.append(text)
                    continue
                if process.poll() is not None:
                    if exited_at is None:
                        exited_at = time.monotonic()
                    elif time.monotonic() - exited_at > 2.0:
                        break
        finally:
            with run.condition:
                run.master = None
            os.close(master)
        tail = decoder.decode(b'', final=True)
        if tail:
            run.append(tail)
        exit_code = process.wait()
        if exit_code < 0:
            run.append(f'\r\n[terminated by signal {-exit_code}]\r\n')
        else:
            run.append(f'\r\n[exit {exit_code}]\r\n')
        run.finish(exit_code)
