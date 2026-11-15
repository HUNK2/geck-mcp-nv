"""Release plan Phase 7 (M13): the CI runner prints why tests were skipped and fails a run that was meant to test
the engine companion but skipped it (Zig missing), so "green" cannot hide an untested companion."""
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ci_run  # noqa: E402
from engine_host import needs_zig  # noqa: E402

ZIG_REASON = 'needs Windows and Zig: set FNV_MCP_ZIG or fnv.zig in a projects/*.json file'


def run(*cases):
    suite = unittest.TestSuite(cases)
    return unittest.TextTestRunner(stream=io.StringIO(),
                                   verbosity=0).run(suite)


def cases():
    """Sample cases, made here so that discovery does not run them as part of the suite."""
    class Passes(unittest.TestCase):
        def test_ok(self):
            pass

    class SkipsEngine(unittest.TestCase):
        @unittest.skip(ZIG_REASON)
        def test_engine(self):
            pass

    class SkipsOther(unittest.TestCase):
        @unittest.skip('live only: FNV_LIVE=1')
        def test_live(self):
            pass

    class Fails(unittest.TestCase):
        def test_no(self):
            self.fail('x')
    return Passes, SkipsEngine, SkipsOther, Fails


Passes, SkipsEngine, SkipsOther, Fails = (None,) * 4


class CiRunTests(unittest.TestCase):
    def setUp(self):
        global Passes, SkipsEngine, SkipsOther, Fails
        Passes, SkipsEngine, SkipsOther, Fails = cases()

    def test_summary_counts_reasons(self):
        result = run(SkipsEngine('test_engine'), SkipsOther('test_live'), SkipsOther('test_live'))
        lines, code = ci_run.verdict(result, require_engine=False)
        self.assertEqual(code, 0)
        text = '\n'.join(lines)
        self.assertIn('3 skipped', text)
        self.assertIn('2  live only: FNV_LIVE=1', text)
        self.assertIn('1  ' + ZIG_REASON, text)

    def test_engine_skip_fails_when_required(self):
        result = run(Passes('test_ok'), SkipsEngine('test_engine'))
        lines, code = ci_run.verdict(result, require_engine=True)
        self.assertEqual(code, 1)
        self.assertIn('engine', lines[-1])

    def test_other_skips_pass_when_engine_required(self):
        result = run(Passes('test_ok'), SkipsOther('test_live'))
        self.assertEqual(ci_run.verdict(result, require_engine=True)[1], 0)

    def test_failure_fails(self):
        self.assertEqual(ci_run.verdict(run(Fails('test_no')), require_engine=False)[1], 1)

    def test_reason_matches_the_real_marker(self):
        # the runner recognises the reason the engine tests actually use
        self.assertTrue(ci_run.is_engine_skip(ZIG_REASON))
        if not getattr(needs_zig(lambda: None), '__unittest_skip__', False):
            return  # Zig is set up here, so the marker does not skip; the constant above is the text it would give
        self.assertEqual(needs_zig(lambda: None).__unittest_skip_why__, ZIG_REASON)


if __name__ == '__main__':
    unittest.main()
