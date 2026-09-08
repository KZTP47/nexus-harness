from unittest import TestCase, mock
from tests import test_long_horizon as fixtures
from our_harness import long_horizon


class SameProjectCompletionTests(TestCase):
    def setUp(self):
        fixtures.LongHorizonTests.setUp(self)
        self.store = long_horizon.GoalStore(self.config)

    def completed_tasks(self):
        goal = self.store.create(self.board, 'project', ['Create a document'], 'completion-boundary',
                                 conversation_id='completion-chat', isolated_workspace=True)
        def ready(document, _db):
            document['success_criteria'] = ['Every required task is complete']
            for task in document['tasks']:
                task['state'] = 'complete'
        self.store._mutate(goal['goal_id'], ready)
        return goal['goal_id']

    def test_pause_prevents_publication_even_if_verifier_loaded_the_paused_revision(self):
        goal_id = self.completed_tasks()
        paused = self.store.control(goal_id, 'pause')
        publish = mock.Mock(return_value={'changes': []})
        result = self.store.complete_verification(goal_id, {'status': 'passed'},
            expected_revision=paused['revision'], publish_workspace=publish)
        self.assertEqual(result['status'], 'paused')
        publish.assert_not_called()
        # Positive control: the same valid task result may publish after explicit Resume.
        self.store.control(goal_id, 'resume')
        resumed = self.store.get(goal_id)
        result = self.store.complete_verification(goal_id, {'status': 'passed'},
            expected_revision=resumed['revision'], publish_workspace=publish)
        self.assertEqual(result['status'], 'complete')
        publish.assert_called_once()

    def test_reconciliation_required_without_pending_action_prevents_legacy_adoption(self):
        goal = self.store.create(self.board, 'project', ['Earlier task'], 'unsettled-adoption',
                                 conversation_id='unsettled-chat')
        def unsettled(document, _db):
            document['status'] = 'paused'
            task = document['tasks'][0]
            task.update(state='blocked', provider_effect_state='reply_received_reconciliation_required',
                        reconciliation_required=True, outcome_unknown=False, pending_action={}, pending_transaction={})
        self.store._mutate(goal['goal_id'], unsettled)
        result = self.store.adopt_isolated_workspace(goal['goal_id'])
        self.assertFalse(result.get('execution_workspace'))
        self.assertEqual(result['tasks'][0]['provider_effect_state'], 'reply_received_reconciliation_required')

    def test_verification_cannot_publish_an_unsettled_effect(self):
        goal_id = self.completed_tasks()
        def unsettle(document, _db):
            document['tasks'][0]['reconciliation_required'] = True
        self.store._mutate(goal_id, unsettle)
        publish = mock.Mock(return_value={'changes': []})
        with self.assertRaisesRegex(long_horizon.HarnessError, 'Unsettled'):
            self.store.complete_verification(goal_id, {'status': 'passed'}, publish_workspace=publish)
        publish.assert_not_called()
