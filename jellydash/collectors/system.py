"""CPU, memory and host identity."""

import glob
import os
import platform
import socket
import time

from ..util import read_int, read_text
from .hwmon import scan_hwmon

PREFERRED_THERMAL_TYPES = ('cpu-thermal', 'cpu_thermal', 'x86_pkg_temp', 'coretemp', 'k10temp', 'soc_thermal')


class SystemCollector:
    def __init__(self):
        self._previous_cpu = {}
        self._host_cache = None

    def host(self):
        if self._host_cache is None:
            os_release = {}
            for line in (read_text('/etc/os-release', '') or '').splitlines():
                if '=' in line:
                    key, _, value = line.partition('=')
                    os_release[key.strip()] = value.strip().strip('"')
            model = read_text('/proc/device-tree/model') or read_text('/sys/firmware/devicetree/base/model')
            if not model:
                model = read_text('/sys/devices/virtual/dmi/id/product_name') or platform.machine()
            self._host_cache = {
                'hostname': socket.gethostname(),
                'model': model,
                'os': os_release.get('PRETTY_NAME') or platform.platform(),
                'kernel': os.uname().release,
                'arch': platform.machine(),
                'cpu_count': os.cpu_count() or 1
            }
        uptime_text = read_text('/proc/uptime', '0 0') or '0 0'
        uptime_seconds = float(uptime_text.split()[0])
        return dict(self._host_cache, uptime_s=uptime_seconds, boot_time=time.time() - uptime_seconds,
                    time=time.time())

    def cpu(self):
        lines = (read_text('/proc/stat', '') or '').splitlines()
        current = {}
        for line in lines:
            if not line.startswith('cpu'):
                continue
            parts = line.split()
            name = parts[0]
            values = [int(value) for value in parts[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
            current[name] = (total - idle, total)
        usage = {}
        for name, (busy, total) in current.items():
            previous = self._previous_cpu.get(name)
            if previous is None:
                usage[name] = 0.0
                continue
            delta_total = total - previous[1]
            delta_busy = busy - previous[0]
            usage[name] = round(100.0 * delta_busy / delta_total, 1) if delta_total > 0 else 0.0
        self._previous_cpu = current
        core_names = sorted((name for name in current if name != 'cpu'), key=lambda name: int(name[3:]))
        load = os.getloadavg() if hasattr(os, 'getloadavg') else (0.0, 0.0, 0.0)
        return {
            'total': usage.get('cpu', 0.0),
            'cores': [usage[name] for name in core_names],
            'count': len(core_names),
            'load': [round(value, 2) for value in load],
            'freq_mhz': self._cpu_frequency('scaling_cur_freq'),
            'freq_max_mhz': self._cpu_frequency('cpuinfo_max_freq'),
            'temp_c': self.cpu_temperature(),
            'fan_rpm': self.fan_speed()
        }

    @staticmethod
    def fan_speed():
        for chip in scan_hwmon():
            for reading in chip['readings']:
                if reading['kind'] == 'fan':
                    return reading['value']
        return None

    @staticmethod
    def _cpu_frequency(attribute):
        values = []
        for path in glob.glob(f'/sys/devices/system/cpu/cpu[0-9]*/cpufreq/{attribute}'):
            value = read_int(path)
            if value is not None:
                values.append(value)
        if not values:
            return None
        return round(sum(values) / len(values) / 1000.0)

    @staticmethod
    def cpu_temperature():
        candidates = []
        for zone in glob.glob('/sys/class/thermal/thermal_zone*'):
            zone_type = read_text(os.path.join(zone, 'type'), '')
            temperature = read_int(os.path.join(zone, 'temp'))
            if temperature is None:
                continue
            priority = PREFERRED_THERMAL_TYPES.index(zone_type) if zone_type in PREFERRED_THERMAL_TYPES else 99
            candidates.append((priority, zone, temperature / 1000.0))
        if candidates:
            candidates.sort()
            return round(candidates[0][2], 1)
        for chip in scan_hwmon():
            if chip['name'] in ('cpu_thermal', 'k10temp', 'coretemp', 'soc_thermal'):
                temps = [reading['value'] for reading in chip['readings'] if reading['kind'] == 'temp']
                if temps:
                    return round(temps[0], 1)
        return None

    @staticmethod
    def memory():
        info = {}
        for line in (read_text('/proc/meminfo', '') or '').splitlines():
            key, _, rest = line.partition(':')
            parts = rest.split()
            if parts:
                info[key] = int(parts[0]) * 1024
        total = info.get('MemTotal', 0)
        available = info.get('MemAvailable', info.get('MemFree', 0))
        used = max(total - available, 0)
        swap_total = info.get('SwapTotal', 0)
        swap_used = max(swap_total - info.get('SwapFree', 0), 0)
        return {
            'total': total,
            'used': used,
            'available': available,
            'free': info.get('MemFree', 0),
            'buffers': info.get('Buffers', 0),
            'cached': info.get('Cached', 0) + info.get('SReclaimable', 0),
            'percent': round(100.0 * used / total, 1) if total else 0.0,
            'swap_total': swap_total,
            'swap_used': swap_used,
            'swap_percent': round(100.0 * swap_used / swap_total, 1) if swap_total else 0.0
        }
