"""Mounted filesystems, block devices, IO rates, temperatures and SMART."""

import json
import os
import time

from ..util import RateTracker, read_int, read_text, run
from .hwmon import scan_hwmon

SECTOR_BYTES = 512


class DiskCollector:
    def __init__(self, disks_config):
        self.exclude_fstypes = set(disks_config.get('exclude_fstypes') or [])
        self.exclude_mounts = set(disks_config.get('exclude_mounts') or [])
        self.smart_enabled = bool(disks_config.get('smart', True))
        self.smart_interval = float(disks_config.get('smart_interval', 300))
        self._rates = RateTracker()
        self._smart_cache = {}
        self._smart_checked = {}

    def collect(self):
        mounts = self._mounts()
        diskstats = self._diskstats()
        temperatures = self._temperatures()
        devices = {}
        for mount in mounts:
            base = self._base_device(mount['device'])
            mount['base_device'] = base
            if base and base not in devices:
                devices[base] = self._device_info(base, diskstats, temperatures)
        live_keys = set()
        for name in devices:
            live_keys.update({f'{name}:read', f'{name}:write', f'{name}:busy'})
        self._rates.forget_missing(live_keys)
        return {'mounts': mounts, 'devices': devices}

    def smart(self, device_names):
        """Refresh SMART data for the given base devices (slow, call rarely)."""
        results = {}
        for name in device_names:
            now = time.monotonic()
            last = self._smart_checked.get(name, 0)
            if name in self._smart_cache and now - last < self.smart_interval:
                results[name] = self._smart_cache[name]
                continue
            self._smart_checked[name] = now
            self._smart_cache[name] = self._smartctl(name)
            results[name] = self._smart_cache[name]
        return results

    def _mounts(self):
        mounts = []
        seen_devices = set()
        for line in (read_text('/proc/mounts', '') or '').splitlines():
            fields = line.split()
            if len(fields) < 3:
                continue
            device, mountpoint, fstype = fields[0], fields[1].replace('\\040', ' '), fields[2]
            if not device.startswith('/dev/') or fstype in self.exclude_fstypes or mountpoint in self.exclude_mounts:
                continue
            if device.startswith('/dev/loop') or mountpoint.startswith('/snap/'):
                continue
            real_device = os.path.realpath(device)
            if real_device in seen_devices:
                continue
            seen_devices.add(real_device)
            try:
                stats = os.statvfs(mountpoint)
            except OSError:
                continue
            total = stats.f_blocks * stats.f_frsize
            free = stats.f_bavail * stats.f_frsize
            used = (stats.f_blocks - stats.f_bfree) * stats.f_frsize
            if total == 0:
                continue
            mounts.append({
                'device': device,
                'mountpoint': mountpoint,
                'fstype': fstype,
                'total': total,
                'used': used,
                'free': free,
                'percent': round(100.0 * used / (used + free), 1) if used + free else 0.0
            })
        mounts.sort(key=lambda mount: mount['mountpoint'])
        return mounts

    @staticmethod
    def _base_device(device):
        name = os.path.basename(os.path.realpath(device))
        sys_path = f'/sys/class/block/{name}'
        if not os.path.exists(sys_path):
            return None
        if os.path.isdir(f'{sys_path}/slaves'):
            slaves = os.listdir(f'{sys_path}/slaves')
            if slaves:
                return DiskCollector._base_device(f'/dev/{slaves[0]}')
        if os.path.exists(f'{sys_path}/partition'):
            return os.path.basename(os.path.dirname(os.path.realpath(sys_path)))
        return name

    @staticmethod
    def _diskstats():
        stats = {}
        for line in (read_text('/proc/diskstats', '') or '').splitlines():
            fields = line.split()
            if len(fields) < 14:
                continue
            stats[fields[2]] = {
                'read_bytes': int(fields[5]) * SECTOR_BYTES,
                'write_bytes': int(fields[9]) * SECTOR_BYTES,
                'busy_ms': int(fields[12])
            }
        return stats

    @staticmethod
    def _temperatures():
        temperatures = {}
        for chip in scan_hwmon():
            if chip['name'] not in ('nvme', 'drivetemp') or not chip['device_path']:
                continue
            temps = [reading['value'] for reading in chip['readings'] if reading['kind'] == 'temp']
            if temps:
                temperatures[chip['device_path']] = temps[0]
        return temperatures

    def _device_info(self, name, diskstats, temperatures):
        sys_path = f'/sys/class/block/{name}'
        device_path = os.path.realpath(f'{sys_path}/device') if os.path.exists(f'{sys_path}/device') else ''
        model = read_text(f'{sys_path}/device/model') or read_text(f'{sys_path}/device/name') or ''
        if name.startswith('nvme'):
            transport = 'nvme'
        elif name.startswith('mmcblk'):
            transport = 'sd card' if 'mmc' in device_path else 'emmc'
        elif '/usb' in device_path:
            transport = 'usb'
        else:
            transport = 'sata'
        rotational = read_int(f'{sys_path}/queue/rotational')
        size = (read_int(f'{sys_path}/size') or 0) * SECTOR_BYTES
        counters = diskstats.get(name, {})
        busy_rate = self._rates.rate(f'{name}:busy', counters.get('busy_ms'))
        temperature = None
        for hwmon_device, value in temperatures.items():
            if device_path and (device_path.startswith(hwmon_device) or hwmon_device.startswith(device_path)):
                temperature = value
                break
        return {
            'name': name,
            'model': model.strip(),
            'transport': transport,
            'rotational': bool(rotational),
            'size': size,
            'read_rate': self._rates.rate(f'{name}:read', counters.get('read_bytes')),
            'write_rate': self._rates.rate(f'{name}:write', counters.get('write_bytes')),
            'utilization': round(min(busy_rate / 10.0, 100.0), 1) if busy_rate is not None else None,
            'temp_c': temperature,
            'smart': self._smart_cache.get(name)
        }

    def _smartctl(self, name):
        if not self.smart_enabled:
            return {'available': False, 'detail': 'disabled in config'}
        code, output, error = run(['smartctl', '--json=c', '-n', 'standby', '-H', '-A', '-i', f'/dev/{name}'],
                                  timeout=20, use_sudo=True)
        if code is None:
            return {'available': False, 'detail': error or 'smartctl unavailable (apt install smartmontools)'}
        try:
            data = json.loads(output) if output.strip() else {}
        except ValueError:
            data = {}
        if code & 2 and not data.get('smart_status'):
            messages = '; '.join(message.get('string', '') for message in (data.get('smartctl') or {}).get('messages', []))
            if 'standby' in output.lower() or 'STANDBY' in messages:
                return {'available': False, 'detail': 'drive in standby, not waking it'}
            return {'available': False, 'detail': messages or (error.strip() or 'smartctl could not open device')[:160]}
        if not data:
            return {'available': False, 'detail': (error or output).strip()[:160] or 'no SMART data'}
        if not data.get('smart_status') and not data.get('nvme_smart_health_information_log') and not data.get('ata_smart_attributes'):
            return {'available': False, 'detail': 'no SMART support on this device'}
        result = {
            'available': True,
            'passed': (data.get('smart_status') or {}).get('passed'),
            'temp_c': (data.get('temperature') or {}).get('current'),
            'power_on_hours': (data.get('power_on_time') or {}).get('hours'),
            'model': data.get('model_name'),
            'serial': data.get('serial_number'),
            'warnings': [],
            'checked_at': time.time()
        }
        nvme_log = data.get('nvme_smart_health_information_log') or {}
        if nvme_log:
            result['percentage_used'] = nvme_log.get('percentage_used')
            result['available_spare'] = nvme_log.get('available_spare')
            if nvme_log.get('media_errors'):
                result['warnings'].append(f"media errors: {nvme_log['media_errors']}")
            if nvme_log.get('critical_warning'):
                result['warnings'].append(f"critical warning: {nvme_log['critical_warning']}")
        for attribute in (data.get('ata_smart_attributes') or {}).get('table', []):
            raw_value = (attribute.get('raw') or {}).get('value', 0)
            if attribute.get('id') in (5, 187, 197, 198) and raw_value:
                result['warnings'].append(f"{attribute.get('name')}: {raw_value}")
        if result['passed'] is False:
            result['warnings'].insert(0, 'SMART overall health FAILED')
        return result
