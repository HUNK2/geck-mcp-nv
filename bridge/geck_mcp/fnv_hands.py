"""Plan 2 wave 3, hands: fnv_play and fnv_ui_do, playing as a player would.

Facts (research/hud-input.md, checked where marked):
- Activation: control 5 pressed in the player input function, not in menu mode, movement controls enabled, the
  crosshair reference is the target; then TESObjectREFR::Activate(player, 0, 0, 1) 0x00573170. Refusals queue HUD
  messages (locked, can't activate) that fnv_hud reads.
- Controls-disabled byte on the player +0x680 (bit 0 movement); auto-move +0x652.
- Menu click as the engine does it: menu vtable slot 3 HandleClick(id = tile trait 0xFAA, tile); the menu is
  TileMenu +0x3C, the tile menus are in the array 0x011F3508 (index id - 0x3E9).
- Fast travel: PlayerCharacter::CanFastTravel 0x0093D660 (thiscall, queues the reason when it refuses) and
  PlayerCharacter::FastTravel(marker) 0x0093CDF0.
- Movement: the input injector forces control 0 (forward) for exact frames; heading through SetAngle Z.
"""
import heapq
import math
import struct
import time

from .fnv_eyes import heading_to
from .fnv_link import hexs, value_of, word
from .fnv_live import LiveError
from .esp import codec as _codec  # noqa: F401 - registers the cp1252-bytes error handler (R16)

PLAYER = 0x011DEA3C
CONTROLS_DISABLED = 0x680
MENU_ARRAY = 0x011F3508
TEXT_EDIT_MENU, TEXT_EDIT_SINGLETON, TEXT_EDIT_CLICK = 1051, 0x011DAEC4, 0x007E66A0
TILEMENU_MENU = 0x3C
TRAIT_ID, TRAIT_STRING = 0xFAA, 0xFC4
CAN_FAST_TRAVEL, FAST_TRAVEL = 0x0093D660, 0x0093CDF0
G_TES = 0x011DEA10
TES_PICK = 0x00458420
PICK_CTOR, PICK_FROM, PICK_TO, PICK_FILTER = 0x004A3C20, 0x004A3DA0, 0x004A3EB0, 0x004A3F70
FIND_REF_FOR_3D = 0x0056F930
LAYER_PATH = 0x26            # path pick layer (fnv_raycast path_pick)
RAY_SLOT = 0x100
RAY_SLOTS = 64
DOOR_TYPE = 28
STEP = 64.0                  # planner grid step in game units
KNEE = 45.0                  # ray height above the floor
HALF_WIDTH = 28.0            # the player's half width for route checks
MAX_STEP = 40.0              # most a floor may rise or fall between grid points (stairs yes, counters no)
ARRIVE = 90.0
ACTIVATE_REACH = 150.0       # how far (3D, feet to the reference) the player can activate something
REACH_MARGIN = 25.0          # goto to a reference stops this much inside the reach (N16)
DOOR_REACH = 140.0        # a closed door this close and ahead of a blocked walk is opened
MAX_REPLANS = 4
MENU_IDS = {'message': 1001, 'inventory': 1002, 'container': 1008, 'dialog': 1009, 'sleepwait': 1012,
            'start': 1013, 'lockpick': 1014, 'quantity': 1016, 'map': 1023, 'book': 1026, 'levelup': 1027,
            'repair': 1035, 'textedit': 1051, 'barter': 1053, 'hacking': 1055, 'vats': 1056, 'computers': 1057,
            'repairservices': 1058, 'companionwheel': 1075, 'recipe': 1077}


def _u32(raw, offset=0):
    return struct.unpack_from('<I', raw, offset)[0]


# DirectInput scan codes by key name (fnv_input key_name).
DIK = {'escape': 1, 'esc': 1, 'minus': 12, 'equals': 13, 'backspace': 14, 'tab': 15, 'enter': 28, 'return': 28,
       'ctrl': 29, 'lctrl': 29, 'shift': 42, 'lshift': 42, 'rshift': 54, 'alt': 56, 'lalt': 56, 'space': 57,
       'capslock': 58, 'grave': 41, 'tilde': 41, 'up': 200, 'down': 208, 'left': 203, 'right': 205, 'pageup': 201,
       'pagedown': 209, 'home': 199, 'end': 207, 'insert': 210, 'delete': 211}
DIK.update({str(n): 1 + n for n in range(1, 10)})
DIK['0'] = 11
DIK.update({c: code for row, start in (('qwertyuiop', 16), ('asdfghjkl', 30), ('zxcvbnm', 44))
            for code, c in enumerate(row, start)})
DIK.update({'f%d' % n: 58 + n for n in range(1, 11)})
DIK.update({'f11': 87, 'f12': 88})


def scan_code(name):
    """A DirectInput scan code from a key name (E, Space, Escape, F1, 1, Up...)."""
    code = DIK.get(str(name).strip().lower())
    if code is None:
        raise ValueError('Unknown key name %r; use letters, digits, F1-F12, Space, Enter, Escape, Tab, Shift, Ctrl, '
                         'Alt, Up, Down, Left, Right, PageUp, PageDown, Home, End, Insert, Delete, Backspace' % name)
    return code


def ref_arrive_radius(player, floor):
    """How close across the floor goto must come to a reference so it is inside the activation reach in 3D (N16):
    a target raised above the player's feet needs a closer stand. At least 30 units, at most ARRIVE."""
    dz = abs(float(floor[2]) - float(player[2]))
    want = ACTIVATE_REACH - REACH_MARGIN
    if dz >= want:
        return 30.0
    return max(30.0, min(ARRIVE, math.sqrt(want * want - dz * dz)))


def pickup_note(flags_before, flags_after):
    """What a press did to the reference's form flags: a taken item's reference is marked deleted (0x20) and points
    at its new holder; it is not disabled."""
    if flags_before is None or flags_after is None:
        return None
    if not flags_before & 0x20 and flags_after & 0x20:
        return 'the item was taken (its reference is now deleted from the world)'
    return None


