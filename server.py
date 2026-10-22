#!/usr/bin/env python3
"""Serve VPN provisioning and its editable UI document from one HTTP listener."""

import argparse
import base64
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shlex
import socket
import sys
from urllib.parse import urlsplit

from validation import load_document
from provisioning import adapt_xray_subscription, load_json, validate_provisioning


DEFAULT_ENV_PATH = Path(__file__).resolve().with_name('.env')
MAX_CONFIG_BYTES = 8 * 1024 * 1024
APP_HEADERS = {'Vary': 'User-Agent, X-Subscription-Format, X-Subscription-Capabilities',
               'X-Subscription-Format': 'vipin-v1'}


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
            raise ValueError('Invalid .env entry')
        parts = shlex.split(value, comments=True, posix=True)
        if len(parts) > 1:
            raise ValueError('Quote values containing spaces')
        values[key.strip()] = parts[0] if parts else ''
    return values


def local_path(value: str, env_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else env_path.parent / path


def settings(env_path: Path) -> dict[str, str]:
    values = read_env(env_path)
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', values.get('SUBSCRIPTION_TOKEN', '')):
        raise ValueError('Invalid subscription token')
    if values.get('VPN_CONFIG_FORMAT', 'sing-box') not in ('sing-box', 'vless-failover', 'xray'):
        raise ValueError('Invalid VPN config format')
    prefix = values.get('SUBSCRIPTION_PATH_PREFIX', '/subscription')
    if not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)+', prefix):
        raise ValueError('Invalid subscription path prefix')
    if values.get('PUBLIC_BASE_URL', '').strip():
        trusted_https_url(values['PUBLIC_BASE_URL'].strip())
    secret = values.get('APP_SUBSCRIPTION_SIGNING_SECRET', '')
    if secret and len(secret.encode('utf-8')) < 32:
        raise ValueError('Signing secret must contain at least 32 bytes')
    agent = values.get('APP_USER_AGENT_PREFIX', 'VipinApp/')
    if not agent or len(agent) > 128 or any(ord(c) < 33 or ord(c) >= 127 for c in agent):
        raise ValueError('Invalid application User-Agent prefix')
    return values


def document_path(values: dict[str, str], env_path: Path, override: Path | None) -> Path:
    if override is not None:
        return override
    return local_path(values.get('UI_DOCUMENT_PATH', 'data/subscription.json'), env_path)


def read_config(values: dict[str, str], env_path: Path) -> dict[str, object]:
    path = local_path(values['SING_BOX_CONFIG_PATH'].strip(), env_path)
    config = load_json(path, MAX_CONFIG_BYTES)
    mode = values.get('VPN_CONFIG_FORMAT', 'sing-box')
    if mode == 'xray':
        return adapt_xray_subscription(json.dumps(config, ensure_ascii=False).encode('utf-8'))
    if mode == 'vless-failover':
        return validate_provisioning(config)
    if not isinstance(config, dict):
        raise ValueError('Configuration must be an object')
    return config


def app_token(values: dict[str, str]) -> str:
    secret = values.get('APP_SUBSCRIPTION_SIGNING_SECRET', '')
    token = values['SUBSCRIPTION_TOKEN']
    if not secret:
        return token
    digest = hmac.digest(secret.encode('utf-8'), ('vipin-v1:' + token).encode('ascii'), 'sha256')
    return base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')


def check_files(values: dict[str, str], env_path: Path, data_path: Path | None) -> None:
    load_document(document_path(values, env_path, data_path))
    if values.get('SING_BOX_CONFIG_PATH', '').strip():
        read_config(values, env_path)


def trusted_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 or ord(c) >= 127 or c == "\\" for c in value)):
        raise ValueError("Invalid public HTTPS URL")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid public HTTPS port")
    return value.rstrip("/")


def request_origin(host: str) -> str:
    if (not host or any(character.isspace() or ord(character) < 32 or ord(character) >= 127
                        or character == '\\' for character in host)):
        raise ValueError('Invalid Host header')
    url = urlsplit('http://' + host)
    if (not url.hostname or url.username is not None or url.password is not None
            or url.path or url.query or url.fragment or url.netloc != host
            or (url.port is not None and not 1 <= url.port <= 65535)):
        raise ValueError('Invalid Host header')
    return 'http://' + host


