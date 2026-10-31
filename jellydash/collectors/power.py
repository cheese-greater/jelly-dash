"""Power: PSU input, firmware throttle flags, and UPS or battery HATs.

Everything here is best-effort. On Ubuntu Server vcgencmd is usually absent, so
the firmware mailbox (/dev/vcio) and hwmon are read directly instead.
"""

import fcntl
import json
import os
import re
import struct
import subprocess
import time
from collections import deque

from ..util import read_dt_u32, run
from .hwmon import scan_hwmon
from .nut import nut_status

THROTTLE_BITS = [
    (0, 'under_voltage', 'now'),
    (1, 'freq_capped', 'now'),
    (2, 'throttled', 'now'),
    (3, 'soft_temp_limit', 'now'),
    (16, 'under_voltage', 'past'),
    (17, 'freq_capped', 'past'),
    (18, 'throttled', 'past'),
    (19, 'soft_temp_limit', 'past')
]

TAG_GET_THROTTLED = 0x00030046
# _IOWR(100, 0, char *): direction 3, size of a pointer, type 100, number 0
MAILBOX_IOCTL = (3 << 30) | (struct.calcsize('P') << 16) | (100 << 8)
I2C_SLAVE = 0x0703

MAX1704X_ADDRESS = 0x36
INA219_ADDRESSES = (0x40, 0x41, 0x42, 0x43, 0x44, 0x45)
PROBE_RETRY_SECONDS = 300


def decode_throttled(raw):
    flags = {}
    for bit, name, when in THROTTLE_BITS:
        flags.setdefault(name, {})[when] = bool(raw & (1 << bit))
    return flags


def mailbox_get_throttled():
    buffer = bytearray(struct.pack('<8I', 32, 0, TAG_GET_THROTTLED, 8, 0, 0, 0, 0))
    descriptor = os.open('/dev/vcio', os.O_RDWR)
    try:
        fcntl.ioctl(descriptor, MAILBOX_IOCTL, buffer, True)
    finally:
        os.close(descriptor)
    words = struct.unpack('<8I', bytes(buffer))
    if words[1] != 0x80000000:
        raise OSError('firmware rejected the request')
    return words[5]


def i2c_read_word(bus, address, register):
    descriptor = os.open(f'/dev/i2c-{bus}', os.O_RDWR)
    try:
        fcntl.ioctl(descriptor, I2C_SLAVE, address)
        os.write(descriptor, bytes([register]))
        data = os.read(descriptor, 2)
    finally:
        os.close(descriptor)
    if len(data) != 2:
        raise OSError('short I2C read')
    return int.from_bytes(data, 'big')


def read_max1704x(bus, address):
    """MAX17040/17043/17048 fuel gauge (used by Geekworm X120x, PiJuice-like HATs)."""
    vcell = i2c_read_word(bus, address, 0x02)
    soc = i2c_read_word(bus, address, 0x04)
    version = i2c_read_word(bus, address, 0x08)
    reading = {
        'voltage': round((vcell >> 4) * 1.25 / 1000.0, 3),
        'percent': round(min(soc / 256.0, 100.0), 1),
        'chip': 'MAX1704x',
        'rate_pct_per_hour': None
    }
    if (version & 0xFFF0) == 0x0010:
        reading['chip'] = 'MAX17048'
        crate = i2c_read_word(bus, address, 0x16)
        if crate & 0x8000:
            crate -= 0x10000
        reading['rate_pct_per_hour'] = round(crate * 0.208, 2)
    return reading


def read_ina219(bus, address, shunt_ohms, cells, invert_current):
    """INA219 power monitor (Waveshare UPS HATs and similar)."""
    configuration = i2c_read_word(bus, address, 0x00)
    if configuration in (0, 0xFFFF):
        raise OSError('INA219 configuration register looks empty')
    bus_raw = i2c_read_word(bus, address, 0x02)
    shunt_raw = i2c_read_word(bus, address, 0x01)
    if shunt_raw & 0x8000:
        shunt_raw -= 0x10000
    voltage = (bus_raw >> 3) * 0.004
    current = shunt_raw * 0.00001 / shunt_ohms
    if invert_current:
        current = -current
    cell_count = max(int(cells), 1)
    percent = (voltage - 3.0 * cell_count) / (1.2 * cell_count) * 100.0
    return {
        'voltage': round(voltage, 3),
        'current': round(current, 3),
        'power': round(voltage * abs(current), 2),
        'percent': round(max(0.0, min(percent, 100.0)), 1),
        'chip': 'INA219'
    }


