"""fnv_session and fnv_health (Plan 2, W1): one call to get the game ready for a test, one to finish it, and one to say
what is abnormal since the last mark. The open session is kept on the live service (live.session_state), so every tool
reads the same one. Start reuses ScenarioRunner.prepare (launch or load, crash context on); end reverts this session's
journal entries, turns the frame control back to normal and optionally quits the game.
"""
import threading
import time

from .fnv_conv import ConvError, dispatch, verdict
from .fnv_inject import DETERMINISTIC_FPS, DETERMINISTIC_SEED, shared_time
from .fnv_link import word
from .fnv_setup import setup_check
from .fnv_warnings import for_live as warnings_for_live

REF_COUNT = 0x011F4400           # the live NiRefObject count (a static; the engine map does not list it)
REF_GROWTH = 1000                # more new live objects than this since the session start is reported
FRAME_TIME_RATIO = 1.5           # a frame time this many times the session start's is reported


def _refs(live):
    """The NiRefObject count now, or None when the game does not answer."""
    result = live.link.batch([('rd', word(REF_COUNT), '4')])[0]
    if not result.get('ok'):
        return None
    return int.from_bytes(bytes.fromhex(result['hex']), 'little')


def _runner(live):
    from .fnv_scenario import ScenarioRunner
    return ScenarioRunner(live)


def _deep(live):
    from .fnv_deep import DeepTools
    deep = getattr(live, 'deep', None) or DeepTools(live)
    live.deep = deep
    return deep


_SESSION_LOCK = threading.RLock()  # start and end run one at a time: two parallel starts must not both launch


