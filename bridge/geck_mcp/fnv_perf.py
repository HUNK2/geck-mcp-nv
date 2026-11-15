"""Plan 2 W6: where the time and the memory go. fnv_profile, fnv_threads, fnv_memory, fnv_object.

fnv_profile sample: from outside the process, suspend each chosen thread N times a second for T seconds, read EIP and
the EBP chain, resume. Nothing is patched, so it works on any code. The bridge then names each address (function and
decomp source line from the symbol database, or module + offset), and reports self time per function and per line,
inclusive time per function, the top callers of the hottest functions, and a folded-stack file for flame graphs.
`diff` compares two saved profiles (before and after a fix).

fnv_threads: every thread with its start routine (named), its current function, and the known spin locks with their
owner. fnv_memory: the pools (exact numbers), live NiRefObjects, and allocation counts by call site (a counting hook
on the allocator). fnv_object: what a pointer is (pool or heap block, class from RTTI, the form), and every live
object of a class (a scan of the pools for its vtable).
"""
import ctypes
import os
import json
import struct
import time
from collections import Counter, defaultdict
from ctypes import wintypes
from pathlib import Path

from . import fnv_procmem
from .fnv_conv import ConvError, verdict
from .fnv_link import hexs, word
from .safe_paths import plain_name

kernel32 = fnv_procmem.kernel32
ntdll = ctypes.WinDLL('ntdll') if fnv_procmem.kernel32 is not None else None     # the server imports off Windows too (N22)
if ntdll is not None:
    ntdll.NtQueryInformationThread.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                               ctypes.POINTER(wintypes.ULONG)]
THREAD_QUERY = 0x0040
THREAD_SAMPLE = 0x0002 | 0x0008 | 0x0040        # suspend/resume, get context, query
MAX_FRAMES = 32
MEMORY_MANAGER, POOL_BY_ADDRESS = 0x011F6238, 0x011F63B8
NIREF_COUNT = 0x011F4400
HEAP_ALLOC = 0x00AA3E40
LOCKS = {'process lists': 0x011F11A0, 'garbage collector': 0x011DE8E0, 'global': 0x011F4480,
         'script delayed actions': 0x011CACF8}


# ====================================================================== sampling
def sample_threads(proc, tids, rate, seconds, sleep=time.sleep, clock=time.perf_counter):
    """{tid: [[eip, ret1, ret2, ...], ...]}: the EBP chain of each thread, rate times a second for seconds."""
    handles = {}
    for tid in tids:
        h = kernel32.OpenThread(THREAD_SAMPLE, False, tid)
        if h:
            handles[tid] = h
    out = defaultdict(list)
    buffer = ctypes.create_string_buffer(fnv_procmem.WOW64_CONTEXT_SIZE)
    period = 1.0 / max(1, rate)
    end = clock() + seconds
    try:
        while clock() < end:
            started = clock()
            for tid, h in handles.items():
                if kernel32.Wow64SuspendThread(h) == 0xFFFFFFFF:
                    continue
                try:
                    struct.pack_into('<I', buffer, 0, fnv_procmem.WOW64_CONTEXT_FULL)
                    if not kernel32.Wow64GetThreadContext(h, buffer):
                        continue
                    eip, = struct.unpack_from('<I', buffer.raw, 0xB8)
                    ebp, = struct.unpack_from('<I', buffer.raw, 0xB4)
                    esp, = struct.unpack_from('<I', buffer.raw, 0xC4)
                    chain = [eip]
                    seen = set()
                    while ebp and esp <= ebp < esp + 0x100000 and ebp not in seen and len(chain) < MAX_FRAMES:
                        seen.add(ebp)
                        raw = proc.read(ebp, 8)
                        if not raw:
                            break
                        nxt, ret = struct.unpack('<II', raw)
                        if ret < 0x00400000:
                            break
                        chain.append(ret)
                        if nxt <= ebp:
                            break
                        ebp = nxt
                    out[tid].append(chain)
                finally:
                    kernel32.ResumeThread(h)
            left = period - (clock() - started)
            if left > 0:
                sleep(left)
    finally:
        for h in handles.values():
            kernel32.CloseHandle(h)
    return out


