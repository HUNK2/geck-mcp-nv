"""Offline tests for fnv_social: schedules, comparisons, option matching, condition reading and combining."""
import struct
import unittest

from geck_mcp import fnv_social
from geck_mcp.fnv_social import SocialTools, _compare, _match, _record_values, _schedule_ok


class FakeLive:
    """Just enough of LiveService for read_conditions / evaluate_conditions with a fake game."""

    def __init__(self, memory, lists, commands, handler_results):
        self.memory, self.lists = memory, lists
        self._engine = type('E', (), {'commands': commands})()
        self.link = FakeLink(self, handler_results)

    def engine(self):
        return self._engine

    def walk_list(self, head, limit=500):
        return self.lists.get(head, [])[:limit]

    def reads(self, spans):
        return [self.memory.get(a, b'\0' * n)[:n] if a in self.memory else None for a, n in spans]

    def values(self, expressions, ref=0):
        return [None for _ in expressions]

    def form_ids(self, pointers):
        return [{'formId': '%08X' % p, 'formType': 0, 'ptr': '0x%08X' % p, 'flags': 0} if p else None for p in pointers]

    class game:
        @staticmethod
        def process():
            return (1, 'FalloutNV.exe')


class FakeLink:
    def __init__(self, live, handler_results):
        self.live, self.results, self.slots = live, handler_results, {}

    def one(self, *op):
        return self.batch([op])[0]

    def batch(self, ops):
        out = []
        for op in ops:
            if op[0] == 'call' and op[1] == '%X' % fnv_social.FORM_HEAP_ALLOCATE:
                out.append({'ok': True, 'eax': '20000000'})
            elif op[0] == 'call':
                handler, slot = int(op[1], 16), int(op[-1], 16)
                ok, value = self.results[handler]
                self.slots[slot] = value
                out.append({'ok': True, 'eax': '1' if ok else '0'})
            elif op[0] == 'wr':
                out.append({'ok': True})
            elif op[0] == 'rd':
                out.append({'ok': True, 'hex': struct.pack('<d', self.slots.get(int(op[1], 16), 0.0)).hex()})
            else:
                out.append({'ok': False})
        return out


def condition(flags, value, function, p1=0, p2=0, run_on=0, ref=0):
    return struct.pack('<B3xfH2xIIiI', flags, value, function, p1, p2, run_on, ref)


class SocialOfflineTests(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(_compare('>=', 10.0, 10))
        self.assertFalse(_compare('<', 10.0, 10))
        self.assertTrue(_compare('!=', 1.0, 0))
        self.assertFalse(_compare('==', None, 1))

    def test_schedule(self):
        self.assertEqual(_schedule_ok(-1, -1, 0, -1, 0, 13.5, 4, 2, 3), (True, None))
        ok, why = _schedule_ok(-1, -1, 0, 8, 10, 19.0, 4, 2, 3)
        self.assertFalse(ok)
        self.assertIn('8:00', why)
        self.assertTrue(_schedule_ok(-1, -1, 0, 22, 8, 3.0, 4, 2, 3)[0])  # wraps past midnight
        self.assertFalse(_schedule_ok(-1, 8, 0, -1, 0, 12.0, 4, 2, 3)[0])  # weekends only, it is Wednesday
        self.assertTrue(_schedule_ok(-1, 7, 0, -1, 0, 12.0, 4, 2, 3)[0])   # weekdays
        self.assertFalse(_schedule_ok(5, -1, 0, -1, 0, 12.0, 4, 2, 3)[0])  # wrong month

    def test_match(self):
        options = [{'index': 0, 'prompt': 'Who are you?', 'info': '0010ABCD'},
                   {'index': 1, 'prompt': 'Goodbye.', 'info': '0010ABCE'}]
        self.assertEqual(_match(options, 1)['prompt'], 'Goodbye.')
        self.assertEqual(_match(options, '0')['index'], 0)
        self.assertEqual(_match(options, 'who are')['index'], 0)
        self.assertEqual(_match(options, '0x0010ABCE')['index'], 1)
        self.assertIsNone(_match(options, 'trade'))

    def test_record_values(self):
        record = {'type': 'WEAP', 'fields': [{'tag': 'FULL', 'value': '9mm Pistol'},
                                            {'tag': 'SCRI', 'form': {'formId': '00012345'}},
                                            {'tag': 'DATA', 'value': {'value': 75, 'health': 200, 'weight': 2.0,
                                                                      'damage': 12, 'clipSize': 13}}]}
        self.assertEqual(_record_values(record), {'name': '9mm Pistol', 'script': '00012345', 'value': 75,
                                                  'health': 200, 'weight': 2.0, 'damage': 12})

    def test_conditions_evaluated_and_combined(self):
        # GetStage (0x103A) >= 10  OR  GetIsID (0x1048) == 1, AND GetDead (0x102E... use 0x1046) == 0
        commands = [{'name': 'GetStage', 'opcode': '0x103A', 'table': 'script', 'record': '0x01000000',
                     'params': [{'label': 'Quest', 'typeId': 14}]},
                    {'name': 'GetIsID', 'opcode': '0x1048', 'table': 'script', 'record': '0x01000028',
                     'params': [{'label': 'ObjectID', 'typeId': 21}]},
                    {'name': 'GetDead', 'opcode': '0x102E', 'table': 'script', 'record': '0x01000050', 'params': []}]

        def record(handler, needs_ref):
            return b'\0' * 0x10 + bytes([needs_ref]) + b'\0' * 0xF + struct.pack('<I', handler)
        memory = {
            0x01000000: record(0x00500000, 0), 0x01000028: record(0x00500010, 1), 0x01000050: record(0x00500020, 1),
            0x30000000: condition(0x60 | 1, 10.0, 0x3A, p1=0x40000000),  # >= 10, OR
            0x30000020: condition(0x00, 1.0, 0x48, p1=0x40000010),        # == 1
            0x30000040: condition(0x00, 0.0, 0x2E),                       # == 0
        }
        lists = {0x50000000: [0x30000000, 0x30000020, 0x30000040]}
        handlers = {0x00500000: (True, 5.0), 0x00500010: (True, 1.0), 0x00500020: (True, 0.0)}
        tools = SocialTools(FakeLive(memory, lists, commands, handlers))
        result = tools.evaluate_conditions(0x50000000, 0x60000000, 0x70000000)
        c = result['conditions']
        self.assertEqual([x['function'] for x in c], ['GetStage', 'GetIsID', 'GetDead'])
        self.assertEqual([x['passed'] for x in c], [False, True, True])
        self.assertEqual(c[0]['comparison'], '>=')
        self.assertTrue(c[0]['or'])
        self.assertTrue(result['result'])  # (False OR True) AND True
        handlers[0x00500010] = (True, 0.0)
        self.assertFalse(tools.evaluate_conditions(0x50000000, 0x60000000, 0x70000000)['result'])

    def test_condition_needing_a_missing_reference_fails(self):
        commands = [{'name': 'GetDead', 'opcode': '0x102E', 'table': 'script', 'record': '0x01000050', 'params': []}]
        memory = {0x01000050: b'\0' * 0x10 + b'\1' + b'\0' * 0xF + struct.pack('<I', 0x00500020),
                  0x30000000: condition(0x00, 0.0, 0x2E, run_on=1)}  # runs on the target, which is absent
        tools = SocialTools(FakeLive(memory, {0x50: [0x30000000]}, commands, {}))
        result = tools.evaluate_conditions(0x50, 0x60000000, 0)
        self.assertFalse(result['result'])
        self.assertIn('needs a reference', result['conditions'][0]['why'])


if __name__ == '__main__':
    unittest.main()
