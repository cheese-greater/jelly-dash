"""Interfaces, addresses, Wi-Fi signal and throughput."""

import json
import os

from ..util import RateTracker, read_int, read_text, run


class NetworkCollector:
    def __init__(self, network_config):
        self.only = list(network_config.get('interfaces') or [])
        self.hide_prefixes = tuple(network_config.get('hide_prefixes') or [])
        self._rates = RateTracker()

    def collect(self):
        counters = self._counters()
        wireless = self._wireless_table()
        addresses = self._addresses()
        primary = self._default_interface()
        interfaces = []
        live_keys = set()
        try:
            names = sorted(os.listdir('/sys/class/net'))
        except OSError:
            names = []
        for name in names:
            if name == 'lo' or name.startswith(self.hide_prefixes):
                continue
            if self.only and name not in self.only:
                continue
            base = f'/sys/class/net/{name}'
            is_wifi = os.path.isdir(f'{base}/wireless') or name in wireless
            if name.startswith('tailscale'):
                kind = 'tailscale'
            elif is_wifi:
                kind = 'wifi'
            elif os.path.isdir(f'{base}/bridge'):
                kind = 'bridge'
            elif os.path.isdir(f'{base}/device'):
                kind = 'ethernet'
            else:
                kind = 'virtual'
            rx_bytes, tx_bytes = counters.get(name, (None, None))
            live_keys.update({f'{name}:rx', f'{name}:tx'})
            speed = read_int(f'{base}/speed')
            interfaces.append({
                'name': name,
                'kind': kind,
                'up': read_text(f'{base}/operstate') in ('up', 'unknown'),
                'operstate': read_text(f'{base}/operstate'),
                'mac': read_text(f'{base}/address'),
                'mtu': read_int(f'{base}/mtu'),
                'speed_mbps': speed if speed and speed > 0 else None,
                'ips': addresses.get(name, []),
                'rx_bytes': rx_bytes,
                'tx_bytes': tx_bytes,
                'rx_rate': self._rates.rate(f'{name}:rx', rx_bytes),
                'tx_rate': self._rates.rate(f'{name}:tx', tx_bytes),
                'wifi': self._wifi(name, wireless) if is_wifi else None
            })
        self._rates.forget_missing(live_keys)
        if primary not in [interface['name'] for interface in interfaces] and interfaces:
            primary = next((interface['name'] for interface in interfaces if interface['up'] and interface['kind'] != 'tailscale'),
                           interfaces[0]['name'])
        return {'primary': primary, 'interfaces': interfaces}

    @staticmethod
    def _counters():
        counters = {}
        for line in (read_text('/proc/net/dev', '') or '').splitlines()[2:]:
            name, _, rest = line.partition(':')
            fields = rest.split()
            if len(fields) >= 9:
                counters[name.strip()] = (int(fields[0]), int(fields[8]))
        return counters

    @staticmethod
    def _default_interface():
        for line in (read_text('/proc/net/route', '') or '').splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 4 and fields[1] == '00000000' and int(fields[3], 16) & 2:
                return fields[0]
        return None

    @staticmethod
    def _wireless_table():
        table = {}
        for line in (read_text('/proc/net/wireless', '') or '').splitlines()[2:]:
            name, _, rest = line.partition(':')
            fields = rest.split()
            if len(fields) < 3:
                continue
            try:
                quality = float(fields[1].rstrip('.'))
                level = float(fields[2].rstrip('.'))
            except ValueError:
                continue
            if level > 127:
                level -= 256
            table[name.strip()] = (quality, level if level < 0 else None)
        return table

    @staticmethod
    def _addresses():
        code, output, _ = run(['ip', '-j', 'addr'], timeout=3)
        addresses = {}
        if code != 0:
            return addresses
        try:
            entries = json.loads(output)
        except ValueError:
            return addresses
        for entry in entries:
            found = []
            for info in entry.get('addr_info') or []:
                if info.get('scope') != 'global':
                    continue
                found.append(f"{info.get('local')}/{info.get('prefixlen')}")
            addresses[entry.get('ifname')] = found
        return addresses

    @staticmethod
    def _wifi(name, wireless):
        info = {'ssid': None, 'signal_dbm': None, 'quality_pct': None, 'freq_mhz': None,
                'tx_bitrate_mbps': None, 'rx_bitrate_mbps': None}
        if name in wireless and wireless[name][1] is not None:
            info['signal_dbm'] = int(wireless[name][1])
        code, output, _ = run(['iw', 'dev', name, 'link'], timeout=3)
        if code == 0:
            for raw_line in output.splitlines():
                line = raw_line.strip()
                try:
                    if line.startswith('SSID:'):
                        info['ssid'] = line[5:].strip()
                    elif line.startswith('freq:'):
                        info['freq_mhz'] = float(line[5:].split()[0])
                    elif line.startswith('signal:'):
                        info['signal_dbm'] = int(float(line.split()[1]))
                    elif line.startswith('tx bitrate:'):
                        info['tx_bitrate_mbps'] = float(line.split(':', 1)[1].split()[0])
                    elif line.startswith('rx bitrate:'):
                        info['rx_bitrate_mbps'] = float(line.split(':', 1)[1].split()[0])
                except (ValueError, IndexError):
                    continue
        if info['signal_dbm'] is not None:
            info['quality_pct'] = max(0, min(100, 2 * (info['signal_dbm'] + 100)))
        return info
