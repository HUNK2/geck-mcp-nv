"""Generate engine_map.json from your own Fallout: New Vegas decompilation checkout.

The map is pinned to Fallout: New Vegas PC 1.4.0.525 and is generated on your machine; nothing in it is
shipped with this repository. Inputs:

  --decomp    a checkout of github.com/momusdigital/fnv_decomp (required): build/units.json, the
              '// @fnv 0x...' markers in src/, and the class headers in include/
  --verified  fnvsym's proofs/verified.csv (optional): which names are proven, and by what evidence
  --exe       your own *unpacked* FalloutNV.exe 1.4.0.525 (optional): build check against the decomp's
              image probes, call graph, imports and the vanilla command tables. The Steam exe is
              DRM-packed; use a Steamless-unpacked copy. Only addresses, names and hashes are written.
  --nvse-src / --plugin-src LABEL=PATH   script-extender source trees for their command tables

    python -m engine_map.generate --decomp C:/src/fnv_decomp --verified C:/src/fnvsym/proofs/verified.csv \\
        --exe %USERPROFILE%/.fnvdecomp/FalloutNV.exe --nvse-src C:/src/NVSE --plugin-src jip=C:/src/JIP-LN-NVSE \\
        --out state/engine_map.json
"""
import argparse
import csv
import datetime
import hashlib
import json
import re
import subprocess
import sys
from bisect import bisect_right
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = 'engine_map'

from . import commands, headers, source  # noqa: E402
from .demangle import clean_name, demangle  # noqa: E402
from .pe import Image  # noqa: E402

SCHEMA = 1
GAME_BUILD = '1.4.0.525'
GENERATOR = 'geck-mcp-nv engine_map 1'


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def hx(value):
    return '0x%08X' % value


def git_head(path):
    try:
        return subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                              timeout=20).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def load_units(decomp):
    data = json.loads((decomp / 'build' / 'units.json').read_text(encoding='utf-8'))
    functions = {}
    for unit in data['units']:
        for f in unit['functions']:
            address = int(f['address'], 16)
            functions.setdefault(address, {'symbol': f['symbol'], 'size': f.get('size'), 'source': unit['source']})
    return data.get('note', ''), functions


