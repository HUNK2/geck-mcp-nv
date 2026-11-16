"""fnv_warnings, first wave (W0): the engine's own warnings from the two log files xNVSE writes.

The engine calls its warning function from about 845 places; the retail body is empty. xNVSE hooks it when
[Logging] EnableGameErrorLog=1 is set in Data\\NVSE\\nvse_config.ini (Hooks_Logging.cpp, Hook_Logging_Init). It
then writes one line per warning, the formatted text plus a newline, with no time or caller, to
falloutnv_error.log (engine) and falloutnv_havok.log (Havok) in the game folder. The game rewrites both files at
each start unless [Logging] bAppendErrorLogs=1. The game holds them open, so `clear` only remembers a mark.

Live capture (W2 stage): the game opens both files with no read sharing, so nothing can read them while it runs
(seen live). `capture` puts a record hook on Warning 0x005B5E40 itself, chained behind xNVSE's jump when it is
there: each call's eight stack words, caller and thread go to a ring, and the bridge formats the printf text with
the format string read from the game (static strings). %s arguments are read when the capture is read: a string in a
temporary buffer may have changed by then. read and summary use the capture while the files are locked."""
import hashlib
import json
import re
import shutil
import struct
import time
from collections import Counter
from pathlib import Path

from .fnv_conv import dispatch, page, verdict
from .fnv_state import read_json, update_json

SOURCES = {'engine': 'falloutnv_error.log', 'havok': 'falloutnv_havok.log'}
CATEGORIES = ('MASTERFILE', 'SCRIPTS', 'MODELS', 'TEXTURES', 'ANIMATION', 'AI', 'Audio')
CONFIG_NAME = 'nvse_config.ini'
SECTION, KEY = 'Logging', 'EnableGameErrorLog'
PLUGIN_NAME = re.compile(r'[^\s\\/:*?"<>|,;=\'()]+\.(?:esp|esm)\b', re.I)
HEX_ID = re.compile(r'\b(?:0x[0-9A-Fa-f]+|(?=[0-9A-Fa-f]*\d)[0-9A-Fa-f]{8})\b')
NUMBER = re.compile(r'\b\d+(?:\.\d+)?\b')
TAIL = 256
WARNING_FN = 0x005B5E40        # int __cdecl Warning(const char *fmt, ...): an empty stub in retail, hooked by xNVSE
CAPTURE_LABEL = 'warnings_live'
CAPTURE_ARGS = 8
SNAP_ARG, SNAP_LEN = 1, 96     # the bytes behind stack word 1 are copied at the call: usually the first %s
SPEC = re.compile(r'%([-+ #0]*)(\*|\d+)?(?:\.(\*|\d+))?(hh|h|ll|l|I64|I32|L)?([diouxXcsSpfFeEgGn%])')


def format_printf(fmt, words, read_str):
    """C printf over 32-bit stack words: (text, words used). read_str(pointer) -> str or None. Missing arguments
    print as <?>; a 64-bit integer or a double takes two words."""
    out, pos, i = [], 0, 0

    def take(n=1):
        nonlocal i
        if i + n > len(words):
            i = len(words)
            return None
        got = words[i:i + n]
        i += n
        return got

    for m in SPEC.finditer(fmt):
        out.append(fmt[pos:m.start()])
        pos = m.end()
        flags, width, prec, length, conv = m.groups()
        if conv == '%':
            out.append('%')
            continue
        if width == '*':
            w = take()
            width = str(w[0]) if w else ''
        if prec == '*':
            pw = take()
            prec = str(pw[0]) if pw else None
        spec = '%' + (flags or '') + (width or '') + ('.' + prec if prec is not None else '')
        if conv in 'fFeEgG':
            got = take(2)
            out.append((spec + conv) % struct.unpack('<d', struct.pack('<II', *got))[0] if got else '<?>')
            continue
        wide = length in ('ll', 'I64')
        got = take(2 if wide else 1)
        if got is None:
            out.append('<?>')
            continue
        value = got[0] | (got[1] << 32) if wide else got[0]
        bits = 64 if wide else 32
        if conv in 'sS':
            text = read_str(value) if value else '(null)'
            out.append((spec + 's') % (text if text is not None else '<?0x%08X>' % value))
        elif conv == 'c':
            out.append(chr(value & 0xFF))
        elif conv in 'di':
            out.append((spec + 'd') % (value - (1 << bits) if value >= 1 << (bits - 1) else value))
        elif conv == 'p':
            out.append('%08X' % value)
        elif conv == 'n':
            continue
        else:
            out.append((spec + conv) % value)
    out.append(fmt[pos:])
    return ''.join(out), i