def thread_start(tid):
    """The thread's start address (Win32 start routine), or None."""
    h = kernel32.OpenThread(THREAD_QUERY, False, tid)
    if not h:
        return None
    try:
        value = ctypes.c_uint64(0)
        if ntdll.NtQueryInformationThread(h, 9, ctypes.byref(value), 8, None) == 0:   # ThreadQuerySetWin32StartAddress
            return value.value & 0xFFFFFFFF
        small = ctypes.c_uint32(0)
        if ntdll.NtQueryInformationThread(h, 9, ctypes.byref(small), 4, None) == 0:
            return small.value
        return None
    finally:
        kernel32.CloseHandle(h)


class Namer:
    """Address to {function, source line} through the engine map and the symbol database, cached."""

    def __init__(self, engine, modules):
        self.engine = engine
        self.mods = [{'name': m['name'], 'base': m['base'], 'size': m['size'], 'path': m.get('path')}
                     for m in modules]
        self.cache = {}

    def __call__(self, address, return_address=False):
        key = (address, return_address)
        hit = self.cache.get(key)
        if hit is None:
            sym = self.engine.symbolize(address, self.mods, return_address=return_address)
            if sym.get('kind') == 'function':
                hit = {'function': sym.get('name'), 'start': sym.get('functionAddress'),
                       'line': sym.get('source')}
            elif sym.get('kind') == 'module':
                hit = {'function': '%s!%s' % (sym['module'], sym.get('offset')), 'start': None, 'line': None,
                       'module': sym['module']}
            else:
                hit = {'function': '0x%08X' % address, 'start': None, 'line': None}
            self.cache[key] = hit
        return hit


