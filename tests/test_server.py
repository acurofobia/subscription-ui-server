import contextlib
import base64
import hashlib
import hmac
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from server import create_server


ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'test-subscription-token-123456'
NODE = {'type': 'vless', 'server': 'example.org', 'server_port': 443,
        'uuid': 'bf000d23-0752-40b4-affe-68f7707a9661'}
LINK = 'wdtt://example.org:443:a:b:password:call'


class ServerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env = self.root / '.env'
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'outbounds': [NODE]}))
        self.data = self.root / 'document.json'
        self.document = json.loads((ROOT / 'data/subscription.example.json').read_text())
        self.write_document()
        self.write_env()
        self.httpd = self.start_server()
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'

    def write_env(self, token=TOKEN, config='config.json', vk=LINK):
        self.env.write_text(f'SUBSCRIPTION_TOKEN={token}\nSING_BOX_CONFIG_PATH={config}\n'
                            f'VK_TURN_LINK="{vk}"\nUI_DOCUMENT_PATH=document.json\n')

    def write_document(self):
        self.data.write_text(json.dumps(self.document, ensure_ascii=False), encoding='utf-8')

    def start_server(self, host='127.0.0.1'):
        httpd = create_server(self.env, host=host, port=0)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()

        def stop():
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

        self.addCleanup(stop)
        return httpd

    def request(self, path=None, method='GET', headers=None, httpd=None):
        httpd = httpd or self.httpd
        connection = HTTPConnection(httpd.server_address[0], httpd.server_port, timeout=2)
        try:
            connection.request(method, path or '/subscription/' + TOKEN, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def assert_json(self, response, status, payload):
        actual_status, headers, body = response
        self.assertEqual(actual_status, status)
        self.assertEqual(json.loads(body), payload)
        self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(int(headers['Content-Length']), len(body))

    def ui(self, token=TOKEN, **kwargs):
        return self.request('/v1/subscription', headers={'Authorization': 'Bearer ' + token}, **kwargs)

    def test_one_listener_serves_complete_subscription_config_and_ui(self):
        envelope = {'sing_box_url': self.base + '/subscription/' + TOKEN + '/config',
                    'vk_turn_link': LINK, 'ui_url': self.base + '/v1/subscription', 'ui_token': TOKEN}
        self.assert_json(self.request(), 200, envelope)
        self.assert_json(self.request('/subscription/' + TOKEN + '/config'), 200, {'outbounds': [NODE]})
        self.assert_json(self.ui(), 200, self.document)
        self.assertIn('Количество серверов'.encode(), self.ui()[2])

    def test_links_use_the_request_host_and_port(self):
        response = self.request(headers={'Host': 'my-mac.local:8082'})
        envelope = json.loads(response[2])
        self.assertEqual(response[0], 200)
        self.assertEqual(envelope['ui_url'], 'http://my-mac.local:8082/v1/subscription')
        self.assertEqual(envelope['sing_box_url'], 'http://my-mac.local:8082/subscription/' + TOKEN + '/config')
        response = self.request(headers={'Host': '[::1]:8082'})
        self.assertEqual(json.loads(response[2])['ui_url'], 'http://[::1]:8082/v1/subscription')

    def test_invalid_host_headers_cannot_create_invalid_or_credentialed_links(self):
        for host in ['', 'user:password@example.org', 'example.org:99999', 'example.org/path',
                     'example.org?query', 'example.org#fragment', 'example.org\\@other.org', 'bad host']:
            with self.subTest(host=host):
                self.assert_json(self.request(headers={'Host': host}), 400, {'error': 'invalid_request'})

    def test_optional_transports_are_independent(self):
        for config, vk in [('', LINK), ('config.json', ''), ('', '')]:
            self.write_env(config=config, vk=vk)
            response = json.loads(self.request()[2])
            self.assertEqual(response['sing_box_url'] is None, not bool(config))
            self.assertEqual(response['vk_turn_link'], vk or None)
            self.assertEqual(self.ui()[0], 200)
            if not config:
                self.assertEqual(self.request('/subscription/' + TOKEN + '/config')[0], 404)

    def test_env_config_and_ui_reload_without_restart(self):
        self.request()
        self.write_env(vk='')
        self.config.write_text('{"outbounds":[]}')
        self.document['blocks'][0]['title'] = 'Новый заголовок'
        self.write_document()
        self.assertIsNone(json.loads(self.request()[2])['vk_turn_link'])
        self.assert_json(self.request('/subscription/' + TOKEN + '/config'), 200, {'outbounds': []})
        self.assert_json(self.ui(), 200, self.document)
        replacement = self.root / 'replacement.json'
        replacement.write_text('{"schemaVersion":1,"blocks":[],"pages":[]}')
        self.env.write_text(self.env.read_text().replace('document.json', 'replacement.json'))
        self.assert_json(self.ui(), 200, {'schemaVersion': 1, 'blocks': [], 'pages': []})

    def test_token_rotation_applies_to_all_routes_without_restart(self):
        replacement = 'replacement-token-123456'
        self.write_env(token=replacement)
        for path in ['/subscription/' + TOKEN, '/subscription/' + TOKEN + '/config']:
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.ui()[0], 401)
        self.assertEqual(self.ui(token=replacement)[0], 200)
        self.assertEqual(json.loads(self.request('/subscription/' + replacement)[2])['ui_token'], replacement)

    def test_ui_requires_exactly_one_bearer_header(self):
        for headers in [{}, {'Authorization': 'Bearer wrong'}, {'Authorization': 'Basic ' + TOKEN},
                        {'Authorization': 'Bearer café'}]:
            response = self.request('/v1/subscription', headers=headers)
            self.assert_json(response, 401, {'error': 'unauthorized'})
            self.assertEqual(response[1]['WWW-Authenticate'], 'Bearer')
        self.assertEqual(self.request('/v1/subscription?token=' + TOKEN)[0], 401)
        connection = HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=2)
        try:
            connection.putrequest('GET', '/v1/subscription')
            for _ in range(2):
                connection.putheader('Authorization', 'Bearer ' + TOKEN)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            connection.close()

    def test_query_does_not_change_the_document(self):
        self.assert_json(self.request('/v1/subscription?revision=2',
                                     headers={'Authorization': 'Bearer ' + TOKEN}), 200, self.document)

    def test_private_files_and_wrong_tokens_are_never_served(self):
        for path in ['/', '/.env', '/config.json', '/document.json', '/data/subscription.json',
                     '/server.py', '/subscription/wrong', '/subscription/wrong/config',
                     '/subscription/' + TOKEN + '/../.env', '/v1/%2e%2e/document.json', '/v1/subscription/']:
            with self.subTest(path=path):
                self.assert_json(self.request(path), 404, {'error': 'not_found'})

    def test_invalid_ui_edits_return_503_then_recover(self):
        for payload in [b'broken', b'[]', b'{"schemaVersion":2}', b'\xff', b' ' * (1024 * 1024 + 1)]:
            self.data.write_bytes(payload)
            self.assert_json(self.ui(), 503, {'error': 'subscription_unavailable'})
        self.data.unlink()
        self.assertEqual(self.ui()[0], 503)
        self.write_document()
        self.assert_json(self.ui(), 200, self.document)

    def test_missing_or_invalid_vpn_config_never_discloses_content(self):
        self.config.unlink()
        self.assertEqual(self.request('/subscription/' + TOKEN + '/config')[0], 503)
        for payload in ['private-invalid-json', '[]', '{"value":NaN}', '{"value":1e400}']:
            self.config.write_text(payload)
            self.assert_json(self.request('/subscription/' + TOKEN + '/config'),
                             503, {'error': 'subscription_unavailable'})
        self.config.write_text(json.dumps(NODE))
        self.assert_json(self.request('/subscription/' + TOKEN + '/config'), 200, NODE)

    def test_health_is_liveness_and_does_not_require_files_or_authorization(self):
        self.data.unlink()
        self.env.unlink()
        self.assert_json(self.request('/health'), 200, {'status': 'ok'})

    def test_head_has_get_status_and_length_without_a_body(self):
        for path, headers in [('/subscription/' + TOKEN, {}),
                              ('/subscription/' + TOKEN + '/config', {}),
                              ('/v1/subscription', {}),
                              ('/v1/subscription', {'Authorization': 'Bearer ' + TOKEN}),
                              ('/health', {}), ('/missing', {})]:
            get_status, get_headers, body = self.request(path, headers=headers)
            status, head_headers, head_body = self.request(path, method='HEAD', headers=headers)
            self.assertEqual(status, get_status)
            self.assertEqual(head_headers['Content-Length'], str(len(body)))
            self.assertEqual(head_headers['Content-Type'], get_headers['Content-Type'])
            self.assertEqual(head_body, b'')

    def test_unsupported_methods_return_405_without_echoing_the_request(self):
        for method in ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'CONNECT', 'CUSTOM']:
            response = self.request(method=method)
            self.assert_json(response, 405, {'error': 'method_not_allowed'})
            self.assertEqual(response[1]['Allow'], 'GET, HEAD')

    def test_invalid_settings_fail_closed_and_recover(self):
        self.env.write_text('SUBSCRIPTION_TOKEN=invalid!\n')
        self.assert_json(self.request(), 503, {'error': 'subscription_unavailable'})
        self.assertEqual(self.ui()[0], 503)
        self.write_env()
        self.assertEqual(self.request()[0], 200)

    def test_invalid_tokens_and_ports_are_rejected_before_listening(self):
        for token in ['', 'short', 'contains spaces here', 'é' * 20, 'a' * 257]:
            self.write_env(token=token)
            with self.assertRaises(ValueError):
                create_server(self.env, host='0.0.0.0', port=0)
        self.write_env()
        for port in [-1, 65536, True]:
            with self.assertRaises(ValueError):
                create_server(self.env, port=port)
        self.data.write_text('broken')
        with self.assertRaises(ValueError):
            create_server(self.env, port=0)

    def test_ipv6_loopback_when_available(self):
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind(('::1', 0))
        except OSError:
            self.skipTest('IPv6 loopback is unavailable')
        self.assert_json(self.request('/health', httpd=self.start_server(host='::1')), 200, {'status': 'ok'})

    def test_credentials_and_file_contents_are_not_logged_or_echoed(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr, contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.request()
            self.ui()
            self.data.write_text('private-invalid-document')
            response = self.ui()
        self.assert_json(response, 503, {'error': 'subscription_unavailable'})
        for secret in [TOKEN, LINK, 'private-invalid-document', str(self.data), 'Traceback']:
            self.assertNotIn(secret, stderr.getvalue() + stdout.getvalue() + response[2].decode())
        self.assertEqual(stderr.getvalue() + stdout.getvalue(), '')

    def configure(self, **values):
        with self.env.open('a') as stream:
            for key, value in values.items():
                stream.write(f'{key}="{value}"\n')

    def native_config(self, state='available'):
        nodes = [{**NODE, 'tag': f'n{i}', 'tls': {'enabled': True, 'server_name': 'example.org',
                  'utls': {'enabled': True, 'fingerprint': 'chrome'},
                  'reality': {'enabled': True, 'public_key': 'A' * 43, 'short_id': 'abcd'}}}
                 for i in range(1, 5)]
        config = {'schemaVersion': 1, 'kind': 'vless-failover', 'state': state,
                  'reason': None, 'outbounds': nodes,
                  'failover': {'selector': 'vk-wg', 'primary': 'n1', 'backups': ['n2', 'n3', 'n4'],
                               'intervalMilliseconds': 10000, 'timeoutMilliseconds': 5000,
                               'failureThreshold': 2, 'recoveryThreshold': 3}}
        self.config.write_text(json.dumps(config))
        self.configure(VPN_CONFIG_FORMAT='vless-failover')
        return config

    def app_headers(self, token=None, capability=True):
        headers = {'User-Agent': 'VipinApp/1.0', 'X-Subscription-Format': 'vipin-v1'}
        if capability:
            headers['X-Subscription-Capabilities'] = 'other vless-failover-v1'
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        return headers

    def test_native_routes_negotiate_format_capability_and_bearer(self):
        config = self.native_config()
        path = '/subscription/' + TOKEN
        response = self.request(headers=self.app_headers())
        self.assertEqual(response[0], 200)
        self.assertEqual(response[1]['X-Subscription-Format'], 'vipin-v1')
        self.assertIn('X-Subscription-Capabilities', response[1]['Vary'])
        descriptor = json.loads(response[2])
        self.assertEqual(descriptor['sing_box_url'], self.base + path + '/app/v1/config')
        self.assertEqual(descriptor['ui_url'], self.base + path + '/app/v1/ui')
        endpoint = path + '/app/v1/config'
        self.assertEqual(self.request(endpoint, headers=self.app_headers())[0], 401)
        self.assertEqual(self.request(endpoint, headers=self.app_headers('wrong'))[0], 401)
        self.assertEqual(self.request(endpoint, headers=self.app_headers(TOKEN, capability=False))[0], 406)
        self.assert_json(self.request(endpoint, headers=self.app_headers(TOKEN)), 200, config)
        self.assertIsNone(json.loads(self.request(headers=self.app_headers(capability=False))[2])['sing_box_url'])
        headers = self.app_headers()
        headers['X-Subscription-Format'] = 'vipin-v2'
        self.assertEqual(self.request(headers=headers)[0], 406)
        # The original URL still advertises its config for clients without native negotiation.
        self.assertEqual(json.loads(self.request()[2])['sing_box_url'], self.base + path + '/config')

    def test_public_base_and_route_prefix_are_configured_not_forwarded(self):
        self.configure(PUBLIC_BASE_URL='https://vpn.example.org/gateway', SUBSCRIPTION_PATH_PREFIX='/api/sub')
        response = self.request('/api/sub/' + TOKEN, headers={**self.app_headers(),
                                'Host': 'untrusted.example.org', 'X-Forwarded-Proto': 'http'})
        self.assertEqual(response[0], 200)
        descriptor = json.loads(response[2])
        self.assertEqual(descriptor['ui_url'], 'https://vpn.example.org/gateway/api/sub/' + TOKEN + '/app/v1/ui')
        self.assertEqual(self.request()[0], 404)
        for value in ['https://user:pass@example.org', 'https://example.org?x=1',
                      'https://example.org#x', 'ftp://example.org', 'https://example.org:99999']:
            self.configure(PUBLIC_BASE_URL=value)
            self.assert_json(self.request('/api/sub/' + TOKEN), 503, {'error': 'subscription_unavailable'})

    def test_optional_signing_secret_scopes_native_tokens_and_rotates(self):
        secret = 'synthetic-signing-secret-12345678901234567890'
        self.configure(APP_SUBSCRIPTION_SIGNING_SECRET=secret)
        descriptor = json.loads(self.request(headers=self.app_headers())[2])
        expected = base64.urlsafe_b64encode(hmac.digest(secret.encode(), ('vipin-v1:' + TOKEN).encode(), hashlib.sha256)).decode().rstrip('=')
        self.assertEqual(descriptor['ui_token'], expected)
        endpoint = '/subscription/' + TOKEN + '/app/v1/ui'
        self.assertEqual(self.request(endpoint, headers=self.app_headers(TOKEN))[0], 401)
        self.assertEqual(self.request(endpoint, headers=self.app_headers(expected))[0], 200)
        self.assertEqual(self.ui()[0], 200)
        self.configure(APP_SUBSCRIPTION_SIGNING_SECRET=secret + 'rotated')
        self.assertEqual(self.request(endpoint, headers=self.app_headers(expected))[0], 401)

    def test_native_config_rejects_invalid_failover_and_recovers(self):
        path = '/subscription/' + TOKEN + '/app/v1/config'
        for field, value in [('primary', 'missing'), ('backups', ['n2', 'n2', 'n4']),
                             ('failureThreshold', True), ('selector', 'direct')]:
            config = self.native_config()
            config['failover'][field] = value
            self.config.write_text(json.dumps(config))
            self.assert_json(self.request(path, headers=self.app_headers(TOKEN)), 503,
                             {'error': 'subscription_unavailable'})
        config = self.native_config()
        self.assert_json(self.request(path, headers=self.app_headers(TOKEN)), 200, config)

    def test_native_unavailable_response_has_no_credentials_and_preserves_bypass(self):
        config = self.native_config('unavailable')
        config.update(reason='traffic_limited', outbounds=[], failover=None)
        self.config.write_text(json.dumps(config))
        response = self.request('/subscription/' + TOKEN + '/app/v1/config', headers=self.app_headers(TOKEN))
        self.assert_json(response, 200, config)
        self.assertNotIn(NODE['uuid'], response[2].decode())
        self.assertEqual(json.loads(self.request()[2])['vk_turn_link'], LINK)
        self.assert_json(self.ui(), 200, self.document)

    def test_local_xray_file_produces_native_config(self):
        from test_provisioning import graph
        self.config.write_text(json.dumps(graph(('n4', 'n3', 'n2', 'n1'))))
        self.configure(VPN_CONFIG_FORMAT='xray')
        response = self.request('/subscription/' + TOKEN + '/app/v1/config', headers=self.app_headers(TOKEN))
        self.assertEqual(response[0], 200)
        config = json.loads(response[2])
        self.assertEqual(config['failover']['primary'], 'n4')
        self.assertEqual(config['failover']['backups'], ['n3', 'n2', 'n1'])

    def test_native_head_auth_and_length_match_get(self):
        self.native_config()
        for suffix in ['/app/v1/config', '/app/v1/ui']:
            path = '/subscription/' + TOKEN + suffix
            for headers in [{}, self.app_headers(TOKEN)]:
                get = self.request(path, headers=headers)
                head = self.request(path, method='HEAD', headers=headers)
                self.assertEqual(head[0], get[0])
                self.assertEqual(head[1]['Content-Length'], get[1]['Content-Length'])
                self.assertEqual(head[2], b'')

    def test_native_ui_preserves_all_custom_content_and_states(self):
        self.native_config()
        self.document['blocks'][0].update(title='Доступные регионы', subtitle='17 серверов',
                                          text='Произвольный текст без расчётов')
        self.document['blocks'][1].update(title='17', subtitle='Количество серверов')
        self.document['blocks'][3]['items'][0].update(state='restricted', stateText='На обслуживании')
        self.document['blocks'][3]['items'][1].update(title='Другой вариант', state='available',
                                                    stateText='Любое пояснение')
        self.document['blocks'][5].update(value=3, maximum=17, unit='count', text='Три из семнадцати')
        self.write_document()
        endpoint = '/subscription/' + TOKEN + '/app/v1/ui'
        for capability in (True, False):
            self.assert_json(self.request(endpoint, headers=self.app_headers(TOKEN, capability)), 200, self.document)
        self.assert_json(self.ui(), 200, self.document)

    def test_ui_has_no_dependencies_on_vpn_config_or_removed_profile(self):
        self.native_config()
        self.configure(PROFILE_PATH='missing-legacy-profile.json')
        self.config.write_text('invalid-private-vpn-data')
        endpoint = '/subscription/' + TOKEN + '/app/v1/ui'
        self.assert_json(self.request(endpoint, headers=self.app_headers(TOKEN)), 200, self.document)
        self.assert_json(self.ui(), 200, self.document)
        self.assertEqual(self.request(headers=self.app_headers())[0], 200)
        self.config.unlink()
        self.assert_json(self.request(endpoint, headers=self.app_headers(TOKEN)), 200, self.document)

    def test_custom_blocks_have_no_business_purpose(self):
        self.document = {'schemaVersion': 1, 'blocks': [
            {'id': 'customCard', 'type': 'card', 'title': 'Количество серверов', 'text': '17', 'buttons': []},
            {'id': 'customProgress', 'type': 'progress', 'value': 2, 'maximum': 5, 'unit': 'count'}], 'pages': []}
        self.write_document()
        self.assert_json(self.request('/subscription/' + TOKEN + '/app/v1/ui',
                                      headers=self.app_headers(TOKEN)), 200, self.document)

    def test_lan_http_uses_the_clients_existing_compatible_routes(self):
        self.native_config()
        self.configure(APP_SUBSCRIPTION_SIGNING_SECRET='synthetic-signing-secret-1234567890')
        for host in ('my-mac.local:8082', '192.0.2.10:8082'):
            response = self.request(headers={**self.app_headers(), 'Host': host})
            self.assertEqual(response[0], 200)
            self.assertIsNone(response[1]['X-Subscription-Format'])
            descriptor = json.loads(response[2])
            self.assertEqual(descriptor['ui_url'], 'http://' + host + '/v1/subscription')
            self.assertEqual(descriptor['sing_box_url'], 'http://' + host + '/subscription/' + TOKEN + '/config')
            self.assertEqual(descriptor['ui_token'], TOKEN)

    def test_transport_link_does_not_depend_on_ui_item_states(self):
        self.document['blocks'][3]['items'][1].update(state='restricted', stateText='Любой текст')
        self.write_document()
        self.assertEqual(json.loads(self.request(headers=self.app_headers())[2])['vk_turn_link'], LINK)
        self.assert_json(self.request('/subscription/' + TOKEN + '/app/v1/ui',
                                      headers=self.app_headers(TOKEN)), 200, self.document)

    def test_native_client_prefix_and_invalid_settings_are_configurable(self):
        self.configure(APP_USER_AGENT_PREFIX='LocalApp/')
        headers = self.app_headers()
        headers['User-Agent'] = 'LocalApp/2.0'
        self.assertIn('/app/v1/ui', json.loads(self.request(headers=headers)[2])['ui_url'])
        self.assertEqual(json.loads(self.request(headers=self.app_headers())[2])['ui_url'], self.base + '/v1/subscription')
        for key, value in [('VPN_CONFIG_FORMAT', 'unknown'),
                           ('SUBSCRIPTION_PATH_PREFIX', '/api/../sub'), ('APP_USER_AGENT_PREFIX', ''),
                           ('APP_SUBSCRIPTION_SIGNING_SECRET', 'short')]:
            self.write_env()
            self.configure(**{key: value})
            self.assert_json(self.request(), 503, {'error': 'subscription_unavailable'})

    def test_malformed_native_data_never_exposes_credentials(self):
        for change in ('state', 'nodes_when_restricted', 'node_port', 'node_uuid', 'duplicate_json'):
            config = self.native_config()
            if change == 'state':
                config['state'] = 'unknown'
            elif change == 'nodes_when_restricted':
                config['state'] = 'restricted'
            elif change == 'node_port':
                config['outbounds'][0]['server_port'] = True
            elif change == 'node_uuid':
                config['outbounds'][0]['uuid'] = 'private-invalid-uuid'
            self.config.write_text(json.dumps(config))
            if change == 'duplicate_json':
                self.config.write_text('{"state":"available","state":"restricted"}')
            response = self.request('/subscription/' + TOKEN + '/app/v1/config', headers=self.app_headers(TOKEN))
            self.assert_json(response, 503, {'error': 'subscription_unavailable'})
            self.assertNotIn(NODE['uuid'], response[2].decode())

    def test_native_tls_and_reasons_match_the_current_client(self):
        path = '/subscription/' + TOKEN + '/app/v1/config'
        for change in ('missing_utls', 'missing_reality', 'disabled_tls', 'unknown_fingerprint',
                       'invalid_key', 'invalid_short_id', 'empty_alpn', 'unexpected_tls', 'available_reason'):
            config = self.native_config()
            tls = config['outbounds'][0]['tls']
            if change == 'missing_utls': tls.pop('utls')
            elif change == 'missing_reality': tls.pop('reality')
            elif change == 'disabled_tls': tls['enabled'] = False
            elif change == 'unknown_fingerprint': tls['utls']['fingerprint'] = 'unknown'
            elif change == 'invalid_key': tls['reality']['public_key'] = 'private-invalid'
            elif change == 'invalid_short_id': tls['reality']['short_id'] = 'a'
            elif change == 'empty_alpn': tls['alpn'] = ['']
            elif change == 'unexpected_tls': tls['insecure'] = True
            elif change == 'available_reason': config['reason'] = 'arbitrary'
            self.config.write_text(json.dumps(config))
            with self.subTest(change=change):
                self.assert_json(self.request(path, headers=self.app_headers(TOKEN)), 503,
                                 {'error': 'subscription_unavailable'})
        config = self.native_config('unavailable')
        config.update(reason='arbitrary', outbounds=[], failover=None)
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.request(path, headers=self.app_headers(TOKEN))[0], 503)

    def test_native_uuid_format_matches_foundation(self):
        path = '/subscription/' + TOKEN + '/app/v1/config'
        for uuid in (NODE['uuid'].replace('-', ''), 'urn:uuid:' + NODE['uuid'], '{' + NODE['uuid'] + '}'):
            config = self.native_config()
            config['outbounds'][0]['uuid'] = uuid
            self.config.write_text(json.dumps(config))
            with self.subTest(uuid_format=uuid[:4]):
                self.assert_json(self.request(path, headers=self.app_headers(TOKEN)), 503,
                                 {'error': 'subscription_unavailable'})

    def test_native_nodes_require_tls_and_recover_after_correction(self):
        self.native_config()
        for value in ('missing', None, {}, {'enabled': 'true'}):
            config = self.native_config()
            if value == 'missing':
                config['outbounds'][0].pop('tls')
            else:
                config['outbounds'][0]['tls'] = value
            self.config.write_text(json.dumps(config))
            response = self.request('/subscription/' + TOKEN + '/app/v1/config', headers=self.app_headers(TOKEN))
            self.assert_json(response, 503, {'error': 'subscription_unavailable'})
            self.assert_json(self.ui(), 200, self.document)
        config = self.native_config()
        self.assert_json(self.request('/subscription/' + TOKEN + '/app/v1/config', headers=self.app_headers(TOKEN)),
                         200, config)
        self.assertEqual(self.ui()[0], 200)


