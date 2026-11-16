"""N21: fnv_launch (now fnv_game launch) sent the coc / save load in the main menu's first frames and the game then took ~190 s to load.
Measured 2026-11-13: the load fills the engine's audio cache (Owner::AddSound 0x00ADCD80 -> BSWin32GameSound::CopyFrom,
a DirectSound buffer copy), and in that state each copy took ~1 s (cache grew one sound a second with
iAudioCacheSize raised to 16 MB; at the 2 MB default it churned for ~3 min). With bEnableAudioCache:Audio = 0 the same
coc at the menu's frame 21 was in the cell at once. The launch turns the cache off for the start load and back on
after it (no menu wait)."""
import sys
import os
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeLive  # noqa: E402
from geck_mcp import fnv_game  # noqa: E402

FLAG = fnv_game.AUDIO_CACHE_FLAG


class AudioCacheStartTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLive()
        self.addCleanup(self.fake.close)
        self.game = fnv_game.Game(self.fake.project)
        self.game.link = self.fake.link
        self.fake.link.put(FLAG, b'\x01')
        self.seen = []

        def wait_in_game(deadline, pid=None, start=None, **kw):
            self.seen.append(self.fake.link.get(FLAG, 1))
            return {'phase': 'in_game'}
        self.game.wait_in_game = wait_in_game

    def test_flag_address_is_bEnableAudioCache(self):
        self.assertEqual(FLAG, 0x011F6EC0)        # Setting bEnableAudioCache:Audio at 0x011F6EBC, value at +4

    def test_coc_loads_with_the_cache_off_and_turns_it_back_on(self):
        out = self.game.start_from_menu('coc', 'HelloCourierRoom', deadline=10 ** 9, pid=4242)
        self.assertEqual(self.seen, [b'\x00'])
        self.assertEqual(self.fake.link.get(FLAG, 1), b'\x01')
        self.assertEqual(out['audioCache'], {'offDuringLoad': True, 'restored': True})
        names = [op for op, _ in self.fake.link.ops]
        self.assertLess(names.index('wr'), names.index('sched'), 'the cache is off before the coc is queued')

    def test_save_start_too(self):
        self.game.start_from_menu('save', 'fnvmcp_test_x', deadline=10 ** 9, pid=4242)
        self.assertEqual(self.seen, [b'\x00'])
        self.assertEqual(self.fake.link.get(FLAG, 1), b'\x01')

    def test_restored_when_the_load_fails(self):
        def fail(deadline, pid=None, start=None, **kw):
            raise fnv_game.GameError('NOT_IN_GAME', 'no')
        self.game.wait_in_game = fail
        with self.assertRaises(fnv_game.GameError):
            self.game.start_from_menu('coc', 'HelloCourierRoom', deadline=10 ** 9, pid=4242)
        self.assertEqual(self.fake.link.get(FLAG, 1), b'\x01')

    def test_a_cache_already_off_stays_off(self):
        self.fake.link.put(FLAG, b'\x00')
        self.game.start_from_menu('coc', 'HelloCourierRoom', deadline=10 ** 9, pid=4242)
        self.assertEqual(self.fake.link.get(FLAG, 1), b'\x00')

    def test_unreadable_flag_is_left_alone(self):
        del self.fake.link.memory[FLAG]
        out = self.game.start_from_menu('coc', 'HelloCourierRoom', deadline=10 ** 9, pid=4242)
        self.assertEqual(out['audioCache'], {'offDuringLoad': False, 'restored': False})
        self.assertNotIn('wr', [op for op, _ in self.fake.link.ops])

    def test_no_menu_wait(self):
        self.assertFalse(hasattr(fnv_game.Game, 'wait_menu_settled'))

    def test_menu_start_touches_nothing(self):
        self.assertEqual(self.game.start_from_menu('menu', None, deadline=10 ** 9, pid=4242)['phase'], 'main_menu')
        self.assertEqual(self.fake.link.ops, [])


class FastPollTests(unittest.TestCase):
    """The launch loops check the game is alive through its process handle, not tasklist (a subprocess of
    0.1-0.3 s on every poll), and poll more often."""

    def setUp(self):
        self.fake = FakeLive()
        self.addCleanup(self.fake.close)
        self.game = fnv_game.Game(self.fake.project)
        self.game.link = self.fake.link

    def test_wait_checks_the_pid_without_tasklist(self):
        from unittest import mock
        answers = iter([None, None, 'ready'])
        with mock.patch.object(fnv_game, 'game_processes', side_effect=AssertionError('tasklist in the loop')), \
                mock.patch.object(fnv_game, 'process_alive', return_value=True) as alive:
            self.assertEqual(self.game._wait(lambda: next(answers), 10 ** 12, 'X', 'x', pid=4242), 'ready')
        self.assertGreaterEqual(alive.call_count, 2)

    def test_wait_reports_an_exit(self):
        from unittest import mock
        self.game.crash_summary = lambda: None
        with mock.patch.object(fnv_game, 'process_alive', return_value=False):
            with self.assertRaises(fnv_game.GameError) as caught:
                self.game._wait(lambda: None, 10 ** 12, 'X', 'x', pid=4242)
        self.assertEqual(caught.exception.code, 'GAME_EXITED')

    def test_wait_in_game_checks_the_pid_without_tasklist(self):
        from unittest import mock
        self.game.phase = lambda ping=None: ('in_game', {})
        with mock.patch.object(fnv_game, 'game_processes', side_effect=AssertionError('tasklist in the loop')), \
                mock.patch.object(fnv_game, 'process_alive', return_value=True):
            out = self.game.wait_in_game(10 ** 12, pid=4242, settle=0.0)
        self.assertEqual(out['phase'], 'in_game')

    @unittest.skipUnless(os.name == 'nt', 'Windows processes and structures')
    def test_process_alive_of_this_process_and_a_dead_pid(self):
        import os
        self.assertTrue(fnv_game.process_alive(os.getpid()))
        self.assertFalse(fnv_game.process_alive(0x7FFFFFF0))


if __name__ == '__main__':
    unittest.main()
