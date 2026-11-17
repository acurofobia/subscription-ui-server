"""Load synthetic subscriptions with the sibling Android project's real repository."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from server import create_server


ROOT = Path(__file__).resolve().parents[1]
CLIENT = Path(os.environ.get('ANDROID_CLIENT_PATH', str(ROOT.parent / 'android-client'))).resolve()
WRAPPER = CLIENT / ('gradlew.bat' if os.name == 'nt' else 'gradlew')
TOKEN = 'synthetic-android-compatibility-token'
LINK = 'wdtt://example.org:443:unused:0:synthetic-password:synthetic-call'


@unittest.skipUnless(shutil.which('java') and shutil.which('javac') and WRAPPER.is_file(),
                     'Android compatibility checks require JDK 17+, Android SDK and the sibling android-client')
class AndroidCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.compiler_folder = Path(directory.name)
        report = cls.compiler_folder / 'classpath.txt'
        init = cls.compiler_folder / 'compatibility.gradle'
        init.write_text('''gradle.projectsEvaluated {
    def app = gradle.rootProject.project(':app')
    app.tasks.register('exportSubscriptionCompatibilityClasspath') {
        dependsOn app.tasks.named('compileDebugUnitTestKotlin')
        doLast {
            new File(System.getProperty('subscription.compatibility.output')).text =
                app.tasks.named('testDebugUnitTest').get().classpath.asPath
        }
    }
}
''')
        env = os.environ.copy()
        cache = CLIENT / '.tools/gradle-home'
        if 'GRADLE_USER_HOME' not in env and cache.is_dir():
            env['GRADLE_USER_HOME'] = str(cache)
        result = subprocess.run([str(WRAPPER), '--offline', '--console=plain', '--init-script', str(init),
                                 '-Dsubscription.compatibility.output=' + str(report),
                                 ':app:exportSubscriptionCompatibilityClasspath'],
                                cwd=CLIENT, env=env, capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.classpath = os.pathsep.join((str(cls.compiler_folder), report.read_text()))
        result = subprocess.run(['javac', '--release', '17', '-cp', cls.classpath, '-d', str(cls.compiler_folder),
                                 str(ROOT / 'tests/android/SubscriptionCheck.java')],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.env = self.folder / '.env'
        (self.folder / 'ui.json').write_bytes((ROOT / 'data/subscription.example.json').read_bytes())

    def download(self, config='config.example.json', mode='sing-box', devices=False):
        if config:
            (self.folder / 'vpn.json').write_bytes((ROOT / config).read_bytes())
        if devices:
            (self.folder / 'devices.json').write_bytes((ROOT / 'data/device-access.example.json').read_bytes())
        self.env.write_text(f'SUBSCRIPTION_TOKEN={TOKEN}\nUI_DOCUMENT_PATH=ui.json\n'
                            f'SING_BOX_CONFIG_PATH={"vpn.json" if config else ""}\n'
                            f'VPN_CONFIG_FORMAT={mode}\nVK_TURN_LINK={LINK}\n'
                            'APP_SUBSCRIPTION_SIGNING_SECRET=synthetic-android-signing-secret-123456789\n'
                            f'DEVICE_ACCESS_PATH={"devices.json" if devices else ""}\n')
        httpd = create_server(self.env, host='127.0.0.1', port=0)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        try:
            args = [f'http://127.0.0.1:{httpd.server_port}/subscription/{TOKEN}']
            if devices:
                args += [str(self.folder / 'devices.json'), str(self.folder / 'ui.json')]
            result = subprocess.run(['java', '-cp', self.classpath, 'SubscriptionCheck', *args],
                                    capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_android_loads_raw_vless_turn_signed_ui_and_authored_content(self):
        result, = self.download()
        self.assertFalse(result['failed'])
        self.assertTrue(result['complete'])
        self.assertTrue(result['vless'])
        self.assertTrue(result['turn'])
        self.assertEqual(result['warnings'], 0)
        self.assertEqual(result['title'], '17')

    def test_android_loads_native_four_node_failover(self):
        result, = self.download('config.failover.example.json', 'vless-failover')
        self.assertFalse(result['failed'])
        self.assertTrue(result['complete'])
        self.assertTrue(result['vless'])
        self.assertTrue(result['turn'])
        self.assertEqual(result['nodes'], 4)

    def test_android_loads_ui_and_turn_without_vless(self):
        result, = self.download(config=None)
        self.assertFalse(result['failed'])
        self.assertTrue(result['complete'])
        self.assertFalse(result['vless'])
        self.assertTrue(result['turn'])

    def test_android_persists_deny_before_ui_failure_and_requires_newer_allow(self):
        results = self.download(devices=True)
        self.assertEqual([item['turn'] for item in results], [True, False, False, True])
        self.assertEqual([item['revision'] for item in results], [1, 2, 2, 3])
        self.assertEqual([item['failed'] for item in results], [False, True, False, False])
        self.assertEqual(len({item['hwid'] for item in results}), 1)
        self.assertTrue(results[-1]['vless'])
