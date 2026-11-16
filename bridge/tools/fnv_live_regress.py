"""Live regression runner (PLAN-3 S4.2): the real tools against the running game, as a script, not an MCP tool.

    .venv/Scripts/python.exe tools/fnv_live_regress.py --attach --suite smoke

1. Preflight: the game runs in the project's dev profile, the companion it loaded is the one built from the current
   sources (state/engine/install.json), and fnv_selftest reports nothing broken.
2. The unittest live tests under tests/live (smoke: test_smoke.py; full: every file), with FNV_LIVE=1.
3. full only: the example scenarios through fnv_test, each with its expected outcome.
4. summary.json and summary.md in --out. Exit 0 only when everything passed and every expected failure failed.

One in-process MCP server serves the whole run (tests/live/base.py), so launch the game with fnv_game launch first and let
the MCP server go idle for 20 s: it then releases the game's single-instance pipe. --attach never launches."""
import argparse
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, str(BRIDGE / 'tests'))

# (scenario file, expected outcome, what it needs). expected: pass | fail | crash. The crash runs last: its relaunch
# fails when the runner itself cannot start the game (a sandboxed shell), and nothing after it would have a game.
SCENARIOS = [
    ('examples/fnv-tests/hello-wasteland.yaml', 'pass', None),
    ('examples/fnv-tests/broken-expectation.yaml', 'fail', None),
    ('examples/fnv-tests/agent_wave.yaml', 'pass', 'save:agentwave'),
    # The shipped HelloBug.esp keeps its bug (scenario S4 fixes it by a hot swap), so the quest never finishes.
    ('examples/hello-bug/tests/hello-bug-finishes.yaml', 'fail', None),
    ('examples/hello-quest/tests/hello-quest.yaml', 'pass', None),
    ('examples/fnv-tests/forced-crash.yaml', 'crash', None),
]


def preflight(session, attach, start):
    """Problems that make the run meaningless (empty list: go)."""
    from geck_mcp import fnv_link
    problems = []
    # The game's pipe takes one client: an MCP server that used it lets go after 20 s idle (EngineLink). Wait for
    # that, before any call through the session (whose own link would then hold the pipe), and say so when it does
    # not happen. Only when a game runs: without one there is nothing to wait for.
    from geck_mcp.fnv_game import game_processes
    if game_processes():
        probe = fnv_link.EngineLink(getattr(session.project.fnv, 'engine_pipe', None))
        try:
            probe.connect(wait=45)
        except fnv_link.LinkError as e:
            return ["cannot attach to the game's pipe (%s): another client holds it; stop it or let the MCP "
                    'server go idle for 20 s, then run again' % e], {}
        finally:
            probe.close()
    status = session.call('fnv_status')
    data = status.get('data') or {}
    if not data.get('running'):
        if attach:
            return ['the game is not running (--attach never launches): fnv_game launch first'], data
        launched = session.call('fnv_game', {'action': 'launch', 'start': start})
        if not launched.get('ok'):
            return ['launch failed: %s' % json.dumps(launched.get('error'))[:300]], data
        status = session.call('fnv_status')
        data = status.get('data') or {}
    install = Path(session.project.state_dir) / 'engine' / 'install.json'
    if not install.exists():
        problems.append('no state/engine/install.json: run fnv_companion_install (game closed)')
    else:
        receipt = json.loads(install.read_text(encoding='utf-8'))
        if receipt.get('source') != fnv_link.source_hashes():
            problems.append('the installed companion is stale: the C sources changed since build %s; '
                            'quit the game, fnv_companion_install, relaunch' % receipt.get('build_id'))
        target = Path(receipt.get('target', ''))
        if not target.is_file() or fnv_link.file_digest(target) != receipt.get('sha256'):
            problems.append('the DLL in the dev profile is not the one install.json records')
        launched_at = (data.get('session') or {}).get('launchedAt') or 0
        if receipt.get('installed_at') and launched_at and launched_at < receipt['installed_at']:
            problems.append('the game was launched before the companion was installed: relaunch it')
    selftest = session.call('fnv_selftest')
    rows = (selftest.get('data') or {}).get('rows') or (selftest.get('data') or {}).get('checks') or []
    broken = [r for r in rows if isinstance(r, dict) and r.get('state') == 'broken']
    if not selftest.get('ok'):
        problems.append('fnv_selftest failed: %s' % json.dumps(selftest.get('error'))[:300])
    elif broken:
        problems.append('fnv_selftest: %d broken: %s' % (len(broken), '; '.join(
            '%s/%s: %s' % (r.get('family'), r.get('check'), r.get('detail')) for r in broken[:5])))
    return problems, data


def run_tests(suite):
    loader = unittest.TestLoader()
    pattern = 'test_smoke.py' if suite == 'smoke' else 'test_*.py'
    tests = loader.discover(str(BRIDGE / 'tests' / 'live'), pattern=pattern, top_level_dir=str(BRIDGE / 'tests'))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(tests)
    rows = []

    def add(kind, items):
        for test, text in items:
            rows.append({'test': test.id(), 'result': kind, 'detail': text[-1500:] if text else None})
    add('failed', result.failures)
    add('error', result.errors)
    add('expected failure', result.expectedFailures)
    add('skipped', result.skipped)
    for test in result.unexpectedSuccesses:
        rows.append({'test': test.id(), 'result': 'unexpected success',
                     'detail': 'the bug looks fixed: remove its expectedFailure mark'})
    bad = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
    return {'ran': result.testsRun, 'bad': bad, 'rows': rows, 'log': stream.getvalue()[-20000:]}


