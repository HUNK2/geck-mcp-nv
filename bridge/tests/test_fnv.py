"""fnv_* tools: engine map generator, engine knowledge queries and crash doctor, on a tiny synthetic decomp.

No real decompilation data, game files or crash logs are used: every input is written here.
"""
import json
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine_map import commands, demangle, generate, headers, source  # noqa: E402
from geck_mcp import fnv_crash, fnv_engine  # noqa: E402
from geck_mcp.config import Project  # noqa: E402
from geck_mcp.fnv_tools import FNV_TOOLS, FnvService  # noqa: E402

ACTOR_CPP = textwrap.dedent('''\
    #include "Actor.h"

    // @fnv 0x00401000
    // Applies damage to the actor.
    void Actor::ApplyDamage(float afAmount)
    {
        if (afAmount > 0.0f)
        {
            FUN_00401100(this);
        }
        const char* s = "}";   // a brace in a string must not end the body
    }

    // @fnv 0x00401100
    struct FUN_00401100_shell
    {
        void FUN_00401100();
    };
    void FUN_00401100_shell::FUN_00401100()
    {
        m_fHealth -= fCombatPlayerLimbDamageMult; /* limb damage */
    }
    ''')
MISC_C = textwrap.dedent('''\
    // @fnv 0x00401200
    int FUN_00401200(int a)
    {
        return a + 1;
    }
    ''')
ACTOR_H = textwrap.dedent('''\
    #ifndef FNV_ACTOR_H
    #define FNV_ACTOR_H
    class TESObjectREFR;
    class Actor : public TESObjectREFR,
                  public ActorValueOwner
    {
      public:
        unsigned int m_uiFlags;  // +0xC
        float m_fHealth;         // +0x10
        unsigned char layoutPadding14[0xC];
        virtual ~Actor();                      // slot 0x0
        virtual void ApplyDamage(float);       // slot 0x1
    };
    FNV_STATIC_ASSERT(sizeof(Actor) == 0x20);
    FNV_STATIC_ASSERT(offsetof(Actor, m_fHealth) == 0x10);
    #endif
    ''')
VERIFIED = textwrap.dedent('''\
    addr,current,name,status,overload,identity,behaviour,by
    0x00401000,Actor::ApplyDamage,Actor::ApplyDamage,proven,,"C: CommonLibNV Offsets.h:1 names this address",reduces health,review:community-01
    0x00401100,FUN_00401100_shell::FUN_00401100,Actor::ApplyLimbDamage,proven,,"C: JG Actor.cpp:9 wraps 0x401100",subtracts limb damage,review:community-02
    0x00401300,Eg_011deed8::operator?__,Init_Setting_bAlwaysActive,proven,,"G: static initializer registering only ""bAlwaysActive:General""",constructs that setting,mech:static-init
    ''')


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def fake_decomp(root):
    units = {'note': 'synthetic', 'totals': {}, 'units': [
        {'source': 'src/actor.cpp', 'compiler': 'sp1', 'flags': [], 'symbols': {}, 'functions': [
            {'address': '0x00401000', 'symbol': '?ApplyDamage@Actor@@QAEXM@Z', 'size': 0x40},
            {'address': '0x00401100', 'symbol': '?FUN_00401100@FUN_00401100_shell@@QAEXXZ', 'size': 0x20}]},
        {'source': 'src/misc.c', 'compiler': 'sp1', 'flags': [], 'symbols': {}, 'functions': [
            {'address': '0x00401200', 'symbol': '_FUN_00401200', 'size': 0x10}]},
    ]}
    write(root, 'build/units.json', json.dumps(units))
    write(root, 'src/actor.cpp', ACTOR_CPP)
    write(root, 'src/misc.c', MISC_C)
    write(root, 'include/Actor.h', ACTOR_H)
    write(root, 'verified.csv', VERIFIED)


class FnvTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.decomp = root / 'decomp'
        fake_decomp(cls.decomp)
        cls.map_path = root / 'state' / 'engine_map.json'
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            generate.main(['--decomp', str(cls.decomp), '--verified', str(cls.decomp / 'verified.csv'),
                           '--out', str(cls.map_path), '--header', str(root / 'state' / 'addresses.h')])
        data = json.loads(cls.map_path.read_text())
        data['rtti'] = {     # what --exe adds; synthetic here
            'Actor': {'bases': ['TESObjectREFR', 'TESForm'], 'directBases': ['TESObjectREFR'],
                      'vtables': [{'address': '0x01010000', 'thisOffset': 0, 'slots': ['0x00401000', '0x00401100']},
                                  {'address': '0x01010020', 'thisOffset': 24, 'slots': ['0x00401200']}]},
            'TESObjectREFR': {'bases': ['TESForm'], 'directBases': ['TESForm'], 'vtables': []},
            'TESForm': {'bases': [], 'directBases': [], 'vtables': []}}
        cls.map_path.write_text(json.dumps(data))
        cls.engine = fnv_engine.EngineMap(cls.map_path)
        game = root / 'game'
        (game / 'Data').mkdir(parents=True)
        cls.game = game
        cls.project = Project.model_validate({
            'project_id': 'fnvtest', 'launcher': 'native', 'game_root': str(game), 'plugin': 'Fixture.esp',
            'records': [{'editor_id': 'FixtureStatic', 'form_id': '01000800'}], 'state_dir': str(root / 'state'),
            'helper': str(root / 'helper.exe'), 'fnv': {'engine_map': str(cls.map_path)}})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class GeneratorTests(FnvTestBase):
    def test_map_contents(self):
        data = json.loads(self.map_path.read_text())
        self.assertEqual(data['game']['build'], '1.4.0.525')
        by = {f['address']: f for f in data['functions']}
        self.assertEqual(by['0x00401000']['status'], 'proven')
        self.assertEqual(by['0x00401100']['name'], 'Actor::ApplyLimbDamage')
        self.assertEqual(by['0x00401200']['status'], 'unnamed')
        self.assertEqual(by['0x00401000']['lines'], [5, 12])
        self.assertEqual(by['0x00401100']['lines'], [19, 22])   # skips the helper struct below the marker
        self.assertEqual(data['calls'], {'0x00401000': ['0x00401100']})
        self.assertIn('source', data['generated']['callGraph'])
        statics = {s['name']: s for s in data['statics']}
        self.assertEqual(statics['bAlwaysActive']['address'], '0x011DEED8')
        self.assertEqual(statics['bAlwaysActive']['setting'], 'bAlwaysActive:General')
        header = (self.map_path.parent / 'addresses.h').read_text()
        self.assertIn('#define FNV_ADDR_Actor_ApplyLimbDamage 0x00401100u', header)
        self.assertNotIn('FUN_00401200', header)

    def test_demangle_fallback_and_clean_names(self):
        self.assertEqual(demangle._fallback_name('??0Foo@Bar@@QAE@XZ'), 'Bar::Foo::Foo')
        self.assertEqual(demangle._fallback_name('??1Foo@@QAE@XZ'), 'Foo::~Foo')
        self.assertEqual(demangle._fallback_name('?Get@Foo@@QAEHXZ'), 'Foo::Get')
        self.assertEqual(demangle.demangle('@FUN_00ba3130@16')[0], 'FUN_00ba3130')
        self.assertEqual(demangle.clean_name('fnv_004302a0::ExtraDismemberedLimbs::FUN_004302a0'),
                         ('ExtraDismemberedLimbs::FUN_004302a0', True))
        self.assertEqual(demangle.clean_name('FUN_004303e0_Class::IsBitSet'), ('IsBitSet', False))

    def test_headers(self):
        classes = headers.parse_file(self.decomp / 'include' / 'Actor.h', 'include/Actor.h')
        self.assertEqual(len(classes), 1)
        actor = classes[0]
        self.assertEqual(actor['bases'], ['TESObjectREFR', 'ActorValueOwner'])
        self.assertEqual(actor['size'], 0x20)
        fields = {f['name']: f for f in actor['fields']}
        self.assertEqual(fields['m_fHealth']['offset'], 0x10)
        self.assertTrue(fields['m_fHealth'].get('checked'))
        self.assertEqual(fields['m_uiFlags']['offset'], 0xC)
        self.assertNotIn('checked', fields['m_uiFlags'])
        self.assertEqual([v['slot'] for v in actor['virtuals']], [0, 1])

    def test_source_locate_rejects_declarations(self):
        lines = ['// @fnv 0x00401000', 'void Foo();', 'int x;']
        self.assertIsNone(source.locate(lines, source.markers(lines), 0x401000))

    def test_plugin_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, 'nvse/CommandTable.h', '#define DEFINE_COMMAND_PLUGIN(name, refRequired, paramInfo) \\\n x\n')
            write(tmp, 'cmds.h', 'static ParamInfo kParams_OneString[1] = { {"text", kParamType_String, 0}, };\n'
                                 'DEFINE_COMMAND_PLUGIN(GetThing, 1, kParams_OneString);\n'
                                 'DEFINE_COMMAND_PLUGIN(DoOther, 0, kParams_Missing);\n')
            write(tmp, 'main.cpp', '\t/*2200*/REG_CMD_STR(GetThing);\n')
            out = {c['name']: c for c in commands.plugin(tmp, 'Test')}
        self.assertEqual(out['GetThing']['opcode'], '0x2200')
        self.assertEqual(out['GetThing']['returns'], 'string')
        self.assertTrue(out['GetThing']['needsParent'])
        self.assertEqual(out['GetThing']['params'], [{'label': 'text', 'type': 'String', 'optional': False}])
        self.assertEqual(out['DoOther']['paramsUnresolved'], 'kParams_Missing')


