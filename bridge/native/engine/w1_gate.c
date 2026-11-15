/* Frame gate (contract FNV-COMPANION-V2.md section 3). Included by fnvmcp_engine.c.
 * The gate lives in the main-loop message. While it is held the main thread stays in gate_park and serves
 * requests, so no frame runs and the engine clock is paused. step and until open the gate for some frames
 * and close it again at the stop; the rest of the request that asked for them is served at that stop. */
#define GATE_TIMEOUT 0x1D4C0u /* dead-man time: 120 s */
#define GATE_FRAMES_MAX 0x2710u
#define SUSP_NONE 0
#define SUSP_STEP 1
#define SUSP_UNTIL 2

/* Defined further down in fnvmcp_engine.c or in w1_input.c. */
static void serve_ops(char *ops, bool first);
static void serve_request(char *line);
static void inp_release_all(void);
static unsigned stop_release_all(bool deadman); /* w2_stop.c */
static bool stop_main_parked(void);         /* w2_stop.c */

static volatile LONG gate_held, step_left, suspend_kind;
/* The dead-man clock: set when the main thread serves a request, and by a ping while no request was dropped unserved
 * since the last served one (K2: a stuck main thread must not be kept "in contact" by an agent that keeps polling). */
static volatile ULONGLONG last_contact;
static volatile LONG request_dropped; /* the pipe thread gave up on a request the main thread never took */
static volatile LONG client_left;     /* the bridge disconnected: the main thread lets go of what it held (H4) */
static volatile uint32_t gate_timeout = GATE_TIMEOUT;
static HANDLE request_event;            /* set by the pipe thread when it queues a request */
static uint32_t gate_disable_fn, gate_enable_fn, gate_timer;
static bool gate_clock_paused, gate_want_hold, gate_suspending;
static uint32_t step_total, until_max, until_frames;
static int until_pred = -1;
static char *suspend_next; /* the ops of the suspended request that come after step or until */

/* Calls BSTimer_Disable or BSTimer_Enable (thiscall, ecx = TimeGlobal) on the main thread. */
static void timer_call(uint32_t target) {
    uint32_t words[1] = {0};
    CallResult result;
    fnvmcp_invoke(target, words, 0, gate_timer, 0, 0, &result);
}
/* The engine's clock counter is paused while the gate is held. We enable it again only if we paused it, so
 * the counter stays balanced with the engine's own users (focus loss). */
static void gate_close(void) {
    InterlockedExchange(&gate_held, 1);
    if (!gate_clock_paused && gate_disable_fn) {
        timer_call(gate_disable_fn);
        gate_clock_paused = true;
    }
}
static void gate_open(void) {
    InterlockedExchange(&gate_held, 0);
    if (gate_clock_paused) {
        gate_clock_paused = false;
        if (gate_enable_fn) timer_call(gate_enable_fn);
    }
}
/* At load: no Windows ghosting, so the message pump of a held window keeps the window alive. */
static bool gate_init(void) {
    typedef VOID(WINAPI * DisableGhostingFn)(void);
    HMODULE user32 = GetModuleHandleA("user32.dll");
    DisableGhostingFn disable;
    if (!user32) user32 = LoadLibraryA("user32.dll");
    disable = user32 ? (DisableGhostingFn)(void *)GetProcAddress(user32, "DisableProcessWindowsGhosting") : NULL;
    if (disable) disable();
    request_event = CreateEventA(NULL, FALSE, FALSE, NULL);
    return request_event != NULL;
}
/* The game is shutting down: never leave it held. */
static void gate_exit(void) {
    gate_want_hold = false;
    gate_open();
    inp_release_all();
    stop_release_all(false);
}
/* The dead-man switch also ends every input job: nothing is held down while no one is driving the game. */
static void gate_deadman(void) {
    char text[96];
    snprintf(text, sizeof(text), "held for %u ms without a request or ping", (unsigned)gate_timeout);
    gate_want_hold = false;
    gate_open();
    inp_release_all();
    record_event(EVENT_DEADMAN, text);
}

