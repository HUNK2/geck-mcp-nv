"""Persistent helper transport (Milestone 1).

One long-lived `Probe.Mcp.exe serve` process per MCP server replaces a Wine
launch per helper call. Requests and responses are single JSON lines. The
worker caches the GECK process, menu command IDs and window inventory; every
reuse is validated inside the worker (process start time, top-level window
fingerprint, handle liveness).

Safety rules carried over from the one-shot transport:
- A request that times out, or whose worker dies after the request was
  written, is OUTCOME_UNKNOWN. The worker is killed and never asked to repeat
  the request automatically.
- Only a request that provably never reached the worker (write failed before
  any byte was accepted, or worker was not running) is retried on a new worker.
- Each request carries its complete GECK_BRIDGE_* environment; the worker never
  merges it with its own process environment, so a stale session guard cannot
  leak between requests.
"""
import itertools
import json
import os
import queue
import subprocess
import threading
import time

from .transport import BridgeError, Transport, interpret

PROTOCOL = 1
_EOF = object()


class PersistentTransport:
    """Drop-in replacement for Transport with the same call() contract."""

    def __init__(self, project, command=None, start_timeout=None, log_path=None):
        self.project = project
        self._command = command
        self.start_timeout = start_timeout or 20
        self.log_path = log_path or (project.state_dir / 'worker-stderr.log')
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._proc = None
        self._lines = None
        self._log = None
        self.started_at = None
        self.helper_identity = None
        self.stats = {'starts': 0, 'requests': 0, 'restarts': 0, 'lastStartMs': None}

    # -- process management -------------------------------------------------
    def command(self):
        if self._command is not None:
            return list(self._command)
        p = self.project
        return p.helper_command('serve')

    def base_env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GECK_BRIDGE_')}
        env.update(self.project.helper_env())
        return env

    def request_env(self, session=None, expected_model=None):
        p = self.project
        env = {'GECK_BRIDGE_PLUGIN': p.plugin,
               'GECK_BRIDGE_RECORDS': '|'.join(r.editor_id for r in p.records),
               'GECK_BRIDGE_MCP': '1'}
        if session:
            env['GECK_BRIDGE_SESSION'] = session
        if expected_model is not None:
            env['GECK_BRIDGE_EXPECTED_MODEL'] = expected_model
        return env

    @property
    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _reader(self, stream, lines):
        try:
            for raw in stream:
                lines.put(raw)
        except (ValueError, OSError):
            pass  # stream closed by _kill
        finally:
            lines.put(_EOF)

    def _start(self):
        started = time.monotonic()
        if self._proc is not None or self._log is not None:
            self._kill()  # close handles/log from a dead idle worker before replacing it
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, 'ab')
        try:
            self._proc = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=self._log, env=self.base_env(), bufsize=0)
        except OSError as error:
            self._log.close()
            raise BridgeError('HELPER_UNAVAILABLE', str(error)) from error
        self._lines = queue.Queue()
        threading.Thread(target=self._reader, args=(self._proc.stdout, self._lines),
                         daemon=True, name='geck-worker-reader').start()
        hello = self._read(time.monotonic() + self.start_timeout, None)
        if hello is None or hello.get('operation') != 'hello' or \
                (hello.get('after') or {}).get('protocol') != PROTOCOL:
            self._kill()
            raise BridgeError('WORKER_UNSUPPORTED', 'Helper did not answer the serve handshake',
                              {'hello': hello})
        # Prove the request direction too: a launcher that swallows stdin would
        # otherwise pass the handshake and time out on the first editor request.
        try:
            self._send({'id': 'handshake', 'args': ['ping']})
        except BridgeError:
            self._kill()
            raise BridgeError('WORKER_UNSUPPORTED', 'Worker input pipe closed during handshake')
        pong = self._read(time.monotonic() + self.start_timeout, 'handshake')
        if pong is None or pong.get('operation') != 'ping' or not pong.get('ok'):
            self._kill()
            raise BridgeError('WORKER_UNSUPPORTED', 'Worker did not answer the handshake ping',
                              {'hello': hello, 'pong': pong})
        self.stats['starts'] += 1
        self.stats['lastStartMs'] = round((time.monotonic() - started) * 1000)
        self.started_at = time.time()
        self.helper_identity = {k: hello['after'].get(k) for k in ('workerPid', 'binaryPath', 'binarySha256', 'pointerBytes')}
        return hello

    def _kill(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            try:
                proc.stdout.close()
            except OSError:
                pass
        if self._log is not None:
            self._log.close()
            self._log = None

    def close(self):
        with self._lock:
            if self.alive:
                try:
                    self._send({'id': 'shutdown', 'args': ['shutdown']})
                    self._read(time.monotonic() + 5, 'shutdown')
                except BridgeError:
                    pass
            self._kill()

    # -- framing ---------------------------------------------------------------
    def _send(self, message):
        data = (json.dumps(message, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
        try:
            sent = 0
            while sent < len(data):
                count = self._proc.stdin.write(data[sent:])
                if not count:
                    raise BrokenPipeError('Worker input accepted no further bytes')
                sent += count
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as error:
            raise BridgeError('OUTCOME_UNKNOWN', 'Worker input failed; delivery cannot be disproved. Inspect before retry.',
                              {'error': str(error)}, uncertain=True) from error

    def _read(self, deadline, request_id):
        """Return the next JSON response whose id matches; None on timeout or EOF."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                raw = self._lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if raw is _EOF:
                return None
            try:
                value = json.loads(raw.decode('utf-8', 'replace'))
            except ValueError:
                continue  # Wine noise on stdout is ignored, as in one-shot mode.
            if not isinstance(value, dict) or 'ok' not in value:
                continue
            if request_id is None or value.get('id') == request_id:
                return value
            # A response for another id means the stream is desynchronised.
            raise BridgeError('HELPER_PROTOCOL_ERROR', 'Worker response id mismatch',
                              {'expected': request_id, 'received': value.get('id')}, uncertain=True)

    def _roundtrip(self, message, timeout):
        """Send one request; retry only when delivery provably did not happen."""
        for attempt in (0, 1):
            if not self.alive:
                if attempt or self.stats['starts']:
                    self.stats['restarts'] += 1
                self._start()
            try:
                self._send(message)
            except BridgeError as error:
                self._kill()
                if attempt or error.uncertain:
                    raise
                continue
            value = self._read(time.monotonic() + timeout, message['id'])
            if value is None:
                died = not self.alive
                self._kill()
                if died:
                    raise BridgeError('OUTCOME_UNKNOWN', 'Worker exited after receiving the request; '
                                      'inspect before retry', {'request': message.get('args')}, uncertain=True)
                raise BridgeError('OUTCOME_UNKNOWN', 'Helper exceeded deadline; inspect before retry',
                                  {'request': message.get('args')}, uncertain=True)
            self.stats['requests'] += 1
            return value

    # -- public API ------------------------------------------------------------
    def call(self, operation, *args, session=None, expected_model=None):
        message = {'id': 'r%d' % next(self._ids), 'args': [operation, *map(str, args)],
                   'env': self.request_env(session, expected_model)}
        with self._lock:
            value = self._roundtrip(message, self.project.helper_timeout_seconds)
        if value.get('operation') == 'protocol':
            # The worker rejects malformed requests before any editor operation runs.
            raise BridgeError('HELPER_PROTOCOL_ERROR', value.get('error') or 'protocol error', value)
        return interpret(value, operation, 0 if value.get('ok') else 2)

    def batch(self, steps, session=None, expected_model=None, stop_on_error=True):
        """Run several helper operations in one round trip; returns each step's `after`.

        Raises for the first failed step, attaching every completed step as evidence.
        """
        if not 1 <= len(steps) <= 16:
            raise ValueError('batch must contain 1..16 steps')
        message = {'id': 'b%d' % next(self._ids), 'stopOnError': stop_on_error,
                   'batch': [{'args': [op, *map(str, a)]} for op, *a in steps],
                   'env': self.request_env(session, expected_model)}
        timeout = min(self.project.helper_timeout_seconds * len(steps), 180)
        with self._lock:
            value = self._roundtrip(message, timeout)
        if value.get('operation') != 'batch':
            raise BridgeError('HELPER_PROTOCOL_ERROR', value.get('error') or 'batch rejected', value,
                              uncertain=True)
        results = []
        for (op, *_), step in zip(steps, value.get('results', [])):
            results.append(interpret(step, op, 0 if step.get('ok') else 2, evidence_extra={'completed': results}))
        if len(results) != len(steps) and stop_on_error:
            raise BridgeError('HELPER_PROTOCOL_ERROR', 'Batch stopped without a failing step', value, True)
        return results

    def ping(self):
        with self._lock:
            return self._roundtrip({'id': 'p%d' % next(self._ids), 'args': ['ping']}, 10)

    def invalidate(self):
        with self._lock:
            if self.alive:
                return self._roundtrip({'id': 'i%d' % next(self._ids), 'args': ['invalidate']}, 10)

    def describe(self):
        return {'mode': 'persistent', 'alive': self.alive, 'startedAt': self.started_at, 'helperIdentity': self.helper_identity, **self.stats}


class AutoTransport:
    """Persistent worker when the helper supports it, otherwise one-shot launches.

    The decision is made once, at the first call, from the serve handshake. A
    handshake failure never dispatches an editor operation, so falling back is safe.
    Set FNV_MCP_TRANSPORT=oneshot (or GECK_MCP_TRANSPORT) to force the legacy path.
    """

    def __init__(self, project, persistent=None, oneshot=None):
        self.project = project
        self.persistent = persistent or PersistentTransport(project)
        self.oneshot = oneshot or Transport(project)
        from . import envvars
        forced = (envvars.get('FNV_MCP_TRANSPORT') or '').lower()
        self.mode = 'oneshot' if forced == 'oneshot' else None
        self.fallback_reason = 'forced by FNV_MCP_TRANSPORT' if self.mode else None

    def _choose(self):
        if self.mode is None:
            try:
                with self.persistent._lock:
                    if not self.persistent.alive:
                        self.persistent._start()
                self.mode = 'persistent'
            except BridgeError as error:
                self.mode = 'oneshot'
                self.fallback_reason = '%s: %s' % (error.code, error)
        return self.persistent if self.mode == 'persistent' else self.oneshot

    def call(self, operation, *args, session=None, expected_model=None):
        return self._choose().call(operation, *args, session=session, expected_model=expected_model)

    def batch(self, steps, session=None, expected_model=None, stop_on_error=True):
        chosen = self._choose()
        if chosen is self.persistent:
            return chosen.batch(steps, session=session, expected_model=expected_model,
                                stop_on_error=stop_on_error)
        return [chosen.call(op, *a, session=session, expected_model=expected_model) for op, *a in steps]

    def close(self):
        if self.persistent.alive:
            self.persistent.close()

    def describe(self):
        if self.mode == 'persistent':
            return self.persistent.describe()
        return {'mode': self.mode or 'undecided', 'fallbackReason': self.fallback_reason}
