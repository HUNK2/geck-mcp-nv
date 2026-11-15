/* Tests the real engine companion DLL against a synthetic xNVSE host, never FNV.
 * Usage: fnvmcp_fake_host.exe <dll> <seconds>
 * Prints one JSON line with the addresses of its test targets, then pumps MainGameLoop messages
 * every 5 ms. Set FNVMCP_PIPE and FNVMCP_CONSOLE_LOG so a test never touches the real pipe or log.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../runtime/nvse_abi.h"

typedef bool (*QueryFn)(const NVSEInterface *, PluginInfo *);
typedef bool (*LoadFn)(const NVSEInterface *);

static void (*listener)(Message *);
static char log_path[MAX_PATH];
static unsigned char memory[64] = "fnvmcp fake host buffer";
static struct { void *vtable; uint8_t type, pad[3]; uint32_t flags, id; } fake_form = {0, 0x2A, {0}, 0, 0x0000ABCD};
static uint32_t chain_c = 0x11223344, *chain_b = &chain_c, **chain_a = &chain_b;

static int __cdecl add3(int a, int b, int c) { return a + b + c; }
static int __stdcall sub2(int a, int b) { return a - b; }
static int __attribute__((thiscall)) member(int *self, int k) { return *self * k; }
static int __fastcall fast(int a, int b, int c) { return a * 100 + b * 10 + c; }
static float __cdecl halfsum(float a, float b) { return (a + b) / 2; }
static double __stdcall scale(double v, int k) { return v * k; }

/* The clock counter's disable and enable (thiscall, like BSTimer_Disable/Enable): they count their calls so
 * tests can check the gate keeps the pause balanced. timer_global stands for TimeGlobal. */
static volatile uint32_t timer_disables, timer_enables, timer_global;
static void __attribute__((thiscall)) fake_timer_disable(void *self) { (void)self; timer_disables++; }
static void __attribute__((thiscall)) fake_timer_enable(void *self) { (void)self; timer_enables++; }

/* Input injector targets. fake_poll stands for InputPoller::Controls_UpdateValues: a 5-byte prologue
 * (55 8b ec 53 56), a counter, and a plain ret (no stack arguments). It is run only by the "poll N" console line,
 * so a test decides exactly how many polls happen. fake_os is an OSInputGlobals block; fake_force stands for
 * OSInputGlobals::Controls_ForceUserAction (thiscall, one int argument). faulter reads the address in its first
 * argument; ud2fn is an illegal instruction. */
volatile uint32_t poll_count; /* not static: the assembly below increments it */
static volatile uint32_t force_count, last_force;
static unsigned char fake_os[0x2000];
static uint32_t fake_os_ptr;
extern void fake_poll(void);
extern int faulter(void), ud2fn(void);
/* Stop targets (tests of the hook and crash holds). hookme is a cdecl (a) -> a + 1000 with the prologue
 * 55 89 e5 83 ec 08 (6 bytes, no relative jump), so a hook moves exactly those bytes. The console lines "hookme <n>",
 * "hookthread <n>", "crash" and "crashthread" run the target on the main thread (through the scheduler, so the call is
 * not inside a served request) or on a second thread. The results are in the JSON header fields. */
extern int hookme(void);
/* Breakpoint and trace target (contract 8.2, 8.4): bp_target(a, b) = bp_callee() + b, with the layout of the asm below.
 * bp_call_site, bp_after_site and bp_ret_site are the labels of the call, the instruction after it and the ret. */
extern int bp_target(int a, int b), bp_callee(void);
extern char bp_call_site[], bp_after_site[], bp_ret_site[];
static volatile uint32_t hook_last, hook_thread_result, hook_thread_done, crash_done, crash_thread_done;
static volatile uint32_t bp_result;
static volatile LONG crash_expected;
/* Race workers (PLAN-3 S1.1): "race <threads> <iters>" starts threads that wait for race_go, then each calls
 * bp_target <iters> times (every call returns 12). The test sets race_go (and the seam) in this process's memory,
 * so it works while the main thread is parked in a seam. */
