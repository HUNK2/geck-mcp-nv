"""Game control for the fnv_* tools: launch Fallout: New Vegas through the MO2 dev profile, wait for the
engine companion, start at the main menu / a save / a cell, report status, quit and restart, install
plugins into the dev profile, read logs, and back up saves before tests change anything.

Nothing here touches the user's real saves or INIs: the dev profile keeps its own (profile-local).
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from . import fnv_link
from .fnv_link import EngineLink, LinkError
from .fnv_state import atomic_write, read_json, update_json
from .safe_paths import no_unc

GAME_EXE = 'FalloutNV.exe'
MENU_START = 1013     # StartMenu: main menu, and the pause menu in game
MENU_LOADING = 1007   # LoadingMenu
SAVE_NAME = re.compile(r'^[A-Za-z0-9_. \-]{1,120}$')
CELL_NAME = re.compile(r'^[A-Za-z0-9_]{1,120}$')


class GameError(Exception):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


# ---------------------------------------------------------------------- processes
def system_exe(name):
    """Full path of a Windows program under %SystemRoot%, so a file of the same name in the current folder or on
    PATH is never started instead (release audit L5)."""
    system32 = Path(os.environ.get('SystemRoot') or os.environ.get('WINDIR') or r'C:\Windows') / 'System32'
    if name.lower() == 'powershell.exe':
        return system32 / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    return system32 / name


def game_processes():
    """[(pid, start time as FILETIME int)] of running FalloutNV.exe processes."""
    out = subprocess.run([str(system_exe('tasklist.exe')), '/FI', 'IMAGENAME eq %s' % GAME_EXE, '/FO', 'CSV', '/NH'],
                         capture_output=True, text=True, timeout=15).stdout
    pids = [int(row.split('","')[1]) for row in out.splitlines() if row.lower().startswith('"%s"' % GAME_EXE.lower())]
    return [(pid, process_started(pid)) for pid in pids]


def process_started(pid):
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


# Setting bEnableAudioCache:Audio (0x011F6EBC; its value at +4). Owner::AddSound 0x00ADCD80 copies each new sound into
# the engine's audio cache unless it is 0 (N21).
AUDIO_CACHE_FLAG = 0x011F6EC0


# The companion build that writes exit notes (fnvmcp_engine.c COMPANION_VERSION 3; build 2 may predate them).
EXIT_NOTE_COMPANION = 3


def process_alive(pid):
    """Is process `pid` still running? Through its handle (microseconds), for the launch loops that used tasklist
    on every poll. A process that refuses the query is counted as alive."""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5        # ERROR_ACCESS_DENIED: it exists; anything else: gone
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102     # WAIT_TIMEOUT: not signalled, still running
    finally:
        kernel32.CloseHandle(handle)


def post_close(pid):
    """Send WM_CLOSE to the game's top-level windows (what closing the window does)."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    for hwnd in found:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)
    return len(found)


MO2_BLOCKING_DIALOGS = ('Cannot launch program', 'Plugin error', 'Executable not found')


def image_path(pid):
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def mo2_processes(mo2_root):
    """pids of ModOrganizer.exe running from this MO2 instance folder."""
    out = subprocess.run([str(system_exe('tasklist.exe')), '/FI', 'IMAGENAME eq ModOrganizer.exe', '/FO', 'CSV', '/NH'],
                         capture_output=True, text=True, timeout=15).stdout
    pids = [int(row.split('","')[1]) for row in out.splitlines() if row.lower().startswith('"modorganizer.exe"')]
    root = str(mo2_root).lower().rstrip('\\/')
    return [pid for pid in pids if (image_path(pid) or '').lower().startswith(root)]


