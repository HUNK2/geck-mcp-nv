/* fnvmcp engine companion: an opt-in xNVSE plugin that gives the geck MCP bridge full control of
 * Fallout: New Vegas 1.4.0.525 (fnv-mcp PLAN.md, milestones 4-6).
 *
 * Unlike geck-mcp-nv's address-free companions, this one works with engine addresses, but it hard-codes
 * none of them: the bridge resolves addresses and class layouts from its engine map (built from the
 * decompilation) and sends small primitives:
 *   rd/rs/rp  read memory, a C string, or a pointer chain
 *   wr        write memory (returns the old bytes for the bridge's undo journal)
 *   call      call a function with explicit ecx/edx and stack words
 *   con       run a console line through xNVSE and return what it printed
 *   eval/run  evaluate an xNVSE expression or a script snippet and return a typed result
 *   sched     run a console line later on the main thread (bounded input, releases on failure)
 *   events    xNVSE messages and script errors seen since a sequence number
 * Requests arrive on a named pipe. The pipe thread only queues them; every request is served on the
 * game's main thread from xNVSE's MainGameLoop message. Memory reads and writes go through
 * Read/WriteProcessMemory on this process, so a bad address fails instead of crashing the game. Engine
 * calls are not guarded: a wrong call can crash the game, and the crash doctor explains it.
 *
 * Wire format (one line each way, ASCII):
 *   request:  <id> TAB <op> SP <arg>... [TAB <op> SP <arg>...]... LF   (text arguments are hex)
 *   response: {"id":"<id>","frame":N,"results":[{...},...]} LF
 * "ping" alone is answered by the pipe thread, so it works while the main loop is stalled.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <sddl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "../runtime/nvse_abi.h"

#define COMPANION_VERSION 1
#define WIRE_VERSION 1
#define REQUEST_MAX (1u << 20)
#define RESPONSE_MAX (4u << 20)
#define READ_MAX 65536u
#define OUTPUT_MAX 65536u
#define EVAL_CACHE 256u
#define EVENTS 256u
#define SCHEDULE 64u
#define ARRAY_ITEMS 200u

/* xNVSE array interface (kInterface_ArrayVar = 5): function table only, no version field. */
typedef struct {
    void *CreateArray, *CreateStringMap, *CreateMap, *AssignCommandResult, *SetElement, *AppendElement;
    uint32_t (*GetArraySize)(void *);
    void *(*LookupArrayByID)(uint32_t);
    bool (*GetElement)(void *, const Element *, Element *);
    bool (*GetElements)(void *, Element *, Element *);
    void *GetArrayPacked;
    int (*GetContainerType)(void *);
} ArrayAPI;

typedef struct { uint32_t eax, edx; double st0; } CallResult;
/* Pushes words[count-1]..words[0], loads ecx/edx, calls target, stores eax/edx (and st0 when the
 * function returns a float), then restores esp from ebp, so cdecl, stdcall, thiscall and fastcall
 * callees all return to a balanced stack. */
extern void fnvmcp_invoke(uint32_t target, const uint32_t *words, uint32_t count, uint32_t ecx,
                          uint32_t edx, uint32_t float_return, CallResult *out);
__asm__(
    ".text\n"
    ".globl _fnvmcp_invoke\n"
    "_fnvmcp_invoke:\n"
    "  pushl %ebp\n"
    "  movl %esp, %ebp\n"
    "  pushl %ebx\n"
    "  pushl %esi\n"
    "  pushl %edi\n"
    "  movl 12(%ebp), %esi\n"
    "  movl 16(%ebp), %ecx\n"
    "1:\n"
    "  testl %ecx, %ecx\n"
    "  jz 2f\n"
    "  decl %ecx\n"
    "  pushl (%esi,%ecx,4)\n"
    "  jmp 1b\n"
    "2:\n"
    "  movl 20(%ebp), %ecx\n"
    "  movl 24(%ebp), %edx\n"
    "  movl 8(%ebp), %eax\n"
    "  call *%eax\n"
    "  movl 32(%ebp), %edi\n"
    "  movl %eax, 0(%edi)\n"
    "  movl %edx, 4(%edi)\n"
    "  cmpl $0, 28(%ebp)\n"
    "  je 3f\n"
    "  fstpl 8(%edi)\n"
    "3:\n"
    "  leal -12(%ebp), %esp\n"
    "  popl %edi\n"
    "  popl %esi\n"
    "  popl %ebx\n"
    "  popl %ebp\n"
    "  ret\n");

static ScriptAPI *scripts;
static ConsoleAPI *console;
static ArrayAPI *arrays;
static char pipe_name[128] = "\\\\.\\pipe\\fnvmcp-engine";
static char console_log[MAX_PATH];
static bool console_routed;
static DWORD pid, main_thread;
static ULONGLONG started;
static uint32_t nvse_version, runtime_version;

static volatile LONG frame, pending, loaded, deferred_init, script_errors, last_message = -1;
static volatile ULONGLONG last_frame_tick;
static HANDLE done_event;
static char *request, *response;
static size_t response_len;
static bool response_overflow;

/* ------------------------------------------------------------------------------------ output */
static void emit(const char *format, ...) {
    if (response_overflow) return;
    va_list args;
    va_start(args, format);
    int n = vsnprintf(response + response_len, RESPONSE_MAX - response_len, format, args);
    va_end(args);
    if (n < 0 || (size_t)n >= RESPONSE_MAX - response_len) response_overflow = true;
    else response_len += (size_t)n;
}
static void emit_text(const char *text, size_t length) {
    emit("\"");
    for (size_t i = 0; i < length && !response_overflow; i++) {
        unsigned char c = (unsigned char)text[i];
        if (c == '"' || c == '\\') emit("\\%c", c);
        else if (c == '\n') emit("\\n");
        else if (c == '\r') emit("\\r");
        else if (c == '\t') emit("\\t");
        else if (c < 32 || c > 126) emit("\\u%04x", c);
        else emit("%c", c);
    }
    emit("\"");
}
static void emit_hex(const unsigned char *bytes, size_t length) {
    emit("\"");
    for (size_t i = 0; i < length && !response_overflow; i++) emit("%02x", bytes[i]);
    emit("\"");
}
static void fail(const char *error) { emit("{\"ok\":false,\"error\":\"%s\"}", error); }

/* ------------------------------------------------------------------------------------ parsing */
static bool parse_u32(const char *text, uint32_t *value) {
    if (!text || !*text) return false;
    char *end;
    unsigned long v = strtoul(text, &end, 16);
    if (*end || v > 0xFFFFFFFFul) return false;
    *value = (uint32_t)v;
    return true;
}
static int hex_digit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
/* Decodes hex in place into a NUL-terminated byte string; returns its length or -1. */
static long unhex(char *text) {
    size_t n = strlen(text);
    if (n % 2) return -1;
    for (size_t i = 0; i < n; i += 2) {
        int hi = hex_digit(text[i]), lo = hex_digit(text[i + 1]);
        if (hi < 0 || lo < 0) return -1;
        text[i / 2] = (char)(hi * 16 + lo);
    }
    text[n / 2] = 0;
    return (long)(n / 2);
}

/* ------------------------------------------------------------------------------------ memory */
static bool read_bytes(uint32_t address, void *out, size_t length) {
    SIZE_T done = 0;
    return address >= 0x10000 && ReadProcessMemory(GetCurrentProcess(), (LPCVOID)(uintptr_t)address, out, length, &done) &&
           done == length;
}
static bool read_u32(uint32_t address, uint32_t *value) { return read_bytes(address, value, 4); }
static bool write_bytes(uint32_t address, const void *data, size_t length) {
    MEMORY_BASIC_INFORMATION info;
    if (address < 0x10000 || !VirtualQuery((LPCVOID)(uintptr_t)address, &info, sizeof(info)) || info.State != MEM_COMMIT)
        return false;
    DWORD old = 0;
    bool unprotected = VirtualProtect((LPVOID)(uintptr_t)address, length, PAGE_EXECUTE_READWRITE, &old);
    SIZE_T done = 0;
    bool ok = WriteProcessMemory(GetCurrentProcess(), (LPVOID)(uintptr_t)address, data, length, &done) && done == length;
    if (unprotected) VirtualProtect((LPVOID)(uintptr_t)address, length, old, &old);
    FlushInstructionCache(GetCurrentProcess(), (LPCVOID)(uintptr_t)address, length);
    return ok;
}
/* Reads a C string up to max bytes, page by page so a string ending near unmapped memory still reads. */
static bool read_cstring(uint32_t address, char *out, size_t max, size_t *length) {
    size_t n = 0;
    while (n < max) {
        size_t chunk = 0x1000 - ((address + n) & 0xFFF);
        if (chunk > max - n) chunk = max - n;
        if (!read_bytes(address + (uint32_t)n, out + n, chunk)) {
            *length = n;
            return n > 0;
        }
        void *nul = memchr(out + n, 0, chunk);
        if (nul) { *length = (size_t)((char *)nul - out); return true; }
        n += chunk;
    }
    *length = n;
    return true;
}

