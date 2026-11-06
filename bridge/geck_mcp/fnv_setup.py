"""fnv_setup_check: is this machine ready for the fnv_* tools? Each prerequisite with its state and the fix.

Read-only: it looks at files and versions, never changes anything. Used as the first step of the setup
guide (docs/FNV-MCP-SETUP.md).
"""
import ctypes
import re
import subprocess
from ctypes import wintypes
from pathlib import Path

GAME_VERSION = '1.4.0.525'


def file_version(path):
    """'a.b.c.d' from a PE file's version resource, or None."""
    try:
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        value, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(buffer, '\\', ctypes.byref(value), ctypes.byref(length)):
            return None
        info = ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
        ms, ls = info[2], info[3]
        return '%d.%d.%d.%d' % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except (OSError, AttributeError):
        return None


def setup_check(project):
    checks = []

    def add(name, ok, detail, fix=None, required=True):
        checks.append({'check': name, 'state': 'ok' if ok else ('missing' if required else 'optional'),
                       'detail': detail, 'fix': None if ok else fix})

    game = Path(project.game_root)
    exe = game / 'FalloutNV.exe'
    version = file_version(exe) if exe.exists() else None
    add('game', version == GAME_VERSION, '%s %s' % (exe, version or 'not found'),
        'The tools target the Steam FalloutNV.exe %s (Fallout: New Vegas, Steam build).' % GAME_VERSION)
    nvse = game / 'nvse_1_4.dll'
    nvse_version = file_version(nvse) if nvse.exists() else None
    add('xNVSE', bool(nvse_version) and (game / 'nvse_loader.exe').exists(), 'nvse_1_4.dll %s' % (nvse_version or 'not found'),
        'Install xNVSE 6.4.9 or newer into the game folder (nvse_loader.exe and nvse_1_4.dll next to FalloutNV.exe).')

    fnv = project.fnv
    mo2 = Path(fnv.mo2_root) if fnv and fnv.mo2_root else None
    profile = mo2 / 'profiles' / fnv.mo2_profile if mo2 and fnv.mo2_profile else None
    add('MO2 instance', bool(mo2 and (mo2 / 'ModOrganizer.exe').exists() and profile and profile.is_dir()),
        '%s, profile %s' % (mo2, fnv.mo2_profile if fnv else None),
        'Create a portable Mod Organizer 2 instance for New Vegas with a separate dev profile, and set fnv.mo2_root '
        'and fnv.mo2_profile in the project file.')
    if mo2 and (mo2 / 'ModOrganizer.ini').exists():
        ini = (mo2 / 'ModOrganizer.ini').read_text(encoding='utf-8', errors='replace')
        titles = re.findall(r'^\d+\\title=(.+)$', ini, re.M)
        wanted = fnv.mo2_executable if fnv else 'NVSE'
        add('MO2 executable', wanted in titles, 'executables: %s' % ', '.join(titles),
            'Add an MO2 executable titled "%s" that runs nvse_loader.exe.' % wanted)
    plugins = []
    if mo2:
        plugins = [p.name.lower() for p in (mo2 / 'mods').glob('*/NVSE/Plugins/*.dll')]
    add('JIP LN NVSE', 'jip_nvse.dll' in plugins, 'in the MO2 mods' if 'jip_nvse.dll' in plugins else 'not found',
        'Install JIP LN NVSE as an MO2 mod (used for CloseActiveMenu, effects and input).')
    add('crash logger', 'crashlogger.dll' in plugins, 'Yvile\'s Crash Logger' if 'crashlogger.dll' in plugins else 'not found',
        'Install Yvile\'s Crash Logger as an MO2 mod (the crash doctor reads its logs).')
    if profile:
        ini = profile / 'Fallout.ini'
        text = ini.read_text(encoding='utf-8', errors='replace') if ini.exists() else ''
        add('keep running unfocused', bool(re.search(r'^bAlwaysActive=1', text, re.M)), str(ini),
            'Set bAlwaysActive=1 under [General] in the dev profile\'s Fallout.ini.')

    from . import fnv_link
    try:
        zig = fnv_link.find_zig(project)
        zig_version = subprocess.run([str(zig), 'version'], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        zig, zig_version = None, None
    add('Zig 0.14.1', zig_version == '0.14.1', '%s %s' % (zig, zig_version),
        'Install Zig 0.14.1 and set fnv.zig (or GECK_MCP_ZIG); it builds the engine companion.')
    installed = mo2 / 'mods' / fnv_link.MOD_NAME / 'NVSE/Plugins' / fnv_link.DLL_NAME if mo2 else None
    current = None
    receipt = Path(project.state_dir) / 'engine/install.json'
    if receipt.exists():
        import json
        try:
            current = json.loads(receipt.read_text())
        except ValueError:
            current = None
    add('engine companion', bool(installed and installed.exists()),
        '%s%s' % (installed, ' (installed build %s)' % current.get('build_id') if current else ''),
        'With the game closed, call fnv_companion_install.')
    if current and installed and installed.exists():
        same = fnv_link.file_digest(installed) == current.get('sha256')
        sources = current.get('source') == fnv_link.source_hashes()
        add('companion up to date', same and sources, 'installed DLL matches its receipt: %s; sources unchanged since: %s'
            % (same, sources), 'Rebuild and install it: fnv_companion_install (game closed).')

    from . import fnv_engine
    try:
        path, decomp = fnv_engine.default_paths(project)
        engine = fnv_engine.load(path, decomp)
        add('engine map', True, '%s (%s)' % (path, engine.identity().get('gameBuild')))
    except Exception as e:
        add('engine map', False, str(e), 'Generate it from your fnv_decomp checkout: python -m engine_map.generate '
            '--decomp <checkout> (see FNV-ENGINE-TOOLS.md).')
    clean = getattr(fnv, 'clean_exe', None) if fnv else None
    if clean and Path(clean).exists():
        data = Path(clean).read_bytes()[:4096]
        add('unpacked exe (patch map)', b'.bind' not in data, str(clean),
            'fnv.clean_exe must be an unpacked FalloutNV.exe (Steamless output, no .bind section).', required=False)
    else:
        add('unpacked exe (patch map)', False, 'fnv.clean_exe not set',
            'Optional, for fnv_patch_map: your own unpacked FalloutNV.exe (Steamless), read locally only.',
            required=False)
    control = getattr(fnv, 'engine_control', 'read') if fnv else 'read'
    add('full control', control == 'full', 'fnv.engine_control = %s' % control,
        'Optional: set fnv.engine_control to "full" to allow the act tools (memory writes, engine calls, '
        'console changes, hot-swap).', required=False)
    missing = [c['check'] for c in checks if c['state'] == 'missing']
    return {'ready': not missing, 'missing': missing, 'checks': checks,
            'notes': ['Turn Discord\'s in-game overlay off globally (Discord > User Settings > Game Overlay): its hook '
                      'crashed the game at startup with JIP LN loaded on the test machine.',
                      'Launch from Claude Code or an MCP client whose job object allows breakaway, or the bridge '
                      'falls back to opening a shortcut through Explorer.']}