class ProfileTools:
    def __init__(self, live, sleep=time.sleep):
        self.live = live
        self.sleep = sleep

    @property
    def folder(self):
        return Path(self.live.project.state_dir) / 'fnv' / 'profiles'

    def sample(self, seconds=5.0, rate=200, threads='main', name=None, top=25):
        if name is not None:
            plain_name(name)        # the saved files are <name>-<stamp>.json/.folded in the profiles folder (H5)
        found = self.live.game.process()
        if not found:
            raise ConvError('NOT_RUNNING', 'Profiling needs the game running.')
        pid = found[0]
        ping = {}
        try:
            ping = self.live.game.ping(timeout=2.0)
        except Exception:  # noqa: BLE001 - the main thread may be busy: the process is read from outside
            pass
        with fnv_procmem.GameProcess(pid) as proc:
            modules = proc.modules()
            all_tids = proc.threads()
            main = ping.get('mainThreadId') or proc.main_thread(all_tids)
            tids = [main] if threads == 'main' else all_tids
            frame0 = ping.get('frame')
            started = time.time()
            stacks = sample_threads(proc, tids, int(rate), float(seconds), sleep=self.sleep)
            took = time.time() - started
        frames_run = None
        try:
            frames_run = int(self.live.game.ping(timeout=2.0).get('frame')) - int(frame0) if frame0 else None
        except Exception:  # noqa: BLE001
            pass
        namer = Namer(self.live.engine(), modules)
        report = self._report(stacks, namer, main, int(top))
        stamp = time.strftime('%Y%m%d-%H%M%S')
        self.folder.mkdir(parents=True, exist_ok=True)
        base = self.folder / ('%s-%s' % (name or 'profile', stamp))
        folded = base.parent / (base.name + '.folded')     # with_suffix would cut a name such as my.run
        with folded.open('w', encoding='utf-8') as f:
            for line, count in report.pop('_folded').items():
                f.write('%s %d\n' % (line, count))
        report.update(seconds=round(took, 2), rate=int(rate), framesRun=frames_run,
                      fps=round(frames_run / took, 1) if frames_run and took else None, folded=str(folded))
        (base.parent / (base.name + '.json')).write_text(json.dumps(report, indent=1), encoding='utf-8')
        top_main = (report['threads'][0]['gameFunctions'] or [{}])[0] if report['threads'] else {}
        return verdict('%d samples over %.1f s; on the main thread the game was mostly in %s (%s%%)' % (
            report['samples'], took, top_main.get('function'), top_main.get('percent')),
            'gameFunctions: the innermost game function of each sample (a wait in a system DLL counts for the game '
            'function that called it); selfFunctions: where the thread was; inclusive: every function on the stack. '
            'Saved as %s '
            '(diff two profiles with fnv_profile diff).' % base.with_suffix('.json').name,
            profile=str(base.with_suffix('.json')), **report)

    def _report(self, stacks, namer, main, top):
        threads, folded, total = [], Counter(), 0
        for tid, samples in sorted(stacks.items(), key=lambda kv: (kv[0] != main, -len(kv[1]))):
            n = len(samples)
            total += n
            if not n:
                continue
            self_fn, self_line, incl_fn, callers = Counter(), Counter(), Counter(), defaultdict(Counter)
            game_fn, game_line = Counter(), Counter()
            for chain in samples:
                names = [namer(a, return_address=i > 0) for i, a in enumerate(chain)]
                leaf = names[0]
                # The innermost frame in the game's own code: where a wait inside a system DLL comes from.
                game = next((nm for nm in names if nm.get('start')), None)
                if game:
                    game_fn[game['function']] += 1
                    if game['line']:
                        game_line[(game['function'], game['line'])] += 1
                self_fn[leaf['function']] += 1
                if leaf['line']:
                    self_line[(leaf['function'], leaf['line'])] += 1
                seen = set()
                for nm in names:
                    if nm['function'] not in seen:
                        incl_fn[nm['function']] += 1
                        seen.add(nm['function'])
                if len(names) > 1:
                    callers[leaf['function']][names[1]['function']] += 1
                folded[';'.join(['thread_%d' % tid] + [nm['function'] for nm in reversed(names)])] += 1

            def pct(c):
                return round(100.0 * c / n, 1)
            threads.append({
                'threadId': tid, 'main': tid == main, 'samples': n,
                'selfFunctions': [{'function': f, 'samples': c, 'percent': pct(c),
                                   'calledFrom': [{'function': k, 'samples': v}
                                                  for k, v in callers[f].most_common(3)]}
                                  for f, c in self_fn.most_common(top)],
                'selfLines': [{'function': f, 'line': l, 'samples': c, 'percent': pct(c)}
                              for (f, l), c in self_line.most_common(top)],
                'inclusive': [{'function': f, 'samples': c, 'percent': pct(c)} for f, c in incl_fn.most_common(top)],
                'gameFunctions': [{'function': f, 'samples': c, 'percent': pct(c)} for f, c in game_fn.most_common(top)],
                'gameLines': [{'function': f, 'line': l, 'samples': c, 'percent': pct(c)}
                              for (f, l), c in game_line.most_common(top)]})
        if len(threads) > 12:
            threads = threads[:12]
        return {'samples': total, 'threads': threads, '_folded': folded}

    def diff(self, before, after, top=20):
        a = self._load(before)
        b = self._load(after)

        def table(report):
            # The innermost game function of each sample: stable between runs (an inclusive table also counts the
            # bottom of the stack, which a frame chain reaches in some samples and not others).
            t = (report['threads'] or [{}])[0]
            rows = t.get('gameFunctions') or t.get('inclusive', [])
            return {r['function']: r['percent'] for r in rows}, t.get('samples', 0)
        ta, na = table(a)
        tb, nb = table(b)
        rows = []
        for f in set(ta) | set(tb):
            rows.append({'function': f, 'before': ta.get(f, 0.0), 'after': tb.get(f, 0.0),
                         'change': round(tb.get(f, 0.0) - ta.get(f, 0.0), 1)})
        rows.sort(key=lambda r: -abs(r['change']))
        fps = {'before': a.get('fps'), 'after': b.get('fps')}
        return verdict('Frame rate %s -> %s; the biggest change: %s (%+.1f points)' % (
            fps['before'], fps['after'], rows[0]['function'] if rows else None, rows[0]['change'] if rows else 0),
            'Percent of the main thread samples per innermost game function, before and after.', fps=fps,
            samples={'before': na, 'after': nb}, changes=rows[:int(top)])

    def _load(self, ref):
        p = Path(ref)
        if not p.is_file():
            p = self.folder / ref
            if not p.is_file():
                p = self.folder / (str(ref) + '.json')
        if not p.is_file():
            named = sorted(self.folder.glob('%s-*.json' % ref), key=lambda q: q.stat().st_mtime)
            if named:
                p = named[-1]      # the newest profile saved under that name
        if not p.is_file():
            raise ConvError('NOT_FOUND', 'No saved profile %s (see state/fnv/profiles).' % ref)
        return json.loads(p.read_text(encoding='utf-8'))

    def run(self, action, **a):
        if action == 'sample':
            return self.sample(a.get('seconds') or 5.0, a.get('rate') or 200, a.get('threads') or 'main',
                               a.get('name'), a.get('top') or 25)
        if action == 'diff':
            if not a.get('before') or not a.get('after'):
                raise ConvError('BAD_ARGUMENTS', 'diff needs before and after (saved profile names or paths).')
            return self.diff(a['before'], a['after'])
        raise ConvError('UNKNOWN_ACTION', 'Unknown action %r. Valid: sample, diff.' % action)


