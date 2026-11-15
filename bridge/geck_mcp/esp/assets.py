"""Asset lookup for validation: loose files under Data, FNV BSA archives, and NIF texture scans.

Standard library only. BSA layout (version 104, Fallout 3/NV):
  header 36 B: 'BSA\\0' version offset archiveFlags folderCount fileCount
               totalFolderNameLength totalFileNameLength fileFlags
  folder records 16 B each: hash u64, fileCount u32, offset u32
  per folder: [bzstring folder name if archiveFlags & 1] + fileCount * 16 B file records
  file-name block (if archiveFlags & 2): zero-terminated names, in file-record order
"""
import os
from pathlib import Path
import re
import struct
from . import codec as _codec  # noqa: F401 - registers the cp1252-bytes error handler (R16)


class AssetError(ValueError):
    pass


def bsa_extract(path, wanted):
    """Extract files from an FNV BSA. `wanted` is a set of lower-case 'folder\\file' paths.

    Returns {path: bytes}. File records: size u32 (bit 30 toggles the archive's default
    compression), offset u32 (absolute). With archive flag 0x100, data starts with a
    bstring of the full path. Compressed data = u32 original size + zlib stream.
    """
    import zlib
    wanted = {w.lower() for w in wanted}
    out = {}
    with open(path, 'rb') as stream:
        header = stream.read(36)
        if header[:4] != b'BSA\0':
            raise AssetError('%s is not a BSA archive' % path)
        version, offset, flags, folders, files, _fnl, name_len, _ff = struct.unpack('<8I', header[4:])
        stream.seek(offset)
        counts = [struct.unpack('<QII', stream.read(16))[1] for _ in range(folders)]
        entries = []
        for count in counts:
            length = stream.read(1)[0]
            folder = stream.read(length).rstrip(b'\0').decode('cp1252', 'cp1252-bytes')
            for _ in range(count):
                _hash, size, data_offset = struct.unpack('<QII', stream.read(16))
                entries.append((folder, size, data_offset))
        names = stream.read(name_len).split(b'\0')
        default_compressed = bool(flags & 0x4)
        for (folder, size, data_offset), name in zip(entries, names):
            full = (folder + '\\' + name.decode('cp1252', 'cp1252-bytes')).lower()
            if full not in wanted:
                continue
            compressed = default_compressed ^ bool(size & 0x40000000)
            size &= 0x3FFFFFFF
            stream.seek(data_offset)
            if flags & 0x100:
                skip = stream.read(1)[0]
                stream.seek(skip, os.SEEK_CUR)
                size -= skip + 1
            raw = stream.read(size)
            out[full] = (_codec.inflate(raw[4:], struct.unpack_from('<I', raw.ljust(4, bytes(1)))[0])
                         if compressed else raw)
    return out


def nif_block_types(data):
    """Block type names and per-block type list from a NIF 20.2.0.7 header (no body parsing)."""
    head, _, rest = data.partition(b'\n')
    if not head.startswith(b'Gamebryo File Format'):
        raise AssetError('not a NIF file')
    pos = len(head) + 1
    version, endian, user_version, num_blocks = struct.unpack_from('<IBII', rest, 0)
    if version != 0x14020007:
        raise AssetError('unsupported NIF version %08X' % version)
    p = 13
    user_version_2 = struct.unpack_from('<I', rest, p)[0]; p += 4
    for _ in range(3):              # creator, export info 1, export info 2 (short strings)
        n = rest[p]; p += 1 + n
    num_types = struct.unpack_from('<H', rest, p)[0]; p += 2
    types = []
    for _ in range(num_types):
        n = struct.unpack_from('<I', rest, p)[0]; p += 4
        types.append(rest[p:p + n].decode('ascii')); p += n
    index = struct.unpack_from('<%dH' % num_blocks, rest, p); p += 2 * num_blocks
    sizes = struct.unpack_from('<%dI' % num_blocks, rest, p); p += 4 * num_blocks
    num_strings, _max_len = struct.unpack_from('<II', rest, p); p += 8
    strings = []
    for _ in range(num_strings):
        n = struct.unpack_from('<I', rest, p)[0]; p += 4
        strings.append(rest[p:p + n].decode('cp1252', 'replace')); p += n
    num_groups = struct.unpack_from('<I', rest, p)[0]; p += 4 + 4 * num_groups
    data_start = len(head) + 1 + p
    offsets, off = [], data_start
    for size in sizes:
        offsets.append(off)
        off += size
    return {'dataStart': data_start, 'offsets': offsets,'userVersion': user_version, 'userVersion2': user_version_2,
            'blocks': [types[i & 0x7FFF] for i in index], 'sizes': list(sizes), 'strings': strings}