class SessionTools:
    def __init__(self, live):
        self.live = live

    def run(self, action, start='continue', timeout_seconds=240, deterministic=False, label=None, quit_game=False):
        return dispatch(action, {
            'start': lambda: self.start(start, timeout_seconds, deterministic),
            'mark': lambda: self.mark(label),
            'end': lambda: self.end(quit_game),
            'status': self.status,
        })

    @property
    def state(self):
        return getattr(self.live, 'session_state', None)

    def _open(self):
        state = self.state
        if state is None:
            raise ConvError('NO_SESSION', 'No session is open. Next: call fnv_session start.')
        return state

    def start(self, start, timeout_seconds, deterministic):
        with _SESSION_LOCK:
            return self._start(start, timeout_seconds, deterministic)

    def _start(self, start, timeout_seconds, deterministic):
        if self.state is not None:
            raise ConvError('SESSION_OPEN', 'A session is already open (since %s). Next: call fnv_session end first, or '
                            'fnv_session status.' % time.strftime('%H:%M:%S', time.localtime(self.state['since'])))
        setup = setup_check(self.live.project)
        if not setup['ready']:
            raise ConvError('SETUP_MISSING', 'Setup is missing: %s. Next: call fnv_setup_check and fix what it names.'
                            % ', '.join(setup['missing']), {'missing': setup['missing']})
        runner = _runner(self.live)
        prepared = runner.prepare({'start': start, 'timeout': timeout_seconds})

        # The warning log: on for this session, and the mark at the start so the first read covers only this session.
        warnings = warnings_for_live(self.live)
        status = warnings.status()
        warnings_state = {'enabled': bool(status.get('enabled'))}
        if not warnings_state['enabled']:
            change = warnings.enable()
            warnings_state.update(changed=bool(change.get('changed')), restartNeeded=bool(change.get('restartNeeded')))
        try:
            # The game keeps the log files locked while it runs: engine warnings come from the live hook instead.
            started = warnings.capture.start()
            warnings_state['capture'] = bool(started.get('capturing'))
            warnings_state['captureStarted'] = bool(started.get('started'))     # this start turned it on (R12)
        except Exception as e:  # noqa: BLE001 - the session works without it; the health check says so
            warnings_state['capture'] = False
            warnings_state['captureError'] = str(e)[:200]
        control, _ = shared_time(self.live)
        deterministic_state = None
        try:
            warnings.clear()
            if deterministic:
                control.fixed(DETERMINISTIC_FPS)
                runner.agent.rng(seed=DETERMINISTIC_SEED)
                deterministic_state = {'fps': DETERMINISTIC_FPS, 'seed': DETERMINISTIC_SEED}
            snap = control.snapshot(measure=True)
            mark = runner.mark()
            baseline = {'realFps': snap.get('realFps'), 'refs': _refs(self.live)}
        except Exception:
            # No session will be open, so nothing could undo these later: take back what this start turned on (and
            # only that: a capture that was already running stays; R12).
            if prepared.get('crashContext') is True:     # 'off (...)' means enable failed (S6 review C)
                try:
                    _deep(self.live).crash_context('disable')
                except Exception:  # noqa: BLE001 - best effort; the original error is what the caller needs
                    pass
            if warnings_state.get('captureStarted'):
                try:
                    warnings.capture.stop()
                except Exception:  # noqa: BLE001 - best effort; the original error is what the caller needs
                    pass
            if deterministic:
                try:
                    control.fixed(0, journal=False)
                    control.release()
                except Exception:  # noqa: BLE001
                    pass
            raise
        self.live.session_state = {'start': start, 'since': time.time(), 'deterministic': deterministic_state,
                                   'baseline': baseline, 'journal': mark['journal'], 'mark': mark,
                                   'marks': [{'label': 'session start', **mark}]}
        return verdict('Session open: the game is ready (%s)' % ('launched' if prepared.get('launched') else 'already running'),
                       'Run the steps with fnv_test steps; fnv_health reports what is abnormal from the last mark; fnv_session end '
                       'closes the session.',
                       changed=True, start=start, game=prepared, warnings=warnings_state,
                       deterministic=deterministic_state, baseline=baseline,
                       next='Call fnv_test steps with the steps to test.')

    def mark(self, label=None):
        state = self._open()
        label = label or 'mark %d' % (len(state['marks']) + 1)
        mark = _runner(self.live).mark()
        warnings_for_live(self.live).clear()
        state['mark'] = mark
        state['marks'].append({'label': label, **mark})
        return verdict('Marked "%s"' % label, 'fnv_health reports from this point on.', changed=True, label=label,
                       marks=len(state['marks']))

    def end(self, quit_game=False):
        with _SESSION_LOCK:
            state = self._open()
            try:
                return self._end(state, quit_game)
            finally:
                # Whatever failed above (the game may be gone), the session is closed so a new one can start.
                self.live.session_state = None

    def _end(self, state, quit_game):
        try:
            health = HealthTools(self.live).check()
        except Exception as e:  # the game crashed or quit during the session: the report says so
            health = {'ok': False, 'items': [{'kind': 'health_check_failed', 'what': 'The health check failed: %s'
                                              % str(e)[:300], 'next': 'Call fnv_status (the game may be gone).'}],
                      'error': {'code': getattr(e, 'code', type(e).__name__), 'message': str(e)[:300]}}
        try:
            out = self.live.revert_all(state['journal'])
            reverted = {'reverted': out.get('reverted', 0)}
        except Exception as e:  # the game may be gone: the report says so
            reverted = {'error': str(e), 'code': getattr(e, 'code', None)}
        control, _ = shared_time(self.live)
        try:
            if state['deterministic']:
                control.fixed(0, journal=False)
            control.release()
            released = {'released': True}
        except Exception as e:
            released = {'released': False, 'error': str(e)}
        try:
            _deep(self.live).crash_context('disable')
        except Exception:   # the crash context is best effort at the end of a session
            pass
        quit_result = None
        if quit_game:
            try:
                quit_result = self.live.game.quit()
            except Exception as e:
                quit_result = {'error': str(e)}
        self.live.session_state = None
        return verdict('Session ended: %s' % ('nothing abnormal' if health['ok']
                                              else '%d item(s) to look at' % len(health['items'])),
                       None if health['ok'] else 'Each item in health names the tool to call next.',
                       ok=health['ok'], health=health, reverted=reverted, released=released, quit=quit_result,
                       next=None if health['ok'] else 'Call the tools the health items name.')

    def status(self):
        state = self.state
        if state is None:
            return verdict('No session is open', 'Call fnv_session start to get the game ready.', open=False)
        since = time.strftime('%H:%M:%S', time.localtime(state['since']))
        return verdict('Session open since %s (%d mark%s)' % (since, len(state['marks']),
                                                              '' if len(state['marks']) == 1 else 's'),
                       'fnv_health reports from the last mark.', open=True, start=state['start'],
                       lastMark=state['marks'][-1]['label'], deterministic=state['deterministic'],
                       gameRunning=bool(self.live.game.process()))


