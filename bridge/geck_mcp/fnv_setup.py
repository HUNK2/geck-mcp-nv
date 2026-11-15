"""fnv_setup_check: is this machine ready for the fnv_* tools? Each prerequisite with its state and the fix.

Read-only: it looks at files and versions, never changes anything. Used as the first step of the setup
guide (docs/FNV-MCP-SETUP.md).
"""
import ctypes
import importlib.util
import os
import re
import struct
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


def large_address_aware(path):
    """True/False from the PE file header's Characteristics (IMAGE_FILE_LARGE_ADDRESS_AWARE), None if unreadable.
    The header is readable even in the Steam exe, whose code is encrypted on disk."""
    try:
        with open(path, 'rb') as f:
            head = f.read(4096)
        at = struct.unpack_from('<I', head, 0x3C)[0]
        if head[at:at + 4] != b'PE\0\0':
            return None
        return bool(struct.unpack_from('<H', head, at + 4 + 18)[0] & 0x0020)
    except (OSError, struct.error):
        return None


def setup_check(project):
    checks = []

    def add(name, ok, detail, fix=None, required=True):
        checks.append({'check': name, 'state': 'ok' if ok else ('missing' if required else 'optional'),
                       'detail': detail, 'fix': None if ok else fix})

    # First row: without an fnv block the other rows read the defaults, so they say "not set", never None.
    fnv = project.fnv
    add('fnv block', fnv is not None, 'present' if fnv is not None else 'the project file has no "fnv" block',
        'Start from projects/example-fnv.json: copy its "fnv" block into the project file (FNV-MCP-SETUP.md, step 2).')
    if fnv is None:
        from .config import FnvSettings
        fnv = FnvSettings()

    game = Path(project.game_root)
    exe = game / 'FalloutNV.exe'
    version = file_version(exe) if exe.exists() else None
    add('game', version == GAME_VERSION, '%s %s' % (exe, version or 'not found'),
        'The tools target the Steam FalloutNV.exe %s (Fallout: New Vegas, Steam build).' % GAME_VERSION)
    laa = large_address_aware(exe) if exe.exists() else None
    add('large address aware', bool(laa), '%s: %s' % (exe.name, {True: 'yes', False: 'no', None: 'not read'}[laa]),
        'Recommended: patch FalloutNV.exe to be large address aware (a 4 GB patcher) so a modded game does not run '
        'out of its 2 GB of address space.', required=False)
    nvse = game / 'nvse_1_4.dll'
    nvse_version = file_version(nvse) if nvse.exists() else None
    add('xNVSE', bool(nvse_version) and (game / 'nvse_loader.exe').exists(), 'nvse_1_4.dll %s' % (nvse_version or 'not found'),
        'Install xNVSE 6.4.9 or newer into the game folder (nvse_loader.exe and nvse_1_4.dll next to FalloutNV.exe).')

    mo2 = Path(fnv.mo2_root) if fnv.mo2_root else None
    profile = mo2 / 'profiles' / fnv.mo2_profile if mo2 and fnv.mo2_profile else None
    add('MO2 instance', bool(mo2 and (mo2 / 'ModOrganizer.exe').exists() and profile and profile.is_dir()),
        '%s, profile %s' % (mo2 or 'fnv.mo2_root not set', fnv.mo2_profile or 'not set'),
        'Create a portable Mod Organizer 2 instance for New Vegas with a separate dev profile, and set fnv.mo2_root '
        'and fnv.mo2_profile in the project file.')
    if mo2 and (mo2 / 'ModOrganizer.ini').exists():
        ini = (mo2 / 'ModOrganizer.ini').read_text(encoding='utf-8', errors='replace')
        titles = re.findall(r'^\d+\\title=(.+)$', ini, re.M)
        wanted = fnv.mo2_executable or 'NVSE'
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
    add('Zig 0.14.1', zig_version == '0.14.1', '%s %s' % (zig, zig_version) if zig else 'fnv.zig not set',
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
        '%s%s' % (installed or 'no MO2 instance', ' (installed build %s)' % current.get('build_id') if current else ''),
        'With the game closed, call fnv_companion_install.')
    if current and installed and installed.exists():
        same = fnv_link.file_digest(installed) == current.get('sha256')
        sources = current.get('source') == fnv_link.source_hashes()
        add('companion up to date', same and sources, 'installed DLL matches its receipt: %s; sources unchanged since: %s'
            % (same, sources), 'Rebuild and install it: fnv_companion_install (game closed).')

    from . import fnv_engine
    path = None
    try:
        path, decomp, symdb = fnv_engine.default_paths(project)
        engine = fnv_engine.load(path, decomp, symdb)
        add('engine map', True, '%s (%s)' % (path, engine.identity().get('gameBuild')))
    except Exception as e:
        add('engine map', False, str(e), 'Generate it from your fnv_decomp checkout: .venv\\Scripts\\python.exe -m '
            'engine_map.generate --decomp <checkout> --out %s (FNV-MCP-SETUP.md, step 4).'
            % (path or '<state_dir>\\engine_map.json'))
    from . import fnv_symdb
    symdb = fnv_symdb.default_path(project)
    add('symbol database', symdb.is_file(), str(symdb),
        'Optional: source lines, named locals and exact layouts. Build it with engine_map.symdb (FNV-ENGINE-TOOLS.md, '
        '"Symbol database").', required=False)
    proofs = os.environ.get('FNVSYM_ROOT')
    add('proof folder', bool(proofs and Path(proofs).is_dir()), proofs or 'FNVSYM_ROOT not set',
        'Optional, for the fnv_name_* tools only: set the FNVSYM_ROOT environment variable (in the MCP server '
        'registration) to a fnvsym checkout.', required=False)
    capstone = importlib.util.find_spec('capstone') is not None
    add('capstone', capstone, 'installed' if capstone else 'not installed',
        'Optional, for fnv_probe and fnv_atlas: .venv\\Scripts\\python.exe -m pip install capstone==5.0.7.',
        required=False)
    if getattr(project, 'plugin', None) or getattr(project, 'helper', None):
        helper = getattr(project, 'helper', None)
        add('GECK helper', bool(helper and Path(helper).is_file()), str(helper) if helper else 'helper not set',
            'The geck_* tools need the helper: set "helper" and build it with build_helper.py --project <file> '
            '(docs/SOURCE-SETUP.md).')
    clean = fnv.clean_exe
    if clean and Path(clean).exists():
        data = Path(clean).read_bytes()[:4096]
        add('unpacked exe (patch map)', b'.bind' not in data, str(clean),
            'fnv.clean_exe must be an unpacked FalloutNV.exe (Steamless output, no .bind section).', required=False)
    else:
        add('unpacked exe (patch map)', False, 'fnv.clean_exe not set',
            'Optional, for fnv_patch_map: your own unpacked FalloutNV.exe (Steamless), read locally only.',
            required=False)
    control = fnv.engine_control or 'read'
    add('full control', control == 'full', 'fnv.engine_control = %s' % control,
        'Optional: set fnv.engine_control to "full" to allow the act tools (memory writes, engine calls, '
        'console changes, hot-swap).', required=False)
    missing = [c['check'] for c in checks if c['state'] == 'missing']
    return {'ready': not missing, 'missing': missing, 'checks': checks,
            'notes': ['Turn Discord\'s in-game overlay off globally (Discord > User Settings > Game Overlay): its hook '
                      'crashed the game at startup with JIP LN loaded on the test machine.',
                      'Launch from Claude Code or an MCP client whose job object allows breakaway, or the bridge '
                      'falls back to opening a shortcut through Explorer.']}
