"""Live fnv_* tools on top of the engine companion: inspection (milestone 5) and full control (milestone 6).

Reads use xNVSE script expressions (evaluated by the running game) and engine memory decoded with the
decomp's layouts (fnv_facts, engine map). Act tools change the running game; every change is journaled
in state/fnv/journal.jsonl with what is needed to undo it, and saves are backed up before the first change
of a game session. Act tools refuse unless the project sets fnv.engine_control = "full".
"""
import contextlib
import json
import logging
import math
import re
import os
import struct
import threading
import time
import uuid
from pathlib import Path

from . import fnv_engine, fnv_facts, fnv_link
from .fnv_facts import FACTS
from .fnv_game import Game, GameError
from .fnv_link import LinkError, element_value, hexs, word
from .fnv_args import as_bool, capped, form_id, is_form_id, ranged, readable_type
from .fnv_conv import next_step
from .fnv_journal import Journal
from .fnv_state import GameIdentity

FORM_ID = re.compile(r'^(?:0x)?([0-9A-Fa-f]{1,8})$')
PLUGIN_LOCAL = re.compile(r'^([A-Za-z0-9_. \'-]{1,80}\.es[mp]):(?:0x)?([0-9A-Fa-f]{1,6})$', re.I)
EDITOR_ID = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')
SIMPLE_TYPES = {'bool': ('<?', 1), 'char': ('<b', 1), 'signed char': ('<b', 1), 'unsigned char': ('<B', 1),
                'UInt8': ('<B', 1), 'SInt8': ('<b', 1), 'short': ('<h', 2), 'unsigned short': ('<H', 2),
                'UInt16': ('<H', 2), 'SInt16': ('<h', 2), 'int': ('<i', 4), 'unsigned int': ('<I', 4),
                'long': ('<i', 4), 'unsigned long': ('<I', 4), 'UInt32': ('<I', 4), 'SInt32': ('<i', 4),
                'float': ('<f', 4), 'double': ('<d', 8), 'u8': ('<B', 1), 'i8': ('<b', 1), 'u16': ('<H', 2),
                'i16': ('<h', 2), 'u32': ('<I', 4), 'i32': ('<i', 4), 'f32': ('<f', 4), 'f64': ('<d', 8),
                'ptr': ('<I', 4)}


class LiveError(Exception):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


def _num(value):
    """A number as console text: integers without a decimal point (some parameters take integers only)."""
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def _fid(value):
    return '%08X' % value


UNDO_LOCK_TIMEOUT = 30.0     # an undo waits this long for the journal lock (see fnv_journal.ADD_TIMEOUT)


PLAYER_FORM_ID = '00000014'     # the player reference (PlayerREF)


