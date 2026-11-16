"""Read a running 32-bit FalloutNV.exe from outside (64-bit Python, WOW64): memory, modules, threads.

Used by the tools that must work while the game is stuck or without the engine companion:
fnv_threads action=hang (thread stacks) and fnv_patch_map (code bytes). Nothing here writes to the game;
threads are suspended only for the moment their context is read.
"""
import ctypes
import os
import struct
from ctypes import wintypes

try:
    import msvcrt
except ImportError:     # macOS/Linux
    msvcrt = None
if msvcrt is not None:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    dbghelp = ctypes.WinDLL('dbghelp', use_last_error=True)
else:   # macOS/Linux (the GECK tools under Wine): the fnv tools need Windows, but the server must still import (N22)
    kernel32 = psapi = dbghelp = None

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
THREAD_SUSPEND_RESUME = 0x0002
THREAD_GET_CONTEXT = 0x0008
THREAD_SET_CONTEXT = 0x0010
THREAD_QUERY_INFORMATION = 0x0040
THREAD_QUERY_LIMITED_INFORMATION = 0x0800
TH32CS_SNAPTHREAD = 0x4
LIST_MODULES_32BIT = 0x01
WOW64_CONTEXT_FULL = 0x10000 | 0x1 | 0x2  # control + integer
WOW64_CONTEXT_DEBUGGER = 0x10000 | 0x1 | 0x2 | 0x4 | 0x10  # control, integer, segments, debug registers
WOW64_CONTEXT_SIZE = 0x2CC
WOW64_THREAD_FAILED = 0xFFFFFFFF
# Byte offsets of the registers in a WOW64_CONTEXT (winnt.h): debug registers from 4, integer registers from 0x9C.
CONTEXT_OFFSETS = {'dr0': 0x04, 'dr1': 0x08, 'dr2': 0x0C, 'dr3': 0x10, 'dr6': 0x14, 'dr7': 0x18,
                   'edi': 0x9C, 'esi': 0xA0, 'ebx': 0xA4, 'edx': 0xA8, 'ecx': 0xAC, 'eax': 0xB0, 'ebp': 0xB4,
                   'eip': 0xB8, 'eflags': 0xC0, 'esp': 0xC4}
# MiniDumpWithDataSegs | MiniDumpWithIndirectlyReferencedMemory | MiniDumpWithThreadInfo: no full heap copy.
MINIDUMP_DEFAULT = 0x4 | 0x40 | 0x1000

if kernel32 is not None:
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                           ctypes.POINTER(ctypes.c_size_t)]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Wow64SuspendThread.argtypes = [wintypes.HANDLE]
    kernel32.Wow64SuspendThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.Wow64GetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Wow64SetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Wow64SetThreadContext.restype = wintypes.BOOL
    dbghelp.MiniDumpWriteDump.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    dbghelp.MiniDumpWriteDump.restype = wintypes.BOOL
    psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]


class MODULEINFO(ctypes.Structure):
    _fields_ = [('lpBaseOfDll', ctypes.c_void_p), ('SizeOfImage', wintypes.DWORD), ('EntryPoint', ctypes.c_void_p)]


if psapi is not None:
    psapi.GetModuleInformation.argtypes = [wintypes.HANDLE, wintypes.HMODULE, ctypes.POINTER(MODULEINFO), wintypes.DWORD]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ThreadID', wintypes.DWORD),
                ('th32OwnerProcessID', wintypes.DWORD), ('tpBasePri', wintypes.LONG), ('tpDeltaPri', wintypes.LONG),
                ('dwFlags', wintypes.DWORD)]


if kernel32 is not None:
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                ('AllocationProtect', wintypes.DWORD), ('PartitionId', wintypes.WORD),
                ('RegionSize', ctypes.c_size_t), ('State', wintypes.DWORD), ('Protect', wintypes.DWORD),
                ('Type', wintypes.DWORD)]


MEM_COMMIT, PAGE_GUARD = 0x1000, 0x100
WRITABLE_PROTECT = (0x04, 0x08, 0x40, 0x80)            # RW, WRITECOPY, EXECUTE_RW, EXECUTE_WRITECOPY
READABLE_PROTECT = WRITABLE_PROTECT + (0x02, 0x20)      # + READONLY, EXECUTE_READ
if kernel32 is not None:
    kernel32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t


class ProcMemError(Exception):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


_MODULES = {}