class WarningCapture:
    """The live hook on Warning (needs the engine companion)."""

    def __init__(self, live):
        self.live = live
        self.since = 0
        self._strings_cache = {}

    def hook(self):
        for h in self.live.link.one('hooks').get('hooks', []):
            if h.get('label') == CAPTURE_LABEL and h.get('stolen'):
                return h
        return None

    def start(self):
        if self.hook():
            return {'capturing': True, 'started': False, 'note': 'Already capturing.'}
        from .fnv_deep import decode_prologue
        from .fnv_link import hexs, word
        raw = self.live.reads([(WARNING_FN, 24)])[0]
        if raw is None:
            raise ValueError('Cannot read Warning 0x%08X' % WARNING_FN)
        stolen, relocs = decode_prologue(raw)
        result = self.live.link.one('hook', word(WARNING_FN), word(stolen), ','.join('%x' % r for r in relocs) or '-',
                                    word(2), '0', '0', '0', word(CAPTURE_ARGS), '0', '0', word(2 + SNAP_ARG),
                                    word(SNAP_LEN), hexs(CAPTURE_LABEL))
        if not result.get('ok'):
            raise ValueError('The companion refused the warning hook: %s' % result.get('error'))
        h = result['hook']
        self.since = int(self.live.u32s([int(h['seqAddress'], 16)])[0] or 0)
        return {'capturing': True, 'started': True, 'chainedBehindXnvse': raw[0] == 0xE9,
                'how': 'record hook on Warning 0x%08X (every thread), %d stack words per call'
                       % (WARNING_FN, CAPTURE_ARGS)}

    def stop(self):
        h = self.hook()
        if h:
            from .fnv_link import word
            self.live.link.one('unhook', word(h['id']))
        return {'capturing': False, 'stopped': bool(h)}

    def _read_strings(self, pointers, fresh=False):
        want = sorted({p for p in pointers if p and p >= 0x10000 and (fresh or p not in self._strings_cache)})
        if want:
            for p, raw in zip(want, self.live.reads([(p, 256) for p in want])):
                self._strings_cache[p] = None if raw is None else raw.split(b'\0', 1)[0].decode('latin-1')
        return self._strings_cache

    def rows(self, since=None):
        """(rows, lost) of the captured calls after since (the capture's start by default); None when off."""
        h = self.hook()
        if h is None:
            return None
        deep = getattr(self.live, 'deep', None)
        if deep is None:
            from .fnv_deep import DeepTools
            deep = DeepTools(self.live)
        _, records, lost = deep.records(h, since=self.since if since is None else since)
        parsed = []
        for r in records:
            args = list(r[12:12 + CAPTURE_ARGS])
            # A few callers pass a small number before the format (the leading-int quirk).
            first = 1 if len(args) > 1 and args[0] < 0xFFFF else 0
            snap = struct.pack('<24I', *r[24:48]).split(b'\0', 1)
            # The copy is a whole string only when it ended inside the 96 bytes.
            snapped = (args[SNAP_ARG], snap[0].decode('latin-1')) if len(snap) > 1 else None
            parsed.append((r, args[first], args[first + 1:], snapped))
        formats = self._read_strings([fmt for _, fmt, _, _ in parsed])
        values = [v for _, fmt, vals, _ in parsed if '%s' in (formats.get(fmt) or '') or '%S' in (formats.get(fmt) or '')
                  for v in vals]
        self._read_strings(values, fresh=True)
        rows = []
        for r, fmt_ptr, vals, snapped in parsed:
            fmt = formats.get(fmt_ptr)
            if fmt is None:
                text = '<format at 0x%08X unreadable>' % fmt_ptr
            else:
                def read_str(p, snapped=snapped):
                    if snapped and p == snapped[0]:
                        return snapped[1]          # copied at the call
                    return self._strings_cache.get(p)
                text, _ = format_printf(fmt, vals, read_str)
            text = text.rstrip('\r\n')
            rows.append({'seq': r[0], 'source': 'engine', 'category': category_of(text), 'text': text,
                         'caller': '0x%08X' % r[5], 'thread': r[2], 'live': True})
        try:
            engine = self.live.engine()
            names = {c: (engine.summary(int(c, 16)) or {}).get('name') for c in sorted({r['caller'] for r in rows})[:60]}
            for row in rows:
                row['callerName'] = names.get(row['caller'])
        except Exception:  # noqa: BLE001 - names are extra
            pass
        return rows, lost


