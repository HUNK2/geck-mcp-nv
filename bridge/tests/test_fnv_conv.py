"""Conventions helper of the action-style fnv_* tools (geck_mcp/fnv_conv.py)."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_conv  # noqa: E402


class FakeLive:
    def __init__(self):
        self.seen = []

    def resolve(self, ref):
        self.seen.append(ref)
        return {'ptr': 1, 'formId': '0001F00D', 'spec': ref}


class ConvTests(unittest.TestCase):
    def test_dispatch(self):
        table = {'read': lambda: 'r', 'clear': lambda: 'c'}
        self.assertEqual(fnv_conv.dispatch('read', table), 'r')
        with self.assertRaises(fnv_conv.ConvError) as caught:
            fnv_conv.dispatch('nope', table)
        self.assertEqual(caught.exception.code, 'UNKNOWN_ACTION')
        self.assertIn('clear, read', str(caught.exception))
        self.assertEqual(caught.exception.evidence, {'actions': ['clear', 'read']})

    def test_page(self):
        rows = [{'a': i, 'b': 'x', 'c': 'y', 'd': 'z'} for i in range(10)]
        first = fnv_conv.page(rows, 4, 0)
        self.assertEqual((first['total'], first['offset'], first['count'], first['more']), (10, 0, 4, True))
        last = fnv_conv.page(rows, 4, 8)
        self.assertEqual((last['count'], last['more']), (2, False))
        self.assertEqual(fnv_conv.page(rows, 2, 0, fields=['a', 'd'])['rows'][1], {'a': 1, 'd': 'z'})
        self.assertEqual(list(fnv_conv.page(rows, 2, 0, brief=True)['rows'][0]), ['a', 'b', 'c'])
        self.assertEqual(list(fnv_conv.page(rows, 2, 0, brief=True, brief_fields=['d'])['rows'][0]), ['d'])
        self.assertEqual(fnv_conv.page([], 5, 0), {'total': 0, 'offset': 0, 'count': 0, 'more': False, 'rows': []})
        self.assertEqual(fnv_conv.page(rows, 99999, -5)['count'], 10)

    def test_verdict_key_order(self):
        out = fnv_conv.verdict('37 warnings', 'because', count=37)
        self.assertEqual(list(out), ['verdict', 'reason', 'count'])

    def test_next_step_uses_existing_codes(self):
        self.assertIn('fnv_game launch', fnv_conv.next_step('NOT_RUNNING'))
        self.assertIn('fnv_companion_install', fnv_conv.next_step('COMPANION_NOT_INSTALLED'))
        self.assertIn('engine_control', fnv_conv.next_step('CONTROL_DISABLED'))
        self.assertIn('engine map', fnv_conv.next_step('ENGINE_MAP_UNAVAILABLE'))
        self.assertIsNone(fnv_conv.next_step('SOMETHING_ELSE'))
        error = fnv_conv.ConvError('NOT_RUNNING', 'The game is not running.')
        self.assertIn('fnv_game launch', str(error))

    def test_references_and_aliases(self):
        live, aliases = FakeLive(), fnv_conv.Aliases()
        resolved = fnv_conv.resolve_ref(live, 'Fixture.esp:000800', save_as='door', aliases=aliases)
        self.assertEqual(resolved['savedAs'], '$door')
        again = fnv_conv.resolve_ref(live, '$door', aliases=aliases)
        self.assertEqual(live.seen, ['Fixture.esp:000800', '0001F00D'])
        self.assertEqual(again['formId'], '0001F00D')
        fnv_conv.resolve_ref(live, 'player', aliases=aliases)
        with self.assertRaises(fnv_conv.ConvError) as caught:
            fnv_conv.resolve_ref(live, '$missing', aliases=aliases)
        self.assertEqual(caught.exception.code, 'REF_NOT_FOUND')
        with self.assertRaises(ValueError):
            aliases.save('bad name', 'x')


if __name__ == '__main__':
    unittest.main()
