"""Plan 2 wave 3, eyes: fnv_camera, fnv_look, fnv_photo, fnv_hud, fnv_visibility.

Facts checked in the game (1.4.0.525, 2026-11-11):
- World scene graph *(0x011DEB7C): +0xAC the NiCamera, +0xBC the world FOV (float, degrees).
- NiCamera: +0x68 world rotate (3x3, row-major; column 0 is the view direction, column 2 points right),
  +0x8C world position, +0x9C world-to-clip 4x4 (row 0 = x, row 1 = y, row 3 = w), +0xDC frustum
  (left, right, top, bottom, near, far).
- Fly camera: OSGlobals *(0x011DEA0C) +6 is the fly flag (console tfc toggles it). The pose is on the player:
  +0x7E0 yaw (radians, 0 = north, clockwise), +0x7E4 pitch (radians, positive looks down), +0x7E8 position.
  Writing the pose moves the view on the next frame and SaveScreenshot captures it.
- A reference's 3D root: ref +0x64 -> +0x14. NiAVObject: +0x20 pointer to the world bound sphere (centre,
  radius), +0x30 flags (bit 0 app-culled), +0x8C world position, +0x98 world scale.
- HUDMainMenu *(0x011D96C0): +0x18C message queue (list of nodes, text at +0), +0x1B8 crosshair ref, +0x1BC
  visibility flags, +0x1C4 HUD mode; tiles at +0x28 + 4n (26 message text, 28 subtitles, 29 prompt, 32 name).
  QueueUIMessage 0x007052F0 (cdecl text, emotion, icon, sound, seconds, next) posts a corner message.
"""
import math
import re
import struct
import threading
import time
import uuid
from functools import wraps
from pathlib import Path

from .fnv_link import word
from .fnv_live import LiveError
from .safe_paths import inside, write_roots

SCENE_GRAPH = 0x011DEB7C
PLAYER = 0x011DEA3C
OS_GLOBALS = 0x011DEA0C
HUD = 0x011D96C0
QUEUE_UI_MESSAGE = 0x007052F0
SG_CAMERA, SG_FOV = 0xAC, 0xBC
CAM_ROT, CAM_POS, CAM_CLIP, CAM_FRUSTUM = 0x68, 0x8C, 0x9C, 0xDC
FLY_FLAG = 6
FLY_POSE = 0x7E0            # yaw, pitch, x, y, z
THIRD_PERSON = 0x64A
REF_RENDER, RENDER_ROOT = 0x64, 0x14
NODE_BOUND, NODE_FLAGS, NODE_POS, NODE_SCALE = 0x20, 0x30, 0x8C, 0x98
REF_BASE, REF_SCALE = 0x20, 0x40
HUD_QUEUE, HUD_CROSSHAIR, HUD_FLAGS, HUD_MODE = 0x18C, 0x1B8, 0x1BC, 0x1C4
HUD_TILES = 0x28
HUD_TILE = {'message': 26, 'subtitle': 28, 'prompt': 29, 'name': 32}
HUD_FLAG_NAMES = {0x1: 'ap', 0x2: 'hp', 0x4: 'radiation', 0x8: 'enemy_health', 0x10: 'quest_reminder',
                  0x20: 'region', 0x40: 'reticle', 0x80: 'sneak', 0x100: 'messages', 0x200: 'info',
                  0x400: 'subtitles', 0x800: 'hotkeys', 0x1000: 'xp', 0x2000: 'breath', 0x4000: 'explosive',
                  0x8000: 'limbs', 0x10000: 'hardcore'}
HUD_MODES = {1: 'changed', 2: 'normal', 3: 'pipboy', 4: 'start', 5: 'load', 6: 'dialogue', 7: 'vats',
             8: 'vats_playback', 9: 'container', 12: 'movement_disabled', 15: 'hacking', 16: 'terminal',
             17: 'message', 22: 'iron_sights'}
FORM_DISABLED, FORM_DELETED = 0x800, 0x20
# Base form types for fnv_look type filters (TESForm +4).
TYPE_NAMES = {21: 'ACTI', 22: 'TACT', 23: 'TERM', 24: 'ARMO', 25: 'BOOK', 26: 'CLOT', 27: 'CONT', 28: 'DOOR',
              29: 'INGR', 30: 'LIGH', 31: 'MISC', 32: 'STAT', 33: 'SCOL', 34: 'MSTT', 35: 'PWAT', 36: 'GRAS',
              37: 'TREE', 38: 'FLOR', 39: 'FURN', 40: 'WEAP', 41: 'AMMO', 42: 'NPC_', 43: 'CREA', 46: 'KEYM',
              47: 'ALCH', 49: 'NOTE'}
TYPE_GROUPS = {'actors': {42, 43}, 'doors': {28}, 'containers': {27}, 'activators': {21, 22, 23},
               'items': {24, 25, 26, 29, 31, 40, 41, 46, 47, 49}, 'furniture': {39},
               'statics': {32, 33, 34, 35, 36, 37, 38}, 'lights': {30}}
DEFAULT_SKIP = TYPE_GROUPS['statics'] | TYPE_GROUPS['lights']
# byte: non-zero while the HUD and menus are SHOWN; tm turns it to 0. Seen live 2026-11-12: flag 1 -> 0 printed
# "Menus -> Off", 0 -> 1 printed "Menus -> On" (N20: it was read the other way round). See fnv_render.TOGGLES.
MENUS_SHOWN_FLAG = 0x0118C6F0
# There is one fly camera, one saved state and one HUD toggle: every camera use (set, orbit, pov, restore and whole
# photo runs) holds this lock, so two calls cannot interleave their moves, captures and restores.
_CAMERA_LOCK = threading.RLock()


def _locked(fn):
    @wraps(fn)
    def inner(*args, **kw):
        with _CAMERA_LOCK:
            return fn(*args, **kw)
    return inner


def _uniq():
    """A per-call token for capture file names (the agent stamps only whole seconds)."""
    return '%d%s' % (int(time.time() * 1000) % 100000000, uuid.uuid4().hex[:4])