def read_sysfs_power_supplies():
    root = '/sys/class/power_supply'
    supplies = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return supplies
    for name in names:
        base = os.path.join(root, name)

        def attribute(key):
            try:
                with open(os.path.join(base, key), 'r', encoding='utf-8') as handle:
                    return handle.read().strip()
            except OSError:
                return None

        def number(key, scale=1.0):
            text = attribute(key)
            try:
                return float(text) / scale if text is not None else None
            except ValueError:
                return None

        supplies.append({
            'name': name,
            'type': attribute('type'),
            'status': attribute('status'),
            'present': attribute('present') not in ('0', None) if attribute('present') is not None else True,
            'online': attribute('online') == '1' if attribute('online') is not None else None,
            'capacity': number('capacity'),
            'voltage': number('voltage_now', 1e6),
            'current': number('current_now', 1e6),
            'power': number('power_now', 1e6),
            'time_to_empty_s': number('time_to_empty_now'),
            'time_to_full_s': number('time_to_full_now'),
            'model': attribute('model_name')
        })
    return supplies


def read_gpio_level(line):
    """Read a header GPIO through libgpiod's CLI (v1 or v2 syntax)."""
    chips = []
    code, output, _ = run(['gpiodetect'], timeout=3)
    if code == 0:
        for text in output.splitlines():
            match = re.match(r'(gpiochip\d+)\s+\[([^\]]*)\]', text)
            if match and any(token in match.group(2) for token in ('rp1', 'pinctrl-bcm', 'bcm2835', 'bcm2711')):
                chips.append(match.group(1))
    if not chips:
        chips = ['gpiochip0', 'gpiochip4']
    for chip in chips:
        for arguments in (['gpioget', '-c', chip, str(line)], ['gpioget', chip, str(line)]):
            code, output, _ = run(arguments, timeout=3)
            if code != 0:
                continue
            text = output.strip().lower()
            if 'inactive' in text:
                return 0
            if 'active' in text:
                return 1
            tail = text.split()[-1] if text.split() else ''
            if tail in ('0', '1'):
                return int(tail)
    return None


