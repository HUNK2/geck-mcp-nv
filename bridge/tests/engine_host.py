"""Shared host for the engine companion tests: where Zig is, the skip decorator, and the fake xNVSE host.

The C tests used to be skipped unless GECK_MCP_ZIG was set, and nothing set it, so they never ran. zig_path() also
reads fnv.zig from the dev project files, so they run wherever the companion can be built."""
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_link  # noqa: E402

PROJECTS = Path(__file__).resolve().parents[1] / 'projects'


def zig_path(projects_dir: Path | str | None = None) -> str | None:
    """FNV_MCP_ZIG (or the old GECK_MCP_ZIG) if set, else fnv.zig from the first project file (sorted by name) whose zig file exists."""
    env = os.environ.get('FNV_MCP_ZIG') or os.environ.get('GECK_MCP_ZIG')
    if env:
        return env
    folder = Path(projects_dir) if projects_dir is not None else PROJECTS
    for path in sorted(folder.glob('*.json')):
        try:
            zig = (json.loads(path.read_text(encoding='utf-8')).get('fnv') or {}).get('zig')
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(zig, str) and zig:
            zig = os.path.expanduser(zig)
            candidate = Path(zig) if Path(zig).is_absolute() else path.parent / zig
            if candidate.is_file():
                return str(candidate)
    return None


def engine_zig(os_name: str = os.name, projects_dir: Path | str | None = None) -> str | None:
    """zig_path() where the companion tests can run: Windows only, since the fake host and the DLL are .exe/.dll."""
    return zig_path(projects_dir) if os_name == 'nt' else None


needs_zig = unittest.skipUnless(engine_zig(), 'needs Windows and Zig: set FNV_MCP_ZIG or fnv.zig in a projects/*.json file')


CODE_SECTIONS = (b'.text', b'.rdata', b'.data', b'.tls', b'.reloc')


def pe_code_digest(path) -> str:
    """sha256 over the code and data sections of a PE file. zig cc builds are not byte-reproducible (the COFF
    timestamp, the .buildid debug record and the DWARF line tables change), so "same DLL" means same code and data."""
    data = Path(path).read_bytes()
    pe = struct.unpack_from('<I', data, 0x3C)[0]
    count = struct.unpack_from('<H', data, pe + 6)[0]
    table = pe + 24 + struct.unpack_from('<H', data, pe + 20)[0]
    digest = hashlib.sha256()
    for i in range(count):
        name, _, _, raw_size, raw_at = struct.unpack_from('<8sIIII', data, table + 40 * i)
        name = name.rstrip(b'\0')
        if name in CODE_SECTIONS:
            digest.update(name + b'\0' + data[raw_at:raw_at + raw_size])
    return digest.hexdigest()


def hexint(value):
    return fnv_link.word(value)


class StopHost:
    """One fake host process and its pipe. The host is started per test class (or per test when it is expected to exit)."""

    def __init__(self, receipt, folder, tmp, seconds):
        self.pipe = 'fnvmcp-stops-' + uuid.uuid4().hex[:8]
        self.log = Path(tmp) / 'console.log'
        env = dict(os.environ, FNVMCP_PIPE=self.pipe, FNVMCP_CONSOLE_LOG=str(self.log),
                   LOCALAPPDATA=str(Path(tmp) / 'localappdata'))
        self.process = subprocess.Popen([str(folder / 'fnvmcp_fake_host.exe'), receipt['dll'], str(seconds)],
                                        stdout=subprocess.PIPE, env=env, text=True)
        self.t = {k: int(v, 16) for k, v in json.loads(self.process.stdout.readline()).items()}
        self.link = fnv_link.EngineLink(self.pipe).connect(wait=10)
        self.link.one('gatecfg', hexint(self.t['timerDisable']), hexint(self.t['timerEnable']),
                      hexint(self.t['timerGlobal']))

    def close(self):
        self.link.close()
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.process.stdout.close()

    def lines(self):
        if not self.log.exists():
            return []
        return self.log.read_text(encoding='utf-8', errors='replace').splitlines()


def build(tmp, **kwargs):
    return fnv_link.build(tmp, zig_path(), with_fake_host=True, **kwargs)


class StopTestCase(unittest.TestCase):
    """Shared helpers: wait for a stop, wait for a console line, and arm or disarm the hook hold."""

    def wait_stops(self, count=1, timeout=6.0):
        end = time.time() + timeout
        while True:
            stops = self.host.link.one('stops')['stops']
            if len(stops) >= count or time.time() > end:
                return stops
            time.sleep(0.02)

    def wait_line(self, text, timeout=6.0):
        end = time.time() + timeout
        while time.time() < end:
            if text in self.host.lines():
                return True
            time.sleep(0.02)
        return False

    def hold(self, hook_id):
        result = self.host.link.one('hookact', hexint(hook_id), 'hold', 'ff')
        self.assertTrue(result.get('ok'), result)

    def disarm(self, hook_id):
        self.host.link.one('hookact', hexint(hook_id), '0', '0', '0', '0', '-')
