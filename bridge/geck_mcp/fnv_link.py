"""Client for the fnvmcp engine companion (native/engine/fnvmcp_engine.c), plus its build and install.

The companion serves one request line at a time over a named pipe; every request runs on the game's
main thread in the same frame. This module encodes requests, decodes the JSON replies, applies
timeouts with overlapped pipe I/O, and reconnects when the game restarts.
"""
import hashlib
import json
from contextlib import contextmanager
import os
import re
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import envvars
from .fnv_state import atomic_write, locked
from .esp import codec as _codec  # noqa: F401,E402 - registers the cp1252-bytes error handler

NATIVE = Path(__file__).resolve().parents[1] / 'native'
SOURCES = ('engine/fnvmcp_engine.c', 'engine/w1_pred.c', 'engine/w1_gate.c', 'engine/w1_samp.c', 'engine/w1_sink.c',
           'engine/w1_guard.c', 'engine/w1_input.c', 'engine/w2_stop.c', 'engine/w2_bp.c', 'engine/w5_mod.c',
           'engine/w5_code.c', 'engine/w6_cov.c', 'engine/fake_host.c', 'runtime/nvse_abi.h', 'sdk/fnvmcp_module.h')
DLL_NAME = 'fnvmcp_engine.dll'
MOD_NAME = 'fnvmcp engine companion'
DEFAULT_PIPE = 'fnvmcp-engine'
WIRE = 2

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


def quoted(text):
    """text as a quoted xNVSE string argument. A double quote or a line break would end the string and let the rest
    run as script (W4), so they are refused."""
    text = str(text)
    if '"' in text or '\n' in text or '\r' in text:
        raise ValueError('a name in a script expression cannot contain a double quote or a line break: %r' % text)
    return '"%s"' % text


def hexs(text):
    """Text argument on the wire: the line's bytes (cp1252, the game's code page) as hex."""
    return text.encode('cp1252', 'cp1252-bytes').hex()     # cp1252's undefined bytes pass through (R16 review)


def word(value):
    return '%x' % (value & 0xFFFFFFFF)