# ====================================================================== threads
class ThreadTools:
    def __init__(self, live):
        self.live = live

    def list(self):
        found = self.live.game.process()
        if not found:
            raise ConvError('NOT_RUNNING', 'Needs the game running.')
        ping = {}
        try:
            ping = self.live.game.ping(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        with fnv_procmem.GameProcess(found[0]) as proc:
            modules = proc.modules()
            tids = proc.threads()
            main = ping.get('mainThreadId') or proc.main_thread(tids)
            stacks = sample_threads(proc, tids, 1, 0.001)
            locks = self._locks(proc)
        namer = Namer(self.live.engine(), modules)
        rows = []
        for tid in tids:
            start = thread_start(tid)
            chain = (stacks.get(tid) or [[]])[0]
            now = [namer(a, return_address=i > 0)['function'] for i, a in enumerate(chain[:6])]
            game_fn = next((f for f in now if f and '!' not in f and not f.startswith('0x')), None)
            rows.append({'threadId': tid, 'main': tid == main,
                         'start': namer(start)['function'] if start else None,
                         'startAddress': '0x%08X' % start if start else None, 'now': now[0] if now else None,
                         'gameFunction': game_fn, 'stack': now})
        rows.sort(key=lambda r: (not r['main'], r['start'] or ''))
        return verdict('%d threads; main %s' % (len(rows), main), 'start is the routine the thread began in; '
                       'now and stack come from one sample taken from outside the process.', threads=rows,
                       locks=locks)

    @staticmethod
    def _locks(proc):
        out = []
        for name, address in LOCKS.items():
            raw = proc.read(address, 8)
            if raw:
                owner, count = struct.unpack('<II', raw)
                out.append({'lock': name, 'address': '0x%08X' % address, 'ownerThread': owner or None,
                            'count': count})
        return out


# ====================================================================== memory and objects
def block_start(pointer, block):
    """Start of the pool block holding pointer. Blocks do not cross 4 KB pages: each page holds whole blocks from
    its start (seen live: Character blocks of 0x1E0 at page + k * 0x1E0)."""
    if block >= 0x1000:
        region = pointer & 0xFF000000
        return region + ((pointer - region) // block) * block
    page = pointer & ~0xFFF
    index = (pointer - page) // block
    if (index + 1) * block > 0x1000:
        return None          # the slack after the page's last whole block: not inside any block
    return page + index * block


def block_offsets(chunk, block, length):
    """Offsets of block starts inside a chunk read at a page-aligned address."""
    if block >= 0x1000:
        region = chunk & 0xFF000000
        first = (-(chunk - region)) % block
        return range(first, length - 3, block)
    out = []
    for page in range(0, length, 0x1000):
        out.extend(range(page, min(page + 0x1000 - block + 1, length - 3), block))
    return out


class MemoryTools:
    def __init__(self, live, deep=None):
        self.live = live
        self.deep = deep

    def pools(self):
        self.live.ready()
        table = self.live.reads([(POOL_BY_ADDRESS, 256 * 4)])[0]
        pools = sorted({p for p in struct.unpack('<256I', table) if p}) if table else []
        rows = []
        for p in pools:
            raw = self.live.reads([(p, 0x60)])[0]
            if not raw:
                continue
            block = struct.unpack_from('<I', raw, 0x40)[0]
            pages = struct.unpack_from('<I', raw, 0x58)[0]
            peak = struct.unpack_from('<I', raw, 0x44)[0]
            rows.append({'pool': '0x%08X' % p, 'blockSize': block, 'committedBytes': pages << 12,
                         'peakPages': peak})
        rows.sort(key=lambda r: r['blockSize'])
        niref = self.live.u32s([NIREF_COUNT])[0]
        return verdict('%d pools, %.1f MB committed; %s live NiRefObjects' % (
            len(rows), sum(r['committedBytes'] for r in rows) / 1048576.0, niref),
            'Pool table 0x011F63B8 (one pool per 16 MB region); block size +0x40, committed pages +0x58, peak +0x44.',
            pools=rows, niRefObjects=niref)

    def alloc_trace(self, seconds=3.0, top=20):
        """Allocation counts and sizes by call site for a while: a counting hook on the allocator keyed by the
        caller (the companion's call table), removed after."""
        self.live.ready()
        from .fnv_deep import decode_prologue
        raw = self.live.reads([(HEAP_ALLOC, 24)])[0]
        if raw and raw[0] == 0xE9:
            target = HEAP_ALLOC + 5 + struct.unpack_from('<i', raw, 1)[0]
            note = 'the allocator entry jumps to 0x%08X (a heap replacer); counted at the entry' % target
        else:
            note = None
        stolen, relocs = decode_prologue(raw)
        h = self.live.link.one('hook', word(HEAP_ALLOC), word(stolen), ','.join('%x' % r for r in relocs) or '-',
                               word(1 | 4), '0', '0', word(10), '1', '0', '0', '0', '0', hexs('alloc_trace'))
        if not h.get('ok'):
            raise ConvError('HOOK_REFUSED', 'The allocator hook was refused (%s).' % h.get('error'), h)
        hook = h['hook']
        try:
            time.sleep(float(seconds))
            rows = self.deep.table(hook) if self.deep else []
        finally:
            self.live.link.one('unhook', word(hook['id']))
        engine = self.live.engine()
        out = []
        for r in sorted(rows, key=lambda r: -r['count'])[:int(top)]:
            site = r['key']
            sym = engine.symbolize(site, None, return_address=True)
            out.append({'caller': '0x%08X' % site, 'function': sym.get('name'), 'source': sym.get('source'),
                        'calls': r['count'], 'perSecond': round(r['count'] / float(seconds), 1)})
        total = sum(r['count'] for r in rows)
        return verdict('%d allocations in %.1f s from %d call sites' % (total, float(seconds), len(rows)),
                       'Counted by the caller of MemoryManager::Allocate 0x00AA3E40 (a counting hook, removed).',
                       sites=out, total=total, note=note)

    def refcount(self, pointer, seconds=5.0):
        """Who changes an NiRefObject's reference count (+4): a write watchpoint through fnv_blame field."""
        self.live.ready()
        blame = getattr(self, 'blame', None)
        if blame is None:
            raise ConvError('NOT_AVAILABLE', 'refcount needs the blame tools.')
        p = int(str(pointer), 16) if isinstance(pointer, str) else int(pointer)
        count = self.live.u32s([p + 4])[0]
        out = blame.field('0x%08X' % (p + 4), length=4, access='write', scope='all', seconds=seconds)
        out['refcountBefore'] = count
        out['refcountAfter'] = self.live.u32s([p + 4])[0]
        out['answer'] = 'Reference count of 0x%08X: %s -> %s; %s' % (p, count, out['refcountAfter'],
                                                                     out.get('answer', ''))
        return out

    def run(self, action, **a):
        if action == 'pools':
            return self.pools()
        if action == 'alloc_trace':
            return self.alloc_trace(a.get('seconds') or 3.0, a.get('top') or 20)
        if action == 'refcount':
            if not a.get('pointer'):
                raise ConvError('BAD_ARGUMENTS', 'refcount needs pointer (an NiRefObject).')
            return self.refcount(a['pointer'], a.get('seconds') or 5.0)
        raise ConvError('UNKNOWN_ACTION', 'Unknown action %r. Valid: pools, alloc_trace, refcount.' % action)


class ObjectTools:
    def __init__(self, live):
        self.live = live

    def identify(self, pointer):
        self.live.ready()
        p = int(str(pointer), 16) if isinstance(pointer, str) else int(pointer)
        table = self.live.reads([(POOL_BY_ADDRESS + 4 * (p >> 24), 4)])[0]
        pool = struct.unpack('<I', table)[0] if table else 0
        where = {'pointer': '0x%08X' % p}
        if pool:
            block = self.live.u32s([pool + 0x40])[0] or 0
            start = block_start(p, block) if block else None
            if start is None and block:
                where.update(heap='pool', pool='0x%08X' % pool, blockSize=block,
                             note='in the unused tail of a pool page: not inside a block')
            elif block:
                where.update(heap='pool', pool='0x%08X' % pool, blockSize=block, blockStart='0x%08X' % start,
                             offsetInBlock=p - start)
        else:
            header = self.live.u32s([p - 4])[0]
            where.update(heap='general heap (or not heap memory)',
                         headerSize=(header & 0x0FFFFFFF) if header is not None else None)
        base = int(where.get('blockStart', '0x%08X' % p), 16)
        info = self.live.rtti([base]).get(base) or {}
        name = info.get('class')
        where['class'] = name
        if name and name not in ('?',):
            try:
                form = self.live.form_ids([base])[0]
                if form.get('formId'):
                    where['form'] = form
            except Exception:  # noqa: BLE001 - not a form
                pass
        readable = self.live.reads([(p, 4)])[0] is not None
        return verdict('0x%08X: %s%s' % (p, where.get('class') or 'no RTTI class', ' in a %d-byte pool block' %
                                         where['blockSize'] if where.get('blockSize') else ''),
                       None if readable else 'The address is not readable.', readable=readable, **where)

    def find(self, class_name, limit=200):
        """Live objects of a class: the pools are scanned for its vtable at block starts."""
        self.live.ready()
        vtable = self._vtable(class_name)
        found = self.live.game.process()
        table = self.live.reads([(POOL_BY_ADDRESS, 1024)])[0]
        regions = [(i << 24, p) for i, p in enumerate(struct.unpack('<256I', table)) if p]
        hits = []
        with fnv_procmem.GameProcess(found[0]) as proc:
            for region, pool in regions:
                block = proc.u32(pool + 0x40) or 0
                if block < 4:
                    continue
                for chunk in range(region, region + 0x01000000, 0x10000):
                    data = proc.read(chunk, 0x10000)
                    if not data:
                        continue
                    for off in block_offsets(chunk, block, len(data)):
                        if struct.unpack_from('<I', data, off)[0] == vtable:
                            hits.append(chunk + off)
                            if len(hits) >= limit:
                                break
                    if len(hits) >= limit:
                        break
                if len(hits) >= limit:
                    break
        return verdict('%d live %s objects in the pools%s' % (len(hits), class_name,
                                                             ' (stopped at the limit)' if len(hits) >= limit else ''),
                       'Pool blocks whose first word is the vtable 0x%08X; objects in the general heap are not '
                       'scanned.' % vtable, vtable='0x%08X' % vtable, objects=['0x%08X' % h for h in hits])

    def referrers(self, pointer, limit=40, identify=12):
        """Who holds this address: every aligned word equal to it in the writable memory of the process, read from
        outside (ReadProcessMemory, region by region); the first hits are identified (pool block, class, form)."""
        self.live.ready()
        p = int(str(pointer), 16) if isinstance(pointer, str) else int(pointer)
        needle = struct.pack('<I', p)
        found = self.live.game.process()
        hits, scanned, started = [], 0, time.time()
        with fnv_procmem.GameProcess(found[0]) as proc:
            for base, size, _ in proc.regions():
                for start in range(base, base + size, 0x100000):
                    data = proc.read(start, min(0x100000, base + size - start))
                    if not data:
                        continue
                    scanned += len(data)
                    at = data.find(needle)
                    while at >= 0:
                        if at % 4 == 0:
                            hits.append(start + at)
                            if len(hits) >= limit:
                                break
                        at = data.find(needle, at + 1)
                    if len(hits) >= limit:
                        break
                if len(hits) >= limit:
                    break
        rows = []
        for h in hits:
            row = {'at': '0x%08X' % h}
            if len(rows) < identify:
                try:
                    who = self.identify(h)
                    row.update({k: who.get(k) for k in ('heap', 'blockStart', 'offsetInBlock', 'class', 'form')
                                if who.get(k) is not None})
                except ConvError:
                    pass
            rows.append(row)
        return verdict('%d places hold 0x%08X%s' % (len(hits), p, ' (stopped at the limit)' if len(hits) >= limit
                                                    else ''),
                       'Aligned words equal to the address in every committed writable region (%.0f MB read in '
                       '%.1f s, from outside the process); the game keeps running, so values can change meanwhile. '
                       'The first %d hits are identified like fnv_object identify.' % (
                           scanned / 1048576.0, time.time() - started, identify), pointer='0x%08X' % p,
                       referrers=rows)

    def _vtable(self, class_name):
        """The class's complete-object vtable (offset 0) from the engine map's RTTI."""
        engine = self.live.engine()
        key = engine.rtti_lower.get(str(class_name).lower(), class_name)
        info = engine.rtti.get(key)
        if not info:
            raise ConvError('NO_VTABLE', 'No class %r in the RTTI of the engine map.' % class_name)
        tables = info.get('vtables', [])
        main = next((vt for vt in tables if int(vt.get('thisOffset', 0) or 0) == 0), tables[0] if tables else None)
        if not main:
            raise ConvError('NO_VTABLE', '%s has no vtable in the engine map.' % key)
        return int(main['address'], 16)

    def run(self, action, **a):
        if action == 'identify':
            if a.get('pointer') is None:
                raise ConvError('BAD_ARGUMENTS', 'identify needs pointer.')
            return self.identify(a['pointer'])
        if action == 'find':
            if not a.get('class_name'):
                raise ConvError('BAD_ARGUMENTS', 'find needs class_name.')
            return self.find(a['class_name'], a.get('limit') or 200)
        if action == 'referrers':
            if not a.get('pointer'):
                raise ConvError('BAD_ARGUMENTS', 'referrers needs pointer.')
            return self.referrers(a['pointer'], min(int(a.get('limit') or 40), 500))
        raise ConvError('UNKNOWN_ACTION', 'Unknown action %r. Valid: identify, find, referrers.' % action)