class HealthTools:
    def __init__(self, live):
        self.live = live

    def check(self):
        state = getattr(self.live, 'session_state', None)
        if state is None:
            raise ConvError('NO_SESSION', 'No session is open. Next: call fnv_session start, then run the steps.')
        mark = state['mark']
        items = []
        self._script_errors(mark, items)
        self._warnings(items)
        self._crashes(mark, items)
        self._frame_time(state, items)
        self._objects(state, items)
        label = state['marks'][-1]['label']
        if not items:
            return verdict('Nothing abnormal since "%s"' % label, None, ok=True, items=[], since=label)
        return verdict('%d thing(s) to look at since "%s"' % (len(items), label),
                       'Each item names the tool to call next.', ok=False, items=items, since=label)

    def _script_errors(self, mark, items):
        events = (self.live.link.events(mark['events']) or {}).get('events', [])
        errors = [e for e in events if e.get('type') == 'script_error']
        if errors:
            sample = '; '.join(str(e.get('text'))[:120] for e in errors[:3])
            items.append({'kind': 'script_errors', 'what': '%d script error(s) since the mark: %s' % (len(errors), sample),
                          'next': 'Call fnv_script_errors, then fnv_guide topic "script".'})

    def _warnings(self, items):
        summary = warnings_for_live(self.live).summary('mark')
        counts = summary.get('byCategory') or {}
        total = sum(counts.values())
        if total:
            detail = ', '.join('%s %d' % (k, v) for k, v in counts.items())
            items.append({'kind': 'warnings', 'what': '%d engine warning(s) since the mark (%s)' % (total, detail),
                          'next': 'Call fnv_warnings read.'})

    def _crashes(self, mark, items):
        from .fnv_deep import context_files
        crash = self.live.game.crash_since(mark['time'])
        if crash:
            shutdown = ' during shutdown (not a crash of play)' if crash.get('duringShutdown') else ''
            items.append({'kind': 'crash', 'what': 'The game wrote a crash log since the mark%s' % shutdown,
                          'next': 'Call fnv_crash, then fnv_guide topic "crash".'})
        fresh = [p for p in context_files() if p.stat().st_mtime >= mark['time']]
        if fresh:
            items.append({'kind': 'crash_context', 'what': '%d crash-context file(s) since the mark' % len(fresh),
                          'next': 'Call fnv_crash context.'})

    def _frame_time(self, state, items):
        base = state['baseline'].get('realFps')
        now = shared_time(self.live)[0].snapshot(measure=True).get('realFps')
        if base and now and 1000.0 / now > FRAME_TIME_RATIO * (1000.0 / base):
            items.append({'kind': 'frame_time', 'what': 'Frame time %.1f ms, %.1f ms at the session start'
                                                        % (1000.0 / now, 1000.0 / base),
                          'next': 'Call fnv_frame_profile.'})

    def _objects(self, state, items):
        now, base = _refs(self.live), state['baseline'].get('refs')
        if now is not None and base is not None and now - base > REF_GROWTH:
            items.append({'kind': 'objects', 'what': 'NiRefObject count up by %d since the session start' % (now - base),
                          'next': 'Call fnv_heap.'})