def game_modules(pid, max_age=30.0):
    """The game's 32-bit modules, cached per pid for max_age seconds (plugins do not load mid-game)."""
    import time
    now = time.monotonic()
    hit = _MODULES.get(pid)
    if hit and now - hit[0] < max_age:
        return hit[1]
    with GameProcess(pid) as proc:
        mods = proc.modules()
    _MODULES.clear()
    _MODULES[pid] = (now, mods)
    return mods


class GameProcess:
    """A read-only handle on the game process."""

    def __init__(self, pid):
        self.pid = pid
        self.handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.handle:
            raise ProcMemError('PROCESS_UNREADABLE', 'Cannot open FalloutNV.exe (pid %d) for reading: error %d'
                               % (pid, ctypes.get_last_error()))

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read(self, address, length):
        """Bytes at address, or None when any of the range is unreadable."""
        buffer = ctypes.create_string_buffer(length)
        done = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(self.handle, ctypes.c_void_p(address), buffer, length, ctypes.byref(done)) \
                or done.value != length:
            return None
        return buffer.raw

    def read_span(self, address, length, chunk=0x10000):
        """Bytes of a long range read page-chunk by chunk; unreadable chunks come back as None entries."""
        out = []
        for start in range(address, address + length, chunk):
            out.append((start, self.read(start, min(chunk, address + length - start))))
        return out

    def u32(self, address):
        data = self.read(address, 4)
        return struct.unpack('<I', data)[0] if data else None

    def regions(self, writable_only=True):
        """Committed regions below 4 GB as (base, size, protect), from VirtualQueryEx."""
        info = MEMORY_BASIC_INFORMATION()
        out, address = [], 0x10000
        while address < 0xFFFF0000:
            if not kernel32.VirtualQueryEx(self.handle, ctypes.c_void_p(address), ctypes.byref(info),
                                           ctypes.sizeof(info)):
                break
            base, size = info.BaseAddress or 0, info.RegionSize
            if size <= 0:
                break
            protect = info.Protect & 0xFF
            if info.State == MEM_COMMIT and not info.Protect & PAGE_GUARD and protect in (
                    WRITABLE_PROTECT if writable_only else READABLE_PROTECT):
                out.append((base, size, info.Protect))
            address = base + size
        return out

    def modules(self):
        """32-bit modules: [{'name', 'path', 'base', 'size'}] sorted by base."""
        needed = wintypes.DWORD()
        array = (wintypes.HMODULE * 1024)()
        if not psapi.EnumProcessModulesEx(self.handle, array, ctypes.sizeof(array), ctypes.byref(needed), LIST_MODULES_32BIT):
            raise ProcMemError('MODULES_UNREADABLE', 'EnumProcessModulesEx failed: error %d' % ctypes.get_last_error())
        out = []
        for i in range(min(needed.value // ctypes.sizeof(wintypes.HMODULE), 1024)):
            module = array[i]
            path = ctypes.create_unicode_buffer(520)
            psapi.GetModuleFileNameExW(self.handle, module, path, 520)
            info = MODULEINFO()
            if not psapi.GetModuleInformation(self.handle, module, ctypes.byref(info), ctypes.sizeof(info)):
                continue
            out.append({'name': os.path.basename(path.value), 'path': path.value, 'base': info.lpBaseOfDll or 0,
                        'size': info.SizeOfImage})
        return sorted(out, key=lambda m: m['base'])

    def threads(self):
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == wintypes.HANDLE(-1).value:
            raise ProcMemError('THREADS_UNREADABLE', 'CreateToolhelp32Snapshot failed')
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        out = []
        try:
            ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while ok:
                if entry.th32OwnerProcessID == self.pid:
                    out.append(entry.th32ThreadID)
                ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return out

    def main_thread(self, threads=None):
        """The process's first thread (earliest creation time): its main thread."""
        best = None
        for tid in threads or self.threads():
            handle = kernel32.OpenThread(THREAD_QUERY_INFORMATION, False, tid)
            if not handle:
                continue
            times = [wintypes.FILETIME() for _ in range(4)]
            try:
                if kernel32.GetThreadTimes(handle, *[ctypes.byref(t) for t in times]):
                    created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                    if best is None or created < best[0]:
                        best = (created, tid)
            finally:
                kernel32.CloseHandle(handle)
        return best[1] if best else None

    def thread_cpu_ms(self, tid):
        """Kernel + user CPU time a thread has used, in ms (GetThreadTimes), or None."""
        thread = kernel32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, tid)
        if not thread:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetThreadTimes(thread, *[ctypes.byref(t) for t in times]):
                return None
            total = 0
            for t in times[2:]:
                total += (t.dwHighDateTime << 32) | t.dwLowDateTime
            return total / 10000.0
        finally:
            kernel32.CloseHandle(thread)

    def thread_context(self, tid):
        """{'eip','esp','ebp',...} of a thread, suspended only while the context is read; None if unavailable."""
        thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, False, tid)
        if not thread:
            return None
        try:
            if kernel32.Wow64SuspendThread(thread) == 0xFFFFFFFF:
                return None
            try:
                buffer = ctypes.create_string_buffer(WOW64_CONTEXT_SIZE)
                struct.pack_into('<I', buffer, 0, WOW64_CONTEXT_FULL)
                if not kernel32.Wow64GetThreadContext(thread, buffer):
                    return None
                raw = buffer.raw
                names = ('edi', 'esi', 'ebx', 'edx', 'ecx', 'eax', 'ebp', 'eip')
                regs = dict(zip(names, struct.unpack_from('<8I', raw, 0x9C)))
                regs['esp'] = struct.unpack_from('<I', raw, 0xC4)[0]
                # Read the stack while the thread is still suspended so the walk sees one consistent state.
                # A shallow thread's ESP sits near the top of its stack region, where a long read fails: shorten it
                # (seen live 2026-11-11: a polling thread read nothing at 0x100 bytes, 0x40 worked).
                regs['stack'] = None
                for size in (0x4000, 0x1000, 0x400, 0x100, 0x40):
                    regs['stack'] = self.read(regs['esp'], size)
                    if regs['stack']:
                        break
                return regs
            finally:
                kernel32.ResumeThread(thread)
        finally:
            kernel32.CloseHandle(thread)

    # ------------------------------------------------------------------ bridge core (fnv_debug, W2)
    def suspend_all(self, keep=()):
        """Suspend every thread of the process except those in keep. Returns the ids that were suspended; a
        thread that cannot be opened or suspended is left alone (it is not in the result)."""
        done = []
        for tid in self.threads():
            if tid in keep:
                continue
            thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, tid)
            if not thread:
                continue
            try:
                if kernel32.Wow64SuspendThread(thread) != WOW64_THREAD_FAILED:
                    done.append(tid)
            finally:
                kernel32.CloseHandle(thread)
        return done

    def resume_all(self, tids):
        """Undo suspend_all for these ids (one resume each). Returns the ids that were resumed."""
        done = []
        for tid in tids:
            thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, tid)
            if not thread:
                continue
            try:
                if kernel32.ResumeThread(thread) != WOW64_THREAD_FAILED:
                    done.append(tid)
            finally:
                kernel32.CloseHandle(thread)
        return done

    def get_context(self, tid, flags=WOW64_CONTEXT_DEBUGGER):
        """Raw WOW64 CONTEXT (WOW64_CONTEXT_SIZE bytes) of a thread. The thread is suspended only while the context
        is read. Raises ProcMemError when the thread cannot be read."""
        thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, False, tid)
        if not thread:
            raise ProcMemError('THREAD_UNREADABLE', 'Cannot open thread %d: error %d' % (tid, ctypes.get_last_error()))
        try:
            if kernel32.Wow64SuspendThread(thread) == WOW64_THREAD_FAILED:
                raise ProcMemError('THREAD_UNREADABLE', 'Cannot suspend thread %d: error %d' % (tid, ctypes.get_last_error()))
            try:
                buffer = ctypes.create_string_buffer(WOW64_CONTEXT_SIZE)
                struct.pack_into('<I', buffer, 0, flags)
                if not kernel32.Wow64GetThreadContext(thread, buffer):
                    raise ProcMemError('THREAD_UNREADABLE', 'Wow64GetThreadContext failed for thread %d: error %d'
                                       % (tid, ctypes.get_last_error()))
                return buffer.raw
            finally:
                kernel32.ResumeThread(thread)
        finally:
            kernel32.CloseHandle(thread)

    def set_context(self, tid, raw):
        """Write a raw WOW64 CONTEXT to a thread (the flags inside it say which parts apply). The thread is suspended
        only while the context is written."""
        if len(raw) != WOW64_CONTEXT_SIZE:
            raise ProcMemError('CONTEXT_SIZE', 'A WOW64 CONTEXT is %d bytes, got %d' % (WOW64_CONTEXT_SIZE, len(raw)))
        thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME | THREAD_SET_CONTEXT | THREAD_GET_CONTEXT
                                     | THREAD_QUERY_INFORMATION, False, tid)
        if not thread:
            raise ProcMemError('THREAD_UNREADABLE', 'Cannot open thread %d: error %d' % (tid, ctypes.get_last_error()))
        try:
            if kernel32.Wow64SuspendThread(thread) == WOW64_THREAD_FAILED:
                raise ProcMemError('THREAD_UNREADABLE', 'Cannot suspend thread %d: error %d' % (tid, ctypes.get_last_error()))
            try:
                buffer = ctypes.create_string_buffer(bytes(raw), WOW64_CONTEXT_SIZE)
                if not kernel32.Wow64SetThreadContext(thread, buffer):
                    raise ProcMemError('CONTEXT_UNWRITABLE', 'Wow64SetThreadContext failed for thread %d: error %d'
                                       % (tid, ctypes.get_last_error()))
                return True
            finally:
                kernel32.ResumeThread(thread)
        finally:
            kernel32.CloseHandle(thread)

    def minidump(self, path, dump_type=MINIDUMP_DEFAULT):
        """Write a minidump of the process with MiniDumpWriteDump (dbghelp.dll). Returns the file size in bytes."""
        with open(path, 'wb') as f:
            handle = msvcrt.get_osfhandle(f.fileno())
            if not dbghelp.MiniDumpWriteDump(self.handle, self.pid, handle, dump_type, None, None, None):
                raise ProcMemError('DUMP_FAILED', 'MiniDumpWriteDump failed for pid %d: error %d'
                                   % (self.pid, ctypes.get_last_error()))
        return os.path.getsize(path)


