"""Release plan Phase 7 (L3): the engine link checks which process serves the companion pipe. The game object
accepts only the running FalloutNV.exe, so another program holding the pipe name is never sent requests."""
import os
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_game, fnv_link  # noqa: E402


@unittest.skipUnless(sys.platform == 'win32', 'named pipes')
class PipeServerCheckTests(unittest.TestCase):
    def serve(self):
        """A pipe served by this process; returns its short name."""
        import _winapi
        name = 'fnvmcp-test-%s' % uuid.uuid4().hex[:8]
        handle = _winapi.CreateNamedPipe(r'\\.\pipe\%s' % name, 0x3 | 0x40000000, 0, 1, 4096, 4096, 0, 0)

        def accept():
            try:
                _winapi.ConnectNamedPipe(handle, 0)
            except OSError:
                pass
        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        self.addCleanup(_winapi.CloseHandle, handle)
        return name

    def test_the_check_sees_the_server_pid(self):
        seen = []
        link = fnv_link.EngineLink(self.serve())
        link.server_check = seen.append
        link.connect(wait=2)
        self.addCleanup(link.close)
        self.assertEqual(seen, [os.getpid()])
        self.assertIsNotNone(link.handle)

    def test_a_refused_server_is_let_go(self):
        link = fnv_link.EngineLink(self.serve())

        def refuse(pid):
            raise fnv_link.LinkError('PIPE_NOT_GAME', 'not the game')
        link.server_check = refuse
        with self.assertRaises(fnv_link.LinkError) as caught:
            link.connect(wait=2)
        self.assertEqual(caught.exception.code, 'PIPE_NOT_GAME')
        self.assertIsNone(link.handle)


class GamePipeServerTests(unittest.TestCase):
    def game(self, running):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = SimpleNamespace(fnv=SimpleNamespace(mo2_root=tmp.name, mo2_profile='Dev', engine_pipe=None),
                                  game_root=tmp.name, state_dir=tmp.name)
        patch = mock.patch.object(fnv_game, 'game_processes', lambda: running)
        patch.start()
        self.addCleanup(patch.stop)
        return fnv_game.Game(project)

    def test_the_game_installs_the_check(self):
        game = self.game([(4242, 1)])
        self.assertIsNotNone(game.link.server_check)
        game.link.server_check(4242)                       # the running game: accepted

    def test_another_process_is_refused(self):
        game = self.game([(4242, 1)])
        with self.assertRaises(fnv_link.LinkError) as caught:
            game.link.server_check(999)
        self.assertEqual(caught.exception.code, 'PIPE_NOT_GAME')
        self.assertIn('999', str(caught.exception))

    def test_no_game_running_is_refused(self):
        game = self.game([])
        with self.assertRaises(fnv_link.LinkError) as caught:
            game.link.server_check(999)
        self.assertEqual(caught.exception.code, 'PIPE_NOT_GAME')


if __name__ == '__main__':
    unittest.main()