def category_of(text):
    head, colon, _ = text.partition(':')
    if colon:
        for name in CATEGORIES:
            if head.strip().lower() == name.lower():
                return name
    return 'other'


def pattern_of(text):
    """The message with hex ids and numbers masked, to group lines that differ only in those."""
    return NUMBER.sub('#', HEX_ID.sub('<id>', text))


def plugins_in(text):
    return sorted(set(m.group(0) for m in PLUGIN_NAME.finditer(text)), key=lambda p: (p.lower(), p))


# ---------------------------------------------------------------- the config file (INI, edited line by line)
def _lines(text):
    return text.splitlines(keepends=True)


def _section_of(line):
    s = line.strip()
    return s[1:s.find(']')].strip().lower() if s.startswith('[') and ']' in s else None


def _key_of(line):
    s = line.strip()
    if s.startswith((';', '#', '[')) or '=' not in s:
        return None
    return s.partition('=')[0].strip().lower()


def read_setting(text, section=SECTION, key=KEY):
    """The value of key in section, or None. Case-insensitive like GetPrivateProfileString."""
    current = None
    for line in _lines(text):
        if line.strip().startswith('['):
            current = _section_of(line)
        elif current == section.lower() and _key_of(line) == key.lower():
            return line.partition('=')[2].strip()
    return None


def is_on(value):
    match = re.match(r'\d+', value or '')
    return bool(match and int(match.group(0)))


def set_setting(text, value, section=SECTION, key=KEY):
    """Text with key=value in section; every other line stays byte for byte."""
    lines = _lines(text)
    eol = '\n' if '\n' in text and '\r\n' not in text else '\r\n'
    current, header = None, None
    for i, line in enumerate(lines):
        if line.strip().startswith('['):
            current = _section_of(line)
            if current == section.lower() and header is None:
                header = i
        elif current == section.lower() and _key_of(line) == key.lower():
            ending = line[len(line.rstrip('\r\n')):]
            lines[i] = '%s=%s%s' % (line.partition('=')[0].rstrip(), value, ending)
            return ''.join(lines)
    if header is not None:
        if not lines[header].endswith(('\n', '\r')):
            lines[header] += eol
        lines.insert(header + 1, '%s=%s%s' % (key, value, eol))
        return ''.join(lines)
    if text and not text.endswith(('\n', '\r')):
        text += eol
    return text + '[%s]%s%s=%s%s' % (section, eol, key, value, eol)


def for_live(live):
    """Warnings with the live capture that belongs to this game connection."""
    capture = getattr(live, 'warning_capture', None)
    if capture is None:
        capture = WarningCapture(live)
        live.warning_capture = capture
    return Warnings.from_game(live.game, capture)


