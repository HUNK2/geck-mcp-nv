/* Stops (contract FNV-COMPANION-V2.md section 8). Included by fnvmcp_engine.c after w1_input.c.
 * A stop parks the thread that reached it until a resume op releases it: a hook hold (8.1) or a crash hold (8.3).
 * Trap breakpoints (8.2) and the trace (8.4) come in a later wave: they call stop_park with their own kind and the
 * addresses of the live CONTEXT and EXCEPTION_RECORD, and resume_op takes their modes once they exist (step, to,
 * trace answer bad_mode until then).
 * The main thread parks in the serve loop of the frame gate, so every op works while it is stopped, and the gate
 * counts as held (the clock is paused). Other threads wait on their stop's event without serving.
 * The dead-man switch releases every stop with run, and MSG_EXIT does the same. A stop is never taken while the main
 * thread serves a request (a served engine call cannot stop inside itself), nor while the game is exiting. */
#define STOPS 16u
#define HOLD_ALWAYS 0xFFFFFFFFu /* hookact <id> hold ff: hold at every passing call */
enum { STOP_HOOK, STOP_BP, STOP_STEP, STOP_FAULT };
enum { RESUME_RUN, RESUME_SKIP, RESUME_EIP, RESUME_RET, RESUME_STEP, RESUME_TO, RESUME_TRACE };
static const char *stop_kind_names[] = {"hook", "bp", "step", "fault"};

typedef struct {
    volatile LONG claimed, ready, resuming, resumed; /* claimed: slot in use; ready: listed; resumed: released */
    HANDLE event;
    uint32_t seq, kind, id, thread, eip, regs, context, exception, pop;
    LONG frame;
    ULONGLONG parked;
    bool main, pause;
    uint32_t mode, arg, arg2;
    CONTEXT snapshot; /* the CONTEXT when parked: a release that changed it recovers the fault */
} Stop;
typedef struct { uint32_t mode, arg, arg2; bool edited; } StopOutcome;
/* Trap stops (w2_bp.c): the step, to and trace resumes. */
static LONG bp_fault_resume(CONTEXT *c, const StopOutcome *out);
static const char *bp_resume_prepare(const Stop *s, uint32_t mode, uint32_t arg, int argc, char **argv);
static void bp_resume_undo(const Stop *s, uint32_t mode, uint32_t arg);

static Stop stops_table[STOPS];
/* A parked stop of this hook, or one whose eip is in [lo, hi): the hook's memory must not be reused (K3). */
static bool stop_holds_hook(uint32_t id, uint32_t lo, uint32_t hi) {
    for (int i = 0; i < (int)STOPS; i++) {
        const Stop *s = &stops_table[i];
        if (s->claimed && ((s->kind == STOP_HOOK && s->id == id) || (s->eip >= lo && s->eip < hi))) return true;
    }
    return false;
}
/* Skip stubs, 16 bytes each, executable; made by the main thread. They are not tied to a stop slot: the slot is freed
 * before the parked thread returns into its stub, and another stop could claim that slot and rewrite the stub first.
 * Each skip takes the next stub of a ring instead, which comes round again only after STOP_STUBS more skips. */
#define STOP_STUBS 256u
static unsigned char *stop_stubs;
static volatile LONG stop_stub_next;
static volatile LONG stop_seq, main_stops, fault_hold;
static bool stop_closed_gate; /* the main thread's stop closed the gate (it was open before), so it opens it again */