def load_verified(path):
    out = {}
    with open(path, encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            try:
                address = int(row['addr'], 16)
            except (KeyError, ValueError):
                continue
            out[address] = row
    return out


def image_check(decomp, image):
    """Compare the exe with the decomp's hashed probes of the 1.4.0.525 image."""
    path = decomp / 'build' / 'image_check.json'
    if not path.exists():
        return {'checked': False, 'reason': 'build/image_check.json missing'}
    probes = json.loads(path.read_text(encoding='utf-8'))
    results = []
    for address, size, digest in probes['probes']:
        data = image.read(int(address, 16), size)
        results.append(hashlib.sha256(data).hexdigest() == digest)
    return {'checked': True, 'release': probes.get('release'), 'probes': len(results), 'matched': sum(results),
            'ok': all(results) and bool(results)}


def call_graph(image, functions):
    """Direct call/jump edges read from the code: E8/E9 rel32 whose target is a function entry, and
    FF 15 / FF 25 calls through the import table."""
    entries = sorted(functions)
    entry_set = set(entries)
    imports = image.imports()
    edges, tails, import_calls = {}, {}, {}
    pattern = re.compile(rb'(?=([\xe8\xe9])(....))', re.S)
    indirect = re.compile(rb'(?=\xff([\x15\x25])(....))', re.S)
    for address in entries:
        size = functions[address]['size'] or 0
        if size <= 0:
            continue
        code = image.read(address, size)
        out, jumps = set(), set()
        for m in pattern.finditer(code):
            target = (address + m.start() + 5 + int.from_bytes(m.group(2), 'little', signed=True)) & 0xffffffff
            if target in entry_set and not (address <= target < address + size):
                (out if m.group(1) == b'\xe8' else jumps).add(target)
        used = set()
        for m in indirect.finditer(code):
            slot = int.from_bytes(m.group(2), 'little')
            if slot in imports:
                used.add(imports[slot])
        if out:
            edges[address] = sorted(out)
        if jumps:
            tails[address] = sorted(jumps)
        if used:
            import_calls[address] = sorted(used)
    return edges, tails, import_calls


def source_call_graph(decomp, records):
    """Without the exe: placeholder names (FUN_xxxxxxxx) referenced inside each function body."""
    by_file = {}
    for r in records.values():
        if r.get('file') and r.get('lines'):
            by_file.setdefault(r['file'], []).append(r)
    entries = set(records)
    edges = {}
    for rel, items in by_file.items():
        lines = source.read_lines(decomp / rel)
        for r in items:
            start, end = r['lines']
            text = '\n'.join(lines[start - 1:end])
            targets = {int(h, 16) for h in re.findall(r'FUN_([0-9a-fA-F]{8})', text)} & entries
            targets.discard(r['address'])
            if targets:
                edges[r['address']] = sorted(targets)
    return edges


def settings_from_proofs(verified):
    """Game/INI settings and other proven static objects, from proven initializer names."""
    out = []
    for address, row in verified.items():
        if row.get('status') != 'proven':
            continue
        m = re.match(r'^Init_(Setting|RTTI|hkClass|hkTypeInfo)_(.+)$', row.get('name', ''))
        g = re.search(r'g_([0-9a-fA-F]{8})', row.get('current', ''))
        if not m:
            continue
        full = None
        if m.group(1) == 'Setting':
            q = re.search(r'"+([A-Za-z_][\w]*(?::[A-Za-z_]\w*)?)"+', row.get('identity', ''))
            full = q.group(1) if q else None
        out.append({'kind': m.group(1), 'name': m.group(2), 'setting': full,
                    'address': hx(int(g.group(1), 16)) if g else None, 'initializer': hx(address)})
    out.sort(key=lambda x: (x['kind'], x['name'].lower()))
    return out


def settings_from_exe(image, statics, functions):
    """Fill each proven setting initializer's object address, full name and default value from its code
    (push value; push name; mov ecx, offset setting; call), then list the functions referencing it."""
    import struct
    for s in statics:
        if s['kind'] != 'Setting':
            continue
        init = int(s['initializer'], 16)
        code = image.read(init, 64)
        pushes, fld = [], None
        for i in range(len(code) - 4):
            op = code[i]
            if op == 0xB9:                        # mov ecx, imm32: the setting object
                obj = struct.unpack_from('<I', code, i + 1)[0]
                if image.section_of(obj):
                    s['address'] = s['address'] or hx(obj)
                break
            if op == 0x68:                        # push imm32
                pushes.append(('imm32', struct.unpack_from('<I', code, i + 1)[0]))
            elif op == 0x6A and i + 1 < len(code):  # push imm8 (sign-extended)
                pushes.append(('imm8', struct.unpack_from('<b', code, i + 1)[0]))
            elif op == 0xD9 and code[i + 1] == 0x05:  # fld dword [imm32]
                fld = struct.unpack_from('<I', code, i + 2)[0]
        name = None
        if pushes and pushes[-1][0] == 'imm32':
            text = image.cstr(pushes[-1][1], 96)
            if re.fullmatch(r'[A-Za-z_][\w]*(?::[A-Za-z_]\w*)?', text or ''):
                name = text
                s['setting'] = s.get('setting') or text
        prefix = (s['setting'] or s['name'])[:1]
        if prefix == 'f' and fld:
            raw = image.read(fld, 4)
            if len(raw) == 4:
                s['default'] = round(struct.unpack('<f', raw)[0], 6)
        elif name and len(pushes) >= 2:
            kind, value = pushes[-2]
            if prefix == 's' and kind == 'imm32':
                s['default'] = image.cstr(value, 200)
            elif prefix in ('i', 'u', 'b'):
                s['default'] = bool(value) if prefix == 'b' else value
    targets = {int(s['address'], 16): s for s in statics if s['kind'] == 'Setting' and s.get('address')}
    users = {}
    for address, unit in functions.items():
        size = unit['size'] or 0
        if size < 5:
            continue
        code = image.read(address, size)
        found = set()
        for k in range(4):
            for (value,) in struct.iter_unpack('<I', code[k:len(code) - ((len(code) - k) % 4)]):
                if value in targets:
                    found.add(value)
        for value in found:
            users.setdefault(value, []).append(address)
    for value, s in targets.items():
        init = int(s['initializer'], 16)
        s['usedBy'] = [hx(a) for a in sorted(users.get(value, [])) if a != init]


def status_of(row, placeholder):
    if row:
        status = row.get('status', '')
        if status == 'proven':
            return 'proven'
        if status == 'class proven':
            return 'class_proven'
        if status == 'probable':
            return 'probable'
        if status == 'rejected':
            return 'rejected' if placeholder else 'decomp'
    return 'unnamed' if placeholder else 'decomp'


def build(args):
    decomp = Path(args.decomp).resolve()
    note, units = load_units(decomp)
    log('units.json: %d functions' % len(units))
    verified = load_verified(args.verified) if args.verified else {}
    if verified:
        log('verified.csv: %d rows' % len(verified))

    records = {}
    for address, unit in units.items():
        qualified, signature = demangle(unit['symbol'])
        name, placeholder = clean_name(qualified)
        records[address] = {'address': address, 'decompName': name, 'placeholder': placeholder,
                            'signature': signature, 'size': unit['size'], 'file': unit['source']}

    # definitions: marker -> body, read per source file
    by_file = {}
    for r in records.values():
        by_file.setdefault(r['file'], []).append(r)
    located = 0
    for rel, items in sorted(by_file.items()):
        path = decomp / rel
        if not path.exists():
            continue
        lines = source.read_lines(path)
        marks = source.markers(lines)
        for r in items:
            short = r['decompName'].split('::')[-1]
            found = source.locate(lines, marks, r['address'], short)
            if found:
                r['marker'], start, end = found
                r['lines'] = [start, end]
                located += 1
            elif r['address'] in marks:
                r['marker'] = marks[r['address']][0] + 1
    log('definitions located: %d / %d' % (located, len(records)))

    image = check = None
    edges = tails = import_calls = {}
    commands_out = []
    if args.exe:
        image = Image(args.exe)
        if image.steam_packed:
            raise SystemExit('%s is the DRM-packed Steam exe; point --exe at an unpacked copy (Steamless)' % args.exe)
        check = image_check(decomp, image)
        log('image check: %s' % check)
        if check.get('checked') and not check['ok'] and not args.force:
            raise SystemExit('the exe does not match the decomp image probes (not 1.4.0.525?); use --force to override')
        edges, tails, import_calls = call_graph(image, units)
        log('call graph from the exe: %d callers, %d edges' % (len(edges), sum(map(len, edges.values()))))
    else:
        edges = source_call_graph(decomp, records)
        log('call graph from source placeholders: %d callers' % len(edges))

    param_names = commands.param_types(args.nvse_src) if args.nvse_src else {}
    if image:
        commands_out += commands.vanilla(image, param_names)
    if args.nvse_src:
        commands_out += commands.plugin(args.nvse_src, 'xNVSE', param_names)
    for spec in args.plugin_src or []:
        label, _, path = spec.partition('=')
        commands_out += commands.plugin(path, label, param_names)
    log('commands: %d' % len(commands_out))

    # every proven/probable address, even ones the decomp has no unit for
    for address, row in verified.items():
        if address not in records:
            records[address] = {'address': address, 'decompName': row.get('current') or 'FUN_%08x' % address,
                                'placeholder': True, 'signature': None, 'size': None, 'file': None,
                                'notInDecomp': True}

    functions = []
    for address in sorted(records):
        r = records[address]
        row = verified.get(address)
        status = status_of(row, r['placeholder'])
        name = row['name'] if status == 'proven' else r['decompName']
        entry = {'address': hx(address), 'name': name, 'status': status, 'decompName': r['decompName'],
                 'signature': r['signature'], 'size': r['size'], 'file': r['file']}
        if r.get('lines'):
            entry['lines'] = r['lines']
        if r.get('marker'):
            entry['marker'] = r['marker']
        if r.get('notInDecomp'):
            entry['notInDecomp'] = True
        if row and row.get('status'):
            entry['proof'] = {k: row.get(k) for k in ('status', 'name', 'overload', 'identity', 'behaviour', 'by')
                              if row.get(k)}
        if address in import_calls:
            entry['imports'] = import_calls[address]
        functions.append(entry)

    layouts = headers.collect(decomp, log)
    log('class layouts: %d' % len(layouts))

    statics = settings_from_proofs(verified)
    if image:
        settings_from_exe(image, statics, units)
        log('settings with addresses: %d' % sum(1 for s in statics if s['kind'] == 'Setting' and s.get('address')))

    return {
        'schema': SCHEMA,
        'game': {'name': 'Fallout: New Vegas', 'build': GAME_BUILD, 'platform': 'PC (Steam)', 'imageBase': '0x00400000'},
        'generated': {
            'by': GENERATOR,
            'at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
            'decomp': {'path': str(decomp), 'commit': git_head(decomp), 'note': note},
            'verified': {'path': str(Path(args.verified).resolve()), 'sha256': file_sha256(args.verified)}
            if args.verified else None,
            'exe': {'sha256': image.sha256, 'imageCheck': check} if image else None,
            'callGraph': 'exe: direct E8/E9 rel32 to function entries; indirect and virtual calls are not resolved'
            if image else 'source: FUN_xxxxxxxx placeholders referenced in each body only (pass --exe for the full graph)',
            'counts': {'functions': len(functions), 'proven': sum(f['status'] == 'proven' for f in functions),
                       'located': located, 'classes': len(layouts), 'commands': len(commands_out)},
        },
        'functions': functions,
        'calls': {hx(a): [hx(t) for t in ts] for a, ts in edges.items()},
        'tailCalls': {hx(a): [hx(t) for t in ts] for a, ts in tails.items()},
        'classes': layouts,
        'statics': statics,
        'commands': commands_out,
    }


def write_header(engine_map, path):
    """C header of proven function addresses, for native companions."""
    lines = ['/* Generated by %s from your own decomp checkout. Do not edit; do not ship. */' % GENERATOR,
             '/* Fallout: New Vegas %s. Proven names only. */' % GAME_BUILD,
             '#pragma once', '']
    seen = set()
    for f in engine_map['functions']:
        if f['status'] != 'proven':
            continue
        ident = re.sub(r'[^A-Za-z0-9_]', '_', f['name']).strip('_')
        ident = re.sub(r'_+', '_', ident)
        if not ident or ident in seen:
            continue
        seen.add(ident)
        lines.append('#define FNV_ADDR_%s %su' % (ident, f['address']))
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--decomp', required=True)
    parser.add_argument('--verified')
    parser.add_argument('--exe')
    parser.add_argument('--nvse-src')
    parser.add_argument('--plugin-src', action='append', help='LABEL=PATH of another script extender source tree')
    parser.add_argument('--out', required=True)
    parser.add_argument('--header', help='also write a C header of proven function addresses')
    parser.add_argument('--force', action='store_true', help='keep going when the exe fails the image check')
    args = parser.parse_args(argv)
    engine_map = build(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.tmp')
    tmp.write_text(json.dumps(engine_map, separators=(',', ':')), encoding='utf-8')
    tmp.replace(out)
    log('wrote %s (%.1f MB)' % (out, out.stat().st_size / 1e6))
    if args.header:
        write_header(engine_map, args.header)
        log('wrote %s' % args.header)
    print(json.dumps(engine_map['generated'], indent=1))


if __name__ == '__main__':
    main()
