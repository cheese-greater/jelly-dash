"""Small helpers shared by the collectors."""

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request


def run(args, timeout=5.0, use_sudo=False):
    """Run a command and return (returncode, stdout, stderr).

    returncode is None when the command could not be run at all (missing binary,
    timeout or OS error); stderr then carries a short reason.
    """
    command = [str(argument) for argument in args]
    if shutil.which(command[0]) is None:
        return None, '', f'{command[0]} not installed'
    if use_sudo and os.geteuid() != 0:
        if shutil.which('sudo') is None:
            return None, '', 'sudo not installed'
        command = ['sudo', '-n'] + command
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, '', f'{command[0]} timed out after {timeout:g}s'
    except OSError as error:
        return None, '', str(error)
    return completed.returncode, completed.stdout, completed.stderr


def read_text(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            return handle.read().strip().rstrip('\x00')
    except OSError:
        return default


def read_int(path, default=None):
    text = read_text(path)
    if text is None:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def read_dt_u32(path):
    """Read a 32-bit big-endian device-tree property."""
    try:
        with open(path, 'rb') as handle:
            data = handle.read(4)
    except OSError:
        return None
    if len(data) != 4:
        return None
    return int.from_bytes(data, 'big')


def http_probe(url, timeout=3.0, want_json=False):
    """GET a URL and describe the result without raising."""
    started = time.monotonic()
    result = {'ok': False, 'status': None, 'latency_ms': None, 'body': None, 'json': None, 'error': None}
    request = urllib.request.Request(url, headers={'User-Agent': 'jellydash'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(65536)
            result['status'] = response.status
    except urllib.error.HTTPError as error:
        result['status'] = error.code
        result['error'] = f'HTTP {error.code}'
        raw = b''
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, 'reason', None) or error
        result['error'] = str(reason)
        return result
    result['latency_ms'] = round((time.monotonic() - started) * 1000, 1)
    result['ok'] = result['status'] is not None and 200 <= result['status'] < 400
    text = raw.decode('utf-8', errors='replace')
    result['body'] = text[:200].strip()
    if want_json:
        try:
            result['json'] = json.loads(text)
        except ValueError:
            result['json'] = None
    return result


def dotted_get(data, dotted_key):
    """Walk a nested dict with a dotted key such as 'a.b.c'."""
    current = data
    for part in dotted_key.split('.'):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


class RateTracker:
    """Turns monotonically increasing counters into per-second rates."""

    def __init__(self):
        self._previous = {}

    def rate(self, key, value, now=None):
        if value is None:
            return None
        now = time.monotonic() if now is None else now
        previous = self._previous.get(key)
        self._previous[key] = (now, value)
        if previous is None:
            return None
        elapsed = now - previous[0]
        if elapsed <= 0:
            return None
        delta = value - previous[1]
        if delta < 0:
            return None
        return delta / elapsed

    def forget_missing(self, live_keys):
        for key in list(self._previous):
            if key not in live_keys:
                del self._previous[key]