static volatile LONG race_go, race_started, race_done, race_bad, race_yield;
static void __attribute__((thiscall)) fake_force(void *self, int control) {
    (void)self;
    force_count++;
    last_force = (uint32_t)control;
}

/* Event manager: SetNativeEventHandler keeps (name, function) pairs; "raise_event <name>" calls them. The
 * name "missing" is refused, as an undefined event would be. Layout matches NVSEEventManagerInterface. */
static struct { char name[48]; void (*fn)(void *, void *); } fake_handlers[8];
static uint32_t event_params[6] = {1, 2, 3, 4, 5, 6};
static bool set_event(const char *name, void (*fn)(void *, void *)) {
    if (!strcmp(name, "missing")) return false;
    for (int i = 0; i < 8; i++)
        if (!fake_handlers[i].fn) {
            snprintf(fake_handlers[i].name, sizeof(fake_handlers[i].name), "%s", name);
            fake_handlers[i].fn = fn;
            return true;
        }
    return false;
}
static bool remove_event(const char *name, void (*fn)(void *, void *)) {
    for (int i = 0; i < 8; i++)
        if (fake_handlers[i].fn == fn && !strcmp(fake_handlers[i].name, name)) {
            fake_handlers[i].fn = NULL;
            return true;
        }
    return false;
}
static struct { void *register_event, *dispatch, *dispatch_alt; bool (*set)(const char *, void (*)(void *, void *));
                bool (*remove)(const char *, void (*)(void *, void *)); } fake_em = {NULL, NULL, NULL, set_event, remove_event};

/* Hook targets with MSVC-style prologues (what the bridge's prologue decoder must handle):
 *   msvc_this  thiscall (this, a, b) -> *this + a*b       55 8b ec 83 ec 08
 *   msvc_seh   cdecl (a) -> a + 1                          55 8b ec 6a ff 68 imm32
 *   prehooked  cdecl (a) -> 3a, starts with a jmp into "another plugin" (rel32 to relocate)
 *   msvc_float cdecl (float a) -> a in st0                 55 8b ec d9 45 08 */
extern int msvc_this(void), msvc_seh(void), prehooked(void), msvc_float(void);
/* Probe targets: tiny is a thiscall getter too short for an entry jump (8b 01 c3); site_caller reaches it
 * with a direct call (site_call is that call instruction), vt_caller through slot 1 of tiny_vtable (.rdata). */
extern int tiny(void), site_caller(void), site_call(void), vt_caller(void), tiny_vtable(void);
/* int3 and watchpoint targets: tiny2 (8b 41 04 c3) is reached only through a pointer (ptr_caller), writer
 * stores its argument in `watched` (a data breakpoint on it names writer). */