class PowerCollector:
    def __init__(self, ups_config):
        self.config = ups_config
        self._detected = None
        self._last_probe = 0.0
        self._probe_error = None
        self._voltage_history = deque(maxlen=30)
        self._nut_seen = False

    def collect(self):
        throttled = self.throttled_state()
        psu = self.psu_info()
        ups = self.ups()
        if ups.get('on_battery') is True:
            source = 'battery'
        elif ups.get('on_battery') is False:
            source = 'external'
        elif not ups.get('available'):
            source = 'external'
        else:
            source = 'unknown'
        return {'source': source, 'throttled': throttled, 'psu': psu, 'ups': ups, 'hwmon': scan_hwmon()}

    def throttled_state(self):
        code, output, _ = run(['vcgencmd', 'get_throttled'], timeout=3)
        if code == 0:
            match = re.search(r'0x([0-9a-fA-F]+)', output)
            if match:
                raw = int(match.group(1), 16)
                return {'raw': raw, 'source': 'vcgencmd', 'flags': decode_throttled(raw)}
        errors = []
        try:
            raw = mailbox_get_throttled()
            return {'raw': raw, 'source': 'firmware mailbox', 'flags': decode_throttled(raw)}
        except OSError as error:
            errors.append(f'/dev/vcio: {error}')
        for chip in scan_hwmon():
            if chip['name'] != 'rpi_volt':
                continue
            alarm = any(reading['kind'] == 'alarm' and reading['value'] for reading in chip['readings'])
            return {'raw': None, 'source': 'hwmon rpi_volt',
                    'flags': {'under_voltage': {'now': alarm, 'past': None}}}
        return {'raw': None, 'source': None, 'flags': {}, 'error': '; '.join(errors)}

    @staticmethod
    def psu_info():
        info = {'max_current_ma': None, 'usb_max_current_enable': None, 'input_voltage': None,
                'rails': {}, 'total_power_w': None, 'properties': {}}
        power_dir = '/proc/device-tree/chosen/power'
        if os.path.isdir(power_dir):
            for name in sorted(os.listdir(power_dir)):
                value = read_dt_u32(os.path.join(power_dir, name))
                if value is not None:
                    info['properties'][name] = value
            info['max_current_ma'] = info['properties'].get('max_current')
            info['usb_max_current_enable'] = info['properties'].get('usb_max_current_enable')
        code, output, _ = run(['vcgencmd', 'pmic_read_adc'], timeout=3)
        if code == 0:
            rails = {}
            for line in output.splitlines():
                match = re.match(r'\s*(\S+)\s+(?:current|volt)\(\d+\)=([0-9.]+)[AV]', line)
                if match:
                    rails[match.group(1)] = float(match.group(2))
            info['rails'] = rails
            info['input_voltage'] = rails.get('EXT5V_V')
            total = 0.0
            found = False
            for name, amps in rails.items():
                if name.endswith('_A') and name[:-2] + '_V' in rails:
                    total += amps * rails[name[:-2] + '_V']
                    found = True
            if found:
                info['total_power_w'] = round(total, 2)
        return info

    def ups(self):
        source = str(self.config.get('source', 'auto')).lower()
        if source == 'none':
            return {'available': False, 'source': None, 'detail': 'disabled in config'}
        if source == 'command':
            return self._ups_command()
        if source in ('auto', 'nut'):
            nut = self._ups_nut()
            if nut['available'] or source == 'nut' or self._nut_seen:
                return nut
        if source in ('auto', 'sysfs'):
            supplies = read_sysfs_power_supplies()
            if supplies and any(supply['type'] == 'Battery' and supply['present'] for supply in supplies):
                return self._ups_from_sysfs(supplies)
            if source == 'sysfs':
                return {'available': False, 'source': 'sysfs', 'detail': 'no battery under /sys/class/power_supply'}
        return self._ups_i2c(source)

    def _ups_nut(self):
        host = self.config.get('nut_host') or 'localhost'
        port = int(self.config.get('nut_port') or 3493)
        try:
            result = nut_status(host, port, self.config.get('nut_ups') or '',
                                username=self.config.get('nut_username') or '',
                                password=self.config.get('nut_password') or '')
        except (OSError, ValueError) as error:
            reason = getattr(error, 'strerror', None) or str(error)
            return {'available': False, 'source': 'nut', 'detail': f'upsd {host}:{port}: {reason}'}
        self._nut_seen = True
        return result

    def _ups_command(self):
        command = self.config.get('command') or ''
        if not command:
            return {'available': False, 'source': 'command', 'detail': 'ups.command is empty'}
        try:
            completed = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=5, check=False)
            data = json.loads(completed.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            return {'available': False, 'source': 'command', 'detail': f'command failed: {error}'}
        result = {'available': True, 'source': 'command', 'chip': data.get('chip', 'custom'),
                  'on_battery': data.get('on_battery'), 'percent': data.get('percent'),
                  'voltage': data.get('voltage'), 'current': data.get('current'), 'power': data.get('power'),
                  'status': data.get('status'), 'detail': data.get('detail', '')}
        if result['status'] is None and result['on_battery'] is not None:
            result['status'] = 'discharging' if result['on_battery'] else 'charging'
        return result

    @staticmethod
    def _ups_from_sysfs(supplies):
        battery = next(supply for supply in supplies if supply['type'] == 'Battery' and supply['present'])
        mains = [supply for supply in supplies if supply['type'] not in ('Battery', None)]
        status_text = (battery['status'] or 'unknown').lower()
        on_battery = None
        if status_text == 'discharging':
            on_battery = True
        elif status_text in ('charging', 'full', 'not charging'):
            on_battery = False
        elif mains:
            on_battery = not any(supply['online'] for supply in mains)
        return {
            'available': True,
            'source': 'sysfs',
            'chip': battery['model'] or battery['name'],
            'on_battery': on_battery,
            'percent': battery['capacity'],
            'voltage': battery['voltage'],
            'current': battery['current'],
            'power': battery['power'],
            'status': status_text,
            'time_to_empty_s': battery['time_to_empty_s'],
            'time_to_full_s': battery['time_to_full_s'],
            'detail': battery['name']
        }

    def _ups_i2c(self, source):
        bus = int(self.config.get('i2c_bus', 1))
        if not os.path.exists(f'/dev/i2c-{bus}'):
            return {'available': False, 'source': None,
                    'detail': f'/dev/i2c-{bus} missing (enable I2C in /boot/firmware/config.txt)'}
        now = time.monotonic()
        if self._detected is None and now - self._last_probe > PROBE_RETRY_SECONDS:
            self._last_probe = now
            self._detected = self._probe(bus, source)
        if self._detected is None:
            return {'available': False, 'source': None,
                    'detail': self._probe_error or 'no supported fuel gauge found on I2C'}
        kind, address = self._detected
        try:
            if kind == 'max17040':
                reading = read_max1704x(bus, address)
            else:
                reading = read_ina219(bus, address, float(self.config.get('shunt_ohms', 0.1)),
                                      self.config.get('cells', 1), bool(self.config.get('invert_current')))
        except OSError as error:
            return {'available': False, 'source': kind, 'detail': f'I2C read failed: {error}'}
        result = {'available': True, 'source': kind, 'address': f'0x{address:02x}', 'on_battery': None,
                  'status': 'unknown', 'detail': ''}
        result.update(reading)
        self._voltage_history.append(reading['voltage'])
        self._apply_power_state(result)
        return result

    def _probe(self, bus, source):
        configured_address = int(self.config.get('i2c_address', 0) or 0)
        if source == 'max17040':
            candidates = [('max17040', configured_address or MAX1704X_ADDRESS)]
        elif source == 'ina219':
            candidates = [('ina219', address) for address in ([configured_address] if configured_address else INA219_ADDRESSES)]
        else:
            candidates = [('max17040', MAX1704X_ADDRESS)] + [('ina219', address) for address in INA219_ADDRESSES]
        errors = []
        for kind, address in candidates:
            try:
                if kind == 'max17040':
                    reading = read_max1704x(bus, address)
                    plausible = 2.0 <= reading['voltage'] <= 5.0
                else:
                    reading = read_ina219(bus, address, float(self.config.get('shunt_ohms', 0.1)),
                                          self.config.get('cells', 1), False)
                    plausible = 2.0 <= reading['voltage'] <= 30.0
                if plausible:
                    return (kind, address)
                errors.append(f'{kind}@0x{address:02x}: implausible {reading["voltage"]}V')
            except OSError as error:
                errors.append(f'{kind}@0x{address:02x}: {error.strerror or error}')
        self._probe_error = 'no UPS found: ' + '; '.join(errors[-3:])
        return None

    def _apply_power_state(self, result):
        """Decide charging/discharging from the best evidence available."""
        gpio_line = int(self.config.get('power_loss_gpio', -1))
        if gpio_line >= 0:
            level = read_gpio_level(gpio_line)
            if level is not None:
                lost = bool(level) if self.config.get('power_loss_active_high', True) else not level
                result['on_battery'] = lost
                result['status'] = 'discharging' if lost else ('full' if (result.get('percent') or 0) >= 98 else 'charging')
                result['detail'] = f'power-loss GPIO{gpio_line}'
                return
            result['detail'] = f'GPIO{gpio_line} unreadable (install gpiod)'
        current = result.get('current')
        if current is not None:
            if current < -0.02:
                result['on_battery'] = True
                result['status'] = 'discharging'
            elif current > 0.02:
                result['on_battery'] = False
                result['status'] = 'charging'
            else:
                result['on_battery'] = False
                result['status'] = 'full' if (result.get('percent') or 0) >= 95 else 'idle'
            return
        rate = result.get('rate_pct_per_hour')
        if rate is not None:
            if rate < -0.3:
                result['on_battery'] = True
                result['status'] = 'discharging'
            elif rate > 0.3:
                result['on_battery'] = False
                result['status'] = 'charging'
            else:
                result['on_battery'] = False
                result['status'] = 'full' if (result.get('percent') or 0) >= 97 else 'idle'
            return
        if len(self._voltage_history) >= 10:
            slope = self._voltage_history[-1] - self._voltage_history[0]
            if slope < -0.01:
                result['on_battery'] = True
                result['status'] = 'discharging (voltage trend)'
            elif slope > 0.01:
                result['on_battery'] = False
                result['status'] = 'charging (voltage trend)'
            elif result.get('voltage', 0) >= 4.12:
                result['on_battery'] = False
                result['status'] = 'full'
            result['detail'] = 'set ups.power_loss_gpio for a definitive reading'
