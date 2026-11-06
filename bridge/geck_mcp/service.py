"""Synchronous GECK workflows. One bottle lock spans every multi-step operation.

Workers finish bounded calls even if the MCP request is cancelled: a dispatched
Windows message cannot be safely undone by killing the waiting client.
"""
from contextlib import closing
import hashlib
import json
import re
import sqlite3
import time
import uuid

from bridge_lock import bottle_lock, reservation
from esp_check import file_snapshot, read_model, subrecords, cstring
import struct
from . import __version__
from .identity import component_identity
from .config import model_asset
from .transport import BridgeError, Transport
from .worker import AutoTransport, PersistentTransport
from .verify import match_cell_references
from .authoring import Authoring, AuthoringError
from .esp.records import SpecError
from .fnv_tools import FNV_TOOLS


CAPABILITIES = {
    'toolCount': 44,
    'fnv': {'toolCount': len(FNV_TOOLS), 'tools': FNV_TOOLS,
            'scope': 'read-only offline engine knowledge (decomp + fnvsym proofs) and crash logs; game build 1.4.0.525'},
    'version': __version__, 'game': 'Fallout New Vegas', 'recordTypes': ['STAT'],
    'transport': 'stdio', 'editorAdapter': '32-bit Win32 controls; native Windows or configured Wine/CrossOver',
    'helperTransport': 'persistent worker (Probe.Mcp.exe serve) with automatic one-shot fallback',
    'recordIdentity': 'configured plugin + persisted STAT FormID + exact EditorID',
    'editorSession': 'optional editor_session argument (alias session_id); omitted = the running GECK session, '
                     'still guarded by expected titles and plugin hashes',
    'recovery': 'uncertain operations block editor work until reviewed or stale after restart. '
                'Compile reservations persist across restart: reload checkpoint bytes and resume, or explicitly '
                'acknowledge with clean editor/current hash to release. CLI: recover.py',
    'tools': {
        'geck_console_setup': {'effect':'explicit preflight/build/install; owned paths/backups; game exit required'},
        'geck_console_query': {'effect':'typed numeric named-plugin/inventory/equipped readback; same durable status workflow'},
        'geck_console_readiness': {'effect': 'observe opt-in console setup/availability and fresh target identity'},
        'geck_console_execute': {'effect': 'in-game console command; opt-in companion; explicit intent; unique request key',
                                 'transport': 'xNVSE public Console API, not keyboard input',
                                 'results': 'sent -> engine acknowledged -> optional later effect verified'},
        'geck_console_status': {'effect': 'reconcile original command ID; never replay; archive owned receipts'},
        'geck_console_release': {'effect': 'release reviewed unknown outcome after FNV normal exit; retain evidence'},
        'geck_capabilities': {'effect': 'observe static operation/probe support and limits'},
        'geck_compile_start': {'effect': 'reserve editor; create durable compile checkpoint', 'result': 'pending'},
        'geck_compile_status': {'effect': 'read checkpoint and journal'},
        'geck_compile_resume': {'effect': 'advance one bounded compile/save/cache/rebuild/install phase'},
        'geck_editor_close': {'effect': 'normal close only; clean identified session; never force-kill'},
        'geck_runtime_readiness': {'effect': 'read per-adapter constraints and live readiness'},
        'geck_runtime_setup_preflight': {'effect': 'preview exact setup paths/backups; no install/download'},
        'geck_runtime_save_context': {'effect': 'invalidate automatic baseline/mapping after explicit context change'},
        'geck_status': {'effect': 'observe'},
        'geck_runtime_prepare': {'effect': 'preview or create unique vanilla batches; user executes', 'adapter': 'vanilla Windows: live item-count/delta/error capture; quest/reference probes offline tested'},
        'geck_runtime_collect': {'effect': 'bounded log read; write local checkpoint/report; no editor lock'},
        'geck_runtime_report': {'effect': 'replay local report; optional sanitized export'},
        'geck_runtime_close': {'effect': 'stop collection; archive unchanged owned helpers after game exit; does not stop SCOF'},
        'geck_runtime_snapshot': {'effect': 'read verified fresh automatic telemetry', 'adapter': 'optional native Windows xNVSE 6.4.9+ companion; quest variables only'},
        'geck_runtime_compare': {'effect': 'read fresh automatic telemetry and compare within one process/load/build/profile'},
        'geck_inspect_windows': {'effect': 'observe', 'scope': 'bounded GECK control inventory'},
        'geck_show_windows': {'effect': 'restore and verify root, panes and owned modals (no clicks)'},
        'geck_project_attach': {'effect': 'observe'},
        'geck_plugin_load': {'effect': 'load configured plugin and FalloutNV.esm; requires clean editor'},
        'geck_plugin_load_status': {'effect': 'observe and reconcile pending load'},
        'geck_records_find': {'effect': 'selection/filter', 'scope': 'configured STAT records'},
        'geck_record_read': {'effect': 'temporary record dialog or disk read'},
        'geck_record_set_model': {'effect': 'editor mutation', 'idempotent': True},
        'geck_preview_open': {'effect': 'preview/selection'},
        'geck_preview_close': {'effect': 'close identified preview'},
        'geck_plugin_validate': {'effect': 'disk read'},
        'geck_plugin_save': {'effect': 'save entire active plugin'},
        'geck_operation_get': {'effect': 'journal read'},
        'geck_recovery_acknowledge': {'effect': 'clear recovery barrier after state review'},
        'geck_spec_validate': {'effect': 'offline build + validation dry run; writes nothing'},
        'geck_plugin_build': {'effect': 'write build/<plugin> and receipt, save FormID map; Data untouched'},
        'geck_plugin_install': {'effect': 'copy build into Data with guards and backup; GECK must reload'},
        'geck_plugin_inspect': {'effect': 'disk read of installed or built plugin'},
        'geck_master_lookup': {'effect': 'search master index and project records'},
        'geck_cell_verify': {'effect': 'compare Cell View with build receipt; optional camera framing'},
        'geck_actor_photo': {'effect': 'build/install/load the throwaway GeckPhotoStudio.esp, frame an NPC_/CREA, '
                             'capture PNG + transparent cut-out; resumable (stage loading -> done)',
                             'never': 'saves or edits the user plugins; discards unsaved edits'},
        'geck_render_capture': {'effect': 'capture of the current Render Window (macOS: CGWindowID; '
                                'Windows: PrintWindow); no clicks; refuses a blank or black image'},
        'geck_image_cutout': {'effect': 'write a transparent PNG of the subject in an existing capture'},
    },
    'authoring': {'recordTypes': ['TES4', 'STAT', 'MSTT', 'LIGH', 'CELL (interior)', 'REFR', 'ACHR', 'ACRE'],
                  'references': '"@EditorID" (project or indexed master) or 8-digit FormID',
                  'formIds': 'stable per project; removed keys retired, never reused'},
    'unsupported': ['editing non-generated plugins through specs', 'multi-master plugin loading',
                    'exterior cells/worldspaces',
                    'automatic rollback', 'animation validation', 'gameplay validation', 'console keyboard injection',
                    'console load/quit/batch commands; arbitrary console-text capture',
                    'first-class master override intents (roundtrip loss is blocked)'],
    'compilation': {'phases': 'selection -> full source verified -> Save requested -> busy/dialog review -> '
                             'close verified -> plugin saved -> persisted SCPT verified -> cached -> rebuilt -> installed',
                    'results': ['pending', 'blocked', 'failed', 'verified'],
                    'durability': 'only persisted verified scripts survive restart after revalidation',
                    'sourceEncoding': 'CP1252 project files; Unicode UI read; CRLF/CR normalized to LF for hash',
                    'reservation': 'same editor install across CLI/MCP/profiles, retained between calls'},
    'verificationLimits': ['32 MiB plugins', 'uncompressed STAT/EDID/MODL only',
                           'no extended XXXX in STAT', 'no visual correctness claim',
                           'active file identified by editor title; master list not verified'],
}


