"""Offline tests for fnv_agent: decomp signatures, trace decoding, crash signatures, RNG seeding, the calendar,
menus and the scenario runner's agent steps."""
import struct
import unittest

from geck_mcp import fnv_agent
from geck_mcp.fnv_agent import AgentTools, crash_signature, layout_args, parse_signature, param_kind
from geck_mcp.fnv_scenario import ScenarioRunner


class Signatures(unittest.TestCase):
    def test_thiscall(self):
        sig = parse_signature('public: bool __thiscall fnv_0089d6f0::ActorStandIn::FUN_0089d6f0(float,float,'
                              'class fnv_0089d6f0::ActorStandIn *)')
        self.assertEqual(sig['convention'], 'thiscall')
        self.assertEqual(sig['returns'], 'bool')
        args, words = layout_args(sig)
        self.assertEqual([a['kind'] for a in args], ['float', 'float', 'pointer'])
        self.assertEqual([a['index'] for a in args], [0, 1, 2])
        self.assertEqual(words, 3)

    def test_fastcall_registers_skip_floats_and_wide(self):
        args, words = layout_args(parse_signature('void __fastcall F(float,int,double,class X *,int)'))
        self.assertEqual([a['where'] for a in args], ['stack', 'ecx', 'stack', 'edx', 'stack'])
        self.assertEqual([a.get('index') for a in args], [0, None, 1, None, 3])
        self.assertEqual(words, 4)

    def test_no_params_and_struct_by_value(self):
        self.assertEqual(parse_signature('int __cdecl F(void)')['params'], [])
        args, _ = layout_args(parse_signature('void __cdecl F(int,struct NiPoint3,int)'))
        self.assertEqual([a['kind'] for a in args], ['int', 'struct'])
        self.assertIsNone(args[1]['where'])

    def test_templates_do_not_split(self):
        sig = parse_signature('void __thiscall A::F(class BSSimpleList<class X *,int> *,unsigned short)')
        self.assertEqual(len(sig['params']), 2)
        self.assertEqual(param_kind(sig['params'][1]), 'short')

    def test_decode(self):
        self.assertEqual(fnv_agent.decode_word('float', [struct.unpack('<I', struct.pack('<f', 2.5))[0]], 0), 2.5)
        lo, hi = struct.unpack('<II', struct.pack('<d', -1.25))
        self.assertEqual(fnv_agent.decode_word('double', [lo, hi], 0), -1.25)
        self.assertEqual(fnv_agent.decode_word('int', [0xFFFFFFFF], 0), -1)
        self.assertIs(fnv_agent.decode_word('bool', [0x100], 0), False)


class FakeEngine:
    functions = {}

    def symbolize(self, address):
        return {'kind': 'function', 'name': 'Caller::Fn', 'status': 'proven', 'offset': '0x10',
                'functionAddress': '0x%08X' % (address - 0x10)}


class FakeLive:
    def __init__(self):
        self.memory = {}
        self.journal_entries = []
        self.console_lines = []
        self.project = type('P', (), {'state_dir': '.'})()
        self.journal = type('J', (), {'add': lambda _, e: (self.journal_entries.append(e), {**e, 'id': len(self.journal_entries)})[1]})()

    def engine(self):
        return FakeEngine()

    def u32s(self, addresses):
        return [struct.unpack('<I', self.memory[a][:4])[0] if a in self.memory else 0 for a in addresses]

    def reads(self, spans):
        return [self.memory.get(a, b'\0' * n)[:n] if a in self.memory else None for a, n in spans]

    def rtti(self, pointers):
        return {p: {'class': 'Character' if p == 0x50000 else None, 'subobjectOffset': 0} for p in pointers}

    def form_ids(self, pointers):
        return [{'formId': '%08X' % p, 'formType': 0x2B, 'ptr': '0x%08X' % p, 'flags': 0} if p else None for p in pointers]

    def console(self, line, ref=0):
        self.console_lines.append(line)
        return {'accepted': True, 'output': []}


class FakeDeep:
    def __init__(self, records):
        self._records = records

    def records(self, h, since=0, limit=4096):
        return len(self._records), self._records, 0

    def describe_forms(self, ids, names=True):
        return {i: {'editorId': 'Npc%s' % i[-2:]} for i in ids}


def record(seq, ecx, caller, args, eax, frame=7):
    r = [0] * fnv_agent.RECORD_WORDS
    r[0], r[1], r[2], r[3], r[5], r[8], r[22] = seq, 2, 111, ecx, caller, eax, frame
    r[10] = 1000
    for k, a in enumerate(args):
        r[12 + k] = a
    return tuple(r)