class CLITests(unittest.TestCase):
    def run_cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(ROOT / 'server.py'), *args], cwd=cwd,
                              capture_output=True, text=True, timeout=5)

    def test_check_validates_local_files_without_opening_a_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'document.json').write_bytes((ROOT / 'data/subscription.example.json').read_bytes())
            (folder / 'config.json').write_text(json.dumps(NODE))
            env = folder / '.env'
            env.write_text(f'SUBSCRIPTION_TOKEN={TOKEN}\nSING_BOX_CONFIG_PATH=config.json\nUI_DOCUMENT_PATH=document.json\n')
            result = self.run_cli('--env', str(env), '--check', cwd='/')
            self.assertEqual(result.returncode, 0, result.stderr)
            (folder / 'config.json').unlink()
            result = self.run_cli('--env', str(env), '--check')
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(TOKEN, result.stderr)
            self.assertNotIn(str(folder), result.stderr)
            env.write_text(env.read_text().replace('SING_BOX_CONFIG_PATH=config.json', 'SING_BOX_CONFIG_PATH='))
            result = self.run_cli('--env', str(env), '--check')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_reports_missing_env_without_details(self):
        result = self.run_cli('--env', '/missing/private.env', '--check')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('/missing/private.env', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_check_validates_native_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'document.json').write_bytes((ROOT / 'data/subscription.example.json').read_bytes())
            (root / 'native.json').write_bytes((ROOT / 'config.failover.example.json').read_bytes())
            env = root / '.env'
            env.write_text(f'SUBSCRIPTION_TOKEN={TOKEN}\nUI_DOCUMENT_PATH=document.json\n'
                           'SING_BOX_CONFIG_PATH=native.json\n'
                           'VPN_CONFIG_FORMAT=vless-failover\n')
            result = self.run_cli('--env', str(env), '--check', cwd='/')
            self.assertEqual(result.returncode, 0, result.stderr)
            (root / 'native.json').write_text('{"kind":"private-invalid"}')
            result = self.run_cli('--env', str(env), '--check')
            self.assertEqual(result.returncode, 1)
            self.assertNotIn('private-invalid', result.stderr)


if __name__ == '__main__':
    unittest.main()
