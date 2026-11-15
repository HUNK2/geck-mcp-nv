"""Byte-exact reader/writer for Fallout: New Vegas plugin files (TES4 format).

Standard library only. Layout reference (FNVEdit):
  https://tes5edit.github.io/fopdoc/FalloutNV/Records.html
  https://tes5edit.github.io/fopdoc/FalloutNV/Groups.html

Record header (24 bytes): type[4] dataSize:u32 flags:u32 formId:u32 vc1:u32 formVersion:u16 vc2:u16
Group header  (24 bytes): 'GRUP' groupSize:u32 label[4] groupType:i32 stamp[8]
Subrecord header (6 bytes): type[4] size:u16. A preceding XXXX subrecord (size 4)
holds the u32 size of the next subrecord, whose own size field is then 0.
Compressed records (flag 0x00040000): data = u32 decompressedSize + zlib stream.

parse(write(x)) == x and write(parse(b)) == b for well-formed input. Compressed
record payloads are kept verbatim until a caller replaces the subrecords, so
round-trips never depend on zlib settings.
"""
import codecs
from dataclasses import dataclass, field
import struct
import zlib

RECORD_HEADER = struct.Struct('<4sIIIIHH')
GROUP_HEADER = struct.Struct('<4sI4si8s')
SUB_HEADER = struct.Struct('<4sH')
COMPRESSED = 0x00040000
MAX_SUBRECORD = 0xFFFF

GROUP_TYPES = {
    0: 'top', 1: 'world children', 2: 'interior cell block', 3: 'interior cell sub-block',
    4: 'exterior cell block', 5: 'exterior cell sub-block', 6: 'cell children',
    7: 'topic children', 8: 'cell persistent children', 9: 'cell temporary children',
    10: 'cell visible distant children',
}


class FormatError(ValueError):
    """Malformed plugin bytes. `offset` is the absolute file position."""

    def __init__(self, message, offset=None):
        super().__init__(message if offset is None else '%s at offset %d' % (message, offset))
        self.offset = offset


@dataclass
class Subrecord:
    tag: str
    data: bytes
    xxxx: bool = field(default=False, compare=False)   # read with a preceding XXXX: written back with it (P4)

    def __repr__(self):
        return 'Subrecord(%s, %d bytes)' % (self.tag, len(self.data))


@dataclass
class Record:
    type: str
    form_id: int
    flags: int = 0
    vc1: int = 0
    form_version: int = 15
    vc2: int = 0
    _subrecords: list = None          # decoded subrecords, or None while still compressed
    _compressed: bytes = None         # original compressed payload, kept for exact round-trip
    warnings: list = None             # read problems the record survived (a wrong zlib checksum; P2)

    @property
    def compressed(self):
        return bool(self.flags & COMPRESSED)

    @property
    def subrecords(self):
        if self._subrecords is None:
            found = []
            self._subrecords = parse_subrecords(decompress(self._compressed, found), 0)
            if found:
                self.warnings = (self.warnings or []) + found
        return self._subrecords

    @subrecords.setter
    def subrecords(self, value):
        self._subrecords = list(value)
        self._compressed = None       # re-encoded on write

    def get(self, tag, default=None):
        for sub in self.subrecords:
            if sub.tag == tag:
                return sub.data
        return default

    def get_all(self, tag):
        return [s.data for s in self.subrecords if s.tag == tag]

    @property
    def editor_id(self):
        value = self.get('EDID')
        return cstring(value) if value is not None else None

    def __repr__(self):
        return 'Record(%s %08X %s)' % (self.type, self.form_id, self.editor_id if self._subrecords is not None or not self.compressed else '<compressed>')


@dataclass
class Group:
    label: bytes
    group_type: int
    stamp: bytes = b'\0' * 8
    children: list = field(default_factory=list)

    @property
    def label_text(self):
        """Top groups use a record type; cell/topic groups use a FormID or grid/block numbers."""
        if self.group_type == 0:
            return self.label.decode('latin-1')     # display only: a corrupt label never raises (P3)
        if self.group_type in (2, 3):
            return str(struct.unpack('<i', self.label)[0])
        if self.group_type in (4, 5):
            y, x = struct.unpack('<hh', self.label)
            return '%d,%d' % (x, y)
        return '%08X' % struct.unpack('<I', self.label)[0]

    def __repr__(self):
        return 'Group(%s %s, %d children)' % (GROUP_TYPES.get(self.group_type, self.group_type),
                                              self.label_text, len(self.children))


@dataclass
class Plugin:
    header: Record
    groups: list = field(default_factory=list)

    def records(self):
        """Yield (record, path) for every record, depth first; path is the list of enclosing groups."""
        def walk(items, path):
            for item in items:
                if isinstance(item, Group):
                    yield from walk(item.children, path + [item])
                else:
                    yield item, path
        yield self.header, []
        yield from walk(self.groups, [])

    def top_group(self, record_type):
        for g in self.groups:
            if g.group_type == 0 and g.label == record_type.encode('ascii'):
                return g
        return None


