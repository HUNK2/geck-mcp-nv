"""Client for the fnvmcp engine companion (native/engine/fnvmcp_engine.c), plus its build and install.

The companion serves one request line at a time over a named pipe; every request runs on the game's
main thread in the same frame. This module encodes requests, decodes the JSON replies, applies
timeouts with overlapped pipe I/O, and reconnects when the game restarts.
"""
import hashlib
import json
import os
import re
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

NATIVE = Path(__file__).resolve().parents[1] / 'native'
SOURCES = ('engine/fnvmcp_engine.c', 'runtime/nvse_abi.h')
DLL_NAME = 'fnvmcp_engine.dll'
MOD_NAME = 'fnvmcp engine companion'
DEFAULT_PIPE = 'fnvmcp-engine'
WIRE = 1

GENERIC_READ_WRITE = 0x80000000 | 0x40000000
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
ERROR_PIPE_BUSY = 231
ERROR_FILE_NOT_FOUND = 2
WAIT_TIMEOUT = 0x102


class LinkError(Exception):
    """The companion could not be reached or answered with an error."""

    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


def hexs(text):
    """Text argument on the wire: the line's bytes (cp1252, the game's code page) as hex."""
    return text.encode('cp1252', errors='strict').hex()


def word(value):
    return '%x' % (value & 0xFFFFFFFF)