/* ------------------------------------------------------------------------------------ events */
typedef struct { LONG seq; ULONGLONG tick; int type; char text[300]; } Event;
static Event events[EVENTS];
static volatile LONG event_seq;
static void record_event(int type, const char *text) {
    LONG seq = InterlockedIncrement(&event_seq);
    Event *e = &events[(unsigned)seq % EVENTS];
    e->seq = 0;
    e->tick = GetTickCount64();
    e->type = type;
    snprintf(e->text, sizeof(e->text), "%s", text ? text : "");
    e->seq = seq;
}
static const char *message_name(int type) {
    static const char *names[] = {"post_load", "exit_game", "exit_to_main_menu", "load_game", "save_game",
                                  "script_precompile", "pre_load_game", "exit_game_console", "post_load_game",
                                  "post_post_load", "script_error", "delete_game", "rename_game", "rename_new_game",
                                  "new_game", "delete_game_name", "rename_game_name", "rename_new_game_name",
                                  "deferred_init"};
    return type >= 0 && type < (int)(sizeof(names) / sizeof(names[0])) ? names[type] : "message";
}

/* ------------------------------------------------------------------------------------ console */
static bool file_size(const char *path, LONGLONG *size) {
    WIN32_FILE_ATTRIBUTE_DATA data;
    if (!GetFileAttributesExA(path, GetFileExInfoStandard, &data)) { *size = 0; return false; }
    *size = ((LONGLONG)data.nFileSizeHigh << 32) | data.nFileSizeLow;
    return true;
}
/* Routes console output to our log with the game's own SetConsoleOutputFile (scof) command:
 * ConsoleManager::Print appends every printed line to that file and closes it (decomp 0x0071DD90). */
