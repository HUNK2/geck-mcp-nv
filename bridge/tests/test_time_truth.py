"""PLAN-3 S5.5: time and status tell the truth (G5, G6, G7, G8, G15)."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeLive  # noqa: E402
from geck_mcp import fnv_game  # noqa: E402
from geck_mcp.fnv_conv import ConvError  # noqa: E402
from test_fnv_time import MULT, TG, TimeTestCase  # noqa: E402


class ClockTests(TimeTestCase):
    def test_game_clock_wait_refuses_when_paused(self):
        self.control.run('pause')
        with self.assertRaises(ConvError) as caught:
            self.wait.run(seconds=1)
        self.assertEqual(caught.exception.code, 'CLOCK_PAUSED')
        self.assertIn('fnv_time resume, or use clock=real', str(caught.exception))
        out = self.wait.run(seconds=1, clock='real')       # real seconds still pass
        self.assertEqual(out['estimatedFrames'], 60)

    def test_real_clock_ignores_fixed_step(self):
        self.control.run('fixed', fps=30)                  # game time per frame, not frames per real second
        out = self.wait.run(seconds=2, clock='real')
        self.assertEqual(out['estimatedFrames'], 120)      # the fake renders 60 frames a real second
        self.assertEqual(out['rateSource'], 'measured')
        self.comp.put_f32(MULT, 1.0)
        out = self.wait.run(seconds=2)
        self.assertEqual(out['estimatedFrames'], 60)
        self.assertEqual(out['rateSource'], 'fixed step')

    def test_status_verdict_paused(self):
        self.control.run('pause')
        out = self.control.run('status')
        self.assertTrue(out['verdict'].startswith('The game clock is paused (frames still run)'), out['verdict'])
        self.comp.put(TG, b'\x00\x00\x00\x00')
        self.assertTrue(self.control.run('status')['verdict'].startswith('The game is running'))


class EnvelopeFrameTests(unittest.TestCase):
    def test_envelope_frame_is_newest(self):
        with FakeLive() as fake:
            fake.link.hello['frame'] = 100
            fake.link.last_frame = 450
            self.assertEqual(fake.live.identity()['frame'], 450)
            fake.link.last_frame = None
            self.assertEqual(fake.live.identity()['frame'], 100)


def filetime(seconds_ago):
    return int((time.time() - seconds_ago + 11644473600) * 10 ** 7)


class ExitingTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLive()
        self.addCleanup(self.fake.close)
        self.game = fnv_game.Game(self.fake.project)
        self.game.link = self.fake.link
        self.game._session = {'pid': 100}

    def test_two_pids_during_exit(self):
        two = [(100, filetime(600)), (200, filetime(1))]          # the session's game and a newborn one
        with mock.patch.object(fnv_game, 'game_processes', return_value=two):
            self.assertEqual(self.game.process(fresh=True), two[0])
            self.assertTrue(self.game.exiting)

    def test_two_old_games_are_still_several(self):
        two = [(100, filetime(600)), (200, filetime(300))]
        with mock.patch.object(fnv_game, 'game_processes', return_value=two):
            with self.assertRaises(fnv_game.GameError) as caught:
                self.game.process(fresh=True)
            self.assertEqual(caught.exception.code, 'SEVERAL_GAMES')

    def test_status_says_exiting(self):
        two = [(100, filetime(600)), (200, filetime(1))]
        with mock.patch.object(fnv_game, 'game_processes', return_value=two):
            out = self.game.status()
        self.assertTrue(out['exiting'])
        self.assertEqual(out['pid'], 100)


if __name__ == '__main__':
    unittest.main()
