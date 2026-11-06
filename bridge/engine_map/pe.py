"""Minimal read-only PE32 reader for your own FalloutNV.exe.

Nothing read here is written out except addresses, sizes, names the game itself contains (command
names, parameter type labels, import names) and hashes. Never copy code bytes into generated files.
"""
import hashlib
import struct
from pathlib import Path


class Image:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        d = self.data
        if d[:2] != b'MZ':
            raise ValueError('not a PE file: %s' % path)
        pe = struct.unpack_from('<I', d, 0x3c)[0]
        if d[pe:pe + 4] != b'PE\0\0':
            raise ValueError('not a PE file: %s' % path)
        self.machine = struct.unpack_from('<H', d, pe + 4)[0]
        if self.machine != 0x14c:
            raise ValueError('not a 32-bit x86 image')
        nsec = struct.unpack_from('<H', d, pe + 6)[0]
        optsize = struct.unpack_from('<H', d, pe + 20)[0]
        opt = pe + 24
        self.base = struct.unpack_from('<I', d, opt + 28)[0]
        self.data_dirs = [struct.unpack_from('<II', d, opt + 96 + 8 * i) for i in range(16)]
        self.sections = []
        for i in range(nsec):
            o = opt + optsize + 40 * i
            name = d[o:o + 8].rstrip(b'\0').decode('ascii', 'replace')
            vsize, rva, rawsize, raw = struct.unpack_from('<IIII', d, o + 8)
            chars = struct.unpack_from('<I', d, o + 36)[0]
            self.sections.append({'name': name, 'va': self.base + rva, 'vsize': vsize, 'raw': raw,
                                  'rawsize': rawsize, 'exec': bool(chars & 0x20000000)})

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()

    @property
    def steam_packed(self):
        """SteamStub DRM adds a .bind section and keeps the real code encrypted on disk."""
        return any(s['name'] == '.bind' for s in self.sections)

    def section_of(self, va):
        for s in self.sections:
            if s['va'] <= va < s['va'] + max(s['vsize'], s['rawsize']):
                return s
        return None

    def read(self, va, size):
        s = self.section_of(va)
        if not s:
            return b''
        off = va - s['va']
        if off >= s['rawsize']:
            return b'\0' * size
        return self.data[s['raw'] + off: s['raw'] + min(off + size, s['rawsize'])]

    def u32(self, va):
        b = self.read(va, 4)
        return struct.unpack('<I', b)[0] if len(b) == 4 else None

    def cstr(self, va, limit=512):
        if not va:
            return ''
        return self.read(va, limit).split(b'\0', 1)[0].decode('latin-1')

    def is_code(self, va):
        s = self.section_of(va)
        return bool(s and s['exec'])

    def imports(self):
        """IAT slot address -> 'module!name' (or 'module!#ordinal')."""
        rva, size = self.data_dirs[1]
        out = {}
        if not rva:
            return out
        desc = self.base + rva
        while True:
            raw = self.read(desc, 20)
            if len(raw) < 20:
                break
            oft, _, _, name_rva, ft = struct.unpack('<IIIII', raw)
            if not (oft or name_rva or ft):
                break
            module = self.cstr(self.base + name_rva)
            thunk, slot = self.base + (oft or ft), self.base + ft
            while True:
                value = self.u32(thunk)
                if not value:
                    break
                if value & 0x80000000:
                    label = '#%d' % (value & 0xffff)
                else:
                    label = self.cstr(self.base + value + 2)
                out[slot] = '%s!%s' % (module, label)
                thunk += 4
                slot += 4
            desc += 20
        return out