static unsigned route_attempts;
static void route_console(void) {
    if (console_routed || !console || !console_log[0] || route_attempts >= 3) return;
    route_attempts++;
    char line[MAX_PATH + 16];
    snprintf(line, sizeof(line), "scof \"%s\"", console_log);
    console_routed = console->RunScriptLine(line, NULL);
}
static void read_console_since(LONGLONG offset) {
    LONGLONG size = 0;
    file_size(console_log, &size);
    if (size <= offset) { emit("\"\""); return; }
    LONGLONG length = size - offset;
    bool truncated = length > OUTPUT_MAX;
    if (truncated) length = OUTPUT_MAX;
    HANDLE file = CreateFileA(console_log, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    char *text = file == INVALID_HANDLE_VALUE ? NULL : malloc((size_t)length);
    DWORD got = 0;
    LARGE_INTEGER at;
    at.QuadPart = offset;
    if (text && SetFilePointerEx(file, at, NULL, FILE_BEGIN) && ReadFile(file, text, (DWORD)length, &got, NULL))
        emit_text(text, got);
    else
        emit("null");
    if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
    free(text);
    if (truncated) emit(",\"truncated\":true");
}

/* ------------------------------------------------------------------------------------ scripts */
typedef struct { char *text; void *script; } Compiled;
static Compiled compiled[EVAL_CACHE];
static unsigned compiled_count;
/* xNVSE allocates a script per compile; cache by text so repeated expressions do not grow memory. */
static void *compile(const char *text, bool expression, const char **failure) {
    for (unsigned i = 0; i < compiled_count; i++)
        if (!strcmp(compiled[i].text, text)) return compiled[i].script;
    void *script = expression ? scripts->CompileExpression(text) : scripts->CompileScript(text);
    if (!script) { *failure = "compile_failed"; return NULL; }
    if (compiled_count < EVAL_CACHE) {
        compiled[compiled_count].text = _strdup(text);
        compiled[compiled_count++].script = script;
    }
    return script;
}
static void emit_element(const Element *e, int depth);
/* The element's union holds a pointer in its first four bytes for forms, strings and arrays. */
static uint32_t element_pointer(const Element *e) {
    uint32_t value;
    memcpy(&value, &e->number, sizeof(value));
    return value;
}
static void emit_array(void *array, int depth) {
    uint32_t size = arrays && arrays->GetArraySize ? arrays->GetArraySize(array) : 0;
    emit("{\"type\":\"array\",\"ptr\":\"0x%08x\",\"size\":%u", (unsigned)(uintptr_t)array, size);
    if (arrays && arrays->GetElements && size && depth < 3) {
        uint32_t n = size < ARRAY_ITEMS ? size : ARRAY_ITEMS;
        Element *values = calloc(size, sizeof(Element)), *keys = calloc(size, sizeof(Element));
        if (values && keys && arrays->GetElements(array, values, keys)) {
            int kind = arrays->GetContainerType ? arrays->GetContainerType(array) : 0;
            emit(",\"container\":\"%s\",\"items\":[", kind == 1 ? "map" : kind == 2 ? "stringmap" : "array");
            for (uint32_t i = 0; i < n; i++) {
                if (i) emit(",");
                if (kind) { emit("{\"key\":"); emit_element(&keys[i], depth + 1); emit(",\"value\":"); }
                emit_element(&values[i], depth + 1);
                if (kind) emit("}");
            }
            emit("]");
            if (n < size) emit(",\"truncated\":true");
        }
        free(values);
        free(keys);
    }
    emit("}");
}
static void emit_element(const Element *e, int depth) {
    if (e->type == 1) {
        if (isfinite(e->number)) emit("{\"type\":\"number\",\"value\":%.17g}", e->number);
        else emit("{\"type\":\"number\",\"value\":null,\"nonFinite\":true}");
    } else if (e->type == 2) {
        uint32_t form = element_pointer(e), id = 0;
        /* TESForm::refID is at +0x0C (decomp tesform.h, offset checked by the decomp build). */
        if (!form) emit("{\"type\":\"form\",\"ptr\":null}");
        else if (read_u32(form + 0x0C, &id)) emit("{\"type\":\"form\",\"ptr\":\"0x%08x\",\"formId\":\"%08X\"}", form, id);
        else emit("{\"type\":\"form\",\"ptr\":\"0x%08x\",\"formId\":null}", form);
    } else if (e->type == 3) {
        const char *text = (const char *)(uintptr_t)element_pointer(e);
        emit("{\"type\":\"string\",\"value\":");
        if (text) emit_text(text, strlen(text));
        else emit("null");
        emit("}");
    } else if (e->type == 4) {
        emit_array((void *)(uintptr_t)element_pointer(e), depth);
    } else {
        emit("{\"type\":\"none\"}");
    }
}

/* ------------------------------------------------------------------------------------ schedule */
typedef struct { ULONGLONG due; char line[512]; bool used; } Scheduled;
static Scheduled schedule[SCHEDULE];
static void run_due(bool all) {
    ULONGLONG now = GetTickCount64();
    for (unsigned i = 0; i < SCHEDULE; i++)
        if (schedule[i].used && (all || now >= schedule[i].due)) {
            schedule[i].used = false;
            if (console) console->RunScriptLine(schedule[i].line, NULL);
        }
}

/* ------------------------------------------------------------------------------------ hooks
 * Watch hooks (milestone 7). The bridge picks the function (a proven decomp address), decodes its
 * prologue and says how many bytes to move ("stolen") and where rel32 fields sit in them. The hook:
 *   target:  jmp entry_stub                    (stolen bytes replaced, rest NOPed)
 *   entry:   push hook; jmp enter_common       enter_common: pushad/pushfd, fnvmcp_on_enter(frame),
 *                                              then "ret" into the trampoline with the stack untouched
 *   tramp:   stolen bytes (rel32 fixed); jmp target+stolen
 * With exit capture, on_enter swaps the caller's return address for exit_stub and keeps the real one
 * on a per-thread shadow stack; exit_stub records eax/edx and the elapsed time and returns to it.
 * Records go to a ring per hook, call times to a per-key table; the bridge reads both with "rd".
 * Hook code never touches the x87 stack (a float result is in st0 at exit) and keeps LastError. */
#define HOOKS 32
#define HOOK_RING 4096u
#define HOOK_WORDS 48u
#define HOOK_TABLE 2048u
#define SHADOW 256
#define HF_EXIT 1u
#define HF_RECORD 2u
#define HF_TABLE 4u
typedef struct { uint32_t key, count; uint64_t total; uint32_t max, pad; } HookStat;
typedef struct {
    uint32_t id, target, flags, nargs, stolen, active;
    int32_t filter_sel, key_sel, deref_sel[2], snap_sel;
    uint32_t filter_value, snap_len;
    volatile LONG seq, calls, lock, table_used, table_full;
    unsigned char original[16];
    unsigned char *entry, *trampoline;
    uint32_t (*records)[HOOK_WORDS];
    HookStat *table;
    char label[40];
} Hook;
typedef struct { uint32_t ret_slot, ret; Hook *hook; uint64_t t0; LONG seq; uint32_t ecx, a0, a1; } ShadowFrame;
typedef struct { int count; ShadowFrame frames[SHADOW]; } Shadow;
static Hook *hooks[HOOKS];
static int hook_count;
static unsigned char *hook_code;
static uint32_t hook_code_used;
static DWORD shadow_tls = TLS_OUT_OF_INDEXES;
static LARGE_INTEGER qpc_frequency;

uint32_t fnvmcp_on_enter(uint32_t *fr);
uint32_t fnvmcp_on_exit(uint32_t *fr);
extern void fnvmcp_enter_common(void);
extern void fnvmcp_exit_stub(void);
/* frame (low to high): eflags edi esi ebp esp ebx edx ecx eax slot ret args... */
__asm__(
    ".text\n"
    ".globl _fnvmcp_enter_common\n"
    "_fnvmcp_enter_common:\n"
    "  pushal\n"
    "  pushfl\n"
    "  movl %esp, %eax\n"
    "  cld\n"
    "  pushl %eax\n"
    "  call _fnvmcp_on_enter\n"
    "  addl $4, %esp\n"
    "  movl %eax, 36(%esp)\n"
    "  popfl\n"
    "  popal\n"
    "  ret\n"
    ".globl _fnvmcp_exit_stub\n"
    "_fnvmcp_exit_stub:\n"
    "  pushl $0\n"
    "  pushal\n"
    "  pushfl\n"
    "  movl %esp, %eax\n"
    "  cld\n"
    "  pushl %eax\n"
    "  call _fnvmcp_on_exit\n"
    "  addl $4, %esp\n"
    "  movl %eax, 36(%esp)\n"
    "  popfl\n"
    "  popal\n"
    "  ret\n");

static Shadow *shadow_get(void) {
    if (shadow_tls == TLS_OUT_OF_INDEXES) return NULL;
    Shadow *s = TlsGetValue(shadow_tls);
    if (!s) {
        s = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(Shadow));
        if (s) TlsSetValue(shadow_tls, s);
    }
    return s;
}
static uint32_t hook_select(const uint32_t *fr, int selector) {
    if (selector == 1) return fr[7];                 /* ecx */
    if (selector >= 2 && selector <= 9) return fr[11 + selector - 2]; /* stack argument 0..7 */
    if (selector == 10) return fr[10];               /* return address (the caller) */
    if (selector == 11) return fr[6];                /* edx */
    if (selector == 12 || selector == 13) {
        /* The caller's first stack argument: [ebp + 8] for a standard frame, [ebx + 8] for MSVC's aligned-stack
         * frame (push ebx; mov ebx, esp; and esp, -16), whose arguments stay addressed through ebx. */
        uint32_t value = 0;
        read_u32((selector == 12 ? fr[3] : fr[5]) + 8, &value);
        return value;
    }
    return 0;
}
static void hook_stat(Hook *h, uint32_t key, uint64_t elapsed) {
    while (InterlockedCompareExchange(&h->lock, 1, 0)) YieldProcessor();
    uint32_t slot = (key * 2654435761u) % HOOK_TABLE;
    for (uint32_t probe = 0; probe < HOOK_TABLE; probe++, slot = (slot + 1) % HOOK_TABLE) {
        HookStat *e = &h->table[slot];
        if (e->count && e->key != key) continue;
        if (!e->count) { e->key = key; h->table_used++; }
        e->count++;
        e->total += elapsed;
        if (elapsed > e->max) e->max = elapsed > 0xffffffffu ? 0xffffffffu : (uint32_t)elapsed;
        InterlockedExchange(&h->lock, 0);
        return;
    }
    h->table_full++;
    InterlockedExchange(&h->lock, 0);
}
uint32_t fnvmcp_on_enter(uint32_t *fr) {
    Hook *h = (Hook *)(uintptr_t)fr[9];
    DWORD error = GetLastError();
    InterlockedIncrement(&h->calls);
    if (!h->active || (h->filter_sel && hook_select(fr, h->filter_sel) != h->filter_value)) goto done;
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    Shadow *s = (h->flags & HF_EXIT) ? shadow_get() : NULL;
    LONG seq = 0;
    if (h->flags & HF_RECORD) {
        seq = InterlockedIncrement(&h->seq);
        uint32_t *r = h->records[(uint32_t)(seq - 1) % HOOK_RING];
        r[0] = 0;
        MemoryBarrier();
        r[1] = 1;
        r[2] = GetCurrentThreadId();
        r[3] = fr[7];
        r[4] = fr[6];
        r[5] = fr[10];
        r[6] = (uint32_t)now.QuadPart;
        r[7] = (uint32_t)(now.QuadPart >> 32);
        r[8] = r[9] = r[10] = r[11] = 0;
        for (uint32_t i = 0; i < 8; i++) r[12 + i] = i < h->nargs ? fr[11 + i] : 0;
        for (int i = 0; i < 2; i++) {
            r[20 + i] = 0;
            if (h->deref_sel[i]) read_u32(hook_select(fr, h->deref_sel[i]), &r[20 + i]);
        }
        r[22] = (uint32_t)frame;
        r[23] = s ? (uint32_t)s->count : 0;
        memset(&r[24], 0, 96);
        if (h->snap_sel && h->snap_len) read_bytes(hook_select(fr, h->snap_sel), &r[24], h->snap_len);
        MemoryBarrier();
        r[0] = (uint32_t)seq;
    }
    if (s && s->count < SHADOW) {
        ShadowFrame *f = &s->frames[s->count++];
        f->ret_slot = (uint32_t)(uintptr_t)&fr[10];
        f->ret = fr[10];
        f->hook = h;
        f->t0 = (uint64_t)now.QuadPart;
        f->seq = seq;
        f->ecx = fr[7];
        f->a0 = fr[11];
        f->a1 = fr[12];
        fr[10] = (uint32_t)(uintptr_t)fnvmcp_exit_stub;
    }
done:
    SetLastError(error);
    return (uint32_t)(uintptr_t)h->trampoline;
}
uint32_t fnvmcp_on_exit(uint32_t *fr) {
    DWORD error = GetLastError();
    Shadow *s = TlsGetValue(shadow_tls);
    uint32_t sp = (uint32_t)(uintptr_t)&fr[10]; /* esp when the hooked function returned */
    /* Frames pushed by deeper calls that never returned (an exception unwound them) sit below this
     * one on the shadow stack; the frame that is returning now is the last one below sp. */
    ShadowFrame f = {0};
    bool found = false;
    while (s && s->count && s->frames[s->count - 1].ret_slot < sp) {
        f = s->frames[--s->count];
        found = true;
    }
    if (!found) {
        /* Unreachable unless the shadow stack was corrupted; there is nowhere safe to return to. */
        record_event(10, "fnvmcp: hook shadow stack lost a frame; terminating");
        TerminateProcess(GetCurrentProcess(), 0xF4C0DE01);
    }
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    uint64_t elapsed = (uint64_t)now.QuadPart - f.t0;
    Hook *h = f.hook;
    if (f.seq) {
        uint32_t *r = h->records[(uint32_t)(f.seq - 1) % HOOK_RING];
        if (r[0] == (uint32_t)f.seq) {
            r[8] = fr[8];
            r[9] = fr[6];
            r[10] = (uint32_t)elapsed;
            r[11] = (uint32_t)(elapsed >> 32);
            MemoryBarrier();
            r[1] = 2;
        }
    }
    if (h->flags & HF_TABLE) {
        uint32_t key = h->key_sel == 1 ? f.ecx : h->key_sel == 2 ? f.a0 : h->key_sel == 3 ? f.a1 : h->key_sel == 10 ? f.ret : 0;
        hook_stat(h, key, elapsed);
    }
    SetLastError(error);
    return f.ret;
}
static Hook *hook_find(uint32_t id) {
    for (int i = 0; i < hook_count; i++)
        if (hooks[i]->id == id) return hooks[i];
    return NULL;
}
static bool code_write(uint32_t address, const void *bytes, size_t length) {
    DWORD old;
    if (!VirtualProtect((void *)(uintptr_t)address, length, PAGE_EXECUTE_READWRITE, &old)) return false;
    memcpy((void *)(uintptr_t)address, bytes, length);
    VirtualProtect((void *)(uintptr_t)address, length, old, &old);
    FlushInstructionCache(GetCurrentProcess(), (void *)(uintptr_t)address, length);
    return true;
}
static void emit_hook(const Hook *h) {
    emit("{\"id\":%u,\"target\":\"0x%08x\",\"label\":", h->id, h->target);
    emit_text(h->label, strlen(h->label));
    emit(",\"active\":%s,\"flags\":%u,\"calls\":%ld,\"seq\":%ld,\"hook\":\"0x%08x\",\"seqAddress\":\"0x%08x\","
         "\"callsAddress\":\"0x%08x\",\"records\":\"0x%08x\",\"ring\":%u,\"recordWords\":%u,\"table\":\"0x%08x\","
         "\"tableSize\":%u,\"tableUsed\":%ld,\"tableFull\":%ld,\"trampoline\":\"0x%08x\",\"stolen\":%u,"
         "\"qpcFrequency\":%lld,\"filter\":%d,\"filterValue\":\"0x%08x\"}",
         h->active ? "true" : "false", h->flags, h->calls, h->seq, (uint32_t)(uintptr_t)h,
         (uint32_t)(uintptr_t)&h->seq, (uint32_t)(uintptr_t)&h->calls, (uint32_t)(uintptr_t)h->records, HOOK_RING,
         HOOK_WORDS, (uint32_t)(uintptr_t)h->table, HOOK_TABLE, h->table_used, h->table_full,
         (uint32_t)(uintptr_t)h->trampoline, h->stolen, (long long)qpc_frequency.QuadPart, h->filter_sel, h->filter_value);
}
/* hook <target> <stolen> <relocs: comma list of rel32 offsets or -> <flags> <filter_sel> <filter_value>
 *      <key_sel> <nargs> <deref0_sel> <deref1_sel> <snap_sel> <snap_len> <label hex> */