class EngineQueryTests(FnvTestBase):
    def test_find_by_name_words(self):
        total, results = self.engine.find_by_name('limb damage')
        self.assertEqual(total, 1)
        self.assertEqual(results[0]['address'], '0x00401100')
        self.assertEqual(results[0]['location'], 'src/actor.cpp:19')

    def test_find_in_code_and_resolve(self):
        total, results = self.engine.find_in_code('fCombatPlayerLimbDamageMult')
        self.assertEqual([r['address'] for r in results], ['0x00401100'])
        self.assertEqual(results[0]['matches'][0]['line'], 21)
        self.assertEqual(self.engine.resolve('Actor::ApplyLimbDamage'), 0x401100)
        self.assertEqual(self.engine.resolve('FUN_00401100'), 0x401100)
        self.assertEqual(self.engine.resolve('0x401105'), 0x401100)
        with self.assertRaises(fnv_engine.EngineMapError):
            self.engine.resolve('NoSuchFunction')

    def test_code_annotates_proven_names(self):
        code = self.engine.code(0x401000)
        self.assertIn('FUN_00401100/*Actor::ApplyLimbDamage*/', code['code'])
        self.assertTrue(code['exactExtent'])
        self.assertEqual(code['firstLine'], 3)   # starts at the marker
        self.assertIn('"}"', code['code'])

    def test_graph_and_symbolize(self):
        callers, _ = self.engine.walk(0x401100, 'callers')
        self.assertEqual([c['address'] for c in callers], ['0x00401000'])
        callees, _ = self.engine.walk(0x401000, 'callees', depth=2)
        self.assertEqual([c['address'] for c in callees], ['0x00401100'])
        s = self.engine.symbolize(0x401105)
        self.assertEqual((s['kind'], s['offset'], s['name']), ('function', '0x5', 'Actor::ApplyLimbDamage'))
        self.assertEqual(self.engine.symbolize(0x011DEED8)['name'], 'bAlwaysActive:General')
        mod = self.engine.symbolize(0x3B58A2AD, [{'name': 'jip_nvse', 'base': 0x3B580000, 'size': 0x80000}])
        self.assertEqual((mod['module'], mod['offset']), ('jip_nvse', '0xA2AD'))
        self.assertEqual(self.engine.symbolize(0x00401500)['kind'], 'unknown_exe_address')

    def test_service_envelopes(self):
        service = FnvService(self.project)
        r = service.func_find('limb damage')
        self.assertTrue(r['ok'])
        self.assertEqual(r['outcome'], 'unchanged')
        self.assertEqual(r['data']['results'][0]['name'], 'Actor::ApplyLimbDamage')
        r = service.func_find('fCombatPlayerLimbDamageMult')     # no name hit: auto falls back to code
        self.assertEqual(r['data']['mode'], 'code')
        r = service.name_status('Actor::ApplyLimbDamage')
        self.assertEqual(r['data']['verdict'], 'proven')
        self.assertTrue(r['data']['results'][0]['sourceKind'].startswith('community source'))
        r = service.name_status('ApplyDamage')
        self.assertEqual(r['data']['results'][0]['address'], '0x00401000')
        r = service.class_layout('actor', include_virtuals=True)
        self.assertEqual(r['data']['size'], '0x20')
        self.assertEqual(r['data']['fields'][1], {'offset': '0x10', 'name': 'm_fHealth', 'type': 'float',
                                                  'span': 4, 'checked': True})
        self.assertEqual(len(r['data']['virtuals']), 2)
        r = service.class_layout('NoSuchClass')
        self.assertFalse(r['ok'])
        self.assertEqual(r['error']['code'], 'NOT_FOUND')
        r = service.func_graph('0x00401100', 'callers', 1, 10)
        self.assertEqual(r['data']['count'], 1)
        r = service.symbolize(['0x00401105', 'zz'])
        self.assertEqual(r['data']['results'][1]['kind'], 'invalid')
        r = service.setting_info('bAlwaysActive')
        self.assertEqual(r['data']['results'][0]['address'], '0x011DEED8')
        r = service.command_info('GetAV')
        self.assertFalse(r['ok'])

    def test_rtti_vtables_and_tree(self):
        vt = self.engine.symbolize(0x01010004)
        self.assertEqual((vt['kind'], vt['class'], vt['slot']), ('vtable', 'Actor', 1))
        self.assertEqual(vt['slotFunction']['name'], 'Actor::ApplyLimbDamage')
        self.assertNotIn('slot', self.engine.symbolize(0x01010000))
        service = FnvService(self.project)
        r = service.class_vtable('actor')
        self.assertEqual(r['data']['vtable'], '0x01010000')
        self.assertEqual(r['data']['slots'][1]['function']['name'], 'Actor::ApplyLimbDamage')
        self.assertEqual(r['data']['slots'][1]['declared']['declaration'], 'virtual void ApplyDamage(float)')
        self.assertEqual(r['data']['otherVtables'], [{'address': '0x01010020', 'thisOffset': '0x18', 'slots': 1}])
        r = service.class_tree('TESForm')
        self.assertEqual(r['data']['directDerived'], ['TESObjectREFR'])
        self.assertEqual(r['data']['allDerived'], ['TESObjectREFR', 'Actor'])
        r = service.class_tree('Nope')
        self.assertEqual(r['error']['code'], 'NOT_FOUND')

    def test_missing_map_is_reported(self):
        project = self.project.model_copy(update={'fnv': None, 'state_dir': Path(self.tmp.name) / 'nowhere'})
        r = FnvService(project).func_find('anything')
        self.assertFalse(r['ok'])
        self.assertEqual(r['error']['code'], 'ENGINE_MAP_UNAVAILABLE')
        self.assertIn('engine_map.generate', r['data']['next'])