PHOTO_ANGLES = {'front': (180.0, 10.0), 'three_quarter': (135.0, 15.0), 'side': (90.0, 10.0),
                'back': (0.0, 10.0), 'top': (180.0, 80.0)}


def _f(raw, offset, n=1):
    v = struct.unpack_from('<%df' % n, raw, offset)
    return v if n > 1 else v[0]


def _r(value, digits=1):
    return None if value is None else round(value, digits)


def _u32(raw, offset=0):
    return struct.unpack_from('<I', raw, offset)[0]


G_TES = 0x011DEA10
CELL_REFS = 0xAC            # TESObjectCELL reference list (checked live: 491 refs in Prospector Saloon)


def attached_cells(live):
    """Pointers of the loaded cells: the player's interior, or the exterior grid (TES +0x34, +0x08)."""
    tes = live.u32s([G_TES])[0]
    if not tes:
        return []
    raw = live.reads([(tes, 0x40)])[0]
    if not raw:
        return []
    grid, interior = struct.unpack_from('<I', raw, 0x08)[0], struct.unpack_from('<I', raw, 0x34)[0]
    if interior:
        return [interior]
    if not grid:
        return []
    g = live.reads([(grid, 0x14)])[0]
    dim, arr = struct.unpack_from('<I', g, 0x0C)[0], struct.unpack_from('<I', g, 0x10)[0]
    if not arr or not 0 < dim <= 15:
        return []
    return [c for c in live.u32s([arr + 4 * i for i in range(dim * dim)]) if c]


def cell_refs(live, limit=6000):
    """Every reference of the loaded cells, from the cells' own lists. xNVSE GetRefs caps at 200 per call and no
    longer lists a reference after Disable and Enable (both seen live)."""
    out, seen = [], set()
    for cell in attached_cells(live):
        for ptr in live.walk_list(cell + CELL_REFS, limit):
            if ptr not in seen:
                seen.add(ptr)
                out.append(ptr)
    return out


def heading_to(src, dst):
    """(yaw, pitch) in degrees from src looking at dst: yaw 0 = north (+y), clockwise; pitch positive looks down."""
    dx, dy, dz = dst[0] - src[0], dst[1] - src[1], dst[2] - src[2]
    yaw = math.degrees(math.atan2(dx, dy)) % 360.0
    pitch = -math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return yaw, pitch


def direction(yaw, pitch):
    """Unit view vector for a yaw and pitch in degrees (pitch positive looks down)."""
    y, p = math.radians(yaw), math.radians(pitch)
    return (math.sin(y) * math.cos(p), math.cos(y) * math.cos(p), -math.sin(p))


class Camera:
    """The camera as numbers, read once: position, view direction, FOV and the world-to-clip matrix."""

    def __init__(self, position, rotate, clip, frustum, fov, width, height):
        self.position = position
        self.rotate = rotate
        self.clip = clip
        self.frustum = frustum
        self.fov = fov
        self.width = width
        self.height = height

    @property
    def forward(self):
        r = self.rotate
        return (r[0], r[3], r[6])

    def angles(self):
        fx, fy, fz = self.forward
        return math.degrees(math.atan2(fx, fy)) % 360.0, -math.degrees(math.asin(max(-1.0, min(1.0, fz))))

    def project(self, point):
        """(u, v, depth): u, v in 0..1 from the top left, or None when the point is behind the camera."""
        m = self.clip
        x = m[0] * point[0] + m[1] * point[1] + m[2] * point[2] + m[3]
        y = m[4] * point[0] + m[5] * point[1] + m[6] * point[2] + m[7]
        w = m[12] * point[0] + m[13] * point[1] + m[14] * point[2] + m[15]
        if w <= self.frustum[4] * 0.5:
            return None
        return (x / w + 1) / 2, (1 - y / w) / 2, w

    def rect(self, centre, radius):
        """Screen rectangle in pixels of a bound sphere (corners of its box), clipped to the screen, or None."""
        pts = []
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    p = self.project((centre[0] + sx * radius, centre[1] + sy * radius, centre[2] + sz * radius))
                    if p:
                        pts.append(p)
        middle = self.project(centre)
        if not pts or middle is None:
            return None
        u0, u1 = min(p[0] for p in pts), max(p[0] for p in pts)
        v0, v1 = min(p[1] for p in pts), max(p[1] for p in pts)
        if u1 < 0 or v1 < 0 or u0 > 1 or v0 > 1:
            return {'onScreen': False, 'depth': middle[2]}
        cu0, cu1, cv0, cv1 = max(0.0, u0), min(1.0, u1), max(0.0, v0), min(1.0, v1)
        return {'onScreen': True, 'depth': middle[2],
                'pixels': [round(cu0 * self.width), round(cv0 * self.height), round(cu1 * self.width),
                           round(cv1 * self.height)],
                'centre': [round(middle[0] * self.width), round(middle[1] * self.height)],
                'area': (cu1 - cu0) * (cv1 - cv0), 'clipped': (cu0, cu1, cv0, cv1) != (u0, u1, v0, v1)}


def fly_on(live):
    """The fly camera flag (OSGlobals +6), read now."""
    osg = live.u32s([OS_GLOBALS])[0]
    flag = live.reads([(osg + FLY_FLAG, 1)])[0] if osg else None
    return bool(flag[0]) if flag else None      # None: unreadable, not "off"


def hud_hidden(live):
    """HUD and menus hidden by tm, read now."""
    flag = live.reads([(MENUS_SHOWN_FLAG, 1)])[0]
    return (not flag[0]) if flag else None      # None: unreadable


