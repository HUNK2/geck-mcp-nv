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

static void send(uint32_t type, void *data) {
    Message m = {"NVSE", type, 0, data};
    listener(&m);
}
static void append_log(const char *text) {
    if (!log_path[0]) return;
    FILE *f = fopen(log_path, "ab");
    if (f) { fprintf(f, "%s\r\n", text); fclose(f); }
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
    return id == 1 ? (void *)&console : id == 2 ? (void *)&messaging : id == 5 ? (void *)&arrays : id == 6 ? (void *)&scripts : NULL;
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
    static int self_value = 6;
    printf("{\"buffer\":\"0x%08x\",\"chain\":\"0x%08x\",\"chainEnd\":\"0x%08x\",\"add3\":\"0x%08x\",\"sub2\":\"0x%08x\","
           "\"member\":\"0x%08x\",\"self\":\"0x%08x\",\"fast\":\"0x%08x\",\"halfsum\":\"0x%08x\",\"scale\":\"0x%08x\"}\n",
           (unsigned)(uintptr_t)memory, (unsigned)(uintptr_t)&chain_a, (unsigned)(uintptr_t)chain_b,
           (unsigned)(uintptr_t)add3, (unsigned)(uintptr_t)sub2, (unsigned)(uintptr_t)member,
           (unsigned)(uintptr_t)&self_value, (unsigned)(uintptr_t)fast, (unsigned)(uintptr_t)halfsum,
           (unsigned)(uintptr_t)scale);
    fflush(stdout);
    send(18, NULL);  /* DeferredInit */
    send(14, NULL);  /* NewGame */
    ULONGLONG end = GetTickCount64() + (ULONGLONG)atoi(argv[2]) * 1000;
    while (GetTickCount64() < end) {
        send(20, NULL);
        Sleep(5);
    }
    send(1, NULL);
    return 0;
}
