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
#include <tlhelp32.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "../runtime/nvse_abi.h"

/* Race seams for the offline tests (PLAN-3 S0.2). Only a build with -DFNVMCP_TEST_SEAM has them; there SEAM(n)
 * parks the thread that reaches it while fnvmcp_seam == n, so a test can line up two threads at a known point.
 * In the product build SEAM(n) is nothing: the code and data are the same as from a source without the seam lines. */
#ifdef FNVMCP_TEST_SEAM
__declspec(dllexport) volatile LONG fnvmcp_seam = 0;
#define SEAM(n) do { while (fnvmcp_seam == (n)) SwitchToThread(); } while (0)
__declspec(dllexport) volatile LONG fnvmcp_hook_reuse_ms = 60000;
#define HOOK_REUSE_MS ((ULONGLONG)fnvmcp_hook_reuse_ms)
#else
#define SEAM(n) ((void)0)
#endif /* FNVMCP_TEST_SEAM */

/* A removed hook's stub, ring and table are reused this long after the unhook (K3): calls that entered before the
 * unhook have long finished by then. */
#ifndef HOOK_REUSE_MS
#define HOOK_REUSE_MS 60000ull
#endif

#define COMPANION_VERSION 3 /* 3: writes exit notes (exit-*.json); fnv_status trusts their absence only from 3 on */
#define WIRE_VERSION 2
#define EVENT_DEADMAN 0x3E9 /* event record type: the gate's dead-man switch opened a held gate */
#define EVENT_INJECT 0x3EA  /* event record type: the input injector faulted and released its jobs */
#define EVENT_PIPE 0x3EB    /* event record type: the pipe could not be created (H4) */
#define EVENT_LEFT 0x3EC    /* event record type: the bridge disconnected and what it held was let go (H4) */
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
static volatile uint32_t main_rec_hook, main_rec_seq;   /* newest hook record the main thread wrote (blame: watch hits) */
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
    /* VirtualProtect reports only the first page's old protection: remember each region's own and put each back,
     * so a write from a read-only page into a read-write one leaves the second one writable. */
    struct { uint32_t base, size; DWORD old; bool set; } regions[8];
    unsigned nregions = 0;
    bool ok = true;
    for (uint32_t at = address; at < address + length && nregions < 8;) {
        if (!VirtualQuery((LPCVOID)(uintptr_t)at, &info, sizeof(info)) || info.State != MEM_COMMIT) { ok = false; break; }
        uint32_t base = (uint32_t)(uintptr_t)info.BaseAddress, end = base + (uint32_t)info.RegionSize;
        uint32_t stop = end < address + length ? end : address + (uint32_t)length;
        regions[nregions].base = at;
        regions[nregions].size = stop - at;
        regions[nregions].set = VirtualProtect((LPVOID)(uintptr_t)at, stop - at, PAGE_EXECUTE_READWRITE,
                                               &regions[nregions].old);
        nregions++;
        at = stop;
    }
    SIZE_T done = 0;
    ok = ok && WriteProcessMemory(GetCurrentProcess(), (LPVOID)(uintptr_t)address, data, length, &done) && done == length;
    for (unsigned k = 0; k < nregions; k++) {
        DWORD ignored;
        if (regions[k].set) VirtualProtect((LPVOID)(uintptr_t)regions[k].base, regions[k].size, regions[k].old, &ignored);
    }
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
    if (type == EVENT_DEADMAN) return "deadman";
    if (type == EVENT_INJECT) return "inject";
    if (type == EVENT_PIPE) return "pipe";
    if (type == EVENT_LEFT) return "disconnect";
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
#define HF_STACK 8u /* record: words on the stack that may be return addresses into the exe, instead of the snapshot */
/* behaviour actions (hookact), applied to calls that pass the filter */
#define ACT_NONE 0u
#define ACT_SKIP 1u       /* do not run the function: return act_value in eax, pop act_aux bytes */
#define ACT_SKIP_FLOAT 2u /* do not run the function: return the float bits act_value in st0, pop act_aux bytes */
#define ACT_RESULT 3u     /* run it, then replace eax with act_value (needs exit capture) */
#define ACT_ARG 4u        /* replace stack argument act_aux (0..31), ecx (100) or edx (101) with act_value first */
#define ACT_SCRIPT 5u     /* only run the script line */
#define ACT_HOLD 6u       /* hookact <id> hold <pred|ff>: park inside the hook while the predicate passes (w2_stop.c) */
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
    volatile uint32_t act_mode, act_value, act_aux, act_limit;
    volatile LONG act_hits, script_pending;
    volatile LONG stop_next; /* resume next <hook>: the next call that passes the filter parks (w2_stop.c) */
    unsigned char *skip_stub;
    char script[256];
    /* W5 native modules (w5_mod.c): a module's handler runs at each call that passes the filter */
    int (*volatile native)(uint32_t *fr, void *user);
    void *native_user;
    volatile LONG *native_busy;
    unsigned char *native_skip;
    volatile LONG act_applied; /* calls the action changed (act_hits also counts the calls past act_limit; G11) */
    ULONGLONG removed_at; /* GetTickCount64 of the unhook (or of a failed install); 0 while live (K3) */
    volatile LONG inflight; /* shadow frames of calls into this hook not yet returned: no reuse while > 0 (K3) */
    void *spare_next; /* the spare list (removed hooks no longer listed), linked through the hook (K3) */
} Hook;
/* A frame is a watch hook's (hook set) or a mass probe's (probe = index + 1, sample = its sample record). */
typedef struct { uint32_t ret_slot, ret; Hook *hook; uint64_t t0; LONG seq; uint32_t ecx, a0, a1, override, probe; uint32_t *sample; } ShadowFrame;
typedef struct { int count; ShadowFrame frames[SHADOW]; } Shadow;
static Hook *hooks[HOOKS];
static int hook_count;
static Hook *hook_spare; /* removed hooks no longer listed, kept for reuse; unbounded (K3 review I3) */
static unsigned char *hook_code;
static uint32_t hook_code_used;
static DWORD shadow_tls = TLS_OUT_OF_INDEXES;
static LARGE_INTEGER qpc_frequency;

/* Defined in w2_stop.c: parks the thread for a hold or a next stop; returns 0 to run the call, or the skip stub. */
static uint32_t hook_stop(Hook *h, uint32_t *fr);
/* Defined in w5_mod.c: runs a module's native handler; 0 to run the call, or the native skip stub. */
static uint32_t mod_hook_enter(Hook *h, uint32_t *fr);
uint32_t fnvmcp_on_enter(uint32_t *fr);
uint32_t fnvmcp_on_exit(uint32_t *fr, const uint32_t *fpu);
uint32_t fnvmcp_probe_enter(uint32_t *fr);
extern void fnvmcp_enter_common(void);
extern void fnvmcp_exit_stub(void);
extern void fnvmcp_probe_common(void);
/* frame (low to high): eflags edi esi ebp esp ebx edx ecx eax slot ret args...
 * The exit stub also reads st0 without popping it: fxam tells an empty register (an integer result) from a
 * float result, which fsts copies; fpu = {status word, float bits}. */
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
    "  movl %esp, %ebx\n"
    "  subl $8, %esp\n"
    "  movl $0, 4(%esp)\n"
    "  fxam\n"
    "  fnstsw %ax\n"
    "  andl $0xffff, %eax\n"
    "  movl %eax, 0(%esp)\n"
    "  andl $0x4500, %eax\n"
    "  cmpl $0x4100, %eax\n"
    "  je 1f\n"
    "  fsts 4(%esp)\n"
    "1:\n"
    "  movl %esp, %eax\n"
    "  cld\n"
    "  pushl %eax\n"
    "  pushl %ebx\n"
    "  call _fnvmcp_on_exit\n"
    "  addl $16, %esp\n"
    "  movl %eax, 36(%esp)\n"
    "  popfl\n"
    "  popal\n"
    "  ret\n"
    ".globl _fnvmcp_probe_common\n"
    "_fnvmcp_probe_common:\n"
    "  pushal\n"
    "  pushfl\n"
    "  movl %esp, %eax\n"
    "  cld\n"
    "  pushl %eax\n"
    "  call _fnvmcp_probe_enter\n"
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
    if (selector == 14) return (uint32_t)(uintptr_t)&fr[19]; /* address of stack argument 8: snap args 8..31 */
    if (selector == 15) return (uint32_t)(uintptr_t)&fr[10]; /* esp at entry (the return address slot) */
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
static void stack_scan(const uint32_t *fr, uint32_t *out, unsigned slots) {
    /* Words above the return address that point into the exe's code: candidate return addresses of the
     * callers further up (the bridge keeps those preceded by a call instruction). */
    unsigned n = 0;
    uint32_t words[256];
    if (!read_bytes((uint32_t)(uintptr_t)&fr[11], words, sizeof(words))) {
        if (!read_bytes((uint32_t)(uintptr_t)&fr[11], words, 64 * 4)) return;
        for (unsigned i = 64; i < 256; i++) words[i] = 0;
    }
    for (unsigned i = 0; i < 256 && n < slots; i++)
        if (words[i] >= 0x00401000u && words[i] < 0x01100000u) out[n++] = words[i];
}
uint32_t fnvmcp_on_enter(uint32_t *fr) {
    Hook *h = (Hook *)(uintptr_t)fr[9];
    DWORD error = GetLastError();
    InterlockedIncrement(&h->calls);
    if (!h->active || (h->filter_sel && hook_select(fr, h->filter_sel) != h->filter_value)) goto done;
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    if (h->act_mode == ACT_HOLD || h->stop_next) {
        uint32_t stub = hook_stop(h, fr);
        if (stub) {
            SetLastError(error);
            return stub;
        }
    }
    if (h->native) {
        uint32_t stub = mod_hook_enter(h, fr);
        if (stub) {
            SetLastError(error);
            return stub;
        }
    }
    uint32_t act = h->act_mode == ACT_HOLD ? ACT_NONE : h->act_mode, original_arg = 0;
    if (act) {
        LONG hit = InterlockedIncrement(&h->act_hits);
        if (h->act_limit && (uint32_t)hit > h->act_limit) act = ACT_NONE;
        else {
            InterlockedIncrement(&h->act_applied);
            if (h->script[0]) InterlockedIncrement(&h->script_pending);
        }
    }
    if (act == ACT_ARG) {
        uint32_t *slot = h->act_aux == 100 ? &fr[7] : h->act_aux == 101 ? &fr[6] : &fr[11 + (h->act_aux & 31)];
        original_arg = *slot;
        *slot = h->act_value;
    }
    bool skip = (act == ACT_SKIP || act == ACT_SKIP_FLOAT) && h->skip_stub;
    Shadow *s = (h->flags & HF_EXIT) && !skip ? shadow_get() : NULL;
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
        r[23] = (s ? (uint32_t)s->count : 0) | (act << 24);
        memset(&r[24], 0, 96);
        if (h->flags & HF_STACK) stack_scan(fr, &r[24], 24);
        else if (h->snap_sel && h->snap_len) read_bytes(hook_select(fr, h->snap_sel), &r[24], h->snap_len);
        if (act == ACT_ARG) r[9] = original_arg; /* edx slot is unused at entry: the value the caller passed */
        if (skip) { r[8] = h->act_value; r[1] = 3; }
        MemoryBarrier();
        r[0] = (uint32_t)seq;
        if (r[2] == main_thread) { main_rec_hook = h->id; main_rec_seq = (uint32_t)seq; }
    }
    if (skip) {
        SetLastError(error);
        return (uint32_t)(uintptr_t)h->skip_stub;
    }
    if (s && s->count < SHADOW) {
        ShadowFrame *f = &s->frames[s->count++];
        f->ret_slot = (uint32_t)(uintptr_t)&fr[10];
        f->ret = fr[10];
        f->hook = h;
        InterlockedIncrement(&h->inflight); /* dropped when the frame pops (K3) */
        f->t0 = (uint64_t)now.QuadPart;
        f->seq = seq;
        f->ecx = fr[7];
        f->a0 = fr[11];
        f->a1 = fr[12];
        f->override = act == ACT_RESULT;
        f->probe = 0;
        f->sample = NULL;
        fr[10] = (uint32_t)(uintptr_t)fnvmcp_exit_stub;
    }
