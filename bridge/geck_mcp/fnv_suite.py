"""Plan 2 W7: fnv_bisect and fnv_selftest.

fnv_bisect level=mods|plugins|records steps=[...] (the fnv_test steps steps of a scenario that fails): find the one MO2 mod,
plugin, or record of a plugin (records plugin=<name>: variants of the plugin file with half of its records left out,
written with the plugin codec over the plugin in its mod folder, the original put back at the end) that makes it fail.
Level hooks (other DLLs' engine patches) is not offered: putting a foreign patch back in a running game is unsafe (its
DLL keeps state that expects it, threads may be inside it); level mods names the DLL's mod instead. Round zero checks that the scenario fails with everything on and passes with every candidate
off; then each round turns off half of the remaining suspects (fnv_profile_set, game closed), runs the scenario
(the runner launches the game), and keeps the half that holds the culprit. The profile is put back at the end.

fnv_selftest: quick checks of what each tool family relies on, against the running game: the companion and its wire,
engine-map identity, the singletons the tools read (player, TES, scene graph camera, HUD, OSGlobals, memory manager),
the prologues the hooks plan, the symbol database, the decomp checkout, and a cheap call into each family. Each row
is ok, degraded (a fallback is in use) or broken, with the fact that failed.
"""
import hashlib
import json
import os
import struct
import threading
import time
from pathlib import Path

from .fnv_conv import ConvError, verdict
from .fnv_state import atomic_write, locked

PROTECTED_MODS = ('fnvmcp engine companion', 'xnvse data', 'jip ln nvse plugin')
# A prepare failure with one of these codes is the game crashing or hanging while it loads, which is exactly what a
# bisect may be hunting (R2): it counts as a failure, not as a run that never got going.
CRASH_CODES = ('GAME_EXITED', 'GAME_CRASHED', 'NOT_IN_GAME')
BASE_PLUGINS = ('falloutnv.esm', 'deadmoney.esm', 'honesthearts.esm', 'oldworldblues.esm', 'lonesomeroad.esm',
                'gunrunnersarsenal.esm', 'classicpack.esm', 'mercenarypack.esm', 'tribalpack.esm', 'caravanpack.esm')


PROFILE_LISTS = ('modlist.txt', 'plugins.txt', 'loadorder.txt')


def bisect_dir(state_dir):
    return Path(state_dir) / 'fnv' / 'bisect'


