"""Background sampling threads and the shared snapshot the server hands out."""

import copy
import threading
import time
import traceback
from collections import deque

from . import __version__
from .collectors.disks import DiskCollector
from .collectors.network import NetworkCollector
from .collectors.power import PowerCollector
from .collectors.processes import ProcessCollector
from .collectors.services import ServiceCollector
from .collectors.system import SystemCollector
from .collectors.tailscale import collect_tailscale


class Sampler:
    def __init__(self, config):
        server_config = config['server']
        self.interval = float(server_config.get('sample_interval', 2.0))
        self.slow_interval = float(server_config.get('slow_interval', 10.0))
        history_points = int(server_config.get('history_points', 180))
        self.system = SystemCollector()
        self.power = PowerCollector(config['ups'])
        self.network = NetworkCollector(config['network'])
        self.disks = DiskCollector(config['disks'])
        self.processes = ProcessCollector(int(server_config.get('top_processes', 15)),
                                          bool(config['processes'].get('show_kernel_threads')))
        self.services = ServiceCollector(config['services'])
        self._lock = threading.Lock()
        self._data = {'host': {}, 'cpu': {}, 'mem': {}, 'power': {}, 'network': {}, 'disks': {},
                      'processes': {}, 'tailscale': {}, 'services': [], 'errors': {}, 'sampled_at': None}
        self._history = {
            'timestamps': deque(maxlen=history_points),
            'cpu': deque(maxlen=history_points),
            'cores': [],
            'temp': deque(maxlen=history_points),
            'mem': deque(maxlen=history_points),
            'net_rx': deque(maxlen=history_points),
            'net_tx': deque(maxlen=history_points),
            'disk_read': deque(maxlen=history_points),
            'disk_write': deque(maxlen=history_points),
            'battery': deque(maxlen=history_points),
            'power_w': deque(maxlen=history_points)
        }
        self._history_points = history_points
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._fast_loop, name='jellydash-fast', daemon=True).start()
        threading.Thread(target=self._slow_loop, name='jellydash-slow', daemon=True).start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            data = copy.deepcopy(self._data)
            history = {key: list(value) for key, value in self._history.items() if key != 'cores'}
            history['cores'] = [list(core) for core in self._history['cores']]
        data['history'] = history
        data['meta'] = {'now': time.time(), 'interval': self.interval, 'version': __version__,
                        'history_points': self._history_points}
        return data

    def tailscale_status(self):
        with self._lock:
            return self._data.get('tailscale') or {}

    def _run(self, name, function, *arguments):
        try:
            return function(*arguments), None
        except Exception as error:  # noqa: BLE001 - a broken collector must never kill the loop
            return None, f'{type(error).__name__}: {error}\n{traceback.format_exc(limit=2)}'

    def _fast_loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            results = {}
            errors = {}
            for name, function in (('host', self.system.host), ('cpu', self.system.cpu),
                                   ('mem', self.system.memory), ('power', self.power.collect),
                                   ('network', self.network.collect), ('disks', self.disks.collect),
                                   ('processes', self.processes.collect)):
                value, error = self._run(name, function)
                if error:
                    errors[name] = error
                else:
                    results[name] = value
            with self._lock:
                self._data.update(results)
                for name in list(self._data['errors']):
                    if name in results:
                        del self._data['errors'][name]
                self._data['errors'].update(errors)
                self._data['sampled_at'] = time.time()
                self._append_history(results)
            elapsed = time.monotonic() - started
            self._stop.wait(max(self.interval - elapsed, 0.2))

    def _append_history(self, results):
        history = self._history
        history['timestamps'].append(round(time.time(), 1))
        cpu = results.get('cpu') or {}
        cores = cpu.get('cores') or []
        while len(history['cores']) < len(cores):
            history['cores'].append(deque(maxlen=self._history_points))
        for index, value in enumerate(cores):
            history['cores'][index].append(value)
        history['cpu'].append(cpu.get('total'))
        history['temp'].append(cpu.get('temp_c'))
        history['mem'].append((results.get('mem') or {}).get('percent'))
        network = results.get('network') or {}
        primary = next((interface for interface in network.get('interfaces', [])
                        if interface['name'] == network.get('primary')), None)
        history['net_rx'].append(primary['rx_rate'] if primary else None)
        history['net_tx'].append(primary['tx_rate'] if primary else None)
        devices = (results.get('disks') or {}).get('devices') or {}
        reads = [device['read_rate'] for device in devices.values() if device.get('read_rate') is not None]
        writes = [device['write_rate'] for device in devices.values() if device.get('write_rate') is not None]
        history['disk_read'].append(sum(reads) if reads else None)
        history['disk_write'].append(sum(writes) if writes else None)
        power = results.get('power') or {}
        history['battery'].append((power.get('ups') or {}).get('percent'))
        history['power_w'].append((power.get('psu') or {}).get('total_power_w'))

    def _slow_loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            results = {}
            errors = {}
            for name, function in (('tailscale', collect_tailscale), ('services', self.services.collect)):
                value, error = self._run(name, function)
                if error:
                    errors[name] = error
                else:
                    results[name] = value
            with self._lock:
                device_names = list(((self._data.get('disks') or {}).get('devices') or {}).keys())
            smart, smart_error = self._run('smart', self.disks.smart, device_names)
            if smart_error:
                errors['smart'] = smart_error
            with self._lock:
                self._data.update(results)
                for name in list(self._data['errors']):
                    if name in results or (name == 'smart' and smart is not None):
                        del self._data['errors'][name]
                self._data['errors'].update(errors)
            elapsed = time.monotonic() - started
            self._stop.wait(max(self.slow_interval - elapsed, 1.0))