class EngineLink:
    """Synchronous client: one request at a time, guarded by a lock. Reconnects on demand."""

    def __init__(self, pipe=None, timeout=15.0):
        self.pipe = r'\\.\pipe\%s' % (pipe or os.environ.get('FNVMCP_PIPE') or DEFAULT_PIPE)
        self.timeout = timeout
        self.handle = None
        self.lock = threading.RLock()
        self.hello = None

    # ------------------------------------------------------------------ connection
    def connect(self, wait=0.0):
        import _winapi
        deadline = time.monotonic() + wait
        while True:
            try:
                self.handle = _winapi.CreateFile(self.pipe, GENERIC_READ_WRITE, 0, 0, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, 0)
                break
            except OSError as e:
                if getattr(e, 'winerror', None) not in (ERROR_PIPE_BUSY, ERROR_FILE_NOT_FOUND) or time.monotonic() >= deadline:
                    raise LinkError('COMPANION_UNREACHABLE', 'Engine companion pipe %s is not available (%s)' % (self.pipe, e))
                if e.winerror == ERROR_PIPE_BUSY:
                    try:
                        _winapi.WaitNamedPipe(self.pipe, 500)
                    except OSError:
                        time.sleep(0.2)
                else:
                    time.sleep(0.25)
        self.hello = None
        return self

    def close(self):
        import _winapi
        if self.handle is not None:
            try:
                _winapi.CloseHandle(self.handle)
            except OSError:
                pass
        self.handle = None

    def _io(self, data, timeout):
        """Write one line and read one line back with a deadline (overlapped I/O, cancelled on timeout)."""
        import _winapi
        deadline = time.monotonic() + timeout
        try:
            ov, err = _winapi.WriteFile(self.handle, data, overlapped=True)
            if err == ERROR_IO_PENDING and _winapi.WaitForSingleObject(ov.event, int(timeout * 1000)) == WAIT_TIMEOUT:
                ov.cancel()
                raise LinkError('COMPANION_TIMEOUT', 'Write to the engine companion timed out')
            ov.GetOverlappedResult(True)
        except OSError as e:  # nothing was delivered: safe to retry on a new connection
            raise LinkError('NOT_SENT', 'Engine companion pipe closed before the request was sent: %s' % e)
        buffer = b''
        while not buffer.endswith(b'\n'):
            left = deadline - time.monotonic()
            if left <= 0:
                raise LinkError('COMPANION_TIMEOUT', 'No reply from the engine companion within %.1f s (main loop '
                                'stalled, loading, or the game is frozen)' % timeout)
            ov, err = _winapi.ReadFile(self.handle, 1 << 20, overlapped=True)
            if err == ERROR_IO_PENDING and _winapi.WaitForSingleObject(ov.event, int(left * 1000) + 1) == WAIT_TIMEOUT:
                ov.cancel()
                try:
                    ov.GetOverlappedResult(True)
                except OSError:
                    pass
                raise LinkError('COMPANION_TIMEOUT', 'No reply from the engine companion within %.1f s (main loop '
                                'stalled, loading, or the game is frozen)' % timeout)
            try:
                ov.GetOverlappedResult(True)
            except OSError as e:
                if getattr(e, 'winerror', None) != 234:  # ERROR_MORE_DATA: keep reading
                    raise
            buffer += ov.getbuffer()
        return buffer

    def _exchange(self, line, timeout):
        with self.lock:
            for attempt in (0, 1):
                if self.handle is None:
                    self.connect(wait=2.0 if attempt else 0.5)
                try:
                    reply = self._io(line.encode('ascii') + b'\n', timeout)
                    return json.loads(reply.decode('utf-8', errors='replace'))
                except LinkError as e:
                    self.close()
                    if e.code != 'NOT_SENT' or attempt:
                        raise
                except OSError as e:
                    # Sent but no complete reply: the request may have run, so never resend it.
                    self.close()
                    raise LinkError('COMPANION_DISCONNECTED', 'Engine companion pipe closed while waiting for the '
                                    'reply (the game may have quit or crashed; the request may have run): %s' % e)

    # ------------------------------------------------------------------ requests
    def ping(self, timeout=3.0):
        reply = self._exchange('%s\tping' % uuid.uuid4().hex[:12], timeout)
        self.hello = reply
        return reply

    def batch(self, ops, timeout=None):
        """Run ops (tuples: name, *args) in one request; all run on the main thread in one frame."""
        if not ops:
            return []
        parts = []
        for op in ops:
            text = ' '.join(str(part) for part in op)
            if '\t' in text or '\n' in text:
                raise ValueError('op text may not contain tabs or newlines')
            parts.append(text)
        request_id = uuid.uuid4().hex[:12]
        reply = self._exchange(request_id + '\t' + '\t'.join(parts), timeout or self.timeout)
        if reply.get('id') != request_id:
            self.close()
            raise LinkError('COMPANION_PROTOCOL', 'Reply for another request (%s)' % reply.get('id'))
        if reply.get('error'):
            raise LinkError('COMPANION_ERROR', reply['error'], reply)
        results = reply.get('results', [])
        if len(results) != len(ops):
            raise LinkError('COMPANION_PROTOCOL', 'Expected %d results, got %d' % (len(ops), len(results)))
        for result in results:
            result.setdefault('frame', reply.get('frame'))
        return results

    def one(self, *op, timeout=None):
        return self.batch([op], timeout)[0]

    # ------------------------------------------------------------------ typed helpers
    def info(self):
        return self.one('info')

    def read(self, address, length):
        result = self.one('rd', word(address), word(length))
        if not result.get('ok'):
            raise LinkError('MEMORY_UNREADABLE', 'Cannot read %d bytes at 0x%08X' % (length, address), result)
        return bytes.fromhex(result['hex'])

    def u32(self, address):
        return struct.unpack('<I', self.read(address, 4))[0]

    def write(self, address, data):
        result = self.one('wr', word(address), bytes(data).hex())
        if not result.get('ok'):
            raise LinkError('MEMORY_UNWRITABLE', 'Cannot write %d bytes at 0x%08X (%s)' % (len(data), address, result.get('error')), result)
        return bytes.fromhex(result['old'])

    def console(self, line, ref=0, timeout=None):
        return self.one('con', hexs(line), word(ref), timeout=timeout)

    def eval(self, expression, ref=0, timeout=None):
        return self.one('eval', hexs(expression), word(ref), timeout=timeout)

    def run(self, script, ref=0, timeout=None):
        return self.one('run', hexs(script), word(ref), timeout=timeout)

    def schedule(self, delay_ms, line):
        return self.one('sched', word(delay_ms), hexs(line))

    def events(self, since=0):
        return self.one('events', word(since))


def value_of(result):
    """Python value of an eval result: number, form id (hex string), string, list/dict, or None."""
    if not result or not result.get('ok'):
        return None
    element = result.get('result') or {}
    return element_value(element)


def element_value(element):
    kind = element.get('type')
    if kind == 'number':
        return element.get('value')
    if kind == 'form':
        return element.get('formId')
    if kind == 'string':
        return element.get('value')
    if kind == 'array':
        items = element.get('items') or []
        if element.get('container') in ('map', 'stringmap'):
            return {str(element_value(i['key'])): element_value(i['value']) for i in items}
        return [element_value(i) for i in items]
    return None


# ---------------------------------------------------------------------- build and install
def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_hashes():
    return {name: file_digest(NATIVE / name) for name in SOURCES}


