"""Live tests for the S1 high bugs (PLAN-3 S1): R1 coverage races, T1 the engine_control gate, R4 reload."""
import time
import unittest

from .base import LiveCase, Session


class S1Tests(LiveCase):
    def test_r1_cov_worker_threads(self):
        # Actor::GetLevel is called from three game threads; round 2 crashed the game here within seconds.
        self.ok('fnv_coverage', action='start', functions=['Actor::GetLevel'], name='live-r1')
        try:
            self.ok('fnv_wait_for', seconds=10, clock='real', timeout=60)
        finally:
            stopped = self.ok('fnv_coverage', action='stop')
        self.assertTrue(self.ok('fnv_status')['running'])
        self.assertGreaterEqual(stopped['functionsRun'][0]['linesRun'], 1)

    def test_t1_gate_read_project(self):
        # A second server over the same project with engine_control = "read": every mutating action is refused
        # before it reaches the game, so it can share the run without touching the pipe.
        reader = Session(self.session.project_path, engine_control='read')
        try:
            for tool, args in (('fnv_trace', {'function': 'Actor::GetLevel', 'action': 'start'}),
                               ('fnv_hooks', {'action': 'remove', 'hook_id': 'all'}),
                               ('fnv_test', {'action': 'steps', 'steps': [{'wait': 1}]}),
                               ('fnv_damage_log', {})):
                envelope = reader.call(tool, args)
                self.assertEqual((envelope.get('error') or {}).get('code'), 'CONTROL_DISABLED', (tool, envelope))
        finally:
            reader.close()
        self.assertEqual(self.ok('fnv_hooks')['hooks'], [])

    @unittest.skip('R4 is manual-assisted: restart the MCP server, start fnv_wait_for frames=3000 in the app, '
                   'cancel it, save a .py file; the wait must end normally and supervisor.log show the reload after it')
    def test_r4_cancel_then_reload(self):
        time.sleep(0)