static void op_hook(int argc, char **argv) {
    uint32_t target, stolen, flags, filter_value, nargs, snap_len, v[6];
    if (argc != 14 || !parse_u32(argv[1], &target) || !parse_u32(argv[2], &stolen) || stolen < 5 || stolen > 16 ||
        !parse_u32(argv[4], &flags) || !parse_u32(argv[5], &v[0]) || !parse_u32(argv[6], &filter_value) ||
        !parse_u32(argv[7], &v[1]) || !parse_u32(argv[8], &nargs) || nargs > 8 || !parse_u32(argv[9], &v[2]) ||
        !parse_u32(argv[10], &v[3]) || !parse_u32(argv[11], &v[4]) || !parse_u32(argv[12], &snap_len) ||
        snap_len > 96)
        return fail("bad_arguments");
    long label = unhex(argv[13]);
    if (label < 0) return fail("bad_arguments");
    for (int i = 0; i < 5; i++)
        if (v[i] > 13) return fail("bad_selector");
    if (hook_count >= HOOKS) { /* forget removed hooks (their memory stays valid) */
        int kept = 0;
        for (int i = 0; i < hook_count; i++)
            if (hooks[i]->stolen) hooks[kept++] = hooks[i];
        hook_count = kept;
    }
    if (hook_count >= HOOKS) return fail("too_many_hooks");
    for (int i = 0; i < hook_count; i++)
        if (hooks[i]->target == target && hooks[i]->stolen) return fail("already_hooked");
    if (shadow_tls == TLS_OUT_OF_INDEXES && (shadow_tls = TlsAlloc()) == TLS_OUT_OF_INDEXES) return fail("no_tls");
    MEMORY_BASIC_INFORMATION info;
    if (!VirtualQuery((LPCVOID)(uintptr_t)target, &info, sizeof(info)) || info.State != MEM_COMMIT ||
        !(info.Protect & (PAGE_EXECUTE | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)))
        return fail("not_executable");
    Hook *h = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(Hook));
    if (!h) return fail("out_of_memory");
    if (!read_bytes(target, h->original, stolen)) { HeapFree(GetProcessHeap(), 0, h); return fail("unreadable"); }
    if (!hook_code) hook_code = VirtualAlloc(NULL, 65536, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    /* Ring and table are only allocated when used, and never freed: a call that entered before an unhook
     * may still write to them when it returns. */
    if (flags & HF_RECORD) h->records = VirtualAlloc(NULL, HOOK_RING * HOOK_WORDS * 4, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (flags & HF_TABLE) h->table = VirtualAlloc(NULL, HOOK_TABLE * sizeof(HookStat), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!hook_code || ((flags & HF_RECORD) && !h->records) || ((flags & HF_TABLE) && !h->table) || hook_code_used + 64 > 65536)
        return fail("out_of_memory");
    static uint32_t next_id = 1;
    h->id = next_id++;
    h->target = target;
    h->flags = flags;
    h->filter_sel = (int32_t)v[0];
    h->filter_value = filter_value;
    h->key_sel = (int32_t)v[1];
    h->nargs = nargs;
    h->deref_sel[0] = (int32_t)v[2];
    h->deref_sel[1] = (int32_t)v[3];
    h->snap_sel = (int32_t)v[4];
    h->snap_len = snap_len;
    h->stolen = stolen;
    snprintf(h->label, sizeof(h->label), "%.*s", (int)label, argv[13]);
    unsigned char *entry = hook_code + hook_code_used, *tramp = entry + 16;
    hook_code_used += 64;
    entry[0] = 0x68; /* push hook */
    uint32_t value = (uint32_t)(uintptr_t)h;
    memcpy(entry + 1, &value, 4);
    entry[5] = 0xE9; /* jmp enter_common */
    value = (uint32_t)(uintptr_t)fnvmcp_enter_common - (uint32_t)(uintptr_t)(entry + 10);
    memcpy(entry + 6, &value, 4);
    memcpy(tramp, h->original, stolen);
    if (strcmp(argv[3], "-")) {
        for (char *item = argv[3]; item && *item;) {
            char *comma = strchr(item, ',');
            if (comma) *comma = 0;
            uint32_t offset, rel;
            if (!parse_u32(item, &offset) || offset + 4 > stolen) return fail("bad_relocation");
            memcpy(&rel, tramp + offset, 4);
            rel += target - (uint32_t)(uintptr_t)tramp;
            memcpy(tramp + offset, &rel, 4);
            item = comma ? comma + 1 : NULL;
        }
    }
    tramp[stolen] = 0xE9;
    value = target + stolen - (uint32_t)(uintptr_t)(tramp + stolen + 5);
    memcpy(tramp + stolen + 1, &value, 4);
    h->entry = entry;
    h->trampoline = tramp;
    h->active = 1;
    unsigned char patch[16];
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xE9;
    value = (uint32_t)(uintptr_t)entry - (target + 5);
    memcpy(patch + 1, &value, 4);
    hooks[hook_count++] = h;
    if (!code_write(target, patch, stolen)) { hook_count--; return fail("unwritable"); }
    emit("{\"ok\":true,\"hook\":");
    emit_hook(h);
    emit("}");
}
/* unhook <id>: puts the original bytes back if the jump is still ours. Stubs stay allocated, so calls
 * already inside the hook (or returning through exit_stub) finish normally. */
static void op_unhook(int argc, char **argv) {
    uint32_t id;
    if (argc != 2 || !parse_u32(argv[1], &id)) return fail("bad_arguments");
    Hook *h = hook_find(id);
    if (!h) return fail("unknown_hook");
    if (!h->stolen) return fail("already_removed");
    unsigned char now[16], expected[5] = {0xE9};
    uint32_t value = (uint32_t)(uintptr_t)h->entry - (h->target + 5);
    memcpy(expected + 1, &value, 4);
    h->active = 0;
    if (!read_bytes(h->target, now, 5)) return fail("unreadable");
    if (memcmp(now, expected, 5)) return fail("overwritten_by_another_patch");
    if (!code_write(h->target, h->original, h->stolen)) return fail("unwritable");
    h->stolen = 0;
    emit("{\"ok\":true,\"id\":%u}", id);
}
/* hookctl <id> <active 0|1> [clear]: pause/resume recording; "clear" empties the call-time table. */
static void op_hookctl(int argc, char **argv) {
    uint32_t id, active;
    if (argc < 3 || argc > 4 || !parse_u32(argv[1], &id) || !parse_u32(argv[2], &active)) return fail("bad_arguments");
    Hook *h = hook_find(id);
    if (!h) return fail("unknown_hook");
    h->active = active ? 1 : 0;
    if (argc == 4 && !strcmp(argv[3], "clear")) {
        while (InterlockedCompareExchange(&h->lock, 1, 0)) YieldProcessor();
        memset(h->table, 0, HOOK_TABLE * sizeof(HookStat));
        h->table_used = h->table_full = 0;
        InterlockedExchange(&h->lock, 0);
    }
    emit("{\"ok\":true,\"hook\":");
    emit_hook(h);
    emit("}");
}
/* hookfilter <id> <selector> <value>: record only calls whose selected register/argument has this value. */
static void op_hookfilter(int argc, char **argv) {
    uint32_t id, selector, value;
    if (argc != 4 || !parse_u32(argv[1], &id) || !parse_u32(argv[2], &selector) || selector > 13 || !parse_u32(argv[3], &value))
        return fail("bad_arguments");
    Hook *h = hook_find(id);
    if (!h) return fail("unknown_hook");
    h->filter_value = value;
    MemoryBarrier();
    h->filter_sel = (int32_t)selector;
    emit("{\"ok\":true,\"hook\":");
    emit_hook(h);
    emit("}");
}
static void op_hooks(void) {
    emit("{\"ok\":true,\"hooks\":[");
    for (int i = 0; i < hook_count; i++) {
        if (i) emit(",");
        emit_hook(hooks[i]);
    }
    emit("]}");
}

/* ------------------------------------------------------------------------------------ crash context
 * A first vectored exception handler that only observes: on a hardware fault it writes what a crash log
 * cannot know (the hooked engine calls active on the faulting thread with their this/arguments and form
 * IDs, the request being served, the console line being run, recent companion events) to
 * %LOCALAPPDATA%\fnvmcp\crash-context-<pid>-<n>.json, then lets the game's handlers run. Faults a
 * handler later recovers from are written too; the crash doctor matches the file to a crash log by
 * exception address and time. */
static char context_dir[MAX_PATH];
static volatile LONG context_files, serving_request, exiting;
static char op_now[256]; /* the op being served (its text before parsing), for the crash context */
static const char *console_line_now;
static char context_buffer[16384];
static size_t context_len;
static void ctx(const char *format, ...) {
    va_list args;
    va_start(args, format);
    int n = vsnprintf(context_buffer + context_len, sizeof(context_buffer) - context_len, format, args);
    va_end(args);
    if (n > 0) context_len = context_len + (size_t)n < sizeof(context_buffer) ? context_len + (size_t)n : sizeof(context_buffer) - 1;
}
static void ctx_text(const char *text, size_t max) {
    ctx("\"");
    for (size_t i = 0; text && text[i] && i < max; i++) {
        unsigned char c = (unsigned char)text[i];
        if (c == '"' || c == '\\') ctx("\\%c", c);
        else if (c < 0x20 || c > 0x7e) ctx("\\u%04x", c);
        else ctx("%c", c);
    }
    ctx("\"");
}
static void ctx_form(const char *name, uint32_t address) {
    uint32_t vtable = 0, form_id = 0;
    unsigned char type = 0;
    ctx(",\"%s\":\"0x%08x\"", name, address);
    if (address > 0x10000 && read_u32(address, &vtable) && vtable >= 0x00401000 && vtable < 0x01300000 &&
        read_u32(address + 0xC, &form_id) && read_bytes(address + 4, &type, 1))
        ctx(",\"%sFormId\":\"%08X\",\"%sFormType\":%u", name, form_id, name, type);
}
static LONG CALLBACK on_exception(EXCEPTION_POINTERS *e) {
    DWORD code = e->ExceptionRecord->ExceptionCode;
    if (code != EXCEPTION_ACCESS_VIOLATION && code != EXCEPTION_ILLEGAL_INSTRUCTION && code != EXCEPTION_INT_DIVIDE_BY_ZERO &&
        code != EXCEPTION_STACK_OVERFLOW && code != EXCEPTION_PRIV_INSTRUCTION && code != EXCEPTION_ARRAY_BOUNDS_EXCEEDED)
        return EXCEPTION_CONTINUE_SEARCH;
    /* After xNVSE's exit messages the game is shutting down; plugins fault in their exit cleanup there
     * (seen with the companion off too), which is not worth a context file. */
    if (!context_dir[0] || code == EXCEPTION_STACK_OVERFLOW || exiting) return EXCEPTION_CONTINUE_SEARCH;
    LONG n = InterlockedIncrement(&context_files);
    if (n > 8) return EXCEPTION_CONTINUE_SEARCH;
    static volatile LONG busy;
    if (InterlockedExchange(&busy, 1)) return EXCEPTION_CONTINUE_SEARCH;
    context_len = 0;
    FILETIME now;
    GetSystemTimeAsFileTime(&now);
    CONTEXT *c = e->ContextRecord;
    ctx("{\"schema\":1,\"pid\":%lu,\"n\":%ld,\"fileTime\":\"%llu\",\"code\":\"0x%08lx\",\"address\":\"0x%08x\","
        "\"threadId\":%lu,\"mainThread\":%s,\"frame\":%ld,\"servingRequest\":%s,\"registers\":{\"eax\":\"0x%08lx\","
        "\"ebx\":\"0x%08lx\",\"ecx\":\"0x%08lx\",\"edx\":\"0x%08lx\",\"esi\":\"0x%08lx\",\"edi\":\"0x%08lx\","
        "\"ebp\":\"0x%08lx\",\"esp\":\"0x%08lx\",\"eip\":\"0x%08lx\"}",
        (unsigned long)pid, n, ((ULONGLONG)now.dwHighDateTime << 32) | now.dwLowDateTime, (unsigned long)code,
        (uint32_t)(uintptr_t)e->ExceptionRecord->ExceptionAddress, GetCurrentThreadId(),
        GetCurrentThreadId() == main_thread ? "true" : "false", frame, serving_request ? "true" : "false",
        c->Eax, c->Ebx, c->Ecx, c->Edx, c->Esi, c->Edi, c->Ebp, c->Esp, c->Eip);
    if (code == EXCEPTION_ACCESS_VIOLATION && e->ExceptionRecord->NumberParameters >= 2)
        ctx(",\"access\":\"%s\",\"accessAddress\":\"0x%08x\"",
            e->ExceptionRecord->ExceptionInformation[0] == 0 ? "read" : e->ExceptionRecord->ExceptionInformation[0] == 8 ? "execute" : "write",
            (uint32_t)e->ExceptionRecord->ExceptionInformation[1]);
    ctx(",\"hookedCalls\":[");
    Shadow *s = shadow_tls != TLS_OUT_OF_INDEXES ? TlsGetValue(shadow_tls) : NULL;
    for (int i = s ? s->count - 1 : -1, first = 1; i >= 0 && i >= s->count - 16; i--, first = 0) {
        ShadowFrame *f = &s->frames[i];
        ctx("%s{\"label\":", first ? "" : ",");
        ctx_text(f->hook->label, sizeof(f->hook->label));
        ctx(",\"target\":\"0x%08x\",\"caller\":\"0x%08x\"", f->hook->target, f->ret);
        ctx_form("this", f->ecx);
        ctx_form("arg0", f->a0);
        ctx_form("arg1", f->a1);
        ctx("}");
    }
    ctx("],\"consoleLine\":");
    ctx_text(console_line_now, 400);
    ctx(",\"request\":");
    ctx_text(serving_request ? op_now : "", sizeof(op_now));
    ctx(",\"recentEvents\":[");
    LONG last = event_seq;
    for (LONG seq = last - 7 > 1 ? last - 7 : 1, first = 1; seq <= last; seq++) {
        Event *ev = &events[(unsigned)seq % EVENTS];
        if (ev->seq != seq) continue;
        ctx("%s{\"seq\":%ld,\"type\":\"%s\",\"text\":", first ? "" : ",", ev->seq, message_name(ev->type));
        ctx_text(ev->text, 200);
        ctx("}");
        first = 0;
    }
    ctx("]}\n");
    char path[MAX_PATH + 64];
    snprintf(path, sizeof(path), "%s\\crash-context-%lu-%ld.json", context_dir, (unsigned long)pid, n);
    HANDLE file = CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file != INVALID_HANDLE_VALUE) {
        DWORD written;
        WriteFile(file, context_buffer, (DWORD)context_len, &written, NULL);
        CloseHandle(file);
    }
    InterlockedExchange(&busy, 0);
    return EXCEPTION_CONTINUE_SEARCH;
}

