"""Persistent transport safety tests against a fake serve-protocol worker."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from geck_mcp.config import Project
from geck_mcp.transport import BridgeError
from geck_mcp.worker import AutoTransport, PersistentTransport

FAKE = [sys.executable, str(Path(__file__).with_name('fake_worker.py'))]


def project(root, timeout=5):
    data = root / 'Data'
    data.mkdir(parents=True, exist_ok=True)
    # model_copy bypasses the 5 s production minimum so timeout tests stay quick.
    return Project.model_validate(dict(
        project_id='worker-test', game_root=root,
        plugin='Fixture.esp', records=[dict(editor_id='FixtureStatic', form_id='01000ADD')],
        state_dir=root / 'state', helper=root / 'helper.exe')).model_copy(
            update={'helper_timeout_seconds': timeout})


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / 'starts.log'
        self.env = patch.dict(os.environ, {'FAKE_WORKER_LOG': str(self.log),
                                           'FAKE_WORKER_MODE': 'normal',
                                           'GECK_BRIDGE_SESSION': 'stale-from-shell'})
        self.env.start()
        self.transports = []

    def tearDown(self):
        for t in self.transports:
            t.close()
        self.env.stop()
        self.tmp.cleanup()

    def make(self, mode='normal', timeout=5):
        os.environ['FAKE_WORKER_MODE'] = mode
        t = PersistentTransport(project(self.root, timeout), command=FAKE, start_timeout=3)
        self.transports.append(t)
        return t

    def starts(self):
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def test_one_process_serves_many_calls(self):
        t = self.make()
        pids = {t.call('status')['workerPid'] for _ in range(20)}
        self.assertEqual(len(pids), 1)
        self.assertEqual(self.starts(), 1)
        self.assertEqual(t.describe()['requests'], 20)

    def test_request_env_is_complete_and_never_inherits_process_guards(self):
        t = self.make()
        after = t.call('status', session='42:1', expected_model='a\\b.nif')
        self.assertEqual(after['env'], {'GECK_BRIDGE_PLUGIN': 'Fixture.esp',
                                        'GECK_BRIDGE_RECORDS': 'FixtureStatic',
                                        'GECK_BRIDGE_MCP': '1',
                                        'GECK_BRIDGE_SESSION': '42:1',
                                        'GECK_BRIDGE_EXPECTED_MODEL': 'a\\b.nif'})
        self.assertEqual(after['leakedProcessEnv'], [])
        # The next request without a session must not reuse the previous one.
        self.assertNotIn('GECK_BRIDGE_SESSION', t.call('status')['env'])

    def test_timeout_is_outcome_unknown_and_never_replayed(self):
        t = self.make('hang:object.setModel', timeout=1)
        t.call('status')
        with self.assertRaises(BridgeError) as caught:
            t.call('object.setModel', 'FixtureStatic', 'x.nif')
        self.assertEqual(caught.exception.code, 'OUTCOME_UNKNOWN')
        self.assertTrue(caught.exception.uncertain)
        self.assertFalse(t.alive)
        # A later call starts a fresh worker and does not resend the mutation.
        self.assertEqual(t.call('status')['args'], ['status'])
        self.assertEqual(self.starts(), 2)

    def test_worker_death_after_delivery_is_outcome_unknown(self):
        t = self.make('die:plugin.save')
        with self.assertRaises(BridgeError) as caught:
            t.call('plugin.save', 'a' * 64)
        self.assertEqual(caught.exception.code, 'OUTCOME_UNKNOWN')
        self.assertTrue(caught.exception.uncertain)

    def test_dead_idle_worker_is_restarted_before_delivery(self):
        t = self.make()
        t.call('status')
        t._proc.kill()
        t._proc.wait()
        self.assertEqual(t.call('status')['served'], 1)
        self.assertEqual(self.starts(), 2)

    def test_pipe_failure_after_partial_delivery_is_never_replayed(self):
        t = self.make()
        t.call('status')
        with patch.object(t, '_send', side_effect=BridgeError('OUTCOME_UNKNOWN', 'partial write', uncertain=True)) as send:
            with self.assertRaises(BridgeError) as caught:
                t.call('script.save', 'Fixture', 'a' * 64)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(send.call_count, 1)
        self.assertFalse(t.alive)

    def test_editor_errors_keep_worker_and_are_typed(self):
        t = self.make()
        with self.assertRaises(BridgeError) as caught:
            t.call('fail.op')
        self.assertEqual(caught.exception.code, 'MODAL_BLOCKED')
        self.assertFalse(caught.exception.uncertain)
        self.assertTrue(t.alive)
        self.assertEqual(self.starts(), 1)

    def test_protocol_rejection_is_not_uncertain(self):
        t = self.make()
        with self.assertRaises(BridgeError) as caught:
            t.call('bad.request')
        self.assertEqual(caught.exception.code, 'HELPER_PROTOCOL_ERROR')
        self.assertFalse(caught.exception.uncertain)

    def test_noise_lines_are_ignored(self):
        t = self.make('noise')
        self.assertEqual(t.call('status')['args'], ['status'])

    def test_batch_single_round_trip_and_stop_on_error(self):
        t = self.make()
        results = t.batch([('category.select', 'World Objects/Static'), ('objects.find', 'X')])
        self.assertEqual([r['args'][0] for r in results], ['category.select', 'objects.find'])
        with self.assertRaises(BridgeError) as caught:
            t.batch([('status',), ('fail.op',), ('objects.find', 'X')])
        self.assertEqual(len(caught.exception.evidence['completed']), 1)
        self.assertEqual(t.describe()['requests'], 2)

    def test_missing_handshake_is_unsupported(self):
        t = self.make('no_hello')
        with self.assertRaises(BridgeError) as caught:
            t.call('status')
        self.assertEqual(caught.exception.code, 'WORKER_UNSUPPORTED')
        self.assertFalse(t.alive)

    def test_launcher_without_stdin_is_unsupported_not_uncertain(self):
        t = self.make('deaf')
        t.start_timeout = 1
        with self.assertRaises(BridgeError) as caught:
            t.call('object.setModel', 'FixtureStatic', 'x.nif')
        self.assertEqual(caught.exception.code, 'WORKER_UNSUPPORTED')
        self.assertFalse(caught.exception.uncertain)
        self.assertFalse(t.alive)

    def test_auto_falls_back_to_oneshot_for_legacy_helper(self):
        os.environ['FAKE_WORKER_MODE'] = 'legacy'
        p = project(self.root)
        persistent = PersistentTransport(p, command=FAKE, start_timeout=3)

        class OneShot:
            calls = []
            def call(self, operation, *args, session=None, expected_model=None):
                self.calls.append(operation)
                return {'oneshot': True}
        auto = AutoTransport(p, persistent=persistent, oneshot=OneShot())
        self.assertEqual(auto.call('status'), {'oneshot': True})
        self.assertEqual(auto.describe()['mode'], 'oneshot')
        self.assertIn('WORKER_UNSUPPORTED', auto.describe()['fallbackReason'])
        # Batch degrades to sequential one-shot calls.
        auto.batch([('status',), ('objects.find', 'X')])
        self.assertEqual(OneShot.calls, ['status', 'status', 'objects.find'])

    def test_auto_uses_persistent_when_supported(self):
        p = project(self.root)
        auto = AutoTransport(p, persistent=PersistentTransport(p, command=FAKE, start_timeout=3))
        self.transports.append(auto.persistent)
        auto.call('status')
        self.assertEqual(auto.describe()['mode'], 'persistent')

    def test_forced_oneshot_env(self):
        with patch.dict(os.environ, {'GECK_MCP_TRANSPORT': 'oneshot'}):
            auto = AutoTransport(project(self.root))
        self.assertEqual(auto.mode, 'oneshot')

    def test_close_shuts_worker_down(self):
        t = self.make()
        t.call('status')
        proc = t._proc
        t.close()
        self.assertIsNotNone(proc.poll())

    def test_persistent_calls_are_fast(self):
        t = self.make()
        t.call('status')
        started = time.monotonic()
        for _ in range(50):
            t.call('status')
        self.assertLess((time.monotonic() - started) / 50, 0.05)


class ServiceBatchTests(unittest.TestCase):
    def test_select_uses_one_batch_step(self):
        from tests.test_service import FakeTransport, plugin
        from geck_mcp.service import Service
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = project(root)
            p.plugin_path.write_bytes(plugin())

            class Batching(FakeTransport):
                batches = []
                def batch(self, steps, session=None, expected_model=None, stop_on_error=True):
                    self.batches.append([s[0] for s in steps])
                    return [self.call(op, *a, session=session) for op, *a in steps]
            fake = Batching(p)
            service = Service(p, fake)
            result = service.read('FixtureStatic', 'editor', fake.session)
            self.assertTrue(result['ok'], result)
            self.assertEqual(fake.batches, [['category.select', 'filter.set', 'objects.find']])
            steps = service.operation_evidence(result['operationId'])['steps']
            self.assertIn('batch:category.select+filter.set+objects.find', [s['operation'] for s in steps])
            with self.assertRaises(ValueError):
                service.call_batch([('plugin.save', 'x')])


if __name__ == '__main__':
    unittest.main()