def visible_windows(pid):
    """[(hwnd, title)] of a process's visible top-level windows."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(300)
            user32.GetWindowTextW(hwnd, title, 300)
            found.append((hwnd, title.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def mo2_dialogs(mo2_root):
    """Blocking MO2 dialogs (a launch that failed, a plugin error): [{'pid', 'title', 'hwnd'}]."""
    return [{'pid': pid, 'title': title, 'hwnd': hwnd} for pid in mo2_processes(mo2_root)
            for hwnd, title in visible_windows(pid) if title in MO2_BLOCKING_DIALOGS]


def complete_environment():
    """Add the user's standard environment variables that this process lacks (never overriding any).

    MCP stdio clients may start the server with a short allow-list of variables; MO2 inherits it and its
    basic_games plugin then fails on a missing ProgramW6432 (seen 2026-11-07). CreateEnvironmentBlock gives
    the environment a fresh logon of this user would have."""
    advapi32, userenv, kernel32 = ctypes.windll.advapi32, ctypes.windll.userenv, ctypes.windll.kernel32
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(wintypes.HANDLE(-1), 0x0008, ctypes.byref(token)):  # this process, TOKEN_QUERY
        return []
    block = ctypes.c_void_p()
    try:
        if not userenv.CreateEnvironmentBlock(ctypes.byref(block), token, False):
            return []
        added, offset = [], 0
        while True:
            entry = ctypes.wstring_at(block.value + offset)
            if not entry:
                break
            offset += (len(entry) + 1) * 2
            name, sep, value = entry.partition('=')
            if sep and name and name not in os.environ:
                os.environ[name] = value
                added.append(name)
        return added
    finally:
        if block.value:
            userenv.DestroyEnvironmentBlock(block)
        kernel32.CloseHandle(token)


def job_forbids_breakaway():
    """True when this process is in a Windows job object whose children cannot leave it (no BREAKAWAY_OK or
    SILENT_BREAKAWAY_OK): MO2 cannot start a hooked game from there, and the game would die with the job."""
    kernel32 = ctypes.windll.kernel32
    in_job = wintypes.BOOL()
    if not kernel32.IsProcessInJob(wintypes.HANDLE(-1), None, ctypes.byref(in_job)) or not in_job.value:
        return False

    class Basic(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [('Basic', Basic), ('Io', ctypes.c_uint64 * 6), ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t)]

    info = Extended()
    if not kernel32.QueryInformationJobObject(None, 9, ctypes.byref(info), ctypes.sizeof(info), None):
        return True
    return not info.Basic.LimitFlags & (0x800 | 0x1000)  # JOB_OBJECT_LIMIT_BREAKAWAY_OK | SILENT_BREAKAWAY_OK


def terminate(pid):
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 0xDEAD))
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------- the game
class Game:
    """One dev-profile game for a project. Holds the companion link and the session record."""

    def __init__(self, project):
        self.project = project
        fnv = project.fnv
        if not fnv or not fnv.mo2_root or not fnv.mo2_profile:
            raise GameError('NOT_CONFIGURED', 'Set fnv.mo2_root and fnv.mo2_profile in the project file')
        self.mo2_root = Path(fnv.mo2_root)
        self.profile = fnv.mo2_profile
        self.executable = getattr(fnv, 'mo2_executable', None) or 'NVSE'
        self.game_root = Path(project.game_root)
        self.state = Path(project.state_dir) / 'fnv'
        self.state.mkdir(parents=True, exist_ok=True)
        self.link = EngineLink(getattr(fnv, 'engine_pipe', None))
        self.link.server_check = self._pipe_server_is_the_game
        self._session = None
        self._proc_cache = None    # (monotonic time, process()) - tasklist costs ~100 ms and tools ask often (N11)

    def _pipe_server_is_the_game(self, pid):
        """Refuse a companion pipe served by anything but the running FalloutNV.exe (release audit L3): another
        program holding the pipe name is never sent requests."""
        running = [p for p, _ in game_processes()]
        if pid is None or pid not in running:
            raise LinkError('PIPE_NOT_GAME', 'The companion pipe %s is served by process %s, which is not the running '
                            'FalloutNV.exe (%s). Close the program that holds the pipe, or set fnv.engine_pipe.'
                            % (self.link.pipe, pid, ', '.join(map(str, running)) or 'none running'),
                            {'serverPid': pid, 'gamePids': running})

    # ------------------------------------------------------------ paths
    @property
    def profile_dir(self):
        return self.mo2_root / 'profiles' / self.profile

    @property
    def saves_dir(self):
        return self.profile_dir / 'saves'

    @property
    def console_log(self):
        return Path(os.environ.get('LOCALAPPDATA', '')) / 'fnvmcp/console.log'

    def log_paths(self):
        return {'nvse': self.game_root / 'nvse.log', 'console': self.console_log,
                'crash': self.game_root / 'CrashLogger.log', 'jip': self.game_root / 'jip_ln_nvse.log',
                'mo2': self.mo2_root / 'logs/mo_interface.log',
                'error': self.game_root / 'falloutnv_error.log', 'havok': self.game_root / 'falloutnv_havok.log'}

    # ------------------------------------------------------------ session record
    def session(self):
        """session.json (pid, ready, launchedAt, saves backup...). A corrupt or missing file reads as {} (R10)."""
        if self._session is None:
            self._session = read_json(self.state / 'session.json', {})
        return self._session

    def save_session(self, **changes):
        """Merge `changes` into session.json under its lock: another bridge process may have written other keys
        since this one read it (R10). The old `journal` key has no reader and is dropped."""
        changes.pop('journal', None)

        def merge(stored):
            stored.pop('journal', None)
            stored.update(changes)
        session = update_json(self.state / 'session.json', merge, {}, indent=2)
        cached = self.session()
        cached.clear()
        cached.update(session)
        return cached

    # ------------------------------------------------------------ status
    PROCESS_CACHE_SECONDS = 1.0

    def process(self, fresh=False):
        """(pid, start) of the one running game, or None. The answer is reused for a second (N11); launch and quit
        forget it, and `fresh=True` asks again."""
        cached = getattr(self, '_proc_cache', None)
        if not fresh and cached and time.monotonic() - cached[0] < self.PROCESS_CACHE_SECONDS:
            return cached[1]
        running = game_processes()
        self.exiting = False
        if len(running) > 1:
            found = self._exiting_game(running)
            if found is None:
                raise GameError('SEVERAL_GAMES', 'More than one FalloutNV.exe is running',
                                {'pids': [p for p, _ in running]})
            self.exiting = True
        else:
            found = running[0] if running else None
        self._proc_cache = (time.monotonic(), found)
        return found

    # FILETIME (100 ns since 1601) of the Unix epoch, and how much earlier than launchedAt a process may have started
    # and still be the one fnv_launch started (launchedAt is taken just before the start).
    FILETIME_UNIX_EPOCH = 116444736000000000
    OWN_START_SLACK_SECONDS = 5

    def owns(self, proc):
        """Did fnv_launch start this (pid, start FILETIME)? The session's pid must match and the process must not have
        started before launchedAt (less a few seconds): a pid Windows reused for another FalloutNV.exe does not count."""
        session = self.session()
        if not proc or session.get('pid') != proc[0] or proc[1] is None or not session.get('launchedAt'):
            return False
        started = (proc[1] - self.FILETIME_UNIX_EPOCH) / 10_000_000
        return started >= float(session['launchedAt']) - self.OWN_START_SLACK_SECONDS

    YOUNG_SECONDS = 5

    def _exiting_game(self, running):
        """Two FalloutNV.exe while the session's game shuts down (a short-lived second one appears on the exit path,
        G15): the session's game when the other one is younger than YOUNG_SECONDS or the session's game left its
        exit note; else None."""
        ours = self.session().get('pid')
        mine = [r for r in running if r[0] == ours]
        others = [r for r in running if r[0] != ours]
        if len(running) != 2 or not mine:
            return None
        now = int((time.time() + 11644473600) * 10 ** 7)          # FILETIME (100 ns since 1601)
        start = others[0][1]
        young = start is not None and 0 <= now - start < self.YOUNG_SECONDS * 10 ** 7
        try:
            note = self.last_exit()
        except Exception:  # noqa: BLE001 - no note folder: decide by age
            note = None
        fresh_note = (note or {}).get('pid') == ours and time.time() - float((note or {}).get('at') or 0) < 60
        return mine[0] if young or fresh_note else None    # an old note (a hung exit) never hides a new game (M11)

    def forget_process(self):
        self._proc_cache = None

    def ping(self, timeout=3.0):
        try:
            return self.link.ping(timeout)
        except LinkError as e:
            self.link.close()
            return {'error': e.code, 'message': str(e)}

    def phase(self, ping=None):
        """main_menu / loading / in_game / in_menu / starting / unknown, from the companion."""
        ping = ping or self.ping()
        if ping.get('error'):
            return 'starting' if self.process() else 'not_running', {}
        if not ping.get('deferredInit'):
            return 'starting', {}
        if ping.get('msSinceFrame', 0) > 3000:
            return 'loading_or_stalled', {}
        try:
            results = self.link.batch([('eval', fnv_link.hexs('MenuMode %d' % MENU_LOADING)),
                                       ('eval', fnv_link.hexs('MenuMode %d' % MENU_START)),
                                       ('eval', fnv_link.hexs('MenuMode')),
                                       ('eval', fnv_link.hexs('player.GetParentCell')),
                                       ('eval', fnv_link.hexs('GetName (player.GetParentCell)'))], timeout=5)
        except LinkError as e:
            return 'loading_or_stalled', {'error': str(e)}
        loading, start, menu, cell, name = [fnv_link.value_of(r) for r in results]
        detail = {'cell': cell, 'cellName': name or None, 'menuMode': bool(menu)}
        if not cell or cell == '00000000':
            # The main menu also reports LoadingMenu as open, so check the start menu first.
            return ('main_menu' if start else 'loading' if loading else 'no_cell'), detail
        if loading:
            return 'loading', detail
        if start:
            return 'paused_menu', detail
        return ('in_menu' if menu else 'in_game'), detail

    def status(self):
        proc = self.process()
        session = self.session()
        out = {'running': bool(proc), 'pid': proc[0] if proc else None, 'exiting': bool(getattr(self, 'exiting', False)),
               'profile': {'mo2': str(self.mo2_root), 'name': self.profile},
               'session': {k: session.get(k) for k in ('pid', 'launchedAt', 'start', 'ready')}}
        from .fnv_suite import restore_markers
        pending = restore_markers(self.project.state_dir)
        if pending:     # a bisect is running, or a server died in one: the next fnv_bisect finishes the restore (R3)
            out['pendingRestore'] = [{k: m.get(k) for k in ('plugin', 'profile', 'pid', 'startedAt', 'marker')
                                      if m.get(k) is not None} for m in pending]
        if not proc:
            crash = self.crash_since(session.get('launchedAt'))
            last = self.last_exit(session.get('launchedAt'))
            out.update(phase='not_running', crashedSinceLaunch=crash, lastExit=last)
            if session.get('launchedAt') and not crash and not (last and last.get('forLastLaunch')) \
                    and (session.get('companion') or 0) >= EXIT_NOTE_COMPANION \
                    and not self._bridge_quit(session.get('launchedAt')):
                # Only a companion build that writes exit notes can tell, and a quit the bridge asked for since the
                # launch (its force path ends in TerminateProcess) is not "from outside" (R18, S6 review C I1/I2).
                # The companion writes its note from the game's own exit path, so a game that ended with neither a
                # note nor a crash log was ended from outside: TerminateProcess (Task Manager, a script), MO2's job
                # object closing with MO2, or a fault so early that the crash logger did not run.
                out['exitNote'] = ('The last launched game ended without an exit note and without a crash log: it was '
                                   'terminated from outside (killed, or closed with its MO2 job), not quit through '
                                   'the console, the menu or the window.')
            return out
        ping = self.ping()
        out['companion'] = ping
        phase, detail = self.phase(ping)
        out.update(phase=phase, **detail)
        if phase in ('in_game', 'in_menu', 'paused_menu', 'main_menu'):
            out['plugins'] = self.loaded_plugins()
        out['stale'] = bool(session.get('pid')) and session.get('pid') != proc[0]
        return out

    def loaded_plugins(self):
        try:
            count = fnv_link.value_of(self.link.eval('GetNumLoadedMods'))
            if not count:
                return None
            results = self.link.batch([('eval', fnv_link.hexs('GetNthModName %d' % i)) for i in range(int(count))])
            return [{'index': '%02X' % i, 'name': fnv_link.value_of(r)} for i, r in enumerate(results)]
        except LinkError:
            return None

    def crash_since(self, since):
        log = self.game_root / 'CrashLogger.log'
        if not since or not log.exists() or log.stat().st_mtime < since:
            return None
        text = log.read_text(encoding='utf-8', errors='replace')
        if not ('Exception:' in text or 'Call Stack:' in text or 'Unhandled exception' in text):
            return None
        from .fnv_crash import SHUTDOWN_MARKERS
        # A fault while the game was already exiting (plugin exit cleanup, seen on every quit) is not a crash
        # of the play session; it is reported but marked.
        shutdown = any(m in text for m in SHUTDOWN_MARKERS)
        return {'log': str(log), 'modified': log.stat().st_mtime, 'duringShutdown': shutdown}

    def crash_summary(self):
        """The crash doctor's verdict on a crash log written since the launch, for errors and status."""
        time.sleep(1.5)  # Crash Logger writes the log while the game dies
        found = self.crash_since(self.session().get('launchedAt'))
        if not found:
            return None
        try:
            from .fnv_tools import FnvService
            result = FnvService(self.project).crash_explain(found['log'])
            data = result.get('data') or {}
            culprits = [{'name': c.get('name'), 'confidence': c.get('confidence'), 'note': c.get('note'),
                         'reasons': (c.get('reasons') or [])[:2]} for c in (data.get('likelyCulprits') or [])[:3]]
            return {**found, 'exception': (data.get('exception') or {}).get('name'), 'thread': data.get('thread'),
                    'likelyCulprits': culprits}
        except Exception as e:  # the summary is best effort
            return {**found, 'doctorError': str(e)}

    # ------------------------------------------------------------ launch / wait
    def launch(self, start='menu', timeout=180):
        proc = self.process()
        if proc:
            raise GameError('ALREADY_RUNNING', 'The game is already running (pid %d); use fnv_restart or fnv_quit' % proc[0],
                            {'pid': proc[0]})
        self.check_companion_installed()
        mo2 = self.mo2_root / 'ModOrganizer.exe'
        if not mo2.exists():
            raise GameError('MO2_MISSING', 'ModOrganizer.exe not found in %s' % self.mo2_root)
        kind, value = parse_start(start, self.saves_dir)
        return self._launch(start, timeout, kind, value)

    def _launch(self, start, timeout, kind, value):
        mo2 = self.mo2_root / 'ModOrganizer.exe'
        leftover = self.close_stale_mo2()
        stale = self.clear_stale_mo2_state()
        launched = time.time()
        self.forget_process()
        method = self.shell_launch(mo2)
        self.save_session(pid=None, launchedAt=launched, start=start, ready=False)
        timeline = [{'t': 0, 'event': 'mo2_run', 'method': method,
                     'command': 'ModOrganizer.exe -p "%s" run -e "%s"' % (self.profile, self.executable)}]
        if stale:
            timeline.insert(0, {'t': 0, 'event': 'stale_mo2_state_moved', 'files': stale})
        if leftover:
            timeline.insert(0, {'t': 0, 'event': 'leftover_mo2_closed', **leftover})
        deadline = time.monotonic() + timeout

        def started():
            proc = self.process()
            if proc:
                return proc
            dialogs = mo2_dialogs(self.mo2_root)
            if dialogs:
                # MO2 waits behind a modal dialog that nobody will click: close it and fail now.
                for d in dialogs:
                    ctypes.windll.user32.PostMessageW(d['hwnd'], 0x0010, 0, 0)
                raise GameError('MO2_LAUNCH_FAILED', 'MO2 could not start the game: it showed "%s"' % dialogs[0]['title'],
                                {'dialogs': [d['title'] for d in dialogs], 'mo2Log': str(self.mo2_root / 'logs/mo_interface.log'),
                                 'hint': 'ERROR_ACCESS_DENIED on nvse_loader.exe in the MO2 log means the launch came from '
                                         'a restricted process (a sandboxed shell): launch through the MCP server.'})
            return None

        pid = self._wait(started, deadline, 'GAME_NOT_STARTED', 'FalloutNV.exe did not start')[0]
        timeline.append({'t': round(time.time() - launched, 1), 'event': 'process', 'pid': pid})
        self.save_session(pid=pid)
        self.link.close()
        self._wait(lambda: (self.ping().get('deferredInit') or None), deadline, 'COMPANION_NOT_READY',
                   'The engine companion did not answer (is it installed and enabled in the dev profile?)', pid)
        timeline.append({'t': round(time.time() - launched, 1), 'event': 'companion_ready'})
        try:        # which companion build: an older one writes no exit notes (fnv_status uses it; R18)
            self.save_session(companion=self.ping().get('companion'))
        except Exception:  # noqa: BLE001 - only the status note depends on it
            pass
        self._wait(lambda: self.phase()[0] == 'main_menu' or None, deadline, 'NO_MAIN_MENU', 'Main menu not reached', pid)
        timeline.append({'t': round(time.time() - launched, 1), 'event': 'main_menu'})
        result = self.start_from_menu(kind, value, deadline, pid)
        timeline.append({'t': round(time.time() - launched, 1), 'event': 'ready', 'phase': result['phase']})
        self.save_session(ready=True)
        return {**result, 'pid': pid, 'timeline': timeline, 'seconds': round(time.time() - launched, 1)}

    ARRIVAL_CLOSER = 'Begin Function { }\nif GetActiveMenuMode == 1001\nCloseActiveMenu\nendif\nEnd'
    UNPAUSER = ('Begin Function { }\nint m\nlet m := GetActiveMenuMode\nif m == 1001 || m == 1013\nCloseActiveMenu\n'
                'endif\nEnd')

    PAUSE_CLOSER = ('Begin Function { }\nif GetActiveMenuMode == 1013\nCloseActiveMenu\nendif\nEnd')

    def keep_unpaused(self, seconds, close_messages=True):
        """For `seconds`, the companion closes the pause menu (which the game opens when its window loses focus)
        and, unless close_messages is false, message boxes every 0.3 s, so a test's world keeps running. Returns
        the task slot, or None."""
        script = self.UNPAUSER if close_messages else self.PAUSE_CLOSER
        try:
            result = self.link.one('every', '12C', '%X' % (int(seconds) * 1000), fnv_link.hexs(script))
        except LinkError:
            return None
        return result.get('slot') if result.get('ok') else None

    def stop_task(self, slot):
        if slot is not None:
            try:
                self.link.one('every', 'cancel', '%X' % slot)
            except LinkError:
                pass

    def close_arrival_messages(self, seconds=90):
        """A coc start makes a fresh character, and the DLC pack and add-on quests then show "items added" and
        radio boxes for about a minute. The companion closes message boxes on its own for `seconds` (every 0.3 s,
        on the main thread), so the launch does not have to wait for them. fnv_status lists the task."""
        try:
            result = self.link.one('every', '12C', '%X' % (int(seconds) * 1000), fnv_link.hexs(self.ARRIVAL_CLOSER))
        except LinkError as e:
            return {'closing': False, 'reason': e.code}
        return {'closing': bool(result.get('ok')), 'seconds': seconds, 'slot': result.get('slot'),
                'reason': result.get('error')}

    def close_stale_mo2(self, wait=8.0):
        """A dev-instance MO2 left running without the game (after a crash, or behind a failed-launch dialog) takes
        the next "run" request and can sit behind a modal dialog: close it (WM_CLOSE, then terminate)."""
        if self.process():
            return None
        pids = mo2_processes(self.mo2_root)
        if not pids:
            return None
        dialogs = [title for pid in pids for _, title in visible_windows(pid) if title]
        for pid in pids:
            post_close(pid)
        end = time.monotonic() + wait
        while time.monotonic() < end and mo2_processes(self.mo2_root):
            time.sleep(0.5)
        killed = []
        for pid in mo2_processes(self.mo2_root):
            if terminate(pid):
                killed.append(pid)
        return {'pids': pids, 'windows': dialogs[:10], 'terminated': killed}

    def clear_stale_mo2_state(self):
        """MO2 writes plugin_loadcheck.tmp while it loads its plugins and deletes it afterwards. When MO2 is not
        running the file is left from an MO2 that was killed, and MO2 would stop at a "Plugin error" dialog asking
        to disable a plugin. Move it into the bridge state (kept, never deleted)."""
        check = self.mo2_root / 'plugin_loadcheck.tmp'
        if not check.exists():
            return []
        out = subprocess.run([str(system_exe('tasklist.exe')), '/FI', 'IMAGENAME eq ModOrganizer.exe', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True, timeout=15).stdout
        if 'modorganizer.exe' in out.lower():
            return []
        target = self.state / 'mo2-backups' / ('plugin_loadcheck-%d.tmp' % time.time())
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(check), str(target))
        return [str(target)]

    def shell_launch(self, mo2):
        """Start MO2 with ShellExecute, like a double-click, unless the bridge runs inside a job object that
        forbids breakaway; then open a shortcut through Explorer, which starts it outside that job.

        Seen 2026-11-06/07: a detached CreateProcess got no window focus and the game's renderer failed to start;
        the Python MCP client puts its server in a kill-on-close job without breakaway, where MO2's hooked start of
        nvse_loader.exe fails with ERROR_ACCESS_DENIED (Claude Code's launcher, libuv, allows breakaway); and a
        game started by Explorer got Discord's overlay injected (DiscordHook) and crashed at startup, 2 of 2,
        while ShellExecute starts never did. So Explorer is only the fallback."""
        added = complete_environment()
        if not job_forbids_breakaway():
            os.startfile(str(mo2), 'open', '-p "%s" run -e "%s"' % (self.profile, self.executable), str(self.mo2_root))
            return 'shell_execute' + (' (added %d environment variables)' % len(added) if added else '')
        link = self.state / ('launch-%s.lnk' % re.sub(r'[^A-Za-z0-9_-]', '_', self.profile))
        quote = lambda s: str(s).replace("'", "''")
        script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s'); $s.TargetPath='%s'; "
                  "$s.Arguments='%s'; $s.WorkingDirectory='%s'; $s.Save()") % (
            quote(link), quote(mo2), quote('-p "%s" run -e "%s"' % (self.profile, self.executable)), quote(self.mo2_root))
        made = subprocess.run([str(system_exe('powershell.exe')), '-NoProfile', '-NonInteractive', '-Command', script],
                              capture_output=True, text=True, timeout=60)
        if made.returncode != 0 or not link.exists():
            raise GameError('LAUNCH_BLOCKED', 'The bridge runs in a job object that forbids breakaway and the launch '
                            'shortcut could not be made', {'stderr': made.stderr[-1000:]})
        explorer = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'explorer.exe'
        subprocess.Popen([str(explorer), str(link)], close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 'explorer_shortcut (overlays such as Discord may inject into Explorer-started games)'

    def start_from_menu(self, kind, value, deadline, pid):
        if kind == 'menu':
            return {'phase': 'main_menu', 'start': 'menu'}
        line = 'load "%s"' % value if kind == 'save' else 'coc %s' % value
        # N21: the engine's audio cache is off while the start loads (a load begun in the menu's first frames copied
        # each sound into the cache at ~1 s a copy: ~3 min); it is turned back on once the game is in gameplay.
        try:
            cache_was = self.link.read(AUDIO_CACHE_FLAG, 1)
        except LinkError:
            cache_was = None
        cache_off = cache_was is not None and cache_was != b'\x00'
        first_ops = [('wr', fnv_link.word(AUDIO_CACHE_FLAG), '00')] if cache_off else []
        restored = False
        try:
            # Scheduled so the reply is sent before the game starts loading.
            closer = None
            if kind == 'coc':
                # Queue the arrival closer in the same request as the coc: once the cell loads, the main loop is
                # busy and a separate request would wait for it.
                ops = first_ops + [('every', '12C', '%X' % 90000, fnv_link.hexs(self.ARRIVAL_CLOSER)),
                                   ('sched', '0', fnv_link.hexs(line))]
                first, result = self.link.batch(ops)[-2:]
                closer = {'closing': bool(first.get('ok')), 'seconds': 90, 'slot': first.get('slot'),
                          'reason': first.get('error')}
            else:
                result = self.link.batch(first_ops + [('sched', '0', fnv_link.hexs(line))])[-1]
            if not result.get('ok'):
                raise GameError('START_FAILED', 'Could not queue "%s"' % line, result)
            ready = self.wait_in_game(deadline, pid, start='%s:%s' % (kind, value))
        finally:
            if cache_off:
                try:
                    self.link.write(AUDIO_CACHE_FLAG, cache_was)
                    restored = True
                except LinkError:
                    pass
        if closer:
            ready['arrivalMessages'] = closer
        ready['audioCache'] = {'offDuringLoad': cache_off, 'restored': restored}
        return ready

    def wait_in_game(self, deadline, pid=None, start=None, settle=1.5, dismiss_messages=True):
        """Wait for gameplay. Message boxes that pop up on arrival (a coc start makes a fresh character, and the
        DLC pack quests then announce their items) are closed with JIP LN's CloseActiveMenu."""
        stable_since = None
        start_menu_since = None
        detail = {}
        dismissed = []
        phases, began = [], time.monotonic()
        while time.monotonic() < deadline:
            if pid and not process_alive(pid):
                raise GameError('GAME_EXITED', 'The game exited while loading', {'crash': self.crash_summary()})
            phase, detail = self.phase()
            if not phases or phases[-1][1] != phase:
                phases.append((round(time.monotonic() - began, 1), phase))
            if phase == 'in_game':
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= settle:
                    return {'phase': phase, 'start': start, 'dismissedMessages': dismissed, 'phases': phases, **detail}
            else:
                stable_since = None
                if phase in ('in_menu', 'paused_menu') and dismiss_messages and len(dismissed) < 20:
                    try:
                        # MessageMenu (1001): the arrival boxes. StartMenu (1013): the pause menu the game opens
                        # when its window loses focus while it loads (the dev profile is ours to drive).
                        menu = fnv_link.value_of(self.link.eval('GetActiveMenuMode'))
                        # The main menu is StartMenu (1013) too, and it stays up while a coc or a save loads:
                        # close 1013 only when it has been open for 5 s with a cell loaded (a real pause).
                        start_menu_since = (start_menu_since or time.monotonic()) if menu == 1013 else None
                        if menu == 1001 or (menu == 1013 and time.monotonic() - start_menu_since > 5):
                            self.link.console('CloseActiveMenu')
                            dismissed.append({'t': round(time.monotonic(), 1),
                                              'menu': 'MessageMenu' if menu == 1001 else 'StartMenu (pause)'})
                            time.sleep(0.5)
                            continue
                    except LinkError:
                        pass
            time.sleep(0.25)
        raise GameError('NOT_IN_GAME', 'The game did not reach gameplay in time', {'lastDetail': detail, 'phases': phases,
                                                                                    'dismissedMessages': dismissed})

    def _wait(self, probe, deadline, code, message, pid=None):
        while time.monotonic() < deadline:
            if pid and not process_alive(pid):
                raise GameError('GAME_EXITED', 'The game exited during startup',
                                {'crash': self.crash_summary()})
            value = probe()
            if value:
                return value
            time.sleep(0.2)
        raise GameError(code, message)

    def ensure_running(self, start='menu', timeout=180):
        """Launch when not running; when running, check the companion answers."""
        proc = self.process()
        if not proc:
            return self.launch(start, timeout)
        ping = self.ping()
        if ping.get('error'):
            raise GameError('COMPANION_UNREACHABLE', 'The game runs but the engine companion does not answer', ping)
        return {'phase': self.phase(ping)[0], 'pid': proc[0], 'alreadyRunning': True}

    def check_companion_installed(self):
        modlist = self.profile_dir / 'modlist.txt'
        dll = self.mo2_root / 'mods' / fnv_link.MOD_NAME / 'NVSE/Plugins' / fnv_link.DLL_NAME
        enabled = modlist.exists() and ('+' + fnv_link.MOD_NAME) in modlist.read_text(encoding='utf-8').splitlines()
        if not dll.exists() or not enabled:
            raise GameError('COMPANION_NOT_INSTALLED', 'The engine companion is not installed in the dev profile; '
                            'run fnv_companion_install first', {'dll': str(dll), 'enabled': enabled})

    # ------------------------------------------------------------ quit / restart
    def quit(self, timeout=20, force=True):
        proc = self.process()
        if not proc:
            return {'wasRunning': False}
        pid = proc[0]
        if not self.owns(proc) and getattr(self.project.fnv, 'engine_control', 'read') != 'full':
            raise GameError('NOT_OUR_GAME', 'This FalloutNV.exe was not started by fnv_launch, so it may be your own '
                            'game. Close it yourself, or set fnv.engine_control to "full".', {'pid': pid})
        steps = []
        self.log_quit(pid, 'quit (timeout %s, force %s)' % (timeout, force))
        if not self.ping().get('error'):
            try:
                self.link.schedule(0, 'qqq')
                steps.append('qqq')
            except LinkError as e:
                steps.append('qqq failed: %s' % e)
        if self._exited(pid, timeout / 2):
            return self._quit_result(pid, steps, 'console')
        steps.append('WM_CLOSE x%d' % post_close(pid))
        if self._exited(pid, timeout / 2):
            return self._quit_result(pid, steps, 'window_close')
        if not force:
            raise GameError('QUIT_TIMEOUT', 'The game did not exit; call again with force=true to terminate it', {'steps': steps})
        steps.append('TerminateProcess')
        terminate(pid)
        if not self._exited(pid, 10):
            raise GameError('QUIT_FAILED', 'The game could not be terminated', {'steps': steps})
        return self._quit_result(pid, steps, 'terminated')

    def last_exit(self, launched_at=None):
        """How the last game with the companion exited (the companion's exit note: "console" = qqq or a script's
        QuitGame, "window_or_menu" = the window closed or the menu's Quit), and whether the bridge asked for it (a
        quit-log line within a minute before). None when no note exists (a crash or a kill writes none)."""
        folder = Path(os.environ.get('LOCALAPPDATA', '')) / 'fnvmcp'
        notes = sorted(folder.glob('exit-*.json'), key=lambda p: p.stat().st_mtime) if folder.is_dir() else []
        if not notes:
            return None
        try:
            note = json.loads(notes[-1].read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        at = notes[-1].stat().st_mtime
        asked = None
        log = Path(self.project.state_dir) / 'fnv' / 'quit-log.jsonl'
        if log.exists():
            try:
                rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()[-50:] if line.strip()]
            except ValueError:
                rows = []
            asked = any(r.get('pid') == note.get('pid') and at - 60 <= r.get('time', 0) <= at + 5 for r in rows)
        return {'pid': note.get('pid'), 'how': note.get('how'), 'at': at, 'frame': note.get('frame'),
                'servingRequest': note.get('servingRequest'), 'askedByBridge': asked,
                'forLastLaunch': None if launched_at is None else at >= float(launched_at)}

    def _bridge_quit(self, launched_at):
        """Did the bridge ask the game to quit since the last launch (a quit-log line after it)? By time, not pid:
        a bridge quit clears the session pid (S6 review C I1)."""
        log = Path(self.project.state_dir) / 'fnv' / 'quit-log.jsonl'
        if not launched_at or not log.exists():
            return False
        try:
            rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()[-200:] if line.strip()]
        except (OSError, ValueError):
            return False
        return any((r.get('time') or 0) >= float(launched_at) for r in rows)

    def log_quit(self, pid, step):
        """Every quit the bridge asks for, with the code path that asked (state/fnv/quit-log.jsonl), so a game that
        closed with nobody knowing why can be traced: if the log has no line for that time, the bridge did not do it."""
        import traceback
        try:
            path = Path(self.project.state_dir) / 'fnv' / 'quit-log.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            stack = [line.strip() for line in traceback.format_stack(limit=10)[:-1]]
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps({'time': time.time(), 'pid': pid, 'step': step, 'caller': stack[-6:]}) + '\n')
        except OSError:
            pass

    def _quit_result(self, pid, steps, how):
        self.forget_process()
        self.link.close()
        self.save_session(pid=None, ready=False)
        return {'wasRunning': True, 'pid': pid, 'how': how, 'steps': steps}

    def _exited(self, pid, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not any(p == pid for p, _ in game_processes()):
                return True
            time.sleep(0.25)
        return False

    def restart(self, start='menu', timeout=180):
        quit_result = self.quit()
        time.sleep(1.0)  # let MO2 unmount its virtual file system
        return {'quit': quit_result, 'launch': self.launch(start, timeout)}

    # ------------------------------------------------------------ install / logs / saves
    def install_plugin(self, plugin_path, mod_name=None):
        source = no_unc(plugin_path, 'plugin_path')      # a network path is refused before anything touches it (H5)
        mod = mod_name or source.stem
        if '..' in mod or not mod.strip(' .'):
            raise ValueError('mod_name %r is not a folder name: no "..", and not only dots and spaces' % mod)
        if self.process(fresh=True):
            raise GameError('GAME_RUNNING', 'Quit the game before installing (plugins load at startup)')
        if not source.is_file() or source.suffix.lower() not in ('.esp', '.esm'):
            raise GameError('BAD_PLUGIN', 'Not a plugin file: %s' % source)
        if not fnv_link.SAFE_NAME.match(mod):
            raise GameError('BAD_NAME', 'Mod name may use letters, digits, space and _ . \' - only')
        target = self.mo2_root / 'mods' / mod / source.name
        backup = None
        if target.exists():
            if target.read_bytes() == source.read_bytes():
                return {'installed': True, 'unchanged': True, 'target': str(target)}
            backup = self.state / 'install-backups' / ('%d-%s' % (time.time(), source.name))
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        fnv_link.enable_mod(self.mo2_root, self.profile, mod)
        added = []
        with fnv_link.profile_locked(self.mo2_root, self.profile):
            for name in ('plugins.txt', 'loadorder.txt'):
                path = self.profile_dir / name
                lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
                if source.name.lower() not in (line.strip().lower() for line in lines):
                    lines.append(source.name)
                    atomic_write(path, '\n'.join(lines) + '\n')
                    added.append(name)
        return {'installed': True, 'unchanged': False, 'target': str(target), 'mod': mod, 'backup': str(backup) if backup else None,
                'addedTo': added}

    def logs(self, sources=None, since='last', max_lines=200):
        cursor_path = self.state / 'log-cursors.json'
        cursors = read_json(cursor_path, {})
        out = {}
        for name, path in self.log_paths().items():
            if sources and name not in sources:
                continue
            if not path.exists():
                out[name] = {'path': str(path), 'missing': True}
                continue
            data = path.read_bytes()
            key = str(path)
            previous = cursors.get(key, {})
            start = 0
            if since == 'last' and previous.get('size', 0) <= len(data) and previous.get('mtime', 0) <= path.stat().st_mtime:
                start = previous.get('size', 0)
            elif isinstance(since, int):
                start = max(0, min(since, len(data)))
            lines = data[start:].decode('utf-8', errors='replace').splitlines()
            out[name] = {'path': str(path), 'from': start, 'size': len(data), 'lines': lines[-max_lines:],
                         'truncated': len(lines) > max_lines, 'reset': since == 'last' and start == 0 and bool(previous)}
            cursors[key] = {'size': len(data), 'mtime': path.stat().st_mtime}
        mine = {k: v for k, v in cursors.items() if k in {str(p) for p in self.log_paths().values()}}
        update_json(cursor_path, lambda stored: stored.update(mine), {}, indent=2)
        return out

    def backup_saves(self, reason):
        """Copy the dev profile's saves before a session writes to the game; keep the newest 10 backups."""
        session = self.session()
        # Key on the live process, not on session.json's pid (that is written only by fnv_launch).
        try:
            proc = self.process()
        except GameError:
            proc = None
        pid = proc[0] if proc else session.get('pid')
        if pid and session.get('savesBackedUpFor') == pid:
            return session.get('savesBackup')
        target = self.state / 'save-backups' / time.strftime('%Y%m%d-%H%M%S')
        target.mkdir(parents=True, exist_ok=True)
        copied = 0
        if self.saves_dir.exists():
            for f in self.saves_dir.iterdir():
                if f.is_file() and f.suffix.lower() in ('.fos', '.nvse', '.bak'):
                    shutil.copy2(f, target / f.name)
                    copied += 1
        backups = sorted(p for p in (self.state / 'save-backups').iterdir() if p.is_dir())
        for old in backups[:-10]:
            shutil.rmtree(old, ignore_errors=True)
        self.save_session(savesBackedUpFor=pid, savesBackup={'path': str(target), 'files': copied, 'reason': reason})
        return self.session()['savesBackup']


def parse_start(start, saves_dir):
    """menu | save:<name> | continue | coc:<cell EditorID>  ->  (kind, value)."""
    if not start or start == 'menu':
        return 'menu', None
    if start == 'continue':
        saves = sorted(Path(saves_dir).glob('*.fos'), key=lambda p: p.stat().st_mtime, reverse=True) \
            if Path(saves_dir).exists() else []
        if not saves:
            raise GameError('NO_SAVES', 'No saves in the dev profile (%s)' % saves_dir)
        return 'save', saves[0].stem
    kind, _, value = start.partition(':')
    if kind == 'save':
        if not SAVE_NAME.match(value):
            raise GameError('BAD_SAVE', 'Save names may use letters, digits, space and _ . - only')
        if not (Path(saves_dir) / (value + '.fos')).exists():
            raise GameError('SAVE_MISSING', 'No save %s.fos in %s' % (value, saves_dir))
        return 'save', value
    if kind == 'cell':
        # Tried 2026-11-12: SStartingCell in the INI makes the retail engine crash during startup in every cell
        # (TES::LoadInterior -> LoadingMenu setup 0x00788AB0 -> Tile::Set on InterfaceManager's root tile, still null
        # because the main menu has not built it yet).
        raise GameError('CELL_START_UNSUPPORTED', 'Starting straight in a cell (SStartingCell) crashes the game at '
                        'startup; use coc:%s, which goes through the main menu' % value, {'use': 'coc:%s' % value})
    if kind == 'coc':
        if not CELL_NAME.match(value):
            raise GameError('BAD_CELL', 'A cell is given by its EditorID (letters, digits, _)')
        return kind, value
    raise GameError('BAD_START', 'start must be menu, continue, save:<name> or coc:<cell EditorID>')