/* ------------------------------------------------------------------------------------ ops */
static void op_info(void) {
    emit("{\"ok\":true,\"pid\":%lu,\"started\":\"%llu\",\"companion\":%d,\"wire\":%d,\"nvse\":\"0x%08x\","
         "\"runtime\":\"0x%08x\",\"frame\":%ld,\"loaded\":%s,\"deferredInit\":%s,\"scriptErrors\":%ld,"
         "\"eventSeq\":%ld,\"consoleLog\":",
         (unsigned long)pid, started, COMPANION_VERSION, WIRE_VERSION, nvse_version, runtime_version, frame,
         loaded ? "true" : "false", deferred_init ? "true" : "false", script_errors, event_seq);
    emit_text(console_log, strlen(console_log));
    emit(",\"consoleRouted\":%s,\"mainThread\":%s,\"mainThreadId\":%lu,\"crashContextDir\":",
         console_routed ? "true" : "false", GetCurrentThreadId() == main_thread ? "true" : "false",
         (unsigned long)main_thread);
    emit_text(context_dir, strlen(context_dir));
    emit("}");
}
static void op_read(int argc, char **argv) {
    uint32_t address, length;
    if (argc != 3 || !parse_u32(argv[1], &address) || !parse_u32(argv[2], &length) || !length || length > READ_MAX)
        return fail("bad_arguments");
    unsigned char *buffer = malloc(length);
    if (!buffer) return fail("out_of_memory");
    if (read_bytes(address, buffer, length)) { emit("{\"ok\":true,\"hex\":"); emit_hex(buffer, length); emit("}"); }
    else fail("unreadable");
    free(buffer);
}
static void op_read_string(int argc, char **argv) {
    uint32_t address, max = 1024;
    if (argc < 2 || argc > 3 || !parse_u32(argv[1], &address) || (argc == 3 && !parse_u32(argv[2], &max)) || !max ||
        max > READ_MAX)
        return fail("bad_arguments");
    char *buffer = malloc(max);
    size_t length = 0;
    if (!buffer) return fail("out_of_memory");
    if (read_cstring(address, buffer, max, &length)) {
        emit("{\"ok\":true,\"text\":");
        emit_text(buffer, length);
        emit(length == max ? ",\"truncated\":true}" : "}");
    } else {
        fail("unreadable");
    }
    free(buffer);
}
static void op_read_chain(int argc, char **argv) {
    uint32_t address;
    if (argc < 3 || argc > 34 || !parse_u32(argv[1], &address)) return fail("bad_arguments");
    emit("{\"ok\":");
    size_t mark = response_len;
    emit("true,\"chain\":[");
    for (int i = 2; i < argc; i++) {
        uint32_t offset, value;
        if (!parse_u32(argv[i], &offset)) { response_len = mark; emit("false,\"error\":\"bad_arguments\"}"); return; }
        if (!read_u32(address + offset, &value)) {
            response_len = mark;
            emit("false,\"error\":\"unreadable\",\"step\":%d,\"address\":\"0x%08x\"}", i - 2, address + offset);
            return;
        }
        emit(i > 2 ? ",\"0x%08x\"" : "\"0x%08x\"", value);
        address = value;
    }
    emit("]}");
}
/* ls <node> [max]: walk a singly linked {data, next} list (BSSimpleList / tList nodes) in one frame. */
static void op_list(int argc, char **argv) {
    uint32_t node, max = 500, count = 0, steps = 0;
    if (argc < 2 || argc > 3 || !parse_u32(argv[1], &node) || (argc == 3 && (!parse_u32(argv[2], &max) || !max || max > 4096)))
        return fail("bad_arguments");
    emit("{\"ok\":true,\"items\":[");
    while (node && count < max && steps++ < 65536) {
        uint32_t pair[2];
        if (!read_bytes(node, pair, sizeof(pair))) {
            emit("],\"unreadableAt\":\"0x%08x\"}", node);
            return;
        }
        if (pair[0]) emit(count++ ? ",\"0x%08x\"" : "\"0x%08x\"", pair[0]);
        node = pair[1];
    }
    emit("],\"more\":%s}", node ? "true" : "false");
}
static void op_write(int argc, char **argv) {
    uint32_t address;
    long length;
    if (argc != 3 || !parse_u32(argv[1], &address) || (length = unhex(argv[2])) <= 0 || length > (long)READ_MAX)
        return fail("bad_arguments");
    unsigned char *old = malloc((size_t)length);
    if (!old) return fail("out_of_memory");
    if (!read_bytes(address, old, (size_t)length)) fail("unreadable");
    else if (!write_bytes(address, argv[2], (size_t)length)) fail("unwritable");
    else { emit("{\"ok\":true,\"old\":"); emit_hex(old, (size_t)length); emit("}"); }
    free(old);
}
/* call <target> <ret i|f|v> <ecx> <edx> [word...]: the bridge maps the calling convention to ecx/edx
 * and the stack words (floats and doubles arrive as their bit patterns). */