extern int tiny2(void), ptr_caller(void), writer(void);
volatile uint32_t watched;
__asm__(
    ".text\n"
    ".globl _msvc_this\n"
    "_msvc_this:\n"
    "  pushl %ebp\n"
    "  movl %esp, %ebp\n"
    "  subl $8, %esp\n"
    "  movl (%ecx), %eax\n"
    "  movl 8(%ebp), %edx\n"
    "  imull 12(%ebp), %edx\n"
    "  addl %edx, %eax\n"
    "  movl %ebp, %esp\n"
    "  popl %ebp\n"
    "  ret $8\n"
    ".globl _msvc_seh\n"
    "_msvc_seh:\n"
    "  pushl %ebp\n"
    "  movl %esp, %ebp\n"
    "  pushl $-1\n"
    "  pushl $0x12345678\n"
    "  addl $8, %esp\n"
    "  movl 8(%ebp), %eax\n"
    "  incl %eax\n"
    "  popl %ebp\n"
    "  ret\n"
    ".globl _prehooked\n"
    "_prehooked:\n"
    "  .byte 0xe9\n"
    "  .long other_plugin - (. + 4)\n"
    "  nop\n"
    "  nop\n"
    "  nop\n"
    "  ret\n"
    "other_plugin:\n"
    "  movl 4(%esp), %eax\n"
    "  imull $3, %eax, %eax\n"
    "  ret\n"
    ".globl _msvc_float\n"
    "_msvc_float:\n"
    "  pushl %ebp\n"
    "  movl %esp, %ebp\n"
    "  flds 8(%ebp)\n"
    "  popl %ebp\n"
    "  ret\n"
    ".globl _tiny\n"
    "_tiny:\n"
    "  movl (%ecx), %eax\n"
    "  ret\n"
    ".globl _site_caller\n"
    "_site_caller:\n"
    "  movl 4(%esp), %ecx\n"
    ".globl _site_call\n"
    "_site_call:\n"
    "  call _tiny\n"
    "  ret\n"
    ".globl _vt_caller\n"
    "_vt_caller:\n"
    "  movl 4(%esp), %ecx\n"
    "  movl $_tiny_vtable, %eax\n"
    "  call *4(%eax)\n"
    "  ret\n"
    ".section .rdata,\"dr\"\n"
    ".globl _tiny_vtable\n"
    "_tiny_vtable:\n"
    "  .long _msvc_seh\n"
    "  .long _tiny\n"
    ".text\n"
    ".globl _tiny2\n"
    "_tiny2:\n"
    "  movl 4(%ecx), %eax\n"
    "  ret\n"
    ".globl _ptr_caller\n"
    "_ptr_caller:\n"
    "  movl 4(%esp), %ecx\n"
    "  movl $_tiny2, %eax\n"
    "  call *%eax\n"
    "  ret\n"
    ".globl _writer\n"
    "_writer:\n"
    "  movl 4(%esp), %eax\n"
    "  movl %eax, _watched\n"
    "  ret\n"
    ".globl _fake_poll\n"
    "_fake_poll:\n"
    "  pushl %ebp\n"
    "  movl %esp, %ebp\n"
    "  pushl %ebx\n"
    "  pushl %esi\n"
    "  incl _poll_count\n"
    "  popl %esi\n"
    "  popl %ebx\n"
    "  popl %ebp\n"
    "  ret\n"
    ".globl _faulter\n"
    "_faulter:\n"
    "  movl 4(%esp), %eax\n"
    "  movl (%eax), %eax\n"
    "  ret\n"
    ".globl _ud2fn\n"
    "_ud2fn:\n"
    "  ud2\n"
    "  ret\n"
    ".globl _hookme\n"
    "_hookme:\n"
    "  pushl %ebp\n"
    "  movl %esp, %ebp\n"
    "  subl $8, %esp\n"
    "  movl 8(%ebp), %eax\n"
    "  addl $1000, %eax\n"
    "  movl %ebp, %esp\n"
    "  popl %ebp\n"
    "  ret\n"
    ".globl _bp_callee\n"
    "_bp_callee:\n"
    "  movl $7, %eax\n"
    "  ret\n"
    ".globl _bp_target\n"
    "_bp_target:\n"
    "  pushl %ebp\n"
    "  movl %esp, %ebp\n"
    "  movl 8(%ebp), %eax\n"
    ".globl _bp_call_site\n"
    "_bp_call_site:\n"
    "  call _bp_callee\n"
    ".globl _bp_after_site\n"
    "_bp_after_site:\n"
    "  addl 12(%ebp), %eax\n"
    "  popl %ebp\n"
    ".globl _bp_ret_site\n"
    "_bp_ret_site:\n"
    "  ret\n");