class LiveService:
    def __init__(self, project):
        self.project = project
        self._game = None
        self._engine = None
        self._rtti = {}
        self._lazy_lock = threading.RLock()  # one Game (so one EngineLink and pipe client) per process
        self.journal = Journal(Path(project.state_dir) / 'fnv/journal.jsonl', game=self.game_identity)
        self._hook_lock = threading.Lock()
        self._hook_map, self._hook_game = {}, None     # label -> {'entry': journal id, 'hooks': {hook ids}}
        self.unhook_listeners = []     # fn(label): a hook of that label came out (its tool's in-memory state follows)

    # ================================================================= plumbing
    @property
    def game(self):
        if self._game is None:
            with self._lazy_lock:
                if self._game is None:
                    self._game = Game(self.project)
        return self._game

    def game_identity(self):
        """{'pid', 'started'} of the game the link talks to (asks the game if the link has not said hello)."""
        if self._game is None:
            return None
        link = self._game.link
        hello = link.hello or link.ping()
        return {'pid': hello.get('pid'), 'started': hello.get('started')}

    @staticmethod
    def same_process(entry, ident):
        """True when a journal entry was made in the game process `ident`. Entries without a pid come from an
        unknown process (older journals, or no game connected) and never match."""
        mine, theirs = GameIdentity.parse(entry), GameIdentity.parse(ident)
        return bool(mine and theirs and mine.same_game(theirs))

    @property
    def link(self):
        link = self.game.link
        if link is not None and getattr(link, 'on_hook', False) is None:
            link.on_hook, link.on_unhook = self._hooked, self._unhooked
        return link

    # Hooks their own tool journals or manages: hook_set (act:), native modules (mod:, fnv_native unload), debugger
    # holds (dbg N: unhooking one would leave a parked thread stopped; fnv_debug break clear resumes it) and the
    # session's warnings capture (fnv_session start/end own it). S3 review B I2/I3.
    OWNER_MANAGED_HOOKS = ('act:', 'mod:', 'dbg ', 'warnings_live')

    def _hooked(self, hook):
        """A hook went in (trace, tracepoint, profile, debugger hold, capture...): one `instrument` entry per label
        and game, undone by unhooking the label (N8, PLAN-3 S3.6)."""
        label = hook.get('label') or ''
        if not label or label.startswith(self.OWNER_MANAGED_HOOKS):
            return
        ident = GameIdentity.parse(self.game_identity())
        key = ident.key() if ident else None
        with self._hook_lock:
            if self._hook_game != key:
                self._hook_map, self._hook_game = {}, key
            known = self._hook_map.get(label)
            known_entry = known['entry'] if known else None
        # The journal is never touched under _hook_lock: an undo holds the journal lock and its unhook needs
        # _hook_lock (S3 review B I1).
        if known_entry and not (self.journal.get(known_entry) or {}).get('undone', True):
            with self._hook_lock:
                if label in self._hook_map:
                    self._hook_map[label]['hooks'].add(hook.get('id'))
                    return
        entry = self.journal.add({'kind': 'instrument', 'label': label, 'target': hook.get('target'),
                                  'undo': {'op': 'unhook', 'label': label}})
        with self._hook_lock:
            before = self._hook_map.get(label)
            self._hook_map[label] = {'entry': entry['id'],
                                     'hooks': (before['hooks'] if before else set()) | {hook.get('id')}}

    def _unhooked(self, hook_id):
        """A hook came out: when it was the last one of its label, its entry is closed (a tool that takes its own
        hook out again leaves nothing for fnv_undo all=true)."""
        done = gone = None
        with self._hook_lock:
            for label, info in list(self._hook_map.items()):
                if hook_id in info['hooks']:
                    gone = label
                    info['hooks'].discard(hook_id)
                    if not info['hooks']:
                        del self._hook_map[label]
                        done = info['entry']
                    break
        if done:
            self.journal.mark_undone([done])
        for listener in list(self.unhook_listeners) if gone else []:
            try:
                listener(gone)
            except Exception:  # noqa: BLE001 - a listener's failure must not fail the unhook
                logging.getLogger(__name__).warning('unhook listener failed for %s', gone, exc_info=True)

    # Console verbs that all set the one weather override: their undo lines restore the same thing.
    WEATHER_VERBS = {'forceweather', 'fw', 'setweather', 'sw', 'releaseweatheroverride', 'rwo'}

    @classmethod
    def _order_targets(cls, entry):
        """What an entry's undo restores, as targets: ('mem', start, end) byte ranges, or keys compared for equality.
        An older undo of a target a newer open entry also changed would set a value that the newer entry's undo then
        overwrites, so only the newest may run first (S3 review B I4/I5; U1: every absolute-value undo, not only
        place_adjust and time). A target names the state restored, never the line's text (round 4 M2): Enable,
        Disable and ref_enabled all restore ('ref', R, 'enabled'), SetScale <old> restores ('ref', R, 'scale')."""
        undo = entry.get('undo') or {}
        op = undo.get('op')
        try:
            if op == 'write':
                start = int(undo['address'], 16)
                return [('mem', start, start + len(undo['hex']) // 2)]
            if op == 'writes':
                return [('mem', int(a, 16), int(a, 16) + len(h) // 2) for a, h in undo['writes']]
        except (KeyError, TypeError, ValueError):
            return []
        ref = cls._norm_id(undo['ref']) if undo.get('ref') else ''
        if op == 'console_ref' and ref:
            lines = [str(line).split() for line in undo.get('lines') or []]
            verbs = {words[0].lower() for words in lines if words}
            if 'markfordelete' in verbs:
                return []       # a delete: later changes to the ref become moot, so it may run first (review I2)
            return [t for words in lines if words for t in cls._line_targets(words, ref)]
        if op == 'ref_enabled' and ref:
            return [('ref', ref, 'enabled')]
        if op == 'actor_flag' and ref:
            return [('ref', ref, 'flag', str(undo.get('flag')).lower())]
        if op == 'item_count' and ref:
            return [('ref', ref, 'item', cls._norm_id(undo.get('item')))]
        if op == 'console':
            words = str(undo.get('line') or '').split()
            if not words:
                return []
            verb = words[0].lower()
            if verb.split('.')[-1] in cls.WEATHER_VERBS:
                return [('weather',)]
            if verb.startswith('player.'):      # the player's ref, as console_ref entries name it
                return cls._line_targets([words[0][7:]] + words[1:], PLAYER_FORM_ID)
            return cls._line_targets(words, None)
        if op == 'time_restore' and undo.get('what') in ('fixed', 'speed'):
            return [('time', undo['what'])]
        if op in ('fly_set', 'hud_set'):
            return [(op,)]
        return []

    @staticmethod
    def _norm_id(text):
        """A form id in one spelling (0x15169, 15169 and 00015169 are one item); anything else upper-cased."""
        text = str(text or '').strip()
        try:
            return '%08X' % form_id(text) if is_form_id(text) else text.upper()
        except ValueError:
            return text.upper()

    # Undo line verb -> the state it sets, from its arguments (lower-cased, quotes stripped), as key tails. The long
    # and short names of a command share one state; a verb not listed keys on the verb alone (no value: safest).
    @staticmethod
    def _axes(name, a):
        return [(name, a[0])] if a else [(name, axis) for axis in 'xyz']

    @staticmethod
    def _named(name, a, n=1):
        return [(name, *[LiveService._norm_id(w) if is_form_id(w) else w for w in a[:n]])]

    REF_STATE = {
        'enable': lambda a: [('enabled',)], 'disable': lambda a: [('enabled',)],
        'setscale': lambda a: [('scale',)],
        'setpos': lambda a: LiveService._axes('pos', a),
        'setangle': lambda a: LiveService._axes('angle', a),
        # a cell move sets the cell and every position axis
        'movetocell': lambda a: [('cell',)] + LiveService._axes('pos', []),
        'moveto': lambda a: [('cell',)] + LiveService._axes('pos', []),
        'setunconscious': lambda a: [('flag', 'unconscious')],
        'setrestrained': lambda a: [('flag', 'restrained')],
        'additem': lambda a: LiveService._named('item', a),
        'removeitem': lambda a: LiveService._named('item', a),
        **dict.fromkeys(('setav', 'setactorvalue', 'forceav', 'forceactorvalue', 'modav', 'modactorvalue', 'damageav',
                         'damageactorvalue', 'restoreav', 'restoreactorvalue'),
                        lambda a: LiveService._named('av', a)),     # base or current: one actor value
    }
    # The same for the global console. Objective lines keep their quest and index, a stage its quest.
    CONSOLE_STATE = {
        'set': lambda a: LiveService._named('var', a),
        'removeimagespacemodifier': lambda a: LiveService._named('imod', a),
        'setstage': lambda a: LiveService._named('stage', a),
        'setquestobject': lambda a: LiveService._named('questobject', a),
        'setobjectivecompleted': lambda a: LiveService._named('objectivecompleted', a, 2),
        'setobjectivedisplayed': lambda a: LiveService._named('objectivedisplayed', a, 2),
        **dict.fromkeys(('setgs', 'setgamesetting', 'setnumericgamesetting', 'con_setgamesetting'),
                        lambda a: LiveService._named('gamesetting', a)),
        **dict.fromkeys(('setini', 'setinisetting', 'setnumericinisetting', 'con_setinisetting'),
                        lambda a: LiveService._named('inisetting', a)),
    }

    @classmethod
    def _line_targets(cls, words, ref):
        """One undo line's targets: the state it sets (the enabled state, the scale, a position axis, an actor
        value, an item, a setting, an objective), on a ref or in the global console."""
        verb = words[0].lower()
        args = [w.strip('"').lower() for w in words[1:]]
        table = cls.REF_STATE if ref else cls.CONSOLE_STATE
        keys = table[verb](args) if verb in table else [(verb,)]
        return [('ref', ref, *k) if ref else ('console', *k) for k in keys]

    @staticmethod
    def _targets_meet(a, b):
        if a[0] == 'mem' and b[0] == 'mem':
            return a[1] < b[2] and b[1] < a[2]
        return a == b

    def _later_open(self, entry):
        mine = self._order_targets(entry)
        if not mine:
            return []
        return [e['id'] for e in self.journal.entries(process=self.game_identity(), undoable=True)
                if e['id'] != entry['id'] and (e.get('seq') or 0) > (entry.get('seq') or 0)
                and any(self._targets_meet(t, u) for t in mine for u in self._order_targets(e))]

    def engine(self):
        if self._engine is None:
            with self._lazy_lock:
                if self._engine is None:
                    self._engine = fnv_engine.load(*fnv_engine.default_paths(self.project))
        return self._engine

    def run(self, name, action, kind='read', needs_full=False):
        # Every kind: a cancelled call keeps running in its thread (act calls are shielded, reads are abandoned but
        # not stopped, and fnv_wait_for holds the frame gate), so the supervisor waits for it before a reload (R4).
        from . import busy
        with busy.track(name):
            return self._run(name, action, kind, needs_full)

    # Codes that all mean "no game to talk to": the envelope reports one code for them (PLAN-3 S5.1, T4).
    # COMPANION_UNREACHABLE joins them only when no game process exists (a running game with a busy pipe keeps it).
    GAME_NOT_RUNNING_CODES = ('NOT_RUNNING', 'GAME_NOT_RUNNING')
    # these join them only when no game process exists: a game at the main menu answers NO_GAME_LOADED with its own
    # next step (fnv_game launch refuses while a game runs; S5 review I1)
    NO_PROCESS_CODES = ('COMPANION_UNREACHABLE', 'NO_GAME_LOADED')
    UNCERTAIN_CODES = ('COMPANION_TIMEOUT', 'COMPANION_DISCONNECTED')

    # The one refusal for both gates (act kinds and FULL_ONLY_ACTIONS); fnv_conv's CONTROL_DISABLED step says the same.
    CONTROL_DISABLED_SENTENCE = ('This call needs fnv.engine_control = "full": it changes the running game, or puts '
                                 'code or files into the game or its mod profile. Set it in the project file.')

    def _run(self, name, action, kind, needs_full=False):
        started = time.monotonic()
        operation_id = str(uuid.uuid4())
        journal = getattr(self, 'journal', None)     # the envelope works even before the journal exists
        recording = journal.recording() if journal is not None else contextlib.nullcontext([])
        made = recording.__enter__()
        try:
            if (kind == 'act' or needs_full) and getattr(self.project.fnv, 'engine_control', 'read') != 'full':
                raise LiveError('CONTROL_DISABLED', self.CONTROL_DISABLED_SENTENCE)
            data = action()
            changed = kind != 'read' and not (isinstance(data, dict) and data.get('changed') is False)
            return {'ok': True, 'operationId': operation_id, 'operation': name, 'projectId': self.project.project_id,
                    'outcome': 'acknowledged' if changed else 'unchanged', 'data': data, 'error': None,
                    'game': self._identity_or_none(), 'elapsedMs': round((time.monotonic() - started) * 1000)}
        except Exception as e:  # reported, never raised to the client
            return self._failure(name, kind, e, operation_id, started, list(made))
        finally:
            recording.__exit__(None, None, None)

    def _identity_or_none(self):
        try:
            return self.identity()
        except Exception:  # noqa: BLE001 - the envelope must still be returned
            return None

    def _envelope_code(self, e):
        code = getattr(e, 'code', None)
        if not isinstance(code, str) or not code:
            return 'INVALID_ARGUMENT' if isinstance(e, ValueError) else 'FNV_ERROR'
        if code in self.GAME_NOT_RUNNING_CODES:
            return 'GAME_NOT_RUNNING'
        if code in self.NO_PROCESS_CODES:
            try:
                if not self.game.process(fresh=True):
                    return 'GAME_NOT_RUNNING'
            except Exception:  # noqa: BLE001 - several games or no process list: keep the companion's code
                pass
        return code

    def _failure(self, name, kind, e, operation_id, started, made):
        """The error envelope. outcome: outcome_unknown when the change may have happened (the companion stopped
        answering, or an act tool failed with an error that is not one of ours); partial_change when this call's
        journal entries are still open (part of the change landed; data.journalIds lists them); else
        failed_before_change."""
        code = self._envelope_code(e)
        open_ids = []
        for entry_id in made:
            try:
                entry = self.journal.get(entry_id)
            except Exception:  # noqa: BLE001 - an unreadable journal: count the entry as open
                entry = None
            if entry is None or not entry.get('undone'):
                open_ids.append(entry_id)
        uncoded = not isinstance(getattr(e, 'code', None), str) and not isinstance(e, ValueError)
        if code in self.UNCERTAIN_CODES and (kind != 'read' or open_ids) or uncoded and kind != 'read':
            outcome, recovery = 'outcome_unknown', 'Check fnv_status / fnv_journal before retrying.'
        elif open_ids:
            outcome = 'partial_change'
            recovery = ('Part of the change was made (data.journalIds): fnv_undo those entries, or check fnv_journal, '
                        'before calling again.')
        else:
            outcome = 'failed_before_change'
            recovery = next_step(code) or 'Fix the input or the game state (fnv_status) and call again.'
        data = getattr(e, 'evidence', None)
        if open_ids:
            data = dict(data, journalIds=open_ids) if isinstance(data, dict) else \
                ({'journalIds': open_ids} if data is None else {'evidence': data, 'journalIds': open_ids})
        return {'ok': False, 'operationId': operation_id, 'operation': name, 'projectId': self.project.project_id,
                'outcome': outcome, 'data': data,
                'error': {'code': code, 'message': str(e), 'retrySafe': outcome == 'failed_before_change',
                          'recovery': recovery},
                'game': self._identity_or_none(), 'elapsedMs': round((time.monotonic() - started) * 1000)}

    def identity(self):
        if self._game is None:
            return None
        link = self._game.link
        hello = link.hello
        if hello:
            # the newest frame this connection saw: the ping's can be minutes old (G8)
            frames = [f for f in (hello.get('frame'), getattr(link, 'last_frame', None)) if isinstance(f, int)]
            return {'pid': hello.get('pid'), 'started': hello.get('started'),
                    'frame': max(frames) if frames else hello.get('frame')}
        if link.handle is None:
            return None
        # Connected but no ping on this connection yet (a fresh server child, a tool that only sends batches): the
        # one game process is the one the pipe talks to; its start time is the companion's `started`.
        try:
            from .fnv_game import process_started
            proc = self._game.process()
            if not proc:
                return None
            started = process_started(proc[0])
        except Exception:  # noqa: BLE001 - identity is best effort
            return None
        return {'pid': proc[0], 'started': str(started) if started is not None else None,
                'frame': link.last_frame}

    def ready(self):
        """The game runs and the companion answers; returns the ping."""
        ping = self.link.ping()
        if not ping.get('deferredInit'):
            raise LiveError('GAME_STARTING', 'The game is still starting; wait or call fnv_game launch')
        return ping

    def in_game(self):
        ping = self.ready()
        if not ping.get('loaded'):
            phase, _ = self.game.phase(ping)
            if phase != 'in_game' and phase != 'in_menu' and phase != 'paused_menu':
                raise LiveError('NO_GAME_LOADED', 'No game is loaded (phase %s): use fnv_game launch with a save or cell' % phase)
        return ping

    def changing(self, reason):
        """Before the first change of a game session: back up the dev profile's saves."""
        return self.game.backup_saves(reason)

    # ================================================================= low-level helpers
    def evals(self, expressions, ref=0):
        ops = [('eval', hexs(e), word(ref)) for e in expressions]
        return self.link.batch(ops)

    def values(self, expressions, ref=0):
        return [fnv_link.value_of(r) for r in self.evals(expressions, ref)]

    def console(self, line, ref=0):
        """Run one console line with its printed output captured (see fnv_facts console flags)."""
        verbose, visible = FACTS['verbose_print_flag']['address'], FACTS['console_visible_flag']['address']
        old = self.link.batch([('rd', word(verbose), '1'), ('rd', word(visible), '1')])
        if not all(r.get('ok') for r in old):
            raise LiveError('ENGINE_MISMATCH', 'Console flags unreadable: is this FalloutNV 1.4.0.525?', old)
        results = self.link.batch([('wr', word(verbose), '01'), ('wr', word(visible), '01'), ('con', hexs(line), word(ref)),
                                   ('wr', word(visible), old[1]['hex']), ('wr', word(verbose), old[0]['hex'])])
        con = results[2]
        output = (con.get('output') or '').replace('\r', '').rstrip('\n')
        lines = output.split('\n') if output else []
        # The console compiler reports a bad line by printing and still returns success to RunScriptLine.
        compile_error = any('Compiled script not saved' in l or "Script 'SysWindowCompileAndRun'" in l for l in lines)
        return {'line': line, 'accepted': bool(con.get('accepted')) and not compile_error, 'compileError': compile_error,
                'output': lines, 'scriptErrors': con.get('scriptErrors', 0), 'outputCaptured': con.get('captured'),
                'frame': con.get('frame')}

    def u32s(self, addresses):
        results = self.link.batch([('rd', word(a), '4') for a in addresses])
        return [struct.unpack('<I', bytes.fromhex(r['hex']))[0] if r.get('ok') else None for r in results]

    def reads(self, spans):
        results = self.link.batch([('rd', word(a), word(n)) for a, n in spans])
        return [bytes.fromhex(r['hex']) if r.get('ok') else None for r in results]

    def walk_list(self, head_address, limit=500):
        """BSSimpleList / tList at an address: nodes {data, next}; returns the data pointers (one frame with
        the companion's ls op, node by node with older companions)."""
        if not head_address:
            return []
        result = self.link.one('ls', word(head_address), word(min(limit, 4096)))
        if result.get('ok'):
            return [int(p, 16) for p in result.get('items', [])][:limit]
        out, node, seen = [], head_address, set()
        while node and node not in seen and len(out) < limit:
            seen.add(node)
            raw = self.reads([(node, 8)])[0]
            if raw is None:
                break
            data, nxt = struct.unpack('<II', raw)
            if data:
                out.append(data)
            node = nxt
        return out

    def rtti(self, pointers):
        """Class names of polymorphic objects from MSVC RTTI (vtable[-1] -> complete object locator)."""
        pointers = [p for p in pointers if p]
        vtables = dict(zip(pointers, self.u32s(pointers))) if pointers else {}
        unknown = sorted({v for v in vtables.values() if v and v not in self._rtti})
        if unknown:
            locators = dict(zip(unknown, self.u32s([v - 4 for v in unknown])))
            raw = dict(zip(unknown, self.reads([(locators[v] or 0x10, 0x14) for v in unknown])))
            names = {}
            descriptors = {}
            for v in unknown:
                if raw[v] is None:
                    self._rtti[v] = (None, 0)
                    continue
                _, offset, _, td, _ = struct.unpack('<IIIII', raw[v])
                descriptors[v] = (td, offset)
            texts = self.link.batch([('rs', word(td + 8), '100') for td, _ in descriptors.values()]) if descriptors else []
            for (v, (td, offset)), text in zip(descriptors.items(), texts):
                name = text.get('text') if text.get('ok') else None
                if name and name.startswith('.?AV') and name.endswith('@@'):
                    name = name[4:-2]
                names[v] = name
                self._rtti[v] = (name, offset)
        return {p: {'class': self._rtti.get(v, (None, 0))[0], 'subobjectOffset': self._rtti.get(v, (None, 0))[1]}
                for p, v in vtables.items()}

    def form_ids(self, pointers):
        """TESForm refID and type for form pointers (0 -> None)."""
        live = [p for p in pointers if p]
        raw = dict(zip(live, self.reads([(p + 4, 12) for p in live]))) if live else {}
        out = []
        for p in pointers:
            data = raw.get(p)
            if not p or data is None:
                out.append(None)
                continue
            form_type, flags, ref_id = data[0], struct.unpack('<I', data[4:8])[0], struct.unpack('<I', data[8:12])[0]
            out.append({'formId': _fid(ref_id), 'formType': form_type, 'ptr': '0x%08X' % p, 'flags': flags})
        return out

    # ================================================================= references
    def resolve(self, ref):
        """A reference/form spec -> {'ptr', 'formId', 'expr'}.

        Accepts: player | crosshair | form id hex (0x00000014, 14, 000A3E7C) | Plugin.esp:LOCALID |
        an EditorID from the record index (looked up, never evaluated: W2)."""
        ref = (ref or '').strip()
        if not ref:
            raise ValueError('ref is required')
        if ref.lower() in ('player', 'playerref'):
            expr = 'player'
        elif ref.lower() == 'crosshair':
            expr = 'GetCrosshairRef'
        elif PLUGIN_LOCAL.match(ref):
            plugin, local = PLUGIN_LOCAL.match(ref).groups()
            expr = '(BuildRef (GetModIndex %s) %d)' % (fnv_link.quoted(plugin), int(local, 16))
        elif is_form_id(ref):          # one parser with the record tools (T6)
            value = form_id(ref)
            expr = '(BuildRef %d %d)' % (value >> 24, value & 0xFFFFFF)
        elif EDITOR_ID.match(ref):
            # A bare name is looked up in the EditorID index, never evaluated: eval "QuitGame" ran QuitGame from a read
            # tool (W2), and plugin commands are not in the engine map to refuse them by name (review I4).
            found = self.editor_id(ref)
            if not found:
                raise LiveError('REF_NOT_FOUND', 'No form named %r in the EditorID index. Next: pass its form id '
                                '(fnv_record_find finds it by name), or a Plugin.esp:LOCALID.' % ref, {'spec': ref})
            expr = self.form_expr(found['formId'])
        else:
            raise ValueError('Not a reference: %r (use player, crosshair, a form id or an EditorID)' % ref)
        result = self.link.eval(expr)
        element = result.get('result') or {}
        if not result.get('ok') or element.get('type') != 'form' or not element.get('ptr'):
            raise LiveError('REF_NOT_FOUND', 'No form for %r in the running game' % ref,
                            {'expression': expr, 'result': result})
        return {'ptr': int(element['ptr'], 16), 'formId': element.get('formId'), 'expr': expr, 'spec': ref}

    # TESForm type ids of placed references (REFR 58, ACHR 59, ACRE 60, read live 2026-11-11; then the placed
    # projectile kinds, which are references too).
    REFERENCE_TYPES = range(58, 66)

    def resolve_ref(self, ref):
        """resolve(), refusing a base form: reference functions (GetPos, GetAngle...) on a base form crash the game
        (seen live 2026-11-11: fnv_photo on a STAT's EditorID faulted in Cmd_GetPos_Eval)."""
        r = self.resolve(ref)
        raw = self.reads([(r['ptr'] + 4, 1)])[0]
        form_type = raw[0] if raw else None
        if form_type not in self.REFERENCE_TYPES:
            raise LiveError('NOT_A_REFERENCE', '%s is a base form (form type %s), not a placed reference. Next: pass '
                            'the reference: fnv_place returns it, fnv_refs_near base=%s lists them.'
                            % (ref, form_type, r.get('formId')), {'formId': r.get('formId'), 'formType': form_type})
        return r

    ACTOR_TYPES = (59, 60)  # ACHR, ACRE

    def resolve_actor(self, ref):
        """resolve(), refusing anything but a placed actor: actor methods (MagicTarget, actor values) called on another
        form run engine code on the wrong layout (seen 2026-11-12: fnv_actor_values on a campfire static)."""
        r = self.resolve(ref)
        raw = self.reads([(r['ptr'] + 4, 1)])[0]
        form_type = raw[0] if raw else None
        if form_type not in self.ACTOR_TYPES:
            raise LiveError('NOT_AN_ACTOR', '%s is not a placed actor (form type %s; actors are ACHR 59 and ACRE 60). '
                            'Next: pass an NPC or creature reference (fnv_refs_near lists them).'
                            % (ref, form_type), {'formId': r.get('formId'), 'formType': form_type})
        return r

    PREFERRED = ('QUST', 'REFR', 'ACHR', 'ACRE', 'NPC_', 'CREA', 'GLOB', 'WEAP', 'ARMO', 'ALCH', 'MISC', 'KEYM',
                 'AMMO', 'BOOK', 'CONT', 'DOOR', 'ACTI', 'STAT', 'CELL', 'PACK', 'SPEL', 'PERK', 'FACT')

    def editor_id(self, edid):
        """EditorID -> runtime form id via the offline master indexes (state/index) and the live load order:
        the running game keeps few EditorIDs in memory."""
        if getattr(self, '_indexes', None) is None:
            with self._lazy_lock:
                if getattr(self, '_indexes', None) is None:
                    import gzip
                    indexes = {}  # filled privately, then published whole: a parallel call never sees half of it
                    for path in sorted((Path(self.project.state_dir) / 'index').glob('*.json.gz')):
                        try:
                            with gzip.open(path) as f:
                                data = json.load(f)
                            indexes[data.get('master') or path.name[:-8]] = data.get('entries', {})
                        except (OSError, ValueError):
                            continue
                    self._indexes = indexes
        hits = []
        for master, entries in self._indexes.items():
            found = entries.get(edid)
            if not found:
                continue
            for entry in (found if isinstance(found[0], list) else [found]):
                hits.append((master, entry))
        if not hits:
            return self._editor_id_from_plugins(edid)
        hits.sort(key=lambda h: self.PREFERRED.index(h[1][0]) if h[1][0] in self.PREFERRED else 99)
        master, (rtype, form_id, name, _model) = hits[0]
        index = fnv_link.value_of(self.link.eval('GetModIndex %s' % fnv_link.quoted(master)))
        if index is None or index < 0 or index > 254:
            return None
        local = int(form_id, 16) & 0xFFFFFF
        return {'formId': _fid((int(index) << 24) | local), 'type': rtype, 'name': name, 'master': master,
                'candidates': len(hits)}

    def _editor_id_from_plugins(self, edid):
        """An EditorID defined in any active plugin (mods, DLC), from the load-order record index."""
        cache = self.__dict__.setdefault('_plugin_edids', {})
        if edid.lower() in cache:
            row = cache[edid.lower()]
        else:
            from .fnv_data import DataTools
            data = DataTools(self.project, self)
            try:
                entries = data._active()[0]
                files = ','.join(str(e['fileId']) for e in entries)
                row = data.index.db.execute('select owner, local, type, full from records where edid = ? collate nocase '
                                            'and file in (%s) limit 1' % files, (edid,)).fetchone()
            except Exception:
                row = None
            finally:
                if data._index is not None:
                    data._index.db.close()  # never keep the SQLite file open between calls
            cache[edid.lower()] = row
        if not row:
            return None
        owner, local, rtype, name = row
        index = fnv_link.value_of(self.link.eval('GetModIndex %s' % fnv_link.quoted(owner)))
        if index is None or index < 0 or index > 254:
            return None
        return {'formId': _fid((int(index) << 24) | local), 'type': rtype, 'name': name, 'master': owner, 'candidates': 1}

    def form_expr(self, form_id):
        value = int(form_id, 16)
        return '(BuildRef %d %d)' % (value >> 24, value & 0xFFFFFF)

    def names(self, form_ids):
        ids = [f for f in form_ids if f]
        vals = self.values(['GetName %s' % self.form_expr(f) for f in ids]) if ids else []
        return dict(zip(ids, vals))

    # ================================================================= milestone 5: reads
    def player(self):
        self.in_game()
        p = self.resolve('player')
        exprs = ['GetPos X', 'GetPos Y', 'GetPos Z', 'GetAngle Z', 'GetParentCell', 'GetName (player.GetParentCell)',
                 'GetAV Health', 'GetBaseAV Health', 'GetAV ActionPoints', 'GetBaseAV ActionPoints', 'GetLevel',
                 'GetAV XP', 'GetAV Karma', 'GetAV RadiationRads', 'IsInCombat', 'GetDead', 'IsPlayerSwimming']
        exprs += ['GetAV %s' % n for n in fnv_facts.SPECIAL + fnv_facts.SKILLS]
        v = self.values(exprs, p['ptr'])
        n = 17
        return {'formId': p['formId'], 'position': {'x': v[0], 'y': v[1], 'z': v[2]}, 'heading': v[3],
                'cell': {'formId': v[4], 'name': v[5]}, 'health': {'current': v[6], 'base': v[7]},
                'actionPoints': {'current': v[8], 'base': v[9]}, 'level': v[10], 'xp': v[11], 'karma': v[12],
                'rads': v[13], 'inCombat': bool(v[14]), 'dead': bool(v[15]), 'swimming': bool(v[16]),
                'special': dict(zip(fnv_facts.SPECIAL, v[n:n + 7])), 'skills': dict(zip(fnv_facts.SKILLS, v[n + 7:])),
                'effects': self.effects('player', _checked=True)['effects'],
                'uses': ['xNVSE expressions (GetPos, GetAV, GetParentCell...)', 'fnv_effects engine read']}

    def ref(self, ref):
        self.in_game()
        r = self.resolve(ref)
        exprs = ['GetBaseObject', 'GetName', 'GetPos X', 'GetPos Y', 'GetPos Z', 'GetAngle X', 'GetAngle Y',
                 'GetAngle Z', 'GetScale', 'GetParentCell', 'GetDisabled', 'GetOwner', 'GetLocked', 'GetLockLevel',
                 'GetDead', 'GetAV Health', 'GetIsReference player']
        v = self.values(exprs, r['ptr'])
        head = self.form_ids([r['ptr']])[0]
        base = self.form_ids([self.u32s([r['ptr'] + 0x20])[0]])[0]
        info = {'formId': r['formId'], 'ptr': '0x%08X' % r['ptr'], 'formType': head['formType'] if head else None,
                'flags': [name for bit, name in fnv_facts.FORM_FLAGS.items() if head and head['flags'] & bit],
                'class': self.rtti([r['ptr']])[r['ptr']]['class'],
                'base': {'formId': v[0] or (base or {}).get('formId'), 'formType': (base or {}).get('formType'), 'name': v[1]},
                'position': {'x': v[2], 'y': v[3], 'z': v[4]}, 'rotation': {'x': v[5], 'y': v[6], 'z': v[7]},
                'scale': v[8], 'cell': v[9], 'disabled': bool(v[10]), 'owner': v[11], 'locked': bool(v[12]),
                'lockLevel': v[13], 'isPlayer': bool(v[16])}
        if info['class'] and ('Character' in info['class'] or 'Creature' in info['class'] or info['class'] == 'Actor'):
            info.update(dead=bool(v[14]), health=v[15])
        info['extraData'] = self.extra_data(ref, _resolved=r)['entries']
        info['uses'] = ['xNVSE expressions', 'TESForm/TESObjectREFR layout (decomp, checked offsets)', 'MSVC RTTI']
        return info

    def extra_data(self, ref, _resolved=None):
        if not _resolved:
            self.in_game()
        r = _resolved or self.resolve(ref)
        head = self.u32s([r['ptr'] + FACTS['TESObjectREFR.extraList']['offset'] + FACTS['BaseExtraList.head']['offset']])[0]
        entries, node, seen = [], head, set()
        while node and node not in seen and len(entries) < 200:
            seen.add(node)
            raw = self.reads([(node, 0x14)])[0]
            if raw is None:
                break
            kind, nxt = raw[4], struct.unpack('<I', raw[8:12])[0]
            entry = {'type': kind, 'name': fnv_facts.EXTRA_TYPES.get(kind, 'Unknown_%02X' % kind), 'ptr': '0x%08X' % node}
            field = fnv_facts.EXTRA_FIELDS.get(kind)
            if field:
                label, ftype = field
                if ftype in ('form', 'ptr'):
                    value = struct.unpack('<I', raw[12:16])[0]
                    entry[label] = ('0x%08X' % value) if ftype == 'ptr' else value
                else:
                    entry[label] = struct.unpack(SIMPLE_TYPES[ftype][0], raw[12:12 + SIMPLE_TYPES[ftype][1]])[0]
            entries.append(entry)
            node = nxt
        forms = [(e, k) for e in entries for k, v in list(e.items()) if isinstance(v, int) and k not in ('type',)
                 and fnv_facts.EXTRA_FIELDS.get(e['type'], (None, ''))[1] == 'form']
        decoded = self.form_ids([e[k] for e, k in forms])
        for (e, k), d in zip(forms, decoded):
            e[k] = d['formId'] if d else None
        for e in entries:
            if e['type'] == 0x2A and e.get('lockData') not in (None, '0x00000000'):
                lock = self.reads([(int(e['lockData'], 16), 12)])[0]
                if lock:
                    level, key, flags = struct.unpack('<IIB', lock[:9])
                    e['lock'] = {'level': level, 'key': (self.form_ids([key])[0] or {}).get('formId'), 'flags': flags}
        return {'formId': r['formId'], 'entries': entries,
                'uses': ['BaseExtraList/BSExtraData layout (decomp, checked)', 'xNVSE ExtraDataType names and fields']}

    def refs_near(self, radius=2000.0, form_type=0, limit=50, base=None):
        self.in_game()
        p = self.resolve('player')
        result = self.link.eval('GetRefs %d 1 0 %.1f' % (int(form_type), float(radius)), p['ptr'])
        element = result.get('result') or {}
        if not result.get('ok') or element.get('type') != 'array':
            raise LiveError('GETREFS_FAILED', 'GetRefs returned no array', result)
        items = [i for i in element.get('items', []) if i.get('type') == 'form' and i.get('ptr')]
        total = element.get('size', len(items))
        out = []
        ptrs = [int(i['ptr'], 16) for i in items]
        per = ['GetBaseObject', 'GetName', 'GetDistance player', 'GetPos X', 'GetPos Y', 'GetPos Z', 'GetDisabled', 'GetDead']
        ops = [('eval', hexs(e), word(ptr)) for ptr in ptrs for e in per]
        values = [fnv_link.value_of(r) for r in self.link.batch(ops)] if ops else []
        for k, item in enumerate(items):
            v = values[k * len(per):(k + 1) * len(per)]
            if base and v[0] != base.upper().removeprefix('0X').zfill(8):
                continue
            out.append({'formId': item['formId'], 'base': v[0], 'name': v[1], 'distance': v[2],
                        'position': {'x': v[3], 'y': v[4], 'z': v[5]}, 'disabled': bool(v[6]), 'dead': bool(v[7])})
        out.sort(key=lambda e: e['distance'] if e['distance'] is not None else 1e12)
        return {'radius': radius, 'formType': form_type, 'count': len(out), 'arrayTruncated': total > len(items),
                'refs': out[:limit], 'truncated': len(out) > limit, 'uses': ['xNVSE GetRefs', 'xNVSE expressions']}

    def actor_ai(self, ref):
        self.in_game()
        r = self.resolve(ref)
        exprs = ['GetCurrentAIPackage', 'GetCurrentAIProcedure', 'GetCurrentPackage', 'IsInCombat', 'GetCombatTarget',
                 'GetDetected player', 'player.GetDetected %s' % r['expr'] if r['expr'] != 'player' else '0',
                 'GetDead', 'GetUnconscious', 'GetSitting', 'GetSleeping', 'GetKnockedState', 'GetRestrained',
                 'GetPlayerTeammate', 'GetIsCreature', 'GetAV Aggression', 'GetAV Confidence', 'GetAV Assistance',
                 'GetAV Responsibility', 'GetAV Mood']
        v = self.values(exprs, r['ptr'])
        process = self.u32s([r['ptr'] + FACTS['MobileObject.baseProcess']['offset']])[0]
        level = self.u32s([process + FACTS['BaseProcess.processType']['offset']])[0] if process else None
        package = v[2]
        return {'formId': r['formId'], 'package': {'formId': package, 'type': v[0], 'procedure': v[1]},
                'processLevel': {0: 'high', 1: 'middle_high', 2: 'middle_low', 3: 'low'}.get(level, level),
                'processPtr': '0x%08X' % process if process else None,
                'combat': {'inCombat': bool(v[3]), 'target': v[4]},
                'detection': {'detectsPlayer': bool(v[5]), 'detectedByPlayer': bool(v[6])},
                'state': {'dead': bool(v[7]), 'unconscious': bool(v[8]), 'sitting': v[9], 'sleeping': v[10],
                          'knocked': v[11], 'restrained': bool(v[12]), 'teammate': bool(v[13]), 'creature': bool(v[14])},
                'aiValues': dict(zip(['Aggression', 'Confidence', 'Assistance', 'Responsibility', 'Mood'], v[15:20])),
                'uses': ['vanilla/xNVSE AI functions (GetCurrentAIPackage, GetCurrentAIProcedure, GetCurrentPackage)',
                         'MobileObject.m_pBaseProcess, BaseProcess.m_eProcessType (decomp, checked)']}

    def quest(self, quest):
        self.in_game()
        q = self.resolve(quest)
        v = self.values(['GetQuestRunning %s' % q['expr'], 'GetStage %s' % q['expr'], 'GetQuestCompleted %s' % q['expr'],
                         'GetName %s' % q['expr']])
        stage_ptrs = self.walk_list(q['ptr'] + FACTS['TESQuest.stages']['offset'], 300)
        stages = [{'index': raw[0], 'done': bool(raw[1])} for raw in self.reads([(p, 2) for p in stage_ptrs]) if raw] \
            if stage_ptrs else []
        objectives = []
        objs = self.walk_list(q['ptr'] + FACTS['TESQuest.objectives']['offset'], 200)
        raws = self.reads([(o, 0x24) for o in objs])
        texts = self.link.batch([('rs', word(struct.unpack('<I', raw[8:12])[0]), '400') for raw in raws if raw]) if objs else []
        for raw, text in zip([r for r in raws if r], texts):
            index, state = struct.unpack('<I', raw[4:8])[0], struct.unpack('<I', raw[0x20:0x24])[0]
            objectives.append({'index': index, 'text': text.get('text') if text.get('ok') else None,
                               'displayed': bool(state & 1), 'completed': bool(state & 2), 'state': state})
        return {'formId': q['formId'], 'name': v[3], 'running': bool(v[0]), 'stage': v[1], 'completed': bool(v[2]),
                'stages': stages, 'objectives': objectives,
                'variables': self._variables(q['ptr'], 'quest')['variables'],
                'uses': ['vanilla quest functions', 'TESQuest stage/objective lists (decomp, checked)',
                         'BGSQuestObjective layout (decomp); state bit 1 displayed, 2 completed (xNVSE)']}

    def _variables(self, ptr, kind):
        if kind == 'quest':
            script = self.u32s([ptr + FACTS['TESQuest.script']['offset']])[0]
            local = self.u32s([ptr + FACTS['TESQuest.locals']['offset']])[0]
        else:
            script = local = None
            for e in self.extra_data(None, _resolved={'ptr': ptr, 'formId': None})['entries']:
                if e['type'] == 0x0D:
                    raw = self.reads([(int(e['ptr'], 16) + 0x0C, 8)])[0]
                    if raw:
                        script, local = struct.unpack('<II', raw)
        if not script:
            return {'script': None, 'variables': []}
        infos = self.walk_list(script + FACTS['Script.variables']['offset'], 500)
        raws = self.reads([(i, 0x20) for i in infos])
        names = self.link.batch([('rs', word(struct.unpack('<I', raw[0x18:0x1C])[0]), '200') for raw in raws if raw]) if infos else []
        values = {}
        if local:
            vars_list = self.u32s([local + FACTS['ScriptLocals.variables']['offset']])[0]
            var_ptrs = self.walk_list(vars_list, 500) if vars_list else []
            for raw in self.reads([(v, 16) for v in var_ptrs]) if var_ptrs else []:
                if raw:
                    values[struct.unpack('<I', raw[:4])[0]] = raw[8:16]
        out = []
        for raw, name in zip([r for r in raws if r], names):
            idx, vtype = struct.unpack('<I', raw[:4])[0], raw[0x10]
            data = values.get(idx)
            if data is None:
                value = None
            elif vtype == 4:
                value = _fid(struct.unpack('<I', data[:4])[0])
            else:
                value = struct.unpack('<d', data)[0]
            out.append({'index': idx, 'name': name.get('text') if name.get('ok') else None,
                        'type': {0: 'float', 1: 'int', 2: 'string', 3: 'array', 4: 'ref'}.get(vtype, vtype), 'value': value})
        return {'script': (self.form_ids([script])[0] or {}).get('formId'), 'variables': out}

    def variables(self, ref):
        self.in_game()
        r = self.resolve(ref)
        head = self.form_ids([r['ptr']])[0]
        kind = 'quest' if head and head['formType'] == 0x47 else 'ref'
        result = self._variables(r['ptr'], kind)
        return {'formId': r['formId'], 'kind': kind, **result,
                'uses': ['Script variable list and ScriptLocals (decomp layouts; xNVSE VariableInfo)']}

    def actor_values(self, ref, names=None):
        self.in_game()
        r = self.resolve_actor(ref)
        names = names or fnv_facts.ACTOR_VALUES
        bad = [n for n in names if n not in fnv_facts.ACTOR_VALUES]
        if bad:
            raise ValueError('Unknown actor values: %s' % ', '.join(bad))
        exprs = [e % n for n in names for e in ('GetAV %s', 'GetBaseAV %s', 'GetPermanentActorValue %s')]
        v = self.values(exprs, r['ptr'])
        effects = self.effects(ref, _checked=True).get('effects', [])
        out = {}
        for k, name in enumerate(names):
            cur, base, perm = v[3 * k:3 * k + 3]
            code = fnv_facts.ACTOR_VALUES.index(name)
            entry = {'current': cur, 'base': base, 'permanent': perm,
                     'permanentModifier': None if None in (perm, base) else perm - base,
                     'temporaryModifier': None if None in (cur, perm) else cur - perm,
                     'sources': [{'effect': e['effect'], 'source': e.get('sourceName') or e.get('source'),
                                  'magnitude': e['magnitude']} for e in effects if e.get('actorValue') == code]}
            out[name] = entry
        return {'formId': r['formId'], 'values': out,
                'uses': ['vanilla GetAV/GetBaseAV/GetPermanentActorValue', 'fnv_effects for the contributing effects']}

    def effects(self, ref, _checked=False):
        if not _checked:
            self.in_game()
        r = self.resolve_actor(ref)
        target = r['ptr'] + FACTS['Actor.magicTarget']['offset']
        vtable = self.u32s([target])[0]
        if not vtable:
            raise LiveError('NOT_AN_ACTOR', '%s has no magic target' % ref)
        fn = self.u32s([vtable + 4 * FACTS['MagicTarget.GetEffectList']['vtableSlot']])[0]
        call = self.link.one('call', word(fn), 'i', word(target), '0')
        if not call.get('ok'):
            raise LiveError('CALL_FAILED', 'MagicTarget::GetEffectList failed', call)
        lst = int(call['eax'], 16)
        effects = []
        eff_ptrs = self.walk_list(lst, 300) if lst else []
        raws = self.reads([(e, 0x48) for e in eff_ptrs]) if eff_ptrs else []
        effect_items = [struct.unpack('<I', raw[0x0C:0x10])[0] if raw else 0 for raw in raws]
        wanted = sorted({p for p in effect_items if p})
        eis = dict(zip(wanted, self.reads([(p, 0x18) for p in wanted]))) if wanted else {}
        for eff, raw in zip(eff_ptrs, raws):
            if raw is None:
                continue
            elapsed, item, effect_item = struct.unpack('<fII', raw[4:16])
            active, applied, terminated = raw[0x10], raw[0x11], raw[0x13]
            magnitude, duration = struct.unpack('<ff', raw[0x1C:0x24])
            spell_type, source = struct.unpack('<I', raw[0x2C:0x30])[0], struct.unpack('<I', raw[0x3C:0x40])[0]
            ei = eis.get(effect_item)
            setting = struct.unpack('<I', ei[0x14:0x18])[0] if ei else 0
            av = struct.unpack('<I', ei[0x10:0x14])[0] if ei else None
            effects.append({'ptr': '0x%08X' % eff, 'item': item, 'setting': setting, 'source': source,
                            'active': bool(active), 'applied': bool(applied), 'terminated': bool(terminated),
                            'magnitude': magnitude, 'duration': duration, 'elapsed': elapsed, 'spellType': spell_type,
                            'actorValue': av if av is not None and av < len(fnv_facts.ACTOR_VALUES) else None})
        # Magic items are subobjects of their forms: RTTI gives the offset back to the TESForm.
        item_ptrs = [e['item'] for e in effects]
        info = self.rtti(item_ptrs)
        forms = self.form_ids([e['item'] - info.get(e['item'], {}).get('subobjectOffset', 0) if e['item'] else 0
                               for e in effects] + [e['setting'] for e in effects] + [e['source'] for e in effects])
        n = len(effects)
        ids = [f['formId'] for f in forms if f]
        named = self.names(ids)
        for k, e in enumerate(effects):
            item, setting, source = forms[k], forms[n + k], forms[2 * n + k]
            e['item'] = item['formId'] if item else None
            e['itemClass'] = info.get(item_ptrs[k], {}).get('class')
            e['itemName'] = named.get(e['item'])
            e['effect'] = setting['formId'] if setting else None
            e['effectName'] = named.get(e['effect'])
            e['source'] = source['formId'] if source else None
            e['sourceName'] = named.get(e['source'])
            e['actorValueName'] = fnv_facts.ACTOR_VALUES[e['actorValue']] if e['actorValue'] is not None else None
            del e['setting']
        return {'formId': r['formId'], 'count': n, 'effects': effects,
                'uses': ['MagicTarget at Actor+0x94, virtual GetEffectList (JIP LN layout, decomp sizes)',
                         'ActiveEffect/EffectItem layout (JIP LN GameEffects.h/GameForms.h)', 'MSVC RTTI']}

    def crosshair(self):
        self.in_game()
        result = self.link.eval('GetCrosshairRef')
        element = result.get('result') or {}
        if element.get('type') != 'form' or not element.get('ptr'):
            return {'ref': None}
        ptr = int(element['ptr'], 16)
        player = self.resolve('player')
        v = self.values(['GetBaseObject', 'GetName', 'GetDistance player'], ptr)
        return {'ref': element.get('formId'), 'base': v[0], 'name': v[1], 'distance': v[2],
                'class': self.rtti([ptr])[ptr]['class'], 'player': player['formId'], 'uses': ['xNVSE GetCrosshairRef']}

    def inventory(self, ref='player', limit=300):
        self.in_game()
        r = self.resolve(ref)
        count = fnv_link.value_of(self.link.eval('GetNumItems', r['ptr']))
        if count is None:
            raise LiveError('NO_INVENTORY', '%s has no inventory' % ref)
        n = min(int(count), limit)
        objs = self.link.batch([('eval', hexs('GetInventoryObject %d' % i), word(r['ptr'])) for i in range(n)])
        ids = [(o.get('result') or {}).get('formId') for o in objs]
        live = [i for i in ids if i]
        per = ['GetItemCount %s', 'GetEquipped %s', 'GetName %s']
        values = self.values([e % self.form_expr(i) for i in live for e in per], r['ptr']) if live else []
        items = []
        for k, form in enumerate(live):
            c, eq, name = values[3 * k:3 * k + 3]
            items.append({'formId': form, 'name': name, 'count': c, 'equipped': bool(eq)})
        return {'formId': r['formId'], 'count': int(count), 'items': items, 'truncated': int(count) > limit,
                'uses': ['xNVSE GetNumItems/GetInventoryObject', 'vanilla GetItemCount/GetEquipped']}

    def formid_live(self, plugin):
        self.ready()
        name = fnv_link.quoted(plugin)       # W4: free text inside a quoted expression
        v = self.values(['IsModLoaded %s' % name, 'GetModIndex %s' % name])
        loaded = bool(v[0])
        return {'plugin': plugin, 'loaded': loaded, 'index': '%02X' % int(v[1]) if loaded and v[1] is not None else None,
                'formIdPrefix': ('%02X' % int(v[1])) if loaded else None}

    def events(self, since=0):
        """The companion's events after `since`, oldest first, at most 200: `next` is the since for the rest."""
        self.ready()
        out = dict(self.link.events(since))
        shown = capped(out.get('events') or [])
        out.update(events=shown['items'], total=shown['total'], truncated=shown['truncated'])
        if shown['truncated']:
            out['next'] = shown['items'][-1].get('seq')
        return out

    def script_errors(self, since='last'):
        """Script errors from the console log (with script, mod and message) since the last call."""
        logs = self.game.logs(['console'], since=since if since != 'last' else 'last', max_lines=2000)
        lines = logs.get('console', {}).get('lines', [])
        errors, current = [], None
        for line in lines:
            m = re.match(r'Error in script ([0-9A-Fa-f]{8})(?: \((.*?)\))? in mod (.*)$', line.strip())
            if m:
                current = {'script': m.group(1).upper(), 'scriptName': m.group(2), 'mod': m.group(3), 'message': []}
                errors.append(current)
            elif current is not None and line.strip() and not line.startswith(('GetActorValue', 'GetPos')):
                current['message'].append(line.strip())
                if len(current['message']) >= 6:
                    current = None
        for e in errors:
            e['message'] = '\n'.join(e['message'])
        return {'errors': errors, 'count': len(errors), 'log': logs.get('console', {}).get('path'),
                'uses': ['console log (scof) written by the engine companion']}

    # ================================================================= milestone 5/6: console and scripts
    # Console lines fnv_console refuses (G14): the bridge's own tools do these with their bookkeeping (quit log,
    # session, the dev profile's save slots). Matched on the first word, case-insensitively, also after `ref.`.
    # Long and short names from the engine map's command table (ExitGame is also `exit`, CenterOnWorld `cow`; JIP
    # adds RunBatchScript; S5 review I2).
    _LOAD = 'fnv_game restart start=save:<name> (or fnv_test_save action=load for a test save)'
    _SAVE = 'fnv_test_save action=save'
    _BATCH = 'fnv_test steps (one console step per line)'
    CONSOLE_INSTEAD = {'qqq': 'fnv_game quit', 'quitgame': 'fnv_game quit', 'exitgame': 'fnv_game quit', 'exit': 'fnv_game quit',
                       'restartgame': 'fnv_game restart', 'load': _LOAD, 'loadgame': _LOAD, 'con_loadgame': _LOAD,
                       'save': _SAVE, 'savegame': _SAVE, 'forcesave': _SAVE, 'systemsave': _SAVE, 'autosave': _SAVE,
                       'cow': 'fnv_move', 'centeronworld': 'fnv_move',
                       'bat': _BATCH, 'runbatch': _BATCH, 'runbatchscript': _BATCH}
    CONSOLE_REFUSED = set(CONSOLE_INSTEAD)

    @classmethod
    def console_refused(cls, line):
        """The refused command word of a console line, or None."""
        first = (line or '').strip().split(None, 1)[0] if (line or '').strip() else ''
        command = first.rsplit('.', 1)[-1].lower()
        return command if command in cls.CONSOLE_REFUSED else None

    @classmethod
    def script_refused(cls, source):
        """The first refused command word anywhere in a script, or None (W7). Every word outside quoted text and
        `;` comments counts, so `(QuitGame)`, a quit on a later line and one inside Begin/End are caught. A variable
        that happens to share a refused name (`save`, `load`, `exit`) is refused too: rename it."""
        for line in (source or '').splitlines():
            code = re.sub(r'"[^"]*"?', ' ', line).split(';', 1)[0]
            for word in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', code):
                if word.lower() in cls.CONSOLE_REFUSED:
                    return word.lower()
        return None

    def run_console(self, line, ref=None):
        if '\n' in line or len(line) > 500:
            raise ValueError('One console line of at most 500 characters')
        refused = self.console_refused(line)
        if refused:
            raise LiveError('CONSOLE_REFUSED', '%s is not run through fnv_console. Next: use %s.'
                            % (refused, self.CONSOLE_INSTEAD[refused]), {'command': refused})
        self.ready()
        r = self.resolve(ref) if ref else None
        self.changing('console: ' + line)
        result = self.console(line, r['ptr'] if r else 0)
        self.journal.add({'kind': 'console', 'line': line, 'ref': r['formId'] if r else None, 'undo': None,
                          'output': result['output'][:20]})
        shown = capped(result['output'], 400)
        return dict(result, output=shown['items'], outputTotal=shown['total'], outputTruncated=shown['truncated'])

    def run_script(self, source, ref=None):
        refused = self.script_refused(source)
        if refused:                    # the same commands as fnv_console refuses (S5 review M10, W7)
            raise LiveError('CONSOLE_REFUSED', '%s is not run through fnv_run_script. Next: use %s.'
                            % (refused, self.CONSOLE_INSTEAD[refused]), {'command': refused})
        self.ready()
        r = self.resolve(ref) if ref else None
        self.changing('script')
        text = source.strip()
        single = '\n' not in text and not text.lower().startswith('begin')
        if single:
            result = self.link.eval(text, r['ptr'] if r else 0)
        else:
            if not text.lower().startswith('begin'):
                text = 'Begin Function { }\n%s\nEnd' % text
            result = self.link.run(text, r['ptr'] if r else 0)
        substituted = {}
        if result.get('error') == 'compile_failed' or not result.get('ok'):
            # The running game keeps few EditorIDs, so its compiler cannot resolve most base forms by name:
            # retry with the EditorIDs it does not know replaced by their runtime form ids.
            retry, substituted = self.substitute_editor_ids(text)
            if substituted:
                result = self.link.eval(retry, r['ptr'] if r else 0) if single else self.link.run(retry, r['ptr'] if r else 0)
        self.journal.add({'kind': 'script', 'source': source[:2000], 'ref': r['formId'] if r else None, 'undo': None})
        return {'compiled': result.get('error') != 'compile_failed', 'ok': result.get('ok'),
                'result': element_value(result.get('result') or {}), 'resultType': (result.get('result') or {}).get('type'),
                'scriptErrors': result.get('scriptErrors'), 'output': result.get('output'), 'mode': 'expression' if single else 'script',
                'substitutedEditorIds': substituted or None}

    KEYWORDS = {'begin', 'end', 'if', 'elseif', 'else', 'endif', 'set', 'to', 'let', 'return', 'short', 'int', 'long',
                'float', 'ref', 'reference', 'array_var', 'string_var', 'player', 'playerref', 'while', 'loop', 'foreach',
                'function', 'call', 'scn', 'scriptname', 'gamemode', 'menumode'}

    # Record types whose EditorIDs the running game keeps, so its compiler takes the name; a form id in their
    # place fails ("set <form id> to" is not a variable). Seen live 2026-11-11: globals compile by name.
    RUNTIME_NAMED = {'GLOB'}

    def _commands(self):
        """Every script command name and short name (lower case), or None when the engine map cannot be read."""
        commands = getattr(self, '_command_names', None)
        if commands is None:
            try:
                found = set()
                for c in self.engine().commands:
                    found.add(c['name'].lower())
                    if c.get('short'):
                        found.add(c['short'].lower())
            except Exception:  # noqa: BLE001 - no engine map: the callers decide what that means
                return None
            commands = self._command_names = found
        return commands

    def substitute_editor_ids(self, text):
        """Replace EditorIDs found in the offline indexes (not commands, keywords or quoted text) with their runtime
        form ids, which the in-game compiler accepts. Returns (new text, {EditorID: form id})."""
        commands = self._commands() or set()
        out, subs = [], {}
        # The script's own name (scn) and its declared variables are names, not forms.
        protected = set()
        for line in text.replace('\r', '').split('\n'):
            words = line.split(';', 1)[0].split()
            if len(words) >= 2 and words[0].lower() in ('scn', 'scriptname', 'short', 'int', 'long', 'float', 'ref',
                                                        'reference', 'array_var', 'string_var'):
                protected.update(w.lower().rstrip(',') for w in words[1:])
        for k, part in enumerate(re.split(r'("[^"]*")', text)):
            if k % 2:  # quoted string
                out.append(part)
                continue

            def repl(m):
                token = m.group(0)
                if token.lower() in self.KEYWORDS or token.lower() in commands or token.lower() in protected or \
                        FORM_ID.match(token) and len(token) == 8:
                    return token
                if token not in subs:
                    found = self.editor_id(token)
                    if not found or found.get('type') in self.RUNTIME_NAMED:
                        return token
                    subs[token] = found['formId']
                return subs[token]
            out.append(re.sub(r'(?<![\w.])[A-Za-z_][A-Za-z0-9_]{2,127}(?![\w.])', repl, part))
        return ''.join(out), subs

    # ================================================================= milestone 6: memory and engine
    def _layout(self, cls):
        if not cls:
            return None
        try:
            return self.engine().class_layout(cls)[1]
        except fnv_engine.EngineMapError:
            return None

    def _fields(self, cls, base_offset=0, depth=0):
        """All fields of a class including base classes, with absolute offsets (bases laid out in order)."""
        layout = self._layout(cls)
        if not layout or depth > 8:
            return []
        out, offset = [], base_offset
        for base in layout.get('bases') or []:
            out += self._fields(base, offset, depth + 1)
            size = (self._layout(base) or {}).get('size') or 0
            offset += size
        for f in layout.get('fields') or []:
            if f.get('offset') is not None:
                out.append({**f, 'absolute': base_offset + f['offset'], 'class': cls})
        return out

    def locate(self, target, path=None, cls=None):
        """target (address | ref spec) + dotted field path -> (address, type, class chain)."""
        steps = []
        if isinstance(target, str) and re.match(r'^0x[0-9A-Fa-f]{1,8}$', target):
            address = int(target, 16)
        else:
            address = self.resolve(target)['ptr']
        if cls:
            current_class = cls
        else:
            current_class = self.rtti([address])[address]['class'] if address else None
        ftype = current_class
        parts = [p for p in (path or '').split('.') if p]
        for position, part in enumerate(parts):
            if not current_class:
                raise LiveError('NO_LAYOUT', 'No class known at 0x%08X to look up %r' % (address, part))
            fields = self._fields(current_class)
            match = next((f for f in fields if f['name'] == part), None) or \
                next((f for f in fields if f['name'].lower() == part.lower()), None)
            if not match:
                raise LiveError('NO_FIELD', '%s has no field %r' % (current_class, part),
                                {'fields': [f['name'] for f in fields][:200]})
            steps.append({'field': match['name'], 'class': match['class'], 'offset': '0x%X' % match['absolute'],
                          'type': match['type'], 'checked': match.get('checked'), 'span': match.get('span')})
            field_address = address + match['absolute']
            ftype = match['type'].strip()
            if ftype.endswith('*') and position < len(parts) - 1:
                pointer = self.u32s([field_address])[0]
                if not pointer:
                    raise LiveError('NULL_POINTER', '%s is null' % match['name'], {'steps': steps})
                address = pointer
                pointee = ftype[:-1].strip()
                runtime = self.rtti([pointer])[pointer]['class']
                current_class = runtime if runtime and self._layout(runtime) else pointee
            else:
                address = field_address
                current_class = ftype if self._layout(ftype) else None
        return address, ftype, steps

    def mem_read(self, target, path=None, as_type=None, length=None, cls=None):
        self.ready()
        address, ftype, steps = self.locate(target, path, cls)
        kind = as_type or ftype
        span = steps[-1].get('span') if steps else None
        if not as_type and kind not in SIMPLE_TYPES and not (kind or '').endswith('*') and not self._layout(kind) \
                and span in (1, 2, 4):
            kind = {1: 'u8', 2: 'u16', 4: 'u32'}[span]   # enum or typedef: an integer of the field's size
        out = {'address': '0x%08X' % address, 'type': kind, 'declaredType': ftype, 'path': steps}
        if kind in SIMPLE_TYPES or (kind and kind.endswith('*')):
            fmt, size = SIMPLE_TYPES.get(kind, ('<I', 4))
            raw = self.link.read(address, size)
            value = struct.unpack(fmt, raw)[0]
            out.update(value=('0x%08X' % value) if kind.endswith('*') or kind == 'ptr' else value, hex=raw.hex())
            if kind.endswith('*') and value:
                out['pointee'] = self.rtti([value])[value]
        elif kind in ('cstr', 'char*'):
            text = self.link.one('rs', word(address), '1000')
            out.update(value=text.get('text'))
        else:
            size = length or (self._layout(kind) or {}).get('size') or 64
            raw = self.link.read(address, min(int(size), 4096))
            out.update(hex=raw.hex(), size=len(raw))
            fields = self._fields(kind) if self._layout(kind) else []
            decoded = {}
            for f in fields:
                t = f['type'].strip()
                spec = SIMPLE_TYPES.get(t) or (('<I', 4) if t.endswith('*') else None)
                if spec and f['absolute'] + spec[1] <= len(raw):
                    v = struct.unpack(spec[0], raw[f['absolute']:f['absolute'] + spec[1]])[0]
                    decoded[f['name']] = ('0x%08X' % v) if t.endswith('*') else v
            if decoded:
                out['fields'] = decoded
        out['uses'] = ['engine map class layouts (decomp headers)', 'MSVC RTTI for runtime classes']
        return out

    def _encode(self, kind, value):
        if kind in SIMPLE_TYPES or kind.endswith('*'):
            fmt, _ = SIMPLE_TYPES.get(kind, ('<I', 4))
            if fmt == '<?':
                return struct.pack(fmt, as_bool('value', value))
            # range-checked per type: an out-of-range value is INVALID_ARGUMENT naming the range (G12)
            return struct.pack(fmt, ranged('value', value, {'<f': 'f32', '<d': 'f64'}.get(fmt, fmt)))
        if kind == 'bytes':
            try:
                return bytes.fromhex(str(value))
            except ValueError:
                raise ValueError('value: expected hex bytes such as "0A FF 00" for bytes, got %r' % (value,)) from None
        raise ValueError('Cannot write a value of type %s (give as_type: u8/u16/u32/i32/f32/f64/ptr/bytes)'
                         % readable_type(kind))

    def mem_write(self, target, value, path=None, as_type=None, cls=None, note=None):
        self.ready()
        address, ftype, steps = self.locate(target, path, cls)
        kind = as_type or ftype
        span = steps[-1].get('span') if steps else None
        if not as_type and kind not in SIMPLE_TYPES and not (kind or '').endswith('*') and not self._layout(kind) \
                and span in (1, 2, 4):
            kind = {1: 'u8', 2: 'u16', 4: 'u32'}[span]
        data = self._encode(kind, value)
        self.changing('memory write')
        # Intent first (S3.2): a timeout on the write still leaves an entry whose undo puts the old bytes back.
        seen = self.link.read(address, len(data))
        entry = self.journal.add({'kind': 'memory', 'state': 'pending', 'address': '0x%08X' % address, 'type': kind,
                                  'old': seen.hex(), 'new': data.hex(), 'target': target, 'path': path, 'note': note,
                                  'undo': {'op': 'write', 'address': '0x%08X' % address, 'hex': seen.hex()}})
        try:
            old = self.link.write(address, data)
        except LinkError as e:
            if e.code == 'MEMORY_UNWRITABLE':     # refused: nothing landed, nothing to undo (S3 review I2)
                self.journal.mark_undone([entry['id']])
            raise
        self.journal.confirm(entry['id'], {'old': old.hex(),
                                           'undo': {'op': 'write', 'address': '0x%08X' % address, 'hex': old.hex()}})
        return {'address': '0x%08X' % address, 'type': kind, 'old': old.hex(), 'new': data.hex(), 'path': steps,
                'journalId': entry['id'], 'changed': old != data}

    def form_edit(self, form, field, value, as_type=None):
        result = self.mem_write(form, value, path=field, as_type=as_type, note='form edit')
        return {**result, 'form': form, 'field': field}

    def _signature(self, function):
        engine = self.engine()
        address = engine.resolve(function)
        record = engine.summary(address)
        fn = engine.functions.get(address, {})
        return address, record, fn.get('signature') or '', fn.get('proof')

    def engine_call(self, function, this=None, args=None, returns='int', convention=None, allow_unproven=False):
        self.ready()
        if re.match(r'^0x[0-9A-Fa-f]{6,8}$', function or ''):
            address = int(function, 16)
            record, signature, proof = {'address': function, 'status': 'raw address'}, '', None
            try:
                address, record, signature, proof = self._signature(function)
            except Exception:
                pass
        else:
            address, record, signature, proof = self._signature(function)
        status = record.get('status')
        if status not in ('proven', 'class_proven') and not allow_unproven:
            raise LiveError('UNPROVEN_FUNCTION', '%s is %s, not proven: pass allow_unproven=true to call it anyway'
                            % (record.get('name', function), status), {'function': record})
        conv = convention or next((c for c in ('thiscall', 'fastcall', 'stdcall', 'cdecl') if '__' + c in signature), None) \
            or ('thiscall' if '::' in (record.get('name') or '') and this is not None else 'cdecl')
        words = []
        this_ptr = 0
        if this is not None:
            this_ptr = int(this, 16) if isinstance(this, str) and this.lower().startswith('0x') else self.resolve(str(this))['ptr']
        for arg in args or []:
            kind, value = (arg.get('type', 'int'), arg.get('value')) if isinstance(arg, dict) else ('int', arg)
            if kind in ('int', 'uint', 'bool'):
                words.append(int(value, 0) if isinstance(value, str) else int(value))
            elif kind == 'ptr':
                words.append(int(str(value), 16) if str(value).lower().startswith('0x') else int(value))
            elif kind in ('ref', 'form'):
                words.append(self.resolve(str(value))['ptr'])
            elif kind == 'float':
                words.append(struct.unpack('<I', struct.pack('<f', float(value)))[0])
            elif kind == 'double':
                lo, hi = struct.unpack('<II', struct.pack('<d', float(value)))
                words += [lo, hi]
            elif kind == 'string':
                words.append(self._game_string(str(value)))
            else:
                raise ValueError('Argument type %r: use int, bool, ptr, ref, form, float, double or string' % kind)
        ecx = edx = 0
        if conv == 'thiscall':
            ecx = this_ptr
        elif conv == 'fastcall':
            regs = ([this_ptr] if this is not None else []) + words
            ecx, edx = (regs + [0, 0])[:2]
            words = regs[2:]
        elif this is not None:
            words = [this_ptr] + words
        self.changing('engine call %s' % (record.get('name') or function))
        ret = {'float': 'f', 'double': 'f', 'void': 'v'}.get(returns, 'i')
        # Unproven functions run under the companion's guard (wire 2): a fault returns instead of crashing the game.
        guarded = status not in ('proven', 'class_proven')
        result = self.link.one('call', word(address), ret + ('g' if guarded else ''), word(ecx), word(edx),
                               *[word(w) for w in words])
        if result.get('fault'):
            fault = result['fault']
            raise LiveError('CALL_FAULTED', '%s faulted at %s (%s %s); the game kept running, but state the function '
                            'had half changed may remain (stateSuspect). Next: check the arguments and the convention, '
                            'or call fnv_health.' % (record.get('name') or function, fault.get('address'),
                                                     fault.get('access'), fault.get('code')), result)
        if not result.get('ok'):
            raise LiveError('CALL_REJECTED', 'The companion refused the call: %s' % result.get('error'), result)
        eax = int(result['eax'], 16)
        value = {'int': struct.unpack('<i', struct.pack('<I', eax))[0], 'uint': eax, 'bool': bool(eax & 0xFF),
                 'ptr': '0x%08X' % eax, 'float': result.get('st0'), 'double': result.get('st0'), 'void': None}.get(returns, eax)
        if returns in ('form', 'ref'):
            value = self.form_ids([eax])[0]
        entry = self.journal.add({'kind': 'engine_call', 'function': record.get('name') or function,
                                  'address': '0x%08X' % address, 'convention': conv, 'words': ['0x%X' % w for w in words],
                                  'this': '0x%08X' % this_ptr if this is not None else None, 'undo': None})
        return {'function': record, 'convention': conv, 'signature': signature or None, 'proof': proof,
                'result': value, 'eax': result['eax'], 'st0': result.get('st0'), 'scriptErrors': result.get('scriptErrors'),
                'journalId': entry['id']}

    def scratch(self, data):
        """Copy bytes into a new game heap block (FormHeap_Allocate 0x00401000) and return its address. The block is
        not freed: use it for small call arguments (a vector, a handle)."""
        call = self.link.one('call', '401000', 'i', '0', '0', word(max(16, len(data))))
        address = int(call['eax'], 16) if call.get('ok') else 0
        if not address:
            raise LiveError('ALLOC_FAILED', 'FormHeap_Allocate returned null')
        self.link.write(address, bytes(data))
        return address

    def _game_string(self, text):
        """Copy a string into game memory with FormHeap_Allocate (0x00401000, proven) and return its address."""
        data = text.encode('cp1252', 'cp1252-bytes') + b'\0'
        call = self.link.one('call', '401000', 'i', '0', '0', word(len(data)))
        address = int(call['eax'], 16) if call.get('ok') else 0
        if not address:
            raise LiveError('ALLOC_FAILED', 'FormHeap_Allocate returned null')
        self.link.write(address, data)
        return address

    def setting_set(self, name, value):
        self.ready()
        is_ini = ':' in name
        if not re.match(r'^[A-Za-z][A-Za-z0-9_ ]{1,80}(:[A-Za-z0-9_ ]{1,40})?$', name):
            raise ValueError('Setting names look like fCombatDistance or bAlwaysActive:General')
        string = name[0] == 's'
        if string:
            if is_ini:
                raise ValueError('String INI settings are not supported; numeric INI settings and game settings are')
            get, put = 'GetStringGameSetting "%s"' % name, 'SetGS %s "%s"' % (name, str(value).replace('"', ''))
        elif is_ini:
            get, put = 'GetNumericIniSetting "%s"' % name, 'SetNumericIniSetting "%s" %s' % (name, float(value))
        else:
            get, put = 'GetNumericGameSetting "%s"' % name, 'SetNumericGameSetting "%s" %s' % (name, float(value))
        old = fnv_link.value_of(self.link.eval(get))
        if old is None:
            raise LiveError('NO_SETTING', 'The running game has no setting %r' % name)
        self.changing('setting ' + name)
        result = self.console(put)
        new = fnv_link.value_of(self.link.eval(get))
        undo_value = old if string else repr(float(old))
        undo = put.replace(str(value) if string else str(float(value)), str(undo_value)) if not string else \
            'SetGS %s "%s"' % (name, old)
        entry = self.journal.add({'kind': 'setting', 'name': name, 'old': old, 'new': new, 'undo': {'op': 'console', 'line': undo}})
        info = None
        try:
            info = self.engine().statics_find(name.split(':')[0], 'Setting', 1)
        except Exception:
            pass
        return {'name': name, 'old': old, 'new': new, 'accepted': result['accepted'], 'journalId': entry['id'],
                'engineMap': info, 'changed': old != new}

    def script_set(self, target, kind, value, name=None, index=None):
        """kind: stage | objective_completed | objective_displayed | variable | global."""
        self.in_game()
        if kind == 'global':
            if not EDITOR_ID.match(target):
                raise ValueError('A global is given by its EditorID')
            old = fnv_link.value_of(self.link.eval('GetGlobalValue %s' % target))
            line, undo = 'set %s to %s' % (target, value), 'set %s to %s' % (target, old)
        elif kind == 'stage':
            q = self.resolve(target)
            old = fnv_link.value_of(self.link.eval('GetStage %s' % q['expr']))
            line, undo = 'SetStage %s %d' % (q['formId'], int(value)), None
        elif kind in ('objective_completed', 'objective_displayed'):
            q = self.resolve(target)
            fn = 'Completed' if kind == 'objective_completed' else 'Displayed'
            old = fnv_link.value_of(self.link.eval('GetObjective%s %s %d' % (fn, q['expr'], int(index))))
            line = 'SetObjective%s %s %d %d' % (fn, q['formId'], int(index), 1 if value else 0)
            undo = 'SetObjective%s %s %d %d' % (fn, q['formId'], int(index), 1 if old else 0)
        elif kind == 'variable':
            if not name or not EDITOR_ID.match(name):
                raise ValueError('variable needs the variable name')
            r = self.resolve(target)
            current = {v['name'].lower(): v for v in self.variables(target)['variables'] if v['name']}
            if name.lower() not in current:
                raise LiveError('NO_VARIABLE', '%s has no variable %s' % (target, name), {'variables': sorted(current)})
            old = current[name.lower()]['value']
            if not EDITOR_ID.match(target):
                raise LiveError('NEEDS_EDITOR_ID', 'Setting a variable needs the quest or reference EditorID (console '
                                '"set X.var to")', {'formId': r['formId']})
            line, undo = 'set %s.%s to %s' % (target, name, value), 'set %s.%s to %s' % (target, name, old)
        else:
            raise ValueError('kind must be stage, objective_completed, objective_displayed, variable or global')
        self.changing('script_set')
        result = self.console(line)
        entry = self.journal.add({'kind': 'script_set', 'what': kind, 'target': target, 'name': name, 'index': index,
                                  'old': old, 'new': value, 'line': line,
                                  'undo': {'op': 'console', 'line': undo} if undo else None})
        return {'line': line, 'accepted': result['accepted'], 'output': result['output'], 'old': old,
                'undoable': bool(undo), 'journalId': entry['id']}

    ACTIONS = {'evaluate_package': 'EvaluatePackage', 'stop_combat': 'StopCombat', 'kill': 'Kill',
               'resurrect': 'Resurrect 1', 'enable': 'Enable', 'disable': 'Disable', 'remove_package': 'RemoveScriptPackage',
               'unconscious_on': 'SetUnconscious 1', 'unconscious_off': 'SetUnconscious 0', 'restrain_on': 'SetRestrained 1',
               'restrain_off': 'SetRestrained 0'}
    TARGET_ACTIONS = {'start_combat': 'StartCombat %s', 'move_to': 'MoveTo %s', 'add_package': 'AddScriptPackage %s',
                      'play_idle': 'PlayIdle %s', 'cast': 'Cast %s player', 'equip': 'EquipItem %s', 'unequip': 'UnequipItem %s',
                      'add_item': 'AddItem %s 1', 'remove_item': 'RemoveItem %s 1', 'look_at': 'Look %s', 'say_to': 'SayTo %s'}

    def _item_form_id(self, item):
        """An item's form id (an EditorID resolved now); the name itself when it does not resolve."""
        if is_form_id(item):
            return item
        try:
            return self.resolve(item).get('formId') or item
        except (LiveError, ValueError):
            return item

    def actor_control(self, ref, action, target=None, value=None):
        self.in_game()
        r = self.resolve(ref)
        undo = None
        if action in self.ACTIONS:
            command = self.ACTIONS[action]
            # Undos that restore the state read before (N9), never a blind opposite command.
            if action in ('enable', 'disable'):
                was = self.values(['GetDisabled'], r['ptr'])[0]
                if was is not None:
                    undo = {'op': 'ref_enabled', 'ref': r['formId'], 'on': not was}
            elif action in ('unconscious_on', 'restrain_on'):
                flag = 'unconscious' if action == 'unconscious_on' else 'restrained'
                was = self.values([ACTOR_FLAGS[flag][0]], r['ptr'])[0]
                if was is not None:
                    undo = {'op': 'actor_flag', 'ref': r['formId'], 'flag': flag, 'on': bool(was)}
        elif action in self.TARGET_ACTIONS:
            if not target:
                raise ValueError('%s needs a target' % action)
            t = target if EDITOR_ID.match(target) and not FORM_ID.match(target) else self.resolve(target)['formId']
            command = self.TARGET_ACTIONS[action] % t
            if action == 'add_item':
                command = 'AddItem %s %d' % (t, int(value or 1))
                before = self.values(['GetItemCount %s' % t], r['ptr'])[0]
                if before is not None:
                    # the item by form id, so its undo meets one of the same item named by form id (round 4 M2)
                    undo = {'op': 'item_count', 'ref': r['formId'], 'item': self._item_form_id(t), 'count': int(before)}
            elif action == 'remove_item':
                command = 'RemoveItem %s %d' % (t, int(value or 1))
        elif action in ('set_av', 'force_av'):
            # SetAV changes the base value; for NPC skills the engine derives the value from SPECIAL, so only
            # ForceAV moves them. Both are read back, and the result says whether the value changed.
            if not target or target not in fnv_facts.ACTOR_VALUES:
                raise ValueError('%s needs target = actor value name' % action)
            before = self.values(['GetAV %s' % target, 'GetBaseAV %s' % target], r['ptr'])
            verb = 'SetAV' if action == 'set_av' else 'ForceAV'
            command = '%s %s %s' % (verb, target, _num(value))
            undo = '%s %s %s' % (verb, target, _num(before[1] if action == 'set_av' else before[0]))
            self.changing('actor_control')
            result = self.console(command, r['ptr'])
            after = self.values(['GetAV %s' % target, 'GetBaseAV %s' % target], r['ptr'])
            entry = self.journal.add({'kind': 'actor', 'ref': r['formId'], 'action': action, 'command': command,
                                      'old': before, 'new': after, 'undo': {'op': 'console_ref', 'ref': r['formId'], 'lines': [undo]}})
            out = {'action': action, 'command': command, 'accepted': result['accepted'], 'output': result['output'],
                   'before': {'current': before[0], 'base': before[1]}, 'after': {'current': after[0], 'base': after[1]},
                   'effective': after != before, 'undoable': True, 'journalId': entry['id']}
            if action == 'set_av' and after[0] == before[0]:
                out['hint'] = 'The current value did not change (NPC skills follow SPECIAL); use force_av'
            return out
        elif action == 'set_position':
            x, y, z = value
            old = self.values(['GetPos X', 'GetPos Y', 'GetPos Z'], r['ptr'])
            self.changing('actor_control')
            # Journaled before the moves: if a later axis fails, the axes already moved can still be undone.
            entry = self.journal.add({'kind': 'actor', 'ref': r['formId'], 'action': action, 'old': old, 'new': value,
                                      'undo': {'op': 'console_ref', 'ref': r['formId'],
                                               'lines': ['SetPos %s %s' % (a, o) for a, o in zip('XYZ', old)]}})
            for axis, v in zip('XYZ', (x, y, z)):
                self.console('SetPos %s %s' % (axis, float(v)), r['ptr'])
            return {'action': action, 'old': old, 'new': value, 'journalId': entry['id']}
        else:
            raise ValueError('Unknown action %r; use one of %s' % (action, ', '.join(
                sorted(list(self.ACTIONS) + list(self.TARGET_ACTIONS) + ['set_av', 'force_av', 'set_position']))))
        self.changing('actor_control')
        entry = self.journal.add({'kind': 'actor', 'state': 'pending', 'ref': r['formId'], 'action': action,
                                  'command': command, 'undo': undo})
        result = self.console(command, r['ptr'])
        self.journal.confirm(entry['id'], {'accepted': result['accepted']})
        return {'action': action, 'command': command, 'accepted': result['accepted'], 'output': result['output'],
                'undoable': bool(undo), 'journalId': entry['id']}

    def input(self, action, control=None, key=None, duration_ms=0, target=None, frames=None, button=None, mouse=None):
        """Bounded player input: tap/hold a control or DirectInput key (released by the companion even if the
        bridge goes away), aim at a ref, or activate a ref as the player. With frames (or button, or mouse as a
        (dx, dy) pair) the input runs through the frame-exact injector instead: it holds for exactly that many frames
        and the call returns when they have run (fnv_inject)."""
        self.in_game()
        if frames is not None or button is not None or mouse is not None:
            return self.inject(action, control, key, frames, button, mouse)
        if action == 'aim_at':
            t = self.resolve(target)
            heading = fnv_link.value_of(self.link.eval('player.GetHeadingAngle %s' % t['expr']))
            angle = fnv_link.value_of(self.link.eval('player.GetAngle Z'))
            if heading is None or angle is None:
                raise LiveError('AIM_FAILED', 'Could not compute the heading to %s' % target)
            new = (angle + heading) % 360
            self.changing('input')
            self.console('player.SetAngle Z %.3f' % new)
            entry = self.journal.add({'kind': 'input', 'action': action, 'target': t['formId'], 'old': angle, 'new': new,
                                      'undo': {'op': 'console', 'line': 'player.SetAngle Z %.3f' % angle}})
            return {'action': action, 'heading': new, 'turnedBy': heading, 'journalId': entry['id']}
        if action == 'activate':
            t = self.resolve(target)
            self.changing('input')
            result = self.console('Activate player', t['ptr'])
            self.journal.add({'kind': 'input', 'action': action, 'target': t['formId'], 'undo': None})
            return {'action': action, 'target': t['formId'], 'accepted': result['accepted'], 'output': result['output']}
        if action not in ('tap', 'hold'):
            raise ValueError('action must be tap, hold, aim_at or activate')
        if control is not None:
            scan = self._scan_of(control)
        elif key is not None:
            scan = int(key)
        else:
            raise ValueError('Give control (name or code) or key (DirectInput scan code)')
        duration_ms = int(duration_ms or 0)
        if action == 'hold' and not 0 < duration_ms <= 10000:
            raise ValueError('hold needs 1..10000 ms')
        self.changing('input')
        if action == 'tap':
            result = self.link.batch([('con', hexs('TapKey %d' % scan), '0')])[0]
        else:
            result = self.link.batch([('con', hexs('HoldKey %d' % scan), '0'),
                                      ('sched', word(duration_ms), hexs('ReleaseKey %d' % scan))])[0]
        self.journal.add({'kind': 'input', 'action': action, 'scan': scan, 'durationMs': duration_ms, 'undo': None})
        return {'action': action, 'scanCode': scan, 'durationMs': duration_ms, 'accepted': result.get('accepted'),
                'releaseScheduled': action == 'hold'}

    def _scan_of(self, control):
        """The DirectInput scan code (or 256 + mouse button) a control is bound to, from the game's bindings."""
        code = fnv_facts.CONTROLS.get(str(control).lower(), control)
        scan, mouse = self.values(['GetControl %d' % int(code), 'GetAltControl %d' % int(code)])
        if scan is None or scan in (255, -1):
            # Bound to a mouse button only (attack, aim): xNVSE key commands take 256 + button.
            if mouse is None or mouse in (255, -1):
                raise LiveError('UNBOUND_CONTROL', 'Control %s has no key or mouse button bound' % control)
            scan = 256 + int(mouse)
        return int(scan)

    def inject(self, action, control, key, frames, button, mouse):
        """Frame-exact input through the injector (fnv_inject): control codes 0..12 through the engine's own force
        call, other controls by their bound key or button, keys and mouse buttons and deltas directly."""
        from .fnv_inject import CONTROL_MAX, Injector, scan_to_job
        if frames is None:
            raise ValueError('Injected input needs frames=N (1 to 10000)')
        if action not in ('tap', 'hold'):
            raise ValueError('Injected input is tap or hold; frames=N is how long it lasts')
        injector = Injector.of(self)
        self.changing('input')
        if mouse is not None:
            dx, dy = mouse
            out = injector.mouse(dx, dy, frames)
        elif button is not None:
            out = injector.button(button, frames)
        elif control is not None:
            code = fnv_facts.CONTROLS.get(str(control).lower(), control)
            try:
                code = int(code)
            except (TypeError, ValueError):
                raise ValueError('Unknown control %r; use a control name or a code from 0 to 12' % control)
            if 0 <= code <= CONTROL_MAX:
                out = injector.control(code, frames)
            else:
                out = scan_to_job(injector, self._scan_of(control), frames)
        elif key is not None:
            out = injector.key(int(key), frames)
        else:
            raise ValueError('Give control (name or code), key, button or mouse for injected input')
        self.journal.add({'kind': 'input', 'action': action, 'frames': int(frames), 'injected': True, 'undo': None})
        return {'action': action, 'injected': True, 'frames': int(frames), **out}

    # ================================================================= journal and undo
    def maintain(self):
        """Server start (S2.6): fold the journal down to the running game's entries (the rest is archived next to it)
        and prune the state folder. Best effort: a failure is logged, never raised."""
        import logging
        from .fnv_game import game_processes
        from .fnv_state import prune
        log = logging.getLogger(__name__)
        out = {}
        try:
            running = game_processes()
            live = GameIdentity(*running[0]) if len(running) == 1 else None
            if len(running) <= 1:     # several games: nothing is sure to be stale, keep everything
                out['journal'] = self.journal.compact(live)
        except Exception as e:  # noqa: BLE001
            out['journalError'] = str(e)
        try:
            out['pruned'] = prune(self.project.state_dir, time.time())
        except Exception as e:  # noqa: BLE001
            out['pruneError'] = str(e)
        log.info('state maintenance at server start: %s', out)
        return out

    def journal_list(self, limit=100, include_undone=True):
        entries = self.journal.entries()
        if not include_undone:
            entries = [e for e in entries if not e['undone']]
        return {'count': len(entries), 'entries': entries[-limit:], 'path': str(self.journal.path)}

    def _apply_undo(self, entry):
        undo = entry.get('undo')
        if not undo:
            return {'id': entry['id'], 'undone': False, 'reason': 'not undoable (%s)' % entry['kind']}
        if not self.same_process(entry, self.game_identity()):
            return {'id': entry['id'], 'undone': False,
                    'reason': 'made in another game process (%s)' % (entry.get('pid') or 'unknown')}
        op = UNDO_OPS.get(undo.get('op'))
        if op is None:   # never mark a change undone that nothing reverted (N1)
            return {'id': entry['id'], 'undone': False, 'kind': entry.get('kind'),
                    'error': {'code': 'UNKNOWN_UNDO_OP', 'message': 'No undo op %r in this bridge' % undo.get('op')}}
        later = self._later_open(entry)
        if later:
            return {'id': entry['id'], 'undone': False, 'kind': entry.get('kind'),
                    'reason': 'undo the later change first', 'later': later}
        repeatable_if = getattr(op, 'repeatable_if', None)
        if entry.get('state') == 'pending' and not (op.repeatable or repeatable_if and repeatable_if(undo)):
            # The tool failed between its intent and its confirm: the change may or may not be in the game, and
            # this op is only right when it is (S3.2).
            return {'id': entry['id'], 'undone': False, 'kind': entry.get('kind'),
                    'reason': 'outcome unknown: the change may not have happened'}
        out = {'id': entry['id'], 'undone': True, 'kind': entry.get('kind')}
        out.update(op(self, entry, undo) or {})
        return out

    def undo(self, count=1, journal_id=None):
        self.ready()
        if journal_id:
            entries = [e for e in self.journal.entries() if not e['undone'] and e['id'] == journal_id]
            if not entries:
                raise LiveError('NOT_FOUND', 'No undoable journal entry %s' % journal_id)
        else:
            entries = self.journal.entries(process=self.game_identity(), undoable=True)[-int(count):]
        results = self._apply_undos(reversed(entries))
        return {'results': results, 'changed': any(r['undone'] for r in results)}

    def _apply_undos(self, entries):
        """Apply undos newest first, marking each one undone as soon as it lands (a later failure must not make
        a retry apply the earlier ones twice). A failing entry is reported and the rest still run."""
        results = []
        for e in entries:
            try:
                # Under the journal's lock: another server may have undone it since the list was read (S2.3).
                with self.journal.locked(timeout=UNDO_LOCK_TIMEOUT):
                    now = self.journal.get(e['id']) or e
                    if now.get('undone'):
                        r = {'id': e['id'], 'undone': False, 'kind': e.get('kind'), 'reason': 'already undone'}
                    else:
                        r = self._apply_undo(now)
                        if r['undone']:
                            self.journal.mark_undone([r['id']])
            except Exception as exc:
                r = {'id': e['id'], 'undone': False, 'kind': e.get('kind'),
                     'error': {'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}}
            results.append(r)
        return results

    def revert_all(self, since=None):
        """Undo every undoable change of this game process (since: only entries after that journal seq number)."""
        self.ready()
        ident = self.game_identity()
        entries = self.journal.entries(since_seq=since, process=ident, undoable=True)
        results = self._apply_undos(reversed(entries))
        # Only this game process and scope, newest first, at most 50 ids (G4: the list once held every entry of
        # every earlier game and made the reply too big to read).
        left = [e['id'] for e in self.journal.entries(since_seq=since, process=ident)
                if not e['undone'] and not e.get('undo')]
        skipped = {'count': len(left), 'ids': left[::-1][:50]}
        # one row per entry, in the order the undos ran: a long session would make the reply too big (S5.3); every
        # failed id is listed, past the cap too (S5 review M9)
        failed = [r for r in results if not r['undone']]
        shown = capped(results)
        return {'reverted': sum(r['undone'] for r in results), 'results': shown['items'], 'total': shown['total'],
                'truncated': shown['truncated'], 'failed': [r['id'] for r in failed], 'notUndoable': skipped,
                'changed': any(r['undone'] for r in results)}


# -- Undo ops (PLAN-3 S3.1) -------------------------------------------------------------------------------------
# One registry: an entry's undo {'op': name, ...} runs UNDO_OPS[name](live, entry, undo). An op returns None when
# the change is reverted, or a dict merged into the result ({'undone': False, 'reason': ...} when it could not).
# `repeatable` (S3.2): running the op when the change never happened is harmless (it restores, not toggles).
# `repeatable_if(undo)`: the same for one entry, when only some entries of an op restore (a hot swap with spans).
UNDO_OPS = {}


def undo_op(name, repeatable=False, repeatable_if=None):
    def register(fn):
        fn.repeatable = repeatable
        fn.repeatable_if = repeatable_if
        UNDO_OPS[name] = fn
        return fn
    return register


def _console_undo(live, line, ref=0):
    """Run one undo line; a line the game rejected or could not compile is not an undo (N2)."""
    r = live.console(line, ref)
    if not r['accepted']:
        return {'undone': False, 'reason': 'the game rejected the undo line %r' % line, 'output': r['output']}
    return None


@undo_op('write', repeatable=True)
def _undo_write(live, entry, undo):
    live.link.write(int(undo['address'], 16), bytes.fromhex(undo['hex']))


@undo_op('console')
def _undo_console(live, entry, undo):
    return _console_undo(live, undo['line'])


@undo_op('console_ref', repeatable=True)    # its lines set absolute values (or Disable/MarkForDelete)
def _undo_console_ref(live, entry, undo):
    try:
        ref = live.resolve(undo['ref'])
    except LiveError as e:
        if e.code != 'REF_NOT_FOUND':
            raise
        # A load or a cell reset removed the ref: there is nothing left to undo.
        return {'note': 'the ref %s no longer exists' % undo['ref']}
    for line in undo['lines']:
        bad = _console_undo(live, line, ref['ptr'])
        if bad:
            return bad
    return None


@undo_op('unhook', repeatable=True)
def _undo_unhook(live, entry, undo):
    for h in live.link.one('hooks').get('hooks') or []:
        if h.get('label') == undo['label'] and h.get('stolen'):
            live.link.one('unhook', word(h['id']))


@undo_op('probe_disarm', repeatable=True)
def _undo_probe_disarm(live, entry, undo):
    info = live.link.one('probe', 'info')
    if info.get('ok') and info.get('armed'):
        live.link.one('probe', 'disarm')


@undo_op('writes', repeatable=True)
def _undo_writes(live, entry, undo):
    for address, data in undo['writes']:
        live.link.write(int(address, 16), bytes.fromhex(data))


@undo_op('probe_remove', repeatable=True)
def _undo_probe_remove(live, entry, undo):
    """Disarm (the companion puts every patched entry and site back) and mark the session file not armed (N5). The
    stub code stays in the companion's heap, unreachable; the next install starts a new code area."""
    from .fnv_probe import ProbeTools
    info = live.link.one('probe', 'info')
    if info.get('ok') and info.get('armed'):
        r = live.link.one('probe', 'disarm')
        if not r.get('ok'):
            return {'undone': False, 'reason': 'probe disarm failed: %s' % r.get('error')}
    probes = ProbeTools(live)
    session = probes._load('probe_session.json', None)
    here = GameIdentity.parse(live.game_identity())
    if session and session.get('armed') and here and here.same_game(GameIdentity.parse(session)):
        session['armed'] = False
        probes._save('probe_session.json', session)
    return None


@undo_op('dbg_clear', repeatable=True)
def _undo_dbg_clear(live, entry, undo):
    """Clear only this watch's debug register, and only while it still holds this watch (N6): a watch releases its
    register when its call ends, and another tool may have it now."""
    slot = undo.get('slot')
    if slot is None:
        return {'note': 'an entry from before slots were journaled: its register was released when the watch ended'}
    info = live.link.one('dbg', 'info')
    slots = info.get('slots') or [] if info.get('ok') else []
    s = slots[int(slot)] if int(slot) < len(slots) else None
    try:
        same = bool(s and s.get('on')) and int(str(s.get('address')), 16) == int(str(undo.get('address')), 16)
    except (TypeError, ValueError):
        same = False
    if not same:
        return {'note': 'debug register %s no longer holds this watch (released, or another tool has it)' % slot}
    r = live.link.one('dbg', 'clear', str(int(slot)))
    if not r.get('ok'):
        return {'undone': False, 'reason': 'dbg clear %s failed: %s' % (slot, r.get('error'))}
    return None


def _undo_ref(live, undo):
    """The ref an undo acts on, or a result saying it is gone (nothing left to undo)."""
    try:
        return live.resolve(undo['ref']), None
    except LiveError as e:
        if e.code != 'REF_NOT_FOUND':
            raise
        return None, {'note': 'the ref %s no longer exists' % undo['ref']}


def _undo_unreadable(what):
    return {'undone': False, 'reason': 'cannot read %s now, so the undo did not run' % what}


@undo_op('fly_set', repeatable=True)
def _undo_fly_set(live, entry, undo):
    from .fnv_eyes import fly_on
    want = bool(undo['on'])
    now = fly_on(live)
    if now is None:
        return _undo_unreadable('the fly camera flag')
    if now != want:
        bad = _console_undo(live, 'tfc')
        if bad:
            return bad
        if fly_on(live) != want:
            return {'undone': False, 'reason': 'the fly camera did not turn %s' % ('on' if want else 'off')}
    if undo.get('fov') and 10 <= float(undo['fov']) <= 150:     # the camera entry's field of view (S3 review I3)
        return _console_undo(live, 'fov %s' % round(float(undo['fov']), 2))
    return None


@undo_op('hud_set', repeatable=True)
def _undo_hud_set(live, entry, undo):
    from .fnv_eyes import hud_hidden
    hidden = hud_hidden(live)
    if hidden is None:
        return _undo_unreadable('the HUD hidden flag')
    if hidden != bool(undo['on']):      # shown == on: nothing to do
        return None
    return _console_undo(live, 'tm')


@undo_op('ref_enabled', repeatable=True)
def _undo_ref_enabled(live, entry, undo):
    ref, gone = _undo_ref(live, undo)
    if gone:
        return gone
    disabled = live.values(['GetDisabled'], ref['ptr'])[0]
    if disabled is None:
        return _undo_unreadable('GetDisabled')
    if bool(disabled) != bool(undo['on']):
        return None
    return _console_undo(live, 'Enable' if undo['on'] else 'Disable', ref['ptr'])


ACTOR_FLAGS = {'unconscious': ('GetUnconscious', 'SetUnconscious %d'),
               'restrained': ('GetRestrained', 'SetRestrained %d')}


@undo_op('actor_flag', repeatable=True)
def _undo_actor_flag(live, entry, undo):
    ref, gone = _undo_ref(live, undo)
    if gone:
        return gone
    getter, setter = ACTOR_FLAGS[undo['flag']]
    now = live.values([getter], ref['ptr'])[0]
    if now is None:
        return _undo_unreadable(getter)
    if bool(now) == bool(undo['on']):
        return None
    return _console_undo(live, setter % int(bool(undo['on'])), ref['ptr'])


@undo_op('item_count', repeatable=True)
def _undo_item_count(live, entry, undo):
    ref, gone = _undo_ref(live, undo)
    if gone:
        return gone
    now = live.values(['GetItemCount %s' % undo['item']], ref['ptr'])[0]
    if now is None:
        return _undo_unreadable('GetItemCount')
    diff = int(undo['count']) - int(now)
    if not diff:
        return None
    return _console_undo(live, '%s %s %d' % ('AddItem' if diff > 0 else 'RemoveItem', undo['item'], abs(diff)),
                         ref['ptr'])


@undo_op('time_restore', repeatable=True)
def _undo_time_restore(live, entry, undo):
    """Put the clock back as it was before a pause, gate hold, fixed step or speed change (S3.6). Each restore checks
    the state first (resume only a pause this bridge made; release an open gate does nothing)."""
    from .fnv_inject import shared_time
    control, _ = shared_time(live)
    what = undo.get('what')
    if what == 'pause':
        control.resume()
    elif what == 'hold':
        control.release()
    elif what == 'fixed':
        control.fixed(int(undo.get('value') or 0), journal=False)
    elif what == 'speed':
        control.speed(float(undo['value']), journal=False)
    else:
        return {'undone': False, 'error': {'code': 'UNKNOWN_UNDO_OP', 'message': 'No time restore %r' % what}}
    return None


# with spans the undo writes both scripts' saved fields back: a restore, so a pending entry (round 4 M3: the swap
# is journaled before it runs) is safe to undo whether or not any write landed
@undo_op('swap_script', repeatable_if=lambda undo: bool(undo.get('spans')))
def _undo_swap_script(live, entry, undo):
    """A swap exchanges two code buffers: it is only right in reverse order, so a swap with a later open swap of
    the same script is refused."""
    from .fnv_data import swap_script_code
    later = [e for e in live.journal.entries(process=live.game_identity())
             if e.get('kind') == 'script_hot_swap' and not e['undone'] and e['id'] != entry['id']
             and (e.get('undo') or {}).get('target') == undo['target'] and (e.get('seq') or 0) > (entry.get('seq') or 0)]
    if later:
        return {'undone': False, 'reason': 'undo the later swap first', 'later': [e['id'] for e in later]}
    spans = undo.get('spans')
    if spans:   # write both scripts' fields from before the swap: repeatable, and repairs a half-done undo (U3, I3)
        from .fnv_data import SCRIPT_DATA, SCRIPT_HEADER, SCRIPT_REFS, SCRIPT_VARS
        offsets = (SCRIPT_HEADER, SCRIPT_DATA, SCRIPT_REFS, SCRIPT_VARS)
        ops = [('wr', '%x' % (int(undo[name], 16) + off), data)
               for name in ('target', 'holder') for off, data in zip(offsets, spans[name])]
        results = live.link.batch(ops)      # one request: within one frame
        if not all(r.get('ok') for r in results):
            return {'undone': False, 'reason': 'writing the script fields failed; fnv_undo again repeats it safely'}
        return None
    if undo.get('data'):     # the target already holds its code from before the swap: an earlier undo landed (U3)
        from .fnv_data import SCRIPT_DATA
        now = live.reads([(int(undo['target'], 16) + SCRIPT_DATA, 4)])[0]
        if now is not None and struct.unpack('<I', now)[0] == int(undo['data'], 16) and int(undo['data'], 16):
            return {'note': 'the script already runs its code from before the swap (an earlier undo landed)'}
    swap_script_code(live, int(undo['target'], 16), int(undo['holder'], 16))