static void op_call(int argc, char **argv) {
    uint32_t target, ecx, edx, words[32];
    if (argc < 5 || argc > 37 || !parse_u32(argv[1], &target) || target < 0x10000 || strlen(argv[2]) != 1 ||
        !strchr("ifv", argv[2][0]) || !parse_u32(argv[3], &ecx) || !parse_u32(argv[4], &edx))
        return fail("bad_arguments");
    MEMORY_BASIC_INFORMATION info;
    if (!VirtualQuery((LPCVOID)(uintptr_t)target, &info, sizeof(info)) || info.State != MEM_COMMIT ||
        !(info.Protect & (PAGE_EXECUTE | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)))
        return fail("not_executable");
    for (int i = 5; i < argc; i++)
        if (!parse_u32(argv[i], &words[i - 5])) return fail("bad_arguments");
    CallResult result = {0, 0, NAN};
    LONG before = script_errors;
    fnvmcp_invoke(target, words, (uint32_t)(argc - 5), ecx, edx, argv[2][0] == 'f', &result);
    emit("{\"ok\":true,\"eax\":\"0x%08x\",\"edx\":\"0x%08x\"", result.eax, result.edx);
    if (argv[2][0] == 'f') emit(isfinite(result.st0) ? ",\"st0\":%.17g" : ",\"st0\":null", result.st0);
    emit(",\"scriptErrors\":%ld}", script_errors - before);
}
static void *ref_argument(int argc, char **argv, int index, bool *ok) {
    uint32_t ref = 0;
    *ok = argc <= index || parse_u32(argv[index], &ref);
    return (void *)(uintptr_t)ref;
}
static void op_console(int argc, char **argv) {
    bool ok;
    void *ref = ref_argument(argc, argv, 2, &ok);
    if (argc < 2 || argc > 3 || !ok || unhex(argv[1]) <= 0 || strlen(argv[1]) > 511 || strchr(argv[1], '\n'))
        return fail("bad_arguments");
    if (!console || !console->RunScriptLine) return fail("console_api_unavailable");
    route_console();
    LONGLONG before = 0;
    file_size(console_log, &before);
    LONG errors = script_errors;
    console_line_now = argv[1];
    bool accepted = console->version >= 2 && console->RunScriptLine2 ? console->RunScriptLine2(argv[1], ref, false)
                                                                     : console->RunScriptLine(argv[1], ref);
    console_line_now = NULL;
    emit("{\"ok\":true,\"accepted\":%s,\"scriptErrors\":%ld,\"captured\":%s,\"output\":", accepted ? "true" : "false",
         script_errors - errors, console_routed ? "true" : "false");
    read_console_since(before);
    emit("}");
}
static void op_eval(int argc, char **argv, bool expression) {
    bool ok;
    void *ref = ref_argument(argc, argv, 2, &ok);
    if (argc < 2 || argc > 3 || !ok || unhex(argv[1]) <= 0) return fail("bad_arguments");
    if (!scripts || !scripts->CallFunction || !(expression ? (void *)scripts->CompileExpression : (void *)scripts->CompileScript))
        return fail("script_api_unavailable");
    route_console();
    const char *failure = "";
    LONG errors = script_errors;
    LONGLONG before = 0;
    file_size(console_log, &before);
    void *script = compile(argv[1], expression, &failure);
    if (!script) {
        emit("{\"ok\":false,\"error\":\"%s\",\"output\":", failure);
        read_console_since(before);
        emit("}");
        return;
    }
    Element result;
    memset(&result, 0, sizeof(result));
    bool called = scripts->CallFunction(script, ref, NULL, &result, 0);
    emit("{\"ok\":%s,\"called\":%s,\"scriptErrors\":%ld,\"result\":", called && script_errors == errors ? "true" : "false",
         called ? "true" : "false", script_errors - errors);
    emit_element(&result, 0);
    if (script_errors != errors) { emit(",\"output\":"); read_console_since(before); }
    emit("}");
}
/* every <interval_ms> <duration_ms> <script hex> | every cancel <slot> | every list: run a compiled script
 * snippet periodically on the main thread (the bridge uses it to close the DLC arrival message boxes that
 * keep appearing for a minute after a fresh character spawns). */