static void send(uint32_t type, void *data) {
    Message m = {"NVSE", type, 0, data};
    listener(&m);
}
static void send_len(uint32_t type, void *data, uint32_t length) {
    Message m = {"NVSE", type, length, data};
    listener(&m);
}
static void append_log(const char *text) {
    if (!log_path[0]) return;
    FILE *f = fopen(log_path, "ab");
    if (f) { fprintf(f, "%s\r\n", text); fclose(f); }
}
static int call_hookme(int n) { return ((int (*)(int))(void *)hookme)(n); }
static int call_bp_target(int a) { return ((int (*)(int, int))(void *)bp_target)(a, 5); }
static int call_faulter(int n) { return ((int (*)(int))(void *)faulter)(n); }
static DWORD WINAPI hook_worker(LPVOID arg) {
    uint32_t result = (uint32_t)call_hookme((int)(intptr_t)arg);
    hook_thread_result = result;
    hook_thread_done = 1;
    char out[64];
    snprintf(out, sizeof(out), "hookthread %u", (unsigned)result);
    append_log(out);
    return 0;
}
static DWORD WINAPI race_worker(LPVOID arg) {
    int iters = (int)(intptr_t)arg;
    InterlockedIncrement(&race_started);
    while (!race_go) SwitchToThread();
    for (int i = 0; i < iters; i++) {
        if (call_bp_target(i) != 12) InterlockedIncrement(&race_bad);
        if (race_yield) SwitchToThread(); /* leaves gaps in which the prologue is free for a hook or unhook */
    }
    InterlockedIncrement(&race_done);
    return 0;
}
static DWORD WINAPI crash_worker(LPVOID arg) {
    (void)arg;
    crash_expected = 1;
    crash_thread_done = (uint32_t)call_faulter(0) + 1; /* 1 when the call returned 0 */
    return 0;
}
/* Recovery for a fault nobody handles: the process ends with code 77 (a crash that is expected, not a hang). Added
 * after the companion's handlers, so it runs only when the companion lets the exception continue. */
