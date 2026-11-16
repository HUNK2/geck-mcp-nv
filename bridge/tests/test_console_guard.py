"""PLAN-3 S5.4: fnv_console refuses lines that quit, load, save or batch-run (G14)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeLive  # noqa: E402
from geck_mcp.fnv_live import LiveError  # noqa: E402


class ConsoleGuardTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLive()
        self.addCleanup(self.fake.close)
        self.live = self.fake.live

    def refused(self, line):
        before = len(self.fake.link.ops)
        with self.assertRaises(LiveError) as caught:
            self.live.run_console(line)
        self.assertEqual(caught.exception.code, 'CONSOLE_REFUSED', line)
        self.assertFalse([op for op in self.fake.link.ops[before:] if op[0] == 'con'], 'refused before the game')
        return str(caught.exception)

    def test_quit_lines_refused(self):
        for line in ('qqq', 'QuitGame', '  exitgame', 'exit'):
            self.assertIn('fnv_game quit', self.refused(line))
        for line in ('load mysave', 'LoadGame x', 'con_loadgame x', 'RestartGame'):
            self.assertIn('fnv_game restart', self.refused(line))     # fnv_game launch refuses while a game runs (review I3)
        for line in ('save x', 'SaveGame x'):
            self.assertIn('fnv_test_save', self.refused(line))
        self.assertIn('fnv_move', self.refused('cow wasteland 0 0'))
        self.refused('bat myfile')
        self.refused('runbatch myfile')

    def test_ref_prefixed_refused(self):
        self.refused('player.qqq')
        self.refused('00000014.QuitGame')

    def test_coc_allowed(self):
        r = self.live.run_console('coc GoodspringsExterior')
        self.assertEqual(r['line'], 'coc GoodspringsExterior')
        self.assertTrue([op for op in self.fake.link.ops if op[0] == 'con'])

    def test_lines_that_only_start_alike_are_allowed(self):
        self.live.run_console('saveref x')
        self.live.run_console('player.loaded')

    def test_refusal_in_envelope_is_failed_before_change(self):
        r = self.live.run('fnv_console', lambda: self.live.run_console('qqq'), 'act')
        self.assertEqual(r['error']['code'], 'CONSOLE_REFUSED')
        self.assertEqual(r['outcome'], 'failed_before_change')


if __name__ == '__main__':
    unittest.main()
