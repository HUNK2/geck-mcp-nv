"""Live smoke suite: the basics every change must keep working, in under two minutes (PLAN-3 S4.2)."""
from .base import CAPS, LiveCase


class SmokeTests(LiveCase):
    def test_smoke_status_in_game(self):
        data = self.ok('fnv_status')
        self.assertTrue(data['running'])
        self.assertIn(data['phase'], ('in_game', 'paused_menu', 'in_menu'))
        self.assertTrue(data['companion']['pong'])
        self.assertEqual(data['companion']['wire'], 2)

    def test_smoke_player(self):
        data = self.ok('fnv_player')
        self.assertTrue(data.get('position') or data.get('pos'), data)

    def test_smoke_spawn_and_undo(self):
        spawned = self.ok('fnv_place', action='at', export=False, base=CAPS, count=1, distance=100)
        journal = self.ok('fnv_journal', limit=5, include_undone=False)
        self.assertTrue(journal['entries'], 'the spawn was not journaled')
        undone = self.ok('fnv_undo')
        self.assertTrue(undone, spawned)

    def test_smoke_place_and_undo(self):
        placed = self.ok('fnv_place', action='at', base=CAPS, near='player', offset=[0, 120, 20])
        self.assertTrue(placed)
        self.ok('fnv_undo')

    def test_smoke_time_pause_and_resume(self):
        self.ok('fnv_time', action='pause')
        status = self.ok('fnv_time', action='status')
        self.assertIn('paused', str(status).lower())
        self.ok('fnv_time', action='resume')
        self.ok('fnv_wait_for', frames=10, timeout=30)

    def test_smoke_hooks_empty(self):
        self.assertEqual(self.ok('fnv_hooks')['hooks'], [])
