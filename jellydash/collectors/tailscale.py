"""Tailscale node and peer status from `tailscale status --json`."""

import json

from ..util import run


def collect_tailscale():
    code, output, error = run(['tailscale', 'status', '--json'], timeout=8)
    if code not in (0, None):
        code, output, error = run(['tailscale', 'status', '--json'], timeout=8, use_sudo=True)
    if code is None:
        return {'available': False, 'state': 'unavailable', 'error': error.strip()}
    if code != 0:
        return {'available': False, 'state': 'error', 'error': (error or output).strip()[:300]}
    try:
        data = json.loads(output)
    except ValueError as parse_error:
        return {'available': False, 'state': 'error', 'error': f'bad JSON from tailscale: {parse_error}'}
    self_node = data.get('Self') or {}
    peers = []
    for peer in (data.get('Peer') or {}).values():
        peers.append({
            'hostname': peer.get('HostName'),
            'dns_name': (peer.get('DNSName') or '').rstrip('.'),
            'os': peer.get('OS'),
            'online': bool(peer.get('Online')),
            'active': bool(peer.get('Active')),
            'direct': bool(peer.get('CurAddr')),
            'cur_addr': peer.get('CurAddr') or None,
            'relay': peer.get('Relay') or None,
            'rx_bytes': peer.get('RxBytes') or 0,
            'tx_bytes': peer.get('TxBytes') or 0,
            'last_seen': peer.get('LastSeen'),
            'ips': peer.get('TailscaleIPs') or [],
            'exit_node': bool(peer.get('ExitNode')),
            'exit_node_option': bool(peer.get('ExitNodeOption'))
        })
    peers.sort(key=lambda peer: (not peer['online'], (peer['hostname'] or '').lower()))
    exit_node = next((peer['hostname'] for peer in peers if peer['exit_node']), None)
    return {
        'available': True,
        'state': data.get('BackendState'),
        'version': (data.get('Version') or '').split('-')[0],
        'ips': data.get('TailscaleIPs') or [],
        'hostname': self_node.get('HostName'),
        'dns_name': (self_node.get('DNSName') or '').rstrip('.'),
        'os': self_node.get('OS'),
        'online': bool(self_node.get('Online')),
        'relay': self_node.get('Relay') or None,
        'tailnet': (data.get('CurrentTailnet') or {}).get('Name'),
        'magic_dns_suffix': data.get('MagicDNSSuffix'),
        'health': data.get('Health') or [],
        'exit_node': exit_node,
        'exit_node_option': bool(self_node.get('ExitNodeOption')),
        'peers': peers,
        'peers_online': sum(1 for peer in peers if peer['online']),
        'peers_total': len(peers)
    }


def find_peer_by_ip(status, ip_address):
    if not status or not status.get('available') or not ip_address:
        return None
    for peer in status.get('peers', []):
        if ip_address in peer.get('ips', []):
            return peer
    return None
