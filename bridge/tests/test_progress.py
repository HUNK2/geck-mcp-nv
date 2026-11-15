"""Release plan Task 6.2 (audit H2): a long tool call tells the client it is alive. While a call runs, the server sends
an MCP progress notification every 5 seconds when the request carries a progress token (and nothing without one)."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

from geck_mcp import server as server_module  # noqa: E402
from geck_mcp.config import Project  # noqa: E402


def project(folder):
    path = Path(folder) / 'p.json'
    path.write_text(json.dumps({'project_id': 'progress', 'game_root': str(folder), 'state_dir': 'state'}),
                    encoding='utf-8')
    return Project.load(path)


def with_slow_tool(seconds):
    def build(folder):
        server = server_module.make_server(project(folder))

        @server.tool()
        async def fnv_slow_fake() -> str:
            """A test tool that takes a while."""
            await anyio.to_thread.run_sync(time.sleep, seconds)
            return 'done'
        return server
    return build


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_12_second_call_sends_at_least_two_notifications(self):
        self.assertEqual(server_module.PROGRESS_SECONDS, 5)
        seen = []

        async def progress(value, total, message):
            seen.append((value, message))

        with tempfile.TemporaryDirectory() as tmp:
            async with Client(with_slow_tool(12)(tmp)) as client:
                result = await client.call_tool('fnv_slow_fake', {}, progress_callback=progress)
        self.assertFalse(result.is_error)
        self.assertGreaterEqual(len(seen), 2, seen)
        self.assertTrue(all('fnv_slow_fake' in (m or '') for _, m in seen), seen)
        self.assertEqual([v for v, _ in seen], sorted(v for v, _ in seen))   # the elapsed seconds only go up

    async def test_a_short_call_sends_none(self):
        seen = []

        async def progress(value, total, message):
            seen.append(value)

        with tempfile.TemporaryDirectory() as tmp:
            async with Client(with_slow_tool(0.2)(tmp)) as client:
                await client.call_tool('fnv_slow_fake', {}, progress_callback=progress)
        self.assertEqual(seen, [])


if __name__ == '__main__':
    unittest.main()