class EngineLink:
    """Synchronous client: one request at a time, guarded by a lock. Reconnects on demand.

    The game's pipe takes one client at a time. After idle_release_seconds without a request the link closes its
    handle, so another client (the live regression runner, a second server) can attach; the next request reconnects
    (PLAN-3 S4.1). It stays open while a request runs, while pinned (pin()), and while the companion last said the
    frame gate is held or a thread is parked at a stop. 0 never releases."""

    idle_release_seconds = 20.0

    DEFAULT_TIMEOUT = 15.0

    def __init__(self, pipe=None, timeout=DEFAULT_TIMEOUT):
        self.pipe = r'\\.\pipe\%s' % (pipe or envvars.get('FNV_MCP_PIPE') or DEFAULT_PIPE)
        self.timeout = timeout
        self.handle = None
        self.lock = threading.RLock()
        self.hello = None
        self.last_frame = None      # frame of the last reply on this connection (identity without a ping)
        self._last_use = time.monotonic()
        self._pins = 0
        self._holding = False       # the companion's last word: gate held or a stop parked
        self._hold_timeout = 120.0  # its dead-man time: with no request for this long it lets go by itself
        self._idle_thread = None

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
                    self.hello = None  # the game this hello came from is gone (or not answering): never report it
                    raise LinkError('COMPANION_UNREACHABLE', 'Engine companion pipe %s is not available (%s)' % (self.pipe, e))
                if e.winerror == ERROR_PIPE_BUSY:
                    try:
                        _winapi.WaitNamedPipe(self.pipe, 500)
                    except OSError:
                        time.sleep(0.2)
                else:
                    time.sleep(0.25)
        self.hello = None
        self._last_use = time.monotonic()
        if self._idle_thread is None:          # cleared by the idle thread under the lock when it ends
            self._idle_thread = threading.Thread(target=self._idle_loop, daemon=True, name='fnvmcp-link-idle')
            self._idle_thread.start()
        return self

    @contextmanager
    def pin(self):
        """Keep the pipe open while the block runs (a sequence of calls another client must not interleave with)."""
        with self.lock:
            self._pins += 1
        try:
            yield self
        finally:
            with self.lock:
                self._pins -= 1
                self._last_use = time.monotonic()

    def _held(self):
        """The gate is held or a stop parked, as far as the last reply said, and the companion's dead-man switch has
        not let go since (it counts from the last request)."""
        return self._holding and time.monotonic() - self._last_use < self._hold_timeout

    def _idle_loop(self):
        while True:
            limit = self.idle_release_seconds
            time.sleep(min(1.0, limit / 4) if limit and limit > 0 else 1.0)
            with self.lock:                             # ends only under the lock, so connect() never misses it
                if self.handle is None:
                    self._idle_thread = None
                    return
            if not limit or limit <= 0 or self._pins or self._held():
                continue
            if time.monotonic() - self._last_use < limit:
                continue
            if self.lock.acquire(blocking=False):      # never while a request runs
                try:
                    if (self.handle is not None and not self._pins and not self._held()
                            and time.monotonic() - self._last_use >= limit and not self._parked_since()):
                        self.close()
                finally:
                    self.lock.release()

    def _parked_since(self):
        """Before an idle close, while the last reply said nothing was held: ask once. Closing the pipe makes the
        companion let go of everything (Task 6.4), and a stop may have parked after that reply (a hook hold hit
        later). If something is held now, the pipe stays open until the companion's own dead-man time from now."""
        if self._holding:
            return False
        try:
            self.ping()
        except LinkError:
            return False
        if self._holding:
            self._last_use = time.monotonic()
            return True
        return False

    def _note(self, results):
        """Track whether the companion holds the gate or parks a stop (then the pipe is not released when idle)."""
        for r in results:
            if isinstance(r, dict) and 'held' in r:
                self._holding = bool(r.get('held')) or bool(r.get('parked'))
                if r.get('held') and isinstance(r.get('timeout'), (int, float)) and r['timeout'] > 0:
                    self._hold_timeout = r['timeout'] / 1000.0

    def close(self):
        import _winapi
        if self.handle is not None:
            try:
                _winapi.CloseHandle(self.handle)
            except OSError:
                pass
        self.handle = None
        self.hello = None  # a closed link no longer knows which game it talks to; the next ping says again
        self.last_frame = None
        self._holding = False

    def _io(self, data, timeout):
        """Write one line and read one line back with a deadline (overlapped I/O, cancelled on timeout)."""
        import _winapi
        deadline = time.monotonic() + timeout
        try:
            ov, err = _winapi.WriteFile(self.handle, data, overlapped=True)
            if err == ERROR_IO_PENDING and _winapi.WaitForSingleObject(ov.event, int(timeout * 1000)) == WAIT_TIMEOUT:
                ov.cancel()
                try:  # wait for the cancel to land before the handle can be closed (as the read path does)
                    ov.GetOverlappedResult(True)
                except OSError:
                    pass
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
                    self._last_use = time.monotonic()
                    return json.loads(reply.decode('utf-8', errors='replace'))
                except LinkError as e:
                    self.close()
                    if e.code != 'NOT_SENT' or attempt:
                        raise
                except OSError as e:
                    # Sent but no complete reply: the request may have run, so never resend it.
                    self.close()
                    self.hello = None
                    raise LinkError('COMPANION_DISCONNECTED', 'Engine companion pipe closed while waiting for the '
                                    'reply (the game may have quit or crashed; the request may have run): %s' % e)

    # ------------------------------------------------------------------ requests
    def ping(self, timeout=3.0):
        reply = self._exchange('%s\tping' % uuid.uuid4().hex[:12], timeout)
        self.hello = reply
        self._note([reply])
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
        self.last_frame = reply.get('frame', self.last_frame)
        if reply.get('error'):
            raise LinkError('COMPANION_ERROR', reply['error'], reply)
        results = reply.get('results', [])
        if len(results) != len(ops):
            raise LinkError('COMPANION_PROTOCOL', 'Expected %d results, got %d' % (len(ops), len(results)))
        for result in results:
            result.setdefault('frame', reply.get('frame'))
        self._note(results)
        return results

    # Set by LiveService: every hook the companion installs or removes, whichever tool sent it, reaches the journal
    # (PLAN-3 S3.6). A failing callback never fails the op.
    on_hook = None
    on_unhook = None

    def one(self, *op, timeout=None):
        result = self.batch([op], timeout)[0]
        if op and op[0] in ('hook', 'unhook') and result.get('ok'):
            notify = self.on_hook if op[0] == 'hook' else self.on_unhook
            if notify is not None:
                try:
                    notify(result.get('hook') or {} if op[0] == 'hook' else int(str(op[1]), 16))
                except Exception:  # noqa: BLE001 - the journal is a record; the game change already happened
                    import logging
                    logging.getLogger(__name__).warning('journaling the %s op failed', op[0], exc_info=True)
        return result

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
    """Digests of every file the companion is built from: the listed ones and any other C file in engine/ (a
    file left out would let a change reuse a stale build)."""
    names = set(SOURCES) | {'engine/%s' % p.name for p in (NATIVE / 'engine').glob('*.c')}
    return {name: file_digest(NATIVE / name) for name in sorted(names)}