def restore_markers(state_dir):
    """The restore markers a bisect left (R3): one per <plugin>.restore.json (records level) and profile.restore.json
    (mods and plugins levels), each with its 'marker' path. A marker exists only while a bisect runs, or after a
    server died in one."""
    folder = bisect_dir(state_dir)
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob('*.restore.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {'corrupt': True}
        if isinstance(data, dict):
            out.append({**data, 'marker': str(path)})
    return out


def _sha(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def pid_alive(pid):
    """True when a process with this pid is running (a marker's owner is still bisecting)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5                      # access denied: it exists
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == 259
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def process_start(pid):
    """The process's creation time (FILETIME int), or None when it cannot be read (or not on Windows)."""
    if os.name != 'nt':
        return None
    from .fnv_game import process_started
    try:
        return process_started(pid)
    except OSError:
        return None


def owner_running(marker, own_pid):
    """True when the marker's bisect is still running in another process. The pid alone is not enough: Windows
    reuses pids quickly, so a marker records its process's start time and a different start means a dead owner."""
    pid = marker.get('pid')
    if pid == own_pid or not pid_alive(pid):
        return False
    started = marker.get('pidStart')
    if started is None:
        return True              # an older marker: the pid is all there is
    now = process_start(pid)
    return now is None or now == started


class BisectTools:
    def __init__(self, live, records, runner_factory, quit_game=None):
        self.live = live
        self.records = records
        self.runner_factory = runner_factory        # () -> ScenarioRunner
        self.quit_game = quit_game or (lambda: self.live.game.quit())
        self._busy = threading.Lock()               # one bisect at a time: it owns the profile and the plugin file
        self.inconclusive = False
        self._masters = {}                          # plugins level: name.lower() -> its masters
        self._runs = []                             # classification and crash flag of every run of this bisect

    def _candidates(self, level, keep):
        if level == 'records':
            return self._record_candidates(keep)
        profile = self.live.game.profile_dir
        keep = {k.lower() for k in keep or []}
        if level == 'mods':
            lines = (profile / 'modlist.txt').read_text(encoding='utf-8').splitlines()
            mods = [l[1:] for l in lines if l.startswith('+')]
            return [m for m in mods if m.lower() not in PROTECTED_MODS and m.lower() not in keep
                    and not m.endswith('_separator')]
        lines = (profile / 'plugins.txt').read_text(encoding='utf-8', errors='replace').splitlines()
        # MO2's New Vegas plugins.txt has no leading '*' (a plugin is active when listed); the '*' style (newer games)
        # is accepted too, and there only starred lines are active.
        content = [l.strip() for l in lines if l.strip() and not l.strip().startswith('#')]
        if any(l.startswith('*') for l in content):
            content = [l for l in content if l.startswith('*')]
        active = [l.lstrip('*') for l in content]
        return [p for p in active if p.lower() not in BASE_PLUGINS and p.lower() not in keep]

    def _set(self, level, on, off):
        if self.live.game.process():
            self.quit_game()
            for _ in range(60):
                if not self.live.game.process():
                    break
                time.sleep(0.5)
        if level == 'records':
            self._write_variant(set(on))
            return
        changes = {**{x: True for x in on}, **{x: False for x in off}}
        if level == 'mods':
            self.records.profile_set(changes, None, self.live.game, False)
        else:
            self.records.profile_set(None, changes, self.live.game, False)

    @staticmethod
    def _is_crash(report):
        if report.get('crash'):
            return True
        for f in report.get('failures') or []:
            evidence = f.get('evidence') if isinstance(f.get('evidence'), dict) else {}
            if f.get('code') in CRASH_CODES or evidence.get('crash') or f.get('crash'):
                return True
        return False

    @staticmethod
    def _classify(report):
        """pass, fail, or inconclusive. A run is inconclusive only when it never got going (every failure is in
        prepare, e.g. the launch was blocked or a master was missing) and none of them is a crash or a hang at load:
        a plugin that crashes the game while it loads is a culprit to find (R2), not noise."""
        if report.get('passed'):
            return 'pass'
        failures = report.get('failures') or []
        if failures and all(f.get('phase') == 'prepare' for f in failures) and not BisectTools._is_crash(report):
            return 'inconclusive'
        return 'fail'

    def _passes(self, scenario):
        report = self.runner_factory().run(data=dict(scenario))
        kind = self._classify(report)
        self.inconclusive = kind == 'inconclusive'
        self._runs.append((kind, self._is_crash(report)))
        return kind == 'pass', report.get('summary') or report.get('failures')

    def _plugin_masters(self, names):
        """name.lower() -> the plugin's masters (TES4 MAST), for the plugins in the active load order."""
        from .fnv_data import load_order, plugin_masters
        entries = load_order(self.live.project, self.records.data._live_plugins())
        paths = {e['name'].lower(): e.get('path') for e in entries}
        out = {}
        for name in names:
            path = paths.get(name.lower())
            try:
                out[name.lower()] = plugin_masters(path) if path else []
            except (OSError, ValueError):
                out[name.lower()] = []
        return out

    def _close_under_masters(self, half, everything):
        """half plus every candidate it needs as a master (transitively), in candidate order, so no tested set has a
        plugin on without its master (which the game reports as a load failure, not as the plugin's fault)."""
        lower = {x.lower(): x for x in everything}
        on = {x.lower() for x in half}
        todo = list(on)
        while todo:
            for master in self._masters.get(todo.pop(), []):
                m = master.lower()
                if m in lower and m not in on:
                    on.add(m)
                    todo.append(m)
        return [x for x in everything if x.lower() in on]

    def _plugin_file(self):
        from .fnv_data import load_order
        entries = load_order(self.live.project, self.records.data._live_plugins())
        target = next((e for e in entries if e['name'].lower() == str(self.plugin).lower()), None)
        if not target or not target.get('path'):
            raise ConvError('PLUGIN_NOT_FOUND', '%s is not in the active load order.' % self.plugin)
        return Path(target['path'])

    def _record_candidates(self, keep):
        """The plugin's records (top-level groups and cell children alike) by form id, as hex text."""
        from .esp import codec
        if not getattr(self, 'plugin', None):
            raise ConvError('BAD_ARGUMENTS', 'level records needs plugin.')
        path = self._plugin_file()
        held = getattr(self, '_original', None)
        if held is None or held[0] != path:         # never reuse another plugin's bytes
            self._original = (path, path.read_bytes())
        keep = {k.upper() for k in keep or []}
        out = []
        for rec, _ in codec.parse(self._original[1]).records():
            fid = '%08X' % rec.form_id
            if rec.type not in ('TES4', 'CELL', 'WRLD', 'DIAL') and fid not in keep and fid not in out:
                out.append(fid)
        return out

    def _write_variant(self, on):
        """The plugin with only the records in `on` (and every CELL, WRLD, DIAL container) kept."""
        from .esp import codec
        path, data = self._original
        plugin = codec.parse(data, lazy=False)
        everything = set(self._record_candidates(None))

        def prune(items):
            kept = []
            for item in items:
                if isinstance(item, codec.Group):
                    item.children = prune(item.children)
                    kept.append(item)
                elif '%08X' % item.form_id in everything and '%08X' % item.form_id not in on:
                    continue
                else:
                    kept.append(item)
            return kept
        plugin.groups = prune(plugin.groups)
        _atomic_write(path, codec.write(plugin))

    def _state_dir(self):
        project = getattr(self.live, 'project', None)
        return getattr(project, 'state_dir', None) if project is not None else None

    @staticmethod
    def _pid():
        return os.getpid()

    def _write_marker(self, name, data):
        folder = bisect_dir(self._state_dir())
        folder.mkdir(parents=True, exist_ok=True)
        marker = folder / name
        body = json.dumps({**data, 'pid': self._pid(), 'pidStart': process_start(self._pid()),
                           'startedAt': time.time()}, indent=2) + '\n'
        _atomic_write(marker, body.encode('utf-8'))
        return marker

    def _backup_original(self):
        """Copy the plugin's original bytes to the project's state dir (named with their sha256) and write the
        restore marker, before any variant is written, so a dead server leaves something to restore from (R3)."""
        path, data = self._original
        if not self._state_dir():
            return None
        sha = hashlib.sha256(data).hexdigest()
        folder = bisect_dir(self._state_dir())
        marker = folder / (path.name + '.restore.json')
        if marker.exists():
            try:
                held = json.loads(marker.read_text(encoding='utf-8')).get('sha256')
            except (OSError, ValueError):
                held = None
            if held != sha:      # finish_pending ran first, so this is a marker it could not settle
                raise ConvError('RESTORE_PENDING', '%s has a restore marker for other bytes; it was not overwritten.'
                                % path.name, {'marker': str(marker)})
        folder.mkdir(parents=True, exist_ok=True)
        backup = folder / ('%s.%s.orig' % (path.name, sha[:16]))
        _atomic_write(backup, data)
        self._write_marker(path.name + '.restore.json', {'plugin': str(path), 'backup': str(backup), 'sha256': sha})
        return {'path': str(backup), 'sha256': sha}

    def _backup_profile(self):
        """Copies of the profile's lists and their marker, before the first _set of a mods or plugins bisect."""
        if not self._state_dir():
            return None
        profile = Path(self.live.game.profile_dir)
        folder = bisect_dir(self._state_dir())
        folder.mkdir(parents=True, exist_ok=True)
        backups, shas = {}, {}
        for name in PROFILE_LISTS:
            source = profile / name
            if not source.is_file():
                continue
            data = source.read_bytes()
            shas[name] = hashlib.sha256(data).hexdigest()
            backups[name] = str(folder / ('profile.%s.%s.orig' % (name, shas[name][:16])))
            _atomic_write(backups[name], data)
        return self._write_marker('profile.restore.json', {'profile': str(profile), 'backups': backups,
                                                           'sha256': shas})

    def _drop_marker(self, name):
        if self._state_dir():
            (bisect_dir(self._state_dir()) / name).unlink(missing_ok=True)

    def finish_pending(self, target=None):
        """Settle the restore markers a dead bisect left (R3), before anything else touches the files: a file that
        already holds its original bytes just loses its marker; one that does not gets its backup written back. A
        marker whose backup is missing or wrong raises RESTORE_PENDING (nothing is written), and one whose owner
        process is still running raises BUSY (another server is bisecting). target: only that plugin's marker."""
        if not self._state_dir():
            return []
        done = []
        for m in restore_markers(self._state_dir()):
            if target is not None and Path(m.get('plugin', '')) != Path(target):
                continue
            if m.get('corrupt'):
                raise ConvError('RESTORE_PENDING', 'The bisect restore marker %s cannot be read; check the plugin '
                                'and profile by hand, then delete it.' % m['marker'], m)
            if owner_running(m, self._pid()):
                raise ConvError('BUSY', 'Another server (pid %s) is bisecting; it owns %s.' % (
                    m.get('pid'), m.get('plugin') or 'the profile'), m)
            if 'plugin' in m:
                done.append(self._settle(Path(m['plugin']), m.get('backup'), m.get('sha256'), m))
            else:
                done.append(self._settle_lists(Path(m.get('profile', '')), m))
            Path(m['marker']).unlink(missing_ok=True)
        return done

    def _settle(self, path, backup, sha, marker):
        if _sha(path) == sha:
            return {'marker': marker['marker'], 'settled': str(path)}
        if not backup or _sha(backup) != sha:
            raise ConvError('RESTORE_PENDING', '%s is not its original and the backup %s is missing or changed; put '
                            'the original back by hand, then delete %s.' % (path, backup, marker['marker']), marker)
        _atomic_write(path, Path(backup).read_bytes())
        if _sha(path) != sha:
            raise ConvError('RESTORE_PENDING', 'Writing %s back did not verify.' % path, marker)
        return {'marker': marker['marker'], 'restored': str(path)}

    def _settle_lists(self, profile, marker):
        backups, shas = marker.get('backups') or {}, marker.get('sha256') or {}
        for name, sha in shas.items():           # check every list before writing any
            if _sha(profile / name) != sha and _sha(backups.get(name) or '') != sha:
                raise ConvError('RESTORE_PENDING', 'The profile list %s is not its original and its backup is '
                                'missing or changed; fix it by hand, then delete %s.' % (name, marker['marker']),
                                marker)
        restored = []
        with locked(profile / 'modlist.txt', 30.0):          # the profile lock every list edit takes (S2.4)
            for name, sha in shas.items():
                if _sha(profile / name) != sha:
                    _atomic_write(profile / name, Path(backups[name]).read_bytes())
                    restored.append(str(profile / name))
        return {'marker': marker['marker'], 'restored': restored or None, 'settled': str(profile)}

    def _restore_plugin(self):
        original = getattr(self, '_original', None)
        if original:
            _atomic_write(original[0], original[1])
            if original[0].read_bytes() != original[1]:
                raise OSError('the restored plugin does not match the original')
            self._drop_marker(original[0].name + '.restore.json')
            self._original = None

    def bisect(self, level, steps, start='continue', keep=None, max_rounds=10, plugin=None):
        if level not in ('mods', 'plugins', 'records'):
            raise ConvError('BAD_ARGUMENTS', 'level is mods, plugins or records (with plugin).')
        if not steps:
            raise ConvError('BAD_ARGUMENTS', 'steps (the failing scenario, as for fnv_test steps) are needed.')
        if not self._busy.acquire(blocking=False):
            raise ConvError('BUSY', 'Another fnv_bisect is running; it owns the profile and the plugin file.')
        try:
            self.plugin = plugin
            self._original = None                       # nothing carried over from an earlier call
            restore_failed = []
            result = None
            touched = False                             # the game was closed or a variant written
            everything = []
            try:
                # A dead bisect's restore comes first (R3), so this one never starts from a pruned file.
                self.finish_pending()
                # Everything is validated before the game is closed.
                everything = self._candidates(level, keep)
                if len(everything) < 1:
                    raise ConvError('NO_CANDIDATES', 'No %s to bisect (protected ones are kept on).' % level)
                backup = None
                if level == 'records':
                    backup = self._backup_original()
                    touched = True
                    if self.live.game.process():
                        self.quit_game()
                else:
                    self._backup_profile()
                result = self._bisect_rounds(level, steps, start, everything, max_rounds)
                if backup:
                    result['originalBackup'] = backup
            finally:
                if not touched:
                    self._original = None               # read for the candidates only: nothing was changed
                if level == 'records' and touched:
                    try:
                        if self.live.game.process():
                            self.quit_game()
                    except Exception as e:  # noqa: BLE001 - reported in the verdict; the file is still put back
                        restore_failed.append('closing the game: %s' % str(e)[:200])
                    try:
                        self._restore_plugin()
                    except Exception as e:  # noqa: BLE001 - reported in the verdict
                        restore_failed.append('putting the plugin back: %s' % str(e)[:200])
                elif everything and level != 'records':
                    try:
                        self._set(level, everything, [])        # the profile as it was
                        self._drop_marker('profile.restore.json')
                    except Exception as e:  # noqa: BLE001 - reported in the verdict; the marker stays for next time
                        restore_failed.append(str(e)[:300])
            if restore_failed:
                if result is None:
                    raise ConvError('RESTORE_FAILED', 'The bisect ended and putting the %s back failed: %s' % (
                        'plugin' if level == 'records' else 'profile', restore_failed[0]), restore_failed)
                result['restoreFailed'] = restore_failed
                result['verdict'] = 'RESTORE FAILED (%s). ' % restore_failed[0] + result['verdict']
            return result
        finally:
            self._busy.release()

    def _bisect_rounds(self, level, steps, start, everything, max_rounds):
        scenario = {'name': 'bisect', 'start': start, 'steps': list(steps), 'relaunch_on_crash': True}
        rounds = []
        self._runs = []
        self._masters = self._plugin_masters(everything) if level == 'plugins' else {}

        def closed(names):
            return self._close_under_masters(names, everything) if level == 'plugins' else list(names)
        self._set(level, everything, [])
        ok, why = self._passes(scenario)
        rounds.append({'round': 0, 'off': [], 'passed': ok})
        if ok:
            return verdict('The scenario passes with every %s on: nothing to bisect' % level[:-1], str(why),
                           rounds=rounds)
        if self.inconclusive:
            return verdict('Inconclusive: the game did not run the scenario with every %s on' % level[:-1], str(why),
                           rounds=rounds, candidates=everything)
        self._set(level, [], everything)
        ok, why = self._passes(scenario)
        rounds.append({'round': 0, 'off': list(everything), 'passed': ok})
        if not ok and self._runs and all(crash for _, crash in self._runs):
            return verdict('Every tested set crashed the game or never reached gameplay, even with every candidate '
                           'off: the cause is not one of the %ss' % level[:-1], str(why), rounds=rounds,
                           candidates=everything, culprit=None)
        if not ok:
            return verdict('The scenario fails even with every candidate off: the cause is not one of them'
                           if not self.inconclusive else 'Inconclusive: the game did not run the scenario with '
                           'every candidate off', str(why), rounds=rounds, candidates=everything)
        suspects = list(everything)
        n = 1
        while len(suspects) > 1 and n <= max_rounds:
            half = closed(suspects[:len(suspects) // 2])     # plugins: with the masters they need
            if all(x in half for x in suspects):
                break                            # the suspects are masters of each other: no half splits them
            rest = [x for x in everything if x not in half]
            self._set(level, half, rest)         # only the first half of the suspects on
            ok, why = self._passes(scenario)
            rounds.append({'round': n, 'on': half, 'passed': ok})
            if self.inconclusive:
                return verdict('Inconclusive at round %d: the game did not run the scenario with this half on '
                               '(a master switched off under its dependent?); %d suspects left' % (n, len(suspects)),
                               str(why), suspects=suspects, rounds=rounds, candidates=everything, culprit=None)
            suspects = [x for x in suspects if (x in half) != ok]
            n += 1
        culprit = suspects[0] if len(suspects) == 1 else None
        confirmed = None
        if culprit and len(everything) > 1:
            # The last step was never observed: run with only the culprit on, and it must fail.
            alone = closed([culprit])
            self._set(level, alone, [x for x in everything if x not in alone])
            ok, why = self._passes(scenario)
            rounds.append({'round': 0, 'on': [culprit], 'confirm': True, 'passed': ok})
            confirmed = (not ok) and not self.inconclusive
            if not confirmed:
                return verdict('Narrowed to %s, but it did not fail on its own: the failure needs another %s as well, '
                               'or the run was inconclusive' % (culprit, level[:-1]), str(why), culprit=None,
                               suspects=suspects, rounds=rounds, candidates=everything, confirmed=False)
        return verdict('The culprit is %s (%d rounds)' % (culprit, n - 1) if culprit else
                       'Narrowed to %d suspects in %d rounds' % (len(suspects), n - 1),
                       'With only the culprit on, the scenario fails%s; with the other half on, it passes.' % (
                           ' (confirmed)' if confirmed else ''),
                       culprit=culprit, suspects=suspects, rounds=rounds, candidates=everything, confirmed=confirmed)


def _atomic_write(path, data):
    """Write through a unique temp file in the same folder and os.replace, so a crash never leaves a half file."""
    atomic_write(path, data)


class SelfTest:
    def __init__(self, live, tools):
        self.live = live
        self.tools = tools          # dict of tool objects by family

    def _row(self, family, check, fn):
        try:
            state, detail = fn()
        except Exception as e:  # noqa: BLE001 - a broken check is a result
            state, detail = 'broken', str(e)[:200]
        return {'family': family, 'check': check, 'state': state, 'detail': detail}

    def run(self):
        live = self.live
        rows = []

        def ping():
            p = live.link.ping()
            return ('ok' if p.get('wire') == 2 else 'degraded'), 'companion %s wire %s frame %s' % (
                p.get('companion'), p.get('wire'), p.get('frame'))
        rows.append(self._row('foundation', 'companion', ping))

        def engine_map():
            ident = live.engine().identity()
            return ('ok' if ident.get('exeImageCheck') else 'degraded'), 'decomp %s' % str(ident.get('decompCommit'))[:10]
        rows.append(self._row('foundation', 'engine map', engine_map))

        def symdb():
            db = getattr(live.engine(), 'symdb', None)
            return ('ok', 'symbol database loaded') if db else ('degraded', 'no symbol database: no lines or locals')
        rows.append(self._row('foundation', 'symbol database', symdb))

        singletons = {'player': 0x011DEA3C, 'TES': 0x011DEA10, 'scene graph': 0x011DEB7C, 'HUD': 0x011D96C0,
                      'OSGlobals': 0x011DEA0C}
        for name, address in singletons.items():
            def single(address=address, name=name):
                v = live.u32s([address])[0]
                return ('ok', '0x%08X' % v) if v else ('broken', '%s pointer at 0x%08X is null' % (name, address))
            rows.append(self._row('eyes and hands', name, single))

        def camera():
            cam = live.u32s([live.u32s([0x011DEB7C])[0] + 0xAC])[0]
            cls = (live.rtti([cam]).get(cam) or {}).get('class')
            return ('ok' if cls == 'NiCamera' else 'broken'), 'scene graph +0xAC is %s' % cls
        rows.append(self._row('eyes', 'camera', camera))

        prologues = {'TESObjectREFR::Activate': (0x00573170, '558bec'), 'TES::RunScripts': (0x00455490, '558bec'),
                     'MemoryManager::Allocate': (0x00AA3E40, None), 'Warning': (0x005B5E40, None)}
        for name, (address, expect) in prologues.items():
            def prologue(address=address, expect=expect):
                raw = live.reads([(address, 6)])[0]
                if raw is None:
                    return 'broken', 'unreadable'
                if raw[0] == 0xE9:
                    return 'degraded', 'starts with another module\'s jump (hooks chain behind it)'
                if expect and not raw.hex().startswith(expect):
                    return 'broken', 'prologue %s, expected %s...' % (raw.hex(), expect)
                return 'ok', raw.hex()
            rows.append(self._row('hooks', name, prologue))

        def memory():
            out = self.tools['memory'].pools()
            return ('ok' if out.get('pools') else 'broken'), out.get('verdict')
        rows.append(self._row('memory', 'pools', memory))

        def formula():
            out = self.tools['systems'].formula('xp_for_level', [2])
            return ('ok' if out.get('result') == 200 else 'broken'), out.get('verdict')
        rows.append(self._row('systems', 'formula call', formula))

        def camera_tool():
            out = self.tools['eyes'].camera_tool('get')
            return 'ok', 'fov %s' % out.get('fov')
        rows.append(self._row('eyes', 'fnv_camera get', camera_tool))

        def hud():
            out = self.tools['eyes'].hud_read()
            return 'ok', 'mode %s' % (out.get('mode') or {}).get('name')
        rows.append(self._row('eyes', 'fnv_hud read', hud))

        def profile():
            out = self.tools['profile'].sample(0.3, 50, 'main', 'selftest', 3)
            return ('ok' if out.get('samples') else 'broken'), '%s samples' % out.get('samples')
        rows.append(self._row('profile', 'sample', profile))

        def native():
            out = live.link.one('mod', 'list')
            return ('ok' if out.get('ok') else 'broken'), '%d modules' % len(out.get('modules', []))
        rows.append(self._row('engine code', 'module loader', native))

        def code_ops():
            out = live.link.one('code', 'free', '10')        # not a block: the op answers unknown_block
            return ('ok' if out.get('error') == 'unknown_block' else 'broken'), 'code ops answer'
        rows.append(self._row('engine code', 'code placement', code_ops))

        def coverage():
            out = live.link.one('cov', 'read', '0')
            return ('ok' if out.get('ok') else 'broken'), 'coverage ops answer'
        rows.append(self._row('coverage', 'cov', coverage))

        def decomp():
            root = getattr(live.project.fnv, 'decomp_root', None)
            ok = root and (Path(root) / 'build' / 'units.json').is_file()
            return ('ok', str(root)) if ok else ('degraded', 'no decomp checkout: fnv_code cannot build')
        rows.append(self._row('engine code', 'decomp checkout', decomp))

        counts = {s: sum(1 for r in rows if r['state'] == s) for s in ('ok', 'degraded', 'broken')}
        return verdict('%d ok, %d degraded, %d broken' % (counts['ok'], counts['degraded'], counts['broken']),
                       'Run after a game, xNVSE, plugin or decomp update.', checks=rows, **counts)
