"""fnv_script_coverage (Plan 2, C-scripts.md section 4): which script lines a play-through or a test folder ran.

start [plugin]   arm the statement hook (ScriptRunner::EvaluateLine 0x005E1550, the one fnv_script_debug and fnv_blame
                 share) and read its ring in the background every 0.2 s, keeping each (script, statement) seen
stop             stop reading; per script of the plugin (or every script seen): statements run and never run as
                 source lines with their text, and the begin blocks never entered
run folder       start, run every scenario of the folder (fnv_test run_all), stop

The ring holds 4,096 statements and wraps in about a second in a busy cell, so the reader runs often; records lost
between reads are counted and reported (a line counted as never run may then have run). Statement numbers map to
source lines as fnv_script_debug does (statement_lines, plus one for a script compiled in the game).
"""
import threading
from pathlib import Path
import time

from .fnv_conv import ConvError, verdict
from .fnv_scriptdbg import statement_lines

POLL = 0.2


class ScriptCoverage:
    def __init__(self, live, statement_hook, script_debug, runner=None, data=None, sleep=time.sleep):
        self.live = live
        self.hook = statement_hook
        self.sd = script_debug
        self.runner = runner              # () -> ScenarioRunner
        self.data = data
        self.sleep = sleep
        self.seen = {}                    # script ptr -> set of statement numbers
        self.lost = 0
        self.records = 0
        self.plugin = None
        self._thread = None
        self._stop = threading.Event()
        self._since = 0
        self._started = None

    def start(self, plugin=None):
        self.live.ready()
        if self._thread and self._thread.is_alive():
            raise ConvError('ALREADY_RUNNING', 'Coverage is already being recorded. Next: stop.')
        h = self.hook.ensure()
        self.seen, self.lost, self.records, self.plugin = {}, 0, 0, plugin
        self._since = int(self.live.u32s([int(h['seqAddress'], 16)])[0] or 0)
        self._stop.clear()
        self._started = time.time()
        self._thread = threading.Thread(target=self._loop, args=(h,), daemon=True, name='script-coverage')
        self._thread.start()
        return verdict('Recording script coverage%s' % (' for %s' % plugin if plugin else ''),
                       'The statement hook is read every %.1f s in the background; play or run tests, then stop.'
                       % POLL, plugin=plugin)

    def _loop(self, h):
        while not self._stop.is_set():
            try:
                self._drain(h)
            except Exception:  # noqa: BLE001 - the game may be loading; try again
                pass
            self._stop.wait(POLL)

    def _drain(self, h):
        seq, recs, lost = self.live.deep.records(h, since=self._since, limit=4096)
        self._since = seq
        self.lost += lost
        self.records += len(recs)
        for r in recs:
            self.seen.setdefault(r[12], set()).add(r[18])

    def stop(self):
        if self._thread:
            self._stop.set()
            self._thread.join(timeout=5)
            self._thread = None
            try:
                h = self.hook.existing()
                if h:
                    self._drain(h)
            except Exception:  # noqa: BLE001
                pass
        if not self.seen and not self.records:
            raise ConvError('NOT_RECORDING', 'Nothing was recorded. Next: start, play or run tests, then stop.')
        return self.report()

    def report(self, limit=60):
        ptrs = sorted(self.seen)
        heads = dict(zip(ptrs, self.live.form_ids(ptrs))) if ptrs else {}
        wanted = self._plugin_scripts(self.plugin) if self.plugin else None
        rows = []
        by_form = {}
        for p in ptrs:
            fid = (heads.get(p) or {}).get('formId')
            if fid:
                by_form[fid.upper()] = p
        forms = wanted if wanted is not None else sorted(by_form)
        for fid in forms:
            ptr = by_form.get(fid.upper())
            if ptr is None:
                try:
                    ptr = self.live.resolve(fid)['ptr']
                except Exception:  # noqa: BLE001
                    ptr = None
            ran = self.seen.get(ptr, set()) if ptr else set()
            source = self.sd._source(int(fid, 16), ptr)
            if not source:
                rows.append({'script': fid, 'statementsRun': len(ran), 'source': None,
                             'note': 'no source text (the plugin has no SCTX): statement numbers only'})
                continue
            lines = statement_lines(source)
            lead = self.sd._lead(ptr)
            text = source.replace('\r', '').split('\n')
            ran_lines = {lines[s - lead - 1] for s in ran if 0 < s - lead <= len(lines)}
            never = [n for n in lines if n not in ran_lines and text[n - 1].strip().split()[0].lower()
                     not in ('scn', 'scriptname', 'end', 'endif', 'else')]
            blocks = []
            for i, n in enumerate(lines):
                if text[n - 1].strip().lower().startswith('begin'):
                    inner = lines[i + 1] if i + 1 < len(lines) else None
                    if inner is not None and inner not in ran_lines:
                        blocks.append({'line': n, 'text': text[n - 1].strip()})
            rows.append({'script': fid, 'statements': len(lines), 'linesRun': len(ran_lines),
                         'percent': round(100.0 * len(ran_lines) / max(1, len(lines)), 1),
                         'neverRun': [{'line': n, 'text': text[n - 1].strip()} for n in never][:limit],
                         'blocksNeverEntered': blocks})
        rows.sort(key=lambda r: r.get('percent', 100.0))
        return verdict('%d scripts; %d statements recorded (%d lost between reads)' % (len(rows), self.records,
                                                                                       self.lost),
                       'Lines from ScriptRunner::EvaluateLine records (statement numbers mapped to source lines). '
                       'A lost record may hide a line that did run: when lost is not 0, rerun with fewer scripts '
                       'running (a quieter cell).', scripts=rows, lost=self.lost, plugin=self.plugin,
                       seconds=round(time.time() - (self._started or time.time()), 1))

    def _plugin_scripts(self, plugin):
        """Form ids (load order) of the SCPT records a plugin adds."""
        if self.data is None:
            raise ConvError('NOT_AVAILABLE', 'plugin filtering needs the data tools.')
        entries, _ = self.data._active()
        target = next((e for e in entries if e['name'].lower() == str(plugin).lower()), None)
        if not target:
            raise ConvError('PLUGIN_NOT_FOUND', '%s is not in the active load order.' % plugin)
        rows = self.data.index.db.execute("select owner, local from records where type = 'SCPT' and owner = ?",
                                          (target['name'].lower(),)).fetchall()
        return [str(self.data._global(entries, owner, local)) for owner, local in rows]

    def run(self, folder, plugin=None):
        if self.runner is None:
            raise ConvError('NOT_AVAILABLE', 'run needs the scenario runner.')
        self.start(plugin)
        try:
            suite = self.runner().run_all(folder)
        finally:
            out = self.stop()
        out['suite'] = {k: suite.get(k) for k in ('passed', 'failed', 'total', 'summary') if k in suite}
        return out

    def decompile(self, form, timeout=5.0):
        """Source text of a compiled script (also a quest's stage scripts, a topic info's, a package's, a terminal's)
        through xNVSE's own decompiler: its console command DecompileScript writes DecompiledScripts\\<plugin>\\
        <name>.<ext> under the game folder (or MO2's overwrite folder); the file is read and removed."""
        self.live.in_game()
        f = self.live.resolve(form)
        roots = [Path(self.live.game.game_root) / 'DecompiledScripts']
        mo2 = getattr(self.live.project.fnv, 'mo2_root', None)
        if mo2:
            roots.append(Path(mo2) / 'overwrite' / 'DecompiledScripts')
        before = {p: p.stat().st_mtime for r in roots if r.is_dir() for p in r.rglob('*.mcp')}
        out = self.live.console('DecompileScript %s mcp' % f['formId'])
        deadline = time.time() + float(timeout)
        found = []
        while time.time() < deadline and not found:
            found = [p for r in roots if r.is_dir() for p in r.rglob('*.mcp')
                     if p not in before or p.stat().st_mtime > before[p]]
            if not found:
                self.sleep(0.2)
        if not found:
            raise ConvError('NOT_DECOMPILED', 'xNVSE wrote no file for %s (console: %s). The form must hold a compiled '
                            'script (SCPT, a quest, a topic info, a package or a terminal).'
                            % (f['formId'], ' '.join(out.get('output') or []) or 'no output'))
        files = []
        for p in sorted(found):
            text = p.read_text(encoding='cp1252', errors='replace')
            files.append({'name': p.stem, 'plugin': p.parent.name, 'source': text})
            try:
                p.unlink()
            except OSError:
                pass
        return verdict('%d script(s) decompiled from %s' % (len(files), f['formId']),
                       'xNVSE ScriptAnalyzer through its DecompileScript command (the game\'s own bytecode and '
                       'command table); variable names come from the script\'s variable list, so they match the '
                       'original when the plugin kept them.', form=f['formId'], scripts=files)

    def dispatch(self, action, **a):
        if action == 'start':
            return self.start(a.get('plugin'))
        if action == 'stop':
            return self.stop()
        if action == 'run':
            if not a.get('folder'):
                raise ConvError('BAD_ARGUMENTS', 'run needs folder (a tests folder).')
            return self.run(a['folder'], a.get('plugin'))
        if action == 'report':
            return self.report()
        raise ConvError('UNKNOWN_ACTION', 'Unknown action %r. Valid: start, stop, run, report.' % action)
