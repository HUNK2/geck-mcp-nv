"""Live fnv_* tools without the game: YAML scenarios, game control helpers, the live service on a fake
companion link, and (with GECK_MCP_ZIG) the real engine companion DLL over its pipe under a fake xNVSE host."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_game, fnv_link, fnv_scenario  # noqa: E402
from geck_mcp.config import Project  # noqa: E402
from geck_mcp.fnv_facts import FACTS  # noqa: E402
from geck_mcp.fnv_live import LiveService  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples/fnv-tests'


class YamlTests(unittest.TestCase):
    def test_examples_parse(self):
        for path in EXAMPLES.glob('*.yaml'):
            data = fnv_scenario.parse_yaml(path.read_text(encoding='utf-8'))
            self.assertIn('steps', data, path.name)
        hello = fnv_scenario.parse_yaml((EXAMPLES / 'hello-wasteland.yaml').read_text(encoding='utf-8'))
        self.assertEqual(hello['start'], {'coc': 'HelloWasteland'})
        self.assertEqual(hello['steps'][1], {'expect': {'eval': 'GetNumRefs 0 0', 'ge': 14}})
        self.assertEqual(hello['teardown'], [{'revert_all': True}])

    def test_subset(self):
        text = '''
name: "quoted # not a comment"   # a comment
plain: hello world
numbers: [1, 2.5, -3, 0x10, true, null]
nested:
  a: {x: 1, y: "two, three", z: [a, b]}
  b:
    - one
    - key: v
      other: 2
    - - inner
list_at_key_indent:
- first
- second
script: |
  line one
  line two
'''
        data = fnv_scenario.parse_yaml(text)
        self.assertEqual(data['name'], 'quoted # not a comment')
        self.assertEqual(data['plain'], 'hello world')
        self.assertEqual(data['numbers'], [1, 2.5, -3, '0x10', True, None])
        self.assertEqual(data['nested']['a'], {'x': 1, 'y': 'two, three', 'z': ['a', 'b']})
        self.assertEqual(data['nested']['b'], ['one', {'key': 'v', 'other': 2}, ['inner']])
        self.assertEqual(data['list_at_key_indent'], ['first', 'second'])
        self.assertEqual(data['script'], 'line one\nline two')
        self.assertEqual(fnv_scenario.parse_yaml('{"steps": []}'), {'steps': []})
        with self.assertRaises(fnv_scenario.ScenarioError):
            fnv_scenario.parse_yaml('a: {x: 1')

    def test_compare(self):
        self.assertTrue(fnv_scenario.compare(10.0001, {'approx': 10, 'tolerance': 0.01})[0])
        self.assertFalse(fnv_scenario.compare(12.2, {'equals': 99})[0])
        self.assertTrue(fnv_scenario.compare(15, {'ge': 14, 'lt': 20})[0])
        self.assertFalse(fnv_scenario.compare(None, {'gt': 1})[0])
        self.assertTrue(fnv_scenario.compare(['GetActorValue: Health >> 1'], {'contains': 'health >>'})[0])
        self.assertTrue(fnv_scenario.compare('0x0000000F', {'equals': '0000000f'})[0])
        self.assertTrue(fnv_scenario.compare(1, {'equals': True})[0])


class GameHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.mo2 = root / 'mo2'
        (self.mo2 / 'profiles/Dev/saves').mkdir(parents=True)
        (self.mo2 / 'profiles/Dev/modlist.txt').write_text('# header\n+Other Mod\n', encoding='utf-8')
        (self.mo2 / 'profiles/Dev/plugins.txt').write_text('# header\nFalloutNV.esm\n', encoding='utf-8')
        self.game_root = root / 'game'
        self.game_root.mkdir()
        self.project = Project.model_validate({
            'project_id': 'livetest', 'launcher': 'native', 'game_root': str(self.game_root), 'plugin': 'Fixture.esp',
            'records': [{'editor_id': 'FixtureStatic', 'form_id': '01000800'}],
            'state_dir': str(root / 'state'), 'helper': str(root / 'helper.exe'),
            'fnv': {'mo2_root': str(self.mo2), 'mo2_profile': 'Dev', 'engine_control': 'full'}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_start(self):
        saves = self.mo2 / 'profiles/Dev/saves'
        self.assertEqual(fnv_game.parse_start('menu', saves), ('menu', None))
        self.assertEqual(fnv_game.parse_start('coc:GoodspringsExt', saves), ('coc', 'GoodspringsExt'))
        with self.assertRaises(fnv_game.GameError):
            fnv_game.parse_start('continue', saves)
        (saves / 'test start.fos').write_bytes(b'x')
        self.assertEqual(fnv_game.parse_start('continue', saves), ('save', 'test start'))
        self.assertEqual(fnv_game.parse_start('save:test start', saves), ('save', 'test start'))
        for bad in ('save:missing', 'coc:bad;cell', 'save:../x', 'sideways'):
            with self.assertRaises(fnv_game.GameError, msg=bad):
                fnv_game.parse_start(bad, saves)

    def test_logs_cursor(self):
        game = fnv_game.Game(self.project)
        log = self.game_root / 'nvse.log'
        log.write_text('one\ntwo\n', encoding='utf-8')
        self.assertEqual(game.logs(['nvse'])['nvse']['lines'], ['one', 'two'])
        self.assertEqual(game.logs(['nvse'])['nvse']['lines'], [])
        with log.open('a', encoding='utf-8') as f:
            f.write('three\n')
        self.assertEqual(game.logs(['nvse'])['nvse']['lines'], ['three'])
        self.assertEqual(game.logs(['nvse'], since=0)['nvse']['lines'], ['one', 'two', 'three'])
        self.assertTrue(game.logs(['crash'])['crash']['missing'])

    def test_install_plugin_and_mod_list(self):
        game = fnv_game.Game(self.project)
        plugin = Path(self.tmp.name) / 'Built.esp'
        plugin.write_bytes(b'TES4 fixture')
        with mock.patch.object(fnv_game.Game, 'process', return_value=None):
            first = game.install_plugin(plugin)
            again = game.install_plugin(plugin)
            plugin.write_bytes(b'TES4 changed')
            third = game.install_plugin(plugin)
        self.assertTrue(first['installed'] and not first['unchanged'])
        self.assertTrue(again['unchanged'])
        self.assertTrue(Path(third['backup']).exists())
        modlist = (self.mo2 / 'profiles/Dev/modlist.txt').read_text(encoding='utf-8').splitlines()
        self.assertEqual(modlist, ['# header', '+Built', '+Other Mod'])
        self.assertIn('Built.esp', (self.mo2 / 'profiles/Dev/plugins.txt').read_text(encoding='utf-8'))
        self.assertEqual((self.mo2 / 'mods/Built/Built.esp').read_bytes(), b'TES4 changed')
        with mock.patch.object(fnv_game.Game, 'process', return_value=(1, 0)):
            with self.assertRaises(fnv_game.GameError):
                game.install_plugin(plugin)
        self.assertTrue(fnv_link.disable_mod(self.mo2, 'Dev', 'Built'))
        self.assertIn('-Built', (self.mo2 / 'profiles/Dev/modlist.txt').read_text(encoding='utf-8'))


class FakeLink(fnv_link.EngineLink):
    """Answers the companion ops from a small model: memory, expressions and console lines. The typed helpers
    (read, write, eval, console...) are the real EngineLink ones on top of this batch()."""

    def __init__(self):
        super().__init__('fnvmcp-fake')
        self.memory = {}
        self.expressions = {}
        self.console_log = []
        self.hello = {'pid': 4242, 'started': '1', 'frame': 10, 'deferredInit': True, 'loaded': True}
        self.calls = []

    def put(self, address, data):
        for i, b in enumerate(data):
            self.memory[address + i] = b

    def get(self, address, length):
        if any(address + i not in self.memory for i in range(length)):
            return None
        return bytes(self.memory[address + i] for i in range(length))

    def ping(self, timeout=3.0):
        return self.hello

    def close(self):
        pass

    def batch(self, ops, timeout=None):
        return [self._op(op[0], [str(a) for a in op[1:]]) for op in ops]

    def _op(self, name, args):
        if name == 'rd':
            data = self.get(int(args[0], 16), int(args[1], 16))
            return {'ok': True, 'hex': data.hex()} if data is not None else {'ok': False, 'error': 'unreadable'}
        if name == 'wr':
            address, data = int(args[0], 16), bytes.fromhex(args[1])
            old = self.get(address, len(data))
            if old is None:
                return {'ok': False, 'error': 'unreadable'}
            self.put(address, data)
            return {'ok': True, 'old': old.hex()}
        if name == 'ls':
            return {'ok': False, 'error': 'unknown_op'}
        if name == 'rs':
            return {'ok': False, 'error': 'unreadable'}
        if name in ('eval', 'run'):
            text = bytes.fromhex(args[0]).decode('cp1252')
            element = self.expressions.get(text)
            if element is None:
                return {'ok': False, 'error': 'compile_failed'}
            return {'ok': True, 'called': True, 'scriptErrors': 0, 'result': element}
        if name == 'con':
            line = bytes.fromhex(args[0]).decode('cp1252')
            flags = (self.get(FACTS['verbose_print_flag']['address'], 1), self.get(FACTS['console_visible_flag']['address'], 1))
            self.console_log.append((line, flags))
            if line.startswith('SetNumericGameSetting'):
                value = float(line.split()[-1])
                self.expressions['GetNumericGameSetting "fJumpHeightMin"'] = {'type': 'number', 'value': value}
            return {'ok': True, 'accepted': True, 'scriptErrors': 0, 'captured': True, 'output': 'echo %s\r\n' % line}
        if name == 'call':
            self.calls.append(args)
            return {'ok': True, 'eax': '0x00000001', 'edx': '0x00000000', 'scriptErrors': 0}
        if name == 'events':
            return {'ok': True, 'seq': 0, 'events': []}
        if name == 'sched':
            return {'ok': True, 'slot': 0}
        return {'ok': False, 'error': 'unknown_op'}


class LiveServiceTests(GameHelperTests):
    PLAYER = 0x10000000

    def live(self, control='full'):
        project = self.project.model_copy(update={'fnv': self.project.fnv.model_copy(update={'engine_control': control})})
        live = LiveService(project)
        link = FakeLink()
        live._game = fnv_game.Game(project)
        live._game.link = link
        verbose, visible = FACTS['verbose_print_flag']['address'], FACTS['console_visible_flag']['address']
        link.put(verbose, b'\x00')
        link.put(visible, b'\x00')
        # player: TESForm header (type 0x3B, refID 0x14) and a float at +0x3C (TESObjectREFR::m_fObjectScale)
        link.put(self.PLAYER, b'\x00' * 0x80)
        link.put(self.PLAYER + 4, bytes([0x3B]))
        link.put(self.PLAYER + 0x0C, struct.pack('<I', 0x14))
        link.put(self.PLAYER + 0x3C, struct.pack('<f', 1.0))
        link.expressions['player'] = {'type': 'form', 'ptr': '0x%08X' % self.PLAYER, 'formId': '00000014'}
        link.expressions['GetNumericGameSetting "fJumpHeightMin"'] = {'type': 'number', 'value': 64.0}
        return live, link

    def test_act_tools_refuse_without_full_control(self):
        live, link = self.live('read')
        result = live.run('fnv_mem_write', lambda: live.mem_write('0x10000000', 1, as_type='u8'), 'act')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'CONTROL_DISABLED')
        self.assertEqual(result['outcome'], 'failed_before_change')
        self.assertEqual(link.get(self.PLAYER, 1), b'\x00')

    def test_console_sets_and_restores_print_flags(self):
        live, link = self.live()
        out = live.console('player.GetAV Health')
        self.assertTrue(out['accepted'])
        self.assertEqual(out['output'], ['echo player.GetAV Health'])
        self.assertEqual(link.console_log[-1][1], (b'\x01', b'\x01'))
        self.assertEqual(link.get(FACTS['verbose_print_flag']['address'], 1), b'\x00')
        self.assertEqual(link.get(FACTS['console_visible_flag']['address'], 1), b'\x00')

    def test_memory_write_journal_and_undo(self):
        live, link = self.live()
        with mock.patch.object(fnv_game.Game, 'backup_saves', return_value={'path': 'x'}):
            result = live.run('fnv_mem_write', lambda: live.mem_write('0x1000003C', 1.25, as_type='f32'), 'act')
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['outcome'], 'acknowledged')
            self.assertEqual(struct.unpack('<f', link.get(self.PLAYER + 0x3C, 4))[0], 1.25)
            entry = live.journal.entries()[-1]
            self.assertEqual(entry['old'], struct.pack('<f', 1.0).hex())
            undone = live.run('fnv_undo', lambda: live.undo(1), 'act')
            self.assertTrue(undone['ok'], undone)
        self.assertEqual(struct.unpack('<f', link.get(self.PLAYER + 0x3C, 4))[0], 1.0)
        self.assertTrue(live.journal.entries()[-1]['undone'])

    def test_setting_set_and_revert_all(self):
        live, link = self.live()
        with mock.patch.object(fnv_game.Game, 'backup_saves', return_value={'path': 'x'}), \
                mock.patch.object(LiveService, 'engine', side_effect=RuntimeError('no map')):
            result = live.setting_set('fJumpHeightMin', 150)
            self.assertEqual((result['old'], result['new']), (64.0, 150.0))
            reverted = live.revert_all()
        self.assertEqual(reverted['reverted'], 1)
        self.assertEqual(link.console_log[-1][0], 'SetNumericGameSetting "fJumpHeightMin" 64.0')

    def test_resolve_specs(self):
        live, link = self.live()
        self.assertEqual(live.resolve('player')['formId'], '00000014')
        link.expressions['(BuildRef 0 1314876)'] = {'type': 'form', 'ptr': '0x20000000', 'formId': '0014103C'}
        self.assertEqual(live.resolve('0x0014103C')['expr'], '(BuildRef 0 1314876)')
        link.expressions['(BuildRef (GetModIndex "Hello.esp") 2051)'] = {'type': 'form', 'ptr': '0x20000010', 'formId': '0A000803'}
        self.assertEqual(live.resolve('Hello.esp:000803')['formId'], '0A000803')
        with self.assertRaises(Exception):
            live.resolve('NoSuchThing')

    def test_scenario_runner_on_fake_link(self):
        live, link = self.live()
        link.expressions['GetGlobalValue GameHour'] = {'type': 'number', 'value': 12.5}
        link.expressions['MenuMode 1007'] = {'type': 'number', 'value': 0}
        runner = fnv_scenario.ScenarioRunner(live)
        text = '''
name: fake
steps:
  - expect: {global: GameHour, approx: 12.5}
  - run: player.GetAV Luck
  - expect: {console: "player.GetAV Luck", contains: "echo"}
  - expect: {global: GameHour, equals: 99}
  - run: skipped
'''
        with mock.patch.object(fnv_scenario.ScenarioRunner, 'prepare', return_value={'launched': False}), \
                mock.patch.object(fnv_scenario.ScenarioRunner, 'screenshot', return_value=None), \
                mock.patch.object(fnv_game.Game, 'backup_saves', return_value={'path': 'x'}):
            report = runner.run(text=text)
        self.assertFalse(report['passed'])
        self.assertEqual([s.get('passed') for s in report['steps']], [True, True, True, False, None])
        self.assertTrue(report['steps'][4]['skipped'])
        self.assertIn('expected equals 99, got 12.5', report['failures'][0]['reason'])
        self.assertTrue(Path(report['report']).exists())


@unittest.skipUnless(os.environ.get('GECK_MCP_ZIG'), 'set GECK_MCP_ZIG to build the engine companion')
class CompanionPipeTests(unittest.TestCase):
    """The real DLL over its pipe, loaded by a fake xNVSE host (never the game)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        receipt = fnv_link.build(cls.tmp.name, os.environ['GECK_MCP_ZIG'], with_fake_host=True)
        folder = Path(receipt['dll']).parent
        cls.pipe = 'fnvmcp-test-' + uuid.uuid4().hex[:8]
        cls.log = Path(cls.tmp.name) / 'console.log'
        env = dict(os.environ, FNVMCP_PIPE=cls.pipe, FNVMCP_CONSOLE_LOG=str(cls.log))
        cls.host = subprocess.Popen([str(folder / 'fnvmcp_fake_host.exe'), receipt['dll'], '60'], stdout=subprocess.PIPE,
                                    env=env, text=True)
        cls.targets = {k: int(v, 16) for k, v in json.loads(cls.host.stdout.readline()).items()}
        cls.link = fnv_link.EngineLink(cls.pipe).connect(wait=10)

    @classmethod
    def tearDownClass(cls):
        cls.link.close()
        cls.host.kill()
        cls.host.wait()
        cls.host.stdout.close()
        cls.tmp.cleanup()

    def test_ping_and_info(self):
        ping = self.link.ping()
        self.assertTrue(ping['pong'] and ping['deferredInit'] and ping['loaded'])
        info = self.link.info()
        self.assertTrue(info['mainThread'])
        self.assertEqual(info['runtime'], '0x040020d0')

    def test_memory(self):
        t = self.targets
        self.assertEqual(self.link.read(t['buffer'], 6), b'fnvmcp')
        old = self.link.write(t['buffer'], b'FNV')
        self.assertEqual(old, b'fnv')
        self.assertEqual(self.link.one('rs', fnv_link.word(t['buffer']), '40')['text'], 'FNVmcp fake host buffer')
        self.link.write(t['buffer'], old)
        chain = self.link.one('rp', fnv_link.word(t['chain']), '0', '0', '0')
        self.assertEqual(chain['chain'][-1], '0x11223344')
        self.assertFalse(self.link.one('rd', '10', '4')['ok'])
        with self.assertRaises(fnv_link.LinkError):
            self.link.write(0x10, b'\x00')

    def test_calls(self):
        t, w = self.targets, fnv_link.word
        self.assertEqual(self.link.one('call', w(t['add3']), 'i', '0', '0', '1', '2', '3')['eax'], '0x00000006')
        self.assertEqual(self.link.one('call', w(t['sub2']), 'i', '0', '0', '9', '4')['eax'], '0x00000005')
        self.assertEqual(self.link.one('call', w(t['member']), 'i', w(t['self']), '0', '7')['eax'], '0x0000002a')
        self.assertEqual(self.link.one('call', w(t['fast']), 'i', '1', '2', '3')['eax'], '0x0000007b')
        f = lambda x: '%x' % struct.unpack('<I', struct.pack('<f', x))[0]
        self.assertEqual(self.link.one('call', w(t['halfsum']), 'f', '0', '0', f(1.5), f(2.5))['st0'], 2)
        lo, hi = struct.unpack('<II', struct.pack('<d', 2.25))
        self.assertEqual(self.link.one('call', w(t['scale']), 'f', '0', '0', '%x' % lo, '%x' % hi, '4')['st0'], 9)
        self.assertEqual(self.link.one('call', '10', 'i', '0', '0')['error'], 'bad_arguments')

    def test_console_eval_events(self):
        out = self.link.console('player.additem f 5')
        self.assertEqual(out['output'], 'echo >> player.additem f 5\r\n')
        err = self.link.console('error')
        self.assertEqual(err['scriptErrors'], 1)
        self.assertFalse(self.link.console('reject')['accepted'])
        self.assertEqual(self.link.eval('form')['result']['formId'], '0000ABCD')
        self.assertEqual(self.link.eval('str')['result']['value'], 'hello "world"')
        self.assertEqual(fnv_link.value_of(self.link.eval('arr')), [7, 8])
        self.assertEqual(self.link.eval('bad')['error'], 'compile_failed')
        events = self.link.events(0)['events']
        self.assertIn('script_error', [e['type'] for e in events])
        self.assertEqual(self.link.schedule(0, 'later')['slot'] >= 0, True)


if __name__ == '__main__':
    unittest.main()