class Warnings:
    def __init__(self, game_root, mo2_root, profile, state_dir, capture=None):
        self.capture = capture
        self.game_root = Path(game_root)
        self.mo2_root = Path(mo2_root) if mo2_root else None
        self.profile = profile
        self.state = Path(state_dir)

    @classmethod
    def from_game(cls, game, capture=None):
        return cls(game.game_root, game.mo2_root, game.profile, game.state, capture)

    # ------------------------------------------------------------ the config file
    def enabled_mods(self):
        """Enabled MO2 mods of the profile, highest priority first (the top line of modlist.txt wins)."""
        path = self.mo2_root / 'profiles' / self.profile / 'modlist.txt' if self.mo2_root and self.profile else None
        if not path or not path.exists():
            return []
        return [line[1:].strip() for line in path.read_text(encoding='utf-8', errors='replace').splitlines()
                if line.startswith('+')]

    def real_config(self):
        return self.game_root / 'Data' / 'NVSE' / CONFIG_NAME

    def overwrite_config(self):
        return self.mo2_root / 'overwrite' / 'NVSE' / CONFIG_NAME if self.mo2_root else self.real_config()

    def config_candidates(self):
        """Where the game's virtual Data\\NVSE\\nvse_config.ini can come from, the winner first."""
        found = []
        if self.mo2_root:
            found = [self.overwrite_config()] + [self.mo2_root / 'mods' / mod / 'NVSE' / CONFIG_NAME
                                                 for mod in self.enabled_mods()]
        return found + [self.real_config()]

    def effective_config(self):
        for path in self.config_candidates():
            if path.exists():
                return path
        return None

    @staticmethod
    def config_text(path):
        return path.read_bytes().decode('latin-1') if path and path.exists() else ''

    def enable_target(self, effective):
        """Write where the game will read it: the winner when it is an MO2 mod or overwrite; else (the real game
        folder, or no file at all) a file in MO2's overwrite folder, so the real game folder stays as it is."""
        if effective and effective != self.real_config():
            return effective
        return self.overwrite_config()

    # ------------------------------------------------------------ files and marks
    def log_path(self, source):
        return self.game_root / SOURCES[source]

    def marks(self):
        return read_json(self.state / 'warnings-marks.json', {})

    def load(self, source):
        """The file's complete lines only (the game may be in the middle of a write)."""
        path = self.log_path(source)
        if not path.exists():
            return {'path': str(path), 'exists': False, 'lines': [], 'end': 0, 'data': b''}
        try:
            data = path.read_bytes()
        except PermissionError:
            # The running game holds the file open with no read sharing (seen live).
            return {'path': str(path), 'exists': True, 'locked': True, 'lines': [], 'end': 0, 'data': b''}
        data = data[:data.rfind(b'\n') + 1]
        lines = [l.rstrip(b'\r').decode('utf-8', errors='replace') for l in data.split(b'\n')[:-1]]
        return {'path': str(path), 'exists': True, 'lines': lines, 'end': len(data), 'data': data}

    @staticmethod
    def tail_hash(data, offset):
        return hashlib.sha1(data[max(0, offset - TAIL):offset]).hexdigest()

    def start_of(self, source, loaded, since, marks):
        """(first line number, note). since: 'mark', 'start' or a line number (the seq of a row)."""
        if since == 'start':
            return 0, None
        if isinstance(since, int):
            return max(0, since), None
        mark = marks.get(source)
        if not mark:
            return 0, None
        ok = loaded['exists'] and loaded['end'] >= mark['offset'] and \
            self.tail_hash(loaded['data'], mark['offset']) == mark['tail']
        if not ok:
            return 0, 'the %s log was rewritten since the mark (new game start): reading from its start' % source
        return mark['lines'], None

    def collect(self, since='mark', sources=None):
        marks = self.marks()
        rows, notes, files = [], [], {}
        for source in sources or SOURCES:
            loaded = self.load(source)
            if loaded.get('locked'):
                live = None
                if self.capture is not None and source == 'engine':
                    live = self.capture.rows(since if isinstance(since, int) else None)
                if live is not None:
                    got, lost = live
                    rows += got
                    files[source] = {'path': loaded['path'], 'exists': True, 'locked': True, 'lines': len(got),
                                     'from': 'live capture', 'lost': lost}
                    continue
                files[source] = {'path': loaded['path'], 'exists': True, 'locked': True, 'lines': 0, 'from': 0}
                notes.append('the %s log is locked by the running game: %s' % (
                    source, 'start fnv_warnings capture to see engine warnings live' if source == 'engine' else
                    'Havok lines can be read after the game quits'))
                continue
            first, note = self.start_of(source, loaded, since, marks)
            if note:
                notes.append(note)
            files[source] = {'path': loaded['path'], 'exists': loaded['exists'], 'lines': len(loaded['lines']),
                             'from': first}
            for seq in range(first, len(loaded['lines'])):
                text = loaded['lines'][seq]
                if text.strip():
                    rows.append({'seq': seq, 'source': source, 'category': category_of(text), 'text': text})
        return rows, files, notes

    # ------------------------------------------------------------ actions
    def status(self):
        config = self.effective_config()
        value = read_setting(self.config_text(config))
        on = is_on(value)
        files = {}
        for source in SOURCES:
            path = self.log_path(source)
            if path.exists():
                st = path.stat()
                files[source] = {'path': str(path), 'size': st.st_size,
                                 'modified': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}
            else:
                files[source] = {'path': str(path), 'exists': False}
        marks = self.marks()
        if on:
            text = 'The warning log is on'
            reason = 'EnableGameErrorLog=%s in %s.' % (value, config)
            if any('size' in f for f in files.values()):
                nxt = 'Call fnv_warnings summary or read.'
            else:
                nxt = 'No log file yet: call fnv_game launch, then fnv_warnings summary.'
        else:
            text = 'The warning log is off'
            reason = ('%s=%s in %s.' % (KEY, value, config)) if value is not None else \
                ('%s has no %s setting.' % (config, KEY) if config else 'No nvse_config.ini exists.')
            nxt = 'Call fnv_warnings enable, then restart the game (fnv_game restart): xNVSE reads the setting at start.'
        return verdict(text, reason, enabled=on, config=str(config) if config else None, files=files,
                       marks={k: v['lines'] for k, v in marks.items()} or None,
                       enableWouldWrite=str(self.enable_target(config)), next=nxt)

    def enable(self):
        effective = self.effective_config()
        target = self.enable_target(effective)
        text = self.config_text(effective)
        if is_on(read_setting(text)):
            out = verdict('The warning log is already on', '%s=1 in %s.' % (KEY, target), restartNeeded=False,
                          config=str(target))
            out['changed'] = False
            return out
        backup = None
        if target.exists():
            backup = self.state / 'warnings-backups' / ('%s-%s' % (time.strftime('%Y%m%d-%H%M%S'), CONFIG_NAME))
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(set_setting(text, 1).encode('latin-1'))
        return verdict('Set %s=1 in %s. It takes effect at the next game start.' % (KEY, target),
                       'xNVSE reads the setting only when the game starts, and opens both log files then.',
                       changed=True, restartNeeded=True, written=str(target),
                       readBefore=str(effective) if effective else None, backup=str(backup) if backup else None,
                       seededFromOtherFile=bool(effective and effective != target),
                       next='Call fnv_game restart (or fnv_game launch when the game is closed), then fnv_warnings read.')

    def read(self, since='mark', category=None, text=None, plugin=None, source=None, limit=100, offset=0, brief=False):
        rows, files, notes = self.collect(since, [source] if source else None)
        total = len(rows)
        by_category = Counter(r['category'] for r in rows if r['source'] == 'engine')
        if category:
            rows = [r for r in rows if r['category'].lower() == category.lower()]
        if text:
            rows = [r for r in rows if text.lower() in r['text'].lower()]
        if plugin:
            rows = [r for r in rows if plugin.lower() in r['text'].lower()]
        for r in rows:
            r['plugins'] = plugins_in(r['text'])
        engine = sum(1 for r in rows if r['source'] == 'engine')
        havok = len(rows) - engine
        when = {'mark': 'since the mark', 'start': 'in the files'}.get(since, 'from line %s' % since)
        head = '%d engine warnings %s' % (engine, when)
        if havok:
            head += ' and %d Havok messages' % havok
        if category or text or plugin:
            head += ' match the filter (of %d lines %s)' % (total, when)
        elif engine and by_category:
            name, count = by_category.most_common(1)[0]
            head += ', %d of them %s' % (count, name)
        return verdict(head, None if rows else self.empty_reason(files),
                       **page(rows, limit, offset, brief=brief, brief_fields=['seq', 'source', 'text']),
                       files=files, notes=notes or None)

    @staticmethod
    def empty_reason(files):
        if not any(f['exists'] for f in files.values()):
            return 'No log file exists. Call fnv_warnings status: the setting may be off or the game has not started.'
        return 'Nothing new. Use since=start to read the whole files.'

    def summary(self, since='mark', top=10):
        rows, files, notes = self.collect(since)
        engine = [r for r in rows if r['source'] == 'engine']
        categories = Counter(r['category'] for r in engine)
        plugins = Counter(p for r in rows for p in plugins_in(r['text']))
        groups = {}
        for r in rows:
            g = groups.setdefault((r['source'], pattern_of(r['text'])), {
                'count': 0, 'source': r['source'], 'example': r['text'], 'first': r['seq']})
            g['count'] += 1
            g['last'] = r['seq']
        common = sorted(groups.values(), key=lambda g: -g['count'])[:max(1, min(int(top), 100))]
        head = '%d engine warnings and %d Havok messages' % (len(engine), len(rows) - len(engine))
        if categories:
            name, count = categories.most_common(1)[0]
            head += ', most of them %s (%d)' % (name, count)
        return verdict(head, None if rows else self.empty_reason(files), byCategory=dict(categories.most_common()),
                       byPlugin=dict(plugins.most_common(20)), repeated=common, files=files, notes=notes or None)

    def clear(self):
        marks = self.marks()
        skipped = {}
        for source in SOURCES:
            loaded = self.load(source)
            if loaded.get('locked'):
                skipped[source] = 0
                if source == 'engine' and self.capture is not None:
                    h = self.capture.hook()
                    if h:   # the mark of the live capture: read starts after the calls captured so far
                        self.capture.since = int(self.capture.live.u32s([int(h['seqAddress'], 16)])[0] or 0)
                continue
            marks[source] = {'path': loaded['path'], 'offset': loaded['end'], 'lines': len(loaded['lines']),
                             'tail': self.tail_hash(loaded['data'], loaded['end'])}
            skipped[source] = len(loaded['lines'])
        mine = {k: v for k, v in marks.items() if k in SOURCES}
        update_json(self.state / 'warnings-marks.json', lambda stored: stored.update(mine), {}, indent=2)
        return verdict('Mark set after %d engine lines and %d Havok lines' % (skipped['engine'], skipped['havok']),
                       'The files stay as they are (the game holds them open); read now starts at the mark.',
                       skipped=skipped, next='Do the action, then call fnv_warnings read.')

    def capture_run(self, mode):
        if self.capture is None:
            raise ValueError('capture needs the running game and its engine companion')
        if mode == 'start':
            out = self.capture.start()
        elif mode == 'stop':
            out = self.capture.stop()
        elif mode == 'status':
            h = self.capture.hook()
            out = {'capturing': bool(h), 'calls': (h or {}).get('calls')}
        else:
            raise ValueError('mode is start, stop or status')
        return verdict('Live warning capture %s' % ('on' if out.get('capturing') else 'off'),
                       'read and summary use it while the game holds the log files locked.', **out)

    def run(self, action, **a):
        table = {
            'status': self.status, 'enable': self.enable, 'clear': self.clear,
            'read': lambda: self.read(a.get('since', 'mark'), a.get('category'), a.get('text'), a.get('plugin'),
                                      a.get('source'), a.get('limit', 100), a.get('offset', 0), a.get('brief', False)),
            'summary': lambda: self.summary(a.get('since', 'mark'), a.get('top', 10)),
            'capture': lambda: self.capture_run(a.get('mode') or 'start')}
        return dispatch(action, table)