LOG5 = textwrap.dedent('''\
    Yvileapsis' Crash Logger version 5.17 at 2026-11-06 12:00:00
    If this file is empty, then your game didn't crash or something went so wrong even crash logger was useless! :snig:
    Topmost stack module is NOT ALWAYS the crash reason! Exercise caution when speculating!


    Exception: EXCEPTION_ACCESS_VIOLATION (C0000005)

    Thread: [FNV] Main

    Calltrace:
         ebp    |                   Address                 |                 Function                 |  Source
     0x001AF504 |                    FalloutNV (0x00401105) |                                          |
     0x001AF754 |                   TestPlugin (0x10001234) | HookThing+0x14                           | C:\\src\\hooks.cpp:12
     0x001AF794 |                    FalloutNV (0x00401010) |                                          |
     0x001AF810 |                   KERNELBASE (0x10161890) | SleepEx+0x50                             |

    Registry:
     REG |    Value   | DEREFERENCE INFO
     eax | 0x00000000 |
     esi | 0x1A143B40 | 0x01016D34 => Class: Character
    \t\t\t\t\tFull Name: "Test Raider"
    \t\t\t\t\tFormID: 01000ABC ("TestRaiderREF")
    \t\t\t\t\tPlugin: "MyMod.esp"
    \t\t\t\t\tCell:
    \t\t\t\t\t Full Name: None
    \t\t\t\t\t FormID: 000DADB7 ("Wilderness")
    \t\t\t\t\t Plugin: "FalloutNV.esm"
    \t\t\t\t\tModel:
    \t\t\t\t\t Model Path: "characters\\_male\\skeleton.nif"
     eip | 0x00401105 |

    Stack:
      # |    Value   | DEREFERENCE INFO
     01 | 0x001AF6A0 |
     16 | 0x382B0E00 | 0x010A4764 => Class: BSFile: Path: DATA\\textures\\test\\broken.dds

    --------------------------------------------------------------------------------

    Mods:
      # |                                        Mod                                       |                            Author
     00 | FalloutNV.esm                                                                    | ipely
     01 | MyMod.esp                                                                        | me

    --------------------------------------------------------------------------------

    Module bases:
          Address Range      |    Size    |                  Module                  |        Version       |                                 Filepath
     0x3B580000 - 0x3B600000 | 512.00 KiB | Test NVSE Plugin                         | 3                    | C:/Games/FNV/Data/NVSE/Plugins/TestPlugin.dll
     0x00400000 - 0x0147B000 |  16.48 MiB | FalloutNV                                | 1.4.0.525            | C:/Games/FNV/FalloutNV.exe
     0x75A00000 - 0x75C00000 |   2.00 MiB | KERNELBASE                               | 10.0                 | C:/WINDOWS/System32/KERNELBASE.dll

    --------------------------------------------------------------------------------

    Install: C:\\Games\\FNV
    ''')