/* Pipe thread, after a client disconnected (H4): stopped threads are released here at once; the gate, held input and
 * a running step or until are let go by the main thread (gate_let_go), woken now. */
static void gate_client_gone(void) {
    unsigned released = stop_release_all(false);
    if (released) {
        char text[96];
        snprintf(text, sizeof(text), "the bridge disconnected: %u stop(s) released", released);
        record_event(EVENT_LEFT, text);
    }
    InterlockedExchange(&client_left, 1);
    SetEvent(request_event);
}
/* Main thread: after a disconnect the gate opens, held input is let go, and a step or until still running ends
 * without its reply (no one is left to read it). Nothing waits on done_event for it: the pipe thread left. */
static void gate_let_go(void) {
    if (!InterlockedExchange(&client_left, 0)) return;
    bool had = gate_held || gate_want_hold || suspend_kind != SUSP_NONE;
    if (suspend_kind != SUSP_NONE) {
        InterlockedExchange(&suspend_kind, SUSP_NONE);
        InterlockedExchange(&step_left, 0);
        until_pred = -1;
        suspend_next = NULL;
        gate_suspending = false;
        InterlockedExchange(&serving_request, 0);
        InterlockedCompareExchange(&pending, 0, 2);
    }
    gate_want_hold = false;
    gate_open();
    inp_release_all();
    if (had) record_event(EVENT_LEFT, "the bridge disconnected: gate opened, input released");
}

/* Called once per frame while a step or until runs (from the main-loop message, after the frame). */
static void gate_stop(bool met);
static void gate_frame_tick(void) {
    if (suspend_kind == SUSP_STEP) {
        if (InterlockedDecrement(&step_left) <= 0) gate_stop(false);
    } else if (suspend_kind == SUSP_UNTIL) {
        until_frames++;
        bool met = pred_eval(until_pred, NULL, NULL, NULL);
        if (met || until_frames >= until_max) gate_stop(met);
    }
}
/* A step or until ends at this frame: close the gate, write the op's own result, serve the rest. */
static void gate_stop(bool met) {
    int kind = (int)suspend_kind;
    suspend_kind = SUSP_NONE;
    InterlockedExchange(&step_left, 0);
    gate_close();
    if (kind == SUSP_STEP) emit("{\"stepped\":%u,\"frame\":%ld}", step_total, frame);
    else emit("{\"met\":%s,\"frames\":%u,\"frame\":%ld}", met ? "true" : "false", until_frames, frame);
    until_pred = -1;
    serve_ops(suspend_next, false);
}

/* A stop parks the main thread while a step or until request is open (seen live: a breakpoint hit inside
 * fnv_wait_for). No frame runs until the stop is released, so the request would never end and the one pipe instance
 * would stay busy, leaving no way to send the resume. End it now with "stopped" and serve the rest of its ops. */
static void gate_abort_stopped(void) {
    int kind = (int)suspend_kind;
    if (kind == SUSP_NONE) return;
    suspend_kind = SUSP_NONE;
    LONG left = InterlockedExchange(&step_left, 0);
    if (kind == SUSP_STEP)
        emit("{\"ok\":false,\"error\":\"stopped\",\"stepped\":%ld,\"frame\":%ld}",
             (long)step_total - (left > 0 ? (long)left : 0), frame);
    else
        emit("{\"ok\":false,\"error\":\"stopped\",\"met\":false,\"frames\":%u,\"frame\":%ld}", until_frames,
             frame);
    until_pred = -1;
    serve_ops(suspend_next, false);
}

/* While the gate is held: serve requests as they come and let the dead-man switch open the gate when no
 * request or ping arrived for the timeout. Returns when the gate opens. */
