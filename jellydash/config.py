"""Configuration loading: defaults merged with an optional TOML file."""

import copy
import os
import sys

DEFAULT_CONFIG = {
    'server': {
        'bind': '0.0.0.0',
        'port': 8585,
        'base_path': '',
        'log_requests': False,
        'sample_interval': 2.0,
        'slow_interval': 10.0,
        'history_points': 180,
        'top_processes': 15
    },
    'ups': {
        'source': 'auto',
        'nut_host': 'localhost',
        'nut_port': 3493,
        'nut_ups': '',
        'nut_username': '',
        'nut_password': '',
        'i2c_bus': 1,
        'i2c_address': 0,
        'cells': 1,
        'shunt_ohms': 0.1,
        'invert_current': False,
        'power_loss_gpio': -1,
        'power_loss_active_high': True,
        'command': ''
    },
    'network': {
        'interfaces': [],
        'hide_prefixes': ['veth', 'br-', 'docker', 'virbr']
    },
    'disks': {
        'exclude_fstypes': ['squashfs', 'tmpfs', 'devtmpfs', 'overlay', 'iso9660', 'fuse.portal'],
        'exclude_mounts': [],
        'smart': True,
        'smart_interval': 300
    },
    'processes': {
        'show_kernel_threads': False
    },
    'actions': [
        {
            'name': 'update',
            'label': 'update -r',
            'description': 'Update the system, Jellyfin, SearXNG and Tailscale',
            'command': 'sudo -n /usr/local/bin/update -r',
            'confirm': True
        }
    ],
    'services': [
        {
            'name': 'Jellyfin',
            'systemd': 'jellyfin',
            'docker': 'jellyfin',
            'url': 'http://127.0.0.1:8096/health',
            'version_url': 'http://127.0.0.1:8096/System/Info/Public',
            'version_key': 'Version'
        },
        {
            'name': 'SearXNG',
            'systemd': ['searxng', 'uwsgi'],
            'docker': 'searxng',
            'url': ['http://127.0.0.1/searxng/healthz', 'http://127.0.0.1:8080/healthz'],
            'version_url': ['http://127.0.0.1/searxng/config', 'http://127.0.0.1:8080/config'],
            'version_key': 'version'
        },
        {
            'name': 'Tailscale',
            'systemd': 'tailscaled'
        },
        {
            'name': 'NUT',
            'systemd': 'nut-server',
            'optional': True
        },
        {
            'name': 'nginx',
            'systemd': 'nginx',
            'docker': 'nginx',
            'optional': True
        },
        {
            'name': 'Caddy',
            'systemd': 'caddy',
            'docker': 'caddy',
            'optional': True
        },
        {
            'name': 'Docker',
            'systemd': 'docker',
            'optional': True
        }
    ]
}

CONFIG_SEARCH_PATHS = [
    os.environ.get('JELLYDASH_CONFIG', ''),
    os.path.join(os.getcwd(), 'config.toml'),
    '/etc/jellydash/config.toml'
]


def _merge(base, override):
    """Deep-merge dicts; lists and scalars in override replace the base value."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config():
    """Return (config, path_used). path_used is None when only defaults apply."""
    for path in CONFIG_SEARCH_PATHS:
        if path and os.path.isfile(path):
            try:
                import tomllib
            except ImportError:
                print(f'config file {path} found but Python {sys.version_info.major}.{sys.version_info.minor} '
                      'lacks tomllib (needs 3.11+); using defaults', file=sys.stderr)
                return copy.deepcopy(DEFAULT_CONFIG), None
            with open(path, 'rb') as handle:
                user_config = tomllib.load(handle)
            return _merge(DEFAULT_CONFIG, user_config), path
    return copy.deepcopy(DEFAULT_CONFIG), None
