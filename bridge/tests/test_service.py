from contextlib import closing
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from bridge_lock import bottle_lock
from geck_mcp.config import Project, model_asset
from geck_mcp.service import Service
from geck_mcp.transport import BridgeError
from esp_check import file_snapshot


# Windows only (decision D5): the native launcher is the only one. NATIVE stays for older imports.
NATIVE = True


def host_launcher(root):
    return {'launcher': 'native'}


def editor_executable(project):
    return str(project.game_root / 'GECK.exe')


def plugin(model='fixture\\old.nif'):
    def field(tag, value):
        return tag + struct.pack('<H', len(value)) + value
    def record(tag, body, identity=0):
        return struct.pack('<4sIIIIHH', tag, len(body), 0, identity, 0, 15, 0) + body
    body = field(b'EDID', b'FixtureStatic\0') + field(b'MODL', model.encode() + b'\0')
    stat = record(b'STAT', body, 0x01000ADD)
    return record(b'TES4', field(b'MAST', b'FalloutNV.esm\0') + field(b'DATA', b'\0'*8)) + struct.pack('<4sI4sI8s', b'GRUP', 24 + len(stat), b'STAT', 0, b'\0'*8) + stat


class FakeTransport:
    def __init__(self, project):
        self.project, self.calls = project, []
        self.model, self.dirty, self.dialog = 'fixture\\old.nif', False, False
        self.fail_set = False
        self.session = '100:123'
        self.loaded = True             # False: GECK shows no plugin until data.load

    def call(self, operation, *args, session=None, expected_model=None):
        self.calls.append(operation)
        if session and session != self.session:
            raise BridgeError('STALE_SESSION', 'Editor restarted')
        if operation == 'status':
            return dict(sessionId=self.session,
                        executable=editor_executable(self.project),
                        editorTitle=(f'Garden of Eden Creation Kit - [{self.project.plugin}]' if self.loaded
                                     else 'Garden of Eden Creation Kit') + ('*' if self.dirty else ''),
                        unsavedChanges=self.dirty,
                        windows=[{'class': '#32770', 'text': 'Static'}] if self.dialog else [])
        if operation == 'objects.find':
            return {'found': True}
        if operation == 'windows.show':
            return {'shown': [], 'allVisible': True}
        if operation == 'data.load':
            self.loaded = True
            return {'loadDispatched': True}
        if operation == 'object.read':
            self.dialog = True
            return {'modelPath': self.model}
        if operation == 'object.cancel':
            self.dialog = False
        if operation == 'object.setModel':
            self.model, self.dirty = args[1], True
            if self.fail_set:
                raise BridgeError('EDITOR_UNRESPONSIVE', 'Readback timed out after mutation')
            return {'committed': True, 'reopenedVerified': True}
        if operation == 'plugin.save':
            self.project.plugin_path.write_bytes(plugin(self.model))
            self.dirty = False
            return {'fileWriteObserved': True}
        return {}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.p = Project(project_id='test', **host_launcher(root),
                         game_root=root/'game', plugin='Fixture.esp', state_dir=root/'state',
                         helper=root/'helper', records=[dict(editor_id='FixtureStatic', form_id='01000ADD')])
        mesh = self.p.game_root/'Data/meshes/fixture'
        mesh.mkdir(parents=True)
        (mesh/'new.nif').write_bytes(b'fixture')
        (mesh/'old.nif').write_bytes(b'fixture')
        self.p.plugin_path.write_bytes(plugin())
        self.transport = FakeTransport(self.p)
        self.service = Service(self.p, self.transport)
        self.hash = file_snapshot(self.p.plugin_path)[0]['sha256']
        # Most tests reload the already-shown plugin; trust its completion at once (see the reload tests below).
        self.settle = patch('geck_mcp.service.RELOAD_SETTLE_SECONDS', 0)
        self.settle.start()

    def tearDown(self):
        self.settle.stop()
        self.tmp.cleanup()

    def edit(self, key='edit-1', old='fixture\\old.nif', new='fixture\\new.nif', session='100:123'):
        return self.service.set_model('FixtureStatic', new, old, self.hash, session, key)

    def test_set_then_save_and_independent_disk_read(self):
        load = self.load('save-precondition')
        self.service.load_status(load['operationId'])
        result = self.edit()
        self.assertEqual(result['outcome'], 'committed_in_editor')
        self.assertEqual(file_snapshot(self.p.plugin_path)[0]['sha256'], self.hash)
        self.assertFalse(self.transport.dialog)
        saved = self.service.save({'FixtureStatic':'fixture\\new.nif'}, self.hash, '100:123', 'save-1')
        self.assertTrue(saved['ok'], saved)
        self.assertEqual(saved['outcome'], 'saved_verified')
        self.assertEqual(Path(saved['data']['backup']).read_bytes(), plugin())
        self.assertTrue(self.service.validate({'FixtureStatic':'fixture\\new.nif'})['ok'])

    def test_idempotency_survives_service_restart(self):
        original = self.edit()
        calls = len(self.transport.calls)
        self.service = Service(self.p, self.transport)
        repeated = self.edit()
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(repeated['operationId'], original['operationId'])
        self.assertTrue(repeated['replayed'])

    def test_conflicting_key_does_not_dispatch(self):
        self.edit()
        calls = len(self.transport.calls)
        result = self.edit(old='different.nif')
        self.assertEqual(result['error']['code'], 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(len(self.transport.calls), calls)

    def test_historical_replay_survives_asset_removal(self):
        first = self.edit()
        (self.p.game_root/'Data/meshes/fixture/new.nif').unlink()
        repeated = self.edit()
        self.assertTrue(repeated['ok'])
        self.assertTrue(repeated['replayed'])
        self.assertEqual(first['operationId'], repeated['operationId'])

    def test_already_correct_is_success_without_mutation(self):
        result = self.edit(new='fixture\\old.nif')
        self.assertTrue(result['ok'])
        self.assertFalse(result['data']['changed'])
        self.assertNotIn('object.setModel', self.transport.calls)

    def test_partial_failure_blocks_following_work(self):
        self.transport.fail_set = True
        failed = self.edit()
        self.assertEqual(failed['outcome'], 'outcome_unknown')
        result = self.service.read('FixtureStatic', 'editor', '100:123')
        self.assertEqual(result['error']['code'], 'RECOVERY_REQUIRED')
        self.assertTrue(self.service.read('FixtureStatic', 'disk')['ok'])
        self.assertTrue(self.service.status()['ok'])

    def test_explicit_recovery_checks_hash_and_dialogs(self):
        self.transport.fail_set = True
        failed = self.edit()
        result = self.service.acknowledge(failed['operationId'], '100:123', self.hash, 'Reviewed edit and disk')
        self.assertFalse(result['ok'])
        self.transport.dialog = False
        result = self.service.acknowledge(failed['operationId'], '100:123', self.hash, 'Reviewed edit and disk')
        self.assertTrue(result['ok'], result)
        self.assertTrue(self.service.read('FixtureStatic', 'editor', '100:123')['ok'])

    def test_restarted_session_never_edits(self):
        result = self.edit(session='99:456')
        self.assertFalse(result['ok'])
        self.assertNotIn('object.setModel', self.transport.calls)

    def test_external_disk_change_never_edits(self):
        self.p.plugin_path.write_bytes(plugin('fixture\\external.nif'))
        result = self.edit()
        self.assertEqual(result['error']['code'], 'STALE_STATE')
        self.assertNotIn('object.setModel', self.transport.calls)

    def test_unrelated_dialog_is_not_closed(self):
        self.transport.dialog = True
        result = self.service.read('FixtureStatic', 'editor', '100:123')
        self.assertEqual(result['error']['code'], 'MODAL_BLOCKED')
        self.assertNotIn('object.cancel', self.transport.calls)

    def test_file_identity_mismatch(self):
        self.p.plugin_path.write_bytes(plugin().replace(b'\xdd\x0a\x00\x01', b'\xee\x0a\x00\x01'))
        result = self.service.read('FixtureStatic', 'disk')
        self.assertEqual(result['error']['code'], 'IDENTITY_MISMATCH')

    def test_path_escape_and_symlink(self):
        for bad in ['..\\escape.nif', 'C:\\escape.nif', '\\escape.nif', 'meshes\\a.nif', 'a/../b.nif']:
            with self.assertRaises(ValueError):
                model_asset(self.p, bad)
        outside = Path(self.tmp.name)/'outside'
        outside.mkdir()
        try:
            (self.p.game_root/'Data/meshes/link').symlink_to(outside)
        except OSError as error:      # Windows without Developer Mode / admin: no symlinks
            self.skipTest('cannot create symlinks here: %s' % error)
        with self.assertRaises(ValueError):
            model_asset(self.p, 'link\\outside.nif')

    def test_lock_contention_returns_busy(self):
        with bottle_lock(self.p.lock_root):
            result = self.service.status()
        self.assertFalse(result['ok'])
        self.assertIn('EDITOR_BUSY', result['error']['message'])
        self.assertEqual(self.transport.calls, [])

    def test_interrupted_journal_survives_restart(self):
        with closing(self.service.db()) as db, db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)',
                       ('interrupted','test',str(self.p.lock_root),'edit','key','hash','running',None,0,None))
        service = Service(self.p, self.transport)
        self.assertEqual(service.attach('100:123')['error']['code'], 'RECOVERY_REQUIRED')

    def test_interrupted_replay_is_error_with_full_envelope(self):
        import hashlib
        args = {'value': 1}
        fingerprint = hashlib.sha256(json.dumps(['edit', args], sort_keys=True).encode()).hexdigest()
        with closing(self.service.db()) as db, db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)',
                       ('interrupted','test',str(self.p.lock_root),'edit','key',fingerprint,'running',None,0,None))
        result = self.service.run('edit', args, lambda: self.fail('must not redispatch'), 'key', True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'OUTCOME_UNKNOWN')
        self.assertTrue(result['replayed'])

    def test_step_evidence_survives_partial_mutation(self):
        self.transport.fail_set = True
        result = self.edit()
        evidence = self.service.operation_evidence(result['operationId'])
        self.assertEqual(evidence['steps'][-1]['operation'], 'object.setModel')
        self.assertEqual(evidence['steps'][-1]['state'], 'failed')
        self.assertTrue(any(s['operation'] == 'object.read' and s['state'] == 'verified'
                            for s in evidence['steps']))

    def test_save_uses_latest_dirty_state(self):
        load = self.load('save-precondition')
        self.service.load_status(load['operationId'])
        original = self.transport.call
        def call(operation, *args, **kwargs):
            result = original(operation, *args, **kwargs)
            if operation == 'object.cancel':
                self.transport.dirty = True
            return result
        self.transport.call = call
        result = self.service.save({'FixtureStatic': 'fixture\\old.nif'}, self.hash, '100:123', 'save-drift')
        self.assertTrue(result['ok'], result)
        self.assertIn('plugin.save', self.transport.calls)

    def load(self, key='load-test'):
        return self.service.load_plugin('100:123', 'Garden of Eden Creation Kit - [Fixture.esp]',
                                        self.hash, key, reload=True)

    def test_load_dispatch_and_completion_clear_barrier(self):
        result = self.load()
        self.assertEqual(result['outcome'], 'loading', result)
        self.assertEqual(self.service.attach('100:123')['error']['code'], 'EDITOR_RESERVED')
        done = self.service.load_status(result['operationId'])
        self.assertTrue(done['ok'], done)
        self.assertFalse(done['data']['loading'])
        self.assertEqual(done['data']['load']['outcome'], 'loaded_verified')
        self.assertTrue(self.service.attach('100:123')['ok'])
        replay = self.load()
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.transport.calls.count('data.load'), 1)

    def test_load_reservation_blocks_another_project_until_complete(self):
        result = self.load()
        other = self.p.model_copy(update={'project_id':'other'})
        competing = Service(other, self.transport).load_plugin('100:123',
            'Garden of Eden Creation Kit - [Fixture.esp]', self.hash, 'compete', reload=True)
        self.assertFalse(competing['ok'])
        self.assertEqual(competing['error']['code'], 'EDITOR_RESERVED')
        self.assertEqual(self.transport.calls.count('data.load'), 1)
        self.assertTrue(self.service.load_status(result['operationId'])['ok'])

    def test_external_plugin_change_requires_review_without_redispatch(self):
        result = self.load()
        original = self.transport.call
        def changed(op, *args, **kw):
            value = original(op, *args, **kw)
            if op == 'status': value['editorTitle'] = 'Garden of Eden Creation Kit - [External.esp]'
            return value
        self.transport.call = changed
        status = self.service.load_status(result['operationId'])
        self.assertFalse(status['ok'])
        self.assertEqual(status['error']['code'], 'LOAD_CONTEXT_CHANGED')
        self.assertEqual(self.transport.calls.count('data.load'), 1)

    def test_load_deadline_is_reviewable_while_unresponsive(self):
        result = self.load()
        original = self.transport.call
        def busy(op, *args, **kw):
            if op == 'status': raise BridgeError('EDITOR_UNRESPONSIVE', 'busy')
            return original(op, *args, **kw)
        self.transport.call = busy
        with patch('geck_mcp.service.time.time', return_value=10**12):
            status = self.service.load_status(result['operationId'])
        self.assertFalse(status['ok'])
        self.assertEqual(status['error']['code'], 'LOAD_REVIEW_REQUIRED')

    def test_load_refuses_unsaved_changes(self):
        self.transport.dirty = True
        result = self.service.load_plugin('100:123','Garden of Eden Creation Kit - [Fixture.esp]*',
                                          self.hash, 'load-dirty', True)
        self.assertEqual(result['error']['code'], 'UNSAVED_CHANGES')
        self.assertNotIn('data.open.inspect', self.transport.calls)

    def test_load_rejects_extra_master_before_dialog(self):
        data = plugin().replace(b'FalloutNV.esm', b'OtherName.esm')
        self.p.plugin_path.write_bytes(data)
        self.hash = file_snapshot(self.p.plugin_path)[0]['sha256']
        result = self.load()
        self.assertEqual(result['error']['code'], 'UNSUPPORTED_MASTERS')
        self.assertNotIn('data.open.inspect', self.transport.calls)

    def test_loading_timeout_preserves_pending_operation(self):
        result = self.load()
        original = self.transport.call
        def call(operation, *args, **kwargs):
            if operation == 'status':
                raise BridgeError('EDITOR_UNRESPONSIVE', 'still loading')
            return original(operation, *args, **kwargs)
        self.transport.call = call
        pending = self.service.load_status(result['operationId'])
        self.assertTrue(pending['ok'])
        self.assertTrue(pending['data']['loading'])
        self.assertEqual(self.service.operation(result['operationId'])['outcome'], 'loading')

    def test_load_status_rejects_restart(self):
        result = self.load()
        self.transport.session = '101:456'
        checked = self.service.load_status(result['operationId'])
        self.assertEqual(checked['error']['code'], 'STALE_SESSION')
        self.assertEqual(self.service.operation(result['operationId'])['outcome'], 'loading')
        # GECK restarted, so that load's barrier is gone (fix 2) and new work is not blocked.
        self.assertEqual(self.service.operation_evidence(result['operationId'])['journalState'], 'stale')
        self.assertEqual(checked['data']['released']['operationId'], result['operationId'])
        self.assertTrue(self.service.attach('101:456')['ok'])

    def test_reviewed_load_cannot_resume_polling(self):
        result = self.load()
        reviewed = self.service.acknowledge(result['operationId'], '100:123', self.hash, 'Reviewed interrupted load')
        self.assertTrue(reviewed['ok'], reviewed)
        checked = self.service.load_status(result['operationId'])
        self.assertEqual(checked['error']['code'], 'RECOVERY_ACKNOWLEDGED')

    def test_loaded_editor_must_match_disk(self):
        result = self.load()
        self.transport.model = 'fixture\\unexpected.nif'
        checked = self.service.load_status(result['operationId'])
        self.assertEqual(checked['error']['code'], 'STALE_STATE')
        self.assertFalse(self.transport.dialog)
        self.assertEqual(self.service.operation(result['operationId'])['outcome'], 'loading')

    def test_interrupted_load_recovery_allows_empty_editor(self):
        result = self.load()
        original = self.transport.call
        def call(operation, *args, **kwargs):
            value = original(operation, *args, **kwargs)
            if operation == 'status':
                value['editorTitle'] = 'Garden of Eden Creation Kit'
            return value
        self.transport.call = call
        reviewed = self.service.acknowledge(result['operationId'], '100:123', self.hash, 'Reviewed clean empty editor')
        self.assertTrue(reviewed['ok'], reviewed)

    # -- 0.2.0: optional editor session (fix 3) -------------------------------------------
    def test_session_can_be_omitted_and_is_adopted(self):
        load = self.load('save-precondition')
        self.service.load_status(load['operationId'])
        attached = self.service.attach(None)
        self.assertTrue(attached['ok'], attached)
        result = self.service.set_model('FixtureStatic', 'fixture\\new.nif', 'fixture\\old.nif', self.hash, None, 'k1')
        self.assertEqual(result['outcome'], 'committed_in_editor', result)
        steps = self.service.operation_evidence(result['operationId'])['steps']
        # After the first status call every helper call is guarded by the adopted session.
        self.assertEqual(steps[0]['operation'], 'status')
        self.assertIn('setModel', ' '.join(s['operation'] for s in steps))
        saved = self.service.save({'FixtureStatic': 'fixture\\new.nif'}, self.hash, None, 'save-x')
        self.assertTrue(saved['ok'], saved)

    def test_explicit_stale_session_is_still_refused(self):
        self.assertEqual(self.service.attach('999:1')['error']['code'], 'STALE_SESSION')

    def test_load_without_session(self):
        result = self.service.load_plugin(None, 'Garden of Eden Creation Kit - [Fixture.esp]', self.hash,
                                          'load-nosession', reload=True)
        self.assertEqual(result['outcome'], 'loading', result)
        self.assertEqual(result['data']['sessionId'], '100:123')

    # -- 0.2.0: stale recovery barriers (fix 2) ------------------------------------------
    def test_barrier_from_earlier_editor_session_is_released(self):
        self.transport.fail_set = True
        failed = self.edit()
        self.assertEqual(failed['outcome'], 'outcome_unknown')
        self.assertEqual(self.service.attach(None)['error']['code'], 'RECOVERY_REQUIRED')
        self.transport.session, self.transport.dialog = '101:456', False     # GECK restarted
        attached = self.service.attach(None)
        self.assertTrue(attached['ok'], attached)
        released = attached['data']['releasedStaleOperations']
        self.assertEqual([r['operationId'] for r in released], [failed['operationId']])
        self.assertEqual(released[0]['operationSession'], '100:123')
        evidence = self.service.operation_evidence(failed['operationId'])
        self.assertEqual(evidence['journalState'], 'stale')
        self.assertEqual(evidence['recoveryAck']['auto'], 'stale')

    def test_barrier_with_unknown_session_is_kept(self):
        with closing(self.service.db()) as db, db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)',
                       ('mystery', 'test', str(self.p.lock_root), 'edit', None, 'h', 'running', None, 0, None))
        self.transport.session = '101:456'
        result = self.service.attach(None)
        self.assertEqual(result['error']['code'], 'RECOVERY_REQUIRED')
        self.assertEqual(result['data']['unresolved'][0]['operationId'], 'mystery')

    def test_release_stale_needs_a_running_editor(self):
        self.transport.fail_set = True
        failed = self.edit()
        original = self.transport.call
        def call(operation, *args, **kwargs):
            if operation == 'status':
                raise BridgeError('EDITOR_ERROR', 'expected exactly one GECK process; found 0')
            return original(operation, *args, **kwargs)
        self.transport.call = call
        released, blocking = self.service.release_stale()
        self.assertEqual(released, [])
        self.assertEqual([r['id'] for r in blocking], [failed['operationId']])

    # -- 0.2.0: hidden windows after a load (fix 1) ---------------------------------------
    def test_load_status_shows_windows_then_verifies(self):
        result = self.load()
        done = self.service.load_status(result['operationId'])
        self.assertEqual(done['data']['load']['outcome'], 'loaded_verified', done)
        self.assertIn('windows.show', self.transport.calls)
        self.assertLess(self.transport.calls.index('windows.show'), self.transport.calls.index('object.read'))

    def test_hidden_cell_view_gives_loaded_unverified_and_no_barrier(self):
        result = self.load()
        original = self.transport.call
        def call(operation, *args, **kwargs):
            if operation == 'objects.find':
                raise BridgeError('EDITOR_ERROR', 'object list selector matched 0 controls')
            return original(operation, *args, **kwargs)
        self.transport.call = call
        done = self.service.load_status(result['operationId'])
        self.assertTrue(done['ok'], done)
        load = done['data']['load']
        self.assertEqual(load['outcome'], 'loaded_unverified')
        self.assertFalse(load['data']['verification']['ok'])
        self.assertIn('selector matched 0', load['data']['verification']['skipped']['message'])
        self.assertEqual(self.service.operation_evidence(result['operationId'])['journalState'], 'loaded_unverified')
        self.transport.call = original
        self.assertTrue(self.service.attach(None)['ok'])            # no barrier left behind
        self.assertEqual(self.service.loaded_plugin('100:123'), self.hash)

    def test_unresponsive_during_verification_keeps_polling(self):
        result = self.load()
        original = self.transport.call
        def call(operation, *args, **kwargs):
            if operation == 'objects.find':
                raise BridgeError('EDITOR_UNRESPONSIVE', 'message timed out or failed')
            return original(operation, *args, **kwargs)
        self.transport.call = call
        pending = self.service.load_status(result['operationId'])
        self.assertTrue(pending['data']['loading'], pending)
        self.assertEqual(self.service.operation(result['operationId'])['outcome'], 'loading')

    def test_show_windows_is_allowed_during_recovery(self):
        self.transport.fail_set = True
        self.edit()
        shown = self.service.show_windows(None)
        self.assertTrue(shown['ok'], shown)
        self.assertEqual(self.transport.calls[-1], 'windows.show')

    def test_hidden_retained_dialog_does_not_block_attach(self):
        original = self.transport.call
        def hidden(op, *args, **kw):
            result = original(op, *args, **kw)
            if op == 'status': result['windows'] = [{'class':'#32770', 'text':'Script Edit [Old *]', 'visible':False}]
            return result
        self.transport.call = hidden
        self.assertTrue(self.service.attach()['ok'])

    def test_status_reports_hidden_render_window(self):
        original = self.transport.call
        def call(operation, *args, **kwargs):
            value = original(operation, *args, **kwargs)
            if operation == 'status':
                value = dict(value, editorWindows={
                    'objectWindow': {'found': True, 'visible': True, 'reachable': True},
                    'cellView': {'found': True, 'visible': True, 'reachable': True},
                    'renderWindow': {'found': True, 'visible': False, 'reachable': False}})
            return value
        self.transport.call = call
        status = self.service.status()['data']
        self.assertFalse(status['renderWindowReachable'])
        self.assertEqual(status['windowsVisible'], {'objectWindow': True, 'cellView': True, 'renderWindow': False})
        self.assertIn('geck_show_windows', status['hint'])

    def test_loaded_plugin_is_per_session(self):
        result = self.load()
        self.service.load_status(result['operationId'])
        self.assertEqual(self.service.loaded_plugin('100:123'), self.hash)
        self.assertIsNone(self.service.loaded_plugin('101:456'))

    def failing_data_load(self, cancel_ok=True, uncertain=False):
        original = self.transport.call
        def call(operation, *args, **kwargs):
            if operation == 'data.load':
                if uncertain:
                    raise BridgeError('OUTCOME_UNKNOWN', 'Helper exceeded deadline; inspect before retry', uncertain=True)
                raise BridgeError('EDITOR_ERROR', 'Data row toggle did not change actual image state')
            if operation == 'data.cancel' and not cancel_ok:
                raise BridgeError('EDITOR_ERROR', 'Data dialog selector matched 0 windows')
            return original(operation, *args, **kwargs)
        self.transport.call = call

    def test_load_failing_before_ok_cancels_and_leaves_no_barrier(self):
        # Job 049 (5 Oct): a Data checkbox toggle failed; the dialog was left open and a barrier stayed behind.
        self.failing_data_load()
        result = self.load('load-toggle')
        self.assertEqual(result['outcome'], 'failed_before_change', result)
        self.assertIn('nothing loaded', result['error']['message'])
        self.assertIn('data.cancel', self.transport.calls)
        self.assertTrue(self.service.attach(None)['ok'])

    def test_load_failure_with_failed_cancel_keeps_barrier(self):
        self.failing_data_load(cancel_ok=False)
        result = self.load('load-nocancel')
        self.assertEqual(result['outcome'], 'outcome_unknown')
        self.assertEqual(self.service.attach(None)['error']['code'], 'EDITOR_RESERVED')

    def test_load_timeout_is_never_cancelled(self):
        self.failing_data_load(uncertain=True)
        result = self.load('load-timeout')
        self.assertEqual(result['outcome'], 'outcome_unknown')
        self.assertNotIn('data.cancel', self.transport.calls)

    # -- reload completion (job 051): the title names the plugin before and after a reload -------------
    def test_reload_is_not_trusted_before_it_is_seen(self):
        self.settle.stop()
        self.settle = patch('geck_mcp.service.RELOAD_SETTLE_SECONDS', 45)
        self.settle.start()
        result = self.load('reload-1')
        self.assertTrue(result['data']['reload'])
        first = self.service.load_status(result['operationId'])
        self.assertTrue(first['data']['loading'], first)
        self.assertIn('reload dispatched', first['data']['lastObservation'])
        self.transport.loaded = False                   # GECK is reloading: plain title
        second = self.service.load_status(result['operationId'])
        self.assertTrue(second['data']['loading'])
        self.assertTrue(self.service.operation(result['operationId'])['data']['loadingObserved'])
        self.transport.loaded = True
        done = self.service.load_status(result['operationId'])
        self.assertEqual(done['data']['load']['outcome'], 'loaded_verified', done)

    def test_reload_trusted_after_settle_time(self):
        self.settle.stop()
        self.settle = patch('geck_mcp.service.RELOAD_SETTLE_SECONDS', 45)
        self.settle.start()
        result = self.load('reload-2')
        with patch('geck_mcp.service.time.time', return_value=result['data']['dispatchedAt'] + 46):
            done = self.service.load_status(result['operationId'])
        self.assertEqual(done['data']['load']['outcome'], 'loaded_verified', done)

    def test_first_load_is_trusted_when_the_title_appears(self):
        self.settle.stop()
        self.settle = patch('geck_mcp.service.RELOAD_SETTLE_SECONDS', 45)
        self.settle.start()
        self.transport.loaded = False
        result = self.service.load_plugin(None, 'Garden of Eden Creation Kit', self.hash, 'first-load')
        self.assertFalse(result['data']['reload'], result)
        done = self.service.load_status(result['operationId'])
        self.assertEqual(done['data']['load']['outcome'], 'loaded_verified', done)

if __name__ == '__main__':
    unittest.main()