def parse_context(raw):
    """Registers of a raw WOW64 CONTEXT: {'eip', 'esp', 'ebp', 'eax', ..., 'eflags', 'dr0'..'dr7'} as ints."""
    if len(raw) < WOW64_CONTEXT_SIZE:
        raise ProcMemError('CONTEXT_SIZE', 'A WOW64 CONTEXT is %d bytes, got %d' % (WOW64_CONTEXT_SIZE, len(raw)))
    return {name: struct.unpack_from('<I', raw, offset)[0] for name, offset in CONTEXT_OFFSETS.items()}


def module_at(modules, address):
    for m in modules:
        if m['base'] <= address < m['base'] + m['size']:
            return m
    return None


def walk_stack(process, regs, modules, max_frames=48):
    """Return addresses of a suspended thread: the EBP chain (MSVC frame pointers), then return-address
    candidates from a scan of the stack top for frames the chain skipped (marked heuristic). Each chain frame
    carries 'ebp', its frame pointer (the top frame's is only valid once its function has set up EBP)."""
    frames = [{'address': regs['eip'], 'how': 'eip', 'ebp': regs['ebp']}]
    stack, esp = regs.get('stack') or b'', regs['esp']

    def stack_u32(address):
        offset = address - esp
        if 0 <= offset <= len(stack) - 4:
            return struct.unpack_from('<I', stack, offset)[0]
        return process.u32(address)

    ebp, seen = regs['ebp'], set()
    while ebp and ebp not in seen and len(frames) < max_frames and esp <= ebp < esp + 0x100000:
        seen.add(ebp)
        ret, nxt = stack_u32(ebp + 4), stack_u32(ebp)
        if ret is None or not module_at(modules, ret):
            break
        frames.append({'address': ret, 'how': 'frame', 'ebp': nxt})   # the caller's own EBP is the saved one
        if nxt is None or nxt <= ebp:
            break
        ebp = nxt
    chained = {f['address'] for f in frames}
    scanned = []
    for offset in range(0, min(len(stack), 0x800) - 3, 4):
        value = struct.unpack_from('<I', stack, offset)[0]
        m = module_at(modules, value)
        if not m or value in chained or value < m['base'] + 0x1000:
            continue
        before = process.read(value - 6, 6)
        # A return address follows a call: E8 rel32 (5 bytes) or FF /2 (2 or 6 bytes).
        if before and (before[1] == 0xE8 or before[4] == 0xFF and (before[5] & 0x38) == 0x10 or
                       before[0] == 0xFF and (before[1] & 0x38) == 0x10):
            scanned.append({'address': value, 'how': 'stack_scan', 'stackOffset': '0x%X' % offset})
        if len(scanned) >= 24:
            break
    return frames, scanned