def bsa_names(path):
    """Return the set of lower-case 'folder\\file' paths stored in a BSA."""
    with open(path, 'rb') as stream:
        header = stream.read(36)
        if len(header) != 36 or header[:4] != b'BSA\0':
            raise AssetError('%s is not a BSA archive' % path)
        version, offset, flags, folders, files, _fnl, name_len, _ff = struct.unpack('<8I', header[4:])
        if version not in (103, 104):
            raise AssetError('%s: unsupported BSA version %d' % (path, version))
        if not flags & 1 or not flags & 2:
            raise AssetError('%s: archive lacks directory or file names' % path)
        stream.seek(offset)
        counts = [struct.unpack('<QII', stream.read(16))[1] for _ in range(folders)]
        dirs = []
        for count in counts:
            length = stream.read(1)[0]
            dirs.append(stream.read(length).rstrip(b'\0').decode('cp1252', 'cp1252-bytes'))
            stream.seek(16 * count, os.SEEK_CUR)
        names = stream.read(name_len).split(b'\0')
    out, i = set(), 0
    for folder, count in zip(dirs, counts):
        for _ in range(count):
            out.add((folder + '\\' + names[i].decode('cp1252', 'cp1252-bytes')).lower())
            i += 1
    if i != files:
        raise AssetError('%s: file count mismatch (%d names, %d declared)' % (path, i, files))
    return out


class AssetIndex:
    """Answers 'does Data\\<path> exist?' across loose files, extra roots and BSAs."""

    def __init__(self, data_dir=None, extra_roots=(), bsas=None):
        self.roots = [Path(p) for p in ([data_dir] if data_dir else []) + list(extra_roots)]
        self.archived = set()
        self.archives = []
        if bsas is None and data_dir:
            bsas = sorted(Path(data_dir).glob('*.bsa'))
        for bsa in bsas or []:
            self.archived |= bsa_names(bsa)
            self.archives.append(Path(bsa).name)
        self._lower = {}

    def _loose(self, root, rel):
        """Case-insensitive lookup below root (Windows paths are case-insensitive)."""
        path = root
        for part in rel.split('\\'):
            key = (str(path), part.lower())
            if key not in self._lower:
                try:
                    self._lower[key] = {p.lower(): p for p in os.listdir(path)}.get(part.lower())
                except (FileNotFoundError, NotADirectoryError):
                    self._lower[key] = None
            found = self._lower[key]
            if found is None:
                return None
            path = path / found
        return path if path.is_file() else None

    def find(self, rel):
        """rel is relative to Data, e.g. 'meshes\\salvatore\\x.nif'. Returns a description or None."""
        rel = rel.replace('/', '\\').strip('\\')
        for root in self.roots:
            hit = self._loose(root, rel)
            if hit:
                return {'where': 'loose', 'path': str(hit)}
        if rel.lower() in self.archived:
            return {'where': 'bsa'}
        return None


SIZED_DDS = re.compile(rb'([\x05-\xff])\x00\x00\x00([ -~]{1,255}?\.dds)', re.IGNORECASE)


def nif_textures(data):
    """Texture paths referenced by a NIF (FNV stores them as u32-length-prefixed strings)."""
    if not data.startswith(b'Gamebryo File Format') and not data.startswith(b'NetImmerse File Format'):
        raise AssetError('not a NIF file')
    found = []
    for match in SIZED_DDS.finditer(data):
        length, text = match.group(1)[0], match.group(2)
        if length == len(text):
            path = text.decode('cp1252', 'cp1252-bytes').replace('/', '\\')
            if not path.lower().startswith('textures\\'):
                path = 'textures\\' + path.lstrip('\\')
            if path not in found:
                found.append(path)
    return found