done:
    SetLastError(error);
    return (uint32_t)(uintptr_t)h->trampoline;
}
static void probe_exit(const ShadowFrame *f, const uint32_t *fr, const uint32_t *fpu, uint64_t elapsed);
uint32_t fnvmcp_on_exit(uint32_t *fr, const uint32_t *fpu) {
    DWORD error = GetLastError();
    Shadow *s = TlsGetValue(shadow_tls);
    uint32_t sp = (uint32_t)(uintptr_t)&fr[10]; /* esp when the hooked function returned */
    /* Frames pushed by deeper calls that never returned (an exception unwound them) sit below this
     * one on the shadow stack; the frame that is returning now is the last one below sp. */
    ShadowFrame f = {0};
    bool found = false;
    while (s && s->count && s->frames[s->count - 1].ret_slot < sp) {
        f = s->frames[--s->count];
        if (f.hook) InterlockedDecrement(&((Hook *)f.hook)->inflight); /* unwound frames too (K3) */
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
    if (f.probe) {
        probe_exit(&f, fr, fpu, elapsed);
        SetLastError(error);
        return f.ret;
    }
    Hook *h = f.hook;
    uint32_t returned = fr[8];
    if (f.override) fr[8] = h->act_value;
    if (f.seq) {
        uint32_t *r = h->records[(uint32_t)(f.seq - 1) % HOOK_RING];
        if (r[0] == (uint32_t)f.seq) {
            r[8] = returned;
            if ((r[23] >> 24) != ACT_ARG) /* with a changed argument r[9] keeps the value the caller passed */
                r[9] = f.override ? fr[8] : fr[6]; /* with a replaced result: the value the caller got */
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
static int code_write_frozen(uint32_t address, const void *bytes, size_t length); /* after probe_suspend */
static void cov_release_range(uint32_t addr, uint32_t len);                       /* w6_cov.c */
static bool cov_in_flight(uint32_t addr, uint32_t len);                           /* w6_cov.c */
/* One code byte from a trap handler, with the other threads running (breakpoint pass and reinstall, a probe's
 * budget disarm). VirtualProtect + store there faults when another thread puts the page protection back between
 * the two (N13). WriteProcessMemory changes the protection itself and fails instead of faulting; it is retried
 * briefly, and writing the same byte twice is harmless. */
static void code_byte(uint32_t address, unsigned char value) {
    unsigned char *p = (unsigned char *)(uintptr_t)address;
    for (int tries = 0; tries < 64 && *p != value; tries++) {
        SIZE_T done = 0;
        if (WriteProcessMemory(GetCurrentProcess(), p, &value, 1, &done) && done == 1) break;
        SwitchToThread();
    }
    FlushInstructionCache(GetCurrentProcess(), p, 1);
}
/* The bytes may span pages with different protections: each page is opened and put back with its own (H4). */
#define CODE_WRITE_PAGES 16
static bool code_write(uint32_t address, const void *bytes, size_t length) {
    if (!length) return true;
    uint32_t first = address & ~0xFFFu, last = (uint32_t)(address + length - 1) & ~0xFFFu;
    if (last < first || (last - first) / 0x1000u >= CODE_WRITE_PAGES) return false;
    unsigned pages = (last - first) / 0x1000u + 1, opened = 0;
    DWORD old[CODE_WRITE_PAGES], ignored;
    for (; opened < pages; opened++)
        if (!VirtualProtect((void *)(uintptr_t)(first + opened * 0x1000u), 1, PAGE_EXECUTE_READWRITE, &old[opened])) break;
    if (opened == pages) memcpy((void *)(uintptr_t)address, bytes, length);
    for (unsigned i = 0; i < opened; i++)
        VirtualProtect((void *)(uintptr_t)(first + i * 0x1000u), 1, old[i], &ignored);
    if (opened != pages) return false;
    FlushInstructionCache(GetCurrentProcess(), (void *)(uintptr_t)address, length);
    return true;
}
static void emit_hook(const Hook *h) {
    emit("{\"id\":%u,\"target\":\"0x%08x\",\"label\":", h->id, h->target);
    emit_text(h->label, strlen(h->label));
    emit(",\"active\":%s,\"flags\":%u,\"calls\":%ld,\"seq\":%ld,\"hook\":\"0x%08x\",\"seqAddress\":\"0x%08x\","
         "\"callsAddress\":\"0x%08x\",\"records\":\"0x%08x\",\"ring\":%u,\"recordWords\":%u,\"table\":\"0x%08x\","
         "\"tableSize\":%u,\"tableUsed\":%ld,\"tableFull\":%ld,\"trampoline\":\"0x%08x\",\"stolen\":%u,"
         "\"qpcFrequency\":%lld,\"filter\":%d,\"filterValue\":\"0x%08x\",\"action\":%u,\"actionValue\":\"0x%08x\","
         "\"actionAux\":%u,\"actionLimit\":%u,\"actionHits\":%ld,\"actionApplied\":%ld}",
         h->active ? "true" : "false", h->flags, h->calls, h->seq, (uint32_t)(uintptr_t)h,
         (uint32_t)(uintptr_t)&h->seq, (uint32_t)(uintptr_t)&h->calls, (uint32_t)(uintptr_t)h->records, HOOK_RING,
         HOOK_WORDS, (uint32_t)(uintptr_t)h->table, HOOK_TABLE, h->table_used, h->table_full,
         (uint32_t)(uintptr_t)h->trampoline, h->stolen, (long long)qpc_frequency.QuadPart, h->filter_sel, h->filter_value,
         h->act_mode, h->act_value, h->act_aux, h->act_limit, h->act_hits, h->act_applied);
}
/* hook <target> <stolen> <relocs: comma list of rel32 offsets or -> <flags> <filter_sel> <filter_value>
 *      <key_sel> <nargs> <deref0_sel> <deref1_sel> <snap_sel> <snap_len> <label hex> */
/* K3: a removed hook's memory (stub, ring, table) for a new hook. Only after HOOK_REUSE_MS since the unhook, and
 * never while a parked stop is inside its stub or trampoline or a native handler still runs on it. */
static bool stop_holds_hook(uint32_t id, uint32_t lo, uint32_t hi);
static bool hook_reusable(Hook *h, ULONGLONG now) {
    uint32_t lo = (uint32_t)(uintptr_t)h->entry;
    return !h->stolen && h->removed_at && now - h->removed_at >= HOOK_REUSE_MS && !h->inflight &&
           !(h->native_busy && *h->native_busy) && !stop_holds_hook(h->id, lo, lo + 64);
}
static Hook *hook_reuse(void) {
    ULONGLONG now = GetTickCount64();
    Hook *h = NULL;
    for (Hook **link = &hook_spare; *link && !h; link = (Hook **)&(*link)->spare_next)
        if (hook_reusable(*link, now)) {
            h = *link;
            *link = h->spare_next;
        }
    for (int i = 0; i < hook_count && !h; i++)
        if (hook_reusable(hooks[i], now)) {
            h = hooks[i];
            memmove(&hooks[i], &hooks[i + 1], (size_t)(hook_count - i - 1) * sizeof(Hook *));
            hook_count--;
        }
    if (!h) return NULL;
    unsigned char *entry = h->entry, *skip = h->skip_stub, *native_skip = h->native_skip;
    uint32_t (*records)[HOOK_WORDS] = h->records;
    HookStat *table = h->table;
    memset(h, 0, sizeof(*h));
    h->entry = entry;
    h->skip_stub = skip;
    h->native_skip = native_skip;
    h->records = records;
    h->table = table;
    if (records) memset(records, 0, HOOK_RING * HOOK_WORDS * 4); /* no stale record reads as a new one */
    return h;
}
/* A hook that never went live (a failed install) goes to the spare list, reusable at once. */
static void hook_retire(Hook *h) {
    h->stolen = 0;
    h->removed_at = 1; /* long ago */
    h->spare_next = hook_spare;
    hook_spare = h;
}

/* K1: bytes another patch owns. An int3 or coverage byte inside a hook's jump corrupts it, and a hook over a live
 * int3 copies the 0xCC into its trampoline: both crash the game on the next call. */
static bool hook_range(uint32_t addr, uint32_t len);
static bool probe_range(uint32_t addr, uint32_t len);
static bool probe_blocks_hook(uint32_t target, uint32_t len);
static bool hook_on_probe_code(void);
static uint32_t hook_restore_bytes(const Hook *h, unsigned char *out, uint32_t *probe_index);
static void probe_unchained(uint32_t probe_index);
static bool isite_in(uint32_t addr, uint32_t len);
static bool code_patch_range(uint32_t addr, uint32_t len);
static void code_site_note(uint32_t addr, uint32_t length, const unsigned char *old, const unsigned char *now);
static bool patched_range(uint32_t addr, uint32_t len) {
    return hook_range(addr, len) || probe_range(addr, len) || code_patch_range(addr, len);
}

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
        if (v[i] > 15) return fail("bad_selector");
    if (hook_count >= HOOKS) { /* stop listing removed hooks; their memory stays valid and is kept for reuse */
        int kept = 0;
        for (int i = 0; i < hook_count; i++) {
            if (hooks[i]->stolen) hooks[kept++] = hooks[i];
            else {
                hooks[i]->spare_next = hook_spare;
                hook_spare = hooks[i];
            }
        }
        hook_count = kept;
    }
    if (hook_count >= HOOKS) return fail("too_many_hooks");
    if (hook_range(target, stolen)) return fail("already_hooked"); /* any overlap, not only the same target (K1) */
    if (probe_blocks_hook(target, stolen) || isite_in(target, stolen)) return fail("in_use");
    if (shadow_tls == TLS_OUT_OF_INDEXES && (shadow_tls = TlsAlloc()) == TLS_OUT_OF_INDEXES) return fail("no_tls");
    MEMORY_BASIC_INFORMATION info;
    if (!VirtualQuery((LPCVOID)(uintptr_t)target, &info, sizeof(info)) || info.State != MEM_COMMIT ||
        !(info.Protect & (PAGE_EXECUTE | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)))
        return fail("not_executable");
    Hook *h = hook_reuse();
    if (!h) h = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(Hook));
    if (!h) return fail("out_of_memory");
    cov_release_range(target, stolen); /* never save a coverage 0xCC as an original byte */
    if (!read_bytes(target, h->original, stolen)) { hook_retire(h); return fail("unreadable"); }
    if (!hook_code) hook_code = VirtualAlloc(NULL, 65536, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    /* Ring and table are only allocated when used, and freed never: a call that entered before an unhook may still
     * write to them when it returns. A reused hook brings its own (K3). */
    if ((flags & HF_RECORD) && !h->records)
        h->records = VirtualAlloc(NULL, HOOK_RING * HOOK_WORDS * 4, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if ((flags & HF_TABLE) && !h->table)
        h->table = VirtualAlloc(NULL, HOOK_TABLE * sizeof(HookStat), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    else if (h->table)
        memset(h->table, 0, HOOK_TABLE * sizeof(HookStat));
    if (!hook_code || ((flags & HF_RECORD) && !h->records) || ((flags & HF_TABLE) && !h->table) ||
        (!h->entry && hook_code_used + 64 > 65536)) {
        hook_retire(h);
        return fail("out_of_memory");
    }
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
    unsigned char *entry = h->entry, *tramp;
    if (!entry) {
        entry = hook_code + hook_code_used;
        hook_code_used += 64;
    }
    tramp = entry + 16;
    h->entry = entry; /* kept with the hook from here on, so a failed install can still be reused */
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
            if (!parse_u32(item, &offset) || offset > stolen - 4) { hook_retire(h); return fail("bad_relocation"); }
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
    int written = code_write_frozen(target, patch, stolen);
    if (written) {
        hook_count--;
        hook_retire(h);
        return fail(written == 1 ? "busy" : "unwritable");
    }
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
    h->act_mode = ACT_NONE;
    if (!read_bytes(h->target, now, 5)) return fail("unreadable");
    if (memcmp(now, expected, 5)) return fail("overwritten_by_another_patch");
    unsigned char restore[16];
    uint32_t chained, length = hook_restore_bytes(h, restore, &chained);
    int written = code_write_frozen(h->target, restore, length);
    if (written) {
        h->active = 1; /* still hooked: keep recording until a later unhook succeeds */
        return fail(written == 1 ? "busy" : "unwritable");
    }
    probe_unchained(chained);
    h->stolen = 0;
    h->native = NULL; /* a module's handler ends with its hook (K3 review I2) */
    h->removed_at = GetTickCount64();
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
        if (h->table) { /* only hooks made with a call table have one */
            while (InterlockedCompareExchange(&h->lock, 1, 0)) YieldProcessor();
            memset(h->table, 0, HOOK_TABLE * sizeof(HookStat));
            h->table_used = h->table_full = 0;
            InterlockedExchange(&h->lock, 0);
        }
    }
    emit("{\"ok\":true,\"hook\":");
    emit_hook(h);
    emit("}");
}
/* hookact <id> <mode> <value> <aux> <limit> <script hex|->: a behaviour action for calls that pass the filter
 * (ACT_*). limit 0 = every call, else only the first <limit> calls. The script line runs on the main thread
 * once per triggering call (at most 8 per frame). */
static void op_hookhold(int argc, char **argv); /* w2_stop.c: hookact <id> hold <pred|ff> [pop] */
static void op_hookact(int argc, char **argv) {
    uint32_t id, mode, value, aux, limit;
    if (argc >= 3 && !strcmp(argv[2], "hold")) return op_hookhold(argc, argv);
    if (argc != 7 || !parse_u32(argv[1], &id) || !parse_u32(argv[2], &mode) || mode > ACT_SCRIPT ||
        !parse_u32(argv[3], &value) || !parse_u32(argv[4], &aux) || !parse_u32(argv[5], &limit))
        return fail("bad_arguments");
    Hook *h = hook_find(id);
    if (!h) return fail("unknown_hook");
    if (!h->stolen) return fail("already_removed");
    if (mode == ACT_RESULT && !(h->flags & HF_EXIT)) return fail("needs_exit_capture");
    if (mode == ACT_ARG && aux > 31 && aux != 100 && aux != 101) return fail("bad_arguments");
    if ((mode == ACT_SKIP || mode == ACT_SKIP_FLOAT) && aux > 0x400) return fail("bad_arguments");
    long script = 0;
    if (strcmp(argv[6], "-")) {
        script = unhex(argv[6]);
        if (script < 0 || script >= (long)sizeof(h->script)) return fail("bad_arguments");
    }
    h->act_mode = ACT_NONE;
    MemoryBarrier();
    if (mode == ACT_SKIP || mode == ACT_SKIP_FLOAT) {
        if (!h->skip_stub) {
            if (hook_code_used + 16 > 65536) return fail("out_of_memory");
            h->skip_stub = hook_code + hook_code_used;
            hook_code_used += 16;
        }
        unsigned char stub[16], *q = stub;
        if (mode == ACT_SKIP) { *q++ = 0xB8; memcpy(q, &value, 4); q += 4; } /* mov eax, value */
        else { /* fld dword [act_value] */
            uint32_t at = (uint32_t)(uintptr_t)&h->act_value;
            *q++ = 0xD9; *q++ = 0x05; memcpy(q, &at, 4); q += 4;
        }
        if (aux) { *q++ = 0xC2; *q++ = (unsigned char)(aux & 0xFF); *q++ = (unsigned char)(aux >> 8); } /* ret aux */
        else *q++ = 0xC3;
        if (!code_write((uint32_t)(uintptr_t)h->skip_stub, stub, (size_t)(q - stub))) return fail("unwritable");
    }
    memset(h->script, 0, sizeof(h->script));
    if (script) memcpy(h->script, argv[6], (size_t)script);
    h->act_value = value;
    h->act_aux = aux;
    h->act_limit = limit;
    InterlockedExchange(&h->act_hits, 0);
    InterlockedExchange(&h->act_applied, 0);
    InterlockedExchange(&h->script_pending, 0);
    MemoryBarrier();
    h->act_mode = mode;
    emit("{\"ok\":true,\"hook\":");
    emit_hook(h);
    emit("}");
}
static void run_hook_scripts(void) {
    for (int i = 0; i < hook_count; i++) {
        Hook *h = hooks[i];
        if (!h->script_pending || !h->script[0] || !console) continue;
        LONG n = InterlockedExchange(&h->script_pending, 0);
        for (LONG k = 0; k < n && k < 8; k++) console->RunScriptLine(h->script, NULL);
    }
}
/* hookfilter <id> <selector> <value>: record only calls whose selected register/argument has this value. */
static void op_hookfilter(int argc, char **argv) {
    uint32_t id, selector, value;
    if (argc != 4 || !parse_u32(argv[1], &id) || !parse_u32(argv[2], &selector) || selector > 15 || !parse_u32(argv[3], &value))
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

/* ------------------------------------------------------------------------------------ mass probes
 * Probes (fnv-mcp NAMING-PLAN N2): light call counters on thousands of functions at once, for naming the
 * decomp's unnamed functions from what actually runs. The bridge plans them offline (whole-instruction
 * prologues, no branch back into the moved bytes, no call before the last moved instruction, bytes equal
 * to the clean exe) and sends batches:
 *   probe init <capacity> <samples> <exit samples> <every>   allocate a set (refused while one is armed)
 *   probe add <hex>    12-byte records: target u32, mode u8 (0 entry jump, 1 sites only: the stub jumps
 *                      straight to the target), stolen u8, relocation count u8, 3 relocation offsets u8, pad 2
 *   probe site <hex>   12-byte records: probe index u32, kind u8 (1 pointer slot such as a vtable entry,
 *                      2 rel32 field of a call or jmp), pad 3, address u32
 *   probe arm          patch every staged or disarmed probe and site with every other thread suspended; a
 *                      probe whose moved bytes a suspended thread is executing stays for the next arm
 *   probe disarm       put the original bytes back (same suspension); stubs stay allocated
 *   probe tag <n>      the current action id, stored in each sample
 *   probe config <samples> <exit samples> <every>
 *   probe clear        zero counters, samples and caller tables
 *   probe info         the arrays' addresses (the bridge reads them in bulk with ReadProcessMemory)
 *   probe list <first> <count>   hex of (target u32, state u8) per probe
 * Stub:    push eax; mov eax,1; lock xadd [count],eax; inc eax; cmp eax,[limit]; jbe sample;
 *          test [mask],eax; jz sample; pop eax; jmp tramp
 * sample:  pop eax; push index; jmp probe_common
 * The fast path changes only arithmetic flags, which no compiled caller expects to survive a call. A sampled
 * call (the first <samples>, then one in <every>) adds its caller to a 16-entry table; the first <samples>
 * also record ecx, edx, the caller, 6 stack words and the vtables at ecx/arg0/arg1, and the first
 * <exit samples> of those the result (eax, and st0 when it holds a float) through the shared shadow stack.
 * Sample words: 0 action, 1 thread, 2 ecx, 3 edx, 4 caller, 5..10 stack words, 11..13 vtables of ecx/arg0/
 * arg1, 14 eax at exit, 15 st0 float bits, 16 exit state (0 none, 1 pending, 2 int, 3 float), 17 frame,
 * 18 elapsed QPC ticks, 19 sample number. */
#define PROBE_WORDS 20u
#define PROBE_CALLERS 16u
#define PROBE_STUB 80u
#define PROBE_TRAMP 56u
#define PROBE_MAX 65536u
#define PROBE_SITES 65536u
#define PROBE_THREADS 512u
#define PROBE_PAGES 8192u
enum { PROBE_STAGED, PROBE_ARMED, PROBE_DISARMED, PROBE_CHANGED };
typedef struct { uint32_t target; uint8_t mode, stolen, state, pad; unsigned char original[16]; unsigned char *stub, *tramp; } Probe;
typedef struct { uint32_t probe, address, original, value; uint8_t kind, state, pad[2]; } ProbeSite;
static Probe *probes;
static bool hook_range(uint32_t addr, uint32_t len) {
    for (int i = 0; i < hook_count; i++)
        if (hooks[i]->stolen && addr < hooks[i]->target + hooks[i]->stolen && hooks[i]->target < addr + len) return true;
    return false;
}
static ProbeSite *probe_sites;
static uint32_t probe_capacity, probe_used, probe_site_used, probe_samples_per;
/* The bytes an armed probe patched at its target: an entry probe (mode 0) its jump over `stolen`, an int3 probe
 * (mode 2) one byte; a call-site probe (mode 1) patches only its callers' fields, nothing at the target. */
static uint32_t probe_patch_size(const Probe *p) {
    if (p->state != PROBE_ARMED) return 0;
    return p->mode == 0 ? p->stolen : p->mode == 2 ? 1 : 0;
}
static bool probe_range(uint32_t addr, uint32_t len) {
    for (uint32_t i = 0; i < probe_used; i++) {
        uint32_t size = probe_patch_size(&probes[i]);
        if (size && addr < probes[i].target + size && probes[i].target < addr + len) return true;
    }
    return false;
}
/* A hook may start exactly at an entry probe's target: it relocates the probe's jump into its trampoline (the
 * designed chain; probe disarm then sees its bytes changed and leaves them). Any other overlap, and any overlap with
 * an int3 probe, would cut an instruction or copy a 0xCC (K1, S7 review I1). */
static bool probe_blocks_hook(uint32_t target, uint32_t len) {
    for (uint32_t i = 0; i < probe_used; i++) {
        uint32_t size = probe_patch_size(&probes[i]);
        if (!size || !(target < probes[i].target + size && probes[i].target < target + len)) continue;
        if (probes[i].mode == 0 && target == probes[i].target) continue;
        return true;
    }
    return false;
}
static volatile LONG *probe_counts;  /* [capacity] calls */
static volatile LONG *probe_meta;    /* [capacity][2]: sampled calls, caller-table overflow */
static uint32_t *probe_samples;      /* [capacity][samples_per][PROBE_WORDS] */
static volatile LONG *probe_callers; /* [capacity][PROBE_CALLERS][2]: return address, count */
static unsigned char *probe_code;
/* A live hook whose saved first bytes are a jump into the current probe code (it chained on an entry probe): a new
 * probe set would reuse that code area, and the unhook would put back a jump into it (S7 review I5). */
static bool hook_on_probe_code(void) {
    if (!probe_code) return false;
    uint32_t lo = (uint32_t)(uintptr_t)probe_code, hi = lo + probe_capacity * PROBE_STUB;
    for (int i = 0; i < hook_count; i++) {
        Hook *h = hooks[i];
        if (!h->stolen || h->original[0] != 0xE9) continue;
        int32_t rel;
        memcpy(&rel, h->original + 1, 4);
        uint32_t dest = h->target + 5 + (uint32_t)rel;
        if (dest >= lo && dest < hi) return true;
    }
    return false;
}
static volatile uint32_t probe_limit, probe_mask = 0xFFFFFFFFu, probe_action, probe_exit_samples;
/* The bytes an unhook writes back. A hook that chained on an entry probe saved the probe's jump; when that probe is
 * no longer armed (disarmed while the hook held its target: it saw its jump changed and left it), writing the jump
 * back would leave a live jump into probe code nothing tracks, and the next probe set would send the function's calls
 * into another function's trampoline (round 4 M1). Then the probe's clean bytes go back instead, over the probe's
 * stolen length, with the hook's own saved bytes after it. *probe_index names that probe, else UINT32_MAX. */
static uint32_t hook_restore_bytes(const Hook *h, unsigned char *out, uint32_t *probe_index) {
    *probe_index = UINT32_MAX;
    memcpy(out, h->original, h->stolen);
    uint32_t length = h->stolen;
    if (!probe_code || length < 5 || h->original[0] != 0xE9) return length;
    int32_t rel;
    memcpy(&rel, h->original + 1, 4);
    uint32_t dest = h->target + 5 + (uint32_t)rel, lo = (uint32_t)(uintptr_t)probe_code;
    if (dest < lo || dest >= lo + probe_capacity * PROBE_STUB || (dest - lo) % PROBE_STUB) return length;
    uint32_t i = (dest - lo) / PROBE_STUB;
    if (i >= probe_used) return length;
    const Probe *p = &probes[i];
    if (p->mode != 0 || p->target != h->target || p->state == PROBE_ARMED || p->stolen > 16) return length;
    memcpy(out, p->original, p->stolen);
    if (p->stolen > length) length = p->stolen;
    *probe_index = i;
    return length;
}
/* After hook_restore_bytes gave a probe's clean bytes back: the probe is disarmed (a later arm may patch it again). */
static void probe_unchained(uint32_t probe_index) {
    if (probe_index != UINT32_MAX && probe_index < probe_used) probes[probe_index].state = PROBE_DISARMED;
}

uint32_t fnvmcp_probe_enter(uint32_t *fr) {
    DWORD error = GetLastError();
    uint32_t i = fr[9], ret = fr[10];
    volatile LONG *table = &probe_callers[(size_t)i * PROBE_CALLERS * 2];
    bool placed = false;
    for (uint32_t k = 0; k < PROBE_CALLERS && !placed; k++) {
        LONG seen = table[2 * k];
        if (!seen) seen = InterlockedCompareExchange(&table[2 * k], (LONG)ret, 0);
        if (!seen || seen == (LONG)ret) {
            InterlockedIncrement(&table[2 * k + 1]);
            placed = true;
        }
    }
    if (!placed) InterlockedIncrement(&probe_meta[2 * i + 1]);
    LONG n = InterlockedIncrement(&probe_meta[2 * i]);
    if ((uint32_t)n <= probe_samples_per && (uint32_t)n <= probe_limit) {
        uint32_t *s = &probe_samples[((size_t)i * probe_samples_per + (uint32_t)(n - 1)) * PROBE_WORDS];
        s[16] = 0;
        s[0] = probe_action;
        s[1] = GetCurrentThreadId();
        s[2] = fr[7];
        s[3] = fr[6];
        s[4] = ret;
        if (!read_bytes((uint32_t)(uintptr_t)&fr[11], &s[5], 24)) memset(&s[5], 0, 24);
        if (!read_u32(fr[7], &s[11])) s[11] = 0;
        if (!read_u32(s[5], &s[12])) s[12] = 0;
        if (!read_u32(s[6], &s[13])) s[13] = 0;
        s[14] = s[15] = s[18] = 0;
        s[17] = (uint32_t)frame;
        s[19] = (uint32_t)n;
        Shadow *sh = (uint32_t)n <= probe_exit_samples ? shadow_get() : NULL;
        if (sh && sh->count < SHADOW) {
            LARGE_INTEGER now;
            QueryPerformanceCounter(&now);
            ShadowFrame *f = &sh->frames[sh->count++];
            memset(f, 0, sizeof(*f));
            f->ret_slot = (uint32_t)(uintptr_t)&fr[10];
            f->ret = ret;
            f->t0 = (uint64_t)now.QuadPart;
            f->ecx = fr[7];
            f->a0 = s[5];
            f->a1 = s[6];
            f->probe = i + 1;
            f->sample = s;
            s[16] = 1;
            fr[10] = (uint32_t)(uintptr_t)fnvmcp_exit_stub;
        }
    }
    SetLastError(error);
    return (uint32_t)(uintptr_t)probes[i].tramp;
}
static void probe_exit(const ShadowFrame *f, const uint32_t *fr, const uint32_t *fpu, uint64_t elapsed) {
    uint32_t *s = f->sample;
    bool is_float = (fpu[0] & 0x4500u) != 0x4100u;
    s[14] = fr[8];
    s[15] = is_float ? fpu[1] : 0;
    s[18] = elapsed > 0xFFFFFFFFull ? 0xFFFFFFFFu : (uint32_t)elapsed;
    MemoryBarrier();
    s[16] = is_float ? 3 : 2;
}
static unsigned char *put32(unsigned char *c, uint32_t v) { memcpy(c, &v, 4); return c + 4; }
static uint32_t addr_of(const volatile void *p) { return (uint32_t)(uintptr_t)p; }
static bool probe_build(uint32_t i, const unsigned char *record) {
    Probe *p = &probes[i];
    memset(p, 0, sizeof(*p));
    memcpy(&p->target, record, 4);
    p->mode = record[4];
    p->stolen = record[5];
    uint8_t nrel = record[6];
    if (p->mode > 2 || nrel > 3 || (p->mode == 0 && (p->stolen < 5 || p->stolen > 16)) ||
        (p->mode == 2 && (p->stolen < 1 || p->stolen > 15)))
        return false;
    if (p->mode != 1 && !read_bytes(p->target, p->original, p->stolen)) return false;
    p->stub = probe_code + (size_t)i * PROBE_STUB;
    p->tramp = p->stub + PROBE_TRAMP;
    unsigned char *c = p->stub, *t = p->tramp;
    *c++ = 0x50;                                                     /* push eax */
    *c++ = 0xB8; c = put32(c, 1);                                    /* mov eax, 1 */
    *c++ = 0xF0; *c++ = 0x0F; *c++ = 0xC1; *c++ = 0x05;              /* lock xadd [count], eax */
    c = put32(c, addr_of(&probe_counts[i]));
    *c++ = 0x40;                                                     /* inc eax */
    *c++ = 0x3B; *c++ = 0x05; c = put32(c, addr_of(&probe_limit));   /* cmp eax, [limit] */
    *c++ = 0x76; unsigned char *j1 = c++;                            /* jbe sample */
    *c++ = 0x85; *c++ = 0x05; c = put32(c, addr_of(&probe_mask));    /* test [mask], eax */
    *c++ = 0x74; unsigned char *j2 = c++;                            /* jz sample */
    *c++ = 0x58;                                                     /* pop eax */
    *c++ = 0xE9; c = put32(c, addr_of(t) - (addr_of(c) + 4));        /* jmp tramp */
    *j1 = (unsigned char)(c - (j1 + 1));
    *j2 = (unsigned char)(c - (j2 + 1));
    *c++ = 0x58;                                                     /* sample: pop eax */
    *c++ = 0x68; c = put32(c, i);                                    /* push index */
    *c++ = 0xE9; c = put32(c, addr_of((void *)fnvmcp_probe_common) - (addr_of(c) + 4));
    if (p->mode == 0 || p->mode == 2) { /* int3 probes run the same relocated first instruction(s) */
        memcpy(t, p->original, p->stolen);
        for (uint8_t k = 0; k < nrel; k++) {
            uint8_t offset = record[7 + k];
            uint32_t rel;
            if (offset + 4u > p->stolen) return false;
            memcpy(&rel, t + offset, 4);
            rel += p->target - addr_of(t);
            memcpy(t + offset, &rel, 4);
        }
        t[p->stolen] = 0xE9;
        put32(t + p->stolen + 1, p->target + p->stolen - (addr_of(t + p->stolen) + 5));
    } else {
        t[0] = 0xE9;
        put32(t + 1, p->target - (addr_of(t) + 5));
    }
    p->state = PROBE_STAGED;
    return true;
}
/* int3 probes (mode 2, NAMING-PLAN N7): for functions too small for a jump and with no call site to retarget,
 * one 0xCC byte at the first instruction. The trap handler counts the call, records the caller from [esp] and
 * resumes in the trampoline (the relocated first instruction, then a jump back): no single-stepping. A trap
 * costs microseconds, so a probe that reaches int3_budget calls puts its byte back by itself. */
static void *probe_alloc(size_t bytes, DWORD protect);
static uint32_t *int3_sorted; /* probe indexes of every int3 probe, sorted by target */
static volatile LONG int3_count;
static volatile uint32_t int3_budget = 200000;
static volatile LONG int3_hot;
static void probe_caller(uint32_t i, uint32_t ret) {
    volatile LONG *table = &probe_callers[(size_t)i * PROBE_CALLERS * 2];
    for (uint32_t k = 0; k < PROBE_CALLERS; k++) {
        LONG seen = table[2 * k];
        if (!seen) seen = InterlockedCompareExchange(&table[2 * k], (LONG)ret, 0);
        if (!seen || seen == (LONG)ret) {
            InterlockedIncrement(&table[2 * k + 1]);
            return;
        }
    }
    InterlockedIncrement(&probe_meta[2 * i + 1]);
}
static int int3_find(uint32_t address) {
    LONG lo = 0, hi = int3_count - 1;
    while (lo <= hi) {
        LONG mid = (lo + hi) / 2;
        uint32_t t = probes[int3_sorted[mid]].target;
        if (t == address) return (int)int3_sorted[mid];
        if (t < address) lo = mid + 1; else hi = mid - 1;
    }
    return -1;
}
static int int3_compare(const void *a, const void *b) {
    uint32_t x = probes[*(const uint32_t *)a].target, y = probes[*(const uint32_t *)b].target;
    return x < y ? -1 : x > y;
}
static void int3_index(void) {
    if (!int3_sorted) int3_sorted = probe_alloc((size_t)PROBE_MAX * 4, PAGE_READWRITE);
    if (!int3_sorted) return;
    LONG n = 0;
    for (uint32_t i = 0; i < probe_used; i++)
        if (probes[i].mode == 2) int3_sorted[n++] = i;
    qsort(int3_sorted, (size_t)n, 4, int3_compare);
    int3_count = n;
}
/* Hardware watchpoints (NAMING-PLAN N7 field watch): DR0..DR3 on the main thread or every thread. */
#define DBG_HITS 4096u
/* mark_hook / mark_seq: the newest hook record the main thread wrote before the hit (blame matches a write made by
   a script to the statement record of ScriptRunner::EvaluateLine that was running). */
typedef struct { uint32_t seq, slot, eip, thread, value, ret, frame_ret, frame, mark_hook, mark_seq; } DbgHit;
static DbgHit dbg_hits[DBG_HITS];
static volatile LONG dbg_seq;
/* dbg_bp_slots: the slots the trap breakpoints (w2_bp.c) own; dbg_scope[k]: slot k also watches the other threads. */
static uint32_t dbg_addr[4], dbg_len[4], dbg_rw[4], dbg_active, dbg_bp_slots, dbg_scope[4];
static bool bp_on_breakpoint(EXCEPTION_POINTERS *e, LONG *result);
static bool bp_on_single_step(EXCEPTION_POINTERS *e, LONG *result);
static void bp_reap(void);
/* Records a watchpoint hit for each slot bit in `which`, then clears DR6. */
static void dbg_note(EXCEPTION_POINTERS *e, uint32_t which) {
    CONTEXT *c = e->ContextRecord;
    for (uint32_t s = 0; s < 4; s++) {
        if (!(which & (1u << s))) continue;
        LONG n = InterlockedIncrement(&dbg_seq);
        DbgHit *h = &dbg_hits[(uint32_t)(n - 1) % DBG_HITS];
        h->seq = (uint32_t)n;
        h->slot = s;
        h->eip = (uint32_t)(uintptr_t)e->ExceptionRecord->ExceptionAddress;
        h->thread = GetCurrentThreadId();
        if (!read_u32(dbg_addr[s], &h->value)) h->value = 0;
        if (!read_u32((uint32_t)c->Esp, &h->ret)) h->ret = 0;
        if (!read_u32((uint32_t)c->Ebp + 4, &h->frame_ret)) h->frame_ret = 0;
        h->frame = (uint32_t)frame;
        h->mark_hook = h->thread == main_thread ? main_rec_hook : 0;
        h->mark_seq = h->thread == main_thread ? main_rec_seq : 0;
    }
    c->Dr6 = 0;
}
static bool cov_on_breakpoint(EXCEPTION_POINTERS *e, LONG *result); /* w6_cov.c */
static LONG CALLBACK on_trap(EXCEPTION_POINTERS *e) {
    DWORD code = e->ExceptionRecord->ExceptionCode;
    CONTEXT *c = e->ContextRecord;
    LONG handled;
    if (code == EXCEPTION_BREAKPOINT && cov_on_breakpoint(e, &handled)) return handled;
    if (code == EXCEPTION_BREAKPOINT && bp_on_breakpoint(e, &handled)) return handled;
    if (code == EXCEPTION_SINGLE_STEP && bp_on_single_step(e, &handled)) return handled;
    if (code == EXCEPTION_BREAKPOINT && int3_count) {
        int i = int3_find((uint32_t)(uintptr_t)e->ExceptionRecord->ExceptionAddress);
        if (i < 0) return EXCEPTION_CONTINUE_SEARCH;
        if (probes[i].state != PROBE_ARMED) { /* disarmed while this trap was in flight */
            c->Eip = addr_of(probes[i].tramp);
            return EXCEPTION_CONTINUE_EXECUTION;
        }
        LONG n = InterlockedIncrement(&probe_counts[i]);
        uint32_t ret = 0;
        if (read_u32((uint32_t)c->Esp, &ret)) probe_caller((uint32_t)i, ret);
        if ((uint32_t)n >= int3_budget) {
            unsigned char *code_at = (unsigned char *)(uintptr_t)probes[i].target;
            if (*code_at == 0xCC) code_byte(probes[i].target, probes[i].original[0]); /* other threads run: no VirtualProtect + store */
            probes[i].state = PROBE_DISARMED;
            InterlockedIncrement(&int3_hot);
        }
        c->Eip = addr_of(probes[i].tramp);
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    if (code == EXCEPTION_SINGLE_STEP && dbg_active) {
        uint32_t which = (uint32_t)c->Dr6 & dbg_active & 0xF;
        if (!which) return EXCEPTION_CONTINUE_SEARCH;
        dbg_note(e, which);
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    return EXCEPTION_CONTINUE_SEARCH;
}
/* dbg set <slot 0-3> <address> <length 1|2|4> <access 1 write|3 read-write> <scope 0 main thread|1 every thread>
 * dbg clear <slot|f for all>     dbg read <after seq>     dbg info
 * Debug registers are per thread and a thread cannot load its own, so a helper thread suspends the targets
 * (the main thread is parked waiting for the helper), sets DR0..DR3/DR7 and resumes them. A hit is recorded by
 * the trap handler: instruction after the access, thread, the value now at the address, [esp], [ebp+4]. */
/* The debug registers a thread should have now (watchpoints and hardware breakpoints, by their scope). Used by
 * dbg_worker for running threads, and for a thread parked inside an exception handler: Windows restores its debug
 * registers from the handler's CONTEXT on resume, so a change made while it was parked must go into that CONTEXT. */
static void dbg_fill(CONTEXT *c, DWORD tid) {
    DWORD dr7 = c->Dr7;
    for (uint32_t k = 0; k < 4; k++) {
        dr7 &= ~((3u << (2 * k)) | (0xFu << (16 + 4 * k)));
        if ((dbg_active & (1u << k)) && (dbg_scope[k] || tid == main_thread)) {
            uint32_t len = dbg_len[k] == 1 ? 0 : dbg_len[k] == 2 ? 1 : 3;
            dr7 |= (1u << (2 * k)) | ((dbg_rw[k] & 3u) << (16 + 4 * k)) | (len << (18 + 4 * k));
        }
    }
    c->Dr0 = dbg_active & 1 ? dbg_addr[0] : 0;
    c->Dr1 = dbg_active & 2 ? dbg_addr[1] : 0;
    c->Dr2 = dbg_active & 4 ? dbg_addr[2] : 0;
    c->Dr3 = dbg_active & 8 ? dbg_addr[3] : 0;
    c->Dr7 = dr7;
}
static DWORD WINAPI dbg_worker(LPVOID arg) {
    (void)arg;
    DWORD applied = 0;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    THREADENTRY32 te;
    te.dwSize = sizeof(te);
    DWORD self = GetCurrentThreadId();
    for (BOOL more = Thread32First(snap, &te); more; more = Thread32Next(snap, &te)) {
        if (te.th32OwnerProcessID != pid || te.th32ThreadID == self) continue;
        HANDLE h = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_SET_CONTEXT, FALSE, te.th32ThreadID);
        if (!h) continue;
        if (SuspendThread(h) != (DWORD)-1) {
            CONTEXT c;
            memset(&c, 0, sizeof(c));
            c.ContextFlags = CONTEXT_DEBUG_REGISTERS;
            if (GetThreadContext(h, &c)) {
                dbg_fill(&c, te.th32ThreadID);
                if (SetThreadContext(h, &c)) applied++;
            }
            ResumeThread(h);
        }
        CloseHandle(h);
    }
    CloseHandle(snap);
    return applied;
}
static DWORD dbg_apply(void) {
    HANDLE t = CreateThread(NULL, 0, dbg_worker, NULL, 0, NULL);
    if (!t) return 0;
    DWORD waited = WaitForSingleObject(t, 5000);
    DWORD n = 0;
    if (waited != WAIT_OBJECT_0 || !GetExitCodeThread(t, &n) || n == STILL_ACTIVE)
        n = (DWORD)-1; /* timed out: the registers may not be on every thread yet */
    CloseHandle(t);
    return n;
}
static void op_dbg(int argc, char **argv) {
    uint32_t v[5];
    bp_reap();
    const char *what = argc > 1 ? argv[1] : "";
    if (!strcmp(what, "set")) {
        if (argc != 7 || !parse_u32(argv[2], &v[0]) || !parse_u32(argv[3], &v[1]) || !parse_u32(argv[4], &v[2]) ||
            !parse_u32(argv[5], &v[3]) || !parse_u32(argv[6], &v[4]) || v[0] > 3 || (v[2] != 1 && v[2] != 2 && v[2] != 4) ||
            (v[1] & (v[2] - 1)) || (v[3] != 1 && v[3] != 3) || v[4] > 1 || v[1] < 0x10000)
            return fail("bad_arguments");
        uint32_t probe;
        if (dbg_bp_slots & (1u << v[0])) return fail("no_debug_register");
        if (!read_u32(v[1] & ~3u, &probe)) return fail("unreadable");
        dbg_addr[v[0]] = v[1];
        dbg_len[v[0]] = v[2];
        dbg_rw[v[0]] = v[3];
        dbg_active |= 1u << v[0];
        dbg_scope[v[0]] = v[4];
        DWORD n = dbg_apply();
        bool timed_out = n == (DWORD)-1;
        emit("{\"ok\":%s,\"slot\":%u,\"threads\":%lu,\"seq\":%ld%s}", n && !timed_out ? "true" : "false", v[0],
             (unsigned long)(timed_out ? 0 : n), dbg_seq, timed_out ? ",\"error\":\"timeout\"" : "");
        return;
    }
    if (!strcmp(what, "clear")) {
        if (argc != 3) return fail("bad_arguments");
        if (!strcmp(argv[2], "f")) dbg_active &= dbg_bp_slots;
        else if (parse_u32(argv[2], &v[0]) && v[0] <= 3) {
            if (dbg_bp_slots & (1u << v[0])) return fail("no_debug_register");
            dbg_active &= ~(1u << v[0]);
        } else return fail("bad_arguments");
        DWORD n = dbg_apply(); /* every thread is visited, so a cleared slot comes off everywhere */
        bool timed_out = n == (DWORD)-1;
        emit("{\"ok\":%s,\"active\":%u,\"threads\":%lu%s}", timed_out ? "false" : "true", dbg_active,
             (unsigned long)(timed_out ? 0 : n), timed_out ? ",\"error\":\"timeout\"" : "");
        return;
    }
    if (!strcmp(what, "read")) {
        if (argc != 3 || !parse_u32(argv[2], &v[0])) return fail("bad_arguments");
        LONG last = dbg_seq;
        uint32_t first = v[0] + 1;
        if (last > (LONG)DBG_HITS && first < (uint32_t)last - DBG_HITS + 1) first = (uint32_t)last - DBG_HITS + 1;
        emit("{\"ok\":true,\"seq\":%ld,\"hits\":[", last);
        unsigned shown = 0;
        for (uint32_t q = first; q <= (uint32_t)last && shown < 300; q++, shown++) {
            const DbgHit *h = &dbg_hits[(q - 1) % DBG_HITS];
            emit("%s[%u,%u,\"0x%08x\",%u,\"0x%08x\",\"0x%08x\",\"0x%08x\",%u,%u,%u]", shown ? "," : "", h->seq,
                 h->slot, h->eip, h->thread, h->value, h->ret, h->frame_ret, h->frame, h->mark_hook, h->mark_seq);
        }
        emit("]}");
        return;
    }
    if (!strcmp(what, "info")) {
        emit("{\"ok\":true,\"active\":%u,\"bpSlots\":%u,\"seq\":%ld,\"slots\":[", dbg_active, dbg_bp_slots, dbg_seq);
        for (uint32_t k = 0; k < 4; k++)
            emit("%s{\"address\":\"0x%08x\",\"length\":%u,\"access\":%u,\"on\":%s,\"scope\":%s}", k ? "," : "",
                 dbg_addr[k], dbg_len[k], dbg_rw[k], dbg_active & (1u << k) ? "true" : "false",
                 dbg_scope[k] ? "true" : "false");
        emit("]}");
        return;
    }
    fail("bad_arguments");
}
/* Every other thread of the process is suspended while code is patched: nothing that allocates or locks
 * runs in between (no emit, no heap). */
static HANDLE probe_threads[PROBE_THREADS];
static uint32_t probe_eips[PROBE_THREADS];
static unsigned probe_thread_count, probe_thread_failures;
static struct { uint32_t page; DWORD old; } probe_pages[PROBE_PAGES];
static unsigned probe_page_count;
static void probe_suspend(void) {
    probe_thread_count = probe_thread_failures = 0;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) return;
    THREADENTRY32 te;
    te.dwSize = sizeof(te);
    DWORD self = GetCurrentThreadId();
    for (BOOL more = Thread32First(snap, &te); more; more = Thread32Next(snap, &te)) {
        if (te.th32OwnerProcessID != pid || te.th32ThreadID == self || probe_thread_count >= PROBE_THREADS) continue;
        HANDLE h = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, te.th32ThreadID);
        if (!h) { probe_thread_failures++; continue; }
        if (SuspendThread(h) == (DWORD)-1) { CloseHandle(h); probe_thread_failures++; continue; }
        CONTEXT context;
        memset(&context, 0, sizeof(context));
        context.ContextFlags = CONTEXT_CONTROL;
        probe_eips[probe_thread_count] = GetThreadContext(h, &context) ? context.Eip : 0;
        probe_threads[probe_thread_count++] = h;
    }
    CloseHandle(snap);
}
static void probe_resume(void) {
    for (unsigned k = 0; k < probe_thread_count; k++) {
        ResumeThread(probe_threads[k]);
        CloseHandle(probe_threads[k]);
    }
    probe_thread_count = 0;
}
static bool probe_writable(uint32_t address, uint32_t length) {
    for (uint32_t page = address & ~0xFFFu; page < address + length; page += 0x1000) {
        bool known = false;
        for (unsigned k = probe_page_count; k-- > 0;)
            if (probe_pages[k].page == page) { known = true; break; }
        if (known) continue;
        if (probe_page_count >= PROBE_PAGES) return false;
        DWORD old;
        if (!VirtualProtect((LPVOID)(uintptr_t)page, 0x1000, PAGE_EXECUTE_READWRITE, &old)) return false;
        probe_pages[probe_page_count].page = page;
        probe_pages[probe_page_count++].old = old;
    }
    return true;
}
static void probe_protect_back(void) {
    for (unsigned k = 0; k < probe_page_count; k++) {
        DWORD old;
        VirtualProtect((LPVOID)(uintptr_t)probe_pages[k].page, 0x1000, probe_pages[k].old, &old);
    }
    probe_page_count = 0;
    FlushInstructionCache(GetCurrentProcess(), NULL, 0);
}
static bool probe_thread_inside(uint32_t start, uint32_t end) {
    for (unsigned k = 0; k < probe_thread_count; k++)
        if (probe_eips[k] > start && probe_eips[k] < end) return true;
    return false;
}
/* Writes code with every other thread suspended, refusing when one of them stands inside the bytes (it would resume
 * in the middle of the new instruction). 0 written, 1 busy, 2 unwritable. Nothing between suspend and resume
 * allocates or emits. */
static int code_write_frozen(uint32_t address, const void *bytes, size_t length) {
    probe_suspend();
    bool busy = probe_thread_inside(address, address + (uint32_t)length) ||
                cov_in_flight(address, (uint32_t)length); /* a coverage hit would write over the new bytes */
    bool written = !busy && code_write(address, bytes, length);
    probe_resume();
    return busy ? 1 : written ? 0 : 2;
}
static void probe_arm(void) {
    unsigned armed = 0, busy = 0, changed = 0, sites = 0, site_changed = 0, unwritable = 0;
    ULONGLONG t0 = GetTickCount64();
    probe_suspend();
    int3_index(); /* before any 0xCC is written: a trap is always found */
    for (uint32_t i = 0; i < probe_used; i++) {
        Probe *p = &probes[i];
        if (p->mode == 2 && (p->state == PROBE_STAGED || p->state == PROBE_DISARMED)) {
            if (probe_thread_inside(p->target, p->target + p->stolen)) { busy++; continue; }
            if (memcmp((const void *)(uintptr_t)p->target, p->original, p->stolen)) { p->state = PROBE_CHANGED; changed++; continue; }
            if (!probe_writable(p->target, 1)) { unwritable++; continue; }
            *(unsigned char *)(uintptr_t)p->target = 0xCC;
            p->state = PROBE_ARMED;
            armed++;
            continue;
        }
        if (p->mode != 0 || (p->state != PROBE_STAGED && p->state != PROBE_DISARMED)) continue;
        if (probe_thread_inside(p->target, p->target + p->stolen)) { busy++; continue; }
        if (memcmp((const void *)(uintptr_t)p->target, p->original, p->stolen)) { p->state = PROBE_CHANGED; changed++; continue; }
        if (!probe_writable(p->target, p->stolen)) { unwritable++; continue; }
        unsigned char *code = (unsigned char *)(uintptr_t)p->target;
        uint32_t rel = addr_of(p->stub) - (p->target + 5);
        for (uint8_t k = 5; k < p->stolen; k++) code[k] = 0x90;
        memcpy(code + 1, &rel, 4);
        code[0] = 0xE9;
        p->state = PROBE_ARMED;
        armed++;
    }
    for (uint32_t k = 0; k < probe_site_used; k++) {
        ProbeSite *s = &probe_sites[k];
        if (s->state != PROBE_STAGED && s->state != PROBE_DISARMED) continue;
        uint32_t *field = (uint32_t *)(uintptr_t)s->address;
        if (*field != s->original) { s->state = PROBE_CHANGED; site_changed++; continue; }
        if (!probe_writable(s->address, 4)) { unwritable++; continue; }
        *field = s->value;
        s->state = PROBE_ARMED;
        if (probes[s->probe].state != PROBE_ARMED) probes[s->probe].state = PROBE_ARMED;
        sites++;
    }
    probe_protect_back();
    unsigned threads = probe_thread_count, failures = probe_thread_failures;
    probe_resume();
    emit("{\"ok\":true,\"armed\":%u,\"sitesArmed\":%u,\"busy\":%u,\"changed\":%u,\"sitesChanged\":%u,\"unwritable\":%u,"
         "\"threadsSuspended\":%u,\"threadsNotSuspended\":%u,\"ms\":%llu}",
         armed, sites, busy, changed, site_changed, unwritable, threads, failures, GetTickCount64() - t0);
}
static void probe_disarm(void) {
    unsigned restored = 0, overwritten = 0, sites = 0;
    ULONGLONG t0 = GetTickCount64();
    probe_suspend();
    for (uint32_t i = 0; i < probe_used; i++) {
        Probe *p = &probes[i];
        if (p->mode == 2 && p->state == PROBE_ARMED) {
            unsigned char *code = (unsigned char *)(uintptr_t)p->target;
            if (*code != 0xCC) { overwritten++; p->state = PROBE_CHANGED; continue; }
            if (!probe_writable(p->target, 1)) continue;
            *code = p->original[0];
            p->state = PROBE_DISARMED;
            restored++;
            continue;
        }
        if (p->mode != 0 || p->state != PROBE_ARMED) continue;
        unsigned char expected[5] = {0xE9};
        uint32_t rel = addr_of(p->stub) - (p->target + 5);
        memcpy(expected + 1, &rel, 4);
        if (memcmp((const void *)(uintptr_t)p->target, expected, 5)) { overwritten++; p->state = PROBE_CHANGED; continue; }
        if (!probe_writable(p->target, p->stolen)) continue;
        memcpy((void *)(uintptr_t)p->target, p->original, p->stolen);
        p->state = PROBE_DISARMED;
        restored++;
    }
    for (uint32_t k = 0; k < probe_site_used; k++) {
        ProbeSite *s = &probe_sites[k];
        if (s->state != PROBE_ARMED) continue;
        uint32_t *field = (uint32_t *)(uintptr_t)s->address;
        if (*field != s->value) { s->state = PROBE_CHANGED; overwritten++; continue; }
        if (!probe_writable(s->address, 4)) continue;
        *field = s->original;
        s->state = PROBE_DISARMED;
        if (probes[s->probe].mode == 1) probes[s->probe].state = PROBE_DISARMED;
        sites++;
    }
    probe_protect_back();
    unsigned threads = probe_thread_count;
    probe_resume();
    emit("{\"ok\":true,\"restored\":%u,\"sitesRestored\":%u,\"overwritten\":%u,\"threadsSuspended\":%u,\"ms\":%llu}",
         restored, sites, overwritten, threads, GetTickCount64() - t0);
}
static bool probe_any_armed(void) {
    for (uint32_t i = 0; i < probe_used; i++)
        if (probes[i].state == PROBE_ARMED) return true;
    return false;
}
static void probe_config(uint32_t samples, uint32_t exits, uint32_t every) {
    probe_limit = samples < probe_samples_per ? samples : probe_samples_per;
    probe_exit_samples = exits;
    probe_mask = every ? every - 1 : 0xFFFFFFFFu;
}
static void *probe_alloc(size_t bytes, DWORD protect) {
    return VirtualAlloc(NULL, bytes, MEM_COMMIT | MEM_RESERVE, protect);
}
static void probe_info(void) {
    unsigned armed = 0, staged = 0, disarmed = 0, changed = 0;
    for (uint32_t i = 0; i < probe_used; i++) {
        uint8_t s = probes[i].state;
        armed += s == PROBE_ARMED;
        staged += s == PROBE_STAGED;
        disarmed += s == PROBE_DISARMED;
        changed += s == PROBE_CHANGED;
    }
    emit("{\"ok\":true,\"capacity\":%u,\"used\":%u,\"armed\":%u,\"staged\":%u,\"disarmed\":%u,\"changed\":%u,"
         "\"sites\":%u,\"counts\":\"0x%08x\",\"meta\":\"0x%08x\",\"samples\":\"0x%08x\",\"callers\":\"0x%08x\","
         "\"code\":\"0x%08x\",\"codeSize\":%u,\"stubSize\":%u,\"samplesPer\":%u,\"words\":%u,\"callerSlots\":%u,"
         "\"limit\":%u,\"mask\":\"0x%08x\",\"exitSamples\":%u,\"action\":%u,\"qpcFrequency\":%lld,"
         "\"int3\":%ld,\"int3Budget\":%u,\"int3Hot\":%ld}",
         probe_capacity, probe_used, armed, staged, disarmed, changed, probe_site_used, addr_of(probe_counts),
         addr_of(probe_meta), addr_of(probe_samples), addr_of(probe_callers), addr_of(probe_code),
         probe_capacity * PROBE_STUB, PROBE_STUB, probe_samples_per, PROBE_WORDS, PROBE_CALLERS, probe_limit, probe_mask,
         probe_exit_samples, probe_action, (long long)qpc_frequency.QuadPart, int3_count, int3_budget, int3_hot);
}
static void op_probe(int argc, char **argv) {
    uint32_t v[4];
    const char *what = argc > 1 ? argv[1] : "";
    if (!strcmp(what, "init")) {
        if (argc != 6 || !parse_u32(argv[2], &v[0]) || !parse_u32(argv[3], &v[1]) || !parse_u32(argv[4], &v[2]) ||
            !parse_u32(argv[5], &v[3]) || !v[0] || v[0] > PROBE_MAX || v[1] > 64 || (v[3] & (v[3] - 1)))
            return fail("bad_arguments");
        if (probe_any_armed()) return fail("probes_armed");
        if (hook_on_probe_code()) return fail("in_use"); /* unhook first (S7 review I5) */
        /* Arrays are reused when they are big enough (nothing is armed, so no stub runs any more); the code area
         * is always new: a thread preempted inside an old stub finishes on the old code. */
        if (!probes || v[0] > probe_capacity || v[1] > probe_samples_per) {
            uint32_t per = v[1] ? v[1] : 1;
            probes = probe_alloc((size_t)v[0] * sizeof(Probe), PAGE_READWRITE);
            probe_counts = probe_alloc((size_t)v[0] * 4, PAGE_READWRITE);
            probe_meta = probe_alloc((size_t)v[0] * 8, PAGE_READWRITE);
            probe_samples = probe_alloc((size_t)v[0] * per * PROBE_WORDS * 4, PAGE_READWRITE);
            probe_callers = probe_alloc((size_t)v[0] * PROBE_CALLERS * 8, PAGE_READWRITE);
            if (!probe_sites) probe_sites = probe_alloc((size_t)PROBE_SITES * sizeof(ProbeSite), PAGE_READWRITE);
            if (!probes || !probe_counts || !probe_meta || !probe_samples || !probe_callers || !probe_sites) {
                probes = NULL;
                return fail("out_of_memory");
            }
            probe_capacity = v[0];
            probe_samples_per = per;
        } else {
            memset((void *)probe_counts, 0, (size_t)probe_capacity * 4);
            memset((void *)probe_meta, 0, (size_t)probe_capacity * 8);
            memset(probe_samples, 0, (size_t)probe_capacity * probe_samples_per * PROBE_WORDS * 4);
            memset((void *)probe_callers, 0, (size_t)probe_capacity * PROBE_CALLERS * 8);
        }
        probe_code = probe_alloc((size_t)probe_capacity * PROBE_STUB, PAGE_EXECUTE_READWRITE);
        if (!probe_code) return fail("out_of_memory");
        if (!int3_sorted) int3_sorted = probe_alloc((size_t)PROBE_MAX * 4, PAGE_READWRITE);
        if (!int3_sorted) return fail("out_of_memory");
        int3_count = 0;
        int3_hot = 0;
        if (shadow_tls == TLS_OUT_OF_INDEXES && (shadow_tls = TlsAlloc()) == TLS_OUT_OF_INDEXES) return fail("no_tls");
        probe_used = probe_site_used = 0;
        probe_config(v[1], v[2], v[3]);
        return probe_info();
    }
    if (!probes) return fail("not_initialized");
    if (!strcmp(what, "add") || !strcmp(what, "site")) {
        if (argc != 3) return fail("bad_arguments");
        long n = unhex(argv[2]);
        if (n < 0 || n % 12) return fail("bad_arguments");
        const unsigned char *r = (const unsigned char *)argv[2];
        uint32_t first = !strcmp(what, "add") ? probe_used : probe_site_used, added = 0;
        for (long k = 0; k < n; k += 12, r += 12) {
            if (!strcmp(what, "add")) {
                if (probe_used >= probe_capacity) return fail("probe_set_full");
                if (!probe_build(probe_used, r)) {
                    emit("{\"ok\":false,\"error\":\"bad_probe\",\"at\":%ld,\"added\":%u}", k / 12, added);
                    return;
                }
                probe_used++;
            } else {
                if (probe_site_used >= PROBE_SITES) return fail("site_table_full");
                ProbeSite *s = &probe_sites[probe_site_used];
                memset(s, 0, sizeof(*s));
                memcpy(&s->probe, r, 4);
                s->kind = r[4];
                memcpy(&s->address, r + 8, 4);
                if (s->probe >= probe_used || probes[s->probe].mode != 1 || (s->kind != 1 && s->kind != 2) ||
                    !read_u32(s->address, &s->original)) {
                    emit("{\"ok\":false,\"error\":\"bad_site\",\"at\":%ld,\"added\":%u}", k / 12, added);
                    return;
                }
                uint32_t stub = addr_of(probes[s->probe].stub), target = probes[s->probe].target;
                if (s->kind == 1 ? s->original != target : s->address + 4 + s->original != target) {
                    emit("{\"ok\":false,\"error\":\"site_does_not_reach_target\",\"at\":%ld,\"added\":%u}", k / 12, added);
                    return;
                }
                s->value = s->kind == 1 ? stub : stub - (s->address + 4);
                s->state = PROBE_STAGED;
                probe_site_used++;
            }
            added++;
        }
        emit("{\"ok\":true,\"first\":%u,\"added\":%u}", first, added);
        return;
    }
    if (!strcmp(what, "arm")) return probe_arm();
    if (!strcmp(what, "disarm")) return probe_disarm();
    if (!strcmp(what, "int3budget")) {
        if (argc != 3 || !parse_u32(argv[2], &v[0]) || !v[0]) return fail("bad_arguments");
        int3_budget = v[0];
        emit("{\"ok\":true,\"int3Budget\":%u}", v[0]);
        return;
    }
    if (!strcmp(what, "tag")) {
        if (argc != 3 || !parse_u32(argv[2], &v[0])) return fail("bad_arguments");
        probe_action = v[0];
        emit("{\"ok\":true,\"action\":%u}", v[0]);
        return;
    }
    if (!strcmp(what, "config")) {
        if (argc != 5 || !parse_u32(argv[2], &v[0]) || !parse_u32(argv[3], &v[1]) || !parse_u32(argv[4], &v[2]) ||
            (v[2] & (v[2] - 1)))
            return fail("bad_arguments");
        probe_config(v[0], v[1], v[2]);
        return probe_info();
    }
    if (!strcmp(what, "clear")) {
        memset((void *)probe_counts, 0, (size_t)probe_capacity * 4);
        memset((void *)probe_meta, 0, (size_t)probe_capacity * 8);
        memset(probe_samples, 0, (size_t)probe_capacity * probe_samples_per * PROBE_WORDS * 4);
        memset((void *)probe_callers, 0, (size_t)probe_capacity * PROBE_CALLERS * 8);
        emit("{\"ok\":true}");
        return;
    }
    if (!strcmp(what, "info")) return probe_info();
    if (!strcmp(what, "list")) {
        if (argc != 4 || !parse_u32(argv[2], &v[0]) || !parse_u32(argv[3], &v[1])) return fail("bad_arguments");
        uint32_t end = v[0] + (v[1] > 8192 ? 8192 : v[1]);
        if (end > probe_used) end = probe_used;
        emit("{\"ok\":true,\"first\":%u,\"probes\":\"", v[0]);
        for (uint32_t i = v[0]; i < end && !response_overflow; i++)
            emit("%08x%02x", probes[i].target, probes[i].state);
        emit("\"}");
        return;
    }
    fail("bad_arguments");
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
#define CONTEXT_NUMBERED 7 /* fault sites with a numbered context file; later ones share the -latest file */
#define CONTEXT_SITES 256  /* fault addresses remembered as survived */
/* At the game's exit message: one small file next to the crash-context files saying how the game was asked to quit
 * (the console's qqq or a script's QuitGame give the console exit message; closing the window or the menu's Quit give
 * the plain one), whether the companion was serving a request then, and the frame. fnv_status / fnv_crash_explain and
 * the bridge's quit log read it to tell who closed the game. */
static void exit_note(unsigned type) {
    char path[MAX_PATH + 64], text[256];
    if (!context_dir[0]) return;
    snprintf(path, sizeof(path), "%s\\exit-%lu.json", context_dir, (unsigned long)pid);
    FILETIME now;
    GetSystemTimeAsFileTime(&now);
    int n = snprintf(text, sizeof(text),
                     "{\"schema\":1,\"pid\":%lu,\"how\":\"%s\",\"fileTime\":\"%llu\",\"frame\":%ld,"
                     "\"servingRequest\":%s,\"thread\":%lu,\"mainThread\":%lu}\n",
                     (unsigned long)pid, type == MSG_EXIT_CONSOLE ? "console" : "window_or_menu",
                     ((ULONGLONG)now.dwHighDateTime << 32) | now.dwLowDateTime, frame,
                     serving_request ? "true" : "false", (unsigned long)GetCurrentThreadId(), (unsigned long)main_thread);
    HANDLE f = CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return;
    DWORD written;
    WriteFile(f, text, n > 0 ? (DWORD)n : 0, &written, NULL);
    CloseHandle(f);
}
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
static LONG stop_fault(EXCEPTION_POINTERS *e); /* w2_stop.c: faulthold, the crash hold (8.3) */
static bool guard_on_this_thread(void); /* w1_guard.c */
static LONG CALLBACK on_exception(EXCEPTION_POINTERS *e) {
    DWORD code = e->ExceptionRecord->ExceptionCode;
    if (guard_on_this_thread()) return EXCEPTION_CONTINUE_SEARCH; /* a guarded call: handled, not a crash */
    if (code != EXCEPTION_ACCESS_VIOLATION && code != EXCEPTION_ILLEGAL_INSTRUCTION && code != EXCEPTION_INT_DIVIDE_BY_ZERO &&
        code != EXCEPTION_STACK_OVERFLOW && code != EXCEPTION_PRIV_INSTRUCTION && code != EXCEPTION_ARRAY_BOUNDS_EXCEEDED)
        return EXCEPTION_CONTINUE_SEARCH;
    /* After xNVSE's exit messages the game is shutting down; plugins fault in their exit cleanup there
     * (seen with the companion off too), which is not worth a context file. */
    if (!context_dir[0] || code == EXCEPTION_STACK_OVERFLOW || exiting) return EXCEPTION_CONTINUE_SEARCH;
    static volatile LONG busy;
    if (InterlockedExchange(&busy, 1)) return EXCEPTION_CONTINUE_SEARCH;
    /* Faults the game recovers from must not use up the files (H4). A fault at an address that faulted before was
     * survived the first time (the game runs on), so it gets no new file. The first CONTEXT_NUMBERED fault sites keep
     * their own file; every later new site overwrites crash-context-<pid>-latest.json, so a real crash late in a
     * session always leaves its context. */
    static uint32_t sites[CONTEXT_SITES];
    static unsigned site_count;
    uint32_t site = (uint32_t)(uintptr_t)e->ExceptionRecord->ExceptionAddress;
    bool seen = site_count >= CONTEXT_SITES;
    for (unsigned i = 0; i < site_count && !seen; i++) seen = sites[i] == site;
    if (seen) {
        InterlockedExchange(&busy, 0);
        return stop_fault(e);
    }
    sites[site_count++] = site;
    LONG n = InterlockedIncrement(&context_files);
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
        if (f->probe) {
            ctx("\"probe\",\"probeIndex\":%u,\"caller\":\"0x%08x\"", f->probe - 1, f->ret);
            ctx_form("this", f->ecx);
            ctx("}");
            continue;
        }
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
    if (n <= CONTEXT_NUMBERED)
        snprintf(path, sizeof(path), "%s\\crash-context-%lu-%ld.json", context_dir, (unsigned long)pid, n);
    else
        snprintf(path, sizeof(path), "%s\\crash-context-%lu-latest.json", context_dir, (unsigned long)pid);
    HANDLE file = CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file != INVALID_HANDLE_VALUE) {
        DWORD written;
        WriteFile(file, context_buffer, (DWORD)context_len, &written, NULL);
        CloseHandle(file);
    }
    InterlockedExchange(&busy, 0);
    return stop_fault(e); /* CONTINUE_SEARCH unless faulthold parked the thread and the resume recovered it */
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
    /* Into code (fnv_patch, fnv_mem_write and their undos): never over a live int3, a hook's or a probe's bytes,
     * and the change is kept as a code site so no int3 or coverage byte lands inside it later (round-4 review I2). */
    MEMORY_BASIC_INFORMATION info;
    bool code = VirtualQuery((LPCVOID)(uintptr_t)address, &info, sizeof(info)) && info.State == MEM_COMMIT &&
                (info.Protect & (PAGE_EXECUTE | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY));
    if (code && (isite_in(address, (uint32_t)length) || hook_range(address, (uint32_t)length) ||
                 probe_range(address, (uint32_t)length)))
        return fail("in_use");
    unsigned char *old = malloc((size_t)length);
    if (!old) return fail("out_of_memory");
    if (!read_bytes(address, old, (size_t)length)) fail("unreadable");
    else if (!write_bytes(address, argv[2], (size_t)length)) fail("unwritable");
    else {
        if (code) code_site_note(address, (uint32_t)length, old, (const unsigned char *)argv[2]);
        emit("{\"ok\":true,\"old\":"); emit_hex(old, (size_t)length); emit("}");
    }
    free(old);
}
static void call_guarded(uint32_t target, bool is_float, uint32_t ecx, uint32_t edx, const uint32_t *words, uint32_t count);
/* call <target> <ret i|f|v> <ecx> <edx> [word...]: the bridge maps the calling convention to ecx/edx
 * and the stack words (floats and doubles arrive as their bit patterns). The two-letter kinds ig, fg, vg make
 * the call guarded (w1_guard.c): a fault comes back as a result instead of a crash. */
static void op_call(int argc, char **argv) {
    uint32_t target, ecx, edx, words[32];
    if (argc < 5 || argc > 37) return fail("bad_arguments"); /* before argv[2] is read */
    bool guarded = strlen(argv[2]) == 2 && argv[2][1] == 'g';
    if (argc < 5 || argc > 37 || !parse_u32(argv[1], &target) || (!guarded && target < 0x10000) ||
        (strlen(argv[2]) != 1 && !guarded) || !strchr("ifv", argv[2][0]) || !parse_u32(argv[3], &ecx) ||
        !parse_u32(argv[4], &edx))
        return fail("bad_arguments");
    for (int i = 5; i < argc; i++)
        if (!parse_u32(argv[i], &words[i - 5])) return fail("bad_arguments");
    if (guarded) return call_guarded(target, argv[2][0] == 'f', ecx, edx, words, (uint32_t)(argc - 5));
    MEMORY_BASIC_INFORMATION info;
    if (!VirtualQuery((LPCVOID)(uintptr_t)target, &info, sizeof(info)) || info.State != MEM_COMMIT ||
        !(info.Protect & (PAGE_EXECUTE | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)))
        return fail("not_executable");
    CallResult result = {0, 0, NAN};
    LONG before = script_errors;
    fnvmcp_invoke(target, words, (uint32_t)(argc - 5), ecx, edx, argv[2][0] == 'f', &result);
    emit("{\"ok\":true,\"eax\":\"0x%08x\",\"edx\":\"0x%08x\"", result.eax, result.edx);
    if (argv[2][0] == 'f') emit(isfinite(result.st0) ? ",\"st0\":%.17g" : ",\"st0\":null", result.st0);
    emit(",\"scriptErrors\":%ld}", script_errors - before);
}
/* The game exe's image range, from its own PE header (no hard-coded addresses). */
static bool exe_range(uint32_t *start, uint32_t *end) {
    static uint32_t s, e;
    if (!s) {
        const unsigned char *base = (const unsigned char *)GetModuleHandleA(NULL);
        if (!base) return false;
        const IMAGE_NT_HEADERS32 *nt = (const IMAGE_NT_HEADERS32 *)(base + ((const IMAGE_DOS_HEADER *)base)->e_lfanew);
        s = (uint32_t)(uintptr_t)base;
        e = s + nt->OptionalHeader.SizeOfImage;
    }
    *start = s;
    *end = e;
    return true;
}
/* A ref argument (0 = none) must be a live object: readable, with a vtable inside the game exe. A form id
 * passed where a pointer belongs would otherwise crash the script runner. */
static void *ref_argument(int argc, char **argv, int index, bool *ok) {
    uint32_t ref = 0;
    *ok = argc <= index || parse_u32(argv[index], &ref);
    return (void *)(uintptr_t)ref;
}
static bool ref_is_object(void *p) {
    uint32_t ref = (uint32_t)(uintptr_t)p, vt = 0, lo = 0, hi = 0;
    return !ref || (read_u32(ref, &vt) && exe_range(&lo, &hi) && vt >= lo && vt < hi);
}
static void op_console(int argc, char **argv) {
    bool ok;
    void *ref = ref_argument(argc, argv, 2, &ok);
    if (argc < 2 || argc > 3 || !ok || unhex(argv[1]) <= 0 || strlen(argv[1]) > 511 || strchr(argv[1], '\n'))
        return fail("bad_arguments");
    if (!ref_is_object(ref)) return fail("bad_ref");
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
    if (!ref_is_object(ref)) return fail("bad_ref");
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
#include "w1_pred.c"
#include "w1_gate.c"
#include "w1_samp.c"
#include "w1_sink.c"
#include "w1_guard.c"
#include "w1_input.c"
#include "w2_stop.c"
#include "w2_bp.c"
#include "w5_mod.c"
#include "w5_code.c"
#include "w6_cov.c"

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
    else if (!strcmp(argv[0], "hookact")) op_hookact(argc, argv);
    else if (!strcmp(argv[0], "probe")) op_probe(argc, argv);
    else if (!strcmp(argv[0], "dbg")) op_dbg(argc, argv);
    else if (!strcmp(argv[0], "pred")) op_pred(argc, argv);
    else if (!strcmp(argv[0], "gatecfg")) op_gatecfg(argc, argv);
    else if (!strcmp(argv[0], "hold")) op_hold(argc, argv);
    else if (!strcmp(argv[0], "step")) op_step(argc, argv);
    else if (!strcmp(argv[0], "until")) op_until(argc, argv);
    else if (!strcmp(argv[0], "gate")) op_gate(argc, argv);
    else if (!strcmp(argv[0], "samp")) op_samp(argc, argv);
    else if (!strcmp(argv[0], "sink")) op_sink(argc, argv);
    else if (!strcmp(argv[0], "inpcfg")) op_inpcfg(argc, argv);
    else if (!strcmp(argv[0], "inp")) op_inp(argc, argv);
    else if (!strcmp(argv[0], "stops")) op_stops(argc, argv);
    else if (!strcmp(argv[0], "resume")) op_resume(argc, argv);
    else if (!strcmp(argv[0], "faulthold")) op_faulthold(argc, argv);
    else if (!strcmp(argv[0], "bp")) op_bp(argc, argv);
    else if (!strcmp(argv[0], "trace")) op_trace(argc, argv);
    else if (!strcmp(argv[0], "mod")) op_mod(argc, argv);
    else if (!strcmp(argv[0], "hooknative")) op_hooknative(argc, argv);
    else if (!strcmp(argv[0], "code")) op_code(argc, argv);
    else if (!strcmp(argv[0], "cov")) op_cov(argc, argv);
    else fail("unknown_op");
}
/* A request runs on the main thread. step and until can stop it in the middle: the ops after them are
 * served later, at the stop frame (serve_ops resumes there). The header's frame is written last. */
static size_t frame_field; /* where the header's frame number sits in the response */
/* Serves the ops of the current request from `ops` on; returns early when an op suspends the request. */
static void serve_ops(char *ops, bool first) {
    InterlockedExchange(&serving_request, 1);
    while (ops && *ops) {
        char *next = strchr(ops, '\t');
        if (next) *next++ = 0;
        if (!first) emit(",");
        first = false;
        serve_op(ops);
        if (gate_suspending) {
            gate_suspending = false;
            suspend_next = next;
            InterlockedExchange(&serving_request, 0);
            return;
        }
        ops = next;
    }
    emit("]}\n");
    if (response_overflow) {
        response_len = 0;
        response_overflow = false;
        emit("{\"id\":");
        emit_text(request, strlen(request));
        emit(",\"frame\":%ld,\"error\":\"response_too_large\"}\n", frame);
    } else {
        char number[16];
        snprintf(number, sizeof(number), "%-10ld", frame);
        memcpy(response + frame_field, number, 10);
    }
    if (gate_want_hold) { /* hold on: the gate closes at the end of the request */
        gate_want_hold = false;
        gate_close();
    }
    InterlockedExchange(&serving_request, 0);
    InterlockedExchange(&pending, 0);
    SetEvent(done_event);
}
/* Starts serving one request line; the pipe thread waits for done_event. */
static void serve_request(char *line) {
    last_contact = GetTickCount64(); /* a served request is contact (K2) */
    InterlockedExchange(&request_dropped, 0);
    char *ops = strchr(line, '\t');
    if (ops) *ops++ = 0;
    if (strlen(line) > 64) line[64] = 0;
    response_len = 0;
    response_overflow = false;
    emit("{\"id\":");
    emit_text(line, strlen(line));
    emit(",\"frame\":");
    frame_field = response_len;
    emit("          ");
    emit(",\"results\":[");
    serve_ops(ops, true);
}

/* ------------------------------------------------------------------------------------ pipe */
/* Appends one line to %LOCALAPPDATA%\fnvmcp\companion.log: what the companion cannot report over its pipe. */
static void companion_log(const char *text) {
    if (!context_dir[0]) return;
    char path[MAX_PATH + 32], line[512];
    SYSTEMTIME t;
    GetLocalTime(&t);
    snprintf(path, sizeof(path), "%s\\companion.log", context_dir);
    int n = snprintf(line, sizeof(line), "%04u-%02u-%02u %02u:%02u:%02u pid %lu: %s\r\n", t.wYear, t.wMonth, t.wDay,
                     t.wHour, t.wMinute, t.wSecond, (unsigned long)pid, text);
    HANDLE f = CreateFileA(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    if (f == INVALID_HANDLE_VALUE) return;
    DWORD written;
    WriteFile(f, line, n > 0 ? (DWORD)(n < (int)sizeof(line) ? n : (int)sizeof(line) - 1) : 0, &written, NULL);
    CloseHandle(f);
}
/* The pipe could not be created (another game holds the name): logged once, then at most once a minute (H4). */
static unsigned pipe_failures;
static void pipe_create_failed(DWORD error) {
    static ULONGLONG logged_at;
    static bool logged;
    ULONGLONG now = GetTickCount64();
    pipe_failures++;
    if (logged && now - logged_at < 60000) return;
    logged = true;
    logged_at = now;
    char text[300];
    snprintf(text, sizeof(text), "cannot create the pipe %s (Windows error %lu, %u tries); is another game with the "
             "companion running?", pipe_name, (unsigned long)error, pipe_failures);
    companion_log(text);
    record_event(EVENT_PIPE, text);
}
static void pipe_created(void) {
    if (!pipe_failures) return;
    char text[200];
    snprintf(text, sizeof(text), "pipe %s created after %u failed tries", pipe_name, pipe_failures);
    companion_log(text);
    record_event(EVENT_PIPE, text);
    pipe_failures = 0;
}
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
    char text[768];
    int live_hooks = 0; /* removed hooks keep their slot until the table fills: count only the armed ones */
    for (int i = 0; i < hook_count; i++)
        if (hooks[i] && hooks[i]->stolen) live_hooks++;
    for (char *c = id; *c; c++)
        if (!((*c >= '0' && *c <= '9') || (*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z') || *c == '-')) *c = '_';
    ULONGLONG now = GetTickCount64(), last = last_frame_tick;
    snprintf(text, sizeof(text),
             "{\"id\":\"%.64s\",\"pong\":true,\"pid\":%lu,\"started\":\"%llu\",\"companion\":%d,\"wire\":%d,\"frame\":%ld,"
             "\"msSinceFrame\":%lld,\"loaded\":%s,\"deferredInit\":%s,\"lastMessage\":\"%s\",\"busy\":%s,"
             "\"mainThreadId\":%lu,\"pipeThread\":%lu,\"hooks\":%d,\"held\":%s,\"stepLeft\":%ld,\"parked\":%u,"
             "\"lastFrameTick\":%llu}\n",
             id, (unsigned long)pid, started, COMPANION_VERSION, WIRE_VERSION, frame, last ? (long long)(now - last) : -1LL,
             loaded ? "true" : "false", deferred_init ? "true" : "false", message_name(last_message),
             pending ? "true" : "false", (unsigned long)main_thread, (unsigned long)GetCurrentThreadId(), live_hooks,
             gate_held ? "true" : "false", (long)step_left, stop_parked(), (unsigned long long)last);
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
            if (!request_dropped) last_contact = GetTickCount64(); /* not while the main thread is stuck (K2) */
            *tab = 0;
            ping(pipe, buffer);
        } else {
            /* A request the last client left behind may still be running (H4): it ends first, and its done_event
             * must not answer this one. */
            while (InterlockedCompareExchange(&pending, 0, 0) != 0) {
                if (client_gone(pipe)) return;
                Sleep(5);
            }
            ResetEvent(done_event);
            memcpy(request, buffer, line_length + 1);
            InterlockedExchange(&pending, 1);
            SetEvent(request_event);
            for (;;) {
                if (WaitForSingleObject(done_event, 250) == WAIT_OBJECT_0) break;
                if (client_gone(pipe)) {
                    if (InterlockedCompareExchange(&pending, 0, 1) == 1)
                        InterlockedExchange(&request_dropped, 1); /* never served: pings no longer count (K2) */
                    /* else the main thread took it: it finishes without a reader, or gate_let_go ends a step or until
                     * that is still running. Either way the pipe is free for the next client now (H4). */
                    return;
                }
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
        if (pipe == INVALID_HANDLE_VALUE) {
            pipe_create_failed(GetLastError());
            Sleep(1000);
            continue;
        }
        pipe_created();
        bool served = false;
        if (ConnectNamedPipe(pipe, NULL) || GetLastError() == ERROR_PIPE_CONNECTED) {
            serve_client(pipe);
            served = true;
        }
        FlushFileBuffers(pipe);
        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
        if (served) gate_client_gone(); /* nothing the bridge held outlives it (H4) */
    }
}

/* ------------------------------------------------------------------------------------ xNVSE */
/* Runs once per frame on the main thread. A step or until counts this frame; then a request waiting for the
 * main thread is served; then, if the gate is held, the thread parks here until the gate opens. */
static void main_loop(void) {
    if (!main_thread) main_thread = GetCurrentThreadId();
    InterlockedIncrement(&frame);
    last_frame_tick = GetTickCount64();
    if (deferred_init) route_console(); /* script errors reach the log even between requests */
    samp_tick();
    mod_frame_tick();
    if (suspend_kind != SUSP_NONE) gate_frame_tick();
    run_due(false);
    run_hook_scripts();
    if (scripts && scripts->CallFunction) run_periodic();
    SEAM(6); /* tests: the main thread is busy elsewhere and takes no request (K2) */
    gate_let_go();
    if (InterlockedCompareExchange(&pending, 2, 1) == 1) serve_request(request);
    gate_park();
}

static void on_message(Message *m) {
    int type = (int)m->type;
    sink_message(m->type, m); /* subscribed types are recorded here, including the per-form ones below */
    if (type == MSG_LOOP) {
        main_loop();
        return;
    }
    /* Newer xNVSE messages (form load/unload, 3D set, cell state...) are only recorded by sinks. */
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
        exit_note(type);
        gate_exit();
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
    nvse_iface = nvse;
    runtime_version = nvse->runtimeVersion;
    scripts = nvse->QueryInterface(6);
    console = nvse->QueryInterface(1);
    arrays = nvse->QueryInterface(5);
    event_mgr = nvse->QueryInterface(8); /* kInterface_EventManager */
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
    AddVectoredExceptionHandler(1, on_trap); /* int3 probes and watchpoints are handled, not logged */
    AddVectoredExceptionHandler(1, on_guard); /* added last, so it runs first: guarded calls never reach the logs */
    FILETIME creation, exit_time, kernel, user;
    if (!GetProcessTimes(GetCurrentProcess(), &creation, &exit_time, &kernel, &user)) return false;
    pid = GetCurrentProcessId();
    started = ((ULONGLONG)creation.dwHighDateTime << 32) | creation.dwLowDateTime;
    request = malloc(REQUEST_MAX + 1);
    response = malloc(RESPONSE_MAX);
    done_event = CreateEventA(NULL, FALSE, FALSE, NULL);
    if (!request || !response || !done_event || !gate_init()) return false;
    if (!messages->RegisterListener(nvse->GetPluginHandle(), "NVSE", on_message)) return false;
    HANDLE thread = CreateThread(NULL, 0, pipe_thread, NULL, 0, NULL);
    if (!thread) return false;
    CloseHandle(thread);
    return true;
}