def create_server(env_path: Path, host: str | None = None, port: int | None = None,
                  data_path: Path | None = None) -> ThreadingHTTPServer:
    """Validate local settings and UI before binding a single server socket."""
    env_path = Path(env_path).resolve()
    data_path = Path(data_path).absolute() if data_path is not None else None
    startup = settings(env_path)
    host = host if host is not None else startup.get('HOST', '127.0.0.1')
    port = port if port is not None else int(startup.get('PORT', '8082'))
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('Port must be between 0 and 65535')
    load_document(document_path(startup, env_path, data_path))

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(10)

        def do_GET(self) -> None:
            self.serve_subscription()

        def do_HEAD(self) -> None:
            self.serve_subscription()

        def __getattr__(self, name: str):
            if name.startswith('do_'):
                return self.method_not_allowed
            raise AttributeError(name)

        def method_not_allowed(self) -> None:
            self.respond(405, {'error': 'method_not_allowed'}, {'Allow': 'GET, HEAD'})

        def serve_subscription(self) -> None:
            try:
                path = urlsplit(self.path).path
            except ValueError:
                self.respond(400, {'error': 'invalid_request'})
                return
            if path == '/health':
                self.respond(200, {'status': 'ok'})
                return
            try:
                values = settings(env_path)
                token = values['SUBSCRIPTION_TOKEN']
                subscription_path = values.get('SUBSCRIPTION_PATH_PREFIX', '/subscription') + '/' + token
                native_base = subscription_path + '/app/v1'
                native_ui = hmac.compare_digest(path.encode('utf-8'), (native_base + '/ui').encode('ascii'))
                native_config = hmac.compare_digest(path.encode('utf-8'), (native_base + '/config').encode('ascii'))
                native = native_ui or native_config or (
                    self.headers.get('User-Agent', '').startswith(values.get('APP_USER_AGENT_PREFIX', 'VipinApp/'))
                    and bool(self.headers.get('X-Subscription-Format')))
                compatible = 'vless-failover-v1' in self.headers.get('X-Subscription-Capabilities', '').split()
                self.native_response = native
                if path == '/v1/subscription' or native_ui or native_config:
                    authorization = self.headers.get_all('Authorization', [])
                    expected = ('Bearer ' + (app_token(values) if native_ui or native_config else token)).encode('ascii')
                    if (len(authorization) != 1
                            or not hmac.compare_digest(authorization[0].encode('utf-8'), expected)):
                        self.respond(401, {'error': 'unauthorized'}, {'WWW-Authenticate': 'Bearer'})
                        return
                    if native_config:
                        if not values.get('SING_BOX_CONFIG_PATH', '').strip():
                            self.respond(404, {'error': 'config_not_configured'})
                            return
                        if values.get('VPN_CONFIG_FORMAT', 'sing-box') != 'sing-box' and not compatible:
                            self.respond(406, {'error': 'unsupported_subscription_capability'})
                            return
                        self.respond(200, read_config(values, env_path))
                    else:
                        self.respond(200, load_document(document_path(values, env_path, data_path)))
                elif hmac.compare_digest(path.encode('utf-8'), (subscription_path + '/config').encode('ascii')):
                    if not values.get('SING_BOX_CONFIG_PATH', '').strip():
                        self.respond(404, {'error': 'not_found'})
                        return
                    self.respond(200, read_config(values, env_path))
                elif hmac.compare_digest(path.encode('utf-8'), subscription_path.encode('ascii')):
                    if native and self.headers.get('X-Subscription-Format') != 'vipin-v1':
                        self.respond(406, {'error': 'unsupported_subscription_format'})
                        return
                    try:
                        public_base = values.get('PUBLIC_BASE_URL', '').strip()
                        if public_base:
                            origin = trusted_https_url(public_base)
                        else:
                            hosts = self.headers.get_all('Host', [])
                            if len(hosts) != 1:
                                raise ValueError('Invalid Host header')
                            origin = request_origin(hosts[0])
                    except ValueError:
                        self.respond(400, {'error': 'invalid_request'})
                        return
                    # The existing app permits native tokens over HTTPS or
                    # loopback HTTP. Its original routes also support LAN HTTP.
                    parsed_origin = urlsplit(origin)
                    if native and parsed_origin.scheme == 'http' and parsed_origin.hostname not in ('localhost', '127.0.0.1', '::1'):
                        native = False
                        self.native_response = False
                    vpn_enabled = bool(values.get('SING_BOX_CONFIG_PATH', '').strip())
                    if native and values.get('VPN_CONFIG_FORMAT', 'sing-box') != 'sing-box' and not compatible:
                        vpn_enabled = False
                    self.respond(200, {
                        'sing_box_url': origin + (native_base if native else subscription_path) + '/config' if vpn_enabled else None,
                        'vk_turn_link': values.get('VK_TURN_LINK', '').strip() or None,
                        'ui_url': origin + native_base + '/ui' if native else origin + '/v1/subscription',
                        'ui_token': app_token(values) if native else token,
                    })
                else:
                    self.respond(404, {'error': 'not_found'})
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError, RecursionError):
                self.respond(503, {'error': 'subscription_unavailable'})

        def respond(self, status: int, payload: dict[str, object],
                    extra_headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                if getattr(self, 'native_response', False):
                    for name, value in APP_HEADERS.items():
                        self.send_header(name, value)
                for name, value in (extra_headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def send_error(self, code: int, message: str | None = None,
                       explain: str | None = None) -> None:
            self.respond(code, {'error': 'invalid_request'})

        def log_message(self, format: str, *args: object) -> None:
            # Subscription URLs and Authorization headers contain credentials.
            pass

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ':' in host else socket.AF_INET

    return Server((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, default=DEFAULT_ENV_PATH, help='Path to the local .env file')
    parser.add_argument('--data', type=Path, help='Override the UI document path')
    parser.add_argument('--host', help='Override the listen address in .env')
    parser.add_argument('--port', type=int, help='Override the listen port in .env')
    parser.add_argument('--check', action='store_true', help='Validate local files without starting HTTP')
    args = parser.parse_args(argv)
    if args.check:
        try:
            env_path = args.env.resolve()
            values = settings(env_path)
            check_files(values, env_path, args.data)
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError, RecursionError):
            print('Cannot validate configuration: check .env, VPN config and UI document.', file=sys.stderr)
            return 1
        print('Configuration and UI document are valid.')
        return 0
    try:
        httpd = create_server(args.env, args.host, args.port, args.data)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, RecursionError):
        print('Cannot start server: check .env, UI document, token and listen address.', file=sys.stderr)
        return 1
    print(f'Subscription server listening on port {httpd.server_port}. Press Ctrl+C to stop.', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
