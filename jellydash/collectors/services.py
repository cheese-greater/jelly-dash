"""Service health: systemd unit, docker container and HTTP probe per service."""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ..util import dotted_get, http_probe, read_text, run


VERSION_REFRESH_SECONDS = 600


class ServiceCollector:
    def __init__(self, services_config):
        self.services = list(services_config or [])
        self._version_cache = {}

    def collect(self):
        if not self.services:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(self.services))) as pool:
            results = list(pool.map(self.check, self.services))
        return [result for result, service in zip(results, self.services)
                if not (service.get('optional') and result['status'] == 'unknown')]

    def _cached_version(self, service):
        """Versions change rarely, so hit the version URL every 10 minutes, not every cycle."""
        key = service.get('name', '')
        cached = self._version_cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < VERSION_REFRESH_SECONDS:
            return cached[1]
        version = None
        _, probe = self._first_working(service['version_url'], want_json=True)
        if probe and probe['json'] is not None:
            value = dotted_get(probe['json'], service.get('version_key', 'version'))
            version = str(value) if value is not None else None
        if version is not None or cached is None:
            self._version_cache[key] = (now, version)
        return version if version is not None else (cached[1] if cached else None)

    @staticmethod
    def _candidates(value):
        """A config value may be one name or a list of names to try in order."""
        if not value:
            return []
        return [value] if isinstance(value, str) else [item for item in value if item]

    @staticmethod
    def _first_working(urls, want_json=False):
        """Probe each candidate URL in order; return (url, probe) for the first OK one, else the last."""
        candidates = [urls] if isinstance(urls, str) else list(urls or [])
        probe = None
        for url in candidates:
            probe = http_probe(url, timeout=3, want_json=want_json)
            if probe['ok']:
                return url, probe
        return (candidates[-1] if candidates else None), probe

    def check(self, service):
        result = {'name': service.get('name', '?'), 'status': 'unknown', 'detail': '', 'runner': None,
                  'uptime_s': None, 'pid': None, 'memory': None, 'restarts': None, 'version': None,
                  'latency_ms': None, 'http_status': None, 'url': service.get('url')}
        process_running = None
        for unit in self._candidates(service.get('systemd')):
            systemd = self._systemd(unit)
            if systemd['found']:
                process_running = systemd['active']
                result.update(runner=f'systemd:{unit}', uptime_s=systemd['uptime_s'], pid=systemd['pid'],
                              memory=systemd['memory'], restarts=systemd['restarts'], detail=systemd['detail'])
                break
        if process_running is None:
            for container in self._candidates(service.get('docker')):
                docker = self._docker(container)
                if docker['found']:
                    process_running = docker['running']
                    result.update(runner=f'docker:{container}', uptime_s=docker['uptime_s'], detail=docker['detail'])
                    break
        http = None
        url = None
        if service.get('url'):
            url, http = self._first_working(service['url'])
            result['url'] = url
            result['latency_ms'] = http['latency_ms']
            result['http_status'] = http['status']
        if service.get('version_url') and (http is None or http['ok']):
            result['version'] = self._cached_version(service)
        if http is not None:
            if http['ok']:
                result['status'] = 'up'
            elif process_running:
                result['status'] = 'degraded'
                result['detail'] = f"running but {url} failed: {http['error'] or http['status']}"
            else:
                result['status'] = 'down'
                if process_running is None:
                    result['detail'] = f"no unit or container found; {http['error'] or http['status']}"
                elif not result['detail']:
                    result['detail'] = http['error'] or f"HTTP {http['status']}"
        elif process_running is True:
            result['status'] = 'up'
        elif process_running is False:
            result['status'] = 'down'
        else:
            result['detail'] = 'nothing found: check systemd/docker names in config'
        return result

    @staticmethod
    def _systemd(unit):
        properties = 'LoadState,ActiveState,SubState,ActiveEnterTimestampMonotonic,MainPID,MemoryCurrent,NRestarts'
        code, output, _ = run(['systemctl', 'show', unit, '--no-pager', f'--property={properties}'], timeout=5)
        info = {'found': False, 'active': False, 'uptime_s': None, 'pid': None, 'memory': None, 'restarts': None,
                'detail': ''}
        if code != 0:
            return info
        values = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if values.get('LoadState') in (None, '', 'not-found'):
            return info
        info['found'] = True
        info['active'] = values.get('ActiveState') == 'active'
        info['detail'] = f"{values.get('ActiveState')} ({values.get('SubState')})"
        try:
            pid = int(values.get('MainPID', '0'))
            info['pid'] = pid or None
        except ValueError:
            pass
        try:
            info['memory'] = int(values.get('MemoryCurrent', ''))
        except ValueError:
            pass
        try:
            info['restarts'] = int(values.get('NRestarts', ''))
        except ValueError:
            pass
        try:
            entered = int(values.get('ActiveEnterTimestampMonotonic', '0')) / 1e6
            uptime_now = float((read_text('/proc/uptime', '0 0') or '0 0').split()[0])
            if entered > 0 and info['active']:
                info['uptime_s'] = max(uptime_now - entered, 0)
        except ValueError:
            pass
        return info

    @staticmethod
    def _docker(container):
        info = {'found': False, 'running': False, 'uptime_s': None, 'detail': ''}
        code, output, _ = run(['docker', 'inspect', container], timeout=8)
        if code != 0:
            return info
        try:
            entries = json.loads(output)
        except ValueError:
            return info
        if not entries:
            return info
        entry = entries[0]
        state = entry.get('State') or {}
        info['found'] = True
        info['running'] = bool(state.get('Running'))
        image = (entry.get('Config') or {}).get('Image', '')
        info['detail'] = f"{state.get('Status', 'unknown')} {image}".strip()
        started = state.get('StartedAt') or ''
        if info['running'] and started:
            trimmed = re.sub(r'(\.\d{6})\d+', r'\1', started).replace('Z', '+00:00')
            try:
                started_at = datetime.fromisoformat(trimmed)
                info['uptime_s'] = max((datetime.now(timezone.utc) - started_at).total_seconds(), 0)
            except ValueError:
                pass
        return info
