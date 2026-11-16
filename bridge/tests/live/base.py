"""Live regression tests: the real tools against the running game (PLAN-3 S4.2).

Skipped unless FNV_LIVE=1 (tools/fnv_live_regress.py sets it for its own run). One in-process MCP server serves the
whole run, so there is one pipe user: its client lives on a background event loop and every test calls through it.
The project is FNV_LIVE_PROJECT (default projects/hellowasteland.windows.json); the game must already run in its
MO2 dev profile (launch it with fnv_game launch, then let the MCP server go idle so the pipe is free)."""
import asyncio
import json
import os
import sys
import threading
import unittest
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRIDGE))

LIVE = os.environ.get('FNV_LIVE') == '1'
DEFAULT_PROJECT = BRIDGE / 'projects' / 'hellowasteland.windows.json'
# A base form every test may place and remove: Caps001 (FalloutNV.esm), a misc item with no script.
CAPS = '0000000F'


class Session:
    """The one server and client of a run, on its own event loop thread."""
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls, project_path=None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(project_path or os.environ.get('FNV_LIVE_PROJECT') or DEFAULT_PROJECT)
            return cls._instance

    def __init__(self, project_path, engine_control=None):
        from geck_mcp.config import Project
        self.project_path = project_path
        self.project = Project.load(project_path)
        if engine_control is not None:
            fnv = self.project.fnv.model_copy(update={'engine_control': engine_control})
            self.project = self.project.model_copy(update={'fnv': fnv})
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.stop = None
        self.client = None
        self.error = None
        threading.Thread(target=self._run, daemon=True, name='fnv-live-session').start()
        if not self.ready.wait(120):
            raise RuntimeError('the in-process MCP server did not start')
        if self.error:
            raise self.error

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())

    async def _serve(self):
        from mcp import Client
        from geck_mcp.server import make_server
        try:
            self.stop = asyncio.Event()
            async with Client(make_server(self.project)) as client:
                self.client = client
                self.ready.set()
                await self.stop.wait()
        except Exception as e:  # noqa: BLE001 - reported to the waiting test
            self.error = e
            self.ready.set()

    def call(self, tool, args=None, timeout=600):
        future = asyncio.run_coroutine_threadsafe(self.client.call_tool(tool, args or {}), self.loop)
        result = future.result(timeout)
        envelope = result.structured_content
        if envelope is None:            # an argument validation error from the MCP layer: no envelope
            text = ''.join(getattr(c, 'text', '') for c in result.content)
            envelope = {'ok': False, 'error': {'code': 'INVALID_PARAMS', 'message': text}, 'data': None}
        envelope['_size'] = sum(len(getattr(c, 'text', '') or '') for c in result.content)
        return envelope

    def close(self):
        if self.stop is not None:
            self.loop.call_soon_threadsafe(self.stop.set)


@unittest.skipUnless(LIVE, 'live test: set FNV_LIVE=1 with the game running (tools/fnv_live_regress.py)')
class LiveCase(unittest.TestCase):
    """call() returns the tool's envelope; ok() asserts ok and returns data. tearDown reverts this process's changes
    and checks that no hook is left in the game."""

    session = None

    @classmethod
    def setUpClass(cls):
        cls.session = Session.get()

    def call(self, tool, **args):
        return self.session.call(tool, args)

    def ok(self, tool, **args):
        envelope = self.call(tool, **args)
        self.assertTrue(envelope.get('ok'), '%s %s -> %s' % (tool, args, json.dumps(envelope.get('error'))[:500]))
        return envelope.get('data')

    def code(self, tool, **args):
        envelope = self.call(tool, **args)
        return (envelope.get('error') or {}).get('code')

    def tearDown(self):
        reverted = self.call('fnv_undo', all=True)
        self.assertTrue(reverted.get('ok'), reverted.get('error'))
        hooks = self.ok('fnv_hooks')['hooks']
        self.assertEqual(hooks, [], 'hooks left in the game after the test')
