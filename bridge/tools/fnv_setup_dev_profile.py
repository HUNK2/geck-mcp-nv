"""Set up the portable MO2 dev instance for fnv-mcp from the user's own downloads.

Dry run by default: prints what it would do. --apply does it and writes an install manifest
(<mo2>/fnv-mcp-install.json) listing every file it added to the game folder, for undo.

    py -3.12 tools\\fnv_setup_dev_profile.py --mo2 C:\\Modding\\FNV-Dev --downloads C:\\Modding\\downloads [--apply]

Steps (each skipped when already done):
 1. the MO2 portable instance: ModOrganizer.ini for New Vegas (Steam) as MO2's wizard would write it,
    plus portable.txt, unless the instance already exists
 2. profile "Dev" (copy of Default) with profile-specific saves and INIs, INIs copied from Documents
 3. profile INI settings: bAlwaysActive=1 (keeps running unfocused; see SETUP-NOTES), intro skipped,
    windowed 1280x720
 4. xNVSE from nvse_*.7z: loader and DLLs into the game folder (it cannot go through MO2)
 5. JIP LN NVSE and Yvile's Crash Logger archives -> MO2 mods, enabled in the Dev profile
Nothing is downloaded and no game file is overwritten (existing files with other content stop the run).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

GAME = Path(r'C:\Program Files (x86)\Steam\steamapps\common\Fallout New Vegas')
DOCS = Path(os.environ.get('USERPROFILE', '~')) / 'Documents' / 'My Games' / 'FalloutNV'
SEVENZIP = Path(r'C:\Program Files\7-Zip\7z.exe')
INI_FILES = ('Fallout.ini', 'FalloutPrefs.ini', 'FalloutCustom.ini')
INI_SETTINGS = {
    'Fallout.ini': {'General': {'bAlwaysActive': '1', 'SIntroSequence': ''},
                    'Display': {'bFull Screen': '0'}},   # some Fallout.ini copies carry it too
    'FalloutPrefs.ini': {'Display': {'bFull Screen': '0', 'iSize W': '1280', 'iSize H': '720'}},
}
MODS = [  # (MO2 mod name, archive name pattern, file that must end up in the mod)
    ('JIP LN NVSE Plugin', re.compile(r'jip.?ln', re.I), 'NVSE/Plugins/jip_nvse.dll'),
    ("Yvile's Crash Logger", re.compile(r'crash.?logger', re.I), 'NVSE/Plugins/CrashLogger.dll'),
]


class Plan:
    def __init__(self, apply):
        self.apply = apply
        self.added = []

    def do(self, text, fn=None):
        print(('DO   ' if self.apply else 'PLAN ') + text)
        if self.apply and fn:
            fn()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def set_ini(path, settings):
    """Set key=value in sections, keeping everything else byte for byte (CRLF files stay CRLF)."""
    raw = Path(path).read_bytes().decode('latin-1')
    newline = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.split(newline)
    for section, values in settings.items():
        start = next((i for i, l in enumerate(lines) if l.strip().lower() == '[%s]' % section.lower()), None)
        if start is None:
            lines += ['[%s]' % section] + ['%s=%s' % kv for kv in values.items()]
            continue
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith('[')), len(lines))
        for key, value in values.items():
            hit = next((i for i in range(start + 1, end) if lines[i].split('=', 1)[0].strip().lower() == key.lower()), None)
            if hit is not None:
                lines[hit] = '%s=%s' % (lines[hit].split('=', 1)[0], value)
            else:
                at = end
                while at > start + 1 and not lines[at - 1].strip():   # before the section's trailing blank lines
                    at -= 1
                lines.insert(at, '%s=%s' % (key, value))
                end += 1
    mode = os.stat(path).st_mode
    read_only = not mode & stat.S_IWRITE
    if read_only:           # Fallout.ini is often kept read-only; keep it that way after editing
        os.chmod(path, mode | stat.S_IWRITE)
    Path(path).write_bytes(newline.join(lines).encode('latin-1'))
    if read_only:
        os.chmod(path, mode)


def extract(archive, into):
    into.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(SEVENZIP), 'x', '-y', '-o' + str(into), str(archive)], check=True,
                   stdout=subprocess.DEVNULL)


def content_root(folder):
    """The folder holding NVSE/ (archives put it at the top, under Data/, or one level deeper)."""
    for candidate in [folder] + [p for p in folder.rglob('*') if p.is_dir()]:
        if (candidate / 'NVSE').is_dir() or (candidate / 'nvse').is_dir():
            return candidate
    return folder


def newest(downloads, pattern):
    hits = [p for p in downloads.iterdir() if p.is_file() and pattern.search(p.name)
            and p.suffix.lower() in ('.7z', '.zip', '.rar')]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--mo2', type=Path, default=Path(r'C:\Modding\FNV-Dev'))
    ap.add_argument('--profile', default='Dev')
    ap.add_argument('--downloads', type=Path, default=Path(r'C:\Modding\downloads'))
    ap.add_argument('--game', type=Path, default=GAME)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    plan = Plan(args.apply)
    problems = []

    # 1. MO2 instance
    ini = args.mo2 / 'ModOrganizer.ini'
    if not (args.mo2 / 'ModOrganizer.exe').exists():
        sys.exit('MO2 not found at %s: unpack Mod.Organizer-2.5.2.7z there first' % args.mo2)
    if not ini.exists():
        # What MO2 2.5.2's instance wizard writes for a portable instance (createinstancedialog.cpp
        # finish(), settings.cpp). game_edition is required: the FNV plugin has three variants and MO2
        # stops with "missing variant" without it. portable.txt locks this install to its own instance
        # even if another MO2 has a global instance selected (instancemanager.cpp currentInstance()).
        text = '\r\n'.join([
            '[General]',
            'gameName=New Vegas',
            'gamePath=@ByteArray(%s)' % str(args.game).replace('\\', '\\\\'),
            'game_edition=Steam',
            'selected_profile=@ByteArray(%s)' % args.profile,
            '',
            '[Settings]',
            'profile_local_inis=true',
            'profile_local_saves=true',
            'profile_archive_invalidation=false',
            '']).encode('utf-8')

        def create_instance():
            for sub in ('downloads', 'mods', 'profiles', 'overwrite'):
                (args.mo2 / sub).mkdir(exist_ok=True)
            (args.mo2 / 'portable.txt').touch()
            ini.write_bytes(text)
        plan.do('create the portable instance: %s (New Vegas, Steam, %s), portable.txt, '
                'downloads/mods/profiles/overwrite' % (ini, args.game), create_instance)
    mo2ini = ini.read_text(encoding='utf-8', errors='replace') if ini.exists() else 'gameName=New Vegas'
    if not re.search(r'gameName\s*=\s*New Vegas', mo2ini):
        problems.append('ModOrganizer.ini does not say gameName=New Vegas')
    print('MO2 instance: %s' % args.mo2)

    # 2. profile
    profiles = args.mo2 / 'profiles'
    profile = profiles / args.profile
    if not profile.exists():
        source = profiles / 'Default'
        plan.do('create profile %s as a copy of %s' % (profile, source),
                lambda: shutil.copytree(source, profile) if source.exists() else profile.mkdir(parents=True))
    settings = profile / 'settings.ini'
    plan.do('profile settings: LocalSaves=true, LocalSettings=true', lambda: (
        settings.exists() or settings.write_text('[General]\n'),
        set_ini(settings, {'General': {'LocalSaves': 'true', 'LocalSettings': 'true'}})))
    plan.do('create %s' % (profile / 'saves'), lambda: (profile / 'saves').mkdir(parents=True, exist_ok=True))
    for name in INI_FILES:
        src, dst = DOCS / name, profile / name
        if src.exists() and not dst.exists():
            plan.do('copy %s -> %s' % (src, dst), lambda s=src, d=dst: shutil.copy2(s, d))

    # 3. INI settings
    for name, values in INI_SETTINGS.items():
        plan.do('set %s in profile %s' % (values, name),
                lambda n=name, v=values: set_ini(profile / n, v) if (profile / n).exists() else None)

    # 4. xNVSE into the game folder
    nvse = newest(args.downloads, re.compile(r'^nvse_\d', re.I))
    if not nvse:
        problems.append('no nvse_*.7z in %s' % args.downloads)
    else:
        tmp = Path(tempfile.mkdtemp(prefix='fnvmcp-nvse-'))
        extract(nvse, tmp)
        root_files = [p for p in tmp.rglob('*') if p.is_file() and p.suffix.lower() in ('.dll', '.exe')
                      and 'data' not in [x.lower() for x in p.relative_to(tmp).parts[:-1]]]
        for p in root_files:
            dst = args.game / p.name
            if dst.exists() and sha256(dst) != sha256(p):
                problems.append('%s exists with different content; not overwritten' % dst)
                continue
            if not dst.exists():
                plan.do('xNVSE: copy %s -> %s' % (p.name, dst), lambda s=p, d=dst: (shutil.copy2(s, d),
                                                                                    plan.added.append(str(d))))
        data = next((p for p in tmp.iterdir() if p.is_dir() and p.name.lower() == 'data'), None)
        if data:
            target = args.mo2 / 'mods' / 'xNVSE Data'
            plan.do('xNVSE Data folder -> MO2 mod %s' % target, lambda: shutil.copytree(data, target, dirs_exist_ok=True))
            MODS_ENABLED.append('xNVSE Data')

    # 5. plugin mods
    for mod_name, pattern, required in MODS:
        archive = newest(args.downloads, pattern)
        if not archive:
            problems.append('no archive matching %s in %s' % (pattern.pattern, args.downloads))
            continue
        tmp = Path(tempfile.mkdtemp(prefix='fnvmcp-mod-'))
        extract(archive, tmp)
        root = content_root(tmp)
        if not (root / required).exists() and not any(p.as_posix().lower().endswith(required.lower())
                                                       for p in root.rglob('*')):
            problems.append('%s: %s not found inside %s' % (mod_name, required, archive.name))
            continue
        target = args.mo2 / 'mods' / mod_name
        plan.do('install %s from %s -> %s' % (mod_name, archive.name, target),
                lambda r=root, t=target: shutil.copytree(r, t, dirs_exist_ok=True))
        MODS_ENABLED.append(mod_name)

    modlist = profile / 'modlist.txt'
    if MODS_ENABLED:
        def enable():
            existing = modlist.read_text(encoding='utf-8').splitlines() if modlist.exists() else []
            names = {l[1:] for l in existing if l[:1] in '+-*'}
            header = [l for l in existing if l.startswith('#')]     # MO2's comment stays on top
            lines = header + ['+' + m for m in MODS_ENABLED if m not in names] + \
                [('+' + l[1:]) if l[1:] in MODS_ENABLED else l for l in existing if not l.startswith('#')]
            modlist.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        plan.do('enable in %s: %s' % (modlist, ', '.join(MODS_ENABLED)), enable)

    if args.apply and plan.added:
        manifest = args.mo2 / 'fnv-mcp-install.json'
        old = json.loads(manifest.read_text()) if manifest.exists() else {'gameFilesAdded': []}
        old['gameFilesAdded'] = sorted(set(old['gameFilesAdded']) | set(plan.added))
        manifest.write_text(json.dumps(old, indent=1))
        print('manifest: %s' % manifest)
    if not (args.game / 'nvse_loader.exe').exists() and not nvse:
        # MO2 adds the game plugin's executables, "NVSE" among them, each time it loads
        # (executableslist.cpp load -> addFromPlugin), but only when nvse_loader.exe is in the game folder
        print('NOTE: nvse_loader.exe is not in the game folder, so MO2 will have no "NVSE" executable.')
    for p in problems:
        print('PROBLEM ' + p)
    print('launch: "%s" -p %s run -e "NVSE"' % (args.mo2 / 'ModOrganizer.exe', args.profile))


MODS_ENABLED = []

if __name__ == '__main__':
    main()