static bool in_served_call(void) { return serving_request && main_thread && GetCurrentThreadId() == main_thread; }
static bool stop_main_parked(void) { return main_stops > 0; }
static unsigned stop_parked(void) {
    unsigned n = 0;
    for (int i = 0; i < (int)STOPS; i++)
        if (stops_table[i].ready && !stops_table[i].resumed) n++;
    return n;
}
static bool stop_stub_init(void) {
    if (!stop_stubs) stop_stubs = VirtualAlloc(NULL, 16u * STOP_STUBS, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    return stop_stubs != NULL;
}
static int stop_claim(void) {
    for (int i = 0; i < (int)STOPS; i++)
        if (!InterlockedCompareExchange(&stops_table[i].claimed, 1, 0)) return i;
    return -1;
}
static void stop_free(int slot) {
    InterlockedExchange(&stops_table[slot].ready, 0);
    InterlockedExchange(&stops_table[slot].claimed, 0);
}
/* Milliseconds since the stop parked or the last request or ping, whichever is later (the dead-man clock). */
static ULONGLONG stop_idle(const Stop *s) {
    ULONGLONG since = last_contact > s->parked ? last_contact : s->parked, now = GetTickCount64();
    return now > since ? now - since : 0;
}
/* Releases one stop with a mode. Only the first release counts; the parked thread reads mode and arg after resumed. */
static bool stop_release(Stop *s, uint32_t mode, uint32_t arg, uint32_t arg2) {
    if (InterlockedCompareExchange(&s->resuming, 1, 0)) return false;
    s->mode = mode;
    s->arg = arg;
    s->arg2 = arg2;
    MemoryBarrier();
    InterlockedExchange(&s->resumed, 1);
    SetEvent(s->event);
    return true;
}
/* Releases every listed stop with run (the dead-man switch, MSG_EXIT, a disconnect). Returns how many. */
static unsigned stop_release_all(bool deadman) {
    unsigned released = 0;
    for (int i = 0; i < (int)STOPS; i++) {
        Stop *s = &stops_table[i];
        if (s->ready && !s->resumed && stop_release(s, RESUME_RUN, 0, 0)) released++;
    }
    if (deadman && released) {
        char text[120];
        snprintf(text, sizeof(text), "%u stop(s) released after %u ms without a request or ping", released,
                 (unsigned)gate_timeout);
        record_event(EVENT_DEADMAN, text);
    }
    return released;
}
/* The main thread waits here and serves requests (resume among them) until its stop is released. */
static void stop_serve_wait(Stop *s) {
    while (!s->resumed) {
        if (InterlockedCompareExchange(&pending, 2, 1) == 1) {
            serve_request(request);
            continue;
        }
        ULONGLONG idle = stop_idle(s);
        if (idle >= gate_timeout) {
            stop_release_all(true);
            continue;
        }
        ULONGLONG left = gate_timeout - idle;
        WaitForSingleObject(request_event, (DWORD)(left < 1000 ? left : 1000));
    }
}
/* Another thread waits on its own event; the dead-man check runs here too, since the main thread may be busy. */
static void stop_wait_worker(Stop *s) {
    while (!s->resumed) {
        WaitForSingleObject(s->event, 500);
        if (!s->resumed && stop_idle(s) >= gate_timeout) stop_release_all(true);
    }
}

/* Parks the calling thread as a stop until a resume op releases it. Returns the claimed slot (free it with stop_free
 * after reading the outcome), or -1 when the table is full: the caller then goes on as if there were no stop.
 * pause: a main-thread stop also pauses the clock, like the frame gate. context / exception: addresses of the live
 * CONTEXT and EXCEPTION_RECORD (0 for a hook stop); the CONTEXT is compared at release (outcome edited). */
static int stop_park(uint32_t kind, uint32_t id, uint32_t eip, uint32_t regs, uint32_t context, uint32_t exception,
                     uint32_t pop, bool pause, StopOutcome *out) {
    int slot = stop_claim();
    if (slot < 0) return -1;
    Stop *s = &stops_table[slot];
    if (!s->event) s->event = CreateEventA(NULL, FALSE, FALSE, NULL);
    if (!s->event) {
        stop_free(slot);
        return -1;
    }
    InterlockedExchange(&s->resuming, 0);
    InterlockedExchange(&s->resumed, 0);
    s->mode = RESUME_RUN;
    s->arg = s->arg2 = 0;
    s->kind = kind;
    s->id = id;
    s->thread = GetCurrentThreadId();
    s->main = main_thread && GetCurrentThreadId() == main_thread;
    s->pause = pause && s->main;
    s->eip = eip;
    s->regs = regs;
    s->context = context;
    s->exception = exception;
    s->pop = pop;
    s->frame = frame;
    s->seq = (uint32_t)InterlockedIncrement(&stop_seq);
    s->parked = GetTickCount64();
    if (context) memcpy(&s->snapshot, (const void *)(uintptr_t)context, sizeof(CONTEXT));
    MemoryBarrier();
    InterlockedExchange(&s->ready, 1);
    if (s->main) {
        if (s->pause) {
            if (!gate_held) stop_closed_gate = true;
            gate_close();
        }
        InterlockedIncrement(&main_stops);
        gate_abort_stopped(); /* an open step / until request ends here, so the pipe is free for the resume */
        stop_serve_wait(s);
        if (InterlockedDecrement(&main_stops) == 0 && stop_closed_gate) {
            stop_closed_gate = false;
            gate_open();
        }
    } else {
        stop_wait_worker(s);
    }
    out->mode = s->mode;
    out->arg = s->arg;
    out->arg2 = s->arg2;
    out->edited = context && memcmp(&s->snapshot, (const void *)(uintptr_t)context, sizeof(CONTEXT)) != 0;
    return slot;
}

/* A fresh skip stub: mov eax, value; ret [pop] (what the existing skip action does). */
static uint32_t stop_skip_stub(int slot, uint32_t value, uint32_t pop) {
    unsigned char stub[16], *q = stub;
    (void)slot;
    if (!stop_stubs) return 0;
    *q++ = 0xB8; /* mov eax, imm32 */
    memcpy(q, &value, 4);
    q += 4;
    if (pop) {
        *q++ = 0xC2; /* ret imm16 */
        *q++ = (unsigned char)(pop & 0xFF);
        *q++ = (unsigned char)(pop >> 8);
    } else {
        *q++ = 0xC3;
    }
    unsigned index = (unsigned)InterlockedIncrement(&stop_stub_next) % STOP_STUBS;
    memcpy(stop_stubs + 16u * index, stub, (size_t)(q - stub));
    FlushInstructionCache(GetCurrentProcess(), stop_stubs + 16u * index, 16);
    return (uint32_t)(uintptr_t)(stop_stubs + 16u * index);
}

/* The predicate context of a hook: the saved register block (contract 8.1) with this = ecx and the arguments. */
static void hook_context(PredContext *ctx, const uint32_t *fr) {
    ctx->regs[0] = fr[8];
    ctx->regs[1] = fr[7];
    ctx->regs[2] = fr[6];
    ctx->regs[3] = fr[5];
    ctx->regs[4] = (uint32_t)(uintptr_t)&fr[10]; /* esp at entry: the return address slot */
    ctx->regs[5] = fr[3];
    ctx->regs[6] = fr[2];
    ctx->regs[7] = fr[1];
    ctx->args = (uint32_t)(uintptr_t)&fr[11];
    ctx->this_ptr = fr[7];
}

/* Called from fnvmcp_on_enter for a hooked call that passed the filter. Parks when a next stop is armed or the hold
 * predicate passes. Returns 0 to run the call, or the skip stub when the resume asked to skip it. */
static uint32_t hook_stop(Hook *h, uint32_t *fr) {
    if (exiting || in_served_call()) return 0;
    bool next = InterlockedCompareExchange(&h->stop_next, 0, 1) == 1;
    if (!next) {
        if (h->act_mode != ACT_HOLD) return 0;
        if (h->act_value != HOLD_ALWAYS) {
            PredContext ctx;
            hook_context(&ctx, fr);
            if (!pred_eval((int)h->act_value, &ctx, NULL, NULL)) return 0;
        }
    }
    StopOutcome out = {0, 0, 0, false};
    int slot = stop_park(STOP_HOOK, h->id, h->target, (uint32_t)(uintptr_t)fr, 0, 0, h->act_aux, true, &out);
    if (slot < 0) return 0;
    uint32_t stub = 0;
    if (out.mode == RESUME_SKIP) stub = stop_skip_stub(slot, out.arg, stops_table[slot].pop);
    stop_free(slot);
    return stub;
}

/* Called by the crash handler (on_exception) after the crash-context file is written. Crash hold (contract 8.3):
 * parks the faulting thread while faulthold is on, except during exit, while the main thread serves a request, and
 * for stack overflows (never reached here). Returns what the handler returns to Windows. */
static LONG stop_fault(EXCEPTION_POINTERS *e) {
    CONTEXT *c = e->ContextRecord;
    StopOutcome out = {0, 0, 0, false};
    if (!fault_hold || exiting || in_served_call()) return EXCEPTION_CONTINUE_SEARCH;
    int slot = stop_park(STOP_FAULT, 0, (uint32_t)c->Eip, 0, (uint32_t)(uintptr_t)c,
                         (uint32_t)(uintptr_t)e->ExceptionRecord, 0, false, &out);
    if (slot < 0) return EXCEPTION_CONTINUE_SEARCH;
    stop_free(slot);
    if (out.mode == RESUME_STEP || out.mode == RESUME_TO || out.mode == RESUME_TRACE) return bp_fault_resume(c, &out);
    if (out.mode == RESUME_EIP) {
        c->Eip = out.arg;
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    if (out.mode == RESUME_RET) {
        uint32_t sp = (uint32_t)c->Esp, ret = 0;
        if (!read_u32(sp, &ret)) return EXCEPTION_CONTINUE_SEARCH;
        c->Eax = out.arg;
        c->Esp = sp + 4 + out.arg2;
        c->Eip = ret;
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    /* run: a CONTEXT the bridge edited is an attempt to recover; otherwise the crash goes on as without the hold */
    return out.edited ? EXCEPTION_CONTINUE_EXECUTION : EXCEPTION_CONTINUE_SEARCH;
}

/* hookact <id> hold <pred|ff> [pop]: park inside the hook at each passing call while the predicate is true (ff: at
 * every passing call). The predicate sees the hook's saved registers and arguments. pop: bytes a skip resume pops
 * (the stdcall argument bytes). Replaces the hook's other behaviour action. */
static void op_hookhold(int argc, char **argv) {
    uint32_t id, pred = HOLD_ALWAYS, pop = 0;
    if (argc < 4 || argc > 5 || !parse_u32(argv[1], &id)) return fail("bad_arguments");
    Hook *h = hook_find(id);
    if (!h) return fail("unknown_hook");
    if (!h->stolen) return fail("already_removed");
    if (strcmp(argv[3], "ff")) {
        if (!parse_u32(argv[3], &pred) || pred >= PRED_SLOTS) return fail("bad_arguments");
        if (!preds[pred].used) return fail("bad_id");
    }
    if (argc == 5 && (!parse_u32(argv[4], &pop) || pop > 0x400)) return fail("bad_arguments");
    if (!stop_stub_init()) return fail("out_of_memory");
    h->act_mode = ACT_NONE;
    MemoryBarrier();
    h->act_value = pred;
    h->act_aux = pop;
    h->act_limit = 0;
    InterlockedExchange(&h->act_hits, 0);
    InterlockedExchange(&h->act_applied, 0);
    memset(h->script, 0, sizeof(h->script));
    MemoryBarrier();
    h->act_mode = ACT_HOLD;
    emit("{\"ok\":true,\"hook\":");
    emit_hook(h);
    emit("}");
}

static void emit_stop_address(uint32_t address) {
    if (address) emit("\"0x%08x\"", address);
    else emit("null");
}
/* stops: the stops that are waiting (not released yet). */
static void op_stops(int argc, char **argv) {
    bool comma = false;
    (void)argv;
    if (argc != 1) return fail("bad_arguments");
    emit("{\"stops\":[");
    for (int i = 0; i < (int)STOPS; i++) {
        Stop *s = &stops_table[i];
        if (!s->ready || s->resumed) continue;
        emit("%s{\"seq\":%u,\"kind\":\"%s\",\"id\":%u,\"thread\":%lu,\"main\":%s,\"eip\":\"0x%08x\",\"frame\":%ld,\"regs\":",
             comma ? "," : "", s->seq, stop_kind_names[s->kind], s->id, (unsigned long)s->thread,
             s->main ? "true" : "false", s->eip, (long)s->frame);
        emit_stop_address(s->regs);
        emit(",\"context\":");
        emit_stop_address(s->context);
        emit(",\"exception\":");
        emit_stop_address(s->exception);
        emit("}");
        comma = true;
    }
    emit("]}");
}

/* resume <seq|ff> run | next <hook_id> | skip <result> | eip <addr> | ret <eax> <pop> | step | to <addr> |
 * trace <max> <lo> <hi> [log_hex]. Hook stops take run, next and skip; fault stops take run, eip, ret, step, to and
 * trace; bp and step stops take run, eip, ret, step, to and trace (eip and ret only where they make sense: see
 * bp_after). step, to and trace on a hook stop: bad_mode. */
static void op_resume(int argc, char **argv) {
    if (argc < 3) return fail("bad_arguments");
    bool all = !strcmp(argv[1], "ff"), next = false;
    uint32_t seq = 0, mode = RESUME_RUN, arg = 0, arg2 = 0, hook_id = 0;
    if (!all && !parse_u32(argv[1], &seq)) return fail("bad_arguments");
    if (!strcmp(argv[2], "run") && argc == 3) mode = RESUME_RUN;
    else if (!strcmp(argv[2], "next") && argc == 4 && parse_u32(argv[3], &hook_id)) next = true;
    else if (!strcmp(argv[2], "skip") && argc == 4 && parse_u32(argv[3], &arg)) mode = RESUME_SKIP;
    else if (!strcmp(argv[2], "eip") && argc == 4 && parse_u32(argv[3], &arg)) mode = RESUME_EIP;
    else if (!strcmp(argv[2], "ret") && argc == 5 && parse_u32(argv[3], &arg) && parse_u32(argv[4], &arg2) &&
             arg2 <= 0x400)
        mode = RESUME_RET;
    else if (!strcmp(argv[2], "step") && argc == 3) mode = RESUME_STEP;
    else if (!strcmp(argv[2], "to") && argc == 4 && parse_u32(argv[3], &arg)) mode = RESUME_TO;
    else if (!strcmp(argv[2], "trace") && (argc == 6 || argc == 7)) mode = RESUME_TRACE;
    else return fail("bad_arguments");
    if (all) {
        unsigned released = 0;
        if (mode != RESUME_RUN || next) return fail("bad_mode");
        for (int i = 0; i < (int)STOPS; i++) {
            Stop *s = &stops_table[i];
            if (s->ready && !s->resumed && stop_release(s, RESUME_RUN, 0, 0)) released++;
        }
        emit("{\"ok\":true,\"resumed\":%u}", released);
        return;
    }
    Stop *s = NULL;
    for (int i = 0; i < (int)STOPS && !s; i++)
        if (stops_table[i].ready && !stops_table[i].resumed && stops_table[i].seq == seq) s = &stops_table[i];
    if (!s) return fail("no_stop");
    if ((next || mode == RESUME_SKIP) && s->kind != STOP_HOOK) return fail("bad_mode");
    if ((mode == RESUME_EIP || mode == RESUME_RET) && s->kind == STOP_HOOK) return fail("bad_mode");
    if ((mode == RESUME_STEP || mode == RESUME_TO || mode == RESUME_TRACE) && s->kind == STOP_HOOK)
        return fail("bad_mode");
    Hook *h = NULL;
    if (next) {
        h = hook_find(hook_id);
        if (!h) return fail("unknown_hook");
    }
    if (mode == RESUME_SKIP && !stop_stub_init()) return fail("out_of_memory");
    if (mode == RESUME_STEP || mode == RESUME_TO || mode == RESUME_TRACE) {
        const char *error = bp_resume_prepare(s, mode, arg, argc, argv); /* the thread is parked: safe to patch */
        if (error) return fail(error);
    }
    if (next) InterlockedExchange(&h->stop_next, 1); /* armed before the thread goes on, so the next call parks */
    if (!stop_release(s, mode, arg, arg2)) {
        if (next) InterlockedExchange(&h->stop_next, 0);
        bp_resume_undo(s, mode, arg);
        return fail("no_stop");
    }
    emit("{\"ok\":true,\"seq\":%u}", seq);
}

/* faulthold on|off: park a faulting thread after its crash-context file is written (contract 8.3). */
static void op_faulthold(int argc, char **argv) {
    if (argc != 2 || (strcmp(argv[1], "on") && strcmp(argv[1], "off"))) return fail("bad_arguments");
    InterlockedExchange(&fault_hold, !strcmp(argv[1], "on"));
    emit("{\"ok\":true,\"faultHold\":%s}", fault_hold ? "true" : "false");
}
