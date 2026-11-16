"""fnv_test steps and the scenario runner additions (step, wait for, input with frames, as and $name, deterministic, repeat)
against the fake companion. The runner's prepare, screenshot and report writing are stubbed: no game is launched."""
from pathlib import Path
from unittest import mock
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from w1_support import W1Live  # noqa: E402
from geck_mcp import fnv_time  # noqa: E402
from geck_mcp.fnv_scenario import ScenarioError, ScenarioRunner  # noqa: E402


class ScenarioLoadTests(unittest.TestCase):
    def setUp(self):
        self.runner = ScenarioRunner(W1Live())

    def test_inline_data_loads_with_a_default_name(self):
        data = self.runner.load(data={'steps': [{'wait': {'seconds': 0}}]})
        self.assertEqual(data['name'], 'inline scenario')
        self.assertEqual(data['_source'], '<inline>')

    def test_break_and_flow_are_refused_with_not_available_yet(self):
        for key in ('break', 'flow'):
            with self.subTest(key=key):
                with self.assertRaises(ScenarioError) as cm:
                    self.runner.load(data={'steps': [{key: 'x'}]})
                self.assertEqual(cm.exception.code, 'NOT_AVAILABLE_YET')
                self.assertIn(key, str(cm.exception))

    def test_on_crash_hold_is_refused_and_report_is_kept(self):
        with self.assertRaises(ScenarioError) as cm:
            self.runner.load(data={'steps': [], 'on_crash': 'hold'})
        self.assertEqual(cm.exception.code, 'NOT_AVAILABLE_YET')
        self.assertEqual(self.runner.load(data={'steps': [], 'on_crash': 'report'})['on_crash'], 'report')

    def test_repeat_is_from_1_to_20(self):
        for bad in (0, 21, True, 'two'):
            with self.subTest(repeat=bad):
                with self.assertRaises(ScenarioError) as cm:
                    self.runner.load(data={'steps': [], 'repeat': bad})
                self.assertEqual(cm.exception.code, 'BAD_SCENARIO')
        self.assertEqual(self.runner.load(data={'steps': [], 'repeat': 3})['repeat'], 3)

    def test_unknown_keys_are_refused(self):
        with self.assertRaises(ScenarioError) as cm:
            self.runner.load(data={'steps': [], 'bogus': 1})
        self.assertEqual(cm.exception.code, 'BAD_SCENARIO')


class FakeHands:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def play(self, action, **kw):
        self.calls.append(('play', action, kw))
        return self.result

    def ui_do(self, action, **kw):
        self.calls.append(('ui', action, kw))
        return {'clicked': kw.get('label')}


class EyesHandsStepTests(unittest.TestCase):
    def runner_with(self, result):
        live = W1Live()
        live.hands = FakeHands(result)
        live.eyes = type('E', (), {'photo': lambda s, action, **kw: {'path': 'x.png', 'action': action}})()
        return ScenarioRunner(live), live

    def test_photo_play_and_ui_load_now(self):
        runner, _ = self.runner_with({})
        runner.load(data={'steps': [{'photo': 'view'}, {'play': {'action': 'goto', 'ref': 'x'}},
                                    {'ui': {'action': 'button', 'label': 'Take All'}}]})

    def test_talk_passes_only_when_a_dialogue_opened(self):
        runner, live = self.runner_with({'dialogue': True})
        self.assertTrue(runner.step({'play': {'action': 'talk', 'ref': 'EasyPeteRef'}}, None)['passed'])
        self.assertEqual(live.hands.calls[0], ('play', 'talk', {'ref': 'EasyPeteRef'}))
        runner, _ = self.runner_with({'dialogue': False})
        out = runner.step({'play': {'action': 'talk', 'ref': 'TrudyRef'}}, None)
        self.assertFalse(out['passed'])

    def test_activate_with_a_refusal_fails(self):
        runner, _ = self.runner_with({'pressed': False, 'refused': [{'reason': 'disabled'}]})
        out = runner.step({'play': {'action': 'activate', 'ref': 'x'}}, None)
        self.assertFalse(out['passed'])
        self.assertIn('disabled', out['reason'])

    def test_ui_and_photo_steps_call_through(self):
        runner, live = self.runner_with({})
        self.assertTrue(runner.step({'ui': {'action': 'button', 'label': 'Take All'}}, None)['passed'])
        self.assertEqual(live.hands.calls[-1], ('ui', 'button', {'label': 'Take All'}))
        out = runner.step({'photo': 'view'}, None)
        self.assertEqual(out['actual']['path'], 'x.png')


