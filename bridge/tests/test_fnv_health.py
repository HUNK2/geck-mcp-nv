"""fnv_health against the fake companion and an open session: script errors since the mark (and not before it), engine
warnings, crash logs and crash-context files since the mark, frame time against the session start, and the live object
count. A clean session is ok; every item names the tool to call next."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from w1_support import W1Live  # noqa: E402
from geck_mcp.fnv_conv import ConvError  # noqa: E402
from geck_mcp.fnv_scenario import ScenarioRunner  # noqa: E402
from geck_mcp.fnv_session import REF_COUNT, HealthTools  # noqa: E402


def open_session(live, real_fps=60, refs=0):
    """The session state fnv_session start leaves behind, without the launch."""
    mark = ScenarioRunner(live).mark()
    live.session_state = {'start': 'continue', 'since': time.time(), 'deterministic': None,
                          'baseline': {'realFps': real_fps, 'refs': refs}, 'journal': mark['journal'],
                          'mark': mark, 'marks': [{'label': 'session start', **mark}]}


class HealthTestCase(unittest.TestCase):
    def setUp(self):
        self.live = W1Live()
        self.warnings = mock.MagicMock()
        self.warnings.from_game.return_value.summary.return_value = {'byCategory': {}}
        for target, kwargs in (('geck_mcp.fnv_session.warnings_for_live', {'new': self.warnings.from_game}),
                               ('geck_mcp.fnv_deep.context_files', {'return_value': []})):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def kinds(self, out):
        return [item['kind'] for item in out['items']]


class HealthTests(HealthTestCase):
    def test_without_a_session_health_needs_one(self):
        with self.assertRaises(ConvError) as cm:
            HealthTools(self.live).check()
        self.assertEqual(cm.exception.code, 'NO_SESSION')

    def test_a_clean_session_is_ok(self):
        open_session(self.live, real_fps=None)
        out = HealthTools(self.live).check()
        self.assertTrue(out['ok'])
        self.assertEqual(out['items'], [])

    def test_a_script_error_since_the_mark_is_reported(self):
        open_session(self.live, real_fps=None)
        self.live.link.add_event('script_error', 'bad line')
        out = HealthTools(self.live).check()
        self.assertFalse(out['ok'])
        self.assertEqual(self.kinds(out), ['script_errors'])
        self.assertIn('bad line', out['items'][0]['what'])
        self.assertIn('fnv_script_errors', out['items'][0]['next'])

    def test_a_script_error_before_the_mark_is_not_reported(self):
        self.live.link.add_event('script_error', 'old')
        open_session(self.live, real_fps=None)
        self.assertTrue(HealthTools(self.live).check()['ok'])

    def test_engine_warnings_are_listed_by_category(self):
        open_session(self.live, real_fps=None)
        self.warnings.from_game.return_value.summary.return_value = {'byCategory': {'missing file': 2, 'form': 1}}
        out = HealthTools(self.live).check()
        self.assertEqual(self.kinds(out), ['warnings'])
        self.assertIn('3 engine warning', out['items'][0]['what'])
        self.assertIn('missing file 2', out['items'][0]['what'])

    def test_a_crash_log_since_the_mark_is_reported(self):
        open_session(self.live, real_fps=None)
        self.live.game.crash = {'log': 'CrashLogger.log', 'duringShutdown': False}
        out = HealthTools(self.live).check()
        self.assertEqual(self.kinds(out), ['crash'])
        self.assertIn('fnv_crash', out['items'][0]['next'])

    def test_a_crash_during_shutdown_is_marked_as_such(self):
        open_session(self.live, real_fps=None)
        self.live.game.crash = {'log': 'CrashLogger.log', 'duringShutdown': True}
        out = HealthTools(self.live).check()
        self.assertIn('during shutdown', out['items'][0]['what'])

    def test_crash_context_files_newer_than_the_mark_are_reported(self):
        open_session(self.live, real_fps=None)
        fresh = SimpleNamespace(stat=lambda: SimpleNamespace(st_mtime=time.time() + 5))
        with mock.patch('geck_mcp.fnv_deep.context_files', return_value=[fresh]):
            out = HealthTools(self.live).check()
        self.assertEqual(self.kinds(out), ['crash_context'])

    def test_a_slower_frame_time_than_the_session_start_is_reported(self):
        open_session(self.live, real_fps=120)
        out = HealthTools(self.live).check()
        self.assertEqual(self.kinds(out), ['frame_time'])
        self.assertIn('fnv_frame_profile', out['items'][0]['next'])

    def test_live_objects_growing_past_the_limit_are_reported(self):
        open_session(self.live, real_fps=None, refs=0)
        self.live.link.put_u32(REF_COUNT, 5000)
        out = HealthTools(self.live).check()
        self.assertEqual(self.kinds(out), ['objects'])
        self.assertIn('5000', out['items'][0]['what'])

    def test_small_object_growth_is_not_reported(self):
        open_session(self.live, real_fps=None, refs=0)
        self.live.link.put_u32(REF_COUNT, 200)
        self.assertTrue(HealthTools(self.live).check()['ok'])

    def test_every_item_names_the_next_tool(self):
        open_session(self.live, real_fps=120, refs=0)
        self.live.link.add_event('script_error', 'x')
        self.live.link.put_u32(REF_COUNT, 9000)
        self.warnings.from_game.return_value.summary.return_value = {'byCategory': {'form': 1}}
        out = HealthTools(self.live).check()
        self.assertGreaterEqual(len(out['items']), 4)
        for item in out['items']:
            self.assertTrue(item['next'], item)
            self.assertIn('kind', item)
            self.assertIn('what', item)


if __name__ == '__main__':
    unittest.main()
