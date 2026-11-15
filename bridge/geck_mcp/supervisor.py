"""Stdio supervisor: keeps the client's connection while the server process is reloaded.

The MCP client (the Claude app, any stdio client) starts this process once. It runs the real server as a child
process and passes every JSON-RPC line through unchanged. When a watched source file changes, it waits until no
call is in flight, starts a fresh child with the new code, makes it ready, swaps it in, and tells the client that
the tool list changed:

- handshake era (the client sent `initialize`): the stored `initialize` and `notifications/initialized` are replayed
  to the new child, and `notifications/tools/list_changed` goes to the client. The `initialize` result is patched to
  declare `tools.listChanged`, so the client knows to expect it;
- 2026-07-28 era: requests are self-contained, so the new child needs no handshake (a `tools/list` with the client's
  envelope opens the same era and proves the new code serves). The supervisor serves `subscriptions/listen` itself,
  so the streams outlive a child, and sends the stamped change notifications on them.

A child that exits on its own (a crash, a kill) fails the calls it had in flight with an error that says so, and
the next request starts a new one. A new child that fails to start (a syntax error in the edited code) leaves the
old one serving; the error goes to stderr (the client's MCP log) and the reload is retried on the next change.

Only the standard library is used here, so a broken edit elsewhere in the package cannot stop the supervisor.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SUBSCRIPTION_ID = 'io.modelcontextprotocol/subscriptionId'
LIST_CHANGED = {'toolsListChanged': 'notifications/tools/list_changed',
                'promptsListChanged': 'notifications/prompts/list_changed',
                'resourcesListChanged': 'notifications/resources/list_changed'}
WATCH_SUFFIXES = ('.py', '.md', '.json', '.txt')
OWN_ID = 'geck-supervisor-'


LOG_FILE = None                               # state/supervisor.log
LOG_LIMIT = 256 * 1024


def note(message):
    """Routine events: the log file only (the Claude app shows any server stderr as an error)."""
    log(message, stderr=False)


def log(message, stderr=True):
    """Problems: stderr (the client's MCP log) and the log file."""
    line = f'[geck-supervisor] {message}\n'
    if stderr:
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
    if LOG_FILE is not None:
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_LIMIT:
                LOG_FILE.replace(LOG_FILE.with_suffix('.log.1'))
            with LOG_FILE.open('a', encoding='utf-8') as f:
                f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + f'pid {os.getpid()} ' + line)
        except OSError:
            pass


class Watcher:
    """Content fingerprint of the watched files. A cheap stat pass first; contents hashed only when stats move,
    so a touch without a change (a checkout of the same commit) does not reload."""

    def __init__(self, paths):
        self.paths = [Path(p) for p in paths]
        self._stats = None
        self._hash = None

    def files(self):
        for root in self.paths:
            if root.is_file():
                yield root
                continue
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d != '__pycache__' and not d.startswith('.')]
                for name in filenames:
                    if name.endswith(WATCH_SUFFIXES):
                        yield Path(dirpath) / name

    def stats(self):
        out = []
        for f in self.files():
            try:
                st = f.stat()
            except OSError:
                continue
            out.append((str(f), st.st_size, st.st_mtime_ns))
        return sorted(out)

    def fingerprint(self):
        stats = self.stats()
        if stats == self._stats:
            return self._hash
        digest = hashlib.sha256()
        for name, _, _ in stats:
            try:
                data = Path(name).read_bytes()
            except OSError:
                continue
            digest.update(name.encode('utf-8', 'replace') + b'\0' + hashlib.sha256(data).digest())
        self._stats, self._hash = stats, digest.hexdigest()
        return self._hash


def busy_calls(path):
    """The calls a child reports as still running (geck_mcp/busy.py writes the file): [] when there is none, or the
    file is missing or unreadable. Read here, not imported, so a broken edit in the package cannot stop the
    supervisor."""
    if path is None:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if int(data.get('count', 0)) <= 0:
            return []
        return [str(c) for c in data.get('calls') or []] or ['?']
    except (OSError, ValueError, TypeError, AttributeError):
        return []