#define PERIODIC 8
typedef struct { void *script; ULONGLONG next, until; uint32_t interval; LONG runs; char text[120]; } Periodic;
static Periodic periodic[PERIODIC];
static void run_periodic(void) {
    ULONGLONG now = GetTickCount64();
    for (unsigned i = 0; i < PERIODIC; i++) {
        Periodic *p = &periodic[i];
        if (!p->script || now < p->next) continue;
        if (now >= p->until) { p->script = NULL; continue; }
        p->next = now + p->interval;
        Element result;
        memset(&result, 0, sizeof(result));
        scripts->CallFunction(p->script, NULL, NULL, &result, 0);
        p->runs++;
    }
}
static void op_every(int argc, char **argv) {
    uint32_t interval, duration, slot;
    if (argc == 2 && !strcmp(argv[1], "list")) {
        emit("{\"ok\":true,\"tasks\":[");
        bool comma = false;
        ULONGLONG now = GetTickCount64();
        for (unsigned i = 0; i < PERIODIC; i++)
            if (periodic[i].script && now < periodic[i].until) {
                emit(comma ? ",{" : "{");
                comma = true;
                emit("\"slot\":%u,\"intervalMs\":%u,\"msLeft\":%llu,\"runs\":%ld,\"text\":", i, periodic[i].interval,
                     periodic[i].until - now, periodic[i].runs);
                emit_text(periodic[i].text, strlen(periodic[i].text));
                emit("}");
            }
        emit("]}");
        return;
    }
    if (argc == 3 && !strcmp(argv[1], "cancel")) {
        if (!parse_u32(argv[2], &slot) || slot >= PERIODIC) return fail("bad_arguments");
        periodic[slot].script = NULL;
        emit("{\"ok\":true}");
        return;
    }
    if (argc != 4 || !parse_u32(argv[1], &interval) || interval < 16 || !parse_u32(argv[2], &duration) ||
        duration > 3600000 || unhex(argv[3]) <= 0)
        return fail("bad_arguments");
    if (!scripts || !scripts->CompileScript || !scripts->CallFunction) return fail("script_api_unavailable");
    const char *failure = "";
    void *script = compile(argv[3], false, &failure);
    if (!script) return fail(failure);
    for (unsigned i = 0; i < PERIODIC; i++)
        if (!periodic[i].script || GetTickCount64() >= periodic[i].until) {
            periodic[i].script = script;
            periodic[i].interval = interval;
            periodic[i].next = GetTickCount64();
            periodic[i].until = GetTickCount64() + duration;
            periodic[i].runs = 0;
            snprintf(periodic[i].text, sizeof(periodic[i].text), "%s", argv[3]);
            emit("{\"ok\":true,\"slot\":%u}", i);
            return;
        }
    fail("periodic_full");
}
/* compile <script text hex>: compile with xNVSE's CompileScript and return the new Script (not run, not
 * cached: every call gets its own object, which a hot-swap keeps the old code in). */
static void op_compile(int argc, char **argv) {
    if (argc != 2 || unhex(argv[1]) <= 0) return fail("bad_arguments");
    if (!scripts || !scripts->CompileScript) return fail("script_api_unavailable");
    route_console();
    LONGLONG before = 0;
    file_size(console_log, &before);
    LONG errors = script_errors;
    void *script = scripts->CompileScript(argv[1]);
    emit("{\"ok\":%s,\"script\":\"0x%08x\",\"scriptErrors\":%ld,\"output\":", script ? "true" : "false",
         (uint32_t)(uintptr_t)script, script_errors - errors);
    read_console_since(before);
    emit("}");
}
static void op_schedule(int argc, char **argv) {
    uint32_t delay;
    if (argc == 2 && !strcmp(argv[1], "flush")) { run_due(true); emit("{\"ok\":true}"); return; }
    if (argc != 3 || !parse_u32(argv[1], &delay) || delay > 600000 || unhex(argv[2]) <= 0 || strlen(argv[2]) > 511)
        return fail("bad_arguments");
    for (unsigned i = 0; i < SCHEDULE; i++)
        if (!schedule[i].used) {
            schedule[i].due = GetTickCount64() + delay;
            snprintf(schedule[i].line, sizeof(schedule[i].line), "%s", argv[2]);
            schedule[i].used = true;
            emit("{\"ok\":true,\"slot\":%u}", i);
            return;
        }
    fail("schedule_full");
}
static void op_events(int argc, char **argv) {
    uint32_t since = 0;
    if (argc > 2 || (argc == 2 && !parse_u32(argv[1], &since))) return fail("bad_arguments");
    LONG last = event_seq, first = last - (LONG)EVENTS + 1;
    if ((LONG)since + 1 > first) first = (LONG)since + 1;
    emit("{\"ok\":true,\"seq\":%ld,\"lost\":%s,\"events\":[", last, (LONG)since + 1 < last - (LONG)EVENTS + 1 ? "true" : "false");
    bool comma = false;
    for (LONG seq = first; seq <= last; seq++) {
        Event *e = &events[(unsigned)seq % EVENTS];
        if (e->seq != seq) continue;
        emit(comma ? ",{" : "{");
        comma = true;
        emit("\"seq\":%ld,\"tickMs\":%llu,\"type\":\"%s\",\"text\":", e->seq, e->tick, message_name(e->type));
        emit_text(e->text, strlen(e->text));
        emit("}");
    }
    emit("]}");
}
static void serve_op(char *op) {
    char *argv[40];
    int argc = 0;
    snprintf(op_now, sizeof(op_now), "%s", op);
    for (char *token = strtok(op, " "); token && argc < 40; token = strtok(NULL, " ")) argv[argc++] = token;
    if (!argc) return fail("empty_op");
    if (!strcmp(argv[0], "info")) op_info();
    else if (!strcmp(argv[0], "rd")) op_read(argc, argv);
    else if (!strcmp(argv[0], "rs")) op_read_string(argc, argv);
    else if (!strcmp(argv[0], "rp")) op_read_chain(argc, argv);
    else if (!strcmp(argv[0], "ls")) op_list(argc, argv);
    else if (!strcmp(argv[0], "wr")) op_write(argc, argv);
    else if (!strcmp(argv[0], "call")) op_call(argc, argv);
    else if (!strcmp(argv[0], "con")) op_console(argc, argv);
    else if (!strcmp(argv[0], "eval")) op_eval(argc, argv, true);
    else if (!strcmp(argv[0], "run")) op_eval(argc, argv, false);
    else if (!strcmp(argv[0], "sched")) op_schedule(argc, argv);
    else if (!strcmp(argv[0], "every")) op_every(argc, argv);
    else if (!strcmp(argv[0], "compile")) op_compile(argc, argv);
    else if (!strcmp(argv[0], "events")) op_events(argc, argv);
    else if (!strcmp(argv[0], "hook")) op_hook(argc, argv);
    else if (!strcmp(argv[0], "unhook")) op_unhook(argc, argv);
    else if (!strcmp(argv[0], "hookctl")) op_hookctl(argc, argv);
    else if (!strcmp(argv[0], "hooks")) op_hooks();
    else if (!strcmp(argv[0], "hookfilter")) op_hookfilter(argc, argv);
    else fail("unknown_op");
}
/* Serves one request line on the main thread; ops run in order within the same frame. */
static void serve(char *line) {
    response_len = 0;
    response_overflow = false;
    char *ops = strchr(line, '\t');
    if (ops) *ops++ = 0;
    if (strlen(line) > 64) line[64] = 0;
    emit("{\"id\":");
    emit_text(line, strlen(line));
    emit(",\"frame\":%ld,\"results\":[", frame);
    bool first = true;
    while (ops && *ops) {
        char *next = strchr(ops, '\t');
        if (next) *next++ = 0;
        if (!first) emit(",");
        first = false;
        serve_op(ops);
        ops = next;
    }
    emit("]}\n");
    if (response_overflow) {
        response_len = 0;
        response_overflow = false;
        emit("{\"id\":");
        emit_text(line, strlen(line));
        emit(",\"frame\":%ld,\"error\":\"response_too_large\"}\n", frame);
    }
}