def ref_arrived(player, floor):
    """goto reached a reference: inside the activation reach (3D) with a little margin."""
    return math.dist(player, floor) <= ACTIVATE_REACH - REACH_MARGIN / 2


class Rays:
    """Many TES::Pick rays in one companion batch (one pick object per slot in a game buffer)."""

    def __init__(self, live):
        self.live = live
        self._buf = None

    def buffer(self):
        pid = (self.live.identity() or {}).get('pid')
        if self._buf and self._buf[0] == pid:
            return self._buf[1]
        result = self.live.link.one('call', '401000', 'i', '0', '0', word(RAY_SLOTS * RAY_SLOT + 16))
        address = int(result['eax'], 16) if result.get('ok') else 0
        if not address:
            raise LiveError('NO_MEMORY', 'Could not allocate the ray buffer')
        self._buf = (pid, address)
        return address

    def cast(self, segments, layer=LAYER_PATH, refs=False):
        """[(start, end)] -> [{'hit', 'fraction', 'point', 'ref'}] (ref = hit reference pointer when refs)."""
        out = []
        for chunk in range(0, len(segments), RAY_SLOTS):
            out += self._cast(segments[chunk:chunk + RAY_SLOTS], layer, refs)
        return out

    def _cast(self, segments, layer, refs):
        base = (self.buffer() + 15) & ~15
        tes = self.live.u32s([G_TES])[0]
        ops = []
        for k, (a, b) in enumerate(segments):
            pick = base + k * RAY_SLOT
            pts = pick + 0xC0
            ops += [('wr', word(pick), '00' * 0xB0), ('wr', word(pts), struct.pack('<6f', *a, *b).hex()),
                    ('call', word(PICK_CTOR), 'i', word(pick), '0'),
                    ('call', word(PICK_FROM), 'i', word(pick), '0', word(pts)),
                    ('call', word(PICK_TO), 'i', word(pick), '0', word(pts + 12)),
                    ('call', word(PICK_FILTER), 'i', word(pick), '0', word(layer)),   # filter on the stack (ret 4)
                    ('call', word(TES_PICK), 'i', word(tes), '0', word(pick)),
                    ('rd', word(pick), word(0xB0))]
        replies = self.live.link.batch(ops, timeout=30)
        results = []
        for k, (a, b) in enumerate(segments):
            pick_reply, raw_reply = replies[8 * k + 6], replies[8 * k + 7]
            hit3d = int(pick_reply.get('eax', '0x0'), 16) if pick_reply.get('ok') else 0
            raw = bytes.fromhex(raw_reply['hex']) if raw_reply.get('ok') else b''
            collidable = _u32(raw, 0x80) if len(raw) >= 0x84 else 0
            throttled = bool(raw[0xAC]) if len(raw) > 0xAC else False
            hit = bool(hit3d or collidable)
            r = {'hit': hit, 'node': hit3d, 'throttled': throttled}
            if hit:
                frac = struct.unpack_from('<f', raw, 0x40)[0]
                if 0 <= frac <= 1:
                    r['fraction'] = frac
                    r['point'] = tuple(a[i] + (b[i] - a[i]) * frac for i in range(3))
            results.append(r)
        if refs:
            nodes = [r['node'] for r in results if r.get('node')]
            if nodes:
                found = self.live.link.batch([('call', word(FIND_REF_FOR_3D), 'i', '0', '0', word(n)) for n in nodes])
                by_node = {n: int(f.get('eax', '0x0'), 16) if f.get('ok') else 0 for n, f in zip(nodes, found)}
                for r in results:
                    if r.get('node'):
                        r['ref'] = by_node.get(r['node'])
        return results


