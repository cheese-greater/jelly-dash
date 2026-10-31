"""Generic /sys/class/hwmon scan: temperatures, voltages, currents, alarms."""

import os

from ..util import read_int, read_text

HWMON_ROOT = '/sys/class/hwmon'

SCALES = {
    'temp': (1000.0, 'C'),
    'in': (1000.0, 'V'),
    'curr': (1000.0, 'A'),
    'power': (1000000.0, 'W'),
    'fan': (1.0, 'rpm')
}


def scan_hwmon():
    """Return a list of hwmon chips, each with its scaled readings."""
    chips = []
    try:
        entries = sorted(os.listdir(HWMON_ROOT))
    except OSError:
        return chips
    for entry in entries:
        chip_path = os.path.join(HWMON_ROOT, entry)
        device_link = os.path.join(chip_path, 'device')
        device_path = os.path.realpath(device_link) if os.path.exists(device_link) else None
        readings = []
        try:
            file_names = sorted(os.listdir(chip_path))
        except OSError:
            continue
        for file_name in file_names:
            if not (file_name.endswith('_input') or file_name.endswith('_alarm')):
                continue
            raw = read_int(os.path.join(chip_path, file_name))
            if raw is None:
                continue
            channel = file_name.split('_', 1)[0]
            kind = channel.rstrip('0123456789')
            label = read_text(os.path.join(chip_path, channel + '_label'), channel)
            if file_name.endswith('_alarm'):
                readings.append({'channel': channel, 'kind': 'alarm', 'label': label,
                                 'name': file_name[:-len('_alarm')], 'value': bool(raw)})
                continue
            scale, unit = SCALES.get(kind, (1.0, ''))
            readings.append({'channel': channel, 'kind': kind, 'label': label,
                             'name': file_name[:-len('_input')], 'value': raw / scale, 'unit': unit})
        chips.append({'id': entry, 'name': read_text(os.path.join(chip_path, 'name'), entry),
                      'device_path': device_path, 'readings': readings})
    return chips
