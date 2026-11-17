"""Exercise the server with the current application's actual Swift importer."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from server import create_server


ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT.parent / 'sing-prototype'
TOKEN = 'synthetic-client-compatibility-token'
LINK = 'wdtt://example.org:443:unused:0:synthetic-password:synthetic-call'


@unittest.skipUnless(shutil.which('xcrun') and (CLIENT / 'PrototypeApp/Subscription.swift').exists(),
                     'Swift compatibility checks require Xcode and the sibling sing-prototype project')
class ClientCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.compiler_directory.cleanup)
        folder = Path(cls.compiler_directory.name)
        source = folder / 'check.swift'
        sources = ['Library/Network/VKTurnConfiguration.swift',
                   'Library/Network/SubscriptionDeviceIdentity.swift',
                   'Library/Network/SubscriptionDeviceAccess.swift',
                   'PrototypeApp/SubscriptionUIDocument.swift',
                   'PrototypeApp/SubscriptionLoader.swift',
                   'PrototypeApp/Subscription.swift']
        source.write_text('\n'.join((CLIENT / path).read_text() for path in sources)
                          .replace('import Library\n', '') + '''
@main struct Checks {
    static func main() async {
        do {
            let snapshot = try await PrototypeSubscriptionLoader.download(CommandLine.arguments[1], deviceHeaders: [
                "x-hwid": String(repeating: "a", count: 64), "x-device-os": "iOS",
                "x-ver-os": "test", "x-device-model": "synthetic-client"
            ])
            let result: [String: Any] = [
                "complete": snapshot.isComplete,
                "vless": snapshot.isAvailable(optionID: "option01"),
                "vk": snapshot.isAvailable(optionID: "option02"),
                "warnings": snapshot.warnings.count,
                "title": snapshot.uiDocument?.block("block02")?.title ?? "",
                "subtitle": snapshot.uiDocument?.block("block02")?.subtitle ?? "",
                "nodes": snapshot.vlessProvisioning?.outbounds.count ?? 0
            ]
            print(String(decoding: try JSONSerialization.data(withJSONObject: result), as: UTF8.self))
        } catch {
            print("Client rejected the subscription")
            exit(1)
        }
    }
}
''')
        cls.executable = folder / 'check'
        result = subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-module-cache-path',
                                 str(folder / 'modules'), str(source), '-o', str(cls.executable)],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        (self.folder / 'ui.json').write_bytes((ROOT / 'data/subscription.example.json').read_bytes())
        self.env = self.folder / '.env'

    def download(self, config_name, mode, secret=''):
        if config_name:
            (self.folder / 'vpn.json').write_bytes((ROOT / config_name).read_bytes())
        self.env.write_text(f'SUBSCRIPTION_TOKEN={TOKEN}\nUI_DOCUMENT_PATH=ui.json\n'
                            f'SING_BOX_CONFIG_PATH={"vpn.json" if config_name else ""}\n'
                            f'VPN_CONFIG_FORMAT={mode}\nVK_TURN_LINK={LINK}\n'
                            f'APP_SUBSCRIPTION_SIGNING_SECRET={secret}\n')
        httpd = create_server(self.env, host='127.0.0.1', port=0)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        try:
            result = subprocess.run([str(self.executable),
                                     f'http://127.0.0.1:{httpd.server_port}/subscription/{TOKEN}'],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_current_client_downloads_raw_config_and_arbitrary_value_row(self):
        result = self.download('config.example.json', 'sing-box')
        self.assertEqual(result, {'complete': True, 'vless': True, 'vk': True, 'warnings': 0,
                                  'title': '17', 'subtitle': 'Количество серверов', 'nodes': 0})

    def test_current_client_downloads_four_nodes_with_signed_bearer(self):
        result = self.download('config.failover.example.json', 'vless-failover',
                               secret='synthetic-client-compatibility-secret-123456')
        self.assertTrue(result['complete'])
        self.assertTrue(result['vless'])
        self.assertTrue(result['vk'])
        self.assertEqual(result['warnings'], 0)
        self.assertEqual(result['nodes'], 4)

    def test_current_client_downloads_ui_with_vless_disabled(self):
        result = self.download(None, 'sing-box')
        self.assertTrue(result['complete'])
        self.assertFalse(result['vless'])
        self.assertTrue(result['vk'])
        self.assertEqual(result['warnings'], 0)