class HandsTools:
    def __init__(self, live, agent=None, eyes=None, sleep=time.sleep, clock=time.monotonic):
        self.live = live
        self.agent = agent
        self.eyes = eyes
        self.sleep = sleep
        self.clock = clock
        self.rays = Rays(live)

    @property
    def link(self):
        return self.live.link

    # ================================================================ helpers
    def _player(self):
        p = self.live.u32s([PLAYER])[0]
        if not p:
            raise LiveError('NO_PLAYER', 'No player: load a game first')
        return p

    def _pos(self, ptr):
        return tuple(self.live.values(['GetPos X', 'GetPos Y', 'GetPos Z'], ptr))

    def _menus(self):
        return self.agent.menus() if self.agent is not None else {'inMenuMode': False, 'visible': []}

    def _new_message(self, before):
        """A HUD message that appeared since `before` (the message tile keeps old text, so compare the queue too)."""
        after = self._hud()
        new = [m for m in after.get('queuedMessages') or [] if m not in (before.get('queuedMessages') or [])]
        if new:
            return new[-1]
        if after.get('message') and after.get('message') != before.get('message'):
            return after['message']
        return None

    def _hud(self):
        if self.eyes is None:
            return {}
        try:
            return self.eyes.hud_read()
        except Exception:  # noqa: BLE001 - the HUD is evidence only
            return {}

    def _press(self, control, frames=1):
        return self.live.input('tap', control=control, frames=frames)

    def _face(self, target_point, pitch=True):
        """Turn the player (heading, and pitch when asked) toward a point."""
        player = self._player()
        eye = list(self._pos(player))
        eye[2] += 110.0
        yaw, pit = heading_to(eye, target_point)
        lines = ['player.SetAngle Z %.3f' % yaw]
        if pitch:
            lines.append('player.SetAngle X %.3f' % max(-80.0, min(80.0, pit)))
        for line in lines:
            self.live.console(line)
        return yaw, pit

    def _target(self, ref):
        """(resolved, aim point, floor point) for a reference: aim at its bound centre."""
        if self.eyes is not None:
            centre, radius, r = self.eyes.ref_bound(ref)
        else:
            r = self.live.resolve_ref(ref)
            x, y, z = self._pos(r['ptr'])
            centre, radius = (x, y, z + 60.0), 64.0
        floor = self._pos(r['ptr'])
        return r, centre, floor, radius

    def _crosshair(self):
        """The form id under the crosshair, or None."""
        c = self.live.crosshair()
        return ((c or {}).get('ref') or '').upper() or None

    # ================================================================ look_at / activate
    def look_at(self, ref):
        self.live.in_game()
        r, centre, _floor, _radius = self._target(ref)
        self.live.changing('play look_at')
        yaw, pitch = self._face(centre)
        self._frames(2)
        cross = self._crosshair()
        on = cross == (r['formId'] or '').upper()
        return {'target': r['formId'], 'yaw': round(yaw, 2), 'pitch': round(pitch, 2), 'crosshair': cross,
                'onTarget': on}

    def _frames(self, n):
        try:
            from .fnv_inject import shared_time
            _, waiter = shared_time(self.live)
            return waiter.run(frames=n, timeout_seconds=10)
        except Exception:  # noqa: BLE001 - fall back to wall time
            self.sleep(n / 30.0)
            return None

    def _aim(self, r, centre, radius):
        """Turn toward the target and try a few points of its bound until the crosshair picks it (things standing on
        a desk or in front of a counter take the pick at the centre). Returns the form id the crosshair picks."""
        want = (r['formId'] or '').upper()
        cx, cy, cz = centre
        k = min(radius * 0.45, 40.0)
        player = self._pos(self._player())
        dx, dy = cx - player[0], cy - player[1]
        n = math.hypot(dx, dy) or 1.0
        sx, sy = -dy / n * k, dx / n * k
        cross = None
        for p in ((cx, cy, cz), (cx, cy, cz - k), (cx + sx, cy + sy, cz - k * 0.5), (cx - sx, cy - sy, cz - k * 0.5),
                  (cx, cy, cz + k)):
            self._face(p)
            self._frames(2)
            cross = self._crosshair()
            if cross == want:
                break
        return cross

    def _refusals(self, r):
        """Why the player cannot activate a reference, from reads (Activate itself changes state)."""
        player = self._player()
        why = []
        menus = self._menus()
        if menus.get('inMenuMode'):
            why.append({'reason': 'menu_mode', 'detail': 'A menu is open: %s' % [m['name'] for m in menus['visible']
                                                                                 if m['name'] != 'HUDMainMenu']})
        flags = self.live.reads([(player + CONTROLS_DISABLED, 1)])[0]
        if flags and flags[0] & 1:
            why.append({'reason': 'controls_disabled', 'detail': 'Movement controls are disabled (player +0x680 = '
                                                                 '0x%02X), which also blocks activation' % flags[0]})
        head = self.live.form_ids([r['ptr']])[0] or {}
        if head.get('flags', 0) & 0x800:
            why.append({'reason': 'disabled', 'detail': 'The reference is disabled'})
        # Only reference functions here: actor functions (IsInCombat, GetDead...) on a container crash the game
        # (seen live, CrashLogger 2026-11-11 17:06).
        v = self.live.values(['GetLocked', 'GetLockLevel', 'GetBaseObject'], r['ptr'])
        v = [v[0], v[1], None, None, None, v[2]]
        pv = self.live.values(['IsInCombat', 'IsSneaking', 'GetAV Lockpick'], player)
        if v[0]:
            why.append({'reason': 'locked', 'detail': 'Locked (level %s); the player\'s Lockpick is %s. Opening it '
                                                      'needs the key or the lock-pick menu' % (v[1], pv[2])})
        if pv[0]:
            why.append({'reason': 'player_in_combat', 'detail': 'The player is in combat: some references refuse '
                                                                'activation then ("can\'t activate" message)'})
        base = self.live.resolve(v[5]) if v[5] else None
        btype = (self.live.form_ids([base['ptr']])[0] or {}).get('formType') if base else None
        if btype == DOOR_TYPE:
            name = self.live.values(['GetName'], r['ptr'])[0]
            if not name or name == '<no name>':
                why.append({'reason': 'unnamed_door', 'detail': 'A door with no name cannot be activated by the '
                                                                'player'})
        return why, btype

    def activate(self, ref, direct=False, wait_frames=20):
        self.live.in_game()
        r, centre, floor, radius = self._target(ref)
        before_menus = {m['id'] for m in self._menus().get('visible', [])}
        before_hud = self._hud()
        before_pos = self._pos(self._player())
        flags_before = (self.live.form_ids([r['ptr']])[0] or {}).get('flags')
        refusals, btype = self._refusals(r)
        self.live.changing('play activate')
        if direct:
            # Scheduled on the main loop, not run inside the request: activating an NPC starts dialogue, whose menu
            # loop would hold the request (and the pipe) until the conversation ends (seen live).
            sched = self.link.one('sched', '0', hexs('%s.Activate player' % r['formId']))
            if not sched.get('ok'):
                raise LiveError('COMPANION_REFUSED', 'Could not schedule the activation: %s' % sched.get('error'),
                                sched)
            result = None
            how = 'direct: Activate on the reference with the player as activator (no crosshair), run from the main loop'
        else:
            blocking = [w for w in refusals if w['reason'] in ('menu_mode', 'controls_disabled', 'disabled')]
            if blocking:
                return {'target': r['formId'], 'pressed': False, 'refused': blocking, 'also': refusals[len(blocking):],
                        'changed': False}
            cross = self._aim(r, centre, radius)
            if cross != (r['formId'] or '').upper():
                dist = math.dist(self._pos(self._player()), floor)
                return {'target': r['formId'], 'pressed': False, 'crosshair': cross,
                        'refused': [{'reason': 'not_in_crosshair', 'detail': 'After turning to it the crosshair '
                                     'picks %s; the target is %.0f units away (activation reach is about 150 units). '
                                     'Next: fnv_play goto, or direct=true.' % (cross, dist)}],
                        'changed': True}
            self._press('activate', 1)
            result = None
            how = 'pressed the activate control for one frame with the crosshair on the target'
        self._frames(wait_frames)
        after_menus = self._menus()
        opened = [m['name'] for m in after_menus.get('visible', []) if m['id'] not in before_menus]
        new_msg = self._new_message(before_hud)
        after_pos = self._pos(self._player())
        moved = math.dist(before_pos, after_pos) > 200
        happened = []
        if opened:
            happened.append('opened %s' % ', '.join(opened))
        if 'DialogMenu' in opened:
            happened.append('started dialogue')
        if moved:
            happened.append('the player was moved (load door or teleport)')
        if new_msg:
            happened.append('HUD message: %s' % new_msg)
        if btype == DOOR_TYPE and not moved:
            state = self.live.values(['GetOpenState'], r['ptr'])[0]
            happened.append('door open state %s (1 open, 2 opening, 3 closed, 4 closing)' % state)
        exists = self.live.values(['GetDisabled'], r['ptr'])[0]
        if exists:
            happened.append('the reference is now disabled (picked up or removed)')
        taken = pickup_note(flags_before, (self.live.form_ids([r['ptr']])[0] or {}).get('flags'))
        if taken:
            happened.append(taken)
        self.live.journal.add({'kind': 'play_activate', 'target': r['formId'], 'direct': direct, 'undo': None})
        out = {'target': r['formId'], 'pressed': not direct, 'how': how, 'happened': happened or ['nothing visible '
               'changed (an OnActivate block may have swallowed it)'], 'menusOpened': opened, 'hudMessage': new_msg,
               'refusalsSeen': [w for w in refusals if w['reason'] in ('locked', 'player_in_combat', 'unnamed_door')]}
        if result is not None:
            out['console'] = {'accepted': result.get('accepted'), 'output': result.get('output')}
        return out

    # ================================================================ goto
    def _floor(self, points):
        """Floor z under each (x, y, z guess): a ray down from above the guess."""
        segs = [((x, y, z + KNEE), (x, y, z - 300.0)) for x, y, z in points]
        hits = self.rays.cast(segs)
        return [h['point'][2] if h.get('point') else None for h in hits]

    def _wide(self, segs, door_ptrs=None):
        """For each segment: True when it is clear for the player's body (three parallel knee-high rays: the centre
        and HALF_WIDTH to each side); rays that hit only the door in door_ptrs[i] count as clear."""
        rays, owners = [], []
        for i, (a, b) in enumerate(segs):
            dx, dy = b[0] - a[0], b[1] - a[1]
            n = math.hypot(dx, dy) or 1.0
            ox, oy = -dy / n * HALF_WIDTH, dx / n * HALF_WIDTH
            # Side rays start part way along: at the start point they would begin inside the player's own body.
            t = min(0.4, 40.0 / n)
            s0 = (a[0] + dx * t, a[1] + dy * t, a[2] + (b[2] - a[2]) * t)
            for side in (0, -1, 1):
                origin = a if side == 0 else s0
                rays.append(((origin[0] + side * ox, origin[1] + side * oy, origin[2]),
                             (b[0] + side * ox, b[1] + side * oy, b[2])))
                owners.append(i)
        hits = self.rays.cast(rays, refs=bool(door_ptrs))
        clear = [True] * len(segs)
        for i, h in zip(owners, hits):
            door = door_ptrs[i] if door_ptrs else 0
            if h['hit'] and not (door and h.get('ref') == door):
                clear[i] = False
        return clear, len(rays)

    def plan(self, start, goal, doors, max_nodes=1500, avoid=()):
        """Weighted A* on a grid of STEP units: an edge is open when a knee-high ray between the two points hits
        nothing, or hits only a door (a door node). Returns (waypoints [(x, y, z, door_ref or 0)], stats)."""
        sx, sy, sz = start
        gx, gy, gz = goal

        def key(p):
            return (round((p[0] - sx) / STEP), round((p[1] - sy) / STEP))

        def point(k, z):
            return (sx + k[0] * STEP, sy + k[1] * STEP, z)

        door_nodes = {}
        for d in doors:
            door_nodes[key(d['position'])] = d
        start_k, goal_k = key(start), key(goal)
        z_of = {start_k: sz}
        g = {start_k: 0.0}
        parent = {start_k: None}
        heap = [(0.0, start_k)]
        expanded = rays = 0
        best = (math.dist(start[:2], goal[:2]), start_k)
        while heap and expanded < max_nodes:
            _, k = heapq.heappop(heap)
            if k == goal_k or math.dist(point(k, 0)[:2], goal[:2]) <= STEP:
                best = (0.0, k)
                break
            expanded += 1
            here = point(k, z_of[k])
            nbrs = [(k[0] + dx, k[1] + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
            nbrs = [n for n in nbrs if n not in g or g[n] > g[k] + STEP * 1.5]
            if not nbrs:
                continue
            guesses = [point(n, z_of[k]) for n in nbrs]
            floors = self._floor(guesses)
            segs, keep, door_ptrs = [], [], []
            for n, guess, fz in zip(nbrs, guesses, floors):
                if any(math.dist(guess[:2], a) < STEP * 0.75 for a in avoid):
                    continue
                # Some floors are not pickable from above (Prospector Saloon, seen live): no hit keeps the height.
                if fz is None:
                    fz = z_of[k]
                elif abs(fz - z_of[k]) > MAX_STEP:
                    continue
                segs.append(((here[0], here[1], here[2] + KNEE), (guess[0], guess[1], fz + KNEE)))
                keep.append((n, fz))
                door = door_nodes.get(n) or door_nodes.get(k)
                door_ptrs.append(door['ptr'] if door else 0)
            clear, used = self._wide(segs, door_ptrs) if segs else ([], 0)
            rays += len(nbrs) + used
            for (n, fz), ok in zip(keep, clear):
                if not ok:
                    continue
                cost = g[k] + STEP * math.hypot(n[0] - k[0], n[1] - k[1])
                if cost < g.get(n, 1e18):
                    g[n] = cost
                    z_of[n] = fz
                    parent[n] = k
                    h_cost = math.dist(point(n, 0)[:2], goal[:2])
                    heapq.heappush(heap, (cost + 1.4 * h_cost, n))
                    if h_cost < best[0]:
                        best = (h_cost, n)
        reached = best[0] <= STEP * 1.5
        path, k = [], best[1]
        while k is not None:
            d = door_nodes.get(k)
            path.append(point(k, z_of[k]) + ((d['ptr'] if d else 0),))
            k = parent[k]
        path.reverse()
        return path, {'expanded': expanded, 'rays': rays, 'reachedGoal': reached, 'leftOver': round(best[0])}

    def _simplify(self, path):
        """Drop waypoints a straight knee-high ray can skip (doors stay)."""
        if len(path) <= 2:
            return path
        out = [path[0]]
        i = 0
        while i < len(path) - 1:
            j = len(path) - 1
            candidates = list(range(j, i, -1))
            segs = [((path[i][0], path[i][1], path[i][2] + KNEE), (path[c][0], path[c][1], path[c][2] + KNEE))
                    for c in candidates]
            clear, _ = self._wide(segs)
            nxt = i + 1
            for c, ok in zip(candidates, clear):
                if any(p[3] for p in path[i + 1:c]):
                    continue
                if ok and abs(path[c][2] - path[i][2]) < 100:
                    nxt = c
                    break
            out.append(path[nxt])
            i = nxt
        return out

    def _doors(self, radius):
        player = self.live.resolve('player')
        result = self.link.eval('GetRefs %d 1 0 %.1f' % (DOOR_TYPE, float(radius)), player['ptr'])
        element = result.get('result') or {}
        items = [i for i in element.get('items', []) if i.get('type') == 'form' and i.get('ptr')]
        doors = []
        for i in items:
            ptr = int(i['ptr'], 16)
            x, y, z, linked = self.live.values(['GetPos X', 'GetPos Y', 'GetPos Z', 'GetLinkedDoor'], ptr)
            teleport = bool(linked) and linked not in ('00000000', 0)
            if x is None:
                continue
            doors.append({'ptr': ptr, 'formId': i.get('formId'), 'position': (x, y, z), 'loadDoor': bool(teleport)})
        return [d for d in doors if not d['loadDoor']]

    def goto(self, target, max_seconds=90.0, run=False):
        self.live.in_game()
        player = self._player()
        if isinstance(target, (list, tuple)):
            goal = tuple(float(v) for v in target)
            goal_ref = None
        else:
            r = self.live.resolve_ref(target)
            goal = self._pos(r['ptr'])
            goal_ref = r
        start = self._pos(player)
        floor_point = goal if goal_ref else None
        goal = (goal[0], goal[1], start[2])  # stand on the player's floor next to it (targets on tables count)

        def arrive_radius(at):
            return ref_arrive_radius(at, floor_point) if floor_point else ARRIVE
        menus = self._menus()
        if menus.get('inMenuMode'):
            raise LiveError('MENU_OPEN', 'A menu is open; close it first (fnv_ui_do close)')
        flags = self.live.reads([(player + CONTROLS_DISABLED, 1)])[0]
        if flags and flags[0] & 1:
            raise LiveError('CONTROLS_DISABLED', 'The player\'s movement controls are disabled (by a script)')
        if goal_ref and self.live.values(['GetInSameCell player'], goal_ref['ptr'])[0] == 0:
            raise LiveError('OTHER_CELL', 'The target is in another cell. goto walks within the loaded area; use a load '
                                          'door with activate, or fnv_move.')
        doors = self._doors(max(2000.0, math.dist(start, goal) * 1.5))
        started = self.clock()
        self.live.changing('play goto')
        used_doors, travelled, plans, routes, avoid, tried_doors = [], 0.0, [], [], [], set()
        via = None             # a door opened on the way: the next route goes through its centre
        stop_reason = None
        menus_waited = 0
        for attempt in range(MAX_REPLANS + 1):
            here = self._pos(player)
            if math.dist(here[:2], goal[:2]) <= arrive_radius(here):
                break
            if via is not None:
                # Through the doorway's centre first (the door frame catches a route that is off centre), then on.
                door_pt = (via[0], via[1], here[2], None)
                raw, stats = self.plan(door_pt[:3], goal, doors, avoid=avoid)
                stats = dict(stats, via='door centre')
                raw = [(here[0], here[1], here[2], None), door_pt] + list(raw[1:])
                via = None
            else:
                raw, stats = self.plan(here, goal, doors, avoid=avoid)
            plans.append(stats)
            if len(raw) < 2:
                stop_reason = 'no_path'
                break
            route = self._simplify(raw) if stats.get('via') is None else \
                raw[:2] + self._simplify(raw[1:])[1:]
            routes.append([[round(p[0]), round(p[1]), round(p[2])] for p in route])
            stop_reason = None
            for wp in route[1:]:
                if wp[3]:
                    used_doors.append(self._through_door(wp[3], doors))
                last_wp = wp is route[-1]
                best = math.dist(self._pos(player)[:2], wp[:2])
                no_gain = 0
                while True:
                    if self.clock() - started > max_seconds:
                        stop_reason = 'timeout'
                        break
                    pos = self._pos(player)
                    dist = math.dist(pos[:2], wp[:2])
                    if dist <= (arrive_radius(pos) if last_wp else 40.0):
                        break
                    self._face((wp[0], wp[1], pos[2] + 110.0), pitch=False)
                    # Hold only for the way to the stop point (inside the arrival radius, not the goal itself): a
                    # step held for the whole distance walked into the item at the goal and kicked it away (s1-play).
                    stop_at = arrive_radius(pos) * 0.7 if last_wp else 20.0
                    frames = max(3, min(30, int((dist - stop_at) / (12.0 if run else 4.0))))
                    self.live.input('hold', control='forward', frames=frames)
                    # Let this hold run out before measuring: holds queued on top of each other carried the player
                    # past the goal and into small items, kicking them away (s1-play).
                    self._frames(frames)
                    now = self._pos(player)
                    travelled += math.dist(pos[:2], now[:2])
                    gained = best - math.dist(now[:2], wp[:2])
                    if gained > 8.0:
                        best, no_gain = math.dist(now[:2], wp[:2]), 0
                    else:
                        no_gain += 1
                        if no_gain == 2:
                            # A closed door: path-pick rays pass through door collision (seen live in Prospector
                            # Saloon), so the plan does not know it is there. Open the door the player is facing.
                            door = self._door_ahead(now, wp, doors, tried_doors)
                            if door is not None:
                                tried_doors.add(door['ptr'])
                                used_doors.append(self._through_door(door['ptr'], doors))
                                via = door['position']
                                # Spots marked blocked at the closed door are open now.
                                avoid[:] = [a for a in avoid if math.dist(a, via[:2]) > DOOR_REACH]
                                stop_reason = 'door_opened'
                                break
                            self._press('jump', 1)
                        if no_gain >= 4:
                            # Remember the spot just ahead as an obstacle so the next plan goes around it.
                            d = math.dist(now[:2], wp[:2]) or 1.0
                            avoid.append((now[0] + (wp[0] - now[0]) / d * 40.0, now[1] + (wp[1] - now[1]) / d * 40.0))
                            stop_reason = 'blocked'
                            break
                    if self._menus().get('inMenuMode'):
                        # A message box that something closes (fnv_test steps closes them, as a player would click OK,
                        # seen live: the DLC "items added" boxes mid-walk) only interrupts the walk: wait for it,
                        # then walk on from where the player stands. A menu that stays open stops the walk.
                        if menus_waited < 5 and self._menu_closes(3.0):
                            menus_waited += 1
                            stop_reason = 'menu_closed'
                        else:
                            stop_reason = 'menu_opened'
                        break
                if stop_reason:
                    break
            if stop_reason in (None, 'timeout', 'menu_opened'):
                break
        if stop_reason in ('door_opened', 'menu_closed'):
            stop_reason = None
        final = self._settle()          # the walk's queued holds would carry the player on past the goal
        left = math.dist(final[:2], goal[:2])
        # A reference: inside the activation reach in 3D, so the next activate can pick it (N16). A point: close
        # across the floor.
        arrived = ref_arrived(final, floor_point) if floor_point else left <= ARRIVE * 1.5
        return {'arrived': arrived, 'distanceLeft': round(left), 'travelled': round(travelled),
                'seconds': round(self.clock() - started, 1), 'routes': routes, 'replans': max(0, len(plans) - 1), 'menusWaited': menus_waited,
                'doorsUsed': used_doors, 'stopped': stop_reason if not arrived else None, 'plans': plans,
                'how': 'route: weighted A* on a %d-unit grid, each step checked with three knee-high path-pick rays as '
                       'wide as the player (doors are passable); walking: the forward control through the input '
                       'injector, heading by SetAngle Z; replans from where the player stands when blocked' % STEP}

    @staticmethod
    def _door_ahead(here, wp, doors, tried, reach=DOOR_REACH):
        """The nearest door within reach that lies ahead of the player on the way to wp (not a load door, not tried
        on this walk)."""
        d = math.dist(here[:2], wp[:2]) or 1.0
        ux, uy = (wp[0] - here[0]) / d, (wp[1] - here[1]) / d
        best = None
        for door in doors:
            if door.get('loadDoor') or door['ptr'] in tried:
                continue
            px, py = door['position'][0] - here[0], door['position'][1] - here[1]
            dist = math.hypot(px, py)
            if dist > reach or px * ux + py * uy <= 0:
                continue
            if best is None or dist < best[0]:
                best = (dist, door)
        return best[1] if best else None

    def _through_door(self, door_ptr, doors):
        door = next((d for d in doors if d['ptr'] == door_ptr), None)
        state = self.live.values(['GetOpenState'], door_ptr)[0]
        form = self.live.form_ids([door_ptr])[0]['formId']
        if state in (3, 4):
            act = self.activate(form)
            self._frames(30)
            return {'formId': form, 'opened': act.get('happened'), 'refused': act.get('refused')}
        return {'formId': door['formId'] if door else form, 'opened': 'already open'}

    def _menu_closes(self, seconds):
        """True when menu mode ends within `seconds` (a message box closed by the runner or by itself)."""
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            time.sleep(0.1)
            if not self._menus().get('inMenuMode'):
                return True
        return False

    def talk(self, ref, max_seconds=60.0):
        out = {'goto': None}
        r, centre, floor, _ = self._target(ref)
        if math.dist(self._pos(self._player()), floor) > 140:
            out['goto'] = self.goto(ref, max_seconds=max_seconds)
        act = self.activate(ref)
        out['activate'] = act
        out['dialogue'] = 'DialogMenu' in (act.get('menusOpened') or [])
        if out['dialogue']:
            out['next'] = 'fnv_dialogue shows the lines; fnv_dialogue_choose picks a response.'
        return out

    def _clear_input(self):
        try:
            from .fnv_inject import Injector
            Injector.of(self.live).clear()
        except Exception:  # noqa: BLE001 - nothing queued
            pass

    def _settle(self, max_rounds=20):
        """Drop the input still queued (goto queues forward holds and does not wait for them) and wait until the
        player stands still: moved under 1 unit over two checks of 3 frames. Returns the position."""
        self._clear_input()
        player = self._player()
        last, still = self._pos(player), 0
        for _ in range(max_rounds):
            self._frames(3)
            now = self._pos(player)
            still = still + 1 if math.dist(now, last) < 1.0 else 0
            last = now
            if still >= 2:
                break
        return last

    def stop(self):
        self.live.ready()
        self._clear_input()
        return {'released': True}

    def play(self, action, ref=None, position=None, direct=False, max_seconds=90.0, run=False):
        if action == 'goto':
            if not ref and not position:
                raise ValueError('goto needs ref or position')
            return self.goto(position if position else ref, max_seconds, run)
        if action == 'look_at':
            return self.look_at(ref)
        if action == 'activate':
            return self.activate(ref, direct=direct)
        if action == 'talk':
            return self.talk(ref, max_seconds)
        if action == 'stop':
            return self.stop()
        if action in ('loot', 'equip', 'use', 'drop'):
            return self._item_action(action, ref, direct)
        raise ValueError('action is goto, look_at, activate, talk, loot, equip, use, drop or stop')

    def _item_action(self, action, item, direct):
        """Item actions through the engine's own script commands on the player (direct); the menu route is
        fnv_ui_do."""
        self.live.in_game()
        if not item:
            raise ValueError('%s needs ref (the item form, or for loot the container)' % action)
        r = self.live.resolve(item)
        self.live.changing('play %s' % action)
        if action == 'loot':
            line = 'RemoveAllItems player'
            result = self.live.console(line, r['ptr'])
        elif action == 'equip':
            result = self.live.console('player.EquipItem %s' % r['formId'])
        elif action == 'use':
            result = self.live.console('player.EquipItem %s' % r['formId'])
        else:
            result = self.live.console('player.Drop %s 1' % r['formId'])
        self.live.journal.add({'kind': 'play_item', 'action': action, 'form': r['formId'], 'undo': None})
        return {'action': action, 'form': r['formId'], 'accepted': result['accepted'], 'output': result['output'],
                'how': 'script command on the player (the menu route is fnv_ui_do)'}

    # ================================================================ fnv_ui_do
    def _menu_root(self, menu_id):
        raw = self.live.reads([(MENU_ARRAY + 4, 8)])[0]
        data, _, size = struct.unpack('<IHH', raw)
        idx = menu_id - 0x3E9
        return self.live.u32s([data + 4 * idx])[0] if data and 0 <= idx < size else 0

    def _top_menu(self):
        state = self._menus()
        active = (state.get('activeMenu') or {}).get('id')
        if active:
            return active
        stack = [m['id'] for m in state.get('menuStack', []) if m['id'] >= 1001 and m['id'] != 1004]
        return stack[-1] if stack else None

    def _tiles(self, root, depth=12, limit=1500):
        """[(tile, path)] breadth first."""
        out, level, total = [], [(root, '')], 0
        for _ in range(depth + 1):
            if not level or total >= limit:
                break
            ptrs = [t for t, _ in level]
            names = self.agent._tile_name(ptrs)
            nxt = []
            for t, parent in level:
                name = names.get(t) or '?'
                path = (parent + '/' + name) if parent else name
                out.append((t, path))
                total += 1
                nxt += [(c, path) for c in self.agent._children(t)]
            level = nxt
        return out

    def _click_tile(self, menu_id, root, tile):
        values = self.agent._tile_values([tile]).get(tile, {})
        tid = values.get(TRAIT_ID, (None, 0))[0]
        if tid is None:
            raise LiveError('NOT_CLICKABLE', 'That tile has no id trait (0xFAA): it is not a button')
        menu = self.live.u32s([root + TILEMENU_MENU])[0]
        vtable = self.live.u32s([menu])[0] if menu else 0
        handler = self.live.u32s([vtable + 0x0C])[0] if vtable else 0
        if not handler:
            raise LiveError('NO_MENU', 'The menu object has no click handler')
        self.live.changing('ui click')
        result = self.link.one('call', word(handler), 'v', word(menu), '0', word(int(tid) & 0xFFFFFFFF), word(tile))
        if not result.get('ok'):
            raise LiveError('CALL_REJECTED', 'The click call failed: %s' % result.get('error'), result)
        return int(tid)

    def click(self, target=None, label=None, menu=None):
        self.live.ready()
        menu_id = MENU_IDS.get(str(menu).lower()) if menu and not str(menu).isdigit() else (int(menu) if menu else None)
        menu_id = menu_id or self._top_menu()
        if not menu_id:
            raise LiveError('NO_MENU', 'No menu is open')
        root = self._menu_root(menu_id)
        if not root:
            raise LiveError('NO_MENU', 'Menu %s has no tiles now' % menu_id)
        tiles = self._tiles(root)
        found = None
        if target:
            want = target.lower()
            found = next((t for t in tiles if t[1].lower().endswith(want)), None) or \
                next((t for t in tiles if want in t[1].lower()), None)
        elif label:
            want = label.lower().strip()
            strings = self.agent._tile_strings([t for t, _ in tiles])
            values = self.agent._tile_values([t for t, _ in tiles])
            matches = [(t, p) for t, p in tiles if (strings.get(t) or '').lower().strip() == want]
            matches += [(t, p) for t, p in tiles if want in (strings.get(t) or '').lower() and (t, p) not in matches]
            # The button is the text tile or its nearest ancestor with an id.
            by_path = {p: t for t, p in tiles}
            for t, p in matches:
                parts = p.split('/')
                for cut in range(len(parts), 0, -1):
                    cand = by_path.get('/'.join(parts[:cut]))
                    if cand and TRAIT_ID in values.get(cand, {}):
                        found = (cand, '/'.join(parts[:cut]))
                        break
                if found:
                    break
        else:
            raise ValueError('click needs target (a tile path or name) or label (the text on the button)')
        if not found:
            raise LiveError('NO_SUCH_TILE', 'No tile %r in menu %s' % (target or label, menu_id),
                            {'tiles': [p for _, p in tiles][:80]})
        before = {m['id'] for m in self._menus().get('visible', [])}
        before_hud = self._hud()
        tid = self._click_tile(menu_id, root, found[0])
        self.sleep(0.3)
        after = self._menus()
        self.live.journal.add({'kind': 'ui_click', 'menu': menu_id, 'tile': found[1], 'undo': None})
        return {'menu': menu_id, 'clicked': found[1], 'id': tid,
                'menusOpened': [m['name'] for m in after.get('visible', []) if m['id'] not in before],
                'menusClosed': sorted(before - {m['id'] for m in after.get('visible', [])}),
                'hudMessage': self._new_message(before_hud),
                'how': 'the menu\'s HandleClick(id, tile) (vtable slot 3), as a mouse click does'}

    def fast_travel(self, marker):
        self.live.in_game()
        m = self.live.resolve(marker)
        player = self._player()
        before_hud = self._hud()
        self.live.changing('fast travel')
        can = self.link.one('call', word(CAN_FAST_TRAVEL), 'i', word(player), '0')
        if not can.get('ok'):
            raise LiveError('CALL_REJECTED', 'CanFastTravel failed: %s' % can.get('error'), can)
        if not int(can['eax'], 16) & 0xFF:
            self.sleep(0.2)
            return {'travelled': False, 'refused': True, 'reason': self._new_message(before_hud),
                    'how': 'PlayerCharacter::CanFastTravel 0x0093D660 refused and queued the reason'}
        before = self._pos(player)
        hours = self.live.values(['GameDaysPassed'])[0]
        result = self.link.one('call', word(FAST_TRAVEL), 'v', word(player), '0', word(m['ptr']), timeout=60)
        if not result.get('ok'):
            raise LiveError('CALL_REJECTED', 'FastTravel failed: %s' % result.get('error'), result)
        deadline = self.clock() + 60
        while self.clock() < deadline:
            self.sleep(0.5)
            try:
                if self.live.in_game().get('loaded'):
                    break
            except Exception:  # noqa: BLE001 - loading
                continue
        after = self._pos(player)
        days = self.live.values(['GameDaysPassed'])[0]
        self.live.journal.add({'kind': 'fast_travel', 'marker': m['formId'], 'undo': None})
        return {'travelled': math.dist(before, after) > 100, 'marker': m['formId'],
                'from': [round(v) for v in before], 'to': [round(v) for v in after],
                'hoursPassed': round((days - hours) * 24, 2) if days is not None and hours is not None else None,
                'how': 'CanFastTravel then PlayerCharacter::FastTravel(marker) 0x0093CDF0'}

    def wait(self, hours, sleep=False):
        """Open the wait (or sleep) menu as a player would; when it opens, pass the hours with the engine's
        one-hour wait and close it. A refusal comes back as the HUD message."""
        self.live.in_game()
        hours = int(hours)
        if not 1 <= hours <= 24:
            raise ValueError('hours is 1..24')
        self.live.changing('ui wait')
        if sleep:
            raise LiveError('NOT_BUILT', 'sleep needs a bed: activate one with fnv_play activate, then fnv_ui_do click')
        before = self._hud()
        self._press('wait', 4)      # one frame is too short for the wait key (seen live)
        self._frames(20)
        opened = any(m['id'] == 1012 for m in self._menus().get('visible', []))
        if not opened:
            return {'waited': False, 'refused': True, 'reason': self._new_message(before) or 'the wait menu did not '
                    'open', 'how': 'the wait control for four frames'}
        self.live.console('CloseAllMenus')
        self._frames(5)
        advanced = self.agent.time_control(advance_hours=hours, mode='engine_wait', allow_unproven=True)
        return {'waited': True, 'hours': hours, 'menuOpened': True, 'time': advanced,
                'how': 'the wait control opened SleepWaitMenu (so waiting is allowed here); the hours ran through the '
                       'engine\'s one-hour wait (fnv_time)'}

    def ui_do(self, action, target=None, label=None, menu=None, hours=None, marker=None, button=None):
        if action == 'click':
            return self.click(target, label, menu)
        if action == 'button':
            return self.click(None, label or target, menu)
        if action in ('wait', 'sleep'):
            return self.wait(hours or 1, sleep=action == 'sleep')
        if action == 'fast_travel':
            return self.fast_travel(marker or target)
        if action == 'message':
            return self.agent.menu_control('press', button=button if button is not None else label)
        if action in ('close', 'back'):
            return self.agent.menu_control('close', menu=menu)
        if action == 'type':
            return self.type_text(label if label is not None else target)
        raise ValueError('action is click, button, wait, sleep, fast_travel, message, close, back or type')

    def type_text(self, text):
        """Enter text in the game's text entry (TextEditMenu 1051) the way its Done button does: the text goes into
        the menu's own string (+0x34), then the menu's click handler 0x007E66A0 runs for the Done tile (id 1), which
        copies it to the text-entry result (0x011DAED8) and closes the menu. The menu that asked (the player name
        entry) takes the result on its next update. Other text fields of New Vegas are console text (fnv_console)."""
        if not text:
            raise LiveError('BAD_ARGUMENTS', 'type needs label (the text to enter).')
        self.live.in_game()
        menu = self.live.u32s([TEXT_EDIT_SINGLETON])[0]
        if not menu:
            raise LiveError('NO_TEXT_ENTRY', 'No text entry menu is open (TextEditMenu, 1051). New Vegas asks for text '
                                             'only when naming the character; console text goes through fnv_console.')
        data = str(text)[:60].encode('cp1252', 'replace')
        self.live.changing('ui type')
        buf = self.live.scratch(data + b'\0')          # game heap: the menu's string frees it with the menu
        self.live.link.write(menu + 0x34, struct.pack('<IHH', buf, len(data), len(data) + 1))
        self.live.engine_call('0x%08X' % TEXT_EDIT_CLICK, '0x%08X' % menu,
                              [{'type': 'int', 'value': 1}, {'type': 'int', 'value': 0}], 'void', 'thiscall', True)
        self._frames(5)
        still = bool(self.live.u32s([TEXT_EDIT_SINGLETON])[0])
        return {'typed': data.decode('cp1252', 'cp1252-bytes'), 'menu': '0x%08X' % menu, 'closed': not still,
                'how': 'the TextEditMenu string (+0x34) set, then its Done click (0x007E66A0, tile id 1): the result '
                       'goes to 0x011DAED8 and the menu that asked (player name entry) takes it on its next update'}