/* ------------------------------------------------------------------------------------ pipe */
static bool write_all(HANDLE pipe, const char *data, size_t length) {
    while (length) {
        DWORD n = 0;
        if (!WriteFile(pipe, data, length > 65536 ? 65536 : (DWORD)length, &n, NULL) || !n) return false;
        data += n;
        length -= n;
    }
    return true;
}
static void ping(HANDLE pipe, char *id) {
    char text[512];
    for (char *c = id; *c; c++)
        if (!((*c >= '0' && *c <= '9') || (*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z') || *c == '-')) *c = '_';
    ULONGLONG now = GetTickCount64(), last = last_frame_tick;
    snprintf(text, sizeof(text),
             "{\"id\":\"%.64s\",\"pong\":true,\"pid\":%lu,\"started\":\"%llu\",\"companion\":%d,\"wire\":%d,\"frame\":%ld,"
             "\"msSinceFrame\":%lld,\"loaded\":%s,\"deferredInit\":%s,\"lastMessage\":\"%s\",\"busy\":%s,"
             "\"mainThreadId\":%lu,\"hooks\":%d}\n",
             id, (unsigned long)pid, started, COMPANION_VERSION, WIRE_VERSION, frame, last ? (long long)(now - last) : -1LL,
             loaded ? "true" : "false", deferred_init ? "true" : "false", message_name(last_message),
             pending ? "true" : "false", (unsigned long)main_thread, hook_count);
    write_all(pipe, text, strlen(text));
}
static bool client_gone(HANDLE pipe) {
    DWORD available = 0;
    return !PeekNamedPipe(pipe, NULL, 0, NULL, &available, NULL);
}
static void serve_client(HANDLE pipe) {
    static char buffer[REQUEST_MAX + 1];
    size_t used = 0;
    for (;;) {
        char *end = memchr(buffer, '\n', used);
        if (!end) {
            DWORD n = 0;
            if (used >= REQUEST_MAX || !ReadFile(pipe, buffer + used, (DWORD)(REQUEST_MAX - used), &n, NULL) || !n) return;
            used += n;
            continue;
        }
        *end = 0;
        size_t line_length = (size_t)(end - buffer);
        if (line_length && buffer[line_length - 1] == '\r') buffer[line_length - 1] = 0;
        char *tab = strchr(buffer, '\t');
        if (tab && !strcmp(tab + 1, "ping")) {
            *tab = 0;
            ping(pipe, buffer);
        } else {
            memcpy(request, buffer, line_length + 1);
            InterlockedExchange(&pending, 1);
            for (;;) {
                if (WaitForSingleObject(done_event, 250) == WAIT_OBJECT_0) break;
                if (client_gone(pipe) && InterlockedCompareExchange(&pending, 0, 1) == 1) return;
            }
            if (!write_all(pipe, response, response_len)) return;
        }
        used -= line_length + 1;
        memmove(buffer, end + 1, used);
    }
}
static DWORD WINAPI pipe_thread(void *unused) {
    (void)unused;
    SECURITY_ATTRIBUTES security = {sizeof(security), NULL, FALSE};
    HANDLE token;
    char buffer[256], sddl[300];
    DWORD size = 0;
    LPSTR sid = NULL;
    /* Only this Windows user (and SYSTEM) may open the pipe; remote clients are rejected. */
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        if (GetTokenInformation(token, TokenUser, buffer, sizeof(buffer), &size) &&
            ConvertSidToStringSidA(((TOKEN_USER *)buffer)->User.Sid, &sid)) {
            snprintf(sddl, sizeof(sddl), "D:P(A;;GA;;;%s)(A;;GA;;;SY)", sid);
            ConvertStringSecurityDescriptorToSecurityDescriptorA(sddl, SDDL_REVISION_1, &security.lpSecurityDescriptor, NULL);
            LocalFree(sid);
        }
        CloseHandle(token);
    }
    if (!security.lpSecurityDescriptor) return 1;
    for (;;) {
        HANDLE pipe = CreateNamedPipeA(pipe_name, PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                                       PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS, 1,
                                       RESPONSE_MAX, REQUEST_MAX, 0, &security);
        if (pipe == INVALID_HANDLE_VALUE) { Sleep(1000); continue; }
        if (ConnectNamedPipe(pipe, NULL) || GetLastError() == ERROR_PIPE_CONNECTED) serve_client(pipe);
        FlushFileBuffers(pipe);
        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
    }
}

/* ------------------------------------------------------------------------------------ xNVSE */
static void on_message(Message *m) {
    int type = (int)m->type;
    if (type == MSG_LOOP) {
        if (!main_thread) main_thread = GetCurrentThreadId();
        InterlockedIncrement(&frame);
        last_frame_tick = GetTickCount64();
        if (deferred_init) route_console(); /* script errors reach the log even between requests */
        run_due(false);
        if (scripts && scripts->CallFunction) run_periodic();
        if (InterlockedCompareExchange(&pending, 2, 1) == 1) {
            InterlockedExchange(&serving_request, 1);
            serve(request);
            InterlockedExchange(&serving_request, 0);
            InterlockedExchange(&pending, 0);
            SetEvent(done_event);
        }
        return;
    }
    /* Newer xNVSE messages (form load/unload, 3D set, cell state...) arrive per form: not recorded. */
    if (type > MSG_DEFERRED) return;
    last_message = type;
    if (type == 10) {
        InterlockedIncrement(&script_errors);
        record_event(type, (const char *)m->data);
        return;
    }
    if (type == MSG_DEFERRED) deferred_init = 1;
    if (type == MSG_PRELOAD || type == MSG_MENU || type == MSG_EXIT || type == MSG_EXIT_CONSOLE) loaded = 0;
    if (type == MSG_NEWGAME) loaded = 1;
    if (type == MSG_POSTLOAD) loaded = m->data != NULL;
    if (type == MSG_EXIT || type == MSG_EXIT_CONSOLE) {
        InterlockedExchange(&exiting, 1);
        run_due(true);
    }
    const char *text = (type == 3 || type == 4) && m->data ? (const char *)m->data
                       : type == MSG_POSTLOAD                ? (m->data ? "success" : "failed")
                                                             : "";
    if (type != 5 && type != 9 && type != 19) record_event(type, text);
}

__declspec(dllexport) bool NVSEPlugin_Query(const NVSEInterface *nvse, PluginInfo *info) {
    info->infoVersion = 1;
    info->name = "fnvmcp engine companion";
    info->version = COMPANION_VERSION;
    /* Build check: FalloutNV.exe 1.4.0.525 (runtime 0x040020D0) under xNVSE 6.x only. */
    return !nvse->isEditor && nvse->nvseVersion >= 0x06040090 && nvse->nvseVersion < 0x07000000 &&
           nvse->runtimeVersion == 0x040020D0;
}

__declspec(dllexport) bool NVSEPlugin_Load(const NVSEInterface *nvse) {
    PluginInfo info;
    if (!NVSEPlugin_Query(nvse, &info)) return false;
    nvse_version = nvse->nvseVersion;
    runtime_version = nvse->runtimeVersion;
    scripts = nvse->QueryInterface(6);
    console = nvse->QueryInterface(1);
    arrays = nvse->QueryInterface(5);
    Messaging *messages = nvse->QueryInterface(2);
    if (!messages || messages->version < 4) return false;
    char value[96];
    DWORD n = GetEnvironmentVariableA("FNVMCP_PIPE", value, sizeof(value));
    if (n && n < sizeof(value)) snprintf(pipe_name, sizeof(pipe_name), "\\\\.\\pipe\\%s", value);
    n = GetEnvironmentVariableA("LOCALAPPDATA", value, sizeof(value));
    char folder[MAX_PATH];
    if (n && n < sizeof(value)) {
        snprintf(folder, sizeof(folder), "%s\\fnvmcp", value);
        CreateDirectoryA(folder, NULL);
        n = GetEnvironmentVariableA("FNVMCP_CONSOLE_LOG", console_log, sizeof(console_log));
        if (!n || n >= sizeof(console_log)) snprintf(console_log, sizeof(console_log), "%s\\console.log", folder);
        DeleteFileA(console_log);
        snprintf(context_dir, sizeof(context_dir), "%s", folder);
    }
    QueryPerformanceFrequency(&qpc_frequency);
    AddVectoredExceptionHandler(1, on_exception);
    FILETIME creation, exit_time, kernel, user;
    if (!GetProcessTimes(GetCurrentProcess(), &creation, &exit_time, &kernel, &user)) return false;
    pid = GetCurrentProcessId();
    started = ((ULONGLONG)creation.dwHighDateTime << 32) | creation.dwLowDateTime;
    request = malloc(REQUEST_MAX + 1);
    response = malloc(RESPONSE_MAX);
    done_event = CreateEventA(NULL, FALSE, FALSE, NULL);
    if (!request || !response || !done_event) return false;
    if (!messages->RegisterListener(nvse->GetPluginHandle(), "NVSE", on_message)) return false;
    HANDLE thread = CreateThread(NULL, 0, pipe_thread, NULL, 0, NULL);
    if (!thread) return false;
    CloseHandle(thread);
    return true;
}
