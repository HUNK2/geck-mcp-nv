"""Build a read-only lookup index of a master file (FalloutNV.esm, DLC .esm).

    python3 esm_index.py [--data DIR] [--out DIR] FalloutNV.esm [DeadMoney.esm ...]

Writes state/index/<master>.json.gz with every record that has an EditorID:
  {"edid": [type, "FORMID", full_name, model_path], ...}
Duplicated EditorIDs map to a list of such entries. The master is never
modified; its size and SHA-256 are recorded so a game update invalidates the index.
Standard library only.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geck_mcp.esp import codec  # noqa: E402

ROOT = Path(__file__).resolve().parent
def default_data():
    """FNV Data folder: $FNV_DATA, else the usual Steam location (Windows only, decision D5)."""
    if os.environ.get('FNV_DATA'):
        return Path(os.environ['FNV_DATA'])
    return Path(r'C:\Program Files (x86)\Steam\steamapps\common\Fallout New Vegas\Data')


DEFAULT_DATA = default_data()
# Bulk geometry records never carry useful EditorIDs; skipping them avoids decompression.
SKIP = {b'LAND', b'NAVM', b'NAVI', b'PGRD'}


def build_index(path):
    started = time.monotonic()
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    entries, counts, total = {}, {}, 0
    wanted = None
    for rec, _groups in codec.iter_records(data):
        total += 1
        if rec.type.encode() in SKIP:
            continue
        counts[rec.type] = counts.get(rec.type, 0) + 1
        try:
            edid = rec.editor_id
        except (codec.FormatError, ValueError, UnicodeDecodeError):
            continue
        if not edid:
            continue
        full = rec.get('FULL')
        model = rec.get('MODL')
        row = [rec.type, '%08X' % rec.form_id,
               codec.cstring(full) if full else None,
               codec.cstring(model) if model else None]
        if edid in entries:
            existing = entries[edid]
            entries[edid] = (existing if isinstance(existing[0], list) else [existing]) + [row]
        else:
            entries[edid] = row
    del wanted
    return {'master': path.name, 'size': len(data), 'sha256': digest, 'records': total,
            'indexed': len(entries), 'types': counts,
            'seconds': round(time.monotonic() - started, 1), 'entries': entries}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('masters', nargs='*', default=['FalloutNV.esm'])
    parser.add_argument('--data', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--out', type=Path, default=ROOT / 'state' / 'index')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name in args.masters:
        index = build_index(args.data / name)
        target = args.out / (name + '.json.gz')
        with gzip.open(target, 'wt', encoding='utf-8') as stream:
            json.dump(index, stream, separators=(',', ':'))
        summary = {k: v for k, v in index.items() if k != 'entries'}
        summary['out'] = str(target)
        print(json.dumps(summary))


if __name__ == '__main__':
    main()