static void gate_park(void) {
    while (gate_held) {
        gate_let_go(); /* the bridge left: open now, not at the dead-man time (H4) */
        if (!gate_held) return;
        if (InterlockedCompareExchange(&pending, 2, 1) == 1) {
            serve_request(request); /* may open the gate again (step, until, hold off) */
            continue;
        }
        ULONGLONG idle = GetTickCount64() - last_contact, limit = gate_timeout;
        if (idle >= limit) {
            gate_deadman();
            return;
        }
        ULONGLONG left = limit - idle;
        WaitForSingleObject(request_event, (DWORD)(left < 1000 ? left : 1000));
    }
}

/* gatecfg <disable_fn> <enable_fn> <timer> */
static void op_gatecfg(int argc, char **argv) {
    uint32_t disable, enable, timer;
    if (argc != 4 || !parse_u32(argv[1], &disable) || !parse_u32(argv[2], &enable) || !parse_u32(argv[3], &timer))
        return fail("bad_arguments");
    gate_disable_fn = disable;
    gate_enable_fn = enable;
    gate_timer = timer;
    emit("{\"ok\":true}");
}
/* hold on [timeout_ms] | hold off. Closing takes effect at the end of the current request. */
static void op_hold(int argc, char **argv) {
    uint32_t timeout = GATE_TIMEOUT;
    if (argc == 2 && !strcmp(argv[1], "off")) {
        gate_want_hold = false;
        gate_open();
        emit("{\"held\":false}");
        return;
    }
    if (argc < 2 || argc > 3 || strcmp(argv[1], "on")) return fail("bad_arguments");
    if (argc == 3 && (!parse_u32(argv[2], &timeout) || !timeout)) return fail("bad_arguments");
    gate_timeout = timeout;
    if (!gate_held) gate_want_hold = true;
    emit("{\"held\":true,\"timeout\":%u}", timeout);
}
/* step <n>: let n frames run, then hold. The ops after it are served at the stop. */
static void op_step(int argc, char **argv) {
    uint32_t frames;
    if (argc != 2 || !parse_u32(argv[1], &frames) || !frames || frames > GATE_FRAMES_MAX) return fail("bad_arguments");
    if (stop_main_parked()) return fail("stopped"); /* no frame runs while the main thread is stopped */
    gate_want_hold = false;
    gate_open();
    step_total = frames;
    InterlockedExchange(&step_left, (LONG)frames);
    InterlockedExchange(&suspend_kind, SUSP_STEP);
    gate_suspending = true;
}
/* until <pred> <max>: run frames until the predicate is true or max frames passed, then hold. */
static void op_until(int argc, char **argv) {
    uint32_t pred, max;
    if (argc != 3 || !parse_u32(argv[1], &pred) || pred >= PRED_SLOTS || !parse_u32(argv[2], &max) || !max ||
        max > GATE_FRAMES_MAX)
        return fail("bad_arguments");
    if (stop_main_parked()) return fail("stopped");
    if (!preds[pred].used) return fail("bad_id");
    if (pred_eval((int)pred, NULL, NULL, NULL)) {
        gate_want_hold = true;
        emit("{\"met\":true,\"frames\":0,\"frame\":%ld}", frame);
        return;
    }
    gate_want_hold = false;
    gate_open();
    until_pred = (int)pred;
    until_max = max;
    until_frames = 0;
    InterlockedExchange(&suspend_kind, SUSP_UNTIL);
    gate_suspending = true;
}
/* gate: the state of the gate. */
static void op_gate(int argc, char **argv) {
    ULONGLONG now = GetTickCount64();
    (void)argv;
    if (argc != 1) return fail("bad_arguments");
    emit("{\"held\":%s,\"clockPausedByUs\":%s,\"timeout\":%u,\"msSinceContact\":%llu,\"stepLeft\":%ld,\"untilPred\":",
         gate_held ? "true" : "false", gate_clock_paused ? "true" : "false", (unsigned)gate_timeout,
         now - last_contact, (long)step_left);
    if (suspend_kind == SUSP_UNTIL) emit("%d", until_pred);
    else emit("null");
    emit("}");
}
