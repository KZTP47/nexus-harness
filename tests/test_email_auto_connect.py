from __future__ import annotations
import copy
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.email_onboarding import EmailOnboarding, load_registrations
from our_harness.models import HarnessError

MS_ID='11111111-2222-3333-4444-555555555555'
MS_OTHER='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
GOOGLE_ID='1234567890-nexusdesktop.apps.googleusercontent.com'

class Secrets:
    def protect(self,value):return value[::-1]
    def unprotect(self,value):return value[::-1]

class AutoConnectTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mail-autoconnect-')
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.config=LoadedConfig(copy.deepcopy(DEFAULT_CONFIG),root,[],{})
        self.connector=Mock()
        self.connector.begin.return_value={'session_id':'pending-id','authorization_url':'https://login.microsoftonline.com/authorize'}
        self.connector.status.return_value={'state':'pending','connection':None,'error':''}
        self.connector.registration_status.return_value=[{'provider':'outlook','configured':True},{'provider':'gmail','configured':False}]
        self.studio=SimpleNamespace(config=self.config,data_dir=root,secrets=Secrets(),connectors=self.connector,_mutation=nullcontext,_assistant_settings=Mock())
        self.onboarding=EmailOnboarding(self.studio)
        self.defaults={'outlook':{'client_id':'','tenant':'common'},'gmail':{'client_id':''}}
        patcher=patch('our_harness.email_oauth_defaults.PUBLISHER_REGISTRATIONS',self.defaults)
        patcher.start();self.addCleanup(patcher.stop)

    def override(self,value):
        path=self.studio.data_dir/'oauth-registration.json'
        path.write_text(json.dumps({'schema_version':1,'encrypted':self.studio.secrets.protect(json.dumps(value))}))

    def test_missing_registration_explains_publisher_boundary_without_signin(self):
        result=self.onboarding.auto_connect({'provider':'outlook'})
        self.assertEqual(result['state'],'publisher_registration_required')
        self.assertEqual(result['registration']['client_id'],'')
        self.assertIn('publisher',result['message'])
        self.assertIn('Manual mailbox setup',result['message'])
        self.connector.begin.assert_not_called()
        self.connector.update_registrations.assert_not_called()

    def test_publisher_id_detected_and_auth_requires_user_consent(self):
        self.defaults['outlook']['client_id']=MS_ID
        result=self.onboarding.auto_connect({'provider':'outlook','provider_route':'claude-local','provider_model':'selected-model'})
        self.assertEqual(result['state'],'authorization_pending')
        self.assertEqual(result['registration'],{'provider':'outlook','source':'publisher','client_id':MS_ID,'tenant':'common'})
        self.assertEqual(result['request_id'],'pending-id')
        self.assertIn('consent',result['message'])
        self.connector.begin.assert_called_once_with('outlook',account_id='')
        self.assertEqual(self.onboarding.pending['pending-id']['settings']['provider_model'],'selected-model')
        self.assertFalse((self.studio.data_dir/'oauth-registration.json').exists())

    def test_local_override_wins_project_then_publisher(self):
        self.defaults['outlook']['client_id']=MS_ID
        self.config.data['email_oauth']={'clients':{'outlook':{'client_id':MS_OTHER}}}
        self.assertEqual(self.onboarding.auto_connect({'provider':'outlook'})['registration']['source'],'project_config')
        self.override({'outlook':{'client_id':MS_ID}})
        result=self.onboarding.auto_connect({'provider':'outlook'})
        self.assertEqual(result['registration']['source'],'local_override')
        self.assertEqual(result['registration']['client_id'],MS_ID)

    def test_partial_override_inherits_id_without_mislabeling_identity_source(self):
        self.defaults['outlook']={'client_id':MS_ID,'tenant':'common'}
        self.override({'outlook':{'tenant':'common'}})
        result=self.onboarding.auto_connect({'provider':'outlook'})
        self.assertEqual(result['registration']['source'],'publisher')
        self.assertEqual(result['registration']['client_id'],MS_ID)

    def test_google_secret_never_public_and_id_change_drops_inherited_secret(self):
        self.defaults['gmail']={'client_id':GOOGLE_ID,'client_secret':'DO_NOT_DISPLAY'}
        self.connector.begin.return_value={'session_id':'google-pending','authorization_url':'https://accounts.google.com/authorize'}
        result=self.onboarding.auto_connect({'provider':'gmail'})
        self.assertEqual(result['registration']['client_id'],GOOGLE_ID)
        self.assertNotIn('DO_NOT_DISPLAY',json.dumps(result))
        self.assertNotIn('client_secret',json.dumps(self.onboarding.snapshot()))
        self.override({'gmail':{'client_id':'987654321-other.apps.googleusercontent.com'}})
        self.assertNotIn('client_secret',load_registrations(self.studio)['gmail'])

    def test_snapshot_returns_autofill_public_fields_only(self):
        self.defaults['outlook']['client_id']=MS_ID
        state=self.onboarding.snapshot()
        self.assertEqual(state['outlook']['client_id'],MS_ID)
        self.assertEqual(state['outlook']['tenant'],'common')
        self.assertEqual(state['outlook']['registration_source'],'publisher')

    def test_unrelated_manual_provider_configuration_does_not_freeze_defaults(self):
        self.defaults['outlook']['client_id']=MS_ID
        self.onboarding.configure({'provider':'gmail','client_id':GOOGLE_ID})
        envelope=json.loads((self.studio.data_dir/'oauth-registration.json').read_text())
        override=json.loads(self.studio.secrets.unprotect(envelope['encrypted']))
        self.assertEqual(set(override),{'gmail'})
        self.defaults['outlook']['client_id']=MS_OTHER
        self.assertEqual(load_registrations(self.studio)['outlook']['client_id'],MS_OTHER)

    def test_invalid_id_is_not_claimed_as_detected_ready(self):
        for provider,client_id in [('outlook','someone@example.test'),('gmail',MS_ID)]:
            with self.subTest(provider=provider):
                self.defaults[provider]['client_id']=client_id
                result=self.onboarding.auto_connect({'provider':provider})
                self.assertEqual(result['state'],'publisher_registration_required')
                self.assertIn('does not match',result['message'])
        self.connector.begin.assert_not_called()

    def test_corrupt_override_fails_closed_not_fallback_to_other_registration(self):
        self.defaults['outlook']['client_id']=MS_ID
        (self.studio.data_dir/'oauth-registration.json').write_text('not-json')
        with self.assertRaises(HarnessError):self.onboarding.auto_connect({'provider':'outlook'})
        self.connector.begin.assert_not_called()

    def test_auto_connect_never_creates_account_before_completed_authorization(self):
        self.defaults['outlook']['client_id']=MS_ID
        self.onboarding.auto_connect({'provider':'outlook'})
        state=self.onboarding.snapshot()
        self.assertEqual(state['pending'][0]['state'],'pending')
        self.assertEqual(state['pending'][0]['account_id'],'')
        self.assertFalse(hasattr(self.studio,'connect_account'))

if __name__=='__main__':unittest.main()