def run_scenarios(session):
    rows = []
    saves = Path(session.project.fnv.mo2_root) / 'profiles' / session.project.fnv.mo2_profile / 'saves'
    gone = None
    for rel, expected, needs in SCENARIOS:
        if gone:
            rows.append({'scenario': rel, 'expected': expected, 'result': 'not run', 'detail': gone})
            continue
        if needs and needs.startswith('save:') and not (saves / (needs[5:] + '.fos')).exists():
            rows.append({'scenario': rel, 'expected': expected, 'result': 'skipped',
                         'detail': 'needs the test save %s' % needs[5:]})
            continue
        started = time.monotonic()
        try:
            envelope = session.call('fnv_test', {'path': str(BRIDGE / rel), 'relaunch_on_crash': True})
        except Exception as e:  # noqa: BLE001 - a timeout or a dead session: this row says so, the others stay
            envelope = {'ok': False, 'error': {'code': type(e).__name__, 'message': str(e)[:300]}, 'data': None}
        report = envelope.get('data') or {}
        got = 'crash' if report.get('crash') else 'pass' if report.get('passed') else 'fail'
        if not envelope.get('ok'):
            got = 'error'
        relaunched = report.get('relaunched')
        rows.append({'scenario': rel, 'expected': expected, 'result': got, 'ok': got == expected,
                     'seconds': round(time.monotonic() - started, 1), 'relaunched': relaunched,
                     'detail': (report.get('summary') if envelope.get('ok') else envelope.get('error')),
                     'failures': [{k: f.get(k) for k in ('phase', 'reason', 'code')}
                                  for f in (report.get('failures') or [])][:5]})
        if not (session.call('fnv_status').get('data') or {}).get('running'):
            gone = ('the game is not running after %s (relaunch: %s); launch it with fnv_game launch and run again'
                    % (rel, json.dumps(relaunched)[:200]))
    return rows


def write_summary(out, summary):
    out.mkdir(parents=True, exist_ok=True)
    (out / 'summary.json').write_text(json.dumps(summary, indent=2, default=str) + '\n', encoding='utf-8')
    lines = ['# Live regression run %s' % summary['stamp'], '',
             '- Suite: %s, project: %s' % (summary['suite'], summary['project']),
             '- Verdict: **%s**' % ('PASS' if summary['passed'] else 'FAIL'), '']
    if summary.get('preflight'):
        lines += ['## Preflight problems', ''] + ['- %s' % p for p in summary['preflight']] + ['']
    tests = summary.get('tests') or {}
    if tests:
        lines += ['## Tests: %d ran, %d bad' % (tests['ran'], tests['bad']), '']
        lines += ['- %s: %s' % (r['result'], r['test']) for r in tests['rows']] or ['- all passed']
        lines.append('')
    if summary.get('scenarios'):
        lines += ['## Scenarios', '', '| Scenario | Expected | Result |', '|---|---|---|']
        lines += ['| %s | %s | %s%s |' % (r['scenario'], r['expected'], r['result'],
                                           ': %s' % r['detail'] if r['result'] in ('not run', 'skipped', 'error')
                                           and r.get('detail') else '') for r in summary['scenarios']]
        lines.append('')
    (out / 'summary.md').write_text('\n'.join(lines), encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--project', default=str(BRIDGE / 'projects' / 'hellowasteland.windows.json'))
    parser.add_argument('--suite', choices=('smoke', 'full'), default='smoke')
    parser.add_argument('--attach', action='store_true', help='never launch the game')
    parser.add_argument('--start', default='coc:GoodspringsSource', help='where to launch when not attached')
    parser.add_argument('--out', default=None, help='default: <state_dir>/fnv/regress/<stamp>')
    args = parser.parse_args(argv)
    os.environ['FNV_LIVE'] = '1'
    os.environ['FNV_LIVE_PROJECT'] = str(Path(args.project).resolve())
    from live.base import Session
    session = Session.get(args.project)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    out = Path(args.out) if args.out else Path(session.project.state_dir) / 'fnv' / 'regress' / stamp
    summary = {'stamp': stamp, 'suite': args.suite, 'project': args.project, 'passed': False}
    started = time.monotonic()
    try:
        problems, status = preflight(session, args.attach, args.start)
        summary['preflight'] = problems
        summary['game'] = {k: status.get(k) for k in ('pid', 'phase', 'cell', 'cellName')}
        if not problems:
            summary['tests'] = run_tests(args.suite)
            if args.suite == 'full':
                summary['scenarios'] = run_scenarios(session)
            summary['passed'] = (summary['tests']['bad'] == 0
                                 and all(r.get('ok', r['result'] == 'skipped') for r in summary.get('scenarios', [])))
            if summary.get('scenarios') and summary['scenarios'][-1]['result'] == 'crash':
                summary['note'] = 'the last scenario crashed the game on purpose: relaunch it before the next run'
    finally:
        summary['seconds'] = round(time.monotonic() - started, 1)
        write_summary(out, summary)
        session.close()
    print('%s: %s (%s s) -> %s' % (args.suite, 'PASS' if summary['passed'] else 'FAIL', summary['seconds'], out))
    if summary.get('note'):
        print('  note: ' + summary['note'])
    for row in summary.get('scenarios') or []:
        if not row.get('ok', row['result'] == 'skipped'):
            print('  scenario %s: expected %s, got %s' % (row['scenario'], row['expected'], row['result']))
    for problem in summary.get('preflight') or []:
        print('  preflight: ' + problem)
    for row in (summary.get('tests') or {}).get('rows', []):
        if row['result'] not in ('skipped', 'expected failure'):
            print('  %s: %s' % (row['result'], row['test']))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