def find_zig(project=None):
    fnv = getattr(project, 'fnv', None) if project else None
    for candidate in (getattr(fnv, 'zig', None), os.environ.get('GECK_MCP_ZIG')):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise LinkError('ZIG_MISSING', 'Zig 0.14.1 is needed to build the engine companion: set fnv.zig in the project '
                    'file or the GECK_MCP_ZIG environment variable')


def build(state_dir, zig, with_fake_host=False):
    """Compile the companion (and optionally its fake xNVSE host for tests) into state/engine/builds/<id>."""
    zig = Path(zig).resolve(strict=True)
    version = subprocess.run([str(zig), 'version'], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    if version != '0.14.1':
        raise LinkError('ZIG_VERSION', 'The engine companion is validated with Zig 0.14.1 only (found %s)' % version)
    identity = {'source': source_hashes(), 'wire': WIRE, 'compiler': version}
    build_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    folder = Path(state_dir) / 'engine/builds' / build_id
    folder.mkdir(parents=True, exist_ok=True)
    common = [str(zig), 'cc', '-target', 'x86-windows-gnu', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror']
    targets = [('engine/fnvmcp_engine.c', DLL_NAME, ['-shared'])]
    if with_fake_host:
        targets.append(('engine/fake_host.c', 'fnvmcp_fake_host.exe', []))
    for source, output, extra in targets:
        if not (folder / output).exists():
            result = subprocess.run(common + extra + [str(NATIVE / source), '-ladvapi32', '-o', str(folder / output)],
                                    capture_output=True, text=True, timeout=300)
            if result.returncode:
                raise LinkError('BUILD_FAILED', 'zig cc failed for %s' % source, {'stderr': result.stderr[-4000:]})
    receipt = {**identity, 'schema_version': 1, 'build_id': build_id, 'dll': str(folder / DLL_NAME),
               'sha256': file_digest(folder / DLL_NAME)}
    (folder / 'build.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return receipt


def install(mo2_root, profile, receipt, state_dir):
    """Install the DLL as its own MO2 mod in the dev profile (never into the game folder). Game must be closed."""
    mo2_root = Path(mo2_root)
    target_dir = mo2_root / 'mods' / MOD_NAME / 'NVSE/Plugins'
    target = target_dir / DLL_NAME
    source = Path(receipt['dll'])
    if file_digest(source) != receipt['sha256']:
        raise LinkError('BUILD_CHANGED', 'The built DLL changed after its build receipt was written')
    backup = None
    if target.exists():
        if file_digest(target) == receipt['sha256']:
            enable_mod(mo2_root, profile, MOD_NAME)
            return {'installed': True, 'unchanged': True, 'target': str(target)}
        backup = Path(state_dir) / 'engine/backups' / ('%d-%s' % (time.time(), DLL_NAME))
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(target.read_bytes())
    target_dir.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.tmp')
    temp.write_bytes(source.read_bytes())
    os.replace(temp, target)
    enable_mod(mo2_root, profile, MOD_NAME)
    record = {**receipt, 'target': str(target), 'installed_at': time.time(), 'backup': str(backup) if backup else None}
    path = Path(state_dir) / 'engine/install.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return {'installed': True, 'unchanged': False, 'target': str(target), 'backup': record['backup']}


def enable_mod(mo2_root, profile, mod):
    """Put '+mod' in the profile's modlist.txt (top = highest priority, after the header comment)."""
    path = Path(mo2_root) / 'profiles' / profile / 'modlist.txt'
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    names = {line[1:] for line in lines if line[:1] in '+-'}
    if '+' + mod in lines:
        return False
    header = [line for line in lines if line.startswith('#')]
    rest = [line for line in lines if not line.startswith('#')]
    if mod in names:
        rest = ['+' + mod if line == '-' + mod else line for line in rest]
    else:
        rest = ['+' + mod] + rest
    path.write_text('\n'.join(header + rest) + '\n', encoding='utf-8')
    return True


def disable_mod(mo2_root, profile, mod):
    path = Path(mo2_root) / 'profiles' / profile / 'modlist.txt'
    if not path.exists():
        return False
    lines = path.read_text(encoding='utf-8').splitlines()
    if '+' + mod not in lines:
        return False
    path.write_text('\n'.join('-' + mod if line == '+' + mod else line for line in lines) + '\n', encoding='utf-8')
    return True


SAFE_NAME = re.compile(r'^[A-Za-z0-9_. \'-]{1,80}$')
