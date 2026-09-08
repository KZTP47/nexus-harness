"""Startup recovery never holds the server lock required by its scheduler."""
from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from our_harness import server
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class LongHorizonInitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.panel = server.HarnessHTTPServer(('127.0.0.1', 0), self.config)
        self.addCleanup(self.panel.server_close)
        self.panel._swarm_runs = SimpleNamespace(active_runs=lambda: [])
        self.panel._swarm_goal_queue = SimpleNamespace(active_project_paths=lambda: [])

    def run_async(self, function):
        result, done = {}, threading.Event()
        def work():
            try:
                result['value'] = function()
            except BaseException as error:
                result['error'] = error
            finally:
                done.set()
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        return result, done

    def factory(self, runtime):
        return mock.patch.object(server, '_long_horizon_module', return_value=SimpleNamespace(
            LongHorizonRuntime=mock.Mock(return_value=runtime),
        ))

    def test_recovery_scheduler_can_reenter_server_authority_without_deadlock(self):
        panel = self.panel
        acquired = threading.Event()
        failures = []
        class Runtime:
            workers = {}
            lock = threading.Lock()
            def recover_all(inner):
                def watcher():
                    with inner.lock:
                        acquired.set()
                        try:
                            panel.legacy_project_conflicts(panel.config.project_root)
                        except BaseException as error:
                            failures.append(error)
                inner.watcher = threading.Thread(target=watcher, daemon=True)
                inner.watcher.start()
                assert acquired.wait(1), 'Watcher did not start'
                if not inner.lock.acquire(timeout=1):
                    raise AssertionError('Recovery and scheduler deadlocked on server authority')
                inner.lock.release()
            def close(inner):
                if hasattr(inner, 'watcher'):
                    inner.watcher.join(2)
        runtime = Runtime()
        with self.factory(runtime):
            self.assertIs(panel.long_horizon, runtime)
        runtime.close()
        self.assertEqual(failures, [])
        self.assertFalse(runtime.watcher.is_alive())

    def test_parallel_callers_get_one_fully_recovered_runtime(self):
        entered, release = threading.Event(), threading.Event()
        runtime = SimpleNamespace(workers={},close=mock.Mock())
        def recover():
            entered.set()
            assert release.wait(3)
        runtime.recover_all = mock.Mock(side_effect=recover)
        with self.factory(runtime) as module:
            first, first_done = self.run_async(lambda: self.panel.long_horizon)
            self.assertTrue(entered.wait(1))
            second, second_done = self.run_async(lambda: self.panel.long_horizon)
            try:
                self.assertIsNone(self.panel._long_horizon, 'Partial recovery was published')
                self.assertFalse(second_done.wait(.05))
                self.assertTrue(self.panel.authority_lock.acquire(timeout=.2), 'Recovery monopolized board authority')
                self.panel.authority_lock.release()
            finally:
                release.set()
            self.assertTrue(first_done.wait(2))
            self.assertTrue(second_done.wait(2))
            self.assertIs(first.get('value'), runtime, first)
            self.assertIs(second.get('value'), runtime, second)
            module.return_value.LongHorizonRuntime.assert_called_once()
        runtime.recover_all.assert_called_once()

    def test_failed_recovery_closes_candidate_before_retry(self):
        runtime = SimpleNamespace(workers={}, recover_all=mock.Mock(side_effect=HarnessError('bad recovery')), close=mock.Mock())
        with self.factory(runtime):
            with self.assertRaisesRegex(HarnessError, 'bad recovery'):
                self.panel.long_horizon
            self.assertIsNone(self.panel._long_horizon)
            runtime.close.assert_called_once()
            runtime.recover_all.side_effect = None
            self.assertIs(self.panel.long_horizon, runtime)

    def test_reload_waits_for_recovery_and_closes_without_holding_authority(self):
        entered, release = threading.Event(), threading.Event()
        runtime = SimpleNamespace(workers={}, store=SimpleNamespace(active_authority_goals=lambda: []))
        def recover():
            entered.set()
            assert release.wait(3)
        def close():
            if not self.panel.authority_lock.acquire(timeout=.5):
                raise AssertionError('Runtime close held callback authority')
            self.panel.authority_lock.release()
        runtime.recover_all, runtime.close = recover, mock.Mock(side_effect=close)
        new_config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        with self.factory(runtime), mock.patch.object(server, 'load_config', return_value=new_config):
            first, first_done = self.run_async(lambda: self.panel.long_horizon)
            self.assertTrue(entered.wait(1))
            changed, changed_done = self.run_async(self.panel.reload_config)
            try:
                self.assertFalse(changed_done.wait(.05))
                self.assertIs(self.panel.config, self.config)
            finally:
                release.set()
            self.assertTrue(first_done.wait(2))
            self.assertTrue(changed_done.wait(2))
            self.assertNotIn('error', first)
            self.assertNotIn('error', changed)
            self.assertIs(self.panel.config, new_config)
            self.assertIsNone(self.panel._long_horizon)
        runtime.close.assert_called_once()

    def test_rejected_reload_preserves_draining_worker_then_reopens_current_config(self):
        from our_harness.long_horizon import LongHorizonRuntime
        # Exercise real close/start fencing. The fixture worker's join is
        # immediate but it stays alive until explicitly released below.
        runtime = object.__new__(LongHorizonRuntime)
        runtime.lock = threading.RLock()
        runtime.workers = {}
        runtime._watcher_stop = threading.Event()
        runtime._watcher_wake = threading.Event()
        runtime._watcher = SimpleNamespace(is_alive=lambda: False)
        runtime._checkpoint_context = mock.MagicMock()
        runtime._auto_start_enabled = True
        alive = [True]
        worker = SimpleNamespace(is_alive=lambda: alive[0], join=lambda timeout=None: None)
        original_close = runtime.close
        def start_before_close():
            runtime.workers['racing-worker'] = worker
            original_close()
        self.panel._long_horizon = runtime
        new_config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        with mock.patch.object(runtime, 'close', side_effect=start_before_close), \
                mock.patch.object(server, 'load_config', return_value=new_config):
            with self.assertRaisesRegex(HarnessError, 'still finishing'):
                self.panel.reload_config()
        self.assertIs(self.panel.config, self.config)
        self.assertIsNone(self.panel._long_horizon)
        self.assertIs(self.panel._long_horizon_recovering, runtime)
        self.assertTrue(runtime._watcher_stop.is_set())
        with self.assertRaisesRegex(HarnessError, 'draining'):
            self.panel.long_horizon
        with self.assertRaisesRegex(HarnessError, 'middle of a provider'):
            self.panel.require_config_reload_boundary()
        with self.assertRaisesRegex(HarnessError, 'runtime is closed'):
            runtime.start_background('unused-fixture-goal')
        reopened = SimpleNamespace(workers={},recover_all=mock.Mock(),close=mock.Mock())
        alive[0] = False
        with self.factory(reopened) as module:
            self.assertIs(self.panel.long_horizon, reopened)
            self.assertIsNone(self.panel._long_horizon_recovering)
            self.assertIs(self.panel.config, self.config)
            self.assertIs(module.return_value.LongHorizonRuntime.call_args.args[0], self.config)
        reopened.recover_all.assert_called_once()
        self.assertTrue(runtime._checkpoint_context.__exit__.called)

    def test_failed_recovery_keeps_draining_worker_owned_until_retry_is_safe(self):
        runtime = SimpleNamespace(
            workers={}, recover_all=mock.Mock(), close=mock.Mock(),
        )
        def fail_after_start():
            runtime.workers['pending-provider'] = SimpleNamespace(is_alive=lambda: True)
            raise HarnessError('Recovery failed after a worker started')
        runtime.recover_all.side_effect = fail_after_start
        with self.factory(runtime) as module:
            with self.assertRaisesRegex(HarnessError, 'after a worker started'):
                self.panel.long_horizon
            self.assertIsNone(self.panel._long_horizon)
            self.assertIs(self.panel._long_horizon_recovering, runtime)
            with self.assertRaisesRegex(HarnessError, 'draining'):
                self.panel.long_horizon
            with self.assertRaisesRegex(HarnessError, 'middle of a provider'):
                self.panel.require_config_reload_boundary()
            with mock.patch.object(server, 'load_config', return_value=self.config):
                with self.assertRaisesRegex(HarnessError, 'draining'):
                    self.panel.reload_config()
            module.return_value.LongHorizonRuntime.assert_called_once()
            runtime.workers.clear()
            runtime.recover_all.side_effect = None
            self.assertIs(self.panel.long_horizon, runtime)
            self.assertIsNone(self.panel._long_horizon_recovering)
            self.assertEqual(module.return_value.LongHorizonRuntime.call_count, 2)

    def test_closed_runtime_cannot_start_a_queued_watcher_candidate(self):
        from our_harness.long_horizon import LongHorizonRuntime
        runtime = object.__new__(LongHorizonRuntime)
        runtime.lock = threading.RLock()
        runtime._watcher_stop = threading.Event()
        runtime._watcher_stop.set()
        runtime._enable_auto_start_watcher = mock.Mock()
        runtime._auto_start_attempted = {}
        with self.assertRaisesRegex(HarnessError, 'runtime is closed'):
            runtime.start_background('arbitrary-old-goal', automatic=True)
        self.assertEqual(runtime._auto_start_attempted, {})


if __name__ == '__main__':
    unittest.main()