def find_zig(project=None):
    fnv = getattr(project, 'fnv', None) if project else None
    for candidate in (getattr(fnv, 'zig', None), envvars.get('FNV_MCP_ZIG')):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise LinkError('ZIG_MISSING', 'Zig 0.14.1 is needed to build the engine companion: set fnv.zig in the project '
                    'file or the FNV_MCP_ZIG environment variable')


COMPILE_FLAGS = ('cc', '-target', 'x86-windows-gnu', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror')


def compile_target(zig, native, source, output, extra, defines=()):
    """One zig cc run with the companion's flags: native/<source> -> output. Raises BUILD_FAILED with the stderr."""
    command = [str(zig), *COMPILE_FLAGS, *['-D' + d for d in defines], *extra, str(Path(native) / source),
               '-ladvapi32', '-o', str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise LinkError('BUILD_FAILED', 'zig cc failed for %s' % source, {'stderr': result.stderr[-4000:]})


def check_defines(defines):
    """Build defines as a sorted list of C identifiers (ASCII letters, digits, _; not starting with a digit). A single
    string is one define, not a list of letters."""
    if isinstance(defines, str):
        defines = [defines]
    out = sorted(set(defines))
    for name in out:
        if not (isinstance(name, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)):
            raise LinkError('BAD_DEFINE', 'A build define must be a C identifier (got %r)' % (name,))
    return out


def build(state_dir, zig, with_fake_host=False, defines=()):
    """Compile the companion (and optionally its fake xNVSE host for tests) into state/engine/builds/<id>.

    defines are preprocessor names passed as -D<name> (tests use FNVMCP_TEST_SEAM for the race seams). They are part
    of the build identity, so a seam build gets its own folder; with none, the identity (and build id) is unchanged.
    A seam build must never be installed: install() refuses a receipt that has defines."""
    zig = Path(zig).resolve(strict=True)
    version = subprocess.run([str(zig), 'version'], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    if version != '0.14.1':
        raise LinkError('ZIG_VERSION', 'The engine companion is validated with Zig 0.14.1 only (found %s)' % version)
    defines = check_defines(defines)
    identity = {'source': source_hashes(), 'wire': WIRE, 'compiler': version}
    if defines:
        identity['defines'] = defines
    build_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    folder = Path(state_dir) / 'engine/builds' / build_id
    folder.mkdir(parents=True, exist_ok=True)
    targets = [('engine/fnvmcp_engine.c', DLL_NAME, ['-shared'])]
    if with_fake_host:
        targets.append(('engine/fake_host.c', 'fnvmcp_fake_host.exe', []))
    for source, output, extra in targets:
        if not (folder / output).exists():
            compile_target(zig, NATIVE, source, folder / output, extra, defines)
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
    if receipt.get('defines'):
        raise LinkError('TEST_BUILD', 'A build with defines %s is a test build and is never installed' % receipt['defines'])
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


def profile_locked(mo2_root, profile, timeout=30.0):
    """The lock every edit of an MO2 profile's lists (modlist.txt, plugins.txt, loadorder.txt) holds: two server
    processes editing one profile keep both changes (S2.4)."""
    return locked(Path(mo2_root) / 'profiles' / profile / 'modlist.txt', timeout)


def enable_mod(mo2_root, profile, mod):
    """Put '+mod' in the profile's modlist.txt (top = highest priority, after the header comment)."""
    path = Path(mo2_root) / 'profiles' / profile / 'modlist.txt'
    with profile_locked(mo2_root, profile):
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
        atomic_write(path, '\n'.join(header + rest) + '\n')
        return True


def disable_mod(mo2_root, profile, mod):
    path = Path(mo2_root) / 'profiles' / profile / 'modlist.txt'
    with profile_locked(mo2_root, profile):
        if not path.exists():
            return False
        lines = path.read_text(encoding='utf-8').splitlines()
        if '+' + mod not in lines:
            return False
        atomic_write(path, '\n'.join('-' + mod if line == '+' + mod else line for line in lines) + '\n')
        return True


SAFE_NAME = re.compile(r'^[A-Za-z0-9_. \'-]{1,80}$')
