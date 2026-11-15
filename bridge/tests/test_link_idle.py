"""PLAN-3 S4.1: the engine link lets go of the game's single-instance pipe when it is idle, so another client (the
live regression runner, a second server) can attach; the next request reconnects. Run against the real companion
under the fake xNVSE host."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/: engine_host
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine_host import StopHost, build, needs_zig  # noqa: E402
from geck_mcp import fnv_link  # noqa: E402

IDLE = 0.4


@needs_zig
class LinkIdleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        receipt = build(cls.tmp.name)
        cls.host = StopHost(receipt, Path(receipt['dll']).parent, cls.tmp.name, 300)
        cls.host.link.close()

    @classmethod
    def tearDownClass(cls):
        cls.host.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.link = fnv_link.EngineLink(self.host.pipe)
        self.link.idle_release_seconds = IDLE
        self.addCleanup(self.link.close)

    def idle(self):
        time.sleep(IDLE * 3)

    def test_closes_after_idle(self):
        self.assertTrue(self.link.ping().get('pong'))
        self.assertIsNotNone(self.link.handle)
        self.idle()
        self.assertIsNone(self.link.handle)
        self.assertIsNone(self.link.hello)
        self.assertIsNone(self.link.last_frame)
        # the pipe is free: another client attaches
        other = fnv_link.EngineLink(self.host.pipe).connect(wait=2)
        self.assertTrue(other.ping().get('pong'))
        other.close()

    def test_reconnects_on_next_call(self):
        self.link.ping()
        self.idle()
        self.assertIsNone(self.link.handle)
        self.assertTrue(self.link.info().get('ok'))
        self.assertIsNotNone(self.link.handle)

    def test_not_closed_while_pinned(self):
        self.link.ping()
        with self.link.pin():
            self.idle()
            self.assertIsNotNone(self.link.handle)
        self.idle()
        self.assertIsNone(self.link.handle)

    def test_not_closed_while_hold_or_stop_open(self):
        self.link.one('gatecfg', fnv_link.word(self.host.t['timerDisable']), fnv_link.word(self.host.t['timerEnable']),
                      fnv_link.word(self.host.t['timerGlobal']))
        self.assertTrue(self.link.one('hold', 'on', fnv_link.word(60000)).get('held'))
        try:
            self.idle()
            self.assertIsNotNone(self.link.handle, 'released the pipe while the frame gate was held')
        finally:
            self.assertFalse(self.link.one('hold', 'off').get('held'))
        self.idle()
        self.assertIsNone(self.link.handle)

    def test_released_after_the_dead_man_opens_the_gate(self):
        # A hold with a 300 ms dead-man time and then silence: the companion opens the gate by itself, so the link
        # must not keep treating the pipe as held.
        self.link.one('gatecfg', fnv_link.word(self.host.t['timerDisable']), fnv_link.word(self.host.t['timerEnable']),
                      fnv_link.word(self.host.t['timerGlobal']))
        self.assertTrue(self.link.one('hold', 'on', fnv_link.word(300)).get('held'))
        self.idle()
        self.idle()
        self.assertIsNone(self.link.handle, 'the pipe stayed held after the dead-man switch opened the gate')

    def test_a_stop_that_parked_after_the_last_reply_keeps_the_pipe(self):
        # Task 6.4: a disconnect releases every stop, so the link asks once before an idle close. The stop here parks
        # on a worker thread after the console reply, so the link's last word was "nothing held".
        link = self.link
        hook = link.one('hook', fnv_link.word(self.host.t['hookme']), fnv_link.word(6), '-', '0', '0', '0', '0', '1',
                        '0', '0', '0', '0', fnv_link.hexs('idlehold'))['hook']['id']
        try:
            self.assertTrue(link.one('hookact', fnv_link.word(hook), 'hold', 'ff').get('ok'))
            link.console('hookthread 4')
            end = time.time() + 5
            while not link.one('stops')['stops'] and time.time() < end:
                time.sleep(0.02)
            link._holding = False                       # as if the last reply came before the stop parked
            self.idle()
            self.assertIsNotNone(link.handle, 'closed the pipe (and so released the stop) while a stop was parked')
            self.assertEqual(len(link.one('stops')['stops']), 1)
        finally:
            link.one('hookact', fnv_link.word(hook), '0', '0', '0', '0', '-')
            for stop in link.one('stops')['stops']:
                link.one('resume', fnv_link.word(stop['seq']), 'run')
            link.one('unhook', fnv_link.word(hook))

    def test_zero_disables(self):
        self.link.idle_release_seconds = 0
        self.link.ping()
        self.idle()
        self.assertIsNotNone(self.link.handle)


if __name__ == '__main__':
    unittest.main()
