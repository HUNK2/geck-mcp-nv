"""MSVC RTTI from your own FalloutNV.exe: class hierarchy and virtual tables.

32-bit layouts (MSVC 2008):
  TypeDescriptor            { void* vftable; void* spare; char name[] }      name ".?AVClass@@"
  CompleteObjectLocator     { 0; offset; cdOffset; TypeDescriptor*; ClassHierarchyDescriptor* }
  ClassHierarchyDescriptor  { 0; attributes; numBaseClasses; BaseClassDescriptor** }
  BaseClassDescriptor       { TypeDescriptor*; numContainedBases; mdisp; pdisp; vdisp; attributes }
  vftable[-1] = CompleteObjectLocator*
Only addresses and class names are written.
"""
import re
import struct

from .demangle import _undecorate, _dbghelp

UNDNAME_NO_ARGUMENTS = 0x2000


def class_name(mangled):
    """'.?AVFoo@Bar@@' -> 'Bar::Foo'."""
    body = mangled[4:] if mangled.startswith(('.?AV', '.?AU')) else mangled.lstrip('.')
    if '?$' in body and _undecorate:
        text = _dbghelp(mangled[1:], UNDNAME_NO_ARGUMENTS)
        if text:
            return re.sub(r'^(class|struct)\s+', '', text)
    head = body[:-2] if body.endswith('@@') else body
    parts = [p for p in head.split('@') if p]
    return '::'.join(reversed(parts)) if parts else mangled


def parse(image):
    data_sections = [s for s in image.sections if not s['exec'] and s['rawsize']]
    # 1. type descriptors: ".?AV"/".?AU" names 8 bytes after an aligned header
    tds = {}
    for s in data_sections:
        raw = image.data[s['raw']: s['raw'] + s['rawsize']]
        for m in re.finditer(rb'\.\?A[VU][\x21-\x7e]{1,400}?@@\x00', raw):
            off = m.start() - 8
            if off < 0 or off % 4:
                continue
            tds[s['va'] + off] = m.group(0)[:-1].decode('latin-1')
    # 2. complete object locators
    cols = {}
    for s in data_sections:
        raw = image.data[s['raw']: s['raw'] + s['rawsize']]
        for off in range(0, len(raw) - 20, 4):
            sig, offset, cd, td, chd = struct.unpack_from('<IIIII', raw, off)
            if sig or td not in tds:
                continue
            head = image.read(chd, 16)
            if len(head) < 16:
                continue
            chd_sig, attrs, nbases, bca = struct.unpack('<IIII', head)
            if chd_sig or not 0 < nbases < 256:
                continue
            bases = []
            for i in range(nbases):
                bcd = image.u32(bca + 4 * i)
                if not bcd:
                    break
                fields = image.read(bcd, 24)
                if len(fields) < 24:
                    break
                btd, contained, mdisp, pdisp, vdisp, battrs = struct.unpack('<IIiiiI', fields)
                if btd not in tds:
                    break
                bases.append({'name': class_name(tds[btd]), 'contained': contained, 'mdisp': mdisp})
            cols[s['va'] + off] = {'class': class_name(tds[td]), 'offset': offset, 'bases': bases,
                                   'attributes': attrs}
    # 3. vtables: a pointer to a locator, followed by code pointers
    vtables = []
    for s in data_sections:
        raw = image.data[s['raw']: s['raw'] + s['rawsize']]
        for off in range(0, len(raw) - 8, 4):
            col = struct.unpack_from('<I', raw, off)[0]
            if col not in cols:
                continue
            slots, k = [], off + 4
            while k + 4 <= len(raw):
                value = struct.unpack_from('<I', raw, k)[0]
                if not image.is_code(value):    # the next vtable's locator pointer is data, so this stops there too
                    break
                slots.append(value)
                k += 4
            if slots:
                vtables.append({'address': s['va'] + off + 4, 'locator': col, 'slots': slots})
    classes = {}
    for col, info in cols.items():
        c = classes.setdefault(info['class'], {'bases': [], 'directBases': [], 'vtables': []})
        if info['offset'] == 0 or not c['bases']:
            flat = info['bases'][1:] if info['bases'] and info['bases'][0]['name'] == info['class'] else info['bases']
            c['bases'] = [b['name'] for b in flat]
            direct, i = [], 0
            while i < len(flat):
                direct.append(flat[i]['name'])
                i += flat[i]['contained'] + 1
            c['directBases'] = direct
    for vt in vtables:
        info = cols[vt['locator']]
        classes[info['class']]['vtables'].append({'address': '0x%08X' % vt['address'], 'thisOffset': info['offset'],
                                                  'slots': ['0x%08X' % a for a in vt['slots']]})
    for c in classes.values():
        c['vtables'].sort(key=lambda v: (v['thisOffset'], v['address']))
    return classes