# -- helpers -----------------------------------------------------------------------

def _cp1252_bytes(error):
    # cp1252 leaves 0x81, 0x8D, 0x8F, 0x90 and 0x9D undefined; plugins can still hold them. Read each as the
    # same code point (as Latin-1 does), so the text survives and writes back to the same byte.
    if isinstance(error, UnicodeDecodeError):
        return ''.join(chr(b) for b in error.object[error.start:error.end]), error.end
    text = error.object[error.start:error.end]
    if isinstance(error, UnicodeEncodeError) and all(0x80 <= ord(c) <= 0x9F for c in text):
        return bytes(ord(c) for c in text), error.end
    raise error


codecs.register_error('cp1252-bytes', _cp1252_bytes)


def cstring(data):
    """Decode a zero-terminated cp1252 string (tolerates a missing terminator and cp1252's undefined bytes)."""
    if data.endswith(b'\0'):
        data = data[:-1]
    return data.decode('cp1252', 'cp1252-bytes')


def text(data):
    """cp1252 bytes as text, keeping the bytes cp1252 leaves undefined (they write back unchanged; R16)."""
    return bytes(data).decode('cp1252', 'cp1252-bytes')


def zstring(text):
    return text.encode('cp1252', 'cp1252-bytes') + b'\0'


# The largest size a compressed record or BSA file may declare (release audit L4). Real ones stay far below it:
# the biggest vanilla record inflates to a few MB.
MAX_INFLATE = 256 * 1024 * 1024


def inflate(stream, declared, wbits=15):
    """A zlib stream inflated to at most `declared` bytes, the size the file states for it. More output, a declared
    size above MAX_INFLATE or a stream that ends early raise zlib.error, as zlib.decompress would for a bad stream."""
    if declared < 0 or declared > MAX_INFLATE:
        raise zlib.error('declared size %d is above the %d byte limit' % (declared, MAX_INFLATE))
    d = zlib.decompressobj(wbits)
    data = d.decompress(stream, declared + 1)
    if len(data) > declared or d.unconsumed_tail:
        raise zlib.error('inflates past its declared size of %d bytes' % declared)
    if not d.eof:
        raise zlib.error('incomplete or truncated stream')
    return data


def decompress(payload, warnings=None):
    """The record bytes of a compressed payload. A stream that inflates to the declared size but fails its Adler-32
    (FalloutNV.esm LAND 00150FC0) is kept, with a line in `warnings`; anything else that does not inflate is a
    FormatError (P2)."""
    if len(payload) < 4:
        raise FormatError('compressed record shorter than its size prefix')
    size = struct.unpack_from('<I', payload)[0]
    try:
        data = inflate(payload[4:], size)
    except zlib.error as e:
        if size > MAX_INFLATE:
            raise FormatError('compressed record does not inflate (%s)' % e) from None
        try:
            raw = zlib.decompressobj(-15)              # the deflate stream without the zlib header and checksum
            data = raw.decompress(payload[6:], size + 1) + raw.flush()
        except zlib.error:
            raise FormatError('compressed record does not inflate (%s)' % e) from None
        if not raw.eof or len(data) != size:
            raise FormatError('compressed record does not inflate (%s)' % e) from None
        if warnings is not None:
            warnings.append('zlib checksum does not match (%s); the inflated bytes have the declared size and '
                            'are kept' % e)
    if len(data) != size:
        raise FormatError('decompressed size %d != declared %d' % (len(data), size))
    return data


def compress(data, level=6):
    return struct.pack('<I', len(data)) + zlib.compress(data, level)


# -- reading -----------------------------------------------------------------------

def parse_subrecords(data, base=0):
    subs, pos, pending = [], 0, None
    while pos < len(data):
        if pos + 6 > len(data):
            raise FormatError('truncated subrecord header', base + pos)
        tag, size = SUB_HEADER.unpack_from(data, pos)
        pos += 6
        big = pending is not None
        if pending is not None:
            if size != 0:
                raise FormatError('subrecord after XXXX must declare size 0', base + pos - 6)
            size, pending = pending, None
        elif tag == b'XXXX':
            if size != 4 or pos + 4 > len(data):
                raise FormatError('malformed XXXX', base + pos - 6)
            pending = struct.unpack_from('<I', data, pos)[0]
            pos += 4
            continue
        if pos + size > len(data):
            raise FormatError('subrecord %r overruns record' % tag, base + pos)
        try:
            name = _tag(tag, base + pos - 6)
        except UnicodeDecodeError:
            raise FormatError('non-ASCII subrecord tag', base + pos - 6)
        subs.append(Subrecord(name, bytes(data[pos:pos + size]), big))
        pos += size
    if pending is not None:
        raise FormatError('XXXX at end of record', base + pos)
    return subs


