"""Live tests for the round-2 bugs found in the game (BUG-LOG Round 2, G entries). Each fails until its fix lands:
the open ones carry @expectedFailure with the bug id; the fix's commit removes the mark (PLAN-3 S4.2).

A test whose unfixed behaviour would break the rest of the run (G14 quits the game) is skipped until the fix."""
import json
import unittest

from .base import CAPS, LiveCase


class Round2Tests(LiveCase):
    def test_g1_nudge_undo_export_pose(self):
        # The export (fnv_author) reads the pose fnv_place list shows. After nudge + undo it must be the pose from
        # before the nudge. (The live position is not compared: a placed item settles under physics.)
        placed = self.ok('fnv_place', action='at', base=CAPS, near='player', offset=[0, 150, 20])
        ref = placed['ref']

        def pose():
            listed = self.ok('fnv_place', action='list')['placed']
            return next(r for r in listed if r['ref'] == ref)['position']
        before = pose()
        self.ok('fnv_place', action='nudge', ref=ref, offset=[0, 0, 50])
        self.ok('fnv_undo')
        self.assertAlmostEqual(pose()[2], before[2], delta=1, msg='the export pose kept the undone nudge')

    # G4: notUndoable is this game's {count, newest 50 ids} (S2.6); results are capped at 200 (S5.3)
    def test_g4_revert_all_reply_small(self):
        self.ok('fnv_place', action='at', export=False, base=CAPS, count=1, distance=100)
        envelope = self.call('fnv_undo', all=True)
        self.assertTrue(envelope.get('ok'), envelope.get('error'))
        self.assertLess(envelope['_size'], 20000, 'revert_all reply of %d characters' % envelope['_size'])

    def test_g5_wait_refuses_paused_clock(self):
        self.ok('fnv_time', action='pause')
        try:
            envelope = self.call('fnv_wait_for', seconds=1, clock='game', timeout=10)
            met = (envelope.get('data') or {}).get('met')
            self.assertFalse(envelope.get('ok') and met, 'game seconds "met" while the game clock was paused')
        finally:
            self.ok('fnv_time', action='resume')

    def test_g9_gamehour_fraction(self):
        before = self.ok('fnv_globals', query='GameHour')
        try:
            self.ok('fnv_globals', set={'GameHour': 9.5})
            out = self.ok('fnv_console', line='GetGlobalValue GameHour')
            self.assertIn('9.5', json.dumps(out), 'GameHour was rounded on write')
        finally:
            self.call('fnv_undo')
        self.assertTrue(before)

    def test_g11_hook_limit_counts(self):
        self.ok('fnv_hook_set', function='Actor::GetLevel', action='result', value=7, value_type='int', limit=5)
        try:
            self.ok('fnv_wait_for', frames=120, timeout=30)
            read = self.ok('fnv_hook_set', function='Actor::GetLevel', action='read')
            # S6.4: actionHits counts every call, actionApplied only the calls the action changed
            applied = read['hook']['actionApplied']
            self.assertLessEqual(applied, 5, 'actionApplied %s past a limit of 5' % applied)
            calls = read.get('calls') or []
            self.assertLessEqual(sum(1 for c in calls if c.get('applied')), 5)
            self.assertIn('of the %d calls listed' % len(calls), read['resultNote'])
        finally:
            self.call('fnv_hook_set', function='Actor::GetLevel', action='remove')

    def test_g12_mem_write_range(self):
        self.assertEqual(self.code('fnv_mem_write', target='player', value=300, as_type='u8'),
                         'INVALID_ARGUMENT')

    def test_g13_break_clear_name(self):
        self.ok('fnv_debug', action='break', op='set', location='Actor::GetLevel', thread='any')
        try:
            status = {}
            for _ in range(20):
                status = self.ok('fnv_debug', action='status')
                if status.get('stopped') or status.get('stops'):
                    break
                self.ok('fnv_wait_for', frames=5, timeout=10)
            self.ok('fnv_debug', action='break', op='clear')
            text = json.dumps(self.ok('fnv_debug', action='status'))
            self.assertNotIn('break 0', text)
        finally:
            self.call('fnv_debug', action='break', op='clear')
            self.call('fnv_debug', action='continue')

    def test_g14_console_qqq_refused(self):
        self.assertEqual(self.code('fnv_console', line='qqq'), 'CONSOLE_REFUSED')
        self.assertTrue(self.ok('fnv_status')['running'])

    def test_g16_engine_get_arg_count(self):
        self.assertEqual(self.code('fnv_engine_get', function='Actor::GetLevel', this='player', args=[1, 2, 3]),
                         'BAD_ARGUMENTS')