class EyesTools:
    def __init__(self, live, agent=None, engine_ai=None, sleep=time.sleep):
        self.live = live
        self.agent = agent
        self.engine_ai = engine_ai
        self.sleep = sleep
        self.saved = None          # camera state before the first change: {'pid', 'fly', 'pose', 'fov'}
        self._screen = None

    @property
    def link(self):
        return self.live.link

    # ================================================================ reads
    def _player(self):
        p = self.live.u32s([PLAYER])[0]
        if not p:
            raise LiveError('NO_PLAYER', 'The player does not exist yet: load a game first')
        return p

    def screen_size(self):
        pid = (self.live.identity() or {}).get('pid')
        if self._screen and self._screen[0] == pid:
            return self._screen[1]
        w, h = self.live.values(['GetNumericIniSetting "iSize W:Display"', 'GetNumericIniSetting "iSize H:Display"'])
        size = (int(w), int(h)) if w and h else (1280, 720)
        self._screen = (pid, size)
        return size

    def camera(self):
        sg = self.live.u32s([SCENE_GRAPH])[0]
        if not sg:
            raise LiveError('NO_CAMERA', 'The world scene graph does not exist yet: load a game first')
        raw_sg = self.live.reads([(sg + SG_CAMERA, 0x14)])[0]
        cam = _u32(raw_sg, 0)
        if not cam:
            raise LiveError('NO_CAMERA', 'The world scene graph has no camera')
        raw = self.live.reads([(cam + CAM_ROT, 0x90)])[0]
        rotate = _f(raw, 0, 9)
        position = _f(raw, CAM_POS - CAM_ROT, 3)
        clip = _f(raw, CAM_CLIP - CAM_ROT, 16)
        frustum = _f(raw, CAM_FRUSTUM - CAM_ROT, 6)
        width, height = self.screen_size()
        return Camera(position, rotate, clip, frustum, _f(raw_sg, SG_FOV - SG_CAMERA), width, height)

    def _saved_undone(self):
        """The saved state's journal entry was undone (fnv_undo / fnv_revert_all ran its undo): the camera is no
        longer ours to restore, and the next change needs a new entry (N3)."""
        jid = (self.saved or {}).get('journalId')
        get = getattr(self.live.journal, 'get', None)
        if not jid or get is None:
            return False
        e = get(jid)
        return bool(e and e.get('undone'))

    def _mark_open_camera_entries_undone(self):
        """restore() with no saved state (a new server after a reload) puts the camera back itself: the earlier
        server's camera entries for this game must not run again (R7)."""
        ident = getattr(self.live, 'game_identity', None)
        try:
            entries = self.live.journal.entries(process=ident()) if ident else []
            ids = [e['id'] for e in entries if e.get('kind') == 'camera' and e.get('undo') and not e['undone']]
            if ids:
                self.live.journal.mark_undone(ids)
        except Exception:  # noqa: BLE001 - the journal is a record; a failed mark must not block the restore
            pass

    def fly_state(self):
        osg, player = self.live.u32s([OS_GLOBALS, PLAYER])
        flag = self.live.reads([(osg + FLY_FLAG, 1)])[0] if osg else None
        pose = self.live.reads([(player + FLY_POSE, 20)])[0] if player else None
        third = self.live.reads([(player + THIRD_PERSON, 1)])[0] if player else None
        out = {'fly': bool(flag and flag[0]), 'thirdPerson': bool(third and third[0])}
        if pose:
            yaw, pitch, x, y, z = struct.unpack('<5f', pose)
            out['pose'] = {'yaw': math.degrees(yaw) % 360.0, 'pitch': math.degrees(pitch), 'position': [x, y, z]}
        return out

    def get(self):
        self.live.in_game()
        cam = self.camera()
        yaw, pitch = cam.angles()
        fly = self.fly_state()
        return {'position': [_r(v) for v in cam.position], 'yaw': _r(yaw, 2), 'pitch': _r(pitch, 2),
                'fov': _r(cam.fov, 2), 'firstPerson': not fly['thirdPerson'], 'fly': fly['fly'],
                'screen': [cam.width, cam.height], 'savedByUs': self.saved is not None, 'changed': False,
                'uses': ['scene graph 0x011DEB7C camera +0xAC (checked live)', 'NiCamera world transform',
                         'OSGlobals +6 fly flag', 'player +0x64A third person']}

    # ================================================================ camera control
    def _remember(self):
        pid = (self.live.identity() or {}).get('pid')
        if self.saved and self.saved.get('pid') == pid and not self._saved_undone():
            return
        state = self.fly_state()
        self.saved = {'pid': pid, 'fly': state['fly'], 'pose': state.get('pose'), 'fov': self.camera().fov}
        self.live.changing('camera')
        # The undo sets the fly camera off; it reads the flag first, so it never toggles blind (R7).
        entry = self.live.journal.add({'kind': 'camera', 'saved': self.saved,
                                       'undo': {'op': 'fly_set', 'on': state['fly'],
                                                'fov': round(self.saved['fov'], 2) if self.saved['fov'] else None}})
        self.saved['journalId'] = (entry or {}).get('id')

    def _mark_camera_undone(self, saved):
        """restore() puts the camera back itself, so the journal's blind `tfc` undo must not run again (it would
        turn the fly camera ON)."""
        jid = (saved or {}).get('journalId')
        mark = getattr(self.live.journal, 'mark_undone', None)
        if jid and mark:
            try:
                mark([jid])
            except Exception:  # noqa: BLE001 - the journal is a record; a failed mark must not block the restore
                pass

    def _fly(self, on, freeze=False):
        state = self.fly_state()
        if state['fly'] != bool(on):
            self.live.console('tfc 1' if on and freeze else 'tfc')
            if self.fly_state()['fly'] != bool(on):
                raise LiveError('FLY_TOGGLE_FAILED', 'The fly camera did not turn %s' % ('on' if on else 'off'))

    def _frames(self, n=2):
        """Let n frames draw (the pose and the 3D update on the next frame)."""
        start = self.link.ping().get('frame') or 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            frame = self.link.ping().get('frame') or 0
            if frame >= start + n:
                return frame
            self.sleep(0.02)
        return None

    def _write_pose(self, position, yaw, pitch):
        player = self._player()
        data = struct.pack('<5f', math.radians(yaw), math.radians(pitch), *position)
        result = self.link.one('wr', word(player + FLY_POSE), data.hex())
        if not result.get('ok'):
            raise LiveError('MEMORY_UNWRITABLE', 'Cannot write the fly pose (%s)' % result.get('error'), result)

    def _set_fov(self, fov):
        if fov is None:
            return
        if not 10 <= float(fov) <= 150:
            raise ValueError('fov is 10..150 degrees')
        self.live.console('fov %s' % round(float(fov), 2))

    def ref_bound(self, ref):
        """(centre, radius, ptr) of a reference's 3D bound; falls back to its position plus a default size."""
        r = self.live.resolve_ref(ref)
        root = self._root(r['ptr'])
        if root:
            bound = self.live.u32s([root + NODE_BOUND])[0]
            raw = self.live.reads([(bound, 16)])[0] if bound else None
            if raw:
                cx, cy, cz, radius = struct.unpack('<4f', raw)
                if radius > 0:
                    return (cx, cy, cz), radius, r
        x, y, z = self.live.values(['GetPos X', 'GetPos Y', 'GetPos Z'], r['ptr'])
        return (x, y, z + 60.0), 64.0, r

    def _root(self, ptr):
        render = self.live.u32s([ptr + REF_RENDER])[0]
        return self.live.u32s([render + RENDER_ROOT])[0] if render else 0

    @_locked
    def set(self, position=None, yaw=None, pitch=None, look_at=None, fov=None, freeze=True):
        self.live.in_game()
        cam = self.camera()
        cur_yaw, cur_pitch = cam.angles()
        pos = tuple(float(v) for v in position) if position else cam.position
        if look_at:
            centre, _radius, _r_ = self.ref_bound(look_at)
            yaw, pitch = heading_to(pos, centre)
        yaw = cur_yaw if yaw is None else float(yaw)
        pitch = cur_pitch if pitch is None else float(pitch)
        if not -89.9 <= pitch <= 89.9:
            raise ValueError('pitch is -89.9..89.9 degrees (positive looks down)')
        self._remember()
        self._fly(True, freeze)
        self._write_pose(pos, yaw, pitch)
        self._set_fov(fov)
        self._frames(2)
        out = self.get()
        out.update(changed=True, note='The camera is free (fly mode). restore returns the player\'s view. Cells load '
                                      'around the player, not the camera.')
        return out

    @_locked
    def orbit(self, ref, yaw=None, pitch=15.0, distance=None, fit=False, freeze=True, relative=True):
        """Place the camera around a reference looking at its bound centre. yaw is relative to the reference's
        heading when relative (0 = behind it, 180 = in front of it), else absolute."""
        self.live.in_game()
        centre, radius, r = self.ref_bound(ref)
        heading = self.live.values(['GetAngle Z'], r['ptr'])[0] or 0.0 if relative else 0.0
        cam_yaw = ((heading or 0.0) + (yaw if yaw is not None else 180.0)) % 360.0
        if fit or distance is None:
            fov = self.camera().fov
            width, height = self.screen_size()
            half_h = math.radians(fov) / 2
            half_v = math.atan(math.tan(half_h) * height / width)
            distance = radius / math.sin(min(half_h, half_v)) * 1.1
        distance = max(float(distance), radius * 1.05 + 10)
        d = direction(cam_yaw, pitch)
        pos = (centre[0] - d[0] * distance, centre[1] - d[1] * distance, centre[2] - d[2] * distance)
        out = self.set(position=pos, yaw=cam_yaw, pitch=pitch, freeze=freeze)
        out.update(target={'formId': r['formId'], 'centre': [_r(v) for v in centre], 'radius': _r(radius)},
                   distance=_r(distance))
        return out

    @_locked
    def pov(self, view):
        self.live.in_game()
        if view not in ('first', 'third'):
            raise ValueError('pov is first or third')
        state = self.fly_state()
        if state['fly']:
            raise LiveError('FLY_ON', 'The camera is in fly mode: call restore first')
        if state['thirdPerson'] == (view == 'third'):
            return {'view': view, 'changed': False}
        self.live.changing('camera pov')
        self.live.input('tap', control='pov', frames=4)     # a one-frame tap does not switch (seen live)
        self._frames(10)
        after = self.fly_state()['thirdPerson']
        return {'view': 'third' if after else 'first', 'switched': after == (view == 'third'),
                'how': 'the POV control for four frames through the input injector'}

    @_locked
    def restore(self):
        self.live.ready()
        if not self.saved:
            self._mark_open_camera_entries_undone()
            state = self.fly_state()
            if state['fly']:
                self.live.console('tfc')
                return {'restored': True, 'note': 'No saved state: turned fly mode off.'}
            return {'restored': False, 'note': 'Nothing to restore: the camera was not changed by this tool.',
                    'changed': False}
        saved, self.saved = self.saved, None
        self._mark_camera_undone(saved)
        if saved.get('pid') != (self.live.identity() or {}).get('pid'):
            return {'restored': False, 'note': 'The game restarted since the change; nothing to restore.',
                    'changed': False}
        if saved['fly']:
            self._fly(True)
            pose = saved.get('pose')
            if pose:
                self._write_pose(pose['position'], pose['yaw'], pose['pitch'])
        else:
            self._fly(False)
        if saved.get('fov') and abs(self.camera().fov - saved['fov']) > 0.01:
            self._set_fov(saved['fov'])
        self._frames(2)
        out = self.get()
        out.update(restored=True, changed=True)
        return out

    def camera_tool(self, action, **kw):
        if action == 'get':
            return self.get()
        if action == 'set':
            return self.set(kw.get('position'), kw.get('yaw'), kw.get('pitch'), kw.get('look_at'), kw.get('fov'),
                            kw.get('freeze', True))
        if action == 'orbit':
            if not kw.get('ref'):
                raise ValueError('orbit needs ref')
            return self.orbit(kw['ref'], kw.get('yaw'), kw.get('pitch') if kw.get('pitch') is not None else 15.0,
                              kw.get('distance'), kw.get('fit', False), kw.get('freeze', True))
        if action == 'pov':
            return self.pov(kw.get('view'))
        if action == 'restore':
            return self.restore()
        raise ValueError('action is get, set, orbit, pov or restore')

    # ================================================================ fnv_look
    def _refs(self, radius, types=None):
        """Reference pointers around the player, asked per base form type (GetRefs returns at most 200 per call)."""
        player = self.live.resolve('player')
        types = sorted(types or TYPE_NAMES)
        results = self.link.batch([('eval', fnv_hexs('GetRefs %d 1 0 %.1f' % (t, float(radius))), word(player['ptr']))
                                   for t in types])
        out, seen = [], set()
        for result in results:
            element = result.get('result') or {}
            if not result.get('ok') or element.get('type') != 'array':
                continue
            for i in element.get('items', []):
                if i.get('type') == 'form' and i.get('ptr') and i['ptr'] not in seen:
                    seen.add(i['ptr'])
                    out.append(int(i['ptr'], 16))
        return out

    def _bounds(self, ptrs):
        """{ptr: info} with base, flags, bound centre and radius, culled flag for references with 3D."""
        heads = dict(zip(ptrs, self.live.reads([(p, 0x68) for p in ptrs]))) if ptrs else {}
        renders = {p: _u32(h, REF_RENDER) for p, h in heads.items() if h}
        roots = dict(zip(renders, self.live.u32s([r + RENDER_ROOT if r else 0 for r in renders.values()])))
        live_roots = {p: r for p, r in roots.items() if r}
        nodes = dict(zip(live_roots, self.live.reads([(r, 0x34) for r in live_roots.values()]))) if live_roots else {}
        spheres = {p: _u32(n, NODE_BOUND) for p, n in nodes.items() if n}
        sraw = dict(zip(spheres, self.live.reads([(s, 16) if s else (0, 0) for s in spheres.values()]))) \
            if spheres else {}
        bases = {p: _u32(h, REF_BASE) for p, h in heads.items() if h}
        btypes = dict(zip(bases, self.live.reads([(b + 4, 1) if b else (0, 0) for b in bases.values()]))) \
            if bases else {}
        out = {}
        for p, h in heads.items():
            if not h:
                continue
            flags = _u32(h, 8)
            info = {'ptr': p, 'formId': '%08X' % _u32(h, 12), 'flags': flags,
                    'disabled': bool(flags & FORM_DISABLED), 'deleted': bool(flags & FORM_DELETED),
                    'base': bases.get(p), 'baseType': (btypes.get(p) or b'\0')[0], 'has3D': p in live_roots}
            raw = sraw.get(p)
            if raw and len(raw) == 16:
                cx, cy, cz, radius = struct.unpack('<4f', raw)
                info.update(centre=(cx, cy, cz), radius=radius)
            node = nodes.get(p)
            if node:
                info['culled'] = bool(_u32(node, NODE_FLAGS) & 1)
            out[p] = info
        return out

    def look(self, types=None, limit=20, radius=6000.0, fields=None, rays=40, include_hidden=False):
        self.live.in_game()
        cam = self.camera()
        ptrs = cell_refs(self.live) or self._refs(radius)
        player = self._player()
        ptrs = [p for p in ptrs if p != player]
        info = self._bounds(ptrs)
        wanted = set()
        for t in types or []:
            if t not in TYPE_GROUPS:
                raise ValueError('types: %s' % ', '.join(sorted(TYPE_GROUPS)))
            wanted |= TYPE_GROUPS[t]
        seen = []
        for p, i in info.items():
            if (wanted and i['baseType'] not in wanted) or (not wanted and i['baseType'] in DEFAULT_SKIP):
                continue
            if 'centre' not in i or i.get('radius', 0) <= 0:
                continue
            if math.dist(cam.position, i['centre']) > radius:
                continue
            rect = cam.rect(i['centre'], i['radius'])
            if not rect or not rect.get('onScreen'):
                continue
            hidden = i['disabled'] or i.get('culled')
            if hidden and not include_hidden:
                continue
            i['rect'] = rect
            seen.append(i)
        seen.sort(key=lambda i: -i['rect']['area'])
        seen = seen[:limit]
        # Names and distances in one batch.
        names = {}
        if seen:
            exprs = []
            for i in seen:
                exprs += [('GetName', i['ptr']), ('GetDistance player', i['ptr'])]
            values = [r for r in self.link.batch([('eval', fnv_hexs(e), word(p)) for e, p in exprs])]
            from .fnv_link import value_of
            for k, i in enumerate(seen):
                names[i['ptr']] = (value_of(values[2 * k]), value_of(values[2 * k + 1]))
        los_budget = int(rays)
        out = []
        for i in seen:
            name, dist = names.get(i['ptr'], (None, None))
            los = 'unknown'
            if los_budget > 0 and self.engine_ai is not None:
                los_budget -= 1
                los = self._line_of_sight(cam.position, i)
            entry = {'formId': i['formId'], 'name': name if name and name != '<no name>' else None,
                     'type': TYPE_NAMES.get(i['baseType'], i['baseType']),
                     'base': self.live.form_ids([i['base']])[0]['formId'] if i['base'] else None,
                     'distance': _r(dist), 'rect': i['rect']['pixels'], 'centre': i['rect']['centre'],
                     'screenShare': round(i['rect']['area'], 4), 'inFrustum': True, 'lineOfSight': los,
                     'flags': [k for k, v in (('disabled', i['disabled']), ('culled', i.get('culled')),
                                              ('clipped', i['rect']['clipped'])) if v]}
            if fields:
                entry = {k: v for k, v in entry.items() if k in fields or k == 'formId'}
            out.append(entry)
        hud = self.hud_read(brief=True)
        menus = None
        if self.agent is not None:
            try:
                menus = [m['name'] for m in self.agent.menus()['visible']]
            except Exception:  # noqa: BLE001 - the menu list is extra
                menus = None
        yaw, pitch = cam.angles()
        return {'camera': {'position': [_r(v) for v in cam.position], 'yaw': _r(yaw, 2), 'pitch': _r(pitch, 2),
                           'fov': _r(cam.fov, 2), 'screen': [cam.width, cam.height]},
                'refs': out, 'count': len(out), 'crosshair': hud.get('crosshair'), 'prompt': hud.get('prompt'),
                'message': hud.get('message'), 'subtitle': hud.get('subtitle'), 'menus': menus, 'changed': False,
                'note': 'Rectangles come from each reference\'s 3D bound sphere projected with the camera matrix (the '
                        'engine keeps no list of what it drew). lineOfSight is a ray from the camera to the bound '
                        'centre; unknown when the ray budget ran out.',
                'uses': ['NiCamera world-to-clip matrix +0x9C (checked live)', 'NiAVObject world bound +0x20',
                         'xNVSE GetRefs', 'TES::Pick ray (fnv_raycast)']}

    def _line_of_sight(self, origin, info):
        try:
            c = info['centre']
            ray = self.engine_ai.raycast(start={'x': origin[0], 'y': origin[1], 'z': origin[2]},
                                         end={'x': c[0], 'y': c[1], 'z': c[2]})
        except Exception:  # noqa: BLE001 - a failed ray leaves the answer unknown
            return 'unknown'
        if ray.get('throttled'):
            return 'unknown'
        if not ray.get('hit'):
            return True
        hit = ((ray.get('hitObject') or {}).get('reference') or {})
        if hit.get('formId') == info['formId']:
            return True
        frac = ray.get('hitFraction')
        if frac is not None and frac >= 1 - min(0.5, info.get('radius', 0) / max(1.0, math.dist(origin, c))):
            return True
        return False

    # ================================================================ fnv_hud
    def hud_read(self, brief=False):
        hud = self.live.u32s([HUD])[0]
        if not hud:
            return {'exists': False, 'note': 'The HUD does not exist yet'}
        raw = self.live.reads([(hud, 0x278)])[0]
        if not raw:
            return {'exists': False}
        tiles = {k: _u32(raw, HUD_TILES + 4 * n) for k, n in HUD_TILE.items()}
        texts = self.agent._tile_strings([t for t in tiles.values() if t]) if self.agent is not None else {}
        text = {k: (texts.get(t) or None) if t else None for k, t in tiles.items()}
        cross = _u32(raw, HUD_CROSSHAIR)
        cross_info = None
        if cross:
            head = self.live.form_ids([cross])[0]
            cross_info = {'formId': head['formId'] if head else None,
                          'name': self.live.values(['GetName'], cross)[0] if head else None}
        out = {'crosshair': cross_info, 'prompt': text['prompt'] if cross_info else None, 'message': text['message'],
               'subtitle': text['subtitle'], 'name': text['name']}
        if brief:
            return out
        flags = _u32(raw, HUD_FLAGS)
        mode = _u32(raw, HUD_MODE)
        queue = []
        for node in self.live.walk_list(hud + HUD_QUEUE, 32):
            msg = self.link.one('rs', word(node), '200')
            if msg.get('ok'):
                queue.append(msg.get('text'))
        out.update(exists=True, mode={'value': mode, 'name': HUD_MODES.get(mode)},
                   visible=[n for bit, n in HUD_FLAG_NAMES.items() if flags & bit], visibleFlags='0x%X' % flags,
                   queuedMessages=queue, changed=False,
                   note='message is the corner message on screen now; queuedMessages wait their turn. Bars: read the '
                        'player\'s actor values (fnv_player), not tile widths.',
                   uses=['HUDMainMenu 0x011D96C0 (checked live): queue +0x18C, crosshair +0x1B8, flags +0x1BC, mode '
                         '+0x1C4', 'HUD tiles 26 message, 28 subtitle, 29 prompt, 32 name (string trait 0xFC4)'])
        return out

    def hud_post(self, text, seconds=3.0):
        self.live.in_game()
        if not text or len(text) > 250:
            raise ValueError('text is 1..250 characters')
        self.live.changing('hud message')
        address = self.live._game_string(str(text))
        fsec = struct.unpack('<I', struct.pack('<f', float(seconds)))[0]
        result = self.link.one('call', word(QUEUE_UI_MESSAGE), 'i', '0', '0', word(address), '0', '0', '0',
                               word(fsec), '0')
        if not result.get('ok'):
            raise LiveError('CALL_REJECTED', 'QueueUIMessage failed: %s' % result.get('error'), result)
        self.live.journal.add({'kind': 'hud_post', 'text': text, 'undo': None})
        return {'posted': text, 'seconds': seconds, 'how': 'QueueUIMessage 0x007052F0 (checked live)'}

    def hud(self, action='read', text=None, seconds=3.0):
        if action == 'read':
            self.live.ready()
            return self.hud_read()
        if action == 'post':
            return self.hud_post(text, seconds)
        raise ValueError('action is read or post')

    # ================================================================ fnv_visibility
    def visibility(self, ref):
        self.live.in_game()
        r = self.live.resolve_ref(ref)
        info = self._bounds([r['ptr']]).get(r['ptr']) or {}
        causes, also = [], []

        def found(cause, detail):
            (causes if not causes else also).append({'cause': cause, 'detail': detail})

        if not info.get('has3D'):
            found('no_3d', 'The reference has no 3D loaded (its model is missing, failed to load, or its cell is not '
                           'attached). Next: fnv_warnings and fnv_asset_check for the model path; fnv_cell_grid.')
        if info.get('deleted'):
            found('deleted', 'The reference is marked deleted.')
        if info.get('disabled'):
            found('disabled', 'The reference is disabled (by script or by its enable parent). Next: fnv_ref for the '
                              'enable parent; fnv_world_edit enable.')
        root = self._root(r['ptr']) if info.get('has3D') else 0
        if root:
            node = self.live.reads([(root, 0xA0)])[0]
            flags = _u32(node, NODE_FLAGS)
            scale = _f(node, NODE_SCALE)
            if flags & 1:
                found('culled', 'The 3D root has the app-culled flag (it is hidden on purpose).')
            if scale <= 0.001:
                found('zero_scale', 'The 3D root has world scale %.4f.' % scale)
            if info.get('radius', 1) <= 0.001:
                found('empty_bound', 'The 3D bound has zero radius: the model has no visible geometry.')
        ref_scale = self.live.values(['GetScale'], r['ptr'])[0]
        if ref_scale is not None and ref_scale <= 0.001:
            found('zero_scale', 'The reference scale is %s.' % ref_scale)
        if r['ptr'] == self._player():
            state = self.fly_state()
            if not state['thirdPerson'] and not state['fly']:
                found('first_person', 'The player is in first person: the third-person body is not drawn.')
        cell = self.live.values(['GetParentCell', 'GetInSameCell player'], r['ptr'])
        if cell[1] is not None and not cell[1]:
            also.append({'cause': 'other_cell', 'detail': 'Not in the player\'s cell (%s); it draws only while its '
                                                         'cell is attached. Next: fnv_cell_grid.' % cell[0]})
        if info.get('centre'):
            cam = self.camera()
            rect = cam.rect(info['centre'], info['radius'])
            if not rect or not rect.get('onScreen'):
                found('outside_view', 'Its bound is outside the camera view.')
            elif self.engine_ai is not None:
                los = self._line_of_sight(cam.position, info)
                if los is False:
                    found('occluded', 'A ray from the camera to its bound centre hits something else first.')
        if not causes:
            verdict = 'Nothing found that stops it from drawing: it should be visible.'
        else:
            verdict = 'Not drawn because: %s.' % causes[0]['cause']
        return {'formId': r['formId'], 'verdict': verdict, 'cause': causes[0] if causes else None, 'also': also,
                'bound': {'centre': [_r(v) for v in info['centre']], 'radius': _r(info['radius'])}
                if info.get('centre') else None, 'changed': False,
                'notChecked': ['fade distances and LOD settings', 'room and portal culling (reported as possible '
                               'only)', 'shader alpha', 'missing textures (fnv_warnings, fnv_asset file_log)'],
                'uses': ['ref +0x64 render data, +0x14 root', 'NiAVObject flags +0x30, world scale +0x98, bound +0x20',
                         'form flags 0x800 disabled, 0x20 deleted']}

    # ================================================================ fnv_photo
    def _shot(self, name, max_width):
        if self.agent is None:
            raise LiveError('NO_CAPTURE', 'No screenshot service')
        return self.agent.screenshot(name=name, max_width=max_width, method='engine')

    def _no_menu(self):
        """A menu (pause, dialogue, container...) covers the world with its blurred backdrop, and the free camera
        does not move under it: every capture would be the same picture of the menu (seen live). Refuse instead."""
        if self.agent is None:
            return
        # A menu that is closing (DialogMenu right after a dialogue ends) stays on the stack for a moment: give it
        # up to 2 s before refusing.
        for _ in range(10):
            try:
                menus = self.agent.menus()
            except Exception:  # noqa: BLE001 - no menu reader: capture anyway
                return
            if not (menus.get('inMenuMode') or menus.get('menuStack')):
                return
            self.sleep(0.2)
        if menus.get('inMenuMode') or menus.get('menuStack'):
            names = [m.get('name') for m in menus.get('menuStack') or []] or ['(menu mode)']
            raise LiveError('MENU_OPEN', 'A menu is open (%s): the capture would show its backdrop, not the world. '
                            'Close it first (fnv_ui_do close).' % ', '.join(str(n) for n in names))

    def _hud_off(self, hide):
        """Toggle menus (tm) off for a capture; returns a restore function."""
        if not hide:
            return lambda: None
        if hud_hidden(self.live) is not False:
            return lambda: None            # already hidden (by the user), or unreadable: toggle nothing
        self.live.console('tm')
        self._frames(2)

        def back():
            if hud_hidden(self.live) is not False:   # the hud_set rule: toggle only while hidden (blind if unreadable)
                self.live.console('tm')
        return back

    @_locked
    def photo(self, action, ref=None, angles=None, size=960, hide_hud=True, label=False, frames=None, every=1,
              baseline=None, name=None, distance=None):
        if action == 'compare' and baseline:
            baseline = str(inside(baseline, write_roots(self.live.project), 'baseline'))   # H5
        self.live.in_game()
        self._no_menu()
        if action == 'view':
            return self._photo_view(hide_hud, label, size, name)
        if action == 'ref':
            if not ref:
                raise ValueError('ref is needed')
            return self._photo_ref(ref, angles or ['front', 'three_quarter', 'side', 'back', 'top'], size, hide_hud,
                                   name, distance)
        if action == 'cell':
            return self._photo_cell(size, hide_hud, name)
        if action == 'sequence':
            return self._photo_sequence(int(frames or 10), int(every or 1), size, hide_hud, name)
        if action == 'compare':
            return self._photo_compare(baseline, size, name)
        if action == 'sheet':
            return self._photo_ref(ref, angles or ['front', 'side', 'back'], size, hide_hud, name, distance) \
                if ref else self._photo_view(hide_hud, label, size, name)
        raise ValueError('action is ref, cell, view, sheet, sequence or compare')

    def _photo_view(self, hide_hud, label, size, name):
        back = self._hud_off(hide_hud)
        try:
            shot = self._shot('%s-%s' % (name or 'view', _uniq()), size)
        finally:
            back()
        out = {'path': shot['path'], 'size': shot['size'], 'method': shot['method']}
        if label:
            seen = self.look(limit=30)
            self._label(shot['path'], seen['refs'], shot['size'], seen['camera']['screen'])
            out['labels'] = [{'formId': r['formId'], 'name': r['name'], 'rect': r['rect']} for r in seen['refs']]
        return out

    def _photo_ref(self, ref, angles, size, hide_hud, name, distance):
        shots, labels = [], []
        token = _uniq()
        back = self._hud_off(hide_hud)
        try:
            for angle in angles:
                if isinstance(angle, str):
                    if angle not in PHOTO_ANGLES:
                        raise ValueError('angles: %s, or [yaw, pitch] pairs' % ', '.join(PHOTO_ANGLES))
                    yaw, pitch = PHOTO_ANGLES[angle]
                    tag = angle
                else:
                    yaw, pitch = float(angle[0]), float(angle[1])
                    tag = 'yaw%g_pitch%g' % (round(yaw, 2), round(pitch, 2))
                self.orbit(ref, yaw=yaw, pitch=pitch, distance=distance, fit=distance is None)
                shot = self._shot('%s-%s-%s' % (name or 'photo', tag, token), size)
                shots.append(shot['path'])
                labels.append(tag)
        finally:
            back()
            restored = self.restore()
        sheet = self._sheet(shots, labels, name or 'photo-sheet')
        return {'sheet': sheet, 'views': [{'angle': a, 'path': p} for a, p in zip(labels, shots)],
                'restored': restored.get('restored'), 'worldPaused': True,
                'note': 'Taken from the fly camera with the world frozen (tfc 1); the player\'s view is restored.'}

    def _photo_cell(self, size, hide_hud, name):
        ptrs = self._refs(6000.0)
        info = [i for i in self._bounds(ptrs).values() if 'centre' in i and not i['disabled']]
        if not info:
            raise LiveError('EMPTY_CELL', 'No references with 3D around the player')
        xs = [i['centre'][0] for i in info]
        ys = [i['centre'][1] for i in info]
        zs = [i['centre'][2] for i in info]
        cx, cy, cz = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(zs)
        span = max(max(xs) - min(xs), max(ys) - min(ys), 256.0)
        views = [('above', (cx, cy - 1.0, cz + span * 0.9), 0.0, 89.0)]
        for k, (sx, sy) in enumerate(((-1, -1), (1, -1), (1, 1), (-1, 1))):
            pos = (cx + sx * span / 2, cy + sy * span / 2, cz + span * 0.35)
            yaw, pitch = heading_to(pos, (cx, cy, min(zs)))
            views.append(('corner%d' % (k + 1), pos, yaw, min(pitch, 60.0)))
        shots, labels = [], []
        token = _uniq()
        back = self._hud_off(hide_hud)
        try:
            for tag, pos, yaw, pitch in views:
                self.set(position=pos, yaw=yaw, pitch=pitch)
                shots.append(self._shot('%s-%s-%s' % (name or 'cell', tag, token), size)['path'])
                labels.append(tag)
        finally:
            back()
            self.restore()
        return {'sheet': self._sheet(shots, labels, name or 'cell-sheet'), 'views': dict(zip(labels, shots)),
                'span': _r(span), 'note': 'Interiors: the views from above see the roof only when the cell has one.'}

    def _photo_sequence(self, count, every, size, hide_hud, name):
        if not 1 <= count <= 60:
            raise ValueError('frames is 1..60 images')
        from .fnv_inject import shared_time
        control, _ = shared_time(self.live)
        shots = []
        token = _uniq()
        try:
            was_held = bool((control.batch([('gate',)])[0] or {}).get('held'))
        except Exception:  # noqa: BLE001 - unreadable gate: assume it was open (release afterwards, as before)
            was_held = False
        back = self._hud_off(hide_hud)
        try:
            control.run('hold')
            for k in range(count):
                if k:
                    control.run('step', frames=every)
                shots.append(self._shot('%s-%03d-%s' % (name or 'seq', k, token), size)['path'])
        finally:
            if not was_held:
                control.run('release')      # leave a gate the user had closed closed
            back()
        return {'sheet': self._sheet(shots, ['f%d' % (k * every) for k in range(len(shots))], name or 'seq-sheet'),
                'frames': shots, 'every': every,
                'note': 'The frame gate held the game between images; each step ran exactly `every` frames.'}

    def _photo_compare(self, baseline, size, name):
        from PIL import Image, ImageChops
        if not baseline or not Path(baseline).exists():
            raise ValueError('baseline is the path of an earlier photo')
        now = self._shot('%s-%s' % (name or 'compare', _uniq()), size)['path']
        a = Image.open(baseline).convert('RGB')
        b = Image.open(now).convert('RGB').resize(a.size)
        diff = ImageChops.difference(a, b).convert('L')
        mask = diff.point(lambda v: 255 if v > 24 else 0)
        changed = sum(1 for v in mask.getdata() if v) / float(a.size[0] * a.size[1])
        out_path = Path(now).with_name(Path(now).stem + '-diff.png')
        Image.merge('RGB', (mask, Image.new('L', a.size), Image.new('L', a.size))).save(out_path)
        return {'baseline': baseline, 'now': now, 'diff': str(out_path), 'changedPercent': round(changed * 100, 2)}

    def _sheet(self, paths, labels, name):
        from PIL import Image, ImageDraw
        images = [Image.open(p).convert('RGB') for p in paths]
        if not images:
            return None
        w, h = images[0].size
        scale = min(1.0, 640.0 / w)
        tw, th = int(w * scale), int(h * scale)
        cols = min(3, len(images))
        rows = (len(images) + cols - 1) // cols
        sheet = Image.new('RGB', (cols * tw, rows * (th + 18)), (20, 20, 20))
        draw = ImageDraw.Draw(sheet)
        for k, (img, label) in enumerate(zip(images, labels)):
            x, y = (k % cols) * tw, (k // cols) * (th + 18)
            sheet.paste(img.resize((tw, th)), (x, y + 18))
            draw.text((x + 4, y + 3), str(label), fill=(255, 220, 120))
        out = Path(paths[0]).with_name(re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:60] + '-%d-%s.png' % (int(time.time()), uuid.uuid4().hex[:6]))
        sheet.save(out)
        return str(out)

    def _label(self, path, refs, size, screen):
        from PIL import Image, ImageDraw
        img = Image.open(path).convert('RGB')
        sx, sy = img.size[0] / float(screen[0]), img.size[1] / float(screen[1])
        draw = ImageDraw.Draw(img)
        for r in refs:
            x0, y0, x1, y1 = r['rect']
            box = (x0 * sx, y0 * sy, x1 * sx, y1 * sy)
            draw.rectangle(box, outline=(255, 200, 0))
            draw.text((box[0] + 2, box[1] + 2), '%s %s' % (r['formId'], r.get('name') or ''), fill=(255, 255, 0))
        img.save(path)


def fnv_hexs(text):
    from .fnv_link import hexs
    return hexs(text)