class TraceDecoding(unittest.TestCase):
    def test_read_trace_decodes_and_counts(self):
        live = FakeLive()
        live.memory[0x9000] = struct.pack('<I', 2)        # calls counter
        f25 = struct.unpack('<I', struct.pack('<f', 25.0))[0]
        recs = [record(1, 0x50000, 0x00401234, [f25, 0, 0x50000], 1), record(2, 0x50000, 0x00401234, [f25, 0, 0], 0)]
        tools = AgentTools(live, FakeDeep(recs))
        tools._form_classes = {'Character'}
        sig = parse_signature('public: bool __thiscall A::F(float,float,class Actor *)')
        args, _ = layout_args(sig)
        h = {'callsAddress': '0x00009000', 'qpcFrequency': 10000000}
        out = tools._read_trace(h, 0x89D6F0, {'name': 'Actor::DamageHealthAndFatigue'}, 'sig', sig, args, 50, True)
        self.assertEqual(out['recorded'], 2)
        self.assertEqual(out['calls'][0]['args'][0]['value'], 25.0)
        self.assertEqual(out['calls'][0]['returned'], True)
        self.assertEqual(out['calls'][1]['returned'], False)
        self.assertEqual(out['calls'][0]['this']['formId'], '00050000')
        self.assertEqual(out['calls'][0]['args'][2]['object']['editorId'], 'Npc00')
        self.assertEqual(out['byCaller'][0]['count'], 2)
        self.assertEqual(out['byCaller'][0]['caller']['function'], 'Caller::Fn')


class Crashes(unittest.TestCase):
    def test_signature_uses_function_names(self):
        info = {'exception': {'code': 'C0000005'},
                'frames': [{'function': {'name': 'A::B'}}, {'module': 'jip.dll', 'offset': '0x10'}, {'absolute': '0x1'}]}
        sig, parts = crash_signature(info)
        self.assertEqual(sig, 'C0000005 | A::B < jip.dll+0x10 < 0x1')
        self.assertEqual(parts['frames'][0], 'A::B')


class Rng(unittest.TestCase):
    def test_seed_state_matches_mersenne_twister_seed(self):
        state, mti = AgentTools._seed_state(5)
        mt = struct.unpack('<624I', state)
        self.assertEqual(mt[0], 5)
        self.assertEqual(mt[1], (5 * 69069) & 0xFFFFFFFF)
        self.assertEqual(mt[623], (mt[622] * 69069) & 0xFFFFFFFF)
        self.assertEqual(struct.unpack('<I', mti)[0], 624)

    def test_seed_ops_cover_the_state(self):
        ops = AgentTools(FakeLive(), None)._seed_ops(0x1000, 1)
        written = sum(len(op[2]) // 2 for op in ops)
        self.assertEqual(written, 624 * 4 + 4)
        self.assertEqual(int(ops[-1][1], 16), 0x1000 + 0x9C8)


class Calendar(unittest.TestCase):
    def test_advance_rolls_day_month_year(self):
        live = FakeLive()
        written = []

        class Link:
            def write(self, address, data):
                written.append((address, data))

        class Tools(AgentTools):
            link = Link()

        tools = Tools(live, None)
        before = {'hour': 22.0, 'day': 31, 'month': 12, 'year': 2281, 'daysPassed': 10.0}
        out = tools._advance_globals(before, 5)
        self.assertIn('set GameHour to 3.0000', out['lines'])
        self.assertIn('set GameDay to 1', out['lines'])
        self.assertIn('set GameMonth to 0', out['lines'])
        self.assertIn('set GameYear to 2282', out['lines'])
        self.assertEqual(written[0][0], AgentTools.CALENDAR + 0x1C)
        self.assertIsNone(live.journal_entries[-1]["undo"])
        self.assertEqual(live.journal_entries[-1]["previous"][0], "set GameHour to 22.0000")


class Scenario(unittest.TestCase):
    def runner(self):
        r = ScenarioRunner.__new__(ScenarioRunner)
        r.vars = {'npc': '0A000801'}
        return r

    def test_subst(self):
        r = self.runner()
        self.assertEqual(r._subst({'ref': '$npc', 'list': ['$npc.GetAV Health', 3]}),
                         {'ref': '0A000801', 'list': ['0A000801.GetAV Health', 3]})

    def test_test_save_start(self):
        r = self.runner()
        self.assertEqual(r._start({'start': {'test_save': 'arena'}}), 'save:fnvmcp_test_arena')

    def test_agent_keys_accepted(self):
        r = self.runner()
        r.live = type('L', (), {'project': type('P', (), {'fnv': None})()})()
        data = r.load(text='name: t\nguard: {message_boxes: keep}\ntrace_script: MyQuest\nallow_menus: false\n'
                           'steps:\n  - spawn: {form: X, as: npc}\n')
        self.assertEqual(data['guard'], {'message_boxes': 'keep'})
        self.assertEqual(data['trace_script'], 'MyQuest')


if __name__ == '__main__':
    unittest.main()
