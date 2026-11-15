"""PLAN-3 S5 review fixes (fresh Opus review of 1ace9fa)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeLive  # noqa: E402
from geck_mcp import fnv_game  # noqa: E402
from geck_mcp.fnv_live import LiveError  # noqa: E402


def raising(error):
    def action():
        raise error
    return action


class NoGameLoadedTests(unittest.TestCase):
    def test_i1_no_game_loaded_with_a_running_game_keeps_its_code(self):
        with FakeLive() as fake:
            r = fake.live.run('fnv_ref', raising(LiveError('NO_GAME_LOADED', 'No game is loaded (phase main_menu)')))
            self.assertEqual(r['error']['code'], 'NO_GAME_LOADED')
            self.assertIn('fnv_restart', r['error']['recovery'])
            fake.live._game.process = lambda fresh=False: None
            r = fake.live.run('fnv_ref', raising(LiveError('NO_GAME_LOADED', 'x')))
            self.assertEqual(r['error']['code'], 'GAME_NOT_RUNNING')


class ConsoleAliasTests(unittest.TestCase):
    def test_i2_engine_aliases_refused(self):
        from geck_mcp.fnv_live import LiveService
        for line in ('exit', 'CenterOnWorld wasteland 0 0', 'RunBatchScript x.txt', 'ForceSave', 'SystemSave',
                     'Autosave'):
            self.assertIsNotNone(LiveService.console_refused(line), line)

    def test_i3_load_and_restart_point_at_tools_that_work_with_a_game_running(self):
        from geck_mcp.fnv_live import LiveService
        for word in ('load', 'loadgame', 'con_loadgame', 'restartgame'):
            self.assertIn('fnv_restart', LiveService.CONSOLE_INSTEAD[word], word)
            self.assertNotIn('fnv_launch', LiveService.CONSOLE_INSTEAD[word], word)

    def test_m10_run_script_single_line_quit_refused(self):
        with FakeLive() as fake:
            for source in ('QuitGame', 'exit', 'player.qqq'):
                with self.assertRaises(LiveError) as caught:
                    fake.live.run_script(source)
                self.assertEqual(caught.exception.code, 'CONSOLE_REFUSED', source)


class EncodeTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLive()
        self.addCleanup(self.fake.close)

    def test_i4_bool_text_parsed(self):
        enc = self.fake.live._encode
        self.assertEqual(enc('bool', 'false'), b'\x00')
        self.assertEqual(enc('bool', '0'), b'\x00')
        self.assertEqual(enc('bool', 'True'), b'\x01')
        self.assertEqual(enc('bool', 1), b'\x01')
        with self.assertRaises(ValueError):
            enc('bool', 'maybe')

    def test_m6_float_ranges_and_whole_numbers(self):
        enc = self.fake.live._encode
        with self.assertRaises(ValueError):
            enc('f32', 1e40)
        with self.assertRaises(ValueError):
            enc('u8', 1.9)
        self.assertEqual(enc('u8', 2.0), b'\x02')


class ParserTests(unittest.TestCase):
    def test_m7_uppercase_0x_prefix(self):
        from geck_mcp import fnv_args
        self.assertEqual(fnv_args.hex_int('start', '0X87F9F0'), 0x87F9F0)
        self.assertEqual(fnv_args.form_id('0X14'), 0x14)


class EngineGetThisTests(unittest.TestCase):
    def test_m8_this_counts_as_an_argument_outside_thiscall(self):
        from geck_mcp.fnv_control import ControlTools
        with FakeLive() as fake:
            live = fake.live
            fn = {'name': 'GetActorThing', 'status': 'proven'}
            live._engine = SimpleNamespace(resolve=lambda f: 0x00500000, summary=lambda a: fn, walk=lambda *a: ([], None),
                                           functions={0x00500000: {'signature': 'int __cdecl GetActorThing(class Actor *)'}})
            control = ControlTools(SimpleNamespace(live=live, deep=None))
            called = []
            control._call = lambda *a: called.append(a) or {'value': 1, 'raw': 1}
            control._purity_of = lambda *a: {'pure': True}    # machine-code check: tests/test_r4_contract.py
            control.engine_get('GetActorThing', this='player', args=[])
            self.assertEqual(len(called), 1)
            with self.assertRaises(LiveError):
                control.engine_get('GetActorThing', this='player', args=[1])


class CapOrderTests(unittest.TestCase):
    def test_m9_revert_all_lists_every_failure(self):
        with FakeLive() as fake:
            live = fake.live
            fake.link.put(0x01000000, b'\x00')
            # undos run newest first: the oldest entry's row comes last, past the cap
            bad = live.journal.add({'kind': 'memory', 'undo': {'op': 'write', 'address': '0x7FFF0000', 'hex': '00'}})
            for _ in range(210):
                live.journal.add({'kind': 'memory', 'undo': {'op': 'write', 'address': '0x01000000', 'hex': '00'}})
            r = live.revert_all()
            self.assertNotIn(bad['id'], [row['id'] for row in r['results']], 'past the cap of 200 rows')
            self.assertEqual(r['failed'], [bad['id']])

    def test_m9_place_list_keeps_the_newest(self):
        from geck_mcp.fnv_create import PlaceTools
        with FakeLive() as fake:
            for k in range(210):
                fake.live.journal.add({'kind': 'place', 'ref': 'FF%06X' % k, 'base': '00000014',
                                       'position': [0, 0, 0], 'rotation': [0, 0, 0], 'scale': 1.0, 'undo': None})
            tools = PlaceTools.__new__(PlaceTools)
            tools.live = fake.live
            r = tools.run('list')
            self.assertEqual(r['placed'][-1]['ref'], 'FF0000D1')
            self.assertEqual(r['placed'][0]['ref'], 'FF00000A')


def filetime(seconds_ago):
    return int((time.time() - seconds_ago + 11644473600) * 10 ** 7)


class ExitNoteAgeTests(unittest.TestCase):
    def test_m11_an_old_exit_note_does_not_hide_a_second_game(self):
        with FakeLive() as fake:
            game = fnv_game.Game(fake.project)
            game._session = {'pid': 100}
            two = [(100, filetime(600)), (200, filetime(300))]
            old_note = {'pid': 100, 'how': 'console', 'at': time.time() - 600}
            with mock.patch.object(fnv_game, 'game_processes', return_value=two), \
                    mock.patch.object(fnv_game.Game, 'last_exit', return_value=old_note):
                with self.assertRaises(fnv_game.GameError):
                    game.process(fresh=True)
            new_note = dict(old_note, at=time.time() - 5)
            with mock.patch.object(fnv_game, 'game_processes', return_value=two), \
                    mock.patch.object(fnv_game.Game, 'last_exit', return_value=new_note):
                self.assertEqual(game.process(fresh=True)[0], 100)


if __name__ == '__main__':
    unittest.main()
