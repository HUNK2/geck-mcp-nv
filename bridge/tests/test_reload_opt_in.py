"""Release plan Task 6.1 (audit H3): run_mcp.py serves directly by default; the hot-reload supervisor is opt-in with
--reload (or FNV_MCP_RELOAD=1). The supervisor marks itself by declaring tools.listChanged in the initialize result
(it is the one that sends the change notices); the server alone does not."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
INITIALIZE = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
              'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                         'clientInfo': {'name': 'test', 'version': '1'}}}


def initialize(extra=(), env_update=None):
    """The initialize result of a run_mcp.py process, which then gets end of input and exits."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / 'p.json'
        project.write_text(json.dumps({'project_id': 'reload', 'game_root': tmp, 'state_dir': 'state'}),
                           encoding='utf-8')
        env = {k: v for k, v in os.environ.items() if k not in ('FNV_MCP_RELOAD', 'GECK_MCP_RELOAD')}
        env['FNV_MCP_SUPERVISOR_LOG'] = ''
        env.update(env_update or {})
        proc = subprocess.Popen([sys.executable, 'run_mcp.py', '--project', str(project), *extra], cwd=str(BRIDGE),
                                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding='utf-8')
        try:
            proc.stdin.write(json.dumps(INITIALIZE) + '\n')
            proc.stdin.flush()
            while True:
                line = proc.stdout.readline()
                if not line:
                    raise AssertionError('no initialize answer')
                message = json.loads(line)
                if message.get('id') == 1:
                    break
            proc.stdin.close()
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
        return message['result']


def supervised(result):
    return bool(result.get('capabilities', {}).get('tools', {}).get('listChanged'))


class ReloadOptInTests(unittest.TestCase):
    def test_default_serves_without_a_supervisor(self):
        self.assertFalse(supervised(initialize()))

    def test_reload_flag_starts_the_supervisor(self):
        self.assertTrue(supervised(initialize(['--reload'])))

    def test_reload_environment_variable_starts_it_too(self):
        self.assertTrue(supervised(initialize(env_update={'FNV_MCP_RELOAD': '1'})))
        self.assertTrue(supervised(initialize(env_update={'GECK_MCP_RELOAD': '1'})))

    def test_no_reload_still_accepted(self):
        self.assertFalse(supervised(initialize(['--no-reload'])))


if __name__ == '__main__':
    unittest.main()