class Child:
    def __init__(self, argv, cwd, on_message, on_exit, busy_file=None):
        env = dict(os.environ, PYTHONUNBUFFERED='1')
        # A cancelled act call is not answered, but the server keeps running it: the server reports such calls in
        # this file (geck_mcp/busy.py), so a reload waits for them instead of killing them (R4).
        self.busy_file = Path(busy_file) if busy_file else None
        if self.busy_file is not None:
            self.busy_file.parent.mkdir(parents=True, exist_ok=True)
            self.busy_file.unlink(missing_ok=True)
            env['GECK_MCP_BUSY_FILE'] = str(self.busy_file)
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=None)
        self.write_lock = threading.Lock()
        self.alive = True
        self._on_message, self._on_exit = on_message, on_exit
        threading.Thread(target=self._read, daemon=True, name=f'child-{self.proc.pid}').start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                if line.strip():
                    self._on_message(self, line)
        except (OSError, ValueError):
            pass
        self.alive = False
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            pass
        if self.busy_file is not None:
            try:
                self.busy_file.unlink(missing_ok=True)
            except OSError:
                pass
        self._on_exit(self)

    def send(self, line):
        if not line.endswith(b'\n'):
            line += b'\n'
        with self.write_lock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def close(self, grace=10.0, wait=False):
        """Close stdin (the server exits on end of input and closes its helpers); kill it if it lingers."""
        def finish():
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if wait:
            finish()
        else:
            threading.Thread(target=finish, daemon=True).start()


