"""Repeatable YAML scenario tests for the running game (fnv-mcp PLAN.md section 7, milestone 4).

    name: hello wasteland gives caps
    start: continue                     # menu | continue | {save: name} | {coc: CellEditorID}
    plugins: [HelloWasteland.esp]       # must be loaded
    steps:
      - run: player.additem f 100       # console line; fails on rejection or script errors
      - wait: {until: "player.GetItemCount f >= 100", timeout: 5}
      - expect: {item_count: f, equals: 100, ref: player}
      - expect: {no_script_errors: true}
    teardown:
      - revert_all: true                # undo journaled changes made by this test

Agent steps (fnv_agent): spawn {form, near, as: name} ($name in later steps), move {ref, to|cell|position}, save /
load (test saves), menu {press: label|index} | {close: true}, screenshot, watch {items, seconds, until}, trace
{function, filter} (then expect {called: function, at_least: 1}), time {...}, rng {seed}. Agent checks: called,
condition {form, list, subject}, message_box (text substring), marker (quest), menu_open (menu name).
Scenario keys: guard {message_boxes: close|keep} (keep: leave message boxes for the test to press), trace_script
(one script or quest whose statements are traced and reported), start {test_save: name}.

A run launches the game when needed, applies `start`, runs the steps and returns a structured report.
When the game dies, the report carries the crash doctor's diagnosis and the game is relaunched.
Files are a YAML subset (block mappings and lists, flow {} and [], quoted or plain scalars, comments) or JSON.
"""
import json
import math
import re
import time
from pathlib import Path

from . import fnv_crash, fnv_link
from .fnv_game import GameError, game_processes
from .fnv_link import LinkError


class ScenarioError(Exception):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


# ====================================================================== YAML subset
def _strip_comment(line):
    quote = None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in '"\'':
            quote = c
        elif c == '#' and (i == 0 or line[i - 1] in ' \t'):
            return line[:i].rstrip()
    return line.rstrip()


def _scalar(text):
    text = text.strip()
    if not text:
        return None
    if text[0] in '"\'' and text[-1] == text[0] and len(text) > 1:
        body = text[1:-1]
        return body.replace("''", "'") if text[0] == "'" else json.loads('"%s"' % body.replace('\\\'', '\''))
    lowered = text.lower()
    if lowered in ('true', 'yes', 'on'):
        return True
    if lowered in ('false', 'no', 'off'):
        return False
    if lowered in ('null', '~'):
        return None
    if re.fullmatch(r'0\d+', text):
        return text   # leading zero: a form id (00138562), kept as written
    if re.fullmatch(r'[-+]?\d+', text):
        return int(text)
    if re.fullmatch(r'[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?', text):
        return float(text)
    if re.fullmatch(r'0x[0-9A-Fa-f]+', text):
        return text
    return text


def _flow(text, i=0):
    """Parse a flow value ({...}, [...] or scalar) starting at text[i]; returns (value, next index)."""
    while i < len(text) and text[i] == ' ':
        i += 1
    if i < len(text) and text[i] in '{[':
        closing = '}' if text[i] == '{' else ']'
        mapping = text[i] == '{'
        out = {} if mapping else []
        i += 1
        while True:
            while i < len(text) and text[i] in ' ,':
                i += 1
            if i >= len(text):
                raise ScenarioError('YAML', 'Unclosed %s' % ('{' if mapping else '['))
            if text[i] == closing:
                return out, i + 1
            if mapping:
                key, i = _flow_token(text, i, ':')
                if i >= len(text) or text[i] != ':':
                    raise ScenarioError('YAML', 'Expected ":" after %r' % key)
                value, i = _flow(text, i + 1)
                out[str(_scalar(key))] = value
            else:
                value, i = _flow(text, i)
                out.append(value)
    token, i = _flow_token(text, i, ',}]')
    return _scalar(token), i


def _flow_token(text, i, stops):
    start = i
    if i < len(text) and text[i] in '"\'':
        quote = text[i]
        i += 1
        while i < len(text) and text[i] != quote:
            i += 2 if text[i] == '\\' and quote == '"' else 1
        return text[start:i + 1], i + 1
    while i < len(text) and text[i] not in stops:
        if text[i] == ':' and ':' in stops and (i + 1 >= len(text) or text[i + 1] in ' ,}]'):
            break
        i += 1
    return text[start:i].strip(), i


def parse_yaml(text):
    stripped = text.lstrip()
    if stripped.startswith('{') or stripped.startswith('['):
        try:
            return json.loads(text)
        except ValueError:
            pass
    lines = []
    for number, raw in enumerate(text.splitlines(), 1):
        if '\t' in raw[:len(raw) - len(raw.lstrip())]:
            raise ScenarioError('YAML', 'Line %d: indent with spaces, not tabs' % number)
        line = _strip_comment(raw)
        if line.strip() and line.strip() != '---':
            lines.append((len(line) - len(line.lstrip(' ')), line.strip(), number))
    value, index = _block(lines, 0, lines[0][0] if lines else 0)
    if index != len(lines):
        raise ScenarioError('YAML', 'Line %d: unexpected indentation' % lines[index][2])
    return value


