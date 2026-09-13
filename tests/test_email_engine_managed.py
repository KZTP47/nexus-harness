import hashlib
import io
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from our_harness.email_engine_managed import ManagedEmailEngine, _ArtifactRedirect, _download, _extract
from our_harness.models import HarnessError
from tests.test_email_engine_workspace import TestSecrets


class ManagedMailTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='independent-mail-root-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_download_verifies_size_hash_and_preserves_good_cache(self):
        payload = b'synthetic executable'
        spec = {'url': 'https://github.com/example/release', 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}
        target = self.root / 'runtime.exe'
        opener = Mock()
        opener.open.return_value = io.BytesIO(payload)
        with patch('our_harness.email_engine_managed.urllib.request.build_opener', return_value=opener):
            self.assertEqual(_download(spec, target).read_bytes(), payload)
            _download(spec, target)
        self.assertEqual(opener.open.call_count, 1)
        target.write_bytes(b'previous retained artifact')
        opener.open.return_value = io.BytesIO(payload + b'too large')
        with patch('our_harness.email_engine_managed.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(HarnessError):
                _download(spec, target)
        self.assertEqual(target.read_bytes(), b'previous retained artifact')
        self.assertFalse(target.with_suffix('.exe.download').exists())
        opener.open.return_value = io.BytesIO(b'x' * len(payload))
        with patch('our_harness.email_engine_managed.urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(HarnessError, 'checksum'):
                _download(spec, target)

    def archive(self, entry, content=b'harmless', mode=None):
        archive = self.root / 'runtime.zip'
        with zipfile.ZipFile(archive, 'w') as stream:
            item = zipfile.ZipInfo(entry)
            if mode:
                item.external_attr = mode << 16
            stream.writestr(item, content)
        return archive

    def test_archive_rejects_traversal_ads_links_and_reserved_names(self):
        for name in ('../outside', '/outside', 'C:/outside', 'safe/file:stream', 'safe/CON.exe', 'safe/../outside'):
            with self.subTest(name=name), self.assertRaises(HarnessError):
                _extract(self.archive(name), self.root / 'install')
        with self.assertRaises(HarnessError):
            _extract(self.archive('link', mode=stat.S_IFLNK | 0o777), self.root / 'install')
        self.assertFalse((self.root / 'outside').exists())
        _extract(self.archive('distribution/redis-server.exe'), self.root / 'install')
        self.assertEqual((self.root / 'install/distribution/redis-server.exe').read_bytes(), b'harmless')

    def test_archive_rejects_unpacked_bomb_before_writing(self):
        entry = zipfile.ZipInfo('oversized.exe')
        entry.file_size = 151 * 1024 * 1024
        archive = Mock()
        archive.__enter__ = Mock(return_value=archive)
        archive.__exit__ = Mock(return_value=False)
        archive.infolist.return_value = [entry]
        with patch('our_harness.email_engine_managed.zipfile.ZipFile', return_value=archive):
            with self.assertRaises(HarnessError):
                _extract(self.root / 'synthetic.zip', self.root / 'unpack')
        archive.open.assert_not_called()

    def test_download_rejects_untrusted_redirect_or_credentials(self):
        handler = _ArtifactRedirect()
        for target in ('http://github.com/release', 'https://attacker.example.test/release', 'https://secret@github.com/release'):
            with self.subTest(target=target), self.assertRaises(HarnessError):
                handler.redirect_request(None, None, 302, '', {}, target)

    def test_redis_secret_uses_stdin_not_command_line_with_durable_loopback_config(self):
        manager = ManagedEmailEngine(self.root, TestSecrets())
        manager.root.mkdir()
        settings = manager._load()
        process, tree = Mock(), Mock()
        process.poll.return_value = 0
        def contained(first, second, **kwargs):
            return first(), tree
        with patch('our_harness.email_engine_managed.subprocess.Popen', return_value=process) as launch, patch('our_harness.execution._start_windows_contained_process', side_effect=contained, create=True):
            manager._start_redis(self.root / 'redis.exe', settings)
        arguments = launch.call_args.args[0]
        self.assertEqual(arguments, [str(self.root / 'redis.exe'), '-'])
        self.assertNotIn(settings['redis_password'], str(arguments))
        config = process.stdin.write.call_args.args[0].decode()
        for required in ('bind 127.0.0.1', 'appendonly yes', 'appendfsync always', 'maxmemory-policy noeviction', 'requirepass ' + settings['redis_password']):
            self.assertIn(required, config)
        self.assertFalse((manager.root / 'redis-data/redis.conf').exists())
        manager.close()

    def test_private_settings_persist_ports_and_secrets_without_plaintext(self):
        store = TestSecrets()
        manager = ManagedEmailEngine(self.root, store)
        manager.root.mkdir()
        settings = manager._load()
        persisted = (manager.root / 'settings.json').read_text(encoding='utf-8')
        self.assertNotIn(settings['redis_password'], persisted)
        self.assertNotIn(settings['secret'], persisted)
        self.assertEqual(ManagedEmailEngine(self.root, store)._load(), settings)
        self.assertNotEqual(settings['redis_port'], settings['port'])
        self.assertNotIn(settings['secret'], json.dumps(manager.status()))
        moved = ManagedEmailEngine(self.root / 'relocated', store)
        moved.root.mkdir(parents=True)
        (moved.root / 'settings.json').write_text(persisted, encoding='utf-8')
        with self.assertRaisesRegex(HarnessError, 'folder or runtime contract changed'):
            moved._load()
        document = json.loads(persisted)
        document['contract'] = 'changed-contract'
        (manager.root / 'settings.json').write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaises(HarnessError):
            manager._load()

    def test_owned_lifecycle_restart_reuses_token_and_config_and_closes_children(self):
        manager = ManagedEmailEngine(self.root, TestSecrets())
        self.addCleanup(manager.close)
        redis = Mock()
        redis.poll.return_value = None
        runtime = Mock()
        runtime.fingerprint = 'synthetic-fingerprint'
        runtime.start.return_value = {'url': 'http://127.0.0.1:12345'}
        runtime.status.return_value = {'url': 'http://127.0.0.1:12345', 'ready': True}
        def spawn(executable, settings):
            manager._redis = redis
        with patch.object(manager, '_acquire'), patch.object(manager, '_provision', return_value=(self.root / 'ee.exe', self.root / 'redis.exe')), patch.object(manager, '_start_redis', side_effect=spawn), patch.object(manager, '_issue_token', return_value='a' * 64) as issue, patch('our_harness.email_engine_managed.redis_preflight'), patch('our_harness.email_engine_managed.EmailEngineRuntime', return_value=runtime), patch('our_harness.email_engine.EmailEngineClient') as client:
            client.return_value._request.return_value = {'updated': ['serviceUrl']}
            first = manager.start()
            settings = dict(manager._settings)
            self.assertEqual(first['token'], 'a' * 64)
            self.assertNotIn('token', manager.status())
            manager.close()
            runtime.close.assert_called()
            redis.terminate.assert_called_once()
            second = manager.start()
            self.assertEqual(second, first)
            self.assertEqual(manager._settings, settings)
            issue.assert_called_once()
            self.assertEqual(client.return_value._request.call_count, 2)
            client.return_value._request.assert_called_with('POST', '/v1/settings', payload={'serviceUrl': 'http://127.0.0.1:12345'})

    def test_hosted_form_setup_only_configures_owned_loopback_origin(self):
        with patch('our_harness.email_engine.EmailEngineClient') as client:
            for url in ('https://remote.example.test', 'http://127.0.0.1:12345/path', 'http://127.0.0.1:12345?token=secret'):
                with self.subTest(url=url), self.assertRaises(HarnessError):
                    ManagedEmailEngine._configure_hosted_authentication(url, 'synthetic-token')
            client.assert_not_called()
            client.return_value._request.return_value = {'updated': []}
            with self.assertRaisesRegex(HarnessError, 'sign-in address'):
                ManagedEmailEngine._configure_hosted_authentication('http://127.0.0.1:12345', 'synthetic-token')

    def test_start_failure_cleans_owned_process_and_retains_durable_settings(self):
        manager = ManagedEmailEngine(self.root, TestSecrets())
        redis = Mock()
        redis.poll.return_value = None
        def fail(*args):
            manager._redis = redis
            raise RuntimeError('Synthetic private failure')
        with patch.object(manager, '_acquire'), patch.object(manager, '_provision', return_value=(self.root / 'ee.exe', self.root / 'redis.exe')), patch.object(manager, '_start_redis', side_effect=fail):
            with self.assertRaises(HarnessError):
                manager.start()
        redis.terminate.assert_called_once()
        self.assertTrue((manager.root / 'settings.json').is_file())
        self.assertNotIn('Synthetic private failure', manager.status()['message'])
        self.assertFalse(manager.status()['ready'])

    def test_preparing_status_does_not_wait_for_runtime_health(self):
        manager = ManagedEmailEngine(self.root, TestSecrets())
        manager._starting = True
        manager._runtime = Mock()
        self.assertEqual(manager.status()['state'], 'preparing')
        manager._runtime.status.assert_not_called()


if __name__ == '__main__':
    unittest.main()
