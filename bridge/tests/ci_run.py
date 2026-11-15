"""Run the test suite the way CI does, then print why tests were skipped (release plan Phase 7, M13).

    python tests/ci_run.py [--require-engine]

With --require-engine the run fails when any engine companion test was skipped for want of Zig, so a green
Windows job means the companion was tested. Exit code 0 only when every test passed (skips allowed otherwise)."""
import argparse
import collections
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE_SKIP = 'needs Windows and Zig'


def is_engine_skip(reason):
    return str(reason).startswith(ENGINE_SKIP)


def verdict(result, require_engine):
    """(lines to print, exit code) for a finished unittest result."""
    reasons = collections.Counter(str(reason) for _, reason in result.skipped)
    lines = ['%d run, %d failed, %d errors, %d skipped' % (result.testsRun, len(result.failures),
                                                          len(result.errors), len(result.skipped))]
    lines += ['%5d  %s' % (count, reason) for reason, count in reasons.most_common()]
    code = 0 if result.wasSuccessful() else 1
    engine = sum(count for reason, count in reasons.items() if is_engine_skip(reason))
    if require_engine and engine:
        lines.append('FAILED: %d engine tests were skipped (Zig not found), and this run must test the engine '
                     'companion. Set FNV_MCP_ZIG.' % engine)
        code = 1
    return lines, code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--require-engine', action='store_true', help='fail when the engine tests are skipped')
    args = parser.parse_args(argv)
    sys.path.insert(0, str(HERE.parent))
    suite = unittest.defaultTestLoader.discover(str(HERE), top_level_dir=str(HERE))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    lines, code = verdict(result, args.require_engine)
    print('\n'.join(['', 'Skip summary:'] + lines), flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
