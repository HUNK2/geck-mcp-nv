"""Opt-in xNVSE console commands, independent from the read-only telemetry adapter.

No keyboard injection. Public Console API executes on the game loop. A durable
claim prevents command replay; API acknowledgment and later effect proof differ.
"""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import time
import uuid

from bridge_lock import bottle_lock, reservation
from .authoring import atomic_write
from .runtime import file_digest
from . import runtime_companion as runtime
from .transport import BridgeError

NATIVE = Path(__file__).resolve().parents[1] / 'native/runtime'
WIRE = struct.Struct('<8s33s33s65sIQIQI513sII65sdI65s65sIII33s')
TARGET_KEYS = ('pid', 'started', 'epoch', 'build_id', 'context', 'runtime_prefix', 'plugin_sha256')
DENIED = {'load', 'loadgame', 'con_loadgame', 'qqq', 'quitgame', 'exitgame', 'restartgame', 'bat', 'runbatch', 'cow'}
TERMINAL = {'acknowledged', 'query_verified', 'effect_verified', 'effect_not_verified',
            'context_changed_after_dispatch', 'rejected_before_dispatch', 'invalid_verification',
            'verification_compile_error', 'invalid_query'}


def paths(project):
    token = 'geck_mcp_console_' + hashlib.sha256(project.project_id.encode()).hexdigest()[:12]
    base = project.game_root / 'Data/NVSE/Plugins'
    files = {suffix: base / (token + suffix) for suffix in
             ('.dll', '.ready.json', '.request', '.claimed', '.result.json', '.context')}
    # Under Mod Organizer 2 the game runs in MO2's virtual file system (usvfs). A file the game
    # creates under Data, where the real folder has none, is written to MO2's overwrite folder, and a
    # file in overwrite hides the real one. So the files the companion writes (.ready.json, .claimed
    # from renaming .request, .result.json, and .context after a teleport) are read where the game
    # sees them. .request stays in the real folder: usvfs fixes its file list at launch, so the game
    # would not see a file added to overwrite from outside, but it does see real files it has no
    # entry for.
    overwrite = mo2_overwrite(project)
    if overwrite:
        for suffix in ('.ready.json', '.claimed', '.result.json', '.context'):
            created = overwrite / (token + suffix)
            if created.exists() or (suffix != '.context' and not files[suffix].exists()):
                files[suffix] = created
    return files | {'token': token, 'receipt': project.state_dir / 'console/install.json'}


def mo2_overwrite(project):
    """MO2's overwrite folder for Data/NVSE/Plugins when the profile's fnv block names an MO2 root."""
    fnv = getattr(project, 'fnv', None)
    root = getattr(fnv, 'mo2_root', None) if fnv else None
    return Path(root) / 'overwrite/NVSE/Plugins' if root else None


def lock_root(project):
    return project.game_root / '.geck-runtime-lock'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, (json.dumps(value, indent=2) + '\n').encode())


def preflight(project):
    p = paths(project)
    return {'adapter': 'xnvse-console', 'optIn': True, 'nativeWindowsRequired': True, 'wireVersion': 2,
            'transport': 'public xNVSE Console API; no simulated keystrokes',
            'requires': ['FNV Steam 1.4.0.525', 'xNVSE 6.4.9+ 6.x', 'Console interface version >=2',
                         'User-provided Zig 0.14.1 for build; no compiler download', 'FNV closed for explicit install'],
            'files': {key: str(value) for key, value in p.items() if key != 'token'},
            'backups': str(project.state_dir / 'console/backups'),
            'installed': p['.dll'].exists() and p['receipt'].exists(), 'restartRequired': True,
            'restrictions': {'singleLine':True,'maxCharacters':512,'crossPlugin':'explicit plugin/local_form/sha256 binding; use {form}',
                             'coc':'authorize_teleport=true plus cell EditorID and matching CELL binding; exact parent-cell proof',
                             'noReplay':True,'typedQueries':['plugin_index','item_count','equipped','weapon_health_percent','equipped_health']},
            'effectVerification': ['quest_running', 'quest_variable; numeric equality on a later game loop'],
            'numericResult': 'Typed plugin_index/item_count/equipped/weapon_health_percent/equipped_health; no arbitrary console-text capture',
            'unsupported': sorted(DENIED), 'arbitraryConsoleOutputCapture': False,
            'steps': ['Build the separate console companion against the exact installed plugin hash.',
                      'Inspect build receipt and these paths, then explicitly install with FNV closed.',
                      'Start through xNVSE and load a disposable save; require fresh console readiness.',
                      'Pass returned target identity and a unique request key for each intended command.',
                      'Poll status; never replay an uncertain command.']}


