"""Synthetic console fixtures only; no game or editor is started by these tests."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from bridge_lock import bottle_lock
from geck_mcp.runtime import Profile, Mapping, Runtime, digest, plan, parse
from geck_mcp.server import make_server
import test_service


PROFILE = {'plugin': 'Fixture.esp', 'probes': [
    {'key': 'state', 'kind': 'quest_variable', 'local_form': '000801', 'variable': 'State'},
    {'key': 'count', 'kind': 'item_count', 'local_form': '000802'},
    {'key': 'disabled', 'kind': 'disabled', 'local_form': '000803'},
    {'key': 'dead', 'kind': 'dead', 'local_form': '000803'}],
    'rules': [{'key': 'state_ok', 'kind': 'equals', 'fields': ['state'], 'value': 3},
              {'key': 'single', 'kind': 'count_limit', 'fields': ['count'], 'maximum': 1},
              {'key': 'stable', 'kind': 'unchanged', 'fields': ['state']},
              {'key': 'bounded', 'kind': 'range', 'fields': ['state'], 'minimum': 0, 'maximum': 4},
              {'key': 'exclusive', 'kind': 'mutually_exclusive', 'fields': ['disabled', 'dead']}]}
MAPPING = {'prefix': '06', 'source': 'user_runtime_observation',
           'evidence': 'Synthetic current runtime inventory observation (06000802).'}


def output(program, count=1, state=3):
    """Model the vanilla echo/response grammar observed by the reference runner."""
    responses = {'sqv 06000801': f'->State = {state}\n',
                 'player.GetItemCount 06000802': f'GetItemCount >> {count}.00\n',
                 'GetDisabled': 'GetDisabled >> 1.00\n', 'GetDead': 'GetDead >> 0.00\n'}
    return ''.join('> ' + c + '\n' + responses.get(c, '') for c in program['commands'])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_service.ServiceTests()
        self.fixture.setUp()
        self.p = self.fixture.p
        self.service = self.fixture.service
        self.runtime = Runtime(self.p)
        self.args = dict(profile=PROFILE, runtime_mapping=MAPPING, save_label='synthetic save A',
                         expected_plugin_hash=digest(self.p.plugin_path.read_bytes()))

    def tearDown(self):
        self.fixture.tearDown()

    def prepare(self, **overrides):
        result = self.service.runtime_call('prepare', {**self.args, **overrides}, 'prep')
        self.assertTrue(result['ok'], result)
        self.receipt = result['data']
        self.session = self.receipt['runtime_session']
        self.log = self.p.game_root / self.receipt['log_name']
        return result

    def collect(self, name):
        return self.service.runtime_call('collect', {'session': self.session, 'checkpoint_label': name})

    def test_preview_validation_and_crlf_allowlist(self):
        result = self.runtime.prepare(**self.args, preview=True)
        self.assertFalse(self.runtime.root.exists())
        self.assertIn('GetDead', result['commands'])
        self.assertNotIn('bat ', '\n'.join(result['commands']))
        self.assertNotIn('nvse', '\n'.join(result['commands']).lower())
        self.assertEqual(result['commands'][-1], 'prid 00000014')
        self.prepare()
        start_name = next(iter(self.receipt['batches']))
        self.assertLessEqual(len(start_name.removesuffix('.txt')), 17)
        self.assertEqual(self.receipt['instructions'][0], 'Once only: bat ' + start_name.removesuffix('.txt'))
        for filename in self.receipt['batches']:
            data = (self.p.game_root / filename).read_bytes()
            self.assertTrue(data.endswith(b'\r\n'))
            self.assertNotIn(b'\n', data.replace(b'\r\n', b''))
            self.assertLess(max(map(len, data.splitlines())), 512)
        self.assertEqual(self.fixture.service.transport.calls, [])

    def test_rejects_injection_paths_unknown_probes_rules_and_nonfinite(self):
        for field, value in [('plugin', '../Other.esp'), ('plugin', 'evil\n.esp'),
                              ('schema_version', 2), ('commands', ['bat nested'])]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                Profile.model_validate({**PROFILE, field: value})
        for field, value in [('variable', 'State\nset x to 1'), ('local_form', '01000801'),
                              ('kind', 'moveto'), ('key', '../state')]:
            bad = copy.deepcopy(PROFILE)
            bad['probes'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                Profile.model_validate(bad)
        for rule in ({'key': 'bad', 'kind': 'equals', 'fields': ['state']},
                     {'key': 'bad', 'kind': 'range', 'fields': ['missing'], 'minimum': 0},
                     {'key': 'bad', 'kind': 'equals', 'fields': ['state'], 'value': float('nan')}):
            with self.assertRaises(ValueError):
                Profile.model_validate({**PROFILE, 'rules': [rule]})
        for prefix in ('FF', '0A\nbat bad', '001'):
            with self.assertRaises(ValueError):
                Mapping(prefix=prefix)

    def test_idempotency_conflict_and_session_separation(self):
        first = self.prepare()
        again = self.service.runtime_call('prepare', self.args, 'prep')
        self.assertTrue(again['replayed'])
        self.assertEqual(first['data'], again['data'])
        conflict = self.service.runtime_call('prepare', {**self.args, 'save_label': 'other'}, 'prep')
        self.assertEqual(conflict['error']['code'], 'IDEMPOTENCY_CONFLICT')
        other = self.service.runtime_call('prepare', self.args, 'other')['data']
        self.assertNotEqual(other['runtime_session'], self.session)
        self.assertNotEqual(other['log_name'], self.log.name)

    def test_missing_empty_echo_only_wrong_form_never_pass(self):
        self.prepare()
        self.assertEqual(self.collect('before')['data']['code'], 'LOG_MISSING')
        self.log.write_text('')
        self.assertEqual(self.collect('before')['data']['status'], 'awaiting_output')
        self.log.write_text('\n'.join('> ' + c for c in self.receipt['commands']) + '\n')
        data = self.collect('echo')['data']
        self.assertEqual(data['status'], 'partial')
        self.assertEqual(len(data['checkpoint']['missing']), 4)
        self.assertEqual(data['assertions'], 'inconclusive')
        with self.log.open('a') as stream:
            stream.write(output(self.receipt).replace('06000802', '07000802'))
        result = self.collect('wrong')['data']
        self.assertEqual(result['status'], 'awaiting_output')
        self.assertTrue(result['issues'])

    def test_checkpoints_delta_rules_and_no_stale_recollection(self):
        self.prepare()
        self.log.write_text(output(self.receipt))
        first = self.collect('before')['data']
        self.assertEqual(first['status'], 'complete')
        self.assertEqual(first['assertions'], 'inconclusive')  # unchanged needs a separate baseline
        self.assertEqual(self.collect('not_fresh')['data']['code'], 'NO_FRESH_SAMPLE')
        with self.log.open('a') as stream:
            stream.write(output(self.receipt, count=2))
        second = self.collect('after')['data']
        self.assertEqual(second['assertions'], 'fail')
        self.assertEqual(second['changes']['count']['delta'], 1)
        self.assertEqual(second['checkpoint']['values']['count']['raw'], 'GetItemCount >> 2.00')
        self.assertGreater(second['checkpoint']['values']['count']['line'], first['checkpoint']['end_line'])
        self.assertEqual(self.collect('after')['error']['code'], 'CHECKPOINT_EXISTS')
        with self.assertRaises(ValueError):
            self.runtime.report(self.session, 'missing')

    def test_locked_partial_truncated_and_same_size_replaced_logs(self):
        self.prepare()
        text = output(self.receipt)
        self.log.write_text(text[:-2])
        result = self.collect('before')['data']
        self.assertEqual(result['status'], 'awaiting_output')
        self.assertTrue(result['pending'])
        original = Path.open
        def locked(path, *args, **kwargs):
            if path == self.log:
                raise PermissionError('synthetic lock')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', locked):
            self.assertEqual(self.collect('before')['data']['code'], 'LOG_LOCKED')
        with self.log.open('a') as stream:
            stream.write(text[-2:])
        self.assertEqual(self.collect('before')['data']['status'], 'complete')
        self.log.write_text(text.replace('1.00', '2.00'))
        self.assertEqual(self.collect('after')['data']['code'], 'LOG_REPLACED')
        self.log.write_text('')
        self.assertEqual(self.collect('after')['data']['code'], 'LOG_REPLACED')

    def test_compile_errors_partial_fields_and_unrelated_errors(self):
        self.prepare()
        self.log.write_text('Error: unrelated command failed\n' + output(self.receipt).replace(
            '->State = 3', 'Missing parameter String'))
        data = self.collect('broken')['data']
        self.assertEqual(data['status'], 'errored')
        self.assertEqual(data['assertions'], 'inconclusive')
        self.assertIn('state', data['checkpoint']['missing'])
        self.assertTrue(data['checkpoint']['unrelated_errors'])
        self.assertEqual(data['checkpoint']['errors'][0]['code'], 'CONSOLE_ERROR')

    def test_unverified_wrong_mapping_and_plugin_change(self):
        self.prepare(runtime_mapping={'prefix': '07', 'source': 'disk_order'})
        self.log.write_text(output(self.receipt))  # wrong form -> missing quest/count
        data = self.collect('wrong')['data']
        self.assertEqual(data['identity'], 'unverified')
        self.assertNotEqual(data['assertions'], 'pass')
        self.p.plugin_path.write_bytes(b'changed')
        self.assertEqual(self.runtime.report(self.session)['identity'], 'plugin_changed')

    def test_archive_only_owned_unchanged_files_and_no_logger_stop(self):
        self.prepare()
        helpers = list(self.receipt['batches'])
        changed = self.p.game_root / helpers[0]
        changed.write_text('user change')
        foreign = self.p.game_root / 'foreign.txt'
        foreign.write_text('keep')
        with self.assertRaises(ValueError):
            self.runtime.close(self.session, archive_helpers=True)
        result = self.runtime.close(self.session, archive_helpers=True, game_exited=True)
        self.assertFalse(result['logger_stopped'])
        self.assertTrue(changed.exists())
        self.assertTrue(foreign.exists())
        self.assertFalse((self.p.game_root / helpers[1]).exists())
        self.assertEqual(self.collect('closed')['error']['code'], 'RUNTIME_CLOSED')
        self.assertEqual(len(self.runtime.close(self.session, True, True)['archived']), 1)

    def test_runtime_does_not_use_editor_lock_or_recovery_barrier(self):
        self.service._active_operation = 'concurrent-editor-operation'
        self.service._mutation_dispatched = True
        with bottle_lock(self.p.lock_root):
            result = self.prepare()
        self.assertTrue(result['ok'])
        evidence = self.service.operation_evidence(result['operationId'])
        self.assertEqual(evidence['journalState'], 'runtime_prepared')
        self.assertEqual(self.service.unresolved(), [])
        self.assertEqual(self.service._active_operation, 'concurrent-editor-operation')
        self.assertTrue(self.service._mutation_dispatched)

    def test_ambiguous_samples_are_not_given_one_label(self):
        self.prepare()
        self.log.write_text(output(self.receipt) * 2)
        self.assertEqual(self.collect('ambiguous')['data']['code'], 'AMBIGUOUS_CHECKPOINT')

    def test_validation_failure_does_not_create_recovery_barrier(self):
        result = self.service.runtime_call('prepare', {**self.args, 'save_label': ''}, 'bad-label')
        self.assertFalse(result['ok'])
        self.assertEqual(result['outcome'], 'failed_before_change')
        self.assertFalse(self.runtime.root.exists())
        self.assertEqual(self.service.unresolved(), [])

    def test_report_lowers_confidence_after_log_replacement(self):
        self.prepare()
        self.log.write_text(output(self.receipt))
        self.collect('before')
        self.log.write_text('replacement\n')
        self.collect('after')
        report = self.runtime.report(self.session)
        self.assertEqual(report['identity'], 'continuity_invalid')
        self.assertEqual(report['assertions'], 'inconclusive')
        self.assertEqual(report['collection_error']['code'], 'LOG_REPLACED')

    def test_session_path_traversal_and_duplicate_profiles_rejected(self):
        for session in ('../../other', 'BAD', 'f'*33):
            with self.assertRaises(ValueError):
                self.runtime.load(session)
        with self.assertRaises(ValueError):
            Profile.model_validate({**PROFILE, 'probes': PROFILE['probes'] * 2})

    def test_explicit_sanitized_export_omits_private_evidence(self):
        self.prepare()
        self.log.write_text(output(self.receipt))
        self.collect('private checkpoint')
        sanitized = self.runtime.report(self.session, sanitized=True)
        text = json.dumps(sanitized)
        self.assertNotIn('private checkpoint', text)
        self.assertNotIn('raw', text)
        self.assertNotIn('runtime_session', text)
        self.assertNotIn('evidence', text)

    def test_duplicate_and_invalid_observations_do_not_pass(self):
        program = plan(Profile.model_validate(PROFILE), Mapping.model_validate(MAPPING), 'fixture')
        for bad in ('GetItemCount >> -1.00', 'GetItemCount >> 1.25',
                    'GetItemCount >> 1.00\nGetItemCount >> 2.00'):
            sample = parse(output(program).replace('GetItemCount >> 1.00', bad), program)['samples'][0]
            self.assertTrue(sample['errors'])
            self.assertNotIn('count', sample['values'])

    def test_incremental_parser_handles_every_byte_boundary(self):
        program = plan(Profile.model_validate(PROFILE), Mapping.model_validate(MAPPING), 'fixture')
        text = output(program).replace('\n', '\r\n')
        expected = parse(text, program)['samples']
        state = None
        samples = []
        for character in text:
            result = parse(character, program, state)
            state = result['state']
            samples.extend(result['samples'])
        self.assertEqual(samples, expected)
        self.assertFalse(result['pending'])
        with self.assertRaises(ValueError):
            plan(Profile.model_validate(PROFILE), Mapping.model_validate(MAPPING), '../escape')

    def test_partial_preparation_has_receipt_and_cannot_be_collected(self):
        original = Path.open
        def interrupted(path, *args, **kwargs):
            if path.parent == self.p.game_root and path.name.endswith('_sample.txt'):
                raise PermissionError('synthetic write interruption')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', interrupted):
            result = self.service.runtime_call('prepare', self.args, 'interrupted')
        self.assertEqual(result['error']['code'], 'RUNTIME_PREPARE_INTERRUPTED')
        self.assertEqual(result['outcome'], 'outcome_unknown')
        session = result['data']['runtime_session']
        self.assertEqual(self.service.unresolved(), [])
        collected = self.service.runtime_call('collect', {'session': session, 'checkpoint_label': 'bad'})
        self.assertEqual(collected['error']['code'], 'RUNTIME_NOT_PREPARED')
        closed = self.runtime.close(session, archive_helpers=True, game_exited=True)
        self.assertEqual(len(closed['archived']), 1)

    def test_compiler_error_with_repeated_cr_is_not_lost(self):
        profile = Profile.model_validate({'plugin': 'Fixture.esp', 'probes': [
            {'key': 'bad', 'kind': 'quest_variable', 'local_form': '000802', 'variable': 'NeverDefined'}]})
        program = plan(profile, Mapping.model_validate(MAPPING), 'fixture')
        text = (">  sqv 06000802\r\nSCRIPTS: Script 'SysWindowCompileAndRun', line 1:\r\n"
                "Invalid info '06000802' for parameter Quest.\r\r\n"
                "Compiled script not saved!\r\n>  prid 00000014\r\n")
        result = parse(text, program)
        self.assertFalse(result['pending'])
        self.assertEqual(len(result['samples']), 1)
        self.assertEqual(result['samples'][0]['errors'][0]['line'], 3)
        self.assertEqual(result['samples'][0]['values'], {})
        state = None
        samples = []
        for character in text:
            chunk = parse(character, program, state)
            state = chunk['state']
            samples.extend(chunk['samples'])
        self.assertEqual(samples, result['samples'])


class RuntimeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_stdio_mcp_and_cli_share_report(self):
        fixture = test_service.ServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        root = Path(__file__).resolve().parents[1]
        project_file = Path(fixture.tmp.name) / 'project.json'
        project_file.write_text(fixture.p.model_dump_json())
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(root / 'run_mcp.py'), '--project', str(project_file), '--legacy'], cwd=str(root))
        async with Client(parameters) as client:
            prepared = await client.call_tool('geck_runtime_prepare', dict(profile=PROFILE, runtime_mapping=MAPPING,
                save_label='synthetic save', expected_plugin_hash=digest(fixture.p.plugin_path.read_bytes()), request_key='stdio'))
            self.assertFalse(prepared.is_error, prepared)
            receipt = prepared.structured_content['data']
            (fixture.p.game_root / receipt['log_name']).write_text(output(receipt))
            collected = await client.call_tool('geck_runtime_collect', dict(runtime_session=receipt['runtime_session'], checkpoint_label='before'))
            self.assertFalse(collected.is_error, collected)
            with (fixture.p.game_root / receipt['log_name']).open('a') as stream:
                stream.write(output(receipt))
            await client.call_tool('geck_runtime_collect', dict(runtime_session=receipt['runtime_session'], checkpoint_label='after'))
            reported = await client.call_tool('geck_runtime_report', dict(runtime_session=receipt['runtime_session']))
            cli = subprocess.run([sys.executable, str(root / 'runtime_diagnostics.py'), '--project', str(project_file),
                                  'report', receipt['runtime_session']], cwd=root, capture_output=True, text=True)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(json.loads(cli.stdout)['data'], reported.structured_content['data'])
            closed = await client.call_tool('geck_runtime_close', dict(runtime_session=receipt['runtime_session'], request_key='close'))
            self.assertFalse(closed.is_error, closed)
            self.assertFalse(closed.structured_content['data']['logger_stopped'])
