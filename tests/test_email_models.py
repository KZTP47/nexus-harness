import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness import email_models as models


class EmailModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='portable-email-models-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'arbitrary-codex.exe'
        self.binary.write_bytes(b'fake binary')
        data = copy.deepcopy(DEFAULT_CONFIG)
        data['provider'].update(name='codex-cli', model='configured-model', command=[str(self.binary)])
        self.config = LoadedConfig(data, self.root, [], {})
        models._CACHE.clear()
        self.resolve = patch.object(models, 'available', return_value=str(self.binary)).start()
        self.addCleanup(patch.stopall)

    def result(self, rows=None, **kwargs):
        return SimpleNamespace(stdout=json.dumps({'models': rows or [dict(slug='new-model', display_name='New', visibility='list'), dict(slug='internal', visibility='hide')]}),
                               timed_out=kwargs.get('timed_out', False), output_truncated=kwargs.get('output_truncated', False), exit_code=kwargs.get('exit_code', 0))

    def test_snapshot_does_not_execute(self):
        with patch.object(models, '_run_bounded') as run:
            options = models.model_options(self.config, 'default')
        run.assert_not_called()
        self.assertIn('gpt-6-astra', {item['id'] for item in options})
        self.assertIn('configured-model', {item['id'] for item in options})

    def test_refresh_filters_hidden_and_cache_is_copy(self):
        with patch.object(models, '_run_bounded', return_value=self.result()) as run:
            result = models.model_options(self.config, 'default', True)
            self.assertEqual(['new-model', 'configured-model'], [item['id'] for item in result])
            self.assertEqual(run.call_args.args[0], [str(self.binary), 'debug', 'models'])
            self.assertLessEqual(run.call_args.kwargs['timeout_seconds'], 8)
            self.assertNotEqual(run.call_args.kwargs['cwd'], self.root)
            result[0]['label'] = 'tampered'
            self.assertEqual('New', models.model_options(self.config, 'default')[0]['label'])
            self.assertEqual(1, run.call_count)

    def test_changed_binary_and_model_invalidate(self):
        with patch.object(models, '_run_bounded', return_value=self.result()):
            models.model_options(self.config, 'default', True)
        self.binary.write_bytes(b'new binary version')
        self.assertNotIn('new-model', {item['id'] for item in models.model_options(self.config, 'default')})
        self.config.data['provider']['model'] = 'changed-model'
        self.assertIn('changed-model', {item['id'] for item in models.model_options(self.config, 'default')})

    def test_expiry_and_restart_fallback(self):
        with patch.object(models, '_run_bounded', return_value=self.result()):
            models.model_options(self.config, 'default', True)
        with patch.object(models.time, 'monotonic', return_value=10**12):
            self.assertNotIn('new-model', {item['id'] for item in models.model_options(self.config, 'default')})
        models._CACHE.clear()
        self.assertNotIn('new-model', {item['id'] for item in models.model_options(self.config, 'default')})

    def test_refresh_failure_uses_bundled(self):
        with patch.object(models, '_run_bounded', side_effect=[self.result(timed_out=True), self.result()]) as run:
            result = models.model_options(self.config, 'default', True)
        self.assertEqual('Installed Codex bundled catalog', result[0]['source'])
        self.assertEqual('--bundled', run.call_args.args[0][-1])

    def test_malformed_truncated_and_missing_cli_fallback(self):
        bad = self.result()
        bad.stdout = 'not json'
        with patch.object(models, '_run_bounded', side_effect=[bad, self.result(output_truncated=True)]):
            self.assertIn('gpt-6-astra', {item['id'] for item in models.model_options(self.config, 'default', True)})
        self.resolve.return_value = ''
        with patch.object(models, '_run_bounded') as run:
            models.model_options(self.config, 'default', True)
        run.assert_not_called()

    def test_untrusted_route_never_executes(self):
        self.config.provenance['provider.command'] = str((self.root / '.harness/config.json').resolve())
        with patch.object(models, 'is_project_shared_config_trusted', return_value=False), patch.object(models, '_run_bounded') as run:
            models.model_options(self.config, 'default', True)
        run.assert_not_called()

    def test_claude_latest_and_aliases_without_commands(self):
        self.config.data['provider']['name'] = 'claude-cli'
        with patch.object(models, '_run_bounded') as run:
            result = models.model_options(self.config, 'default', True)
        run.assert_not_called()
        values = {item['id'] for item in result}
        self.assertTrue({'claude-fable-5-1', 'claude-fable-5', 'claude-opus-5', 'claude-sonnet-5', 'opus', 'sonnet', 'haiku', 'default'} <= values)


if __name__ == '__main__':
    unittest.main()