def build(project, zig, expected_plugin_hash):
    if project.launcher != 'native' or not re.fullmatch(r'[A-Za-z0-9_. -]{1,60}\.esp', project.plugin, re.I):
        raise ValueError('Console companion requires native Windows and a simple configured ESP name')
    if file_digest(project.plugin_path) != expected_plugin_hash:
        raise ValueError('Installed plugin differs from expected build')
    zig = Path(zig).resolve(strict=True)
    version = subprocess.run([str(zig), 'version'], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    if version != '0.14.1':
        raise ValueError('Console companion currently validates Zig 0.14.1 only')
    p = paths(project)
    source_hashes = {name: file_digest(NATIVE / name) for name in
                     ('console_companion.c', 'console_protocol.h', 'nvse_abi.h')}
    identity = {'project_id': project.project_id, 'plugin': project.plugin,
                'plugin_sha256': expected_plugin_hash, 'source': source_hashes, 'wire_version': 2}
    build_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    folder = project.state_dir / 'console/builds' / uuid.uuid4().hex
    folder.mkdir(parents=True)
    macros = {'GMC_PLUGIN': project.plugin, 'GMC_PLUGIN_HASH': expected_plugin_hash, 'GMC_BUILD_ID': build_id,
              'GMC_NAME': 'GECK MCP console ' + project.project_id, 'GMC_TOKEN': p['token']}
    (folder / 'console_config.h').write_text('\n'.join('#define %s %s' % (key, json.dumps(value))
                                                     for key, value in macros.items()) + '\n', encoding='ascii')
    for source, output, shared in [('console_companion.c', p['token'] + '.dll', True),
                                    ('console_fake_host.c', 'console_fake_host.exe', False)]:
        command = [str(zig), 'cc', '-target', 'x86-windows-gnu', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                   '-I', str(folder), '-I', str(NATIVE)]
        if shared:
            command.append('-shared')
        subprocess.run(command + [str(NATIVE / source), '-ladvapi32', '-o', str(folder / output)],
                       check=True, timeout=300)
    receipt = {**identity, 'schema_version': 1, 'build_id': build_id, 'dll': str(folder / (p['token'] + '.dll')),
               'sha256': file_digest(folder / (p['token'] + '.dll')), 'compilerVersion': version}
    receipt['build_receipt'] = str(folder / 'build.json')
    write_json(folder / 'build.json', receipt)
    return receipt


def install(project, receipt, expected_plugin_hash):
    with bottle_lock(lock_root(project)):
        runtime.game_closed(project)
        p = paths(project)
        if reservation(lock_root(project)) or any(p[suffix].exists() for suffix in ('.request', '.claimed', '.result.json')):
            raise ValueError('Review/release the prior command before installing a console companion')
        if (receipt.get('schema_version') != 1 or receipt.get('project_id') != project.project_id or
                receipt.get('plugin') != project.plugin or receipt.get('plugin_sha256') != expected_plugin_hash or
                file_digest(project.plugin_path) != expected_plugin_hash):
            raise ValueError('Console build/plugin identity differs')
        if receipt.get('wire_version') != 2 or receipt.get('source') != {name:file_digest(NATIVE/name) for name in
                ('console_companion.c','console_protocol.h','nvse_abi.h')}:
            raise ValueError('Build receipt is for an older wire/source version; build the current companion first')
        source = Path(receipt['dll']).resolve(strict=True)
        if not source.is_relative_to((project.state_dir / 'console/builds').resolve()) or file_digest(source) != receipt['sha256']:
            raise ValueError('Console DLL changed or is outside owned build state')
        previous = json.loads(p['receipt'].read_text()) if p['receipt'].exists() else None
        if p['.dll'].exists() and (not previous or previous.get('sha256') != file_digest(p['.dll']) or
                previous.get('target') != str(p['.dll'].resolve()) or previous.get('game_root') != str(project.game_root.resolve())):
            raise ValueError('Refusing to replace an unowned/modified console companion')
        if not previous and any(p[suffix].exists() for suffix in ('.context', '.ready.json', '.result.json')):
            raise ValueError('Unowned console state exists; preserve and review it before installation')
        backup = project.state_dir / 'console/backups' / uuid.uuid4().hex
        backup.mkdir(parents=True)
        for suffix in ('.dll', '.context', '.ready.json', '.result.json'):
            if p[suffix].exists():
                atomic_write(backup / p[suffix].name, p[suffix].read_bytes())
        if previous:
            write_json(backup / 'install.json', previous)
        p['.dll'].parent.mkdir(parents=True, exist_ok=True)
        atomic_write(p['.dll'], source.read_bytes())
        context_path = project.state_dir / 'companion/save-context.json'
        context = json.loads(context_path.read_text())['id'] if context_path.exists() else uuid.uuid4().hex
        atomic_write(p['.context'], context.encode('ascii'))
        installed = {**receipt, 'target': str(p['.dll'].resolve()), 'game_root': str(project.game_root.resolve()),
                     'installed_at': time.time(), 'backup': str(backup)}
        write_json(p['receipt'], installed)
        return {'installed': True, 'restartRequired': True, 'receipt': str(p['receipt']), 'backup': str(backup)}


def invalidate_context(project, context):
    """Explicit save-context changes invalidate queued commands before native dispatch."""
    p = paths(project)
    if p['receipt'].exists():
        receipt = json.loads(p['receipt'].read_text())
        if receipt.get('target') != str(p['.dll'].resolve()) or receipt.get('game_root') != str(project.game_root.resolve()):
            raise ValueError('Console context target ownership differs')
        atomic_write(p['.context'], context.encode('ascii'))


def live(project, expected_plugin_hash):
    p = paths(project)
    if project.launcher != 'native' or not p['receipt'].exists():
        raise ValueError('Console companion is not installed; use explicit console preflight/build/install')
    receipt = json.loads(p['receipt'].read_text())
    if (receipt.get('project_id') != project.project_id or receipt.get('plugin') != project.plugin or
            receipt.get('game_root') != str(project.game_root.resolve()) or receipt.get('target') != str(p['.dll'].resolve()) or
            receipt.get('plugin_sha256') != expected_plugin_hash or file_digest(project.plugin_path) != expected_plugin_hash or
            file_digest(p['.dll']) != receipt.get('sha256')):
        raise ValueError('Console installation/plugin/DLL build identity changed; rebuild/restart explicitly')
    document = json.loads(p['.ready.json'].read_text())
    if document.get('wire_version') != 2:
        raise ValueError('Console wire version changed; explicitly build/install updated companion and restart')
    if document.get('schema_version') != 1 or document.get('adapter') != 'geck-console-1':
        raise ValueError('Unsupported console handshake')
    pid = document.get('pid')
    if type(pid) != int or not 0 < pid < 2 ** 32:
        raise ValueError('Invalid console process identity')
    started = runtime.process_identity(pid, project.game_root / 'FalloutNV.exe', p['.dll'])
    if str(document.get('started')) != str(started) or document.get('build_id') != receipt['build_id']:
        raise ValueError('Console handshake belongs to another process/build')
    age = runtime.ticks() - document.get('tick_ms', -10000)
    if not 0 <= age <= 3000 or document.get('status') != 'ready' or document.get('console_api', 0) < 2:
        raise ValueError('Console companion is stale/not ready: ' + str(document.get('status')))
    if (document.get('plugin') != project.plugin or document.get('plugin_sha256') != expected_plugin_hash or
            document.get('context') != p['.context'].read_text(encoding='ascii') or
            not re.fullmatch(r'[a-f0-9]{32}', document.get('context', '')) or
            type(document.get('runtime_prefix')) != int or not 0 <= document['runtime_prefix'] < 255 or
            type(document.get('epoch')) != int or document['epoch'] < 1):
        raise ValueError('Console plugin/mapping/save-context identity differs')
    document['started'] = str(started)
    return document


def readiness(project, expected_plugin_hash):
    result = {**preflight(project), 'ready': False, 'mapping': 'unverified', 'target': None}
    try:
        doc = live(project, expected_plugin_hash)
        with bottle_lock(lock_root(project)):
            occupied = bool(reservation(lock_root(project)))
        occupied = occupied or any(paths(project)[s].exists() for s in ('.request', '.claimed', '.result.json'))
        result.update(ready=not occupied, mapping='verified_loaded_console_companion',
                      target={key: doc[key] for key in TARGET_KEYS}, evidence=doc)
        if occupied:
            result['reason'] = 'Prior console operation requires status/reconciliation; no new dispatch is available'
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        result['reason'] = str(error)
    reason = result.get('reason', '')
    result['state'] = ('ready' if result['ready'] else 'not_installed' if not result['installed'] else
                       'waiting_for_loaded_save' if 'no_loaded_game' in reason else
                       'waiting_for_loading' if 'teleport_loading' in reason or 'waiting_for_loading' in reason else
                       'busy' if 'Prior console' in reason else 'restart_needed')
    result['setupTool'] = 'geck_console_setup'
    return result


def prepare(command, target, verify=None, binding=None, authorize_teleport=False):
    if not isinstance(command, str) or not 1 <= len(command) <= 512 or any(not 32 <= ord(c) <= 126 for c in command):
        raise ValueError('One printable ASCII console line, 1..512 characters, is required')
    if binding:
        command = re.sub(r'(?i)\b(?:0x)?[0-9a-f]{2}' + binding['local_form'] + r'\b', '{form}', command)
    teleport = re.fullmatch(r'(?i)coc +([A-Za-z_][A-Za-z0-9_]{0,99})', command.strip())
    if not command.strip(): raise ValueError('Command cannot be blank')
    if authorize_teleport and not teleport: raise ValueError('Teleport authorization applies only to coc CellEditorID')
    if command.strip().split()[0].lower() == 'coc' and (not authorize_teleport or not teleport):
        raise ValueError('coc requires explicit authorize_teleport=true and one cell EditorID')
    command = command.replace('{prefix}', '%02X' % target['runtime_prefix']).strip()
    if not command or ';' in command or '{' in command.replace('{form}', '') or '}' in command.replace('{form}', '') or command.split()[0].lower() in DENIED:
        raise ValueError('Load/quit/batch commands and multiline/chained input are unsupported')
    if any(token[:2].upper() not in ('00', '%02X' % target['runtime_prefix'])
           for token in re.findall(r'(?i)\b(?:0x)?([0-9a-f]{8})\b', command)):
        raise ValueError('Raw FormID uses another runtime prefix; use {prefix} with project-local suffixes')
    if '{form}' in command and not binding:
        raise ValueError('{form} requires explicit plugin/local_form/hash binding')
    verify = dict(verify or {})
    if set(verify) - {'kind', 'local_form', 'variable', 'equals'}:
        raise ValueError('Unknown verification field')
    if verify and authorize_teleport:
        raise ValueError('Teleport uses its own destination proof; do not attach another verification')
    if verify:
        if binding and verify.get('local_form', '').upper() != binding['local_form'].upper():
            raise ValueError('Verification local_form must match the explicit named binding')
        if verify.get('kind') not in ('quest_running', 'quest_variable', 'item_count', 'equipped') or not re.fullmatch(r'[0-9a-fA-F]{6}', verify.get('local_form', '')):
            raise ValueError('Verification needs quest_running/quest_variable and a six-digit local form')
        if int(verify['local_form'], 16) == 0:
            raise ValueError('Verification local form cannot be zero')
        if verify['kind'] == 'quest_variable' and not re.fullmatch(r'[A-Za-z_]\w{0,63}', verify.get('variable', ''), re.ASCII):
            raise ValueError('quest_variable verification requires an ASCII variable name')
        if type(verify.get('equals')) not in (int, float) or not math.isfinite(verify['equals']):
            raise ValueError('Verification requires a finite numeric equals value')
    return command, verify


def subject(project, binding, plugin_only=False):
    if not binding: return {}
    binding = dict(binding)
    if plugin_only: binding.setdefault('local_form', '000000')
    if set(binding) != {'plugin', 'local_form', 'sha256'}:
        raise ValueError('Binding requires plugin, local_form and exact installed sha256')
    name = binding['plugin']
    if not re.fullmatch(r'[A-Za-z0-9_. -]{1,60}\.(?:esp|esm)', name, re.I) or '..' in name:
        raise ValueError('Invalid named plugin')
    if not re.fullmatch(r'[0-9a-fA-F]{6}', binding['local_form']) or not (0 if plugin_only else 1) <= int(binding['local_form'], 16) <= 0xFFFFFF:
        raise ValueError('Binding needs nonzero six-digit local FormID')
    if file_digest(project.game_root / 'Data' / name) != binding['sha256']:
        raise ValueError('Named plugin installed hash differs')
    return dict(binding)


def teleport_destination(project, command, binding):
    """Bind the console EditorID to an originating CELL, never a runtime EDID lookup."""
    from .esp import codec
    if not binding:
        raise ValueError('coc requires a destination CELL binding: plugin/local_form/sha256 matching the cell EditorID')
    data = (project.game_root / 'Data' / binding['plugin']).read_bytes()
    if hashlib.sha256(data).hexdigest() != binding['sha256']:
        raise ValueError('Destination plugin changed while resolving CELL identity')
    headers = list(codec.iter_records(data, types={'TES4'}))
    if len(headers) != 1:
        raise ValueError('Destination plugin requires one TES4 header')
    header = headers[0][0]
    origin_index = len(header.get_all('MAST'))
    local = int(binding['local_form'], 16)
    matches = [r for r, _ in codec.iter_records(data, types={'CELL'})
               if r.form_id == (origin_index << 24 | local)]
    cell = command.split()[1]
    if len(matches) != 1 or (matches[0].editor_id or '').lower() != cell.lower():
        raise ValueError('Destination binding must name the originating CELL with the exact coc EditorID; overrides are not origins')
    return {'plugin': binding['plugin'], 'local_form': binding['local_form'], 'sha256': binding['sha256'],
            'editor_id': matches[0].editor_id, 'proof': 'exact_player_parent_cell'}


def query(service, kind, intent, target, expected_plugin_hash, request_key, binding=None, slot=5):
    kinds = {'plugin_index':2, 'item_count':3, 'equipped':4, 'weapon_health_percent':5, 'equipped_health':6}
    if kind not in kinds or type(slot) != int or not 0 <= slot <= 19:
        raise ValueError('Unsupported typed query/slot')
    if kind in ('plugin_index', 'item_count', 'equipped') and not binding:
        raise ValueError('This query requires a named plugin binding')
    # The native query never runs arbitrary console text; command carries an allowlisted slot only.
    return Console(service).execute(str(slot), intent, target, expected_plugin_hash, request_key,
                                    binding=binding, query_kind=kinds[kind])


class Console:
    def __init__(self, service):
        self.s, self.p = service, service.project
        with closing(service.db()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS console_operations (id TEXT PRIMARY KEY,data TEXT NOT NULL)')

    def write(self, cp):
        with closing(self.s.db()) as db, db:
            db.execute('INSERT OR REPLACE INTO console_operations VALUES (?,?)', (cp['operationId'], json.dumps(cp)))

    def read(self, operation_id):
        with closing(self.s.db()) as db:
            row = db.execute('SELECT data FROM console_operations WHERE id=?', (operation_id,)).fetchone()
        if row is None:
            raise ValueError('Console operation not found in this project')
        return json.loads(row[0])

    def owner(self, op):
        return {'operationId': op, 'journal': str(self.s.db_path.resolve())}

    def execute(self, command, intent, target, expected_plugin_hash, request_key, verify=None, timeout_seconds=10, binding=None, authorize_teleport=False, query_kind=0):
        args = dict(command=command, intent=intent, target=target, expected_plugin_hash=expected_plugin_hash,
                    verify=verify, timeout_seconds=timeout_seconds, binding=binding, authorize_teleport=authorize_teleport, query_kind=query_kind)
        def action():
            if not isinstance(intent, str) or not 3 <= len(intent.strip()) <= 500 or not 1 <= timeout_seconds <= 30:
                raise ValueError('Explicit command intent and bounded timeout (1..30 seconds) are required')
            doc = live(self.p, expected_plugin_hash)
            if set(target) != set(TARGET_KEYS) or any(target[k] != doc[k] for k in TARGET_KEYS):
                raise ValueError('Game/process/build/mapping/context changed; obtain fresh readiness and preserve started as a decimal string')
            binding_checked = subject(self.p, binding, query_kind == 2)
            line, verification = prepare(command, target, verify, binding_checked, authorize_teleport)
            destination = teleport_destination(self.p, line, binding_checked) if authorize_teleport else None
            p = paths(self.p)
            op = self.s._active_operation
            if reservation(lock_root(self.p)) or any(p[s].exists() for s in ('.request', '.claimed', '.result.json')):
                raise BridgeError('CONSOLE_BUSY', 'Prior console command requires status/reconciliation; do not replay it')
            cp = {**args, 'command': line, 'verify': verification, 'operationId': op, 'id': uuid.uuid4().hex,
                  'phase': 'dispatch_intent', 'createdAt': time.time(), 'sent': False, 'acknowledged': False,
                  'effectVerified': None, 'deadlineTick': runtime.ticks() + timeout_seconds * 1000, 'nextContext': uuid.uuid4().hex if authorize_teleport else '',
                  'bindingVerifiedOnDisk': binding_checked, 'destinationVerifiedOnDisk': destination}
            self.write(cp)
            reservation(lock_root(self.p), self.owner(op))
            query = line == 'GetModIndex "%s"' % self.p.plugin
            payload = WIRE.pack(b'GMCMD02\0', cp['id'].encode(), target['context'].encode(), target['build_id'].encode(),
                target['pid'], int(target['started']), target['epoch'], cp['deadlineTick'], target['runtime_prefix'], line.encode(),
                {'quest_running': 1, 'quest_variable': 2, 'item_count': 3, 'equipped': 4, 'weapon_health_percent': 5, 'equipped_health': 6}.get(verification.get('kind'), 0),
                int(verification.get('local_form', '0'), 16), verification.get('variable', '').encode(),
                verification.get('equals', 0), int(query),
                binding_checked.get('plugin', '').encode(), binding_checked.get('sha256', '').encode(),
                int(binding_checked.get('local_form', '0'), 16), query_kind, int(authorize_teleport), cp['nextContext'].encode())
            # Receipt+reservation survive cancellation before/after the atomic inbox write.
            self.s._mutation_dispatched = True
            if cp['nextContext']:
                write_json(self.p.state_dir / 'companion/save-context.json', {'id':cp['nextContext'],
                    'label':'Console teleport '+cp['id'],'tick_ms':runtime.ticks(),'time':time.time()})
            atomic_write(p['.request'], payload)
            cp['sent'], cp['phase'] = True, 'sent'
            self.write(cp)
            return {**cp, '_outcome': 'pending', 'next': 'Poll geck_console_status; never automatically retry this command.'}
        return self.s.run('geck_console_execute', args, action, request_key, modifies=True, editor=False, lock_root=lock_root(self.p))

    def status(self, operation_id):
        def action():
            cp = self.read(operation_id)
            cp['target']['started'] = str(cp['target']['started'])
            if cp.get('terminal'):
                if reservation(lock_root(self.p)) == self.owner(operation_id):
                    self.archive(cp)
                    reservation(lock_root(self.p), self.owner(operation_id), release=True)
                return {**cp, '_outcome': cp['outcome']}
            p = paths(self.p)
            if p['.result.json'].exists():
                result = json.loads(p['.result.json'].read_text())
                target = cp['target']
                if (result.get('id') != cp['id'] or result.get('pid') != target['pid'] or
                        str(result.get('started')) != target['started'] or result.get('build_id') != target['build_id'] or
                        result.get('context') != target['context']):
                    raise BridgeError('CONSOLE_RECEIPT_MISMATCH', 'Command receipt identity differs; nothing replayed', result)
                result['started'] = str(result['started'])
                if cp.get('nextContext') and result.get('nextContext') == cp['nextContext'] and result.get('sent'):
                    write_json(self.p.state_dir / 'companion/save-context.json', {'id':cp['nextContext'],
                        'label':'Console teleport '+cp['id'],'tick_ms':runtime.ticks(),'time':time.time()})
                cp.update(sent=bool(result.get('sent')) or cp['sent'], acknowledged=result.get('acknowledged') is True,
                          effectVerified=result.get('effectVerified'), native=result, phase=result['status'])
                if result['status'] in TERMINAL:
                    cp['terminal'] = True
                    cp['outcome'] = 'verified' if cp['effectVerified'] is True else 'acknowledged' if cp['acknowledged'] else 'failed'
                    self.write(cp)  # retain terminal proof even if evidence archival is interrupted
                    self.archive(cp)
                    self.write(cp)
                    reservation(lock_root(self.p), self.owner(operation_id), release=True)
                    return {**cp, '_outcome': cp['outcome'], 'next': 'Command is complete; inspect acknowledgment and effect proof separately.'}
            if runtime.ticks() > cp['deadlineTick'] or cp.get('native', {}).get('status') == 'execution_unconfirmed':
                cp['phase'] = 'outcome_unknown'
                self.write(cp)
                return {**cp, '_outcome': 'outcome_unknown', 'next': 'Inspect game/receipt; do not replay. Explicit release requires FNV normally closed.'}
            self.write(cp)
            return {**cp, '_outcome': 'pending', 'next': 'Await the game loop receipt; no resubmission.'}
        return self.s.run('geck_console_status', {'operation_id': operation_id}, action, editor=False, lock_root=lock_root(self.p))

    def archive(self, cp):
        p = paths(self.p)
        folder = self.p.state_dir / 'console/operations' / cp['id']
        folder.mkdir(parents=True, exist_ok=True)
        for suffix in ('.request', '.claimed', '.result.json'):
            if p[suffix].exists():
                target = folder / p[suffix].name
                if target.exists():
                    if target.read_bytes() != p[suffix].read_bytes():
                        raise ValueError('Console evidence archive differs; review before release')
                    p[suffix].replace(folder / (p[suffix].name + '.duplicate-' + uuid.uuid4().hex))
                else:
                    p[suffix].replace(target)
        write_json(folder / 'operation.json', cp)

    def release(self, operation_id, note):
        def action():
            if not isinstance(note, str) or len(note.strip()) < 12:
                raise ValueError('Describe the explicit uncertainty review before release')
            runtime.game_closed(self.p)
            cp = self.read(operation_id)
            held = reservation(lock_root(self.p))
            if held != self.owner(operation_id):
                raise ValueError('This command does not own the runtime reservation')
            cp.update(terminal=True, outcome='outcome_unknown', phase='released_after_game_exit', review=note)
            self.write(cp)
            self.archive(cp)
            self.write(cp)
            reservation(lock_root(self.p), self.owner(operation_id), release=True)
            return {**cp, '_outcome': 'outcome_unknown', 'next': 'Historical outcome remains unknown; this command will never be replayed.'}
        return self.s.run('geck_console_release', {'operation_id': operation_id, 'note': note}, action,
                          editor=False, lock_root=lock_root(self.p))