static volatile LONG handled_expected; /* "handled <n>": the host recovers from its own ud2 faults, like the game */
static LONG CALLBACK last_chance(EXCEPTION_POINTERS *e) {
    if (handled_expected && e->ExceptionRecord->ExceptionCode == EXCEPTION_ILLEGAL_INSTRUCTION) {
        e->ContextRecord->Eip += 2; /* over the ud2 of ud2fn, to its ret */
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    if (crash_expected) TerminateProcess(GetCurrentProcess(), 77);
    return EXCEPTION_CONTINUE_SEARCH;
}
static bool run_line(const char *line, void *ref) {
    (void)ref;
    if (!strncmp(line, "scof \"", 6)) {
        snprintf(log_path, sizeof(log_path), "%s", line + 6);
        char *quote = strrchr(log_path, '"');
        if (quote) *quote = 0;
        return true;
    }
    if (!strcmp(line, "reject")) return false;
    /* Test hooks (answered on the main thread, like every console line): "raise <type> <dataLen>" sends an xNVSE
     * message whose data is fake_form; "raise_event <name>" calls the handlers set under that name; "poll <n>" runs
     * the fake input poll n times. */
    if (!strncmp(line, "raise ", 6)) {
        unsigned type = 0, length = 0;
        if (sscanf(line + 6, "%u %u", &type, &length) == 2) send_len(type, &fake_form, length);
        return true;
    }
    if (!strncmp(line, "raise_event ", 12)) {
        for (int i = 0; i < 8; i++)
            if (fake_handlers[i].fn && !strcmp(fake_handlers[i].name, line + 12)) fake_handlers[i].fn(&fake_form, event_params);
        return true;
    }
    if (!strncmp(line, "poll ", 5)) {
        unsigned count = (unsigned)atoi(line + 5);
        for (unsigned i = 0; i < count; i++) fake_poll();
        return true;
    }
    if (!strncmp(line, "hookme ", 7)) {
        hook_last = (uint32_t)call_hookme(atoi(line + 7));
        char out[64];
        snprintf(out, sizeof(out), "hookme %u", (unsigned)hook_last);
        append_log(out);
        return true;
    }
    if (!strncmp(line, "hookthread ", 11)) {
        hook_thread_done = 0;
        HANDLE thread = CreateThread(NULL, 0, hook_worker, (LPVOID)(intptr_t)atoi(line + 11), 0, NULL);
        if (thread) CloseHandle(thread);
        return true;
    }
    /* bpcall <n>: runs bp_target(n, 5) on the main thread and logs its result. */
    if (!strncmp(line, "bpcall ", 7)) {
        bp_result = (uint32_t)call_bp_target(atoi(line + 7));
        char out[64];
        snprintf(out, sizeof(out), "bpcall %u", (unsigned)bp_result);
        append_log(out);
        return true;
    }
    if (!strncmp(line, "race ", 5)) {
        int threads = 0, iters = 0, yield = 0; /* race <threads> <iters> [1: yield between calls] */
        if (sscanf(line + 5, "%d %d %d", &threads, &iters, &yield) < 2 || threads < 1 || threads > 64 || iters < 1)
            return false;
        race_yield = yield;
        race_go = 0;
        race_started = race_done = race_bad = 0;
        for (int i = 0; i < threads; i++) {
            HANDLE thread = CreateThread(NULL, 0, race_worker, (LPVOID)(intptr_t)iters, 0, NULL);
            if (thread) CloseHandle(thread);
        }
        return true;
    }
    if (!strcmp(line, "crash")) {
        crash_expected = 1;
        crash_done = (uint32_t)call_faulter(0) + 1; /* not reached unless the fault is handled and resumed */
        return true;
    }
    /* handled <n>: n faults at one address, each recovered by the host (faults the game survives). */
    if (!strncmp(line, "handled ", 8)) {
        int count = atoi(line + 8);
        handled_expected = 1;
        for (int i = 0; i < count; i++) ((int (*)(void))(void *)ud2fn)();
        handled_expected = 0;
        char out[64];
        snprintf(out, sizeof(out), "handled %d", count);
        append_log(out);
        return true;
    }
    if (!strcmp(line, "crashthread")) {
        crash_thread_done = 0;
        HANDLE thread = CreateThread(NULL, 0, crash_worker, NULL, 0, NULL);
        if (thread) CloseHandle(thread);
        return true;
    }
    if (!strcmp(line, "error")) {
        append_log("Error in script 00000000 in mod fake\nboom");
        send(10, "boom");
        return true;
    }
    char text[600];
    snprintf(text, sizeof(text), "echo >> %s", line);
    append_log(text);
    return true;
}
static bool run_line2(const char *line, void *ref, bool suppress) { (void)suppress; return run_line(line, ref); }

static void *compile_expr(const char *text) { return strstr(text, "bad") ? NULL : _strdup(text); }
static bool call_fn(void *script, void *object, void *container, Element *out, uint8_t count, ...) {
    (void)object; (void)container; (void)count;
    const char *text = script;
    memset(out, 0, sizeof(*out));
    if (!strcmp(text, "form")) {
        uint32_t p = (uint32_t)(uintptr_t)&fake_form;
        memcpy(&out->number, &p, 4);
        out->type = 2;
    } else if (!strcmp(text, "str")) {
        static const char *s = "hello \"world\"";
        uint32_t p = (uint32_t)(uintptr_t)s;
        memcpy(&out->number, &p, 4);
        out->type = 3;
    } else if (!strcmp(text, "arr")) {
        uint32_t p = 0x1234;
        memcpy(&out->number, &p, 4);
        out->type = 4;
    } else if (!strcmp(text, "err")) {
        send(10, "expression error");
        out->type = 1;
    } else {
        out->number = strlen(text);
        out->type = 1;
    }
    return true;
}
static uint32_t array_size(void *array) { return array == (void *)0x1234 ? 2 : 0; }
static bool array_elements(void *array, Element *values, Element *keys) {
    (void)array; (void)keys;
    values[0].type = 1; values[0].number = 7;
    values[1].type = 1; values[1].number = 8;
    return true;
}
static int array_kind(void *array) { (void)array; return 0; }

static ConsoleAPI console = {3, run_line, run_line2};
static ScriptAPI scripts = {.CallFunction = call_fn, .CompileExpression = compile_expr, .CompileScript = compile_expr};
static struct {
    void *f[6];
    uint32_t (*size)(void *);
    void *lookup;
    void *get;
    bool (*elements)(void *, Element *, Element *);
    void *packed;
    int (*kind)(void *);
} arrays = {{0}, array_size, 0, 0, array_elements, 0, array_kind};
static bool register_listener(uint32_t handle, const char *sender, void (*fn)(Message *)) {
    (void)handle; (void)sender;
    listener = fn;
    return true;
}
static Messaging messaging = {4, register_listener, NULL};
static void *query(uint32_t id) {
    return id == 1 ? (void *)&console : id == 2 ? (void *)&messaging : id == 5 ? (void *)&arrays : id == 6 ? (void *)&scripts : id == 8 ? (void *)&fake_em : NULL;
}
static uint32_t handle(void) { return 7; }
static const char *root(void) { return ".\\"; }

int main(int argc, char **argv) {
    if (argc < 3) return 2;
    HMODULE dll = LoadLibraryA(argv[1]);
    if (!dll) return 3;
    QueryFn q = (QueryFn)(void *)GetProcAddress(dll, "NVSEPlugin_Query");
    LoadFn l = (LoadFn)(void *)GetProcAddress(dll, "NVSEPlugin_Load");
    NVSEInterface nvse = {0x06040090, 0x040020D0, 0, 0, NULL, NULL, query, handle, NULL, root, 0};
    PluginInfo info;
    NVSEInterface editor = nvse;
    editor.isEditor = 1;
    NVSEInterface wrong_build = nvse;
    wrong_build.runtimeVersion = 0x040020C1;
    if (!q || !l || q(&editor, &info) || q(&wrong_build, &info) || !q(&nvse, &info) || !l(&nvse)) return 4;
    AddVectoredExceptionHandler(0, last_chance); /* appended: runs after the companion's handlers */
    static int self_value = 6;
    fake_form.vtable = (void *)(uintptr_t)add3; /* a vtable inside this image: the companion sees a game object */
    fake_os_ptr = (uint32_t)(uintptr_t)fake_os;
    printf("{\"buffer\":\"0x%08x\",\"chain\":\"0x%08x\",\"chainEnd\":\"0x%08x\",\"add3\":\"0x%08x\",\"sub2\":\"0x%08x\","
           "\"member\":\"0x%08x\",\"self\":\"0x%08x\",\"fast\":\"0x%08x\",\"halfsum\":\"0x%08x\",\"scale\":\"0x%08x\",\"msvcThis\":\"0x%08x\",\"msvcSeh\":\"0x%08x\",\"prehooked\":\"0x%08x\",\"msvcFloat\":\"0x%08x\",\"tiny\":\"0x%08x\",\"siteCaller\":\"0x%08x\",\"siteCall\":\"0x%08x\",\"vtCaller\":\"0x%08x\",\"tinyVtable\":\"0x%08x\",\"tiny2\":\"0x%08x\",\"ptrCaller\":\"0x%08x\",\"writer\":\"0x%08x\",\"watched\":\"0x%08x\","
           "\"timerDisable\":\"0x%08x\",\"timerEnable\":\"0x%08x\",\"timerGlobal\":\"0x%08x\",\"timerDisables\":\"0x%08x\",\"timerEnables\":\"0x%08x\","
           "\"poll\":\"0x%08x\",\"polls\":\"0x%08x\",\"force\":\"0x%08x\",\"forces\":\"0x%08x\",\"lastForce\":\"0x%08x\","
           "\"osPtr\":\"0x%08x\",\"osGlobals\":\"0x%08x\",\"faulter\":\"0x%08x\",\"ud2\":\"0x%08x\","
           "\"hookme\":\"0x%08x\",\"hookLast\":\"0x%08x\",\"hookThreadResult\":\"0x%08x\",\"hookThreadDone\":\"0x%08x\","
           "\"crashDone\":\"0x%08x\",\"crashThreadDone\":\"0x%08x\",\"bpTarget\":\"0x%08x\",\"bpCall\":\"0x%08x\","
           "\"bpAfter\":\"0x%08x\",\"bpRet\":\"0x%08x\",\"bpCallee\":\"0x%08x\",\"bpResult\":\"0x%08x\",\"seam\":\"0x%08x\",\"hookReuseMs\":\"0x%08x\","
           "\"raceGo\":\"0x%08x\",\"raceStarted\":\"0x%08x\",\"raceDone\":\"0x%08x\",\"raceBad\":\"0x%08x\"}\n",
           (unsigned)(uintptr_t)memory, (unsigned)(uintptr_t)&chain_a, (unsigned)(uintptr_t)chain_b,
           (unsigned)(uintptr_t)add3, (unsigned)(uintptr_t)sub2, (unsigned)(uintptr_t)member,
           (unsigned)(uintptr_t)&self_value, (unsigned)(uintptr_t)fast, (unsigned)(uintptr_t)halfsum,
           (unsigned)(uintptr_t)scale, (unsigned)(uintptr_t)msvc_this, (unsigned)(uintptr_t)msvc_seh,
           (unsigned)(uintptr_t)prehooked, (unsigned)(uintptr_t)msvc_float, (unsigned)(uintptr_t)tiny,
           (unsigned)(uintptr_t)site_caller, (unsigned)(uintptr_t)site_call, (unsigned)(uintptr_t)vt_caller,
           (unsigned)(uintptr_t)tiny_vtable, (unsigned)(uintptr_t)tiny2, (unsigned)(uintptr_t)ptr_caller,
           (unsigned)(uintptr_t)writer, (unsigned)(uintptr_t)&watched,
           (unsigned)(uintptr_t)fake_timer_disable, (unsigned)(uintptr_t)fake_timer_enable,
           (unsigned)(uintptr_t)&timer_global, (unsigned)(uintptr_t)&timer_disables,
           (unsigned)(uintptr_t)&timer_enables,
           (unsigned)(uintptr_t)fake_poll, (unsigned)(uintptr_t)&poll_count, (unsigned)(uintptr_t)fake_force,
           (unsigned)(uintptr_t)&force_count, (unsigned)(uintptr_t)&last_force, (unsigned)(uintptr_t)&fake_os_ptr,
           (unsigned)(uintptr_t)fake_os, (unsigned)(uintptr_t)faulter, (unsigned)(uintptr_t)ud2fn,
           (unsigned)(uintptr_t)hookme, (unsigned)hook_last, (unsigned)hook_thread_result,
           (unsigned)hook_thread_done, (unsigned)crash_done, (unsigned)crash_thread_done,
           (unsigned)(uintptr_t)bp_target, (unsigned)(uintptr_t)bp_call_site, (unsigned)(uintptr_t)bp_after_site,
           (unsigned)(uintptr_t)bp_ret_site, (unsigned)(uintptr_t)bp_callee, (unsigned)bp_result,
           (unsigned)(uintptr_t)(void *)GetProcAddress(dll, "fnvmcp_seam"), /* 0 unless a seam build */
           (unsigned)(uintptr_t)(void *)GetProcAddress(dll, "fnvmcp_hook_reuse_ms"), /* 0 unless a seam build */
           (unsigned)(uintptr_t)&race_go, (unsigned)(uintptr_t)&race_started, (unsigned)(uintptr_t)&race_done,
           (unsigned)(uintptr_t)&race_bad);
    fflush(stdout);
    send(18, NULL);  /* DeferredInit */
    send(14, NULL);  /* NewGame */
    ULONGLONG end = GetTickCount64() + (ULONGLONG)atoi(argv[2]) * 1000;
    while (GetTickCount64() < end) {
        send(20, NULL);
        Sleep(5);
    }
    send(1, NULL);
    Sleep(200); /* let a hook thread that is still running finish before the result is printed */
    printf("{\"exitHookThreadDone\":%u}\n", (unsigned)hook_thread_done);
    fflush(stdout);
    return 0;
}