LOG45 = textwrap.dedent('''\
    Joe Cobb's Crash Logger (NVSE) version 4.5 beta 4 at 2024-04-21 03:59:01.6393753

    Exception C0000005 caught!

    Thread: [FNV] BSTaskManagerThread

    Calltrace:
    0x7035F7E0 ==>            FalloutNV (0x00401105) : EntryPoint+0xFFFFFFFF
    0x7035F810 ==>           TestPlugin (0x10000010) : EntryPoint+0xFFFFFFFF

    Registry:
    REG |    VALUE   | DEREFERENCE INFO
    eax | 0x00000000 |
    esi | 0xFBC20000 | 0x010A4764 ==> Class: BSFile: Path: DATA\\textures\\lod\\x.dds

    Module bases:
    0x3B580000 - 0x3B600000 ==> TestPlugin,                 NVSE plugin version:    3, C:/Games/FNV/Data/NVSE/Plugins/TestPlugin.dll
    0x00400000 - 0x0147B000 ==> FalloutNV,                                             C:/Games/FNV/FalloutNV.exe
    ''')


class CrashTests(FnvTestBase):
    def test_parse_yvile5(self):
        p = fnv_crash.parse_text(LOG5)
        self.assertEqual(p['format'], 'yvile5')
        self.assertEqual(p['logger']['version'], '5.17')
        self.assertEqual(p['exception']['code'], 'C0000005')
        self.assertEqual(p['thread'], '[FNV] Main')
        self.assertEqual(len(p['calltrace']), 4)
        self.assertEqual(p['calltrace'][1]['symbol'], 'HookThing+0x14')
        self.assertEqual([m['name'] for m in p['mods']], ['FalloutNV.esm', 'MyMod.esp'])
        self.assertEqual(len(p['modules']), 3)
        esi = next(r for r in p['registers'] if r['register'] == 'esi')
        head, actor, cell = esi['objects']
        self.assertEqual(head['class'], 'Character')
        self.assertEqual(head['modelPath'], 'characters\\_male\\skeleton.nif')
        self.assertEqual((actor['formId'], actor['editorId'], actor['plugin']), ('01000ABC', 'TestRaiderREF', 'MyMod.esp'))
        self.assertEqual((cell['formId'], cell['role']), ('000DADB7', 'Cell'))
        self.assertEqual(p['install'], 'C:\\Games\\FNV')

    def test_explain_yvile5(self):
        e = fnv_crash.explain(fnv_crash.parse_text(LOG5), self.engine)
        frames = e['frames']
        self.assertEqual(frames[0]['function']['name'], 'Actor::ApplyLimbDamage')
        self.assertEqual(frames[0]['offset'], '0x5')
        self.assertEqual((frames[1]['kind'], frames[1]['offset'], frames[1]['absolute']),
                         ('nvse_plugin', '0x1234', '0x3B581234'))
        self.assertEqual(frames[2]['function']['name'], 'Actor::ApplyDamage')
        self.assertEqual(frames[3]['kind'], 'system')
        culprits = {(s['kind'], s['name']): s for s in e['likelyCulprits']}
        self.assertEqual(culprits[('module', 'Test NVSE Plugin')]['confidence'], 'medium')
        self.assertEqual(culprits[('plugin', 'MyMod.esp')]['confidence'], 'medium')
        self.assertIn(('asset', 'DATA\\textures\\test\\broken.dds'), culprits)
        self.assertIn(('engine_function', 'Actor::ApplyLimbDamage'), culprits)
        forms = {f['formId']: f for f in e['formsInvolved']}
        self.assertFalse(forms['01000ABC']['vanilla'])
        self.assertTrue(forms['000DADB7']['vanilla'])
        self.assertIn('meaning', e['exception'])

    def test_plugin_first_frame_is_high(self):
        log = '\n'.join(line for line in LOG5.split('\n') if not line.startswith(' 0x001AF504 |'))
        e = fnv_crash.explain(fnv_crash.parse_text(log), self.engine)
        top = e['likelyCulprits'][0]
        self.assertEqual((top['kind'], top['name'], top['confidence']), ('module', 'Test NVSE Plugin', 'high'))

    def test_parse_yvile45_order(self):
        p = fnv_crash.parse_text(LOG45)
        self.assertEqual(p['format'], 'yvile4')
        self.assertEqual(p['calltrace'][0]['address'], 0x401105)       # 4.5 prints ebp first
        e = fnv_crash.explain(p, self.engine)
        self.assertEqual(e['frames'][0]['function']['name'], 'Actor::ApplyLimbDamage')
        self.assertEqual((e['frames'][1]['offset'], e['frames'][1]['absolute']), ('0x10', '0x3B580010'))
        self.assertIn(('asset', 'DATA\\textures\\lod\\x.dds'), {(s['kind'], s['name']) for s in e['likelyCulprits']})

    def test_parse_yvile43_order(self):
        log = LOG45.replace('version 4.5 beta 4', 'version 4.3').replace(
            '0x7035F7E0 ==>            FalloutNV (0x00401105)', '0x00401105 ==>            FalloutNV (0x7035F7E0)')
        self.assertEqual(fnv_crash.parse_text(log)['calltrace'][0]['address'], 0x401105)

    def test_find_latest_skips_logs_without_crash(self):
        write(self.game, 'CrashLogger.log', "Yvileapsis' Crash Logger version 5.17 at 2026-11-06 12:00:00\n")
        old = write(self.game, 'Crash Logs/CrashLogger 2026-11-05.log', LOG5)
        self.assertEqual(fnv_crash.find_latest(self.project), old)
        service = FnvService(self.project)
        r = service.crash_explain()
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['data']['frames'][0]['function']['name'], 'Actor::ApplyLimbDamage')
        r = service.crash_latest(max_frames=5)
        self.assertEqual(r['data']['frameCount'], 4)
        r = service.crash_latest(path=str(self.game / 'missing.log'))
        self.assertEqual(r['error']['code'], 'CRASH_LOG_MISSING')

    def test_tool_catalog(self):
        self.assertEqual(len(FNV_TOOLS), 15)
        self.assertTrue(all(name.startswith('fnv_') for name in FNV_TOOLS))


if __name__ == '__main__':
    unittest.main()
