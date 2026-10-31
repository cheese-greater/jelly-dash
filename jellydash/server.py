"""Tiny threaded HTTP server: static dashboard plus a JSON status endpoint."""

import json
import mimetypes
import os
import signal
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .actions import ActionManager
from .collectors.tailscale import find_peer_by_ip
from .config import load_config
from .sampler import Sampler

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
ROOT_FILES = {'/manifest.webmanifest': 'manifest.webmanifest', '/sw.js': 'sw.js', '/favicon.ico': 'icon-192.png'}
mimetypes.add_type('application/manifest+json', '.webmanifest')


def _client_ip(handler):
    forwarded = handler.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    address = handler.client_address[0]
    if address.startswith('::ffff:'):
        address = address[7:]
    return address


def _is_tailscale_ip(address):
    if address.startswith('fd7a:115c:a1e0'):
        return True
    parts = address.split('.')
    if len(parts) != 4:
        return False
    try:
        first, second = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return first == 100 and 64 <= second <= 127


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = f'jellydash/{__version__}'
    sampler = None
    actions = None
    verbose = False
    base_path = ''

    def _route(self):
        """Return (path, parts) with the configured base path stripped, or (None, parts) when a
        redirect to the slash-terminated base path was sent instead."""
        parts = urlsplit(self.path)
        path = parts.path or '/'
        if self.base_path:
            if path == self.base_path:
                location = self.base_path + '/' + (f'?{parts.query}' if parts.query else '')
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header('Location', location)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return None, parts
            if path.startswith(self.base_path + '/'):
                path = path[len(self.base_path):]
        return path, parts

    def do_GET(self):
        path, parts = self._route()
        if path is None:
            return None
        if path == '/':
            return self._send_static('index.html')
        if path.startswith('/static/'):
            return self._send_static(path[len('/static/'):])
        if path in ROOT_FILES:
            return self._send_static(ROOT_FILES[path])
        if path == '/api/status':
            return self._send_json(self._status())
        if path == '/api/health':
            return self._send_json({'ok': True, 'version': __version__})
        if path == '/api/actions':
            return self._send_json({'actions': self.actions.status()})
        if path.startswith('/api/actions/') and path.endswith('/stream'):
            name = path[len('/api/actions/'):-len('/stream')]
            return self._stream_action(name, parse_qs(parts.query))
        return self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self):
        """Same routing as GET; the body is suppressed in _write."""
        return self.do_GET()

    def do_POST(self):
        path, _ = self._route()
        if path is None:
            return None
        if path.startswith('/api/actions/'):
            remainder = path[len('/api/actions/'):]
            name, _, verb = remainder.rpartition('/')
            if verb == 'run':
                return self._run_action(name)
            if verb in ('input', 'stop'):
                return self._control_action(name, verb)
        return self.send_error(HTTPStatus.NOT_FOUND)

    def _read_json_body(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _control_action(self, name, verb):
        if not self._same_origin_request():
            return self._send_json({'error': 'cross-site request refused'}, HTTPStatus.FORBIDDEN)
        if self.actions.get(name) is None:
            return self._send_json({'error': 'unknown action'}, HTTPStatus.NOT_FOUND)
        body = self._read_json_body()
        try:
            if verb == 'input':
                text = str(body.get('text', ''))
                if body.get('newline', True):
                    text += '\n'
                self.actions.send_input(name, text)
            else:
                self.actions.stop(name)
        except RuntimeError as error:
            return self._send_json({'error': str(error)}, HTTPStatus.CONFLICT)
        except OSError as error:
            return self._send_json({'error': f'terminal write failed: {error}'}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return self._send_json({'ok': True})

    def _same_origin_request(self):
        """Reject cross-site requests: a custom header forces a CORS preflight that we never answer."""
        if self.headers.get('X-Requested-With') != 'jellydash':
            return False
        fetch_site = self.headers.get('Sec-Fetch-Site')
        return fetch_site in (None, 'same-origin', 'none')

    def _run_action(self, name):
        if not self._same_origin_request():
            return self._send_json({'error': 'cross-site request refused'}, HTTPStatus.FORBIDDEN)
        if self.actions.get(name) is None:
            return self._send_json({'error': 'unknown action'}, HTTPStatus.NOT_FOUND)
        try:
            run = self.actions.start(name)
        except RuntimeError as error:
            return self._send_json({'error': str(error)}, HTTPStatus.CONFLICT)
        return self._send_json({'started': True, 'run': run.describe()})

    def _stream_action(self, name, query):
        if self.actions.get(name) is None:
            return self.send_error(HTTPStatus.NOT_FOUND)
        run = self.actions.run_for(name)
        if run is None:
            return self._send_json({'error': 'no run yet'}, HTTPStatus.NOT_FOUND)
        offset = 0
        last_event_id = self.headers.get('Last-Event-ID')
        try:
            if last_event_id is not None:
                offset = int(last_event_id)
            elif query.get('from'):
                offset = int(query['from'][0])
        except ValueError:
            offset = 0
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        if self.command == 'HEAD':
            return
        if offset < run.dropped:
            self._send_event('output', json.dumps(f'[... {run.dropped - offset} earlier chunks dropped ...]\r\n'), None)
        while True:
            next_offset, chunks, finished = run.wait_from(offset, 15.0)
            for index, chunk in enumerate(chunks):
                if not self._send_event('output', json.dumps(chunk), offset + index + 1):
                    return
            if chunks:
                offset = next_offset
                continue
            if finished:
                self._send_event('exit', json.dumps(run.describe()), None)
                return
            if not self._send_raw(b': keepalive\n\n'):
                return

    def _send_event(self, event, data, event_id):
        lines = [f'event: {event}']
        if event_id is not None:
            lines.append(f'id: {event_id}')
        lines.append(f'data: {data}')
        return self._send_raw(('\n'.join(lines) + '\n\n').encode('utf-8'))

    def _send_raw(self, payload):
        try:
            self.wfile.write(payload)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _status(self):
        data = self.sampler.snapshot()
        data['actions'] = self.actions.status()
        ip_address = _client_ip(self)
        viewer = {'ip': ip_address, 'via': 'tailscale' if _is_tailscale_ip(ip_address) else 'lan',
                  'login': self.headers.get('Tailscale-User-Login'),
                  'name': self.headers.get('Tailscale-User-Name'), 'peer': None}
        peer = find_peer_by_ip(data.get('tailscale'), ip_address)
        if peer:
            viewer['via'] = 'tailscale'
            viewer['peer'] = {key: peer.get(key) for key in ('hostname', 'os', 'direct', 'cur_addr', 'relay', 'dns_name')}
        data['viewer'] = viewer
        return data

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self._write(body)

    def _send_static(self, relative):
        full_path = os.path.realpath(os.path.join(STATIC_DIR, relative))
        if not full_path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full_path):
            return self.send_error(HTTPStatus.NOT_FOUND)
        content_type = mimetypes.guess_type(full_path)[0] or 'application/octet-stream'
        if content_type.startswith('text/') or content_type in ('application/javascript', 'application/json'):
            content_type += '; charset=utf-8'
        with open(full_path, 'rb') as handle:
            body = handle.read()
        if relative == 'sw.js':
            body = body.replace(b'__VERSION__', __version__.encode('utf-8'))
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        if relative == 'sw.js':
            self.send_header('Service-Worker-Allowed', '/')
        self.end_headers()
        self._write(body)

    def _write(self, body):
        if self.command == 'HEAD':
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *arguments):  # noqa: A002 - signature fixed by BaseHTTPRequestHandler
        if self.verbose:
            super().log_message(format, *arguments)


def main():
    # A parent that started us as a background job may have left SIGINT/SIGQUIT ignored; children
    # would inherit that and Ctrl-C from the action dialog would do nothing. Restore the defaults.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGQUIT, signal.SIG_DFL)
    config, config_path = load_config()
    DashboardHandler.verbose = ('-v' in sys.argv or os.environ.get('JELLYDASH_VERBOSE') == '1'
                                or bool(config['server'].get('log_requests')))
    sampler = Sampler(config)
    sampler.start()
    DashboardHandler.sampler = sampler
    DashboardHandler.actions = ActionManager(config.get('actions'))
    base_path = str(config['server'].get('base_path') or '').strip().rstrip('/')
    if base_path and not base_path.startswith('/'):
        base_path = '/' + base_path
    DashboardHandler.base_path = base_path
    bind = os.environ.get('JELLYDASH_BIND', config['server']['bind'])
    port = int(os.environ.get('JELLYDASH_PORT', config['server']['port']))
    server = ThreadingHTTPServer((bind, port), DashboardHandler)
    server.daemon_threads = True
    print(f'jellydash {__version__} listening on http://{bind}:{port}{base_path}  (config: {config_path or "defaults"})',
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sampler.stop()
        server.server_close()


if __name__ == '__main__':
    main()
