"""PLAN-3 S5.1: one envelope, honest outcomes (T4)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeLive  # noqa: E402
from geck_mcp.fnv_conv import ConvError  # noqa: E402
from geck_mcp.fnv_deep import HookError  # noqa: E402
from geck_mcp.fnv_link import LinkError  # noqa: E402
from geck_mcp.fnv_live import LiveError  # noqa: E402


def raising(error):
    def action():
        raise error
    return action


class NotRunningTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLive()
        self.addCleanup(self.fake.close)
        self.live = self.fake.live

    def test_not_running_codes_unified(self):
        self.live._game.process = lambda fresh=False: None          # no FalloutNV.exe
        paths = [
            ('fnv_mem_read', LinkError('COMPANION_UNREACHABLE', 'Engine companion pipe x is not available')),
            ('fnv_place', LiveError('NOT_RUNNING', 'The game is not running')),
            ('fnv_time', ConvError('NOT_RUNNING', 'Time control needs the game running.')),
            ('fnv_threads', HookError('GAME_NOT_RUNNING', 'FalloutNV.exe is not running')),
            ('fnv_ref', LiveError('NO_GAME_LOADED', 'No game is loaded (phase main_menu)')),
        ]
        for name, error in paths:
            with self.subTest(name):
                r = self.live.run(name, raising(error), kind='act' if name in ('fnv_place',) else 'read')
                self.assertFalse(r['ok'])
                self.assertEqual(r['error']['code'], 'GAME_NOT_RUNNING')
                self.assertEqual(r['error']['recovery'], 'fnv_game launch, then call again.')
                self.assertEqual(r['outcome'], 'failed_before_change')
                self.assertTrue(r['error']['retrySafe'])

    def test_unreachable_with_a_game_running_keeps_its_code(self):
        r = self.live.run('fnv_mem_read', raising(LinkError('COMPANION_UNREACHABLE', 'busy')))
        self.assertEqual(r['error']['code'], 'COMPANION_UNREACHABLE')

    def test_recovery_is_the_codes_next_step(self):
        r = self.live.run('fnv_ref', raising(LiveError('REF_NOT_FOUND', 'No such reference')))
        self.assertIn('fnv_record action=find', r['error']['recovery'])


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLive()
        self.addCleanup(self.fake.close)
        self.live = self.fake.live

    def test_uncoded_error_in_act_is_outcome_unknown(self):
        r = self.live.run('fnv_place', raising(KeyError('pose')), kind='act')
        self.assertEqual(r['outcome'], 'outcome_unknown')
        self.assertFalse(r['error']['retrySafe'])
        self.assertEqual(r['error']['code'], 'FNV_ERROR')

    def test_uncoded_error_in_read_is_failed_before_change(self):
        r = self.live.run('fnv_ref', raising(KeyError('pose')))
        self.assertEqual(r['outcome'], 'failed_before_change')

    def test_value_error_in_act_stays_invalid_argument(self):
        r = self.live.run('fnv_place', raising(ValueError('x: expected a number')), kind='act')
        self.assertEqual(r['error']['code'], 'INVALID_ARGUMENT')
        self.assertEqual(r['outcome'], 'failed_before_change')

    def test_non_string_code_is_fnv_error(self):
        error = OSError('http')
        error.code = 404
        r = self.live.run('fnv_asset', raising(error), kind='act')
        self.assertEqual(r['error']['code'], 'FNV_ERROR')

    def test_partial_change_lists_journal_ids(self):
        made = []

        def action():
            made.append(self.live.journal.add({'kind': 'spawn', 'ref': 'FF000900',
                                               'undo': {'op': 'console_ref', 'ref': 'FF000900', 'lines': ['Disable']}}))
            raise LiveError('SPAWN_FAILED', 'the second copy failed', {'made': ['FF000900']})
        r = self.live.run('fnv_place', action, kind='act')
        self.assertEqual(r['outcome'], 'partial_change')
        self.assertFalse(r['error']['retrySafe'])
        self.assertEqual(r['data']['journalIds'], [made[0]['id']])
        self.assertEqual(r['data']['made'], ['FF000900'])
        self.assertIn('fnv_undo', r['error']['recovery'])

    def test_an_intent_closed_by_its_refusal_is_not_a_change(self):
        def action():
            entry = self.live.journal.add({'kind': 'mem_write', 'state': 'pending',
                                           'undo': {'op': 'writes', 'writes': []}})
            self.live.journal.mark_undone([entry['id']])
            raise LiveError('MEMORY_UNWRITABLE', 'not writable')
        r = self.live.run('fnv_mem_write', action, kind='act')
        self.assertEqual(r['outcome'], 'failed_before_change')
        self.assertTrue(r['error']['retrySafe'])

    def test_timeout_after_an_intent_stays_outcome_unknown(self):
        def action():
            self.live.journal.add({'kind': 'mem_write', 'state': 'pending', 'undo': {'op': 'writes', 'writes': []}})
            raise LinkError('COMPANION_TIMEOUT', 'no answer')
        r = self.live.run('fnv_mem_write', action, kind='act')
        self.assertEqual(r['outcome'], 'outcome_unknown')
        self.assertEqual(len(r['data']['journalIds']), 1)

    def test_entries_from_other_threads_are_not_this_calls(self):
        import threading

        def other():
            self.live.journal.add({'kind': 'spawn', 'undo': {'op': 'console_ref', 'ref': 'FF0', 'lines': ['Disable']}})

        def action():
            t = threading.Thread(target=other)
            t.start()
            t.join()
            raise LiveError('SPAWN_FAILED', 'nothing made')
        r = self.live.run('fnv_place', action, kind='act')
        self.assertEqual(r['outcome'], 'failed_before_change')

    def test_identity_failure_does_not_break_the_error_envelope(self):
        def boom():
            raise RuntimeError('identity')
        self.live.identity = boom
        r = self.live.run('fnv_ref', raising(LiveError('REF_NOT_FOUND', 'x')))
        self.assertFalse(r['ok'])
        self.assertIsNone(r['game'])


class CoverageNotStartedTests(unittest.TestCase):
    def test_coverage_stop_without_a_run_is_not_game_not_running(self):
        from geck_mcp.fnv_coverage import CoverageTools
        with FakeLive() as fake:
            cov = CoverageTools(fake.live)
            r = fake.live.run('fnv_coverage', cov.stop)
            self.assertEqual(r['error']['code'], 'NOT_STARTED')


if __name__ == '__main__':
    unittest.main()
