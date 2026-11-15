"""Console actuator fault injection and actual DLL under a synthetic xNVSE host."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bridge_lock import bottle_lock, reservation
from geck_mcp import console as c
from geck_mcp.config import Project
from geck_mcp.runtime import file_digest
from geck_mcp.service import Service
from geck_mcp.esp import codec
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/: engine_host
from engine_host import needs_zig, zig_path  # noqa: E402,F401


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.p = Project(project_id='console-fixture', launcher='native', game_root=root / 'game',
                         plugin='Fixture.esp', state_dir=root / 'state', helper=root / 'unused.exe',
                         records=[{'editor_id': 'Fixture', 'form_id': '01000800'}])
        self.paths = c.paths(self.p)
        self.paths['.dll'].parent.mkdir(parents=True)
        self.p.plugin_path.write_bytes(b'console fixture plugin')
        self.paths['.dll'].write_bytes(b'synthetic console dll')
        self.paths['.context'].write_bytes(b'a' * 32)
        self.hash = file_digest(self.p.plugin_path)
        self.receipt = {'wire_version':2, 'source':{name:file_digest(c.NATIVE/name) for name in
                         ('console_companion.c','console_protocol.h','nvse_abi.h')}, 'project_id': self.p.project_id, 'plugin': self.p.plugin,
                        'plugin_sha256': self.hash, 'build_id': 'b' * 64,
                        'game_root': str(self.p.game_root.resolve()), 'target': str(self.paths['.dll'].resolve()),
                        'sha256': file_digest(self.paths['.dll'])}
        c.write_json(self.paths['receipt'], self.receipt)
        self.doc = {'schema_version': 1, 'adapter': 'geck-console-1', 'pid': 123, 'started': '456',
                    'epoch': 1, 'tick_ms': 1000, 'sequence': 2, 'build_id': 'b' * 64,
                    'context': 'a' * 32, 'runtime_prefix': 17, 'plugin': self.p.plugin,
                    'plugin_sha256': self.hash, 'status': 'ready', 'console_api': 3, 'wire_version': 2}
        c.write_json(self.paths['.ready.json'], self.doc)
        self.target = {key: self.doc[key] for key in c.TARGET_KEYS}
        self.process = patch.object(c.runtime, 'process_identity', return_value=456);self.process.start();self.addCleanup(self.process.stop)
        self.tick = patch.object(c.runtime, 'ticks', return_value=1100);self.tick.start();self.addCleanup(self.tick.stop)
        self.service = Service(self.p, transport=object())

    def execute(self, key='command-one', **kw):
        args = dict(command='startquest {prefix}000800', intent='Start disposable test quest', target=self.target,
                    expected_plugin_hash=self.hash, request_key=key, verify={'kind': 'quest_running', 'local_form': '000800', 'equals': 1})
        args.update(kw)
        return self.service.console_execute(**args)

    def result(self, op, status='effect_verified', **kw):
        cp = c.Console(self.service).read(op)
        receipt = {'schema_version': 1, 'id': cp['id'], 'pid': 123, 'started': '456', 'epoch': 1,
                   'build_id': 'b' * 64, 'context': 'a' * 32, 'status': status,
                   'sent': True, 'acknowledged': True, 'effectVerified': True, 'value': 1}
        receipt.update(kw)
        self.paths['.request'].replace(self.paths['.claimed'])
        c.write_json(self.paths['.result.json'], receipt)

    def test_readiness_missing_stale_wrong_build_and_context(self):
        self.assertTrue(c.readiness(self.p, self.hash)['ready'])
        for key, value in [('tick_ms', 999999), ('started', 457), ('build_id', 'z' * 64), ('epoch', 0),
                           ('context', 'z' * 32), ('runtime_prefix', 255), ('console_api', 1), ('status', 'no_loaded_game')]:
            with self.subTest(key=key):
                c.write_json(self.paths['.ready.json'], {**self.doc, key: value})
                self.assertFalse(c.readiness(self.p, self.hash)['ready'])

    def test_preflight_does_not_install_or_dispatch(self):
        before = {str(p): p.read_bytes() for p in self.p.game_root.rglob('*') if p.is_file()}
        result = c.preflight(self.p)
        self.assertTrue(result['optIn'])
        self.assertFalse(result['arbitraryConsoleOutputCapture'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.p.game_root.rglob('*') if p.is_file()})

    def test_wire_binding_prefix_and_effect_separation(self):
        result = self.execute()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['outcome'], 'pending')
        self.assertFalse(result['data']['acknowledged'])
        self.assertIsNone(result['data']['effectVerified'])
        payload = c.WIRE.unpack(self.paths['.request'].read_bytes())
        self.assertEqual(c.WIRE.size, 940)
        self.assertEqual(payload[4:9], (123, 456, 1, 11100, 17))
        self.assertEqual(payload[9].rstrip(b'\0'), b'startquest 11000800')
        self.assertEqual(payload[10:12], (1, 0x800))

    def test_single_line_and_world_teardown_restrictions(self):
        for line in ('', 'qqq', 'load MySave', 'con_LoadGame MySave', 'bat foo', 'coc Test',
                     'tgm\nqqq', 'tgm;qqq', 'tgm\x00', 'startquest 0A000800', 'x' * 513):
            with self.subTest(line=line), self.assertRaises(ValueError): c.prepare(line, self.target)
        for line in ('tgm', 'player.additem 0000000F 1', 'GetModIndex "Fixture.esp"'):
            self.assertEqual(c.prepare(line, self.target)[0], line)

    def test_wrong_identity_never_dispatches(self):
        target = {**self.target, 'started': 457}
        result = self.execute(target=target)
        self.assertFalse(result['ok'])
        self.assertFalse(self.paths['.request'].exists())

    def test_uncertain_queue_is_not_replayed_after_client_restart(self):
        result = self.execute(); op = result['operationId']
        payload = self.paths['.request'].read_bytes()
        self.service = Service(self.p, transport=object())
        replay = self.execute()
        self.assertTrue(replay['replayed']);self.assertEqual(replay['operationId'], op)
        self.assertEqual(self.paths['.request'].read_bytes(), payload)
        with patch.object(c.runtime, 'ticks', return_value=999999):
            status = self.service.console_status(op)
        self.assertEqual(status['outcome'], 'outcome_unknown')
        self.assertFalse(status['data']['acknowledged'])
        self.assertEqual(self.paths['.request'].read_bytes(), payload)

    def test_competing_profiles_and_commands_are_blocked(self):
        result = self.execute()
        other = self.p.model_copy(update={'project_id': 'other', 'state_dir': self.p.state_dir.parent / 'other'})
        # Use the same live proof, then test shared reservation rather than a missing installation.
        with patch.object(c, 'live', return_value=self.doc):
            competing = Service(other, transport=object()).console_execute('tgm', 'Explicit test toggle', self.target, self.hash, 'other-key')
        self.assertFalse(competing['ok'])
        self.assertEqual(competing['error']['code'], 'CONSOLE_BUSY')
        self.assertEqual(c.Console(self.service).read(result['operationId'])['phase'], 'sent')

    def test_engine_acceptance_without_readback_is_not_verified(self):
        result = self.execute(verify=None)
        self.result(result['operationId'], 'acknowledged', effectVerified=None, value=None)
        status = self.service.console_status(result['operationId'])
        self.assertTrue(status['data']['acknowledged'])
        self.assertIsNone(status['data']['effectVerified'])
        self.assertNotEqual(status['outcome'], 'verified')

    def test_native_receipt_effect_proof_and_owned_archival(self):
        result = self.execute(); op = result['operationId']
        self.result(op)
        status = self.service.console_status(op)
        self.assertEqual(status['outcome'], 'verified', status)
        self.assertTrue(status['data']['effectVerified'])
        self.assertFalse(self.paths['.claimed'].exists())
        with bottle_lock(c.lock_root(self.p)): self.assertIsNone(reservation(c.lock_root(self.p)))
        self.assertEqual(self.service.console_status(op)['outcome'], 'verified')

    def test_mismatched_receipt_cannot_acknowledge(self):
        result = self.execute();self.result(result['operationId'], id='c' * 32)
        status = self.service.console_status(result['operationId'])
        self.assertFalse(status['ok'])
        self.assertEqual(status['error']['code'], 'CONSOLE_RECEIPT_MISMATCH')
        self.assertTrue(self.paths['.claimed'].exists())

    def test_interrupted_archival_preserves_duplicates_and_finishes(self):
        result = self.execute(); op = result['operationId']
        self.result(op)
        cp = c.Console(self.service).read(op)
        folder = self.p.state_dir / 'console/operations' / cp['id']
        folder.mkdir(parents=True)
        (folder / self.paths['.claimed'].name).write_bytes(self.paths['.claimed'].read_bytes())
        status = self.service.console_status(op)
        self.assertEqual(status['outcome'], 'verified', status)
        self.assertFalse(self.paths['.claimed'].exists())
        self.assertEqual(len(list(folder.glob('*.duplicate-*'))), 1)
        self.assertTrue(c.readiness(self.p, self.hash)['ready'])

    def test_install_requires_closed_game_and_preserves_owned_previous_files(self):
        source = self.p.state_dir / 'console/builds/test/new.dll'
        source.parent.mkdir(parents=True); source.write_bytes(b'new synthetic dll')
        build = {**self.receipt, 'schema_version': 1, 'dll': str(source), 'sha256': file_digest(source)}
        before = self.paths['.dll'].read_bytes()
        with patch.object(c.runtime, 'game_closed', side_effect=ValueError('running')):
            with self.assertRaises(ValueError): c.install(self.p, build, self.hash)
        self.assertEqual(self.paths['.dll'].read_bytes(), before)
        with patch.object(c.runtime, 'game_closed'):
            installed = c.install(self.p, build, self.hash)
        self.assertEqual(self.paths['.dll'].read_bytes(), source.read_bytes())
        self.assertEqual((Path(installed['backup']) / self.paths['.dll'].name).read_bytes(), before)
        self.paths['.dll'].write_bytes(b'foreign changed dll')
        with patch.object(c.runtime, 'game_closed'):
            with self.assertRaisesRegex(ValueError, 'unowned/modified'): c.install(self.p, build, self.hash)
        self.assertEqual(self.paths['.dll'].read_bytes(), b'foreign changed dll')

    def test_large_started_is_exact_decimal_string_and_rounded_target_is_refused(self):
        exact = 134372848633699989
        self.doc['started'] = str(exact)
        c.write_json(self.paths['.ready.json'], self.doc)
        with patch.object(c.runtime, 'process_identity', return_value=exact):
            ready = self.service.console_readiness(self.hash)
            self.assertEqual(ready['data']['target']['started'], str(exact))
            target = ready['data']['target']
            wrong = dict(target, started='134372848633699980')
            self.assertFalse(self.execute(key='rounded-identity', target=wrong)['ok'])
            result = self.execute(target=target)
            self.assertTrue(result['ok'], result)
            self.assertEqual(c.WIRE.unpack(self.paths['.request'].read_bytes())[5], exact)

    def binding(self):
        path = self.p.game_root / 'Data/Probe.esp';path.write_bytes(b'explicit other plugin')
        return {'plugin':'Probe.esp','local_form':'000800','sha256':file_digest(path)}

    def test_cross_plugin_raw_argument_becomes_native_resolved_binding(self):
        binding = self.binding()
        result = self.execute(command='player.AddItemHealthPercent 0C000800 1 0.66', binding=binding,
                              verify={'kind':'item_count','local_form':'000800','equals':6})
        self.assertTrue(result['ok'], result)
        wire = c.WIRE.unpack(self.paths['.request'].read_bytes())
        self.assertIn(b'{form}', wire[9])
        self.assertEqual(wire[15].rstrip(b'\0'), b'Probe.esp')
        self.assertEqual(wire[17], 0x800)

    def test_changed_named_plugin_refused_before_claim(self):
        binding = self.binding();binding['sha256'] = '0'*64
        self.assertFalse(self.execute(command='player.additem {form} 1', binding=binding)['ok'])
        self.assertFalse(self.paths['.request'].exists())

    def test_typed_inventory_query_uses_same_durable_claim(self):
        result = self.service.console_query('item_count','Read probe inventory count',self.target,self.hash,'count-1',self.binding())
        self.assertTrue(result['ok'], result)
        wire = c.WIRE.unpack(self.paths['.request'].read_bytes())
        self.assertEqual(wire[18], 3)
        repeated = self.service.console_query('item_count','Read probe inventory count',self.target,self.hash,'count-1',self.binding())
        self.assertTrue(repeated['replayed'])

    def test_coc_requires_explicit_intent_rotates_context_without_replay(self):
        self.assertFalse(self.execute(command='coc TestCell', verify=None)['ok'])
        binding = self.cell_binding()
        result = self.execute(key='teleport-1', command='coc TestCell', verify=None, binding=binding, authorize_teleport=True)
        self.assertTrue(result['ok'], result)
        cp = c.Console(self.service).read(result['operationId'])
        self.assertRegex(cp['nextContext'], r'^[a-f0-9]{32}$')
        document = json.loads((self.p.state_dir/'companion/save-context.json').read_text())
        self.assertEqual(document['id'], cp['nextContext'])
        self.assertEqual(cp['destinationVerifiedOnDisk']['editor_id'], 'TestCell')
        self.assertTrue(self.execute(key='teleport-1', command='coc TestCell', verify=None, binding=binding, authorize_teleport=True)['replayed'])

    def cell_binding(self, record_type='CELL', edid='TestCell', prefix=1):
        header = codec.Record('TES4', 0, _subrecords=[codec.Subrecord('MAST', b'FalloutNV.esm\0')])
        cell = codec.Record(record_type, prefix << 24 | 0x800, _subrecords=[codec.Subrecord('EDID', edid.encode()+b'\0')])
        path = self.p.game_root/'Data/Cells.esp'
        path.write_bytes(codec.write(codec.Plugin(header, [codec.Group(record_type.encode(), 0, children=[cell])])))
        return {'plugin':'Cells.esp', 'local_form':'000800', 'sha256':file_digest(path)}

    def test_coc_wrong_destination_identity_never_queues_or_rotates_context(self):
        for i, options in enumerate((None, {'record_type':'WEAP'}, {'edid':'DifferentCell'}, {'prefix':0})):
            binding = None if options is None else self.cell_binding(**options)
            result = self.execute(key='bad-cell-%d'%i, command='coc TestCell', verify=None,
                                  binding=binding, authorize_teleport=True)
            self.assertFalse(result['ok'], result)
            self.assertFalse(self.paths['.request'].exists())
            self.assertFalse((self.p.state_dir/'companion/save-context.json').exists())
            self.assertIsNone(reservation(c.lock_root(self.p)))

    def test_plugin_index_query_accepts_name_and_hash_without_local_form(self):
        binding = self.binding();binding.pop('local_form')
        result = self.service.console_query('plugin_index','Resolve explicitly named loaded plugin',
            self.target,self.hash,'plugin-index-only',binding)
        self.assertTrue(result['ok'], result)
        wire = c.WIRE.unpack(self.paths['.request'].read_bytes())
        self.assertEqual(wire[17:19], (0,2))

    def test_install_refuses_old_wire_receipt_before_replacement(self):
        source = self.p.state_dir/'console/builds/old/old.dll'
        source.parent.mkdir(parents=True);source.write_bytes(b'old build')
        receipt = dict(self.receipt, schema_version=1, wire_version=1, dll=str(source), sha256=file_digest(source))
        before = self.paths['.dll'].read_bytes()
        with patch.object(c.runtime,'game_closed'):
            with self.assertRaisesRegex(ValueError, 'older wire/source'):c.install(self.p,receipt,self.hash)
        self.assertEqual(self.paths['.dll'].read_bytes(),before)

    def test_install_setup_refuses_unowned_build_receipt_and_busy_game(self):
        missing = self.service.console_setup('install',self.hash,'foreign-receipt',build_receipt=str(self.p.plugin_path))
        self.assertFalse(missing['ok'])
        self.assertIn('inside this project console build state',missing['error']['message'])

    def test_readiness_states_and_preflight_restrictions(self):
        self.assertEqual(c.readiness(self.p,self.hash)['state'],'ready')
        c.write_json(self.paths['.ready.json'],dict(self.doc,status='no_loaded_game'))
        self.assertEqual(c.readiness(self.p,self.hash)['state'],'waiting_for_loaded_save')
        c.write_json(self.paths['.ready.json'],dict(self.doc,wire_version=1))
        self.assertEqual(c.readiness(self.p,self.hash)['state'],'restart_needed')
        self.assertEqual(c.preflight(self.p)['restrictions']['singleLine'],True)

    def test_explicit_save_context_invalidates_command_readiness(self):
        c.runtime.set_save_context(self.p, 'New explicit test save')
        self.assertNotEqual(self.paths['.context'].read_text(), self.doc['context'])
        self.assertFalse(c.readiness(self.p, self.hash)['ready'])

    def test_release_requires_game_exit_and_retains_unknown_outcome(self):
        result = self.execute();op = result['operationId']
        with patch.object(c.runtime, 'game_closed', side_effect=ValueError('FNV is still running')):
            self.assertFalse(self.service.console_release(op, 'Reviewed timed out command')['ok'])
        with patch.object(c.runtime, 'game_closed'):
            released = self.service.console_release(op, 'Reviewed unknown outcome after normal exit')
        self.assertEqual(released['outcome'], 'outcome_unknown')
        self.assertFalse(self.paths['.request'].exists())
        self.assertTrue(self.execute()['replayed'])


@unittest.skipUnless(os.name == 'nt' and zig_path(), 'requires Windows and Zig')
class NativeConsoleTests(unittest.TestCase):
    def test_actual_console_dll_identity_replay_acceptance_and_delayed_effect(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = Project(project_id='console-native', launcher='native', game_root=root / 'game',
                              plugin='Fixture.esp', state_dir=root / 'state', helper=root / 'unused',
                              records=[{'editor_id': 'Fixture', 'form_id': '01000800'}])
            project.plugin_path.parent.mkdir(parents=True)
            project.plugin_path.write_bytes(b'console fixture plugin')
            receipt = c.build(project, zig_path(), file_digest(project.plugin_path))
            dll = Path(receipt['dll']);host = dll.parent / 'console_fake_host.exe'
            for mode in range(30):
                with self.subTest(mode=mode):
                    scenario = root / ('case-%d' % mode)
                    (scenario / 'Data/NVSE/Plugins').mkdir(parents=True)
                    (scenario / 'Data/Fixture.esp').write_bytes(b'console fixture plugin')
                    run = subprocess.run([str(host), str(dll), str(mode)], cwd=scenario, capture_output=True, text=True, timeout=15)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    final = json.loads((scenario / 'final.json').read_text())
                    self.assertIsInstance(final['started'], str)
                    if mode == 0:
                        self.assertEqual(final['status'], 'effect_verified');self.assertTrue(final['effectVerified'])
                    elif mode in (13,):
                        self.assertEqual(final['status'],'acknowledged');self.assertEqual(final['subjectPrefix'],12)
                    elif mode in (14,15,20,25,26):
                        self.assertEqual(final['status'],'query_verified')
                        self.assertEqual(final['value'], {14:12,15:5,20:132,25:66,26:1}[mode])
                        self.assertFalse(final['sent'])
                    elif mode in (16,17):
                        self.assertEqual(final['status'],'effect_verified')
                    elif mode == 22:
                        self.assertEqual(final['status'],'effect_not_verified')
                    elif mode == 28:
                        self.assertEqual(final['status'],'verification_compile_error')
                        self.assertIn('player.GetParentCell', final['failedExpression'])
                        self.assertIn('"Other.esp"', final['failedExpression'])
                        self.assertEqual(final['compileFailure'], 'CompileExpression_returned_NULL')
                        self.assertFalse(final['sent'])
                    elif mode == 29:
                        self.assertEqual(final['status'],'invalid_verification')
                        self.assertFalse(final['sent'])
                    elif mode == 21:
                        self.assertEqual(final['status'],'invalid_query')
                    elif mode == 7:
                        self.assertEqual(final['status'], 'query_verified');self.assertEqual(final['value'], 17)
                    elif mode == 8:
                        self.assertEqual(final['status'], 'effect_not_verified');self.assertFalse(final['effectVerified'])
                    elif mode == 6:
                        self.assertEqual(final['status'], 'execution_unconfirmed');self.assertFalse(final['acknowledged'])
                    elif mode == 12:
                        self.assertEqual(final['status'], 'context_changed_after_dispatch');self.assertIsNone(final['effectVerified'])
                    else:
                        self.assertEqual(final['status'], 'rejected_before_dispatch');self.assertFalse(final['sent'])


class ConsoleMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_decimal_identity_typed_query_and_setup_discovery(self):
        from mcp import Client
        from geck_mcp.server import make_server
        fixture = ConsoleTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        exact = 134372848633699989
        fixture.doc['started'] = str(exact)
        c.write_json(fixture.paths['.ready.json'], fixture.doc)
        with patch.object(c.runtime, 'process_identity', return_value=exact):
            async with Client(make_server(fixture.p.model_copy(update={'legacy_tools': True}),
                                          fixture.service)) as client:
                result = await client.call_tool('geck_console_readiness', {'expected_plugin_hash':fixture.hash})
                target = result.structured_content['data']['target']
                self.assertEqual(target['started'], str(exact))
                self.assertIsInstance(target['started'], str)
                wrong = dict(target, started=134372848633699980)
                bad = await client.call_tool('geck_console_execute', {'command':'tgm','intent':'Explicit test',
                    'target':wrong,'expected_plugin_hash':fixture.hash,'request_key':'rounded-js-number'})
                self.assertTrue(bad.is_error)
                setup = await client.call_tool('geck_console_setup', {'mode':'preflight',
                    'expected_plugin_hash':fixture.hash,'request_key':'setup-preview'})
                self.assertFalse(setup.is_error, setup)
                self.assertIn('restrictions', setup.structured_content['data'])
                binding = fixture.binding()
                query = await client.call_tool('geck_console_query', {'kind':'item_count','intent':'Read probe count',
                    'target':target,'expected_plugin_hash':fixture.hash,'request_key':'typed-count','binding':binding})
                self.assertFalse(query.is_error, query)
                self.assertEqual(query.structured_content['outcome'], 'pending')
                self.assertEqual(query.structured_content['data']['target']['started'], str(exact))