class ScenarioStepTests(unittest.TestCase):
    def setUp(self):
        self.live = W1Live()
        self.link = self.live.link
        self.runner = ScenarioRunner(self.live)

    def test_step_advances_exactly_the_frames_asked(self):
        before = self.link.frame
        out = self.runner.step({'step': {'frames': 5}}, None)
        self.assertTrue(out['passed'])
        self.assertEqual(out['action'], 'step')
        self.assertEqual(out['actual']['advanced'], 5)
        self.assertEqual(self.link.frame - before, 5)

    def test_as_keeps_the_result_for_later_dollar_names(self):
        out = self.runner.step({'step': {'frames': 2}, 'as': 'first'}, None)
        self.assertEqual(out['as'], 'first')
        self.assertEqual(self.runner.vars['first']['advanced'], 2)

    def test_dollar_names_are_replaced_in_later_steps(self):
        self.runner.vars = {'ref': '0012AB'}
        self.assertEqual(self.runner._subst({'ref': '$ref', 'n': 1}), {'ref': '0012AB', 'n': 1})

    def test_input_with_frames_goes_through_the_injector(self):
        self.live.input = mock.MagicMock(return_value={'accepted': True, 'injected': True})
        out = self.runner.step({'input': {'control': 'jump', 'frames': 4}}, None)
        self.assertTrue(out['passed'])
        args, kwargs = self.live.input.call_args
        self.assertEqual(args[0], 'tap')
        self.assertEqual(args[1], 'jump')
        self.assertEqual(kwargs['frames'], 4)

    def test_input_with_a_mouse_delta_passes_the_pair(self):
        self.live.input = mock.MagicMock(return_value={'accepted': True})
        self.runner.step({'input': {'mouse': [-3, 2], 'frames': 2}}, None)
        self.assertEqual(self.live.input.call_args[1]['mouse'], (-3, 2))

    def test_wait_for_loaded_returns_once_the_cell_is_loaded(self):
        out = self.runner.step({'wait': {'for': 'loaded', 'timeout': 5}}, None)
        self.assertTrue(out['passed'])

    def test_wait_for_an_event_finds_one_that_arrives_during_the_wait(self):
        with mock.patch('geck_mcp.fnv_scenario.time.sleep', side_effect=lambda s: self.link.add_event('script_error', 'x')):
            out = self.runner.step({'wait': {'for': 'script_error', 'timeout': 5}}, None)
        self.assertTrue(out['passed'])
        self.assertEqual(out['actual']['type'], 'script_error')

    def test_wait_for_an_event_that_never_comes_times_out_with_a_reason(self):
        out = self.runner.step({'wait': {'for': 'quest_stage', 'timeout': 0.1}}, None)
        self.assertFalse(out['passed'])
        self.assertIn('no quest_stage event', out['reason'])

    def test_wait_for_a_predicate_runs_frames_until_true(self):
        out = self.runner.step({'wait': {'for': 'frame >= 105', 'timeout': 5}}, None)
        self.assertTrue(out['passed'])
        self.assertEqual(self.link.frame, 105)


class ScenarioRunTests(unittest.TestCase):
    def setUp(self):
        self.live = W1Live()
        self.runner = ScenarioRunner(self.live)
        for name, value in (('prepare', {'launched': False}), ('save_report', None), ('screenshot', None),
                            ('debug_bundle', {})):
            patcher = mock.patch.object(ScenarioRunner, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_steps_run_in_order_and_the_report_says_so(self):
        data = {'name': 't', 'allow_menus': True,
                'steps': [{'step': {'frames': 2}}, {'wait': {'seconds': 0}}]}
        report = self.runner.run(data=data)
        self.assertTrue(report['passed'])
        self.assertEqual([s['action'] for s in report['steps']], ['step', 'wait'])

    def test_a_failed_step_fails_the_run_with_its_reason(self):
        data = {'name': 'f', 'allow_menus': True, 'steps': [{'wait': {'for': 'quest_stage', 'timeout': 0.1}}]}
        report = self.runner.run(data=data)
        self.assertFalse(report['passed'])
        self.assertEqual(report['failures'][0]['phase'], 'steps')

    def test_deterministic_fixes_the_step_seeds_the_rng_and_settles_after(self):
        agent = mock.MagicMock()
        with mock.patch.object(ScenarioRunner, 'agent', new_callable=mock.PropertyMock, return_value=agent):
            report = self.runner.run(data={'name': 'd', 'allow_menus': True, 'deterministic': True, 'steps': []})
        agent.rng.assert_called_with(seed=1)
        self.assertEqual(report['deterministic'], {'fps': 60, 'seed': 1})
        self.assertEqual(self.link.u32(fnv_time.TIME_GLOBAL + 4), 0)   # fixed step off again
        self.assertFalse(self.link.held)

    def test_repeat_runs_the_scenario_and_lists_the_steps_that_differ(self):
        steps = [{'wait': {'seconds': 0}}, {'step': {'frames': 2}}]
        report = self.runner.run(data={'name': 'r', 'allow_menus': True, 'repeat': 2, 'steps': steps})
        self.assertEqual(report['repeat'], 2)
        self.assertTrue(report['passed'])
        self.assertEqual(report['comparison']['runs'], 2)
        self.assertFalse(report['comparison']['identical'])
        self.assertEqual([d['step'] for d in report['comparison']['differences']], [2])

    @property
    def link(self):
        return self.live.link


if __name__ == '__main__':
    unittest.main()