def _value(text, lines, index, indent, number):
    """The value after 'key:' or '- ': inline, or a nested block on the next lines."""
    if text:
        if text[0] in '{[':
            value, end = _flow(text)
            if text[end:].strip():
                raise ScenarioError('YAML', 'Line %d: text after a flow value' % number)
            return value, index
        if text in ('|', '>'):
            block, j = [], index
            while j < len(lines) and lines[j][0] > indent:
                block.append(lines[j][1])
                j += 1
            return ('\n' if text == '|' else ' ').join(block), j
        return _scalar(text), index
    if index < len(lines) and lines[index][0] > indent:
        return _block(lines, index, lines[index][0])
    if index < len(lines) and lines[index][0] == indent and lines[index][1].startswith('- '):
        return _block(lines, index, indent)   # a list at the same indent as its key
    return None, index


def _block(lines, index, indent):
    if index >= len(lines):
        return None, index
    if lines[index][1].startswith('- ') or lines[index][1] == '-':
        out = []
        while index < len(lines) and lines[index][0] == indent and (lines[index][1].startswith('- ') or lines[index][1] == '-'):
            _, text, number = lines[index]
            item = text[2:].strip() if text != '-' else ''
            index += 1
            m = re.match(r'^([A-Za-z_][\w .-]*?)\s*:(\s+|$)(.*)$', item)
            if item.startswith('- ') or item == '-':
                # "- - x": a list inside a list item
                child = indent + 2
                synthetic = [(child, item, number)]
                j = index
                while j < len(lines) and lines[j][0] > indent:
                    synthetic.append(lines[j])
                    j += 1
                value, used = _block(synthetic, 0, child)
                out.append(value)
                index = j
            elif m and not item.startswith(('"', "'", '{', '[')):
                # "- key: value" starts a mapping whose other keys are indented under the item
                child = indent + 2
                synthetic = [(child, item, number)]
                j = index
                while j < len(lines) and lines[j][0] > indent:
                    synthetic.append(lines[j])
                    j += 1
                value, used = _block(synthetic, 0, child)
                if used != len(synthetic):
                    raise ScenarioError('YAML', 'Line %d: bad indentation in list item' % synthetic[used][2])
                out.append(value)
                index = j
            else:
                value, index = _value(item, lines, index, indent, number)
                out.append(value)
        return out, index
    out = {}
    while index < len(lines) and lines[index][0] == indent:
        _, text, number = lines[index]
        m = re.match(r'^("[^"]*"|\'[^\']*\'|[^:]+?)\s*:(\s+|$)(.*)$', text)
        if not m:
            raise ScenarioError('YAML', 'Line %d: expected "key: value"' % number)
        key = str(_scalar(m.group(1)))
        value, index = _value(m.group(3).strip(), lines, index + 1, indent, number)
        out[key] = value
    return out, index


# ====================================================================== runner
COMPARISONS = ('equals', 'not_equals', 'gt', 'ge', 'lt', 'le', 'approx', 'contains', 'is_true', 'is_false')


def compare(actual, spec):
    """(passed, description) for the comparison keys in spec."""
    checks = [k for k in COMPARISONS if k in spec]
    if not checks:
        return bool(actual), 'is truthy'
    for key in checks:
        want = spec[key]
        if key == 'equals':
            ok = _equal(actual, want)
        elif key == 'not_equals':
            ok = not _equal(actual, want)
        elif key == 'approx':
            ok = actual is not None and abs(float(actual) - float(want)) <= float(spec.get('tolerance', 0.01))
        elif key == 'contains':
            ok = actual is not None and str(want).lower() in (
                '\n'.join(actual) if isinstance(actual, list) else str(actual)).lower()
        elif key == 'is_true':
            ok = bool(actual) == bool(want)
        elif key == 'is_false':
            ok = (not actual) == bool(want)
        else:
            if actual is None:
                return False, '%s %s (value missing)' % (key, want)
            ok = {'gt': actual > want, 'ge': actual >= want, 'lt': actual < want, 'le': actual <= want}[key]
        if not ok:
            return False, '%s %r' % (key, want)
    return True, ' and '.join('%s %r' % (k, spec[k]) for k in checks)


