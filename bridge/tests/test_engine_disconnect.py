"""Release plan Task 6.4 (audit H4): the companion lets go when the bridge leaves, over the real DLL and the fake host.

- A killed bridge: a held gate opens, a stopped thread runs on and a running step ends, within two seconds (not at
  the 120 s dead-man time), and the pipe takes the next client.
- A pipe that cannot be created is written to companion.log once (then at most once a minute).
- Faults the game recovers from do not use up the crash-context files.
- A hook relocation offset near 2^32 is refused; code_write puts back each page's own protection.
Skipped without Zig."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/: engine_host
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine_host import StopHost, StopTestCase, build, hexint, needs_zig, zig_path  # noqa: E402
from geck_mcp import fnv_link  # noqa: E402

BRIDGE = Path(__file__).resolve().parents[1]
SDK = BRIDGE / 'native' / 'sdk'
PROLOGUE_LEN = 6

# A stand-in for the bridge process: connects, sends the ops given as JSON, says "ready", then waits to be killed.
CHILD = r'''
import json, sys, threading, time
sys.path.insert(0, sys.argv[1])
from geck_mcp import fnv_link
link = fnv_link.EngineLink(sys.argv[2]).connect(wait=10)
link.idle_release_seconds = 0
for op in json.loads(sys.argv[3]):
    link.one(*op)
background = json.loads(sys.argv[4])
if background:
    threading.Thread(target=lambda: link.one(*background, timeout=600), daemon=True).start()
    time.sleep(0.5)
print('ready', flush=True)
time.sleep(600)
'''

PAGES_MODULE = r'''
#include <windows.h>
#include "fnvmcp_module.h"
FNVMCP_EXPORT int fnvmcp_module_init(const FnvmcpApi *api) {
    unsigned char *p = VirtualAlloc(NULL, 0x2000, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    DWORD old;
    MEMORY_BASIC_INFORMATION one, two;
    unsigned char bytes[4] = {1, 2, 3, 4};
    if (!p || !VirtualProtect(p + 0x1000, 0x1000, PAGE_EXECUTE_READ, &old)) return 3;
    if (api->write((uint32_t)(uintptr_t)(p + 0xFFE), bytes, 4) != 1) return 4;
    if (!VirtualQuery(p, &one, sizeof(one)) || !VirtualQuery(p + 0x1000, &two, sizeof(two))) return 5;
    if (one.Protect != PAGE_READWRITE) return 6;
    if (two.Protect != PAGE_EXECUTE_READ) return 7;
    if (p[0xFFE] != 1 || p[0x1001] != 4) return 8;
    return 0;
}
'''


def crash_dir(tmp):
    return Path(tmp) / 'localappdata' / 'fnvmcp'


@needs_zig
class DisconnectTests(StopTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.receipt = build(cls.tmp.name)
        (Path(cls.tmp.name) / 'localappdata').mkdir()
        cls.host = StopHost(cls.receipt, Path(cls.receipt['dll']).parent, cls.tmp.name, 600)
        target = hexint(cls.host.t['hookme'])
        result = cls.host.link.one('hook', target, hexint(PROLOGUE_LEN), '-', hexint(0), hexint(0), hexint(0),
                                   hexint(0), hexint(1), '0', '0', hexint(0), hexint(0), fnv_link.hexs('hookme'))
        cls.hook_id = result['hook']['id']

    @classmethod
    def tearDownClass(cls):
        cls.host.close()
        cls.tmp.cleanup()

    def tearDown(self):
        link = self.host.link
        self.disarm(self.hook_id)
        for stop in link.one('stops')['stops']:
            link.one('resume', hexint(stop['seq']), 'run')
        link.one('hold', 'off')

    def kill_bridge_after(self, ops, background=None):
        """A bridge process sends ops (and starts one more op it never sees answered), then is killed."""
        self.host.link.close()                 # the pipe takes one client
        script = Path(self.tmp.name) / 'child.py'
        script.write_text(CHILD, encoding='utf-8')
        child = subprocess.Popen([sys.executable, str(script), str(BRIDGE), self.host.pipe, json.dumps(ops),
                                  json.dumps(background or [])], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
        finally:
            child.kill()
            child.wait()
            child.stdout.close()
        return time.monotonic()

    def assert_running_within(self, killed, seconds=2.0):
        link = self.host.link
        link.connect(wait=seconds)
        while True:
            ping = link.ping()
            if not ping['held'] and not ping['parked'] and not ping['stepLeft']:
                break
            self.assertLess(time.monotonic() - killed, seconds, ping)
            time.sleep(0.05)
        before = link.info()['frame']
        time.sleep(0.1)
        self.assertGreater(link.info()['frame'], before)
        self.assertLess(time.monotonic() - killed, seconds + 1)

    def test_a_killed_bridge_opens_a_held_gate(self):
        killed = self.kill_bridge_after([['hold', 'on', hexint(60000)]])
        self.assert_running_within(killed)
        events = self.host.link.events(0)['events']
        self.assertTrue(any(e['type'] == 'disconnect' and 'gate opened' in e['text'] for e in events), events)

    def test_a_killed_bridge_releases_a_stopped_thread(self):
        self.hold(self.hook_id)
        self.host.link.console('hookthread 4')
        self.assertEqual(len(self.wait_stops()), 1)
        killed = self.kill_bridge_after([])
        self.assert_running_within(killed)
        self.assertTrue(self.wait_line('hookthread 1004', timeout=2))
        self.assertEqual(self.host.link.one('stops')['stops'], [])

    def test_a_killed_bridge_ends_a_running_step(self):
        killed = self.kill_bridge_after([['hold', 'on', hexint(60000)]], background=['step', hexint(0x2710)])
        self.assert_running_within(killed)
        self.assertEqual(self.host.link.one('gate')['stepLeft'], 0)

    def test_a_relocation_offset_near_2_32_is_refused(self):
        for offset in ('fffffffe', 'fffffffc', '3'):     # 3 + 4 > 6 stolen bytes
            with self.subTest(offset=offset):
                result = self.host.link.one('hook', hexint(self.host.t['bpTarget']), hexint(PROLOGUE_LEN), offset,
                                            hexint(0), hexint(0), hexint(0), hexint(0), hexint(1), '0', '0',
                                            hexint(0), hexint(0), fnv_link.hexs('reloc'))
                self.assertEqual(result.get('error'), 'bad_relocation', result)

    def test_code_write_keeps_each_pages_protection(self):
        src = Path(self.tmp.name) / 'pages.c'
        src.write_text(PAGES_MODULE, encoding='utf-8')
        out = Path(self.tmp.name) / 'pages.dll'
        subprocess.run([zig_path(), 'cc', '-target', 'x86-windows-gnu', '-shared', '-O2', '-I', str(SDK),
                        '-o', str(out), str(src)], check=True, capture_output=True, timeout=120)
        loaded = self.host.link.one('mod', 'load', fnv_link.hexs(str(out)))
        self.assertTrue(loaded.get('ok'), loaded)
        self.host.link.one('mod', 'unload', hexint(loaded['module']['id']))


@needs_zig
class PipeAndCrashTests(StopTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.receipt = build(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_a_pipe_that_cannot_be_created_is_logged_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'localappdata').mkdir()
            self.host = StopHost(self.receipt, Path(self.receipt['dll']).parent, tmp, 300)
            second = Path(tmp) / 'second'
            second.mkdir()
            try:
                env = dict(os.environ, FNVMCP_PIPE=self.host.pipe, FNVMCP_CONSOLE_LOG=str(second / 'console.log'),
                           LOCALAPPDATA=str(second))
                folder = Path(self.receipt['dll']).parent
                other = subprocess.Popen([str(folder / 'fnvmcp_fake_host.exe'), self.receipt['dll'], '300'],
                                         stdout=subprocess.PIPE, env=env, text=True)
                try:
                    other.stdout.readline()
                    time.sleep(3.5)                # the pipe thread retries every second
                    log = second / 'fnvmcp' / 'companion.log'
                    lines = log.read_text(encoding='utf-8').splitlines() if log.exists() else []
                finally:
                    other.kill()
                    other.wait()
                    other.stdout.close()
                self.assertEqual(len(lines), 1, lines)
                self.assertIn('cannot create the pipe', lines[0])
                self.assertIn(self.host.pipe, lines[0])
                self.assertTrue(self.host.link.ping().get('pong'))   # the first game keeps its pipe
            finally:
                self.host.close()

    def test_recovered_faults_leave_room_for_the_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'localappdata').mkdir()
            self.host = StopHost(self.receipt, Path(self.receipt['dll']).parent, tmp, 300)
            try:
                self.host.link.console('handled 20')     # 20 faults the host recovers from, all at one address
                self.assertTrue(self.wait_line('handled 20'))
                files = sorted(crash_dir(tmp).glob('crash-context-*.json'))
                self.assertEqual(len(files), 1, files)
                self.host.link.one('faulthold', 'off')
                self.host.link.schedule(0, 'crash')
                self.assertEqual(self.host.process.wait(timeout=10), 77)
                codes = [json.loads(p.read_text(encoding='utf-8'))['code']
                         for p in crash_dir(tmp).glob('crash-context-*.json')]
                self.assertIn('0xc0000005', codes)
                self.assertEqual(len(codes), 2)
            finally:
                self.host.close()


if __name__ == '__main__':
    unittest.main()