class Supervisor:
    def __init__(self, argv, watch, cwd=None, poll=1.0, settle=1.0, ready_timeout=60.0, out=None, inp=None):
        self.argv, self.cwd = list(argv), cwd
        self.watcher = Watcher(watch)
        self.poll, self.settle, self.ready_timeout = poll, settle, ready_timeout
        self.out = out or sys.stdout.buffer
        self.inp = inp or sys.stdin.buffer
        self.out_lock = threading.Lock()
        self.route = threading.RLock()        # held while a client message is routed or a child is swapped
        self.maps = threading.Lock()          # guards in_flight and server_requests
        self.child = None
        self.in_flight = {}                   # client request id (json) -> child
        self.server_requests = {}             # server request id (json) -> child
        self.initialize = None                # the client's initialize request (handshake era)
        self.initialized = None               # the client's notifications/initialized
        self.set_level = None                 # the client's last logging/setLevel (handshake era)
        self.envelope = None                  # the _meta of a 2026-07-28 request, for the new child's probe
        self.listens = {}                     # listen request id (json) -> (raw id, honored filter)
        self.pending_ready = {}               # probe id (json) -> [Event, response]
        self.probes = 0
        self.reloads = 0
        self.code = None                      # fingerprint of the running child's code
        self.failed = None                    # fingerprint whose child failed to start
        self.closing = False
        self.methods = set()                  # client methods seen (names only), for the log
        self.close_grace = 10.0               # seconds a replaced child gets to exit before it is killed
        self.drain_limit = 660.0              # longest a reload waits for a busy child (longest tool timeout + margin)
        self.children = 0
        self.busy_since = None                # when the current child was first seen busy with a pending reload

    # -- output to the client --------------------------------------------------------------------------
    def emit(self, message):
        self.emit_raw(json.dumps(message, separators=(',', ':')).encode('utf-8'))

    def emit_raw(self, line):
        if not line.endswith(b'\n'):
            line += b'\n'
        with self.out_lock:
            try:
                self.out.write(line)
                self.out.flush()
            except (OSError, ValueError):
                pass

    # -- children --------------------------------------------------------------------------------------
    def spawn(self):
        """Start a child and make it ready for the client's era. Returns the child, or None if it failed."""
        self.children += 1
        busy_file = Path(os.environ.get('TEMP') or os.environ.get('TMPDIR') or '/tmp') / 'geck-mcp' / (
            'busy-%d-%d.json' % (os.getpid(), self.children))
        child = Child(self.argv, self.cwd, self.from_child, self.child_exited, busy_file)
        if self.initialize is not None:
            probe = dict(self.initialize)
        elif self.envelope is not None:
            probe = {'jsonrpc': '2.0', 'method': 'tools/list', 'params': {'_meta': self.envelope}}
        else:
            return child                      # the client has said nothing yet: nothing to make ready
        self.probes += 1
        rid = f'{OWN_ID}ready-{self.probes}'
        key = json.dumps(rid)
        done = threading.Event()
        self.pending_ready[key] = [done, None]
        child.send(json.dumps(dict(probe, id=rid)).encode('utf-8'))
        deadline = time.monotonic() + self.ready_timeout
        while not done.wait(0.1) and child.alive and time.monotonic() < deadline:
            pass                                  # a child that dies on import ends the wait at once
        ok = done.is_set()
        response = self.pending_ready.pop(key, [None, None])[1]
        if not ok or not isinstance(response, dict) or 'result' not in response:
            if isinstance(response, dict) and 'error' in response:
                log(f'the new server refused the probe: {response["error"]}')
            child.proc.kill()
            return None
        if self.initialize is not None:
            if self.initialized is not None:
                child.send(json.dumps(self.initialized).encode('utf-8'))
            if self.set_level is not None:
                child.send(json.dumps(dict(self.set_level, id=f'{OWN_ID}level-{self.probes}')).encode('utf-8'))
        return child

    def child_exited(self, child):
        with self.route, self.maps:
            current = child is self.child
            if current:
                self.child = None
            # Calls owned by this child fail even when a new child already replaced it (a message that arrived
            # between the crash and this callback starts one): otherwise they would never be answered.
            failed = [rid for rid, owner in self.in_flight.items() if owner is child]
            for rid in failed:
                del self.in_flight[rid]
                self.emit({'jsonrpc': '2.0', 'id': json.loads(rid), 'error': {
                    'code': -32603, 'message': 'The geck server process exited during this call (a crash or a '
                    'kill). A new one starts on the next call; check the game and editor state before retrying.'}})
            for rid in [r for r, owner in self.server_requests.items() if owner is child]:
                del self.server_requests[rid]
        if not self.closing and (current or failed):
            log(f'server process {child.proc.pid} exited (code {child.proc.poll()}); {len(failed)} call(s) '
                'failed; the next call starts a new one')

    def ensure_child(self):
        """Called with the route lock held. Starts a child if there is none."""
        if self.child is not None and self.child.alive:
            return self.child
        fingerprint = self.watcher.fingerprint()
        child = self.spawn()
        if child is None:
            log('the server did not start (the traceback is above)')
            return None
        restarted = self.code is not None
        self.child, self.code = child, fingerprint
        if restarted:
            self.announce()
        return child

    def reload(self, fingerprint):
        """Called with the route lock held and nothing in flight."""
        child = self.spawn()
        if child is None:
            self.failed = fingerprint
            log('the changed code did not start; the previous server keeps serving. Fix the error and save '
                'again (the traceback is above).')
            return False
        old, self.child, self.code, self.failed = self.child, child, fingerprint, None
        if old is not None:
            old.close(self.close_grace)
        self.reloads += 1
        note(f'reloaded the server (process {child.proc.pid}) after a code change')
        self.announce()
        return True

    def announce(self):
        """Tell the client that what it listed may have changed."""
        note(f'told the client the tool list changed (handshake: {self.initialize is not None}, '
            f'listen streams: {len(self.listens)})')
        if self.initialize is not None:
            self.emit({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
        for raw_id, honored in list(self.listens.values()):
            for key, method in LIST_CHANGED.items():
                if honored.get(key):
                    self.emit({'jsonrpc': '2.0', 'method': method, 'params': {'_meta': {SUBSCRIPTION_ID: raw_id}}})

    # -- routing ---------------------------------------------------------------------------------------
    def from_child(self, child, line):
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            self.emit_raw(line)
            return
        has_id = 'id' in message
        rid = json.dumps(message.get('id')) if has_id else None
        if 'method' not in message and has_id:                    # a response
            if rid in self.pending_ready:
                slot = self.pending_ready[rid]
                slot[1] = message
                slot[0].set()
                return
            if str(message.get('id')).startswith(OWN_ID):
                return                                            # a replayed setLevel, or a late probe
            with self.maps:
                self.in_flight.pop(rid, None)
            init = self.initialize
            if init is not None and message.get('id') == init.get('id') and isinstance(message.get('result'), dict):
                caps = message['result'].setdefault('capabilities', {})
                if isinstance(caps.get('tools'), dict):
                    caps['tools']['listChanged'] = True
                self.emit(message)
                return
        elif 'method' in message and has_id:                      # a request from the server to the client
            with self.maps:
                self.server_requests[rid] = child
        self.emit_raw(line)

    def from_client(self, line):
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            with self.route:
                child = self.ensure_child()
                if child is not None:
                    child.send(line)
            return
        method = message.get('method')
        has_id = 'id' in message
        rid = json.dumps(message.get('id')) if has_id else None
        if method and method not in self.methods:
            self.methods.add(method)
            note(f'client sent {method} (first time)')
        if method == 'subscriptions/listen' and has_id:
            self.listen(message)
            return
        if method == 'notifications/cancelled':
            target = json.dumps((message.get('params') or {}).get('requestId'))
            if target in self.listens:
                self.listens.pop(target, None)
                return
            # A cancelled call is never answered (the protocol says so), so stop waiting for it here: otherwise
            # in_flight keeps it and hot reload waits forever.
            with self.maps:
                self.in_flight.pop(target, None)
        with self.route:
            with self.maps:
                owner = self.server_requests.pop(rid, None) if method is None and has_id else None
            if owner is not None:                                 # the client's answer to the server
                owner.send(line)
                return
            child = self.ensure_child()          # before recording, so a restart does not replay this message
            if method == 'initialize' and has_id:
                self.initialize = message
            elif method == 'notifications/initialized':
                self.initialized = message
            elif method == 'logging/setLevel':
                self.set_level = message
            elif method is not None and has_id and self.envelope is None and self.initialize is None:
                meta = (message.get('params') or {}).get('_meta')
                if isinstance(meta, dict) and any('protocolVersion' in k for k in meta):
                    self.envelope = {k: v for k, v in meta.items() if k != 'progressToken'}
            if child is None:
                if has_id and method is not None:
                    self.emit({'jsonrpc': '2.0', 'id': message['id'], 'error': {
                        'code': -32603, 'message': 'The geck server process could not start; the error is in the '
                        'MCP server log (stderr). Fix it and call again.'}})
                return
            if has_id and method is not None:
                with self.maps:
                    self.in_flight[rid] = child
            child.send(line)
        if method == 'notifications/initialized':
            # A client that kept its tool list from an earlier process (a respawn) refetches it.
            self.emit({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})

    def listen(self, message):
        wanted = (message.get('params') or {}).get('notifications') or {}
        honored = {k: True for k in LIST_CHANGED if wanted.get(k)}
        if wanted.get('resourceSubscriptions'):
            honored['resourceSubscriptions'] = list(wanted['resourceSubscriptions'])
        self.listens[json.dumps(message['id'])] = (message['id'], honored)
        note(f'listen stream opened: {sorted(honored)}')
        self.emit({'jsonrpc': '2.0', 'method': 'notifications/subscriptions/acknowledged',
                   'params': {'notifications': honored, '_meta': {SUBSCRIPTION_ID: message['id']}}})

    # -- loops -----------------------------------------------------------------------------------------
    def watch_loop(self):
        seen, since = None, None
        while not self.closing:
            time.sleep(self.poll)
            try:
                fingerprint = self.watcher.fingerprint()
            except Exception as exc:                              # a file vanished mid-walk: next poll
                log(f'watch: {exc}')
                continue
            if self.code is None or fingerprint in (self.code, self.failed):
                seen, since = None, None
                self.busy_since = None                            # no reload pending: a later wait starts afresh
                continue
            if fingerprint != seen:                               # wait until the files stop changing
                seen, since = fingerprint, time.monotonic()
                continue
            if time.monotonic() - since < self.settle:
                continue
            with self.route:
                with self.maps:
                    busy = bool(self.in_flight)
                if busy or (self.initialize is not None and self.initialized is None):
                    continue                                      # wait until no call is running
                # A cancelled act call is gone from in_flight but still running in the child (R4). Wait for it: the
                # old child stays current and serves new calls meanwhile (two servers would fight over the game's
                # single-instance pipe). Past drain_limit, reload anyway and say so.
                calls = busy_calls(self.child.busy_file) if self.child is not None else []
                if calls:
                    now = time.monotonic()
                    if self.busy_since is None:
                        self.busy_since = now
                        note(f'reload waits: the server is still running {calls}')
                    if now - self.busy_since < self.drain_limit:
                        continue
                    log(f'killed a busy server: {calls} still running after {self.drain_limit:.0f} s; it had '
                        f'{self.close_grace:.0f} s to finish before the kill')
                self.busy_since = None
                if self.child is None:
                    self.code = fingerprint                       # the next call starts the new code anyway
                    self.announce()
                else:
                    self.reload(fingerprint)
            seen, since = None, None

    def run(self):
        threading.Thread(target=self.watch_loop, daemon=True, name='watch').start()
        with self.route:
            self.ensure_child()
        try:
            for line in self.inp:
                if line.strip():
                    self.from_client(line)
        finally:
            self.closing = True
            for raw_id, _ in list(self.listens.values()):        # graceful end of each listen stream
                self.emit({'jsonrpc': '2.0', 'id': raw_id, 'result': {'_meta': {SUBSCRIPTION_ID: raw_id}}})
            self.listens.clear()
            if self.child is not None:
                self.child.close(wait=True)


def main(script, argv):
    """`script` is run_mcp.py; the child runs it with --serve and the same arguments. Watched: the geck_mcp package
    and the project file."""
    global LOG_FILE
    script = Path(script).resolve()
    root = script.parent
    watch = [root / 'geck_mcp']
    project = None
    if '--project' in argv:
        index = argv.index('--project') + 1
        if index < len(argv):
            project = Path(argv[index])
            watch.append(project)
    LOG_FILE = log_path(root, project)
    child = [sys.executable, str(script), '--serve', *argv]
    Supervisor(child, watch, cwd=str(root)).run()


def log_path(root, project):
    """FNV_MCP_SUPERVISOR_LOG (or GECK_SUPERVISOR_LOG) wins (empty: no log file); else the project's state_dir (so a test project with a
    temporary state folder never writes into the real log); else <root>/state when it exists."""
    override = os.environ.get('FNV_MCP_SUPERVISOR_LOG', os.environ.get('GECK_SUPERVISOR_LOG'))
    if override is not None:
        return Path(override) if override else None
    if project is not None:
        try:
            data = json.loads(project.read_text(encoding='utf-8-sig'))
            state = data.get('state_dir')
            if state:
                state = Path(state).expanduser()
                state = state if state.is_absolute() else project.resolve().parent / state
                if state.is_dir():
                    return state / 'supervisor.log'
                return None
        except (OSError, ValueError, AttributeError):
            pass
    if (root / 'state').is_dir():
        return root / 'state' / 'supervisor.log'
    return None
