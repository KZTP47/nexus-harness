import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.email_studio import EmailStudio
from our_harness.models import HarnessError


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mail-model-choice-')
        self.addCleanup(self.temp.cleanup)
        data = copy.deepcopy(DEFAULT_CONFIG)
        data['provider'].update(name='codex-cli', model='old-default', command=['codex'])
        self.config = LoadedConfig(data, Path(self.temp.name), [], {})
        self.requests = []
        def create(config):
            model = config.get('provider.model')
            def complete(request):
                self.requests.append((model, request.model))
                return SimpleNamespace(text='A model-specific reply.')
            return SimpleNamespace(effective_dispatch_fingerprint=lambda:'fp:'+model, complete=complete)
        provider = patch('our_harness.email_studio.create_provider', side_effect=create)
        provider.start(); self.addCleanup(provider.stop)
        catalog = patch('our_harness.email_models.model_options', return_value=[{'id':'new-supported-model','label':'New model','source':'fixture'}])
        catalog.start(); self.addCleanup(catalog.stop)
        self.studio = EmailStudio(self.config)

    def create_draft(self):
        account = self.studio.dispatch('account_save', {'email':'writer@example.test', 'provider_route':'default', 'provider_model':'new-supported-model'})['account']
        message = self.studio.dispatch('import', {'account_id':account['id'], 'sender':'reader@example.test','subject':'A question','body':'Please answer.'})['message']
        draft = self.studio.dispatch('create_draft', {'account_id':account['id'], 'message_id':message['id']})['draft']
        return account, draft

    def test_exact_selected_model_persists_and_drives_drafting_and_reflection(self):
        account, draft = self.create_draft()
        self.studio = EmailStudio(self.config)
        self.assertEqual(self.studio.snapshot()['accounts'][0]['provider_model'], 'new-supported-model')
        draft = self.studio.process_draft(draft['id'])['draft']
        self.studio.dispatch('approve_draft', {'account_id':account['id'], 'draft_id':draft['id'], 'revision':draft['revision'], 'text':'A shorter edited reply.', 'learn':True})
        self.studio.finalize_draft(draft['id'])
        self.assertEqual(self.requests, [('new-supported-model','new-supported-model')]*2)
        self.assertEqual(self.config.get('provider.model'), 'old-default')

    def test_unknown_model_is_rejected_before_account_or_provider_effect(self):
        with self.assertRaises(HarnessError):
            self.studio.dispatch('account_save', {'email':'writer@example.test','provider_route':'default','provider_model':'made-up-model'})
        self.assertEqual(self.studio.snapshot()['accounts'], [])
        self.assertEqual(self.requests, [])

    def test_changed_route_default_does_not_replace_explicit_draft_model(self):
        _, draft = self.create_draft()
        self.config.data['provider']['model'] = 'another-default'
        self.studio.process_draft(draft['id'])
        self.assertEqual(self.requests, [('new-supported-model','new-supported-model')])

    def test_mutated_draft_model_is_not_silently_used_under_old_fingerprint(self):
        _, draft = self.create_draft()
        draft['provider_model'] = 'different-model'
        self.studio._put('draft', draft)
        with self.assertRaises(HarnessError):
            self.studio.process_draft(draft['id'])
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