def _equal(actual, want):
    if isinstance(want, bool):
        return bool(actual) == want
    if isinstance(want, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(actual, want, rel_tol=0, abs_tol=1e-6)
    if isinstance(want, str) and isinstance(actual, str):
        return actual.upper().removeprefix('0X') == want.upper().removeprefix('0X') or actual == want
    return actual == want


class ScenarioRunner:
    def __init__(self, live):
        self.live = live
        self.game = live.game
        self.vars = {}

    @property
    def agent(self):
        agent = getattr(self.live, 'agent', None)
        if agent is None:
            from .fnv_agent import AgentTools
            from .fnv_data import DataTools
            from .fnv_deep import DeepTools
            from .fnv_social import SocialTools
            deep = getattr(self.live, 'deep', None) or DeepTools(self.live)
            self.live.deep = deep
            data = DataTools(self.live.project, self.live)
            agent = AgentTools(self.live, deep, SocialTools(self.live, data), data)
            self.live.agent = agent
        return agent

    # ------------------------------------------------------------ loading
    def load(self, path=None, text=None):
        if text is None:
            p = Path(path)
            if not p.is_file():
                tests = getattr(self.live.project.fnv, 'tests_dir', None)
                if tests and (Path(tests) / path).is_file():
                    p = Path(tests) / path
                else:
                    raise ScenarioError('NOT_FOUND', 'No scenario file %s' % path)
            text = p.read_text(encoding='utf-8')
            source = str(p)
        else:
            source = '<inline>'
        data = parse_yaml(text)
        if not isinstance(data, dict) or not isinstance(data.get('steps'), list):
            raise ScenarioError('BAD_SCENARIO', 'A scenario is a mapping with a "steps" list')
        unknown = set(data) - {'name', 'start', 'plugins', 'plugin', 'steps', 'setup', 'teardown', 'restart',
                               'continue_on_failure', 'relaunch_on_crash', 'timeout', 'description', 'tags',
                               'allow_menus', 'guard', 'trace_script'}
        if unknown:
            raise ScenarioError('BAD_SCENARIO', 'Unknown scenario keys: %s' % ', '.join(sorted(unknown)))
        data.setdefault('name', Path(source).stem if source != '<inline>' else 'inline scenario')
        data['_source'] = source
        return data

    # ------------------------------------------------------------ run
    def run(self, path=None, text=None, relaunch=True):
        scenario = self.load(path, text)
        started = time.time()
        report = {'name': scenario['name'], 'source': scenario['_source'], 'passed': False, 'steps': [],
                  'setup': [], 'teardown': [], 'failures': [], 'crash': None, 'relaunched': None}
        unpause = None
        self.vars = {}
        self._marker = None
        traced = None
        try:
            report['game'] = self.prepare(scenario)
            guard = scenario.get('guard') or {}
            if not scenario.get('allow_menus'):
                # The game opens its pause menu when its window loses focus; keep the world running while the
                # test runs (the companion closes the pause menu, and message boxes unless guard keeps them).
                keep = guard.get('message_boxes', 'close') == 'keep'
                unpause = self.game.keep_unpaused(int(scenario.get('timeout', 240)) + 120, close_messages=not keep)
                report['game']['keptUnpaused'] = unpause is not None
                report['game']['messageBoxes'] = 'kept for the test' if keep else 'closed automatically'
            if scenario.get('trace_script'):
                try:
                    self.live.deep.script_trace(scenario['trace_script'], 'start')
                    traced = scenario['trace_script']
                except Exception as e:
                    report['game']['traceScript'] = 'not started: %s' % e
            marker = self.mark()
            self._marker = marker
            phases = [('setup', scenario.get('setup') or []), ('steps', scenario['steps'])]
            failed = False
            for phase, steps in phases:
                for number, step in enumerate(steps, 1):
                    if failed and not scenario.get('continue_on_failure'):
                        report[phase].append({'step': number, 'skipped': True, 'spec': step})
                        continue
                    result = self.step(step, marker)
                    result['step'] = number
                    report[phase].append(result)
                    if not result['passed']:
                        failed = True
                        report['failures'].append({'phase': phase, 'step': number, 'spec': step,
                                                   'reason': result.get('reason'), 'actual': result.get('actual')})
            report['scriptErrors'] = self.live.script_errors('last')['errors'] if not report['crash'] else None
            report['passed'] = not failed
            if traced:
                report['scriptTrace'] = self._trace_summary(traced)
            if failed:
                report['debug'] = self.debug_bundle(scenario)
        except _Crash as crash:
            report['crash'] = crash.diagnosis
            report['failures'].append({'phase': 'run', 'reason': 'the game crashed or quit: %s' % crash.reason})
            if relaunch and scenario.get('relaunch_on_crash', True):
                try:
                    self.game.link.close()
                    report['relaunched'] = self.game.launch(self._start(scenario), timeout=240)
                except GameError as e:
                    report['relaunched'] = {'failed': True, 'code': e.code, 'message': str(e)}
        except (GameError, LinkError, ScenarioError) as e:
            report['failures'].append({'phase': 'prepare', 'reason': str(e), 'code': getattr(e, 'code', None),
                                       'evidence': getattr(e, 'evidence', None)})
        if not report['crash']:
            for number, step in enumerate(scenario.get('teardown') or [], 1):
                try:
                    result = self.step(step, None)
                except _Crash as crash:
                    report['crash'] = crash.diagnosis
                    break
                result['step'] = number
                report['teardown'].append(result)
        if not report['crash']:
            self.game.stop_task(unpause)
        if not report['passed'] and not report['crash'] and not (report.get('debug') or {}).get('screenshot'):
            report['screenshot'] = self.screenshot(scenario['name'])
        report['seconds'] = round(time.time() - started, 1)
        report['summary'] = '%s: %s (%d steps, %d failed)%s' % (
            scenario['name'], 'PASSED' if report['passed'] else 'FAILED', len(report['steps']), len(report['failures']),
            ', crashed' if report['crash'] else '')
        self.save_report(report)
        return report

    def run_all(self, folder=None, pattern='*.yaml'):
        root = Path(folder or getattr(self.live.project.fnv, 'tests_dir', None) or '.')
        files = sorted(p for ext in ('*.yaml', '*.yml', '*.json') for p in root.glob(ext)) if pattern == '*.yaml' \
            else sorted(root.glob(pattern))
        if not files:
            raise ScenarioError('NO_TESTS', 'No scenario files in %s' % root)
        reports = [self.run(str(f)) for f in files]
        return {'folder': str(root), 'total': len(reports), 'passed': sum(r['passed'] for r in reports),
                'failed': [r['name'] for r in reports if not r['passed']],
                'summaries': [r['summary'] for r in reports],
                'details': [r for r in reports if not r['passed']]}

    def _start(self, scenario):
        start = scenario.get('start', 'menu')
        if isinstance(start, dict):
            if 'save' in start:
                return 'save:%s' % start['save']
            if 'coc' in start:
                return 'coc:%s' % start['coc']
            if 'test_save' in start:
                from .fnv_agent import TEST_SAVE_PREFIX
                return 'save:%s%s' % (TEST_SAVE_PREFIX, start['test_save'])
            raise ScenarioError('BAD_SCENARIO', 'start is menu, continue, {save: name}, {test_save: name} or {coc: cell}')
        return start

    def prepare(self, scenario):
        start = self._start(scenario)
        proc = self.game.process()
        if scenario.get('restart') and proc:
            self.game.quit()
            proc = None
        if not proc:
            launched = self.game.launch(start, timeout=int(scenario.get('timeout', 240)))
            info = {'launched': True, **launched}
        else:
            ping = self.game.ping()
            if ping.get('error'):
                raise ScenarioError('COMPANION_UNREACHABLE', 'The game runs but the engine companion does not answer')
            info = {'launched': False, 'pid': proc[0]}
            if start not in ('menu', None):
                kind, value = start.split(':', 1) if ':' in start else (start, None)
                if kind == 'continue':
                    from .fnv_game import parse_start
                    kind, value = parse_start('continue', self.game.saves_dir)
                line = 'load "%s"' % value if kind == 'save' else 'coc %s' % value
                if kind == 'coc':  # the closer goes in the same request: once the cell loads the main loop is busy
                    first, _ = self.game.link.batch([('every', '12C', '%X' % 90000, fnv_link.hexs(self.game.ARRIVAL_CLOSER)),
                                                     ('sched', '0', fnv_link.hexs(line))])
                    info['arrivalMessages'] = {'closing': bool(first.get('ok')), 'seconds': 90}
                else:
                    self.game.link.schedule(0, line)
                info['started'] = self.game.wait_in_game(time.monotonic() + 240, proc[0], start)
        try:  # the pause menu opens when the game window loses focus; steps need the world running
            if fnv_link.value_of(self.game.link.eval('GetActiveMenuMode')) == 1013:
                self.game.link.console('CloseActiveMenu')
                info['closedPauseMenu'] = True
        except Exception:
            pass
        wanted = scenario.get('plugins') or ([scenario['plugin']] if scenario.get('plugin') else [])
        for plugin in wanted:
            state = self.live.formid_live(plugin)
            if not state['loaded']:
                raise ScenarioError('PLUGIN_NOT_LOADED', '%s is not loaded in the dev profile (fnv_install)' % plugin)
        info['plugins'] = wanted
        if scenario.get('crash_context', True):
            # Script hooks so a crash during the test names the running script (fnv_crash_context).
            try:
                from .fnv_deep import DeepTools
                deep = getattr(self.live, 'deep', None) or DeepTools(self.live)
                self.live.deep = deep
                deep.crash_context('enable')
                info['crashContext'] = True
            except Exception as e:  # older companion without hooks: the test still runs
                info['crashContext'] = 'off (%s)' % getattr(e, 'code', e)
        return info

    def mark(self):
        self.live.script_errors('last')   # move the console cursor to now
        return {'journal': len(self.live.journal.entries()), 'events': (self.live.link.events(0) or {}).get('seq', 0),
                'time': time.time()}

    # ------------------------------------------------------------ steps
    def step(self, spec, marker):
        started = time.monotonic()
        if not isinstance(spec, dict) or len([k for k in spec if k not in ('note', 'name')]) != 1:
            return {'passed': False, 'reason': 'each step is a mapping with one action key', 'spec': spec}
        action = next(k for k in spec if k not in ('note', 'name'))
        arg = self._subst(spec[action])
        try:
            result = self._dispatch(action, arg, marker)
        except (LinkError, GameError) as e:
            if self._dead(e):
                raise _Crash(str(e), self.diagnose())
            result = {'passed': False, 'reason': '%s: %s' % (getattr(e, 'code', 'ERROR'), e)}
        except ScenarioError as e:
            result = {'passed': False, 'reason': str(e)}
        except Exception as e:  # a tool error is a failed step, not a crashed runner
            if getattr(e, 'code', None) in ('COMPANION_DISCONNECTED', 'COMPANION_TIMEOUT') and self._dead(e):
                raise _Crash(str(e), self.diagnose())
            result = {'passed': False, 'reason': '%s: %s' % (getattr(e, 'code', type(e).__name__), e)}
        result.update(action=action, ms=round((time.monotonic() - started) * 1000))
        if spec.get('name'):
            result['name'] = spec['name']
        return result

    def _dead(self, error):
        if getattr(error, 'code', None) not in ('COMPANION_DISCONNECTED', 'COMPANION_TIMEOUT', 'COMPANION_UNREACHABLE',
                                                 'GAME_EXITED'):
            return False
        time.sleep(1.5)
        return not game_processes() or bool(self.game.ping().get('error'))

    def diagnose(self):
        """Crash doctor on the newest crash log (Crash Logger writes it as the game dies)."""
        time.sleep(2.0)
        try:
            from .fnv_tools import FnvService
            result = FnvService(self.live.project).crash_explain()
            data = result.get('data') if result.get('ok') else {'error': result.get('error')}
            if result.get('ok') and data:
                return {'log': data.get('file'), 'modified': data.get('modified'), 'exception': data.get('exception'),
                        'crashSite': data.get('crashSite'), 'thread': data.get('thread'),
                        'likelyCulprits': (data.get('likelyCulprits') or [])[:5], 'frames': (data.get('frames') or [])[:8],
                        'formsInvolved': (data.get('formsInvolved') or [])[:10], 'runningScript': data.get('runningScript'),
                        'companionContext': data.get('companionContext'),
                        'runningScriptFromCompanion': data.get('runningScriptFromCompanion')}
            return data
        except Exception as e:  # diagnosis is best effort
            return {'error': str(e)}

    def _expr(self, text):
        """An expression or console line with the EditorIDs the running game does not keep (base forms, a mod's
        own records) replaced by their form ids; cached per text."""
        cache = self.__dict__.setdefault('_expr_cache', {})
        if text not in cache:
            try:
                cache[text] = self.live.substitute_editor_ids(text)[0]
            except Exception:
                cache[text] = text
        return cache[text]

    def _dispatch(self, action, arg, marker):
        live = self.live
        if action in ('run', 'console'):
            line = self._expr(arg if isinstance(arg, str) else arg.get('line'))
            ref = arg.get('ref') if isinstance(arg, dict) else None
            out = live.run_console(line, ref)
            allow = isinstance(arg, dict) and arg.get('allow_errors')
            passed = bool(out['accepted']) and (allow or not out['scriptErrors'])
            return {'passed': passed, 'output': out['output'], 'reason': None if passed else
                    'console line rejected' if not out['accepted'] else 'script errors: %d' % out['scriptErrors']}
        if action == 'script':
            source = arg if isinstance(arg, str) else arg.get('source')
            out = live.run_script(source, arg.get('ref') if isinstance(arg, dict) else None)
            passed = out['compiled'] and out['ok'] is not False
            return {'passed': passed, 'actual': out['result'], 'reason': None if passed else 'script failed: %s' % out}
        if action == 'wait':
            return self._wait(arg)
        if action == 'expect':
            return self._expect(arg, marker)
        if action == 'set':
            # {stage: Quest, value: 50} | {global: Name, value: 1} | {variable: name, of: Quest, value: 2} |
            # {objective: 10, quest: Quest, completed: true} (or displayed: true)
            if 'stage' in arg:
                out = live.script_set(arg['stage'], 'stage', arg['value'])
            elif 'global' in arg:
                out = live.script_set(arg['global'], 'global', arg['value'])
            elif 'variable' in arg:
                out = live.script_set(arg['of'], 'variable', arg['value'], name=arg['variable'])
            elif 'objective' in arg:
                kind = 'objective_completed' if 'completed' in arg else 'objective_displayed'
                out = live.script_set(arg['quest'], kind, arg.get('completed', arg.get('displayed')), index=arg['objective'])
            else:
                raise ScenarioError('BAD_STEP', 'set takes stage, global, variable (with of) or objective (with quest)')
            return {'passed': bool(out['accepted']), 'output': out['output']}
        if action == 'actor':
            out = live.actor_control(arg['ref'], arg['action'], arg.get('target'), arg.get('value'))
            return {'passed': out.get('accepted', True) is not False, 'output': out.get('output')}
        if action == 'input':
            out = live.input(arg['action'], arg.get('control'), arg.get('key'), arg.get('duration_ms', 0), arg.get('target'))
            return {'passed': out.get('accepted', True) is not False, 'actual': out}
        if action == 'move':
            if isinstance(arg, str):
                arg = {'to': arg}
            out = self.agent.move(arg.get('ref', 'player'), arg.get('to'), arg.get('cell'), arg.get('position'),
                                  arg.get('rotation'), int(arg.get('timeout', 180)))
            return {'passed': True, 'actual': {'after': out['after'], 'arrived': bool(out['arrived'])}}
        if action == 'mem_write':
            out = live.mem_write(arg['target'], arg['value'], arg.get('path'), arg.get('as'))
            return {'passed': True, 'actual': out}
        if action == 'call':
            out = live.engine_call(arg['function'], arg.get('this'), arg.get('args'), arg.get('returns', 'int'),
                                   arg.get('convention'), arg.get('allow_unproven', False))
            spec = {k: v for k, v in arg.items() if k in COMPARISONS}
            passed, why = compare(out['result'], spec) if spec else (True, '')
            return {'passed': passed, 'actual': out['result'], 'reason': None if passed else 'result not ' + why}
        if action == 'revert_all':
            # Only this test's changes: entries after the journal position marked when the steps began.
            since = (getattr(self, '_marker', None) or {}).get('journal') if arg != 'process' else None
            out = live.revert_all(since) if arg else {'reverted': 0}
            return {'passed': True, 'actual': {'reverted': out['reverted']}}
        if action == 'quit':
            out = self.game.quit() if arg else {}
            return {'passed': True, 'actual': out}
        if action == 'snapshot':
            return {'passed': True, 'actual': snapshot(live, arg.get('items'), arg.get('name'))}
        agent_result = self._agent_step(action, arg)
        if agent_result is not None:
            return agent_result
        raise ScenarioError('UNKNOWN_ACTION', 'Unknown step %r (run, script, wait, expect, set, actor, input, move, '
                            'mem_write, call, revert_all, quit, snapshot, spawn, save, load, menu, screenshot, watch, '
                            'trace, time, rng)' % action)

    def _wait(self, arg):
        if isinstance(arg, (int, float)):
            arg = {'seconds': arg}
        if 'seconds' in arg:
            time.sleep(min(float(arg['seconds']), 120))
            return {'passed': True}
        if 'frames' in arg:
            start = self.live.link.ping()['frame']
            deadline = time.monotonic() + float(arg.get('timeout', 30))
            while time.monotonic() < deadline:
                if self.live.link.ping()['frame'] - start >= int(arg['frames']):
                    return {'passed': True}
                time.sleep(0.05)
            return {'passed': False, 'reason': 'frames did not advance (game paused or stalled?)'}
        if 'until' in arg:
            deadline = time.monotonic() + float(arg.get('timeout', 10))
            last = None
            while time.monotonic() < deadline:
                last = fnv_link.value_of(self.live.link.eval(self._expr(arg['until'])))
                if last:
                    return {'passed': True, 'actual': last}
                time.sleep(float(arg.get('poll', 0.2)))
            return {'passed': False, 'actual': last, 'reason': 'timed out after %ss waiting for %s' % (arg.get('timeout', 10), arg['until'])}
        raise ScenarioError('BAD_STEP', 'wait takes seconds, frames or until')

    def _expect(self, arg, marker):
        live = self.live
        ref = arg.get('ref', 'player')
        if 'no_script_errors' in arg:
            errors = live.script_errors('last')['errors']
            events = [e for e in (live.link.events(marker['events']) if marker else {}).get('events', [])
                      if e['type'] == 'script_error']
            count = max(len(errors), len(events))
            passed = (count == 0) == bool(arg['no_script_errors'])
            return {'passed': passed, 'actual': errors or [e['text'] for e in events],
                    'reason': None if passed else '%d script error(s)' % count}
        if 'eval' in arg:
            r = live.resolve(ref) if 'ref' in arg else None
            actual = fnv_link.value_of(live.link.eval(self._expr(arg['eval']), r['ptr'] if r else 0))
            what = arg['eval']
        elif 'item_count' in arg:
            r = live.resolve(ref)
            item = live.resolve(str(arg['item_count']))
            actual = fnv_link.value_of(live.link.eval('GetItemCount %s' % item['expr'], r['ptr']))
            what = 'item count of %s on %s' % (arg['item_count'], ref)
        elif 'stage' in arg:
            q = live.resolve(arg['stage'])
            actual = fnv_link.value_of(live.link.eval('GetStage %s' % q['expr']))
            what = 'stage of %s' % arg['stage']
        elif 'quest_running' in arg:
            q = live.resolve(arg['quest_running'])
            actual = bool(fnv_link.value_of(live.link.eval('GetQuestRunning %s' % q['expr'])))
            what = '%s running' % arg['quest_running']
        elif 'actor_value' in arg:
            r = live.resolve(ref)
            actual = fnv_link.value_of(live.link.eval('GetAV %s' % arg['actor_value'], r['ptr']))
            what = '%s of %s' % (arg['actor_value'], ref)
        elif 'variable' in arg:
            values = {v['name'].lower(): v['value'] for v in live.variables(arg.get('of', ref))['variables'] if v['name']}
            actual = values.get(str(arg['variable']).lower())
            what = 'variable %s of %s' % (arg['variable'], arg.get('of', ref))
        elif 'global' in arg:
            actual = fnv_link.value_of(live.link.eval('GetGlobalValue %s' % arg['global']))
            what = 'global %s' % arg['global']
        elif 'cell' in arg:
            actual = fnv_link.value_of(live.link.eval('player.GetParentCell'))
            target = arg['cell']
            want = live.resolve(target)['formId'] if not re.fullmatch(r'(0x)?[0-9A-Fa-f]{8}', str(target)) else str(target)
            arg = {**arg, 'equals': want}
            what = 'player cell'
        elif 'console' in arg:
            actual = live.console(self._expr(arg['console']))['output']
            what = 'output of %s' % arg['console']
        elif 'disabled' in arg:
            r = live.resolve(arg['disabled'])
            actual = bool(fnv_link.value_of(live.link.eval('GetDisabled', r['ptr'])))
            what = '%s disabled' % arg['disabled']
        elif 'dead' in arg:
            r = live.resolve(arg['dead'])
            actual = bool(fnv_link.value_of(live.link.eval('GetDead', r['ptr'])))
            what = '%s dead' % arg['dead']
        elif 'exists' in arg:
            try:
                live.resolve(arg['exists'])
                actual = True
            except Exception:
                actual = False
            what = '%s exists' % arg['exists']
        elif 'mem' in arg:
            actual = live.mem_read(arg['mem'], arg.get('path'), arg.get('as')).get('value')
            what = 'memory %s %s' % (arg['mem'], arg.get('path') or '')
        elif any(k in arg for k in ('called', 'condition', 'message_box', 'marker', 'menu_open')):
            actual, what, arg = self._agent_expect(arg)
        else:
            raise ScenarioError('BAD_STEP', 'expect needs one of eval, item_count, stage, quest_running, actor_value, '
                                'variable, global, cell, console, disabled, dead, exists, mem, no_script_errors, '
                                'called, condition, message_box, marker, menu_open')
        passed, why = compare(actual, arg)
        return {'passed': passed, 'actual': actual, 'expected': why,
                'reason': None if passed else '%s: expected %s, got %r' % (what, why, actual)}

    # ------------------------------------------------------------ agent steps and checks
    def _subst(self, value):
        """$name -> a value saved by an earlier step (spawn ... as: name)."""
        if isinstance(value, str) and '$' in value:
            for key in sorted(self.vars, key=len, reverse=True):
                value = value.replace('$' + key, str(self.vars[key]))
            return value
        if isinstance(value, dict):
            return {k: self._subst(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._subst(v) for v in value]
        return value

    def _agent_step(self, action, arg):
        agent = self.agent
        if action == 'spawn':
            arg = {'form': arg} if isinstance(arg, str) else arg
            out = agent.spawn(arg['form'], arg.get('near', 'player'), int(arg.get('count', 1)), int(arg.get('distance', 0)),
                              int(arg.get('direction', 0)))
            if arg.get('as'):
                self.vars[arg['as']] = out['refs'][0]['ref']
                for k, r in enumerate(out['refs']):
                    self.vars['%s%d' % (arg['as'], k)] = r['ref']
            return {'passed': bool(out['refs']), 'actual': [r['ref'] for r in out['refs']]}
        if action in ('save', 'load'):
            name = arg if isinstance(arg, str) else arg['name']
            out = agent.test_save(action, name)
            return {'passed': True, 'actual': {k: v for k, v in out.items() if k in ('saved', 'loaded', 'phase', 'file')}}
        if action == 'menu':
            if 'press' in arg:
                out = agent.menu_control('press', arg['press'])
                return {'passed': True, 'actual': out['pressed']}
            if arg.get('close') or arg.get('close_all'):
                out = agent.menu_control('close_all' if arg.get('close_all') else 'close', menu=arg.get('menu'))
                return {'passed': True, 'actual': out}
            raise ScenarioError('BAD_STEP', 'menu takes press (button label or index), close or close_all')
        if action == 'screenshot':
            out = agent.screenshot(arg if isinstance(arg, str) else arg.get('name', 'step'))
            return {'passed': True, 'actual': {'path': out['path'], 'method': out['method']}}
        if action == 'watch':
            out = agent.watch(arg['items'], float(arg.get('seconds', 5)), int(arg.get('interval_ms', 100)),
                              arg.get('until'), keep_unpaused=False)
            passed = out['untilMet'] is not False
            return {'passed': passed, 'actual': out['items'], 'reason': None if passed else
                    'watched %ss and %s never became true' % (out['seconds'], arg.get('until'))}
        if action == 'trace':
            arg = {'function': arg} if isinstance(arg, str) else arg
            out = agent.trace(arg['function'], arg.get('action', 'start'), arg.get('filter'))
            return {'passed': True, 'actual': {k: out.get(k) for k in ('started', 'recorded', 'callsSinceStart')}}
        if action == 'time':
            out = agent.time_control(**arg) if isinstance(arg, dict) else agent.time_control(advance_hours=float(arg))
            return {'passed': True, 'actual': out.get('after')}
        if action == 'rng':
            out = agent.rng(**arg) if isinstance(arg, dict) else agent.rng(seed=int(arg))
            return {'passed': True, 'actual': out}
        return None

    def _agent_expect(self, arg):
        agent = self.agent
        if 'called' in arg:
            out = agent.trace(arg['called'], 'read', limit=20)
            count = out['recorded']
            spec = dict(arg)
            if not any(k in spec for k in COMPARISONS):
                spec['ge'] = 1
            if 'at_least' in spec:
                spec['ge'] = spec.pop('at_least')
            if 'at_most' in spec:
                spec['le'] = spec.pop('at_most')
            return count, '%s calls' % arg['called'], spec
        if 'condition' in arg:
            out = agent.condition_eval(arg['condition'], arg.get('subject', 'player'), arg.get('target'),
                                       arg.get('tab_subjects'))
            lists = [l for l in out['lists'] if not arg.get('list') or str(arg['list']).lower() in l['list'].lower()]
            if not lists:
                raise ScenarioError('BAD_STEP', 'No condition list matches %r' % arg.get('list'),
                                    {'lists': [l['list'] for l in out['lists']]})
            actual = all(l['passes'] for l in lists)
            spec = dict(arg)
            spec.setdefault('equals', bool(spec.pop('passes', True)))
            return actual, 'conditions of %s%s' % (arg['condition'], ' (%s)' % arg['list'] if arg.get('list') else ''), spec
        if 'message_box' in arg:
            box = agent.message_box()
            text = ' '.join(filter(None, [box and box.get('title'), box and box.get('text')])) if box else ''
            want = arg['message_box']
            actual = bool(box) and (want is True or str(want).lower() in text.lower())
            return actual, 'message box %r (open: %r)' % (want, text or None), _truthy(arg)
        if 'marker' in arg:
            out = agent.quest_targets(arg['marker'])
            shown = [t for o in out['objectives'] for t in o['targets'] if t['shown']]
            return bool(shown), 'quest marker of %s' % arg['marker'], {**arg, 'equals': bool(arg.get('shown', True))}
        if 'menu_open' in arg:
            names = [m['name'].lower() for m in agent.menus()['visible']]
            want = str(arg['menu_open']).lower()
            return any(want in n for n in names), 'menu %s open' % arg['menu_open'], _truthy(arg)
        raise ScenarioError('BAD_STEP', 'unknown agent check')

    def _trace_summary(self, script):
        try:
            out = self.live.deep.script_trace(script, 'read', limit=400)
            self.live.deep.script_trace(script, 'stop')
        except Exception as e:
            return {'error': str(e)}
        ran = sorted({s['statement'] for s in out.get('steps', []) if s.get('statement')})
        return {'script': out.get('script'), 'statementsRun': ran, 'statements': out.get('statements'),
                'notRun': (out.get('notRun') or [])[:40], 'stopped': out.get('stopped'), 'lost': out.get('lost'),
                'steps': len(out.get('steps', []))}

    def debug_bundle(self, scenario):
        """Evidence for a failed test: open menus and message box, the console's last lines, the frame."""
        bundle = {}
        try:
            bundle['menus'] = self.agent.menus()
            bundle['menus'].pop('uses', None)
        except Exception as e:
            bundle['menus'] = {'error': str(e)}
        try:
            console = self.game.logs(['console'], 0, 40).get('console') or {}
            bundle['consoleTail'] = console.get('lines', [])
        except Exception as e:
            bundle['consoleTail'] = {'error': str(e)}
        try:
            shot = self.agent.screenshot(scenario['name'])
            bundle['screenshot'] = {'path': shot['path'], 'method': shot['method'], 'blank': shot.get('blank')}
        except Exception as e:
            bundle['screenshot'] = {'error': str(e)}
        return bundle

    # ------------------------------------------------------------ evidence
    def screenshot(self, name):
        try:
            from . import wincapture
            proc = self.game.process()
            if not proc:
                return None
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            found = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def callback(hwnd, _):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                buffer = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buffer, 256)
                if pid.value == proc[0] and buffer.value == 'Fallout: New Vegas' and user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                return True

            user32.EnumWindows(callback, 0)
            if not found:
                return None
            safe = re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:60]
            path = Path(self.live.project.state_dir) / 'fnv/test-screens' / ('%s-%d.png' % (safe, time.time()))
            shot = wincapture.capture({'hwnd': found[0]}, path)
            return {'path': shot['path'], 'blank': shot['blank']}
        except Exception as e:  # evidence is best effort
            return {'error': str(e)}

    def save_report(self, report):
        folder = Path(self.live.project.state_dir) / 'fnv/test-reports'
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[^A-Za-z0-9_-]+', '_', report['name'])[:60]
        path = folder / ('%s-%d.json' % (safe, time.time()))
        path.write_text(json.dumps(report, indent=2, default=str) + '\n', encoding='utf-8')
        report['report'] = str(path)