def _tag(raw, pos):
    """A record or subrecord type: 4 ASCII characters, else FormatError with the offset (P3)."""
    try:
        return bytes(raw).decode('ascii')
    except UnicodeDecodeError:
        raise FormatError('type %r is not 4 ASCII characters' % bytes(raw), pos) from None


def _parse_record(data, pos, end, lazy):
    if pos + 24 > end:
        raise FormatError('truncated record header', pos)
    tag, size, flags, form_id, vc1, version, vc2 = RECORD_HEADER.unpack_from(data, pos)
    body_start, body_end = pos + 24, pos + 24 + size
    if body_end > end:
        raise FormatError('record %r overruns its container' % tag, pos)
    rec = Record(_tag(tag, pos), form_id, flags, vc1, version, vc2)
    body = bytes(data[body_start:body_end])
    if flags & COMPRESSED:
        rec._compressed = body
        if not lazy:
            found = []
            rec._subrecords = parse_subrecords(decompress(body, found), body_start)
            rec.warnings = found or None
    else:
        rec._subrecords = parse_subrecords(body, body_start)
    return rec, body_end


def _parse_items(data, pos, end, lazy):
    items = []
    while pos < end:
        if data[pos:pos + 4] == b'GRUP':
            if pos + 24 > end:
                raise FormatError('truncated group header', pos)
            _, size, label, gtype, stamp = GROUP_HEADER.unpack_from(data, pos)
            if size < 24 or pos + size > end:
                raise FormatError('group size %d invalid' % size, pos)
            group = Group(bytes(label), gtype, bytes(stamp))
            group.children = _parse_items(data, pos + 24, pos + size, lazy)
            items.append(group)
            pos += size
        else:
            rec, pos = _parse_record(data, pos, end, lazy)
            items.append(rec)
    return items


def parse(data, lazy=True):
    """Parse a whole plugin. With lazy=True compressed records decode on first access."""
    data = memoryview(data)
    if bytes(data[:4]) != b'TES4':
        raise FormatError('not a TES4 plugin', 0)
    header, pos = _parse_record(data, 0, len(data), lazy)
    return Plugin(header, _parse_items(data, pos, len(data), lazy))


def iter_records(data, types=None):
    """Stream (record, group_labels) without building the tree; for large masters.

    `types` restricts which records are materialised (others are skipped cheaply).
    Compressed payloads stay lazy until `.subrecords` is read.
    """
    wanted = None if types is None else {t.encode('ascii') for t in types}
    data = memoryview(data)
    stack = []  # (end, label)
    pos, end = 0, len(data)
    while pos < end:
        while stack and pos >= stack[-1][0]:
            stack.pop()
        if pos + 24 > end:
            raise FormatError('truncated record or group header', pos)
        if bytes(data[pos:pos + 4]) == b'GRUP':
            _, size, label, gtype, _ = GROUP_HEADER.unpack_from(data, pos)
            if size < 24 or pos + size > end:
                raise FormatError('group size %d invalid' % size, pos)
            stack.append((pos + size, (bytes(label), gtype)))
            pos += 24
            continue
        tag, size = struct.unpack_from('<4sI', data, pos)
        if wanted is None or tag in wanted:
            rec, pos = _parse_record(data, pos, end, True)
            yield rec, [s[1] for s in stack]
        else:
            pos += 24 + size


# -- writing -----------------------------------------------------------------------

def encode_subrecords(subs):
    out = bytearray()
    for sub in subs:
        tag = sub.tag.encode('ascii')
        if len(tag) != 4:
            raise ValueError('subrecord tag must be 4 ASCII characters: %r' % sub.tag)
        if len(sub.data) > MAX_SUBRECORD or getattr(sub, 'xxxx', False):
            out += SUB_HEADER.pack(b'XXXX', 4) + struct.pack('<I', len(sub.data))
            out += SUB_HEADER.pack(tag, 0)
        else:
            out += SUB_HEADER.pack(tag, len(sub.data))
        out += sub.data
    return bytes(out)


def encode_record(rec):
    if rec.compressed:
        body = rec._compressed if rec._compressed is not None else compress(encode_subrecords(rec._subrecords))
    else:
        body = encode_subrecords(rec.subrecords)
    tag = rec.type.encode('ascii')
    if len(tag) != 4:
        raise ValueError('record type must be 4 ASCII characters: %r' % rec.type)
    return RECORD_HEADER.pack(tag, len(body), rec.flags, rec.form_id, rec.vc1,
                              rec.form_version, rec.vc2) + body


def encode_group(group):
    body = b''.join(encode_group(c) if isinstance(c, Group) else encode_record(c)
                    for c in group.children)
    if len(group.label) != 4 or len(group.stamp) != 8:
        raise ValueError('group label must be 4 bytes and stamp 8 bytes')
    return GROUP_HEADER.pack(b'GRUP', 24 + len(body), group.label, group.group_type, group.stamp) + body


def write(plugin):
    return encode_record(plugin.header) + b''.join(
        encode_group(g) if isinstance(g, Group) else encode_record(g) for g in plugin.groups)