# Editor work these tools may do while an uncertain operation blocks the editor.
BARRIER_EXEMPT = ('geck_status', 'geck_inspect_windows', 'geck_plugin_load_status', 'geck_recovery_acknowledge',
                  'geck_show_windows')
UNRESOLVED = ('running', 'outcome_unknown', 'loading')
# Helper errors meaning "this UI check could not run" (hidden or missing window, selector miss), as opposed to a
# verified mismatch. A load whose plugin is clearly active but hits one of these reports loaded_unverified.
UNVERIFIABLE = ('EDITOR_ERROR', 'HELPER_PROTOCOL_ERROR')
# A reload whose progress was never observed is trusted only after this long (a FalloutNV.esm load takes 15+ s).
RELOAD_SETTLE_SECONDS = 45


def make_transport(project):
    if project.transport == 'oneshot':
        return Transport(project)
    if project.transport == 'persistent':
        return PersistentTransport(project)
    return AutoTransport(project)


class Service:
    def console_readiness(self, expected_plugin_hash):
        from .console import readiness
        return self._envelope('geck_console_readiness', time.monotonic(),
                              lambda: readiness(self.project, expected_plugin_hash))

    def console_execute(self, command, intent, target, expected_plugin_hash, request_key, verify=None, timeout_seconds=10, binding=None, authorize_teleport=False):
        from .console import Console
        worker = Service(self.project, transport=self.transport)
        return Console(worker).execute(command, intent, target, expected_plugin_hash, request_key, verify, timeout_seconds, binding, authorize_teleport)

    def console_query(self, kind, intent, target, expected_plugin_hash, request_key, binding=None, slot=5):
        from .console import query
        return query(Service(self.project, transport=self.transport), kind, intent, target, expected_plugin_hash,
                     request_key, binding, slot)

    def console_setup(self, mode, expected_plugin_hash, request_key, zig=None, build_receipt=None):
        from . import console
        from pathlib import Path
        worker = Service(self.project, transport=self.transport)
        def action():
            if mode == 'preflight': return {**console.preflight(self.project), '_outcome':'unchanged'}
            if mode == 'build':
                if not zig: raise ValueError('Provide local Zig executable path')
                return {**console.build(self.project, zig, expected_plugin_hash), '_outcome':'built'}
            if mode == 'install':
                if not build_receipt: raise ValueError('Provide owned build.json path returned by setup build')
                path = Path(build_receipt).resolve(strict=True)
                if not path.is_relative_to((self.project.state_dir / 'console/builds').resolve()):
                    raise ValueError('Build receipt must be inside this project console build state')
                console.runtime.game_closed(self.project)
                worker._mutation_dispatched = True
                return {**console.install(self.project, json.loads(path.read_text()), expected_plugin_hash), '_outcome':'installed'}
            raise ValueError('Unsupported console setup mode')
        # install owns its runtime lock internally; outer journal lock has a separate setup namespace.
        return worker.run('geck_console_setup', dict(mode=mode, hash=expected_plugin_hash, zig=zig, receipt=build_receipt),
                          action, request_key, modifies=mode == 'install', editor=False, lock_root=self.project.state_dir / 'console/setup-lock')

    def console_status(self, operation_id):
        from .console import Console
        return Console(Service(self.project, transport=self.transport)).status(operation_id)

    def console_release(self, operation_id, note):
        from .console import Console
        return Console(Service(self.project, transport=self.transport)).release(operation_id, note)

    def editor_close(self, session, expected_title, expected_hash, request_key):
        def action():
            state = self.check_session(session)
            disk = hashlib.sha256(self.project.plugin_path.read_bytes()).hexdigest()
            if state['editorTitle'] != expected_title or state['unsavedChanges']:
                raise BridgeError('UNSAVED_CHANGES', 'Normal close requires matching clean editor state', state)
            if disk != expected_hash or self.loaded_plugin(state['sessionId']) != disk:
                raise BridgeError('STALE_EDITOR', 'Review/reload the installed build before normal close')
            return self.call('editor.close', session=state['sessionId'])
        return self.run('geck_editor_close', dict(session=session, expected_title=expected_title,
                         expected_hash=expected_hash), action, request_key, modifies=True)

    def runtime_readiness(self, profile, expected_plugin_hash, adapter='xnvse'):
        from .runtime_companion import readiness
        return self._envelope('geck_runtime_readiness', time.monotonic(),
                              lambda: readiness(self.project, profile, expected_plugin_hash, adapter))

    def runtime_preflight(self, profile, zig=None):
        from .runtime_companion import preflight
        return self._envelope('geck_runtime_setup_preflight', time.monotonic(),
                              lambda: preflight(self.project, profile, zig))

    def runtime_save_context(self, label):
        from .runtime_companion import set_save_context
        return self.run('geck_runtime_save_context', {'label': label}, lambda: set_save_context(self.project, label),
                        editor=False, lock_root=self.project.game_root / '.geck-runtime-lock')

    def compile_start(self, request_key, deadline_seconds=1200):
        from .compilation import Compilation
        return Compilation(self).start(request_key, deadline_seconds)

    def compile_resume(self, operation_id, extend_seconds=0, reviewed_roundtrip=None, review_note=None):
        from .compilation import Compilation
        return Compilation(self).resume(operation_id, extend_seconds, reviewed_roundtrip, review_note)

    def compile_status(self, operation_id):
        from .compilation import Compilation
        return self._envelope('geck_compile_status', time.monotonic(),
                              lambda: {**Compilation(self).read(operation_id),
                                       'journal': self.operation_evidence(operation_id)})

    def runtime_snapshot(self, profile, expected_plugin_hash, baseline=None):
        """Read-only automatic runtime observation; never attaches to GECK or holds its lock."""
        from .runtime_companion import snapshot
        return self._envelope('geck_runtime_compare' if baseline is not None else 'geck_runtime_snapshot',
                              time.monotonic(),lambda: snapshot(self.project,profile,expected_plugin_hash,baseline))

    def runtime_call(self, mode, args, request_key=None):
        """Runtime operations have a separate per-install lock/journal namespace.

        No waits for user gameplay occur inside these bounded calls. Interrupted
        runtime preparation cannot create a GECK recovery barrier.
        """
        from .runtime import Runtime
        if mode not in ('prepare', 'collect', 'report', 'close'):
            raise ValueError('Unknown runtime operation')
        # run() keeps per-operation dispatch/journal context on its Service instance.
        # A separate instance lets an editor call run concurrently under its own lock
        # without runtime collection overwriting that editor operation's context.
        runtime_service = Service(self.project, transport=self.transport)
        def on_change():
            runtime_service._mutation_dispatched = True
        runtime = Runtime(self.project, on_change)
        def action():
            data = getattr(runtime, mode)(**args)
            if mode == 'prepare' and not args.get('preview'):
                data['_outcome'] = 'runtime_prepared'
            elif mode == 'collect' and data.get('checkpoint'):
                data['_outcome'] = 'runtime_collected'
            elif mode == 'close':
                data['_outcome'] = 'runtime_closed'
            return data
        return runtime_service.run('geck_runtime_' + mode, args, action, request_key=request_key,
                        modifies=mode in ('prepare', 'close'), editor=False,
                        lock_root=self.project.game_root / '.geck-runtime-lock')

    def __init__(self, project, transport=None):
        self.project = project
        self.transport = transport or make_transport(project)
        project.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = project.state_dir / 'operations.sqlite3'
        with closing(self.db()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, project TEXT, bottle TEXT, name TEXT,
                request_key TEXT, fingerprint TEXT, state TEXT, result TEXT,
                started REAL, recovery_ack TEXT,
                UNIQUE(project, request_key))''')
            db.execute('''CREATE TABLE IF NOT EXISTS steps (
                operation_id TEXT, sequence INTEGER, operation TEXT, state TEXT,
                evidence TEXT, PRIMARY KEY(operation_id, sequence))''')

    def db(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def operation(self, operation_id):
        with closing(self.db()) as db:
            row = db.execute('SELECT * FROM operations WHERE id=? AND project=?',
                             (operation_id, self.project.project_id)).fetchone()
        if row is None:
            raise ValueError('operation not found in this project')
        return json.loads(row['result']) if row['result'] else {
            'ok': False, 'operationId': row['id'], 'operation': row['name'],
            'projectId': row['project'], 'outcome': 'outcome_unknown',
            'data': None, 'elapsedMs': 0,
            'error': {'code': 'OUTCOME_UNKNOWN', 'retrySafe': False,
                      'message': 'Operation is running or interrupted; inspect state before any retry'},
        }

    def operation_evidence(self, operation_id):
        result = self.operation(operation_id)
        with closing(self.db()) as db:
            rows = db.execute('SELECT * FROM steps WHERE operation_id=? ORDER BY sequence',
                              (operation_id,)).fetchall()
            journal = db.execute('SELECT state, recovery_ack FROM operations WHERE id=?', (operation_id,)).fetchone()
        ack = json.loads(journal['recovery_ack']) if journal and journal['recovery_ack'] else None
        return dict(result, journalState=journal['state'] if journal else None, recoveryAck=ack,
                    steps=[dict(row, evidence=json.loads(row['evidence'])
                                if row['evidence'] else None) for row in rows])

    # -- recovery barrier ----------------------------------------------------------------
    def unresolved(self):
        """Uncertain operations that block editor work on this GECK install (oldest first)."""
        with closing(self.db()) as db:
            return db.execute('SELECT * FROM operations WHERE bottle=? AND state IN (%s) AND recovery_ack IS NULL '
                              'ORDER BY started' % ','.join('?' * len(UNRESOLVED)),
                              (str(self.project.lock_root), *UNRESOLVED)).fetchall()

    def operation_session(self, row):
        """The GECK session an operation ran against: its result, else its journaled steps. None if unknown."""
        try:
            data = (json.loads(row['result']) if row['result'] else {}).get('data')
        except (ValueError, AttributeError):
            data = None
        if isinstance(data, dict) and data.get('sessionId'):
            return data['sessionId']
        with closing(self.db()) as db:
            steps = db.execute('SELECT evidence FROM steps WHERE operation_id=? ORDER BY sequence',
                               (row['id'],)).fetchall()
        for step in steps:
            try:
                evidence = json.loads(step['evidence']) if step['evidence'] else None
            except ValueError:
                continue
            if isinstance(evidence, dict):
                for key in ('session', 'sessionId'):
                    if isinstance(evidence.get(key), str) and evidence[key]:
                        return evidence[key]
        return None

    def current_session(self):
        """Session ID of the running GECK, or None when it cannot be read (not running, busy)."""
        try:
            return self.transport.call('status')['sessionId']
        except Exception:
            return None

    def mark_stale(self, row, own, current):
        ack = {'auto': 'stale', 'operationSession': own, 'currentSession': current, 'time': time.time(),
               'note': 'GECK was restarted (session %s, now %s); the editor state this operation left behind '
                       'is gone. Released automatically; disk changes, if any, were not reviewed.' % (own, current)}
        with closing(self.db()) as db, db:
            db.execute("UPDATE operations SET state='stale', recovery_ack=? WHERE id=? AND recovery_ack IS NULL",
                       (json.dumps(ack), row['id']))
        owner = {'operationId': row['id'], 'journal': str(self.db_path.resolve())}
        if row['name'] == 'geck_plugin_load' and reservation(self.project.lock_root) == owner:
            reservation(self.project.lock_root, owner, release=True)
        return {'operationId': row['id'], 'operation': row['name'], 'previousState': row['state'],
                'operationSession': own, 'currentSession': current}

    def release_stale(self, rows=None):
        """Release barriers whose GECK session is gone. Returns (released, still_blocking)."""
        rows = self.unresolved() if rows is None else rows
        if not rows:
            return [], []
        current = self.current_session()
        released, blocking = [], []
        for row in rows:
            own = self.operation_session(row)
            if current and own and own != current:
                released.append(self.mark_stale(row, own, current))
            else:
                blocking.append(row)
        return released, blocking

    def run(self, name, args, action, request_key=None, modifies=False, editor=True, lock_root=None,
            resume_id=None):
        started = time.monotonic()
        operation_id = resume_id or str(uuid.uuid4())
        fingerprint = hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()
        began = False
        try:
            if request_key is not None and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', request_key):
                raise ValueError('request_key must be 1..100 simple identifier characters')
            with bottle_lock(lock_root or self.project.lock_root):
                # Idempotent start replay precedes reservation checks: it cannot dispatch another phase.
                if request_key:
                    with closing(self.db()) as db:
                        replay = db.execute('SELECT * FROM operations WHERE project=? AND request_key=?',
                                            (self.project.project_id, request_key)).fetchone()
                    if replay:
                        if replay['fingerprint'] != fingerprint:
                            raise BridgeError('IDEMPOTENCY_CONFLICT', 'Key already used for different arguments')
                        return dict(self.operation(replay['id']), replayed=True)
                if editor and name not in BARRIER_EXEMPT:
                    held = reservation(self.project.lock_root)
                    recovery_load = False
                    if held and held['journal'] == str(self.db_path.resolve()) and name == 'geck_plugin_load':
                        with closing(self.db()) as owner_db:
                            held_row = owner_db.execute('SELECT name,project FROM operations WHERE id=?', (held['operationId'],)).fetchone()
                        if held_row and held_row['name'] == 'geck_compile' and held_row['project'] == self.project.project_id:
                            from .compilation import Compilation
                            checkpoint = Compilation(self).read(held['operationId'])
                            current = self.current_session()
                            recovery_load = bool(current and current != checkpoint['sessionId']
                                                 and args.get('expected_hash') == checkpoint['expectedDiskSha256'])
                    if held and not recovery_load and (held['operationId'] != operation_id or held['journal'] != str(self.db_path.resolve())):
                        raise BridgeError('EDITOR_RESERVED', 'Resume or review the reserved editor operation before editor work', held)
                with closing(self.db()) as db, db:
                    if request_key:
                        row = db.execute('SELECT * FROM operations WHERE project=? AND request_key=?',
                                         (self.project.project_id, request_key)).fetchone()
                        if row:
                            if row['fingerprint'] != fingerprint:
                                raise BridgeError('IDEMPOTENCY_CONFLICT', 'Key already used for different arguments')
                            return dict(self.operation(row['id']), replayed=True)
                    released = []
                    if editor and name not in BARRIER_EXEMPT:
                        blocked = [r for r in self.unresolved() if r['id'] != resume_id
                                   and not (recovery_load and held and r['id'] == held['operationId'])]
                        if blocked:
                            released, blocking = self.release_stale(blocked)
                            if blocking:
                                raise BridgeError('RECOVERY_REQUIRED', 'Uncertain prior operation blocks editor work',
                                                  {'operationId': blocking[0]['id'],
                                                   'unresolved': [dict(operationId=r['id'], operation=r['name'],
                                                                       state=r['state']) for r in blocking],
                                                   'released': released,
                                                   'next': 'Inspect geck_operation_get, then geck_recovery_acknowledge '
                                                           '(or recover.py --list / --ack). Restarting GECK '
                                                           'releases it automatically.'})
                    if resume_id:
                        prior = db.execute('SELECT * FROM operations WHERE id=? AND project=? AND name=?',
                                           (resume_id, self.project.project_id, name)).fetchone()
                        if prior is None:
                            raise ValueError('Not a compile operation in this project')
                        db.execute("UPDATE operations SET state='running' WHERE id=?", (resume_id,))
                    else:
                        db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)',
                                   (operation_id, self.project.project_id, str(lock_root or self.project.lock_root), name,
                                    request_key, fingerprint, 'running', None, time.time(), None))
                began = True
                self._mutation_dispatched = False
                self._active_operation = operation_id
                with closing(self.db()) as db:
                    self._step_sequence = db.execute('SELECT COALESCE(MAX(sequence),0) FROM steps WHERE operation_id=?',
                                                     (operation_id,)).fetchone()[0]
                # Only errors after entering the action may represent partial edits.
                try:
                    data = action()
                    outcome = data.pop('_outcome', 'unchanged')
                    if released:
                        data['releasedStaleOperations'] = released
                    result = {'ok': True, 'operationId': operation_id, 'operation': name,
                              'projectId': self.project.project_id, 'outcome': outcome,
                              'data': data, 'error': None}
                except Exception as e:
                    result = self.failure(e, operation_id, name, modifies and self._mutation_dispatched)
                finally:
                    self._active_operation = None
                result['elapsedMs'] = round((time.monotonic() - started) * 1000)
                with closing(self.db()) as db, db:
                    db.execute('UPDATE operations SET state=?, result=? WHERE id=?',
                               (result['outcome'], json.dumps(result), operation_id))
                return result
        except Exception as e:
            # If journaling failed after dispatch, never describe the call as safe to retry.
            result = self.failure(e, operation_id, name, modifies and began)
            result['elapsedMs'] = round((time.monotonic() - started) * 1000)
            return result

    def failure(self, error, operation_id, name, modifies):
        code = getattr(error, 'code', 'INVALID_ARGUMENT' if isinstance(error, ValueError) else 'BRIDGE_ERROR')
        unknown = modifies or (getattr(error, 'uncertain', False) and name not in
                               ('geck_status', 'geck_inspect_windows', 'geck_plugin_validate', 'geck_project_attach'))
        return {'ok': False, 'operationId': operation_id, 'operation': name,
                'projectId': self.project.project_id,
                'outcome': 'outcome_unknown' if unknown else 'failed_before_change',
                'data': getattr(error, 'evidence', None),
                'error': {'code': code, 'message': str(error),
                          'retrySafe': False,
                          'recovery': 'Read status and operation evidence before retrying.'}}

    def call(self, operation, *args, session=None, expected_model=None):
        operation_id = getattr(self, '_active_operation', None)
        sequence = None
        if operation_id:
            self._step_sequence += 1
            sequence = self._step_sequence
            with closing(self.db()) as db, db:
                db.execute('INSERT INTO steps VALUES (?,?,?,?,?)',
                           (operation_id, sequence, operation, 'dispatching',
                            json.dumps({'arguments': args, 'session': session})))
        if operation in ('object.setModel', 'plugin.save', 'data.load', 'script.prepare', 'script.save', 'script.resolveSave',
                         'script.editor.close', 'editor.close'):
            self._mutation_dispatched = True
        try:
            result = self.transport.call(operation, *args, session=session, expected_model=expected_model)
        except Exception as error:
            if operation_id:
                with closing(self.db()) as db, db:
                    db.execute('UPDATE steps SET state=?, evidence=? WHERE operation_id=? AND sequence=?',
                               ('failed', json.dumps({'error': str(error),
                                 'evidence': getattr(error, 'evidence', None)}), operation_id, sequence))
            raise
        if operation_id:
            with closing(self.db()) as db, db:
                db.execute('UPDATE steps SET state=?, evidence=? WHERE operation_id=? AND sequence=?',
                           ('verified', json.dumps(result), operation_id, sequence))
        return result

    def call_batch(self, steps, session=None):
        """Run read/selection steps in one worker round trip when supported.

        Journaled as one step per helper operation. Never used for mutations.
        """
        if any(op in ('object.setModel', 'plugin.save', 'data.load') for op, *_ in steps):
            raise ValueError('mutating operations are never batched')
        batch = getattr(self.transport, 'batch', None)
        if batch is None:
            return [self.call(op, *args, session=session) for op, *args in steps]
        operation_id = getattr(self, '_active_operation', None)
        label = '+'.join(op for op, *_ in steps)
        sequence = None
        if operation_id:
            self._step_sequence += 1
            sequence = self._step_sequence
            with closing(self.db()) as db, db:
                db.execute('INSERT INTO steps VALUES (?,?,?,?,?)',
                           (operation_id, sequence, 'batch:' + label, 'dispatching',
                            json.dumps({'steps': steps, 'session': session})))
        try:
            results = batch(steps, session=session)
        except Exception as error:
            if operation_id:
                with closing(self.db()) as db, db:
                    db.execute('UPDATE steps SET state=?, evidence=? WHERE operation_id=? AND sequence=?',
                               ('failed', json.dumps({'error': str(error),
                                 'evidence': getattr(error, 'evidence', None)}), operation_id, sequence))
            raise
        if operation_id:
            with closing(self.db()) as db, db:
                db.execute('UPDATE steps SET state=?, evidence=? WHERE operation_id=? AND sequence=?',
                           ('verified', json.dumps(results), operation_id, sequence))
        return results

    def transport_info(self):
        describe = getattr(self.transport, 'describe', None)
        return describe() if describe else {'mode': 'custom'}

    def close(self):
        photo = getattr(self, '_photo', None)
        if photo is not None:
            photo.close()
        close = getattr(self.transport, 'close', None)
        if close:
            close()

    def status(self):
        def action():
            state = self.call('status')
            out = dict(state, bridgeTransport=self.transport_info(), components=component_identity(self.project))
            out['artifactIdentities'] = {'loadedSha256': self.loaded_plugin(state['sessionId']),
                                         'installedSha256': hashlib.sha256(self.project.plugin_path.read_bytes()).hexdigest()
                                         if self.project.plugin_path.exists() else None,
                                         'builtSha256': self.authoring().receipt()['sha256']
                                         if self.project.spec and self.authoring().receipt_path.exists() else None}
            panes = state.get('editorWindows')
            if isinstance(panes, dict):   # helper 0.1.2+: Object Window, Cell View, Render Window
                out['windowsVisible'] = {name: bool(pane.get('visible')) for name, pane in panes.items()}
                out['renderWindowReachable'] = bool((panes.get('renderWindow') or {}).get('reachable'))
                if not all(out['windowsVisible'].values()):
                    out['hint'] = 'Some GECK windows are hidden: geck_show_windows shows them again.'
            return out
        return self.run('geck_status', {}, action)

    def inspect_windows(self, limit):
        return self.run('geck_inspect_windows', {'limit': limit}, lambda: self.call('inspect', limit))

    def check_session(self, session=None, require_plugin=True, no_dialogs=True):
        """Identify the running GECK. With session None (the MCP argument was omitted) the current session is
        adopted; every later helper call of the operation is then guarded by it (state['sessionId'])."""
        state = self.call('status', session=session)
        if session and state['sessionId'] != session:
            raise BridgeError('STALE_SESSION', 'Editor restarted; attach again')
        # Wine may report C: rather than Z:. Compare through the configured bottle.
        actual = state['executable']
        if self.project.launcher == 'native':
            from pathlib import PureWindowsPath
            same = (PureWindowsPath(actual).as_posix().lower()
                    == PureWindowsPath(self.project.game_root / 'GECK.exe').as_posix().lower())
            if not same:
                raise BridgeError('WRONG_EDITOR', 'Running editor is outside configured game root', state)
            actual_path = None
        elif actual[:3].lower() == 'c:\\':
            actual_path = self.project.bottle / 'drive_c' / actual[3:].replace('\\', '/')
        elif actual[:3].lower() == 'z:\\':
            from pathlib import Path
            actual_path = Path('/' + actual[3:].replace('\\', '/'))
        else:
            raise BridgeError('WRONG_EDITOR', 'Unsupported editor executable path')
        if actual_path is not None and str(actual_path.resolve()).lower() != str((self.project.game_root / 'GECK.exe').resolve()).lower():
            raise BridgeError('WRONG_EDITOR', 'Running editor is outside configured game root', state)
        if require_plugin and state['editorTitle'].rstrip('*') != f'Garden of Eden Creation Kit - [{self.project.plugin}]':
            raise BridgeError('WRONG_PLUGIN', 'Load the configured active plugin before editing', state)
        if no_dialogs:
            blocked = [w for w in state.get('windows', []) if w['class'] == '#32770' and w.get('visible', True)
                       and not w['text'].startswith("Preview Object: '")]
            if blocked:
                raise BridgeError('MODAL_BLOCKED', 'Existing dialogs belong to the user or an interrupted operation', blocked)
        return state

    def persisted(self, editor_id):
        configured = self.project.record(editor_id)
        snapshot, data = file_snapshot(self.project.plugin_path)
        record = read_model(data, editor_id)
        if record['formId'].upper() != configured.form_id.upper():
            raise BridgeError('IDENTITY_MISMATCH', 'Persisted FormID differs from configured record', record)
        return snapshot, record

    def attach(self, session=None):
        def action():
            state = self.check_session(session)
            records = [self.persisted(r.editor_id)[1] for r in self.project.records]
            return {'session': state, 'records': records, 'verificationScope': CAPABILITIES['verificationLimits']}
        return self.run('geck_project_attach', {'session': session}, action)

    def load_plugin(self, session, expected_title, expected_hash, request_key, reload=False):
        def action():
            state = self.check_session(session, require_plugin=False)
            sid = state['sessionId']
            if state['editorTitle'] != expected_title:
                raise BridgeError('STALE_STATE', 'Editor title changed before plugin load', state)
            if state['unsavedChanges']:
                raise BridgeError('UNSAVED_CHANGES', 'Save or resolve existing edits before loading another plugin')
            snapshot, data = file_snapshot(self.project.plugin_path)
            if snapshot['sha256'] != expected_hash:
                raise BridgeError('STALE_STATE', 'Plugin changed before loading', snapshot)
            if data[:4] != b'TES4' or len(data) < 24:
                raise ValueError('Invalid plugin header')
            size = struct.unpack_from('<I', data, 4)[0]
            if size > len(data) - 24:
                raise ValueError('Truncated plugin header')
            masters = [cstring(value) for key, value in subrecords(data[24:24+size]) if key == b'MAST']
            if masters != ['FalloutNV.esm']:
                raise BridgeError('UNSUPPORTED_MASTERS', 'Loader currently supports exactly FalloutNV.esm', {'masters': masters})
            for record in self.project.records:
                self.persisted(record.editor_id)
            if expected_title == f'Garden of Eden Creation Kit - [{self.project.plugin}]' and not reload:
                return {'alreadyLoaded': True, 'sessionId': sid, 'file': snapshot}
            owner = {'operationId': self._active_operation, 'journal': str(self.db_path.resolve())}
            reservation(self.project.lock_root, owner)
            self.call('data.open.inspect', session=sid)
            try:
                selection = self.call('data.load', self.project.plugin, session=sid)
            except BridgeError as error:
                self.cancel_failed_load(error, sid)
                if not self._mutation_dispatched:
                    reservation(self.project.lock_root, owner, release=True)
                raise
            # Reloading the plugin GECK already shows: the title names it before and after the load, so
            # load_status must first see the load happen (or RELOAD_SETTLE_SECONDS pass) before trusting it.
            same_title = expected_title.rstrip('*') == f'Garden of Eden Creation Kit - [{self.project.plugin}]'
            return {'_outcome': 'loading', 'sessionId': sid, 'file': snapshot, 'reload': same_title,
                    'dispatchedAt': time.time(), 'deadlineAt': time.time() + 900, 'initialTitle': expected_title, 'loadingObserved': False,
                    'dispatch': selection, 'loaded': False,
                    'next': 'Call geck_plugin_load_status with this operationId; do not redispatch.'}
        return self.run('geck_plugin_load', dict(session=session, expected_title=expected_title,
                        expected_hash=expected_hash, reload=reload), action, request_key, modifies=True)

    def cancel_failed_load(self, error, session):
        """data.load fails before it presses OK (its last step), unless it timed out. Cancel the Data dialog then:
        nothing was loaded, so the failure is failed_before_change instead of a recovery barrier."""
        if error.code != 'EDITOR_ERROR' or error.uncertain:
            return
        try:
            self.call('data.cancel', session=session)
        except BridgeError as cancel_error:
            error.evidence = {'load': error.evidence, 'cancel': str(cancel_error)}
            return                      # dialog state unknown: keep the barrier
        self._mutation_dispatched = False
        error.args = (str(error) + '; Data dialog cancelled, nothing loaded',)

    def load_status(self, operation_id):
        def action():
            previous = self.operation(operation_id)
            with closing(self.db()) as db:
                journal = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            if journal and journal['recovery_ack']:
                raise BridgeError('RECOVERY_ACKNOWLEDGED', 'This load was reviewed and released; start a fresh load instead')
            if previous['operation'] != 'geck_plugin_load':
                raise ValueError('Not a plugin-load operation')
            if previous['outcome'] != 'loading':
                owner = {'operationId': operation_id, 'journal': str(self.db_path.resolve())}
                if previous['outcome'] in ('loaded_verified', 'loaded_unverified') and reservation(self.project.lock_root) == owner:
                    reservation(self.project.lock_root, owner, release=True)
                return {'load': previous}
            session = previous['data']['sessionId']

            def still_loading(observation, observed=True):
                if observed and not previous['data'].get('loadingObserved'):
                    previous['data']['loadingObserved'] = True
                    with closing(self.db()) as db, db:
                        db.execute('UPDATE operations SET result=? WHERE id=? AND state=?',
                                   (json.dumps(previous), operation_id, 'loading'))
                return {'load': previous, 'loading': True, 'lastObservation': observation}
            try:
                state = self.check_session(session, require_plugin=False)
            except BridgeError as error:
                if error.code == 'EDITOR_UNRESPONSIVE':
                    if time.time() > previous['data'].get('deadlineAt', previous['data'].get('dispatchedAt', 0) + 900):
                        raise BridgeError('LOAD_REVIEW_REQUIRED', 'Load deadline expired; inspect the original operation without redispatch', previous)
                    return still_loading(str(error))
                if error.code == 'WRONG_PLUGIN':
                    return still_loading(str(error))
                if error.code == 'STALE_SESSION' and journal is not None:
                    # GECK restarted mid-load: nothing of that load survives, so the barrier goes too.
                    current = self.current_session()
                    if current and current != session:
                        error.evidence = {'released': self.mark_stale(journal, session, current),
                                          'next': 'Start a fresh geck_plugin_load'}
                raise
            expected_title = 'Garden of Eden Creation Kit - [%s]' % self.project.plugin
            actual_title = state['editorTitle'].rstrip('*')
            if actual_title != expected_title:
                data = previous['data']
                initial = data.get('initialTitle', '')
                neutral = actual_title == 'Garden of Eden Creation Kit'
                expired = time.time() > data.get('deadlineAt', data.get('dispatchedAt', 0) + 900)
                if expired or (not neutral and actual_title != initial.rstrip('*')):
                    raise BridgeError('LOAD_CONTEXT_CHANGED', 'Load no longer owns the observed editor context; review, do not redispatch',
                                      {'operationId': operation_id, 'expectedSession': session, 'observedSession': state['sessionId'],
                                       'expectedTitle': expected_title, 'initialTitle': initial, 'observedTitle': actual_title})
                return still_loading('Waiting for requested plugin; prior/neutral title remains', observed=neutral)
            snapshot, _ = file_snapshot(self.project.plugin_path)
            if snapshot['sha256'] != previous['data']['file']['sha256']:
                raise BridgeError('STALE_STATE', 'Plugin bytes changed during load', snapshot)
            if state['unsavedChanges']:
                raise BridgeError('UNSAVED_CHANGES', 'Editor changed while loading; inspect before proceeding', state)
            data = previous['data']
            if (data.get('reload') and not data.get('loadingObserved')
                    and time.time() - data.get('dispatchedAt', 0) < RELOAD_SETTLE_SECONDS):
                return still_loading('reload dispatched; waiting to see GECK reload (the title already named the '
                                     'plugin before)', observed=False)
            # The title names the plugin and GECK answers: the load is done. GECK sometimes keeps its Object
            # Window, Cell View and Render Window hidden after a load (native Windows); show them again.
            windows = self.restore_windows(session)
            # A responsive identified editor and exact configured record readbacks
            # provide stronger completion evidence than the title alone.
            records, cells, unverified = [], None, None
            try:
                for record in self.project.records:
                    self.select(record.editor_id, session)
                    current = self.call('object.read', record.editor_id, session=session)
                    self.call('object.cancel', record.editor_id, session=session)
                    persisted = self.persisted(record.editor_id)[1]
                    if current['modelPath'] != persisted['modelPath']:
                        raise BridgeError('STALE_STATE', 'Loaded model differs from persisted plugin',
                                          {'editor': current, 'disk': persisted})
                    records.append(current)
                cells = self.verify_receipt_cells(session)
            except BridgeError as error:
                if error.code == 'EDITOR_UNRESPONSIVE':
                    return dict(still_loading(str(error)), windows=windows)
                if error.code not in UNVERIFIABLE:
                    raise
                # The plugin is active (title) but the check itself could not run (e.g. a hidden Cell View).
                unverified = {'code': error.code, 'message': str(error)}
            outcome = 'loaded_unverified' if unverified else 'loaded_verified'
            previous['outcome'] = outcome
            previous['data'].update(loaded=True, records=records, cells=cells, session=state, windows=windows,
                                    verification={'ok': not unverified, 'skipped': unverified}, verifiedAt=time.time())
            if unverified:
                previous['data']['next'] = ('GECK shows the plugin, but the cell check could not run. Try '
                                            'geck_show_windows, then geck_cell_verify; or look at GECK.')
            with closing(self.db()) as db, db:
                db.execute('UPDATE operations SET state=?, result=? WHERE id=?',
                           (outcome, json.dumps(previous), operation_id))
            owner = {'operationId': operation_id, 'journal': str(self.db_path.resolve())}
            if reservation(self.project.lock_root) == owner:
                reservation(self.project.lock_root, owner, release=True)
            return {'load': previous, 'loading': False}
        return self.run('geck_plugin_load_status', {'operation_id': operation_id}, action)

    def loaded_plugin(self, session):
        """Latest independently observed load/save identity for this editor session; unknown is None."""
        candidates = []
        with closing(self.db()) as db:
            table = db.execute("SELECT name FROM sqlite_master WHERE name='compile_checkpoints'").fetchone()
            if table:
                for row in db.execute('SELECT data FROM compile_checkpoints').fetchall():
                    cp = json.loads(row[0])
                    if cp['sessionId'] == session and cp.get('scripts'):
                        candidates.append((cp.get('loadedAt', cp['startedAt']), cp['loadedSha256']))
            rows = db.execute("SELECT result, started FROM operations WHERE project=? AND "
                              "state IN ('loaded_verified','loaded_unverified','saved_verified') "
                              "ORDER BY started DESC LIMIT 50", (self.project.project_id,)).fetchall()
        for row in rows:
            data = (json.loads(row['result']) or {}).get('data') or {}
            if data.get('sessionId') == session:
                digest = (data.get('file') or data.get('after') or {}).get('sha256')
                if digest:
                    candidates.append((data.get('verifiedAt', row['started']), digest))
        return max(candidates, default=(0, None), key=lambda item: item[0])[1]

    def restore_windows(self, session):
        """Show GECK's Object Window, Cell View and Render Window if they are hidden. Never raises."""
        try:
            return self.call('windows.show', session=session)
        except BridgeError as error:
            return {'error': {'code': error.code, 'message': str(error)}}

    def show_windows(self, session=None):
        def action():
            # Open dialogs are no reason to refuse: the helper itself refuses while GECK's main window is
            # disabled (a modal dialog or a load in progress).
            sid = self.check_session(session, require_plugin=False, no_dialogs=False)['sessionId']
            return self.call('windows.show', session=sid)
        return self.run('geck_show_windows', {'session': session}, action)

    def verify_receipt_cells(self, session):
        """For spec projects whose installed plugin is the current build: check every cell's references."""
        spec_path = getattr(self.project, 'spec', None)
        if not spec_path:
            return None
        try:
            receipt = Authoring(self.project).receipt()
        except AuthoringError:
            return None
        snapshot, _ = file_snapshot(self.project.plugin_path)
        if snapshot['sha256'] != receipt['sha256']:
            return {'skipped': 'installed plugin is not the current build'}
        out = {}
        for cell_id in receipt.get('references', {}):
            shown = self.call('cell.refs', cell_id, session=session)
            match = match_cell_references(receipt, cell_id, shown)
            if not match['ok']:
                raise BridgeError('CELL_MISMATCH', 'Loaded cell differs from the build receipt',
                                  {'cell': cell_id, 'match': match})
            out[cell_id] = {k: match[k] for k in ('expected', 'shown', 'matched')}
        return out

    def select(self, editor_id, session):
        self.project.record(editor_id)
        found = self.call_batch([('category.select', 'World Objects/Static'),
                                 ('filter.set', editor_id),
                                 ('objects.find', editor_id)], session=session)[-1]
        if not found['found']:
            raise BridgeError('RECORD_NOT_FOUND', 'Configured record absent from the scoped Object Window', found)

    def find(self, query, offset, limit, session=None):
        def action():
            sid = self.check_session(session)['sessionId']
            selected = [r for r in self.project.records if query.lower() in r.editor_id.lower()]
            rows = []
            for r in selected[offset:offset + limit]:
                self.persisted(r.editor_id)
                self.select(r.editor_id, sid)
                rows.append(r.model_dump())
            return {'scope': 'configured STAT records verified in editor and disk', 'rows': rows,
                    'total': len(selected), 'nextOffset': offset + limit if offset + limit < len(selected) else None,
                    'uiChanged': True}
        return self.run('geck_records_find', dict(query=query, offset=offset, limit=limit, session=session), action)

    def read(self, editor_id, source, session=None):
        def action():
            snapshot, record = self.persisted(editor_id)
            if source == 'disk':
                return {'source': 'disk', 'file': snapshot, 'record': record}
            sid = self.check_session(session)['sessionId']
            self.select(editor_id, sid)
            current = self.call('object.read', editor_id, session=sid)
            self.call('object.cancel', editor_id, session=sid)
            return {'source': 'editor', 'record': dict(record, modelPath=current['modelPath']),
                    'identitySource': 'disk plus exact editor ID in scoped Static dialog', 'dialogClosed': True}
        return self.run('geck_record_read', dict(editor_id=editor_id, source=source, session=session),
                        action, editor=source != 'disk')

    def set_model(self, editor_id, model_path, expected_model, expected_hash, session, request_key):
        # session may be None: the running editor is adopted; expected_model and expected_hash still guard.
        def action():
            # Validate after durable replay lookup: a historical receipt remains
            # retrievable even if an asset has since moved or been removed.
            self.project.record(editor_id)
            if not model_asset(self.project, model_path).is_file():
                raise ValueError('model file does not exist under configured meshes root')
            sid = self.check_session(session)['sessionId']
            snapshot, _ = self.persisted(editor_id)
            if snapshot['sha256'] != expected_hash:
                raise BridgeError('STALE_STATE', 'Plugin changed since it was read', snapshot)
            self.select(editor_id, sid)
            current = self.call('object.read', editor_id, session=sid)
            if current['modelPath'] != expected_model:
                self.call('object.cancel', editor_id, session=sid)
                raise BridgeError('STALE_STATE', 'Editor model differs from expected value', current)
            if current['modelPath'] == model_path:
                self.call('object.cancel', editor_id, session=sid)
                return {'changed': False, 'modelPath': model_path, 'pluginSaved': False}
            edited = self.call('object.setModel', editor_id, model_path, session=sid,
                               expected_model=expected_model)
            self.call('object.cancel', editor_id, session=sid)
            return {'_outcome': 'committed_in_editor', 'changed': True, 'edit': edited,
                    'pluginSaved': False, 'dialogClosed': True}
        return self.run('geck_record_set_model', dict(editor_id=editor_id, model_path=model_path,
                        expected_model=expected_model, expected_hash=expected_hash, session=session),
                        action, request_key, modifies=True)

    def preview(self, editor_id, session, opening):
        def action():
            sid = self.check_session(session)['sessionId']
            self.persisted(editor_id)
            if opening:
                self.select(editor_id, sid)
            return self.call('preview.open' if opening else 'preview.close', editor_id, session=sid)
        name = 'geck_preview_open' if opening else 'geck_preview_close'
        return self.run(name, dict(editor_id=editor_id, session=session), action)

    def validate(self, expected_models):
        def action():
            snapshot, data = file_snapshot(self.project.plugin_path)
            rows = []
            for editor_id, model in expected_models.items():
                configured = self.project.record(editor_id)
                row = read_model(data, editor_id)
                if row['formId'].upper() != configured.form_id.upper() or row['modelPath'] != model:
                    raise BridgeError('PERSISTENCE_MISMATCH', 'Persisted identity or model differs', row)
                rows.append(row)
            return {'file': snapshot, 'records': rows, 'persistenceVerified': True,
                    'verificationScope': CAPABILITIES['verificationLimits']}
        return self.run('geck_plugin_validate', expected_models, action, editor=False)

    def save(self, expected_models, expected_hash, session, request_key):
        def action():
            state = self.check_session(session)
            sid = state['sessionId']
            before, data = file_snapshot(self.project.plugin_path)
            if before['sha256'] != expected_hash:
                raise BridgeError('STALE_STATE', 'Plugin changed before save preflight', before)
            loaded = self.loaded_plugin(sid)
            if loaded is None or loaded != before['sha256']:
                raise BridgeError('STALE_EDITOR', 'Loaded build is unknown or older than installed bytes; reload before saving',
                                  {'loadedSha256': loaded, 'installedSha256': before['sha256']})
            # Every expectation must identify a configured existing record.
            for editor_id, model in expected_models.items():
                model_asset(self.project, model)
                self.persisted(editor_id)
            folder = self.project.state_dir / 'backups'
            folder.mkdir(exist_ok=True)
            backup = folder / (self.project.plugin + '.' + before['sha256'] + '.bak')
            if not backup.exists():
                with backup.open('xb') as stream:
                    stream.write(data)
                    stream.flush()
                    import os
                    os.fsync(stream.fileno())
            if backup.read_bytes() != data:
                raise BridgeError('BACKUP_MISMATCH', 'Recovery copy does not match pre-save bytes')
            # Check the in-editor expectations before dispatch, closing only our dialogs.
            for editor_id, model in expected_models.items():
                self.select(editor_id, sid)
                current = self.call('object.read', editor_id, session=sid)
                self.call('object.cancel', editor_id, session=sid)
                if current['modelPath'] != model:
                    raise BridgeError('STALE_STATE', 'Editor model differs from save expectation', current)
            state = self.check_session(sid)
            if state['unsavedChanges']:
                saved = self.call('plugin.save', before['sha256'], session=sid)
            else:
                saved = {'changed': False, 'reason': 'editor already clean'}
            after, persisted = file_snapshot(self.project.plugin_path)
            final_state = self.check_session(sid)
            if final_state['unsavedChanges']:
                raise BridgeError('SAVE_UNCONFIRMED', 'Editor has pending changes after save verification')
            rows = []
            for editor_id, model in expected_models.items():
                row = read_model(persisted, editor_id)
                if (row['modelPath'] != model or
                        row['formId'].upper() != self.project.record(editor_id).form_id.upper()):
                    raise BridgeError('PERSISTENCE_MISMATCH', 'Saved record differs from expectation', row)
                rows.append(row)
            return {'_outcome': 'saved_verified', 'before': before, 'after': after, 'sessionId': sid, 'verifiedAt': time.time(),
                    'backup': str(backup), 'records': rows, 'save': saved,
                    'persistenceVerified': True, 'saveScope': 'all pending changes in active plugin',
                    'verificationScope': CAPABILITIES['verificationLimits']}
        return self.run('geck_plugin_save', dict(expected_models=expected_models,
                        expected_hash=expected_hash, session=session), action, request_key, modifies=True)

    def cell_verify(self, cell_id, session, receipt, frame_form_id=None):
        """Tier 3: compare Cell View's reference list with a build receipt; optionally frame one reference."""
        def action():
            sid = self.check_session(session)['sessionId']
            if receipt.get('plugin') != self.project.plugin:
                raise ValueError('receipt is for %s, project plugin is %s' % (receipt.get('plugin'), self.project.plugin))
            snapshot, _ = file_snapshot(self.project.plugin_path)
            if snapshot['sha256'] != receipt['sha256']:
                raise BridgeError('STALE_STATE', 'Installed plugin differs from the receipt build', snapshot)
            windows = self.restore_windows(sid)
            shown = self.call('cell.refs', cell_id, session=sid)
            match = match_cell_references(receipt, cell_id, shown)
            out = {'cell': cell_id, 'match': match, 'columns': shown.get('columns'), 'rows': shown.get('rows')}
            if windows.get('shown'):
                out['windowsRestored'] = windows['shown']
            if not match['ok']:
                raise BridgeError('CELL_MISMATCH', 'Cell View does not match the build receipt', out)
            if frame_form_id:
                out['camera'] = self.call('cell.show', cell_id, frame_form_id, session=sid)
            return out
        return self.run('geck_cell_verify', dict(cell=cell_id, session=session, sha256=receipt.get('sha256'),
                                                 frame=frame_form_id), action)

    # -- Milestone 2: spec-driven authoring ------------------------------------------------
    def authoring(self):
        return Authoring(self.project)

    def _authoring_call(self, fn):
        try:
            return fn()
        except SpecError as e:
            raise BridgeError('SPEC_INVALID', str(e), {'where': e.where})
        except AuthoringError as e:
            raise BridgeError(e.code, str(e), e.evidence)

    def spec_validate(self, spec=None):
        def action():
            receipt, _ = self._authoring_call(lambda: self.authoring().build(write=False, spec_override=spec))
            out = {k: receipt[k] for k in ('plugin', 'sha256', 'size', 'recordCounts', 'formIds',
                                            'references', 'warnings', 'validation')}
            if not receipt['validation']['ok']:
                raise BridgeError('VALIDATION_FAILED', 'Spec builds but fails validation', out)
            return out
        return self.run('geck_spec_validate', {'inline': spec is not None}, action, editor=False)

    def plugin_build(self, request_key):
        def action():
            receipt, _ = self._authoring_call(lambda: self.authoring().build(write=True))
            if not receipt['validation']['ok']:
                raise BridgeError('VALIDATION_FAILED', 'Build failed validation; nothing written',
                                  receipt['validation'])
            return {'_outcome': 'built', **{k: receipt[k] for k in (
                'plugin', 'sha256', 'size', 'recordCounts', 'references', 'warnings', 'validation', 'written')},
                'next': 'geck_plugin_install, then geck_plugin_load (reload) and geck_cell_verify'}
        spec_path = getattr(self.project, 'spec', None)
        spec_hash = hashlib.sha256(spec_path.read_bytes()).hexdigest() if spec_path and spec_path.exists() else None
        return self.run('geck_plugin_build', {'spec': spec_hash}, action, request_key)

    def plugin_install(self, session, request_key, allow_replace_foreign=None):
        # session may be None (adopt the running editor); install still refuses a dirty or edited plugin.
        def action():
            state = self.check_session(session, require_plugin=False, no_dialogs=False)
            entry = self._authoring_call(lambda: self.authoring().install(state['editorTitle'], allow_replace_foreign))
            if entry.get('changed'):
                entry['_outcome'] = 'installed'
                if entry.get('reloadNeeded'):
                    entry['next'] = 'GECK still shows the old version: geck_plugin_load with reload=true'
            return entry
        return self.run('geck_plugin_install', dict(session=session, foreign=allow_replace_foreign),
                        action, request_key, modifies=True)

    def plugin_inspect(self, which):
        return self.run('geck_plugin_inspect', {'which': which},
                        lambda: self._authoring_call(lambda: self.authoring().inspect(which)), editor=False)

    def master_lookup(self, query, record_type=None, limit=25):
        return self.run('geck_master_lookup', dict(query=query, type=record_type, limit=limit),
                        lambda: self._authoring_call(lambda: self.authoring().lookup(query, record_type, limit)),
                        editor=False)

    def cell_verify_ref(self, cell_id, session=None, frame_ref_id=None):
        receipt = self._authoring_call(lambda: self.authoring().receipt())
        frame = None
        if frame_ref_id:
            refs = [r for r in receipt['references'].get(cell_id, []) if r['refId'] == frame_ref_id]
            if len(refs) != 1:
                raise ValueError('ref_id %r is not in the build receipt for %s' % (frame_ref_id, cell_id))
            frame = refs[0]['formId']
        return self.cell_verify(cell_id, session, receipt, frame)

    def acknowledge(self, operation_id, session, expected_hash, note):
        def action():
            previous = self.operation(operation_id)
            state = self.check_session(session, require_plugin=False, no_dialogs=True)
            snapshot, _ = file_snapshot(self.project.plugin_path)
            if snapshot['sha256'] != expected_hash:
                raise BridgeError('STALE_STATE', 'Disk changed during recovery review')
            is_compile = previous['operation'] == 'geck_compile'
            if is_compile and state['unsavedChanges']:
                raise BridgeError('UNSAVED_CHANGES', 'Resolve dirty work before releasing the compile reservation', state)
            workflow = None
            if is_compile:
                from .compilation import Compilation
                workflow = Compilation(self)
                checkpoint = workflow.read(operation_id)  # validate recovery state before writing acknowledgment
            with closing(self.db()) as db, db:
                row = db.execute('SELECT * FROM operations WHERE id=? AND project=?',
                                 (operation_id, self.project.project_id)).fetchone()
                allowed = (*UNRESOLVED, 'pending', 'blocked', 'failed_before_change') if is_compile else UNRESOLVED
                finishing_review = bool(is_compile and row and row['recovery_ack'] and
                                        reservation(self.project.lock_root) == workflow.owner(operation_id))
                if row is None or row['state'] not in allowed or (row['recovery_ack'] and not finishing_review):
                    raise ValueError('operation is not an unresolved operation in this project')
                db.execute('UPDATE operations SET recovery_ack=? WHERE id=?',
                           (json.dumps(dict(session=state['sessionId'], file=snapshot, note=note, time=time.time())),
                            operation_id))
            if is_compile:
                from .compilation import Compilation
                workflow = Compilation(self)
                cp = workflow.read(operation_id)
                cp['released'] = {'note': note, 'time': time.time()}
                workflow.write(cp)
                reservation(self.project.lock_root, workflow.owner(operation_id), release=True)
            if previous['operation'] == 'geck_plugin_load':
                owner = {'operationId': operation_id, 'journal': str(self.db_path.resolve())}
                if reservation(self.project.lock_root) == owner:
                    reservation(self.project.lock_root, owner, release=True)
            return {'acknowledged': operation_id, 'note': note,
                    'warning': 'Barrier cleared; this is not rollback or proof of the prior result'}
        return self.run('geck_recovery_acknowledge', dict(operation_id=operation_id, session=session,
                        expected_hash=expected_hash, note=note), action)

    # -- Photo workflow (geck_mcp/photo.py, geck_mcp/imaging.py) -------------------------------
    def photo_studio(self):
        if getattr(self, '_photo', None) is None:
            from .photo import PhotoStudio
            self._photo = PhotoStudio(self.project, lambda project: Service(project))
        return self._photo

    def _envelope(self, name, started, fn):
        from .imaging import ImagingError
        try:
            data = fn()
            error, ok = None, True
        except (BridgeError, ImagingError, AuthoringError) as e:
            data, ok = getattr(e, 'evidence', None), False
            error = {'code': e.code, 'message': str(e), 'retrySafe': e.code not in ('OUTCOME_UNKNOWN',)}
        except (ValueError, SpecError) as e:
            data, ok = None, False
            error = {'code': 'INVALID_ARGUMENT', 'message': str(e), 'retrySafe': True}
        except RuntimeError as e:
            data, ok = None, False
            error = {'code': 'EDITOR_BUSY' if 'EDITOR_BUSY' in str(e) else 'BRIDGE_ERROR', 'message': str(e),
                     'retrySafe': 'EDITOR_BUSY' in str(e)}
        outcome = 'failed_before_change'
        if ok:
            outcome = {'loading': 'loading', 'done': 'captured'}.get((data or {}).get('stage'), 'captured')
            if name in ('geck_compile_status', 'geck_runtime_readiness', 'geck_runtime_setup_preflight', 'geck_console_readiness'):
                outcome = 'unchanged'
        return {'ok': ok, 'operationId': (data or {}).get('operationId') or str(uuid.uuid4()), 'operation': name,
                'projectId': self.project.project_id, 'outcome': outcome, 'data': data, 'error': error,
                'elapsedMs': round((time.monotonic() - started) * 1000)}

    def actor_photo(self, subject, rotation=180, pitch=None, fill=0.86, lighting='auto', render_size=None):
        from .photo import DEFAULT_PITCH
        started = time.monotonic()
        return self._envelope('geck_actor_photo', started, lambda: self.photo_studio().photo(
            subject, rotation=rotation, pitch=DEFAULT_PITCH if pitch is None else pitch, fill=fill,
            lighting=lighting, render_size=render_size))

    def render_capture(self, cutout=False):
        from . import imaging
        import os

        def action_windows():
            from . import wincapture
            window = wincapture.render_window()
            folder = self.project.state_dir.parent / 'photos'
            stamp = time.strftime('%Y%m%d-%H%M%S')
            # PrintWindow sometimes returns only the grey window background for GECK's Direct3D view, and a
            # freshly loaded cell takes a moment to draw: retry a blank capture, asking GECK to repaint, for ~12 s.
            deadline, tries = time.monotonic() + 12, 0
            while True:
                shot = wincapture.capture(window, folder / ('render-%s.png' % stamp), repaint=tries > 0)
                tries += 1
                if not shot['blank'] or time.monotonic() > deadline:
                    break
            out = {'photo': shot['path'], 'size': shot['size'], 'client': shot['client'], 'window': window['title'],
                   'method': shot['method'], 'pixels': shot['stats'], 'blank': shot['blank'],
                   'attempts': shot['attempts'], 'tries': tries}
            if shot['blank']:
                raise imaging.ImagingError('CAPTURE_BLANK', 'The Render Window capture is one flat colour (blank '
                                           'or black): load a cell (geck_cell_verify with frame_ref_id) or check '
                                           'that GECK is not minimised', dict(out, attempts=shot['attempts']))
            if cutout:
                out['cutout'] = imaging.cutout(shot['path'], folder / ('render-%s-cutout.png' % stamp))
            return out

        def action():
            from .photo import MAC_TITLE_BAR_POINTS
            window = imaging.render_window()
            folder = self.project.state_dir.parent / 'photos'
            stamp = time.strftime('%Y%m%d-%H%M%S')
            raw = imaging.capture_window(window['id'], folder / ('render-%s-raw.png' % stamp))
            _, Image = imaging._np()
            image = Image.open(raw)
            top = int(round(MAC_TITLE_BAR_POINTS * image.width / float(window['bounds']['Width'])))
            path = folder / ('render-%s.png' % stamp)
            image.crop((0, top, image.width, image.height)).convert('RGB').save(path)
            raw.unlink()
            out = {'photo': str(path), 'size': [image.width, image.height - top], 'window': window['title']}
            if cutout:
                out['cutout'] = imaging.cutout(path, folder / ('render-%s-cutout.png' % stamp))
            return out
        return self._envelope('geck_render_capture', time.monotonic(), action_windows if os.name == 'nt' else action)

    def image_cutout(self, path, tolerance=30, chroma_tolerance=22):
        from . import imaging
        from pathlib import Path

        def action():
            src = Path(path).expanduser().resolve(strict=True)
            if src.suffix.lower() != '.png':
                raise ValueError('path must be a .png file')
            return {'cutout': imaging.cutout(src, src.with_name(src.stem + '-cutout.png'),
                                             tolerance=tolerance, chroma_tolerance=chroma_tolerance)}
        return self._envelope('geck_image_cutout', time.monotonic(), action)