def _truthy(arg):
    """A check without a comparison means "is true"."""
    return arg if any(k in arg for k in COMPARISONS) else {**arg, 'is_true': True}


class _Crash(Exception):
    def __init__(self, reason, diagnosis):
        super().__init__(reason)
        self.reason = reason
        self.diagnosis = diagnosis


# ====================================================================== snapshots
def snapshot(live, items=None, name=None):
    """Capture chosen state: items like {eval: expr}, {item_count: form}, {actor_value: Health, ref: player},
    {stage: quest}, {global: name}, {variable: name, of: quest}, or 'player'. Saved under state/fnv/snapshots."""
    items = items or ['player']
    values = {}
    for item in items:
        if item == 'player':
            p = live.player()
            values['player.position'] = p['position']
            values['player.cell'] = p['cell']['formId']
            values['player.health'] = p['health']['current']
            values['player.level'] = p['level']
            continue
        key = json.dumps(item, sort_keys=True)
        runner = ScenarioRunner(live)
        values[key] = runner._expect({**item, 'is_true': True}, None).get('actual') if isinstance(item, dict) else None
    shot = {'name': name or 'snapshot-%d' % time.time(), 'time': time.time(), 'game': live.identity(), 'values': values}
    folder = Path(live.project.state_dir) / 'fnv/snapshots'
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^A-Za-z0-9_-]+', '_', shot['name'])[:80]
    (folder / (safe + '.json')).write_text(json.dumps(shot, indent=2) + '\n', encoding='utf-8')
    shot['path'] = str(folder / (safe + '.json'))
    return shot


def load_snapshot(live, name):
    folder = Path(live.project.state_dir) / 'fnv/snapshots'
    safe = re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:80]
    path = folder / (safe + '.json')
    if not path.exists():
        raise ScenarioError('NOT_FOUND', 'No snapshot %s' % name)
    return json.loads(path.read_text(encoding='utf-8'))


def diff(a, b):
    keys = sorted(set(a['values']) | set(b['values']))
    changes = []
    for k in keys:
        va, vb = a['values'].get(k), b['values'].get(k)
        if va != vb:
            changes.append({'item': k, 'before': va, 'after': vb})
    return {'before': a['name'], 'after': b['name'], 'changed': len(changes), 'changes': changes,
            'unchanged': len(keys) - len(changes)}
