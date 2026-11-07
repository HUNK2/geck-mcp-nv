"""World, UI, memory and math tools built from the game's code (fnv-mcp phase 2, engine companion).

  fnv_globals       every global variable (TESDataHandler's list), filtered; set one (journaled)
  fnv_dialogue      the conversation now: speaker, spoken topic and line, the choices on screen and their state
  fnv_heap          memory: the 2 GB address space, the game's pools, form/actor counts, growth over time
  fnv_world_edit    move, rotate, scale, enable/disable, delete or duplicate a ref; force weather; image space
  fnv_scene_graph   a ref's 3D node tree: nodes, transforms, geometry, shaders and texture paths, controllers
  fnv_ui_tiles      a menu's live tile tree with trait values named by the game's own trait table
  fnv_vats_odds     the VATS hit chance per target and limb, recomputed with CombatFormulas::GetVATSChanceToHit
                    term by term, plus the perk layers and the number the menu shows
  fnv_frame_profile time per frame by subsystem, from timing hooks on the main loop's children

Addresses and layouts come from the decomp (research notes in the fnv-mcp project); each result lists them in
`uses`. Unproven functions are called only where the research traced their arguments from the call sites.
"""
import ctypes
import math
import struct
import time
from ctypes import wintypes

from .fnv_deep import HF_EXIT, HF_TABLE, HookError, decode_prologue
from .fnv_link import hexs, word
from .fnv_live import LiveError

PLAYER = 0x011DEA3C
DATA_HANDLER = 0x011C3F2C
FORM_MAP = 0x011C54C0
PROCESS_LISTS = 0x011E0E80
MEMORY_MANAGER = 0x011F6238
POOL_MAP = 0x011F63B8
FRAME_COUNTER = 0x011A2FE0
DIALOG_MENU = 0x011D9510
DIALOG_INFO = 0x011D9518
TOPIC_MANAGER = 0x011DDDE4
INTERFACE_MANAGER = 0x011D8A80
MENU_ARRAY = 0x011F3508
TRAIT_MAP = 0x011F32F4
VATS_MENU = 0x011DB0D4
VATS_TARGET = 0x011F21CC
VATS_TARGETS = 0x011DB150
FIST = 0x011CA278

MENU_IDS = {'message': 1001, 'inventory': 1002, 'stats': 1003, 'hud': 1004, 'loading': 1007, 'container': 1008,
            'dialog': 1009, 'sleepwait': 1012, 'start': 1013, 'pause': 1013, 'lockpick': 1014, 'quantity': 1016,
            'map': 1023, 'book': 1026, 'levelup': 1027, 'repair': 1035, 'racesex': 1036, 'chargen': 1048,
            'textedit': 1051, 'barter': 1053, 'surgery': 1054, 'hacking': 1055, 'vats': 1056, 'computers': 1057,
            'repairservices': 1058, 'tutorial': 1059, 'specialbook': 1060, 'itemmod': 1061, 'lovetester': 1074,
            'companionwheel': 1075, 'traitselect': 1076, 'recipe': 1077, 'slotmachine': 1080, 'blackjack': 1081,
            'roulette': 1082, 'caravan': 1083, 'trait': 1084}
DIALOG_STATES = ['camera zoom in', 'load speech', 'speech idle', 'play speech', 'camera zoom out', 'service fade out']
GOODBYE_STATES = ['in dialogue', 'natural goodbye', 'forced goodbye', 'invalid choice list']
LIMBS = ['Torso', 'Head 1', 'Head 2', 'Left Arm 1', 'Left Arm 2', 'Right Arm 1', 'Right Arm 2', 'Left Leg 1',
         'Left Leg 2', 'Left Leg 3', 'Right Leg 1', 'Right Leg 2', 'Right Leg 3', 'Brain', 'Weapon']
TEXTURE_SET_SHADERS = ('PPLighting', 'Lighting30', 'HairShader', 'SkinShader', 'Parallax', 'TallGrass')  # texture set at +0xA4
TEXTURE_SLOTS = ['diffuse', 'normal', 'glow or skin', 'parallax', 'cube', 'envmask']
VATS_SETTINGS = {
    'fVATSHitChanceMult': 0x011CF504, 'fVATSSpreadMult': 0x011CFE08, 'fVATSRangeSpreadMax': 0x011CFE50,
    'fVATSMaxChance': 0x011CE10C, 'fVATSScreenPercentFactor': 0x011CFAB0, 'fVATSStealthMult': 0x011CEE54,
    'fVATSDestructibleMult': 0x011CE6FC, 'fVATSGrenadeTargetArea': 0x011CEB20, 'fVATSMeleeMaxDistance': 0x011CF678,
}
# fnv_frame_profile: the main loop and its children (research-vats-frame.md 2.2-2.4), main thread, once a frame
FRAME_HOOKS = [
    (0x0086E650, 'frame', 'whole frame (MainLoop)'),
    (0x0086F940, 'player', 'player and world update, grid cell crossing'),
    (0x0086FD90, 'imagespace', 'weather and image-space modifiers'),
    (0x0086F390, 'input', 'input polling'),
    (0x00C3DBF0, 'io', 'IO manager / background loader tick'),
    (0x00978550, 'actor_scripts', 'scripts of high-process actors'),
    (0x009777A0, 'actor_lod', 'actor visibility / LOD refresh'),
    (0x00868850, 'gc', 'garbage collector'),
    (0x0086FBE0, 'cell_stream', 'cell streaming trigger'),
    (0x0045B070, 'cull', 'culling / visibility over loaded cells'),
    (0x0086FC60, 'scene_update', 'main-thread scene graph update, cell animations, sky'),
    (0x0086FF70, 'render', 'render the frame (world, image space, UI)'),
    (0x0086F640, 'audio', 'audio, radio, pending sounds'),
    (0x0096D810, 'process_lists', 'ProcessLists update (low/middle processes, crime)'),
    (0x00455490, 'quest_cell_scripts', 'TES::RunScripts: quest and cell scripts'),
    (0x008C7990, 'ai_wait', 'main thread waiting for the AI threads'),
    (0x0086F6A0, 'post_render', 'post-render: temp effects, animations, task queue, cell attaches'),
    (0x00870610, 'delayed_actions', 'delayed enable/disable, queued menus'),
]
FRAME_PREFIX = 'prof:'


def _f(w):
    return struct.unpack('<f', struct.pack('<I', w))[0]


def _f32(v):
    """Round to float32: the game's x87 runs in single precision (Direct3D's default control word)."""
    return struct.unpack('<f', struct.pack('<f', float(v)))[0]


def _fw(v):
    return struct.unpack('<I', struct.pack('<f', float(v)))[0]


def _r(x, n=3):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, n)


class WorldTools:
    def __init__(self, agent):
        self.agent = agent
        self._traits = None
        self._ni = {}

    @property
    def live(self):
        return self.agent.live

    @property
    def link(self):
        return self.agent.link

    def _call(self, fn, ecx=0, *args, ret='i'):
        result = self.link.one('call', word(fn), ret, word(ecx), '0', *[word(a & 0xFFFFFFFF) for a in args])
        if not result.get('ok'):
            raise LiveError('CALL_FAILED', 'Engine call 0x%08X failed: %s' % (fn, result.get('error')), result)
        return result

    def _eax(self, fn, ecx=0, *args):
        return int(self._call(fn, ecx, *args)['eax'], 16)

    def _st0(self, fn, ecx=0, *args):
        return self._call(fn, ecx, *args, ret='f').get('st0')

    def _classes(self, pointers):
        """{ptr: class name}: MSVC RTTI where the class has it, else Gamebryo's own NiRTTI (NiObject::GetRTTI, vtable
        slot 2, returns {const char *name, NiRTTI *base})."""
        pointers = [p for p in pointers if p]
        if not pointers:
            return {}
        msvc = self.live.rtti(pointers)
        out = {p: (msvc.get(p) or {}).get('class') for p in pointers}
        missing = [p for p in pointers if not out[p]]
        if missing:
            vts = dict(zip(missing, self.live.u32s(missing)))
            for p in missing:
                vt = vts.get(p)
                if not vt or not 0x00400000 <= vt < 0x01300000:
                    continue
                if vt not in self._ni:
                    name = None
                    try:
                        fn = self.live.u32s([vt + 8])[0]
                        if fn and 0x00401000 <= fn < 0x01100000:
                            rtti = self._eax(fn, p)
                            np_ = self.live.u32s([rtti])[0] if rtti else 0
                            name = self._texts([np_], 80)[0] if np_ else None
                    except LiveError:
                        name = None
                    self._ni[vt] = name
                out[p] = self._ni[vt]
        return out

    def _texts(self, pointers, limit=400):
        return self.agent.social._texts(pointers, limit)

    def _describe(self, pointers):
        d = self.agent.describe_pointers([p for p in pointers if p])
        return {p: {k: v for k, v in (d.get(p) or {}).items() if k in ('formId', 'editorId', 'name', 'class')} or None
                for p in pointers}

    # ================================================================== fnv_globals
    def globals(self, query=None, set_values=None, limit=200):
        self.live.ready()
        dh = self.live.u32s([DATA_HANDLER])[0]
        if not dh:
            raise LiveError('NO_DATA_HANDLER', 'The data handler does not exist yet')
        items = self.live.walk_list(dh + 0xE8, 4096)
        raws = self.live.reads([(g, 0x28) for g in items])
        names = self._texts([struct.unpack_from('<I', r, 0x18)[0] if r else 0 for r in raws], 120)
        out, by_name = [], {}
        for g, raw, name in zip(items, raws, names):
            if not raw:
                continue
            kind = chr(raw[0x20]) if 32 <= raw[0x20] < 127 else '?'
            value = struct.unpack_from('<f', raw, 0x24)[0]
            entry = {'editorId': name, 'formId': '%08X' % struct.unpack_from('<I', raw, 0x0C)[0],
                     'type': {'s': 'short', 'l': 'long', 'f': 'float', 'r': 'ref'}.get(kind, kind),
                     'value': int(value) if kind in 'sl' else _r(value, 6), 'ptr': g}
            by_name[(name or '').lower()] = entry
            by_name[entry['formId'].lower()] = entry
            if query and query.lower() not in (name or '').lower() and query.upper() != entry['formId']:
                continue
            out.append(entry)
        changed = []
        for key, value in (set_values or {}).items():
            g = by_name.get(str(key).lower())
            if not g:
                raise LiveError('NO_GLOBAL', 'No global variable %r' % key)
            number = float(value)
            if g['type'] in ('short', 'long'):
                number = float(int(number))
            result = self.live.mem_write('0x%08X' % (g['ptr'] + 0x24), number, as_type='f32',
                                         note='global %s' % g['editorId'])
            changed.append({'editorId': g['editorId'], 'old': g['value'], 'new': int(number) if g['type'] in
                            ('short', 'long') else number, 'journalId': result['journalId']})
        for e in out:
            e.pop('ptr', None)
        return {'count': len(out), 'globals': out[:limit], 'more': len(out) > limit, 'changed': changed or None,
                'uses': ['TESDataHandler 0x011C3F2C, global list tList +0xE8',
                         'TESGlobal: editor id BSString +0x18, type char +0x20, float value +0x24 (the game stores '
                         'shorts and longs as floats too; set writes +0x24, as script "set" does)']}

    # ================================================================== fnv_dialogue
    def dialogue(self):
        self.live.ready()
        menu, info_now, mgr = self.live.u32s([DIALOG_MENU, DIALOG_INFO, TOPIC_MANAGER])
        if not menu:
            return {'inConversation': False, 'note': 'No dialogue menu is open (DialogMenu singleton 0x011D9510 is '
                                                     'null). fnv_dialogue_tree shows what an NPC could say.'}
        raw = self.live.reads([(menu, 0x13C)])[0] or b''
        state, goodbye = struct.unpack_from('<II', raw, 0x28) if len(raw) >= 0x30 else (None, None)
        speaker = struct.unpack_from('<I', raw, 0x80)[0] if len(raw) >= 0x84 else 0
        label = raw[0x84:0x104].split(b'\0', 1)[0].decode('cp1252', 'replace') if len(raw) >= 0x104 else None
        tiles = list(struct.unpack_from('<4I', raw, 0x30)) if len(raw) >= 0x40 else []
        shown = self.agent._tile_strings([t for t in tiles[1:3] if t]) if tiles else {}
        out = {'inConversation': True, 'speaker': self._describe([speaker]).get(speaker),
               'state': DIALOG_STATES[state] if state is not None and state < len(DIALOG_STATES) else state,
               'goodbye': GOODBYE_STATES[goodbye] if goodbye is not None and goodbye < len(GOODBYE_STATES) else goodbye,
               'topicLabel': label, 'speakerNameOnScreen': shown.get(tiles[1]) if len(tiles) > 1 else None,
               'lineOnScreen': shown.get(tiles[2]) if len(tiles) > 2 else None,
               'currentInfo': self._describe([info_now]).get(info_now) if info_now else None}
        if mgr:
            mraw = self.live.reads([(mgr, 0x18)])[0] or b''
            stack_head = struct.unpack_from('<I', mraw, 4)[0] if len(mraw) >= 8 else 0
            current = stack_head  # the head item of the topic-list stack = the current list
            nodes = self.live.walk_list(current, 64) if current else []
            topics = self.live.reads([(t, 0x2C) for t in nodes])
            prompts = self._texts([struct.unpack_from('<I', t, 0)[0] if t else 0 for t in topics], 300)
            refs = []
            for t in topics:
                if t:
                    refs += [struct.unpack_from('<I', t, 0x18)[0], struct.unpack_from('<I', t, 0x28)[0],
                             struct.unpack_from('<I', t, 0x14)[0]]
            names = self._describe(refs)
            entries = []
            for k, (ptr, t, prompt) in enumerate(zip(nodes, topics, prompts)):
                if not t:
                    continue
                info, topic, quest = (struct.unpack_from('<I', t, o)[0] for o in (0x18, 0x28, 0x14))
                entries.append({'index': k, 'prompt': prompt, 'topic': names.get(topic), 'info': names.get(info),
                                'quest': names.get(quest), 'alreadyHeard': bool(t[0x25]), 'refusal': bool(t[0x09]),
                                'responseNode': '0x%08X' % struct.unpack_from('<I', t, 0x20)[0]})
            out['spokenTopic'] = entries[0] if entries else None
            out['choices'] = entries[1:]
            out['speakerInManager'] = self._describe([struct.unpack_from('<I', mraw, 0x0C)[0]]).get(
                struct.unpack_from('<I', mraw, 0x0C)[0]) if len(mraw) >= 0x10 else None
        out['whyOthersAreHidden'] = [
            'a topic shows only if the player knows it (Player+0x6A8; AddTopic, info add-topics)',
            'TESTopic::FindMatchingTopicInfo (0x0061A790) must find an info whose quest is running and whose '
            'conditions pass; say-once infos already said (+0x22) are skipped',
            'low Intelligence players get the "dumb" variant of topics with dumb responses',
            'topics with no responses and duplicates are dropped; speech challenges roll on choosing',
            'fnv_dialogue_tree evaluates every info of a topic for this speaker and shows which condition failed']
        out['uses'] = ['DialogMenu 0x011D9510 (state +0x28, goodbye +0x2C, tiles +0x30, speaker +0x80, label +0x84)',
                       'current TESTopicInfo 0x011D9518', 'MenuTopicManager 0x011DDDE4: topic-list stack +4, node 0 = '
                       'spoken topic, nodes 1.. = choices (MenuTopic: prompt +0, quest +0x14, info +0x18, read +0x25, '
                       'topic +0x28), built by BuildMenuTopics 0x0083ED50']
        return out

    # ================================================================== fnv_heap
    def heap(self, seconds=0.0):
        self.live.ready()
        first = self._heap_sample()
        if seconds and seconds > 0:
            time.sleep(min(float(seconds), 120.0))
            second = self._heap_sample()
            growth = {k: (second['counts'][k] - first['counts'][k]) if isinstance(first['counts'].get(k), int) and
                      isinstance(second['counts'].get(k), int) else None for k in first['counts']}
            space = {k: second['addressSpaceMB'][k] - first['addressSpaceMB'][k] for k in ('committed', 'reserved', 'free')}
            pools = []
            before = {p['address']: p for p in first['pools']}
            for p in second['pools']:
                b = before.get(p['address'])
                if b and p['allocatedPages'] != b['allocatedPages']:
                    pools.append({'pool': p['name'], 'blockSize': p['blockSize'],
                                  'pagesGrowth': p['allocatedPages'] - b['allocatedPages']})
            second['growth'] = {'seconds': seconds, 'counts': growth, 'addressSpaceMB': space, 'pools': pools}
            return second
        return first

    def _heap_sample(self):
        pid = (self.agent.game.process() or [None])[0]
        laa = None
        head = self.live.reads([(0x00400000, 0x400)])[0]
        if head and head[:2] == b'MZ':
            pe = struct.unpack_from('<I', head, 0x3C)[0]
            if pe + 0x18 <= len(head):
                laa = bool(struct.unpack_from('<H', head, pe + 0x16)[0] & 0x20)
        space = address_space(pid, 0xFFFF0000 if laa else 0x80000000) if pid else None
        if space:
            space['largeAddressAware'] = laa
        pl_raw, fm_ptr, frame = self.live.reads([(PROCESS_LISTS, 0x40)])[0], *self.live.u32s([FORM_MAP, FRAME_COUNTER])
        counts = {'frame': frame}
        if fm_ptr:
            counts['forms'] = self.live.u32s([fm_ptr + 0x0C])[0]
        if pl_raw and len(pl_raw) >= 0x34:
            heads = struct.unpack_from('<4I', pl_raw, 0x14)
            tails = struct.unpack_from('<4I', pl_raw, 0x24)
            for name, h, t in zip(('highActors', 'middleHighActors', 'middleLowActors', 'lowActors'), heads, tails):
                counts[name] = t - h if t >= h and t - h < 100000 else None
        pool_ptrs = self.live.reads([(POOL_MAP, 256 * 4)])[0] or b''
        distinct = sorted({p for p in struct.unpack('<256I', pool_ptrs) if p}) if len(pool_ptrs) == 1024 else []
        raws = self.live.reads([(p, 0x5C) for p in distinct])
        names = self._texts([struct.unpack_from('<I', r, 0)[0] if r else 0 for r in raws], 80)
        pools = []
        for p, r, n in zip(distinct, raws, names):
            if not r:
                continue
            block, peak, pages, size, free, allocated = (struct.unpack_from('<I', r, o)[0] for o in (0x40, 0x44, 0x4C, 0x50, 0x54, 0x58))
            pools.append({'address': '0x%08X' % p, 'name': n, 'blockSize': block, 'pageCount': pages,
                          'peakPages': peak, 'poolBytes': size, 'freeBlocks': free, 'allocatedPages': allocated,
                          'allocatedMB': _r(allocated * 4096 / 1048576.0, 2)})
        pools.sort(key=lambda x: -(x['allocatedPages'] or 0))
        out = {'counts': counts, 'addressSpaceMB': space, 'pools': pools,
               'uses': ['ProcessLists 0x011E0E80 (per-level [head, tail) indices +0x14/+0x24)',
                        'all-forms map 0x011C54C0 (count +0x0C)', 'pool address map 0x011F63B8 (MemoryPool: block '
                        'size +0x40, pages +0x4C, free blocks +0x54, allocated pages +0x58)',
                        'VirtualQueryEx over the game process (address space)']}
        if space:
            out['note'] = ('The running FalloutNV.exe is %slarge address aware: it can use %d MB of address space, %d MB '
                           'are used and the largest free block is %d MB. Running out of it crashes the game in '
                           'allocations, often in worker threads.' % ('' if space.get('largeAddressAware') else 'not ',
                                                                     space['limit'], space['used'], space['largestFreeBlock']))
        return out

    # ================================================================== fnv_world_edit
    def world_edit(self, action, ref=None, position=None, rotation=None, scale=None, fade=False, weather=None,
                   override=True, imagespace=None, strength=1.0, distance=0, count=1):
        self.live.in_game()
        undo_lines, out = [], {'action': action}
        if action in ('move', 'rotate', 'scale', 'enable', 'disable', 'delete', 'duplicate', 'transform'):
            if not ref:
                raise ValueError('ref is required')
            r = self.live.resolve(ref)
            p = r['ptr']
            raw = self.live.reads([(p, 0x68)])[0]
            if not raw:
                raise LiveError('UNREADABLE', 'Cannot read the reference')
            rot = struct.unpack_from('<3f', raw, 0x24)
            pos = struct.unpack_from('<3f', raw, 0x30)
            old_scale = struct.unpack_from('<f', raw, 0x3C)[0]
            out['ref'] = self._describe([p]).get(p)
            out['before'] = {'position': [_r(v, 2) for v in pos], 'rotationDegrees': [_r(math.degrees(v), 2) for v in rot],
                             'scale': _r(old_scale, 3)}
            self.live.changing('world edit %s' % action)
            if action in ('move', 'transform') and position:
                for axis, v in zip('XYZ', position):
                    if v is not None:
                        self._call(0x005C0740, 0, p, ord(axis), _fw(v))  # Script::SetPosBase(ref, axis, value)
                undo_lines += ['SetPos %s %f' % (a, v) for a, v in zip('XYZ', pos)]
            if action in ('rotate', 'transform') and rotation:
                for axis, v in zip('XYZ', rotation):
                    if v is not None:
                        self._call(0x005C0B10, 0, p, ord(axis), _fw(v))  # Script::SetAngleBase(ref, axis, degrees)
                undo_lines += ['SetAngle %s %f' % (a, math.degrees(v)) for a, v in zip('XYZ', rot)]
            if action in ('scale', 'transform') and scale is not None:
                self._call(0x005C1C10, 0, p, _fw(scale))  # Script::SetReferenceScale(ref, scale)
                undo_lines.append('SetScale %f' % old_scale)
            if action == 'enable':
                self._call(0x005AA580, 0, p)  # AddPendingEnabledReference: applied by the next frame's delayed pass
                undo_lines.append('Disable')
            elif action == 'disable':
                self._call(0x005AA500, 0, p, 1 if fade else 0)  # AddPendingDisabledReference(ref, fade)
                undo_lines.append('Enable')
            elif action == 'delete':
                if p == self.live.u32s([PLAYER])[0]:
                    raise ValueError('The player cannot be deleted')
                self.live.console('MarkForDelete', p)  # handler 0x005D8E20 -> TESObjectREFR::SetDelete (vtbl 0x49)
                out['note'] = 'Marked for deletion (not undoable: the game frees created refs).'
            elif action == 'duplicate':
                base = struct.unpack_from('<I', raw, 0x20)[0]
                made = []
                for _ in range(max(1, min(int(count), 20))):
                    new = self._eax(0x005C4B30, 0, p, base, 1, int(distance), 0, _fw(1.0))  # PlaceAtMe
                    if new:
                        made.append(new)
                descr = self._describe(made)
                out['created'] = [descr.get(m) for m in made]
                for m in made:
                    fid = (descr.get(m) or {}).get('formId')
                    if fid:
                        self.live.journal.add({'kind': 'world_edit', 'action': 'duplicate', 'ref': fid,
                                               'undo': {'op': 'console_ref', 'ref': fid, 'lines': ['Disable', 'MarkForDelete']},
                                               'pid': (self.link.hello or {}).get('pid')})
            if undo_lines:
                entry = self.live.journal.add({'kind': 'world_edit', 'action': action, 'ref': r['formId'],
                                               'undo': {'op': 'console_ref', 'ref': r['formId'], 'lines': undo_lines},
                                               'pid': (self.link.hello or {}).get('pid')})
                out['journalId'] = entry['id']
            if action in ('move', 'rotate', 'scale', 'transform'):
                after = self.live.reads([(p, 0x40)])[0]
                if after:
                    out['after'] = {'position': [_r(v, 2) for v in struct.unpack_from('<3f', after, 0x30)],
                                    'rotationDegrees': [_r(math.degrees(v), 2) for v in struct.unpack_from('<3f', after, 0x24)],
                                    'scale': _r(struct.unpack_from('<f', after, 0x3C)[0], 3)}
            out['uses'] = {'move': 'Script::SetPosBase 0x005C0740', 'rotate': 'Script::SetAngleBase 0x005C0B10',
                           'scale': 'Script::SetReferenceScale 0x005C1C10', 'transform': 'SetPosBase/SetAngleBase/'
                           'SetReferenceScale', 'enable': 'AddPendingEnabledReference 0x005AA580',
                           'disable': 'AddPendingDisabledReference 0x005AA500', 'delete': 'MarkForDelete',
                           'duplicate': 'PlaceAtMe 0x005C4B30'}[action]
            return out
        if action == 'weather':
            sky = self._eax(0x0046DD00)  # Sky::GetInstance
            if not sky:
                raise LiveError('NO_SKY', 'No sky object')
            current = self.live.u32s([sky + 0x10, sky + 0x1C])
            before = self._describe([v for v in current if v])
            if weather in (None, 'release'):
                self.live.console('ReleaseWeatherOverride')
                out['released'] = True
            else:
                w = self.live.resolve(weather)
                self.live.changing('force weather')
                self._call(0x0063D0E0, sky, w['ptr'], 1 if override else 0)  # Sky::ForceWeather(weather, override)
                old = (before.get(current[0]) or {}).get('formId')
                if old:
                    entry = self.live.journal.add({'kind': 'world_edit', 'action': 'weather', 'weather': w['formId'],
                                                   'undo': {'op': 'console', 'line': 'ForceWeather %s' % old}})
                    out['journalId'] = entry['id']
            now = self.live.u32s([sky + 0x10, sky + 0x1C])
            names = self._describe([v for v in now if v])
            out.update({'before': {'current': before.get(current[0]), 'override': before.get(current[1])},
                        'after': {'current': names.get(now[0]), 'override': names.get(now[1])},
                        'uses': 'Sky::GetInstance 0x0046DD00, Sky::ForceWeather 0x0063D0E0 (current +0x10, override +0x1C)'})
            return out
        if action in ('imagespace', 'imagespace_stop'):
            m = self.live.resolve(imagespace)
            self.live.changing('image space modifier')
            if action == 'imagespace':
                self._call(0x005299A0, 0, m['ptr'], _fw(strength), 0)  # ImageSpaceModifierInstanceForm::Trigger
                self.live.journal.add({'kind': 'world_edit', 'action': 'imagespace', 'imod': m['formId'],
                                       'undo': {'op': 'console', 'line': 'RemoveImageSpaceModifier %s' % m['formId']}})
            else:
                self._call(0x00529C90, 0, m['ptr'])  # ImageSpaceModifierInstanceForm::Stop
            out.update({'imagespaceModifier': self._describe([m['ptr']]).get(m['ptr']),
                        'uses': 'ImageSpaceModifierInstanceForm::Trigger 0x005299A0 / Stop 0x00529C90'})
            return out
        raise ValueError('action: move, rotate, scale, transform, enable, disable, delete, duplicate, weather, '
                         'imagespace, imagespace_stop')

    # ================================================================== fnv_scene_graph
    def scene_graph(self, ref='player', depth=6, max_nodes=300, first_person=False, name_filter=None):
        self.live.ready()
        r = self.live.resolve(ref)
        p = r['ptr']
        if first_person and p == self.live.u32s([PLAYER])[0]:
            root = self.live.u32s([p + 0x694])[0]
        else:
            loaded = self.live.u32s([p + 0x64])[0]
            root = self.live.u32s([loaded + 0x14])[0] if loaded else 0
        model = None
        try:
            mp = self._eax(0x005715D0, p)  # TESObjectREFR::GetModelFilePath
            model = self._texts([mp])[0] if mp else None
        except LiveError:
            pass
        if not root:
            return {'ref': self._describe([p]).get(p), 'model': model, 'loaded': False,
                    'note': 'The reference has no 3D loaded (not in a loaded cell, disabled, or not drawn yet).'}
        nodes, level, seen, total = [], [(root, 0, None)], {root}, 0
        while level and total < max_nodes:
            ptrs = [n for n, _, _ in level]
            raws = self.live.reads([(n, 0xC0) for n in ptrs])
            classes = self._classes(ptrs)
            names = self._texts([struct.unpack_from('<I', rw, 0x08)[0] if rw else 0 for rw in raws], 200)
            nxt = []
            for (n, d, parent), rw, name in zip(level, raws, names):
                if not rw or total >= max_nodes:
                    continue
                total += 1
                cls = classes.get(n) or '?'
                local = struct.unpack_from('<13f', rw, 0x34)
                world = struct.unpack_from('<13f', rw, 0x68)
                item = {'node': '0x%08X' % n, 'class': cls, 'name': name, 'depth': d, 'parent': parent,
                        'flags': '0x%X' % struct.unpack_from('<I', rw, 0x30)[0],
                        'hidden': bool(struct.unpack_from('<I', rw, 0x30)[0] & 1),
                        'local': {'translate': [_r(v, 2) for v in local[9:12]], 'scale': _r(local[12], 3)},
                        'world': {'translate': [_r(v, 2) for v in world[9:12]], 'scale': _r(world[12], 3)}}
                ctrl = struct.unpack_from('<I', rw, 0x0C)[0]
                if ctrl:
                    item['controllers'] = ctrl
                extra, extra_n = struct.unpack_from('<IH', rw, 0x10)
                if extra and extra_n:
                    item['extra'] = (extra, min(extra_n, 16))
                if struct.unpack_from('<I', rw, 0x1C)[0]:
                    item['collision'] = '0x%08X' % struct.unpack_from('<I', rw, 0x1C)[0]
                if 'Node' in cls or cls in ('NiSwitchNode', 'NiBillboardNode', 'NiLODNode'):
                    data, cap, size = struct.unpack_from('<IHH', rw, 0xA0)
                    item['childCount'] = size
                    if d + 1 <= depth and data and size:
                        kids = self.live.reads([(data, 4 * min(size, 128))])[0] or b''
                        for c in struct.unpack('<%dI' % (len(kids) // 4), kids):
                            if c and c not in seen:
                                seen.add(c)
                                nxt.append((c, d + 1, '0x%08X' % n))
                if any(k in cls for k in ('TriShape', 'TriStrips', 'Geometry', 'Particles', 'Lines')):
                    item['geometry'] = struct.unpack_from('<6I', rw, 0x9C) + struct.unpack_from('<2I', rw, 0xB8)
                nodes.append(item)
            level = nxt
        self._geometry_details(nodes)
        self._controller_details(nodes)
        self._extra_details(nodes)
        if name_filter:
            nodes = [n for n in nodes if name_filter.lower() in (n.get('name') or '').lower()]
        textures = sorted({t for n in nodes for t in (n.get('textures') or {}).values() if t})
        return {'ref': self._describe([p]).get(p), 'model': model, 'root': '0x%08X' % root, 'nodeCount': len(nodes),
                'capped': total >= max_nodes, 'nodes': nodes, 'textures': textures,
                'uses': ['ref +0x64 loaded data, +0x14 root node (player first person +0x694)',
                         'NiObjectNET name +0x08, controllers +0x0C, extra data +0x10; NiAVObject flags +0x30, local '
                         'transform +0x34, world +0x68; NiNode children +0xA0', 'NiGeometry properties +0x9C..+0xB0, '
                         'data +0xB8 (vertices +0x08, triangles +0x40), skin +0xBC; shader texture set +0xA4',
                         'TESObjectREFR::GetModelFilePath 0x005715D0']}

    def _geometry_details(self, nodes):
        geos = [n for n in nodes if 'geometry' in n]
        if not geos:
            return
        props = []
        for n in geos:
            alpha, cull, material, shader, stencil, texturing, data, skin = n.pop('geometry')
            n['_g'] = (alpha, material, shader, texturing, data, skin)
            props += [shader, texturing, data]
        props = [x for x in props if x]
        classes = self._classes(props)
        raws = dict(zip(props, self.live.reads([(x, 0xB0) for x in props])))
        set_ptrs = {}
        for x in props:
            cls = classes.get(x) or ''
            if any(k in cls for k in TEXTURE_SET_SHADERS) and raws.get(x) and len(raws[x]) >= 0xA8:
                set_ptrs[x] = struct.unpack_from('<I', raws[x], 0xA4)[0]
        sets = dict(zip(set_ptrs.values(), self.live.reads([(s, 8 + 8 * 6) for s in set_ptrs.values()]))) if set_ptrs else {}
        string_ptrs = []
        for s, raw in sets.items():
            if raw:
                string_ptrs += [struct.unpack_from('<I', raw, 8 + 8 * i)[0] for i in range(6)]
        nolight = {x: struct.unpack_from('<I', raws[x], 0x64)[0] for x in props
                   if 'NoLighting' in (classes.get(x) or '') and raws.get(x)}
        texts = dict(zip(string_ptrs + list(nolight.values()), self._texts(string_ptrs + list(nolight.values()), 260)))
        for n in geos:
            alpha, material, shader, texturing, data, skin = n.pop('_g')
            g = {'alpha': bool(alpha), 'material': bool(material), 'skinned': bool(skin)}
            if data and raws.get(data):
                g['vertices'] = struct.unpack_from('<H', raws[data], 0x08)[0]
                g['triangles'] = struct.unpack_from('<H', raws[data], 0x40)[0]
            if shader:
                g['shader'] = classes.get(shader)
                tex = {}
                if shader in set_ptrs and sets.get(set_ptrs[shader]):
                    raw = sets[set_ptrs[shader]]
                    for i, slot in enumerate(TEXTURE_SLOTS):
                        t = texts.get(struct.unpack_from('<I', raw, 8 + 8 * i)[0])
                        if t:
                            tex[slot] = t
                elif shader in nolight:
                    t = texts.get(nolight[shader])
                    if t:
                        tex['diffuse'] = t
                if tex:
                    n['textures'] = tex
            if texturing:
                g['texturingProperty'] = True
            n['geometryInfo'] = g

    def _controller_details(self, nodes):
        heads = {n['node']: n.pop('controllers') for n in nodes if 'controllers' in n}
        for node, head in heads.items():
            out, c, seen = [], head, set()
            while c and c not in seen and len(out) < 8:
                seen.add(c)
                raw = self.live.reads([(c, 0x48)])[0]
                if not raw:
                    break
                cls = self._classes([c]).get(c)
                entry = {'class': cls, 'flags': '0x%X' % struct.unpack_from('<H', raw, 0x08)[0]}
                if cls == 'NiControllerManager':
                    data, _, size = struct.unpack_from('<IHH', raw, 0x38)
                    seqs = self.live.reads([(data, 4 * min(size, 32))])[0] if data and size else b''
                    sp = [s for s in struct.unpack('<%dI' % (len(seqs or b'') // 4), seqs or b'') if s]
                    sraw = self.live.reads([(s, 0x48) for s in sp])
                    snames = self._texts([struct.unpack_from('<I', x, 0x08)[0] if x else 0 for x in sraw], 100)
                    entry['sequences'] = [{'name': nm, 'active': bool(struct.unpack_from('<I', x, 0x44)[0]) if x else None}
                                          for nm, x in zip(snames, sraw)]
                out.append(entry)
                c = struct.unpack_from('<I', raw, 0x30)[0]
            for n in nodes:
                if n['node'] == node:
                    n['controllers'] = out

    def _extra_details(self, nodes):
        for n in nodes:
            if 'extra' not in n:
                continue
            data, count = n.pop('extra')
            arr = self.live.reads([(data, 4 * count)])[0] or b''
            ptrs = [x for x in struct.unpack('<%dI' % (len(arr) // 4), arr) if x]
            raws = self.live.reads([(x, 0x10) for x in ptrs])
            classes = self._classes(ptrs)
            names = self._texts([struct.unpack_from('<I', rw, 0x08)[0] if rw else 0 for rw in raws], 100)
            out = []
            for x, rw, nm in zip(ptrs, raws, names):
                cls = classes.get(x)
                e = {'class': cls, 'name': nm}
                if rw and cls == 'NiStringExtraData':
                    e['value'] = self._texts([struct.unpack_from('<I', rw, 0x0C)[0]], 300)[0]
                elif rw and cls in ('NiIntegerExtraData', 'BSXFlags'):
                    e['value'] = struct.unpack_from('<i', rw, 0x0C)[0]
                elif rw and cls == 'NiFloatExtraData':
                    e['value'] = _r(struct.unpack_from('<f', rw, 0x0C)[0], 4)
                out.append(e)
            n['extraData'] = out

    # ================================================================== fnv_ui_tiles
    def _trait_names(self):
        if self._traits is not None:
            return self._traits
        raw = self.live.reads([(TRAIT_MAP, 0x10)])[0]
        names = {}
        if raw:
            buckets_n, buckets = struct.unpack_from('<II', raw, 4)
            arr = self.live.reads([(buckets, 4 * min(buckets_n, 4096))])[0] if buckets and buckets_n else b''
            pending = [b for b in struct.unpack('<%dI' % (len(arr or b'') // 4), arr or b'') if b]
            entries, guard = [], 0
            while pending and guard < 20:
                guard += 1
                raws = self.live.reads([(e, 12) for e in pending])
                nxt = []
                for e, rw in zip(pending, raws):
                    if rw:
                        n_, key, tid = struct.unpack('<III', rw)
                        entries.append((key, tid))
                        if n_:
                            nxt.append(n_)
                pending = nxt
            texts = self._texts([k for k, _ in entries], 80)
            names = {tid: t for (k, tid), t in zip(entries, texts) if t}
        self._traits = names
        return names

    def ui_tiles(self, menu='hud', path=None, depth=8, max_tiles=400, traits=None):
        self.live.ready()
        key = str(menu).lower().replace(' ', '').replace('menu', '') if not str(menu).isdigit() else int(menu)
        if key == 'root':
            im = self.live.u32s([INTERFACE_MANAGER])[0]
            root = self.live.u32s([im + 0x9C])[0] if im else 0
            menu_id = None
        else:
            menu_id = key if isinstance(key, int) else MENU_IDS.get(key)
            if not menu_id:
                raise ValueError('menu: a menu name (%s), an id 1001..1084, or root' % ', '.join(sorted(MENU_IDS)))
            raw = self.live.reads([(MENU_ARRAY + 4, 8)])[0]
            data, _, size = struct.unpack('<IHH', raw)
            idx = menu_id - 0x3E9
            root = self.live.u32s([data + 4 * idx])[0] if data and 0 <= idx < size else 0
        if not root:
            return {'menu': menu, 'exists': False, 'note': 'That menu has no tile tree now (it is not open or not created).'}
        names = self._trait_names()
        wanted = {t.lower() for t in traits} if traits else None
        tiles, level, total = [], [(root, 0, '')], 0
        while level and total < max_tiles:
            ptrs = [t for t, _, _ in level]
            tile_names = self.agent._tile_name(ptrs)
            classes = self._classes(ptrs)
            values = self.agent._tile_values(ptrs)
            strings = {}
            sptrs = [(t, tid, s) for t in ptrs for tid, (_, s) in values.get(t, {}).items() if s]
            texts = self._texts([s for _, _, s in sptrs], 200)
            for (t, tid, _), txt in zip(sptrs, texts):
                strings[(t, tid)] = txt
            nxt = []
            for t, d, parent_path in level:
                if total >= max_tiles:
                    break
                total += 1
                name = tile_names.get(t) or '?'
                full = (parent_path + '/' + name) if parent_path else name
                vals = {}
                for tid, (num, s) in sorted(values.get(t, {}).items()):
                    tname = names.get(tid, '0x%X' % tid)
                    if wanted and tname.lower() not in wanted:
                        continue
                    vals[tname] = strings.get((t, tid)) if s else _r(num, 3)
                entry = {'path': full, 'class': classes.get(t), 'depth': d, 'values': vals}
                if not path or path.lower() in full.lower():
                    tiles.append(entry)
                if d < depth:
                    nxt += [(c, d + 1, full) for c in self.agent._children(t)]
            level = nxt
        return {'menu': {'id': menu_id, 'name': menu}, 'root': '0x%08X' % root, 'tileCount': len(tiles),
                'capped': total >= max_tiles, 'tiles': tiles, 'traitNamesFromGame': len(names),
                'uses': ['TileMenu array 0x011F3508 (index menu id - 0x3E9)', 'InterfaceManager +0x9C menu root',
                         'Tile: children +0x04, values +0x14 (id, float +8, string +0xC), name +0x20',
                         "the game's trait name map 0x011F32F4"]}

    # ================================================================== fnv_vats_odds
    def vats_odds(self, target=None, limb=None, include_terms=True):
        self.live.in_game()
        player = self.live.u32s([PLAYER])[0]
        menu, current = self.live.u32s([VATS_MENU, VATS_TARGET])
        settings = {k: _r(v, 4) for k, v in zip(VATS_SETTINGS, (None if x is None else _f(x) for x in
                                                               self.live.u32s([a + 4 for a in VATS_SETTINGS.values()])))}
        weapon = self._equipped(player)
        records = []
        if menu:
            for rec in self.live.walk_list(VATS_TARGETS, 64):
                raw = self.live.reads([(rec, 0x14)])[0]
                if not raw:
                    continue
                ref, mode = struct.unpack_from('<Ii', raw, 0)
                limbs = []
                for le in self.live.walk_list(rec + 8, 32):
                    lr = self.live.reads([(le, 0x34)])[0]
                    if lr:
                        idx, vis, hit = struct.unpack_from('<iff', lr, 0x20)
                        tile = struct.unpack_from('<I', lr, 0x30)[0]
                        limbs.append({'idx': idx, 'visible': vis, 'stored': hit, 'tile': tile})
                records.append({'ref': ref, 'mode': mode, 'limbs': limbs})
        if target:
            t = self.live.resolve(target)['ptr']
            records = [r for r in records if r['ref'] == t] or [{'ref': t, 'mode': None, 'limbs': []}]
        if not records:
            return {'vatsOpen': bool(menu), 'note': 'VATS is not open and no target was given: open VATS (or pass '
                                                    'target, and optionally limb) to compute odds.', 'settings': settings}
        buf = self.agent._buffer()
        out_targets = []
        tile_text = {}
        tiles = [l['tile'] for r in records for l in r['limbs'] if l['tile']]
        if tiles:
            tile_text = self.agent._tile_strings(tiles)
        for rec in records[:8]:
            t = rec['ref']
            is_actor = (self.live.rtti([t]).get(t) or {}).get('class') in ('Actor', 'Character', 'Creature', 'PlayerCharacter')
            limbs = rec['limbs']
            if not limbs:
                indexes = [limb] if limb is not None else ([0, 1, 3, 5, 7, 10] if is_actor else [-1])
                limbs = [{'idx': i, 'visible': 1.0, 'stored': None, 'tile': 0, 'assumedVisible': True} for i in indexes]
            elif limb is not None:
                limbs = [l for l in limbs if l['idx'] == limb]
            tweapon = self._equipped(t) if is_actor and rec['mode'] != 2 else 0
            bpd = 0
            if is_actor and rec['mode'] != 2:
                base = self._eax(0x004181E0, t)  # GetActorBase
                vt = self.live.u32s([base])[0] if base else 0
                fn = self.live.u32s([vt + 0x180])[0] if vt else 0
                bpd = self._eax(fn, base) if fn else 0  # TESActorBase::GetBodyPartData (vtbl +0x180)
            dist = self._st0(0x009A64D0, 0, player, t, 0)
            bound = (self._st0(0x00571600, t) or 0) * 2
            if bound < 1.0:
                bound = 128.0
            spread_bonus = self._st0(0x008B0DD0, player, 3) if weapon else 1.0
            rows = []
            for l in limbs[:16]:
                part = self._eax(0x005E50F0, bpd, l['idx']) if bpd and 0 <= l['idx'] < 15 else 0
                words = [player + 0xA4, t, weapon, _fw(1.0), _fw(0.0), _fw(spread_bonus), part, tweapon,
                         _fw(l['visible']), _fw(dist), _fw(bound), 0]
                base_chance = self._st0(0x00646F70, 0, *words)
                # perk layer: ep 8 Calculate To-Hit Chance, starting from the base chance
                self.link.write(buf, struct.pack('<f', base_chance or 0.0))
                self._call(0x005E58F0, 0, 8, player, weapon or self.live.u32s([FIST])[0] or 0, t, buf)
                perk_max = struct.unpack('<f', self.live.reads([(buf, 4)])[0])[0]
                final = max(base_chance or 0.0, perk_max)
                part_raw = self.live.reads([(part, 0x68)])[0] if part else None
                row = {'limb': LIMBS[l['idx']] if 0 <= l['idx'] < len(LIMBS) else ('whole body' if l['idx'] == -1 else l['idx']),
                       'index': l['idx'], 'visibleFraction': _r(l['visible']), 'baseChance': _r(base_chance, 4),
                       'afterPerks': _r(final, 4), 'shownPercent': min(int(_f32(_f32(final) * 100.0)), 95),
                       'limbToHitPercent': part_raw[0x64] if part_raw else None,
                       'limbDamageMult': _r(struct.unpack_from('<f', part_raw, 0x5C)[0], 3) if part_raw else None}
                if l.get('assumedVisible'):
                    row['note'] = 'VATS is not targeting this ref: computed as if the limb were fully visible'
                if l['tile']:
                    row['menuShows'] = tile_text.get(l['tile'])
                    row['menuStoredChance'] = _r(l['stored'], 4)
                if include_terms and weapon:
                    row['terms'] = self._vats_terms(weapon, spread_bonus, dist, bound, l['visible'], part_raw, settings)
                rows.append(row)
            out_targets.append({'target': self._describe([t]).get(t), 'current': t == current, 'distance': _r(dist, 1),
                                'boundSize': _r(bound, 1), 'targetWeapon': self._describe([tweapon]).get(tweapon) if tweapon else None,
                                'limbs': rows})
        return {'vatsOpen': bool(menu), 'weapon': self._describe([weapon]).get(weapon) if weapon else 'unarmed',
                'spreadBonus': _r(spread_bonus if weapon else None, 4), 'targets': out_targets, 'settings': settings,
                'uses': ['CombatFormulas::GetVATSChanceToHit 0x00646F70 (12 args rebuilt as VATSMenu 0x007F1290 builds '
                         'them; that function is never called, it changes menu state)',
                         'ApplyPerkModifiers 0x005E58F0 ep 8 (can only raise the chance)', 'VATS target list 0x011DB150, '
                         'limb entries (index +0x20, visible +0x24, stored chance +0x28, tile +0x30)',
                         'BGSBodyPartData::GetBodyPart 0x005E50F0 (to-hit % +0x64)', 'distance 0x009A64D0, bound size 0x00571600, '
                         'spread GetCachedSpeed(3) 0x008B0DD0'],
                'note': 'shownPercent = the menu rule (VATSMenu 0x007F0EA0): (int)(chance x 100) in single precision, cap 95 (concentrated fire adds '
                        'iVATSConcentratedFireBonus per repeated shot at the same limb).'}

    def _equipped(self, actor):
        try:
            v = self.live.values(['GetEquippedObject 5'], actor)[0]
        except Exception:
            return 0
        if not v:
            return 0
        fid = v.get('formId') if isinstance(v, dict) else v
        try:
            return self.live.resolve(str(fid))['ptr'] if fid else 0
        except LiveError:
            return 0

    def _vats_terms(self, weapon, spread_bonus, dist, bound, visible, part_raw, s):
        try:
            min_spread = self._st0(0x00524B80, weapon, 0)  # TESObjectWEAP::GetMinSpreadDegrees(false)
        except LiveError:
            return None
        if min_spread is None:
            return None
        spread = min_spread + spread_bonus
        r = math.tan(spread * (s.get('fVATSSpreadMult') or 1) * 0.017453292) * dist
        cone = r * r * math.pi
        area = bound / 3 * bound
        if 2 * r < bound:
            area *= 2 * r / bound
        ratio = min(area / cone, s.get('fVATSRangeSpreadMax') or 2.0) if cone > 0 else 0
        w = min(s.get('fVATSScreenPercentFactor') or 0, 1.0)
        cond = w * visible + (1 - w)
        ammo = float(part_raw[0x64]) if part_raw else 1.0
        odds = (s.get('fVATSHitChanceMult') or 0) * cond * ratio * ammo
        return {'minSpreadDegrees': _r(min_spread, 4), 'spreadDegrees': _r(spread, 4), 'coneRadius': _r(r, 2),
                'coneArea': _r(cone, 1), 'targetArea': _r(area, 1), 'areaRatio': _r(ratio, 4),
                'visibilityFactor': _r(cond, 4), 'limbToHit': ammo, 'oddsPercent': _r(odds, 2),
                'cappedAt': s.get('fVATSMaxChance'),
                'formula': 'fVATSHitChanceMult x visibility x min(targetArea / coneArea, fVATSRangeSpreadMax) x limb '
                           'to-hit %; cone radius = tan(spread x fVATSSpreadMult) x distance; stealth and melee '
                           'branches are not shown'}

    # ================================================================== fnv_frame_profile
    def frame_profile(self, seconds=3.0, keep=False):
        self.live.in_game()
        installed, refused = [], []
        try:
            for h in self.agent.deep.hooks():
                if h['label'].startswith(FRAME_PREFIX):
                    self.agent.deep.remove(h['id'])
            for address, label, what in FRAME_HOOKS:
                raw = self.live.reads([(address, 24)])[0]
                clean = self.agent.deep._clean_bytes(address, 24)
                if raw is None or (clean is not None and raw[:8] != clean[:8] and raw[0] != 0xE9):
                    refused.append({'subsystem': label, 'reason': 'patched by another module or unreadable'})
                    continue
                try:
                    stolen, relocs = decode_prologue(raw)
                except Exception as e:
                    refused.append({'subsystem': label, 'reason': str(e)})
                    continue
                result = self.link.one('hook', word(address), word(stolen), ','.join('%x' % x for x in relocs) or '-',
                                       word(HF_EXIT | HF_TABLE), '0', '0', '0', '0', '0', '0', '0', '0',
                                       hexs(FRAME_PREFIX + label))
                if result.get('ok'):
                    installed.append((result['hook'], label, what, address))
                else:
                    refused.append({'subsystem': label, 'reason': result.get('error')})
            f0 = self.live.u32s([FRAME_COUNTER])[0]
            t0 = time.monotonic()
            time.sleep(max(0.5, min(float(seconds), 30.0)))
            f1 = self.live.u32s([FRAME_COUNTER])[0]
            wall = time.monotonic() - t0
            rows = []
            for h, label, what, address in installed:
                stats = self.agent.deep.table(h)
                count = sum(s['count'] for s in stats)
                ticks = sum(s['ticks'] for s in stats)
                mx = max((s['maxTicks'] for s in stats), default=0)
                freq = h['qpcFrequency']
                rows.append({'subsystem': label, 'what': what, 'function': '0x%08X' % address, 'calls': count,
                             'totalMs': _r(ticks * 1000.0 / freq, 2), 'maxMs': _r(mx * 1000.0 / freq, 3)})
        finally:
            if not keep:
                for h, *_ in installed:
                    try:
                        self.agent.deep.remove(h['id'])
                    except Exception:
                        pass
        frames = (f1 - f0) if f0 is not None and f1 is not None else None
        frame_row = next((r for r in rows if r['subsystem'] == 'frame'), None)
        n = frame_row['calls'] if frame_row and frame_row['calls'] else frames or 1
        for r in rows:
            r['msPerFrame'] = _r((r['totalMs'] or 0) / n, 3)
        total_ms = frame_row['totalMs'] if frame_row else None
        for r in rows:
            if total_ms and r is not frame_row:
                r['shareOfFrame'] = _r((r['totalMs'] or 0) / total_ms, 3)
        rows.sort(key=lambda r: -(r['totalMs'] or 0))
        not_called = [r['subsystem'] for r in rows if not r['calls']]
        rows = [r for r in rows if r['calls']]
        threads = self.live.u32s([0x011C3EA4 + 4])[0]
        return {'seconds': _r(wall, 2), 'frames': frames, 'fps': _r(frames / wall, 1) if frames else None,
                'msPerFrame': _r(total_ms / n, 2) if total_ms else None, 'subsystems': rows, 'notInstalled': refused or None,
                'notCalledOnMainThread': not_called or None,
                'iNumHWThreads': threads,
                'note': 'Inclusive times of main-thread functions (a child is also inside its parent); render includes '
                        'waiting for vsync / the GPU, so with vsync on it fills the frame up to the refresh rate. With '
                        'iNumHWThreads > 1 AI runs on worker threads: its cost shows as ai_wait (the main thread waiting '
                        'for them). Everything outside the listed children is the frame minus their sum.',
                'uses': ['MainLoop 0x0086E650 and its children (research from the decomp main loop)',
                         'companion timing hooks (QPC, per-function call table)', 'frame counter 0x011A2FE0']}



    # ================================================================== fnv_window
    INPUT_GLOBALS = 0x011F35CC      # OSInputGlobals* (mouse device +0x30, flags +0x04, bit 8 = background mouse)
    DI_ACQUIRE, DI_UNACQUIRE, DI_SET_COOPERATIVE = 7, 8, 13  # IDirectInputDevice8 vtable slots

    def _game_window(self):
        pid = (self.agent.game.process() or [None])[0]
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
                found.append(hwnd)
            return True

        user32.EnumWindows(callback, 0)
        return found[0] if found else None

    def _mouse_mode(self, hwnd, background):
        ig = self.live.u32s([self.INPUT_GLOBALS])[0]
        device, flags = self.live.u32s([ig + 0x30, ig + 4]) if ig else (None, None)
        if not device:
            raise LiveError('NO_MOUSE_DEVICE', 'The game has no DirectInput mouse device yet')
        vt = self.live.u32s([device])[0]
        acquire, unacquire, coop = self.live.u32s([vt + 4 * self.DI_ACQUIRE, vt + 4 * self.DI_UNACQUIRE,
                                                   vt + 4 * self.DI_SET_COOPERATIVE])
        # DISCL_EXCLUSIVE 1, NONEXCLUSIVE 2, FOREGROUND 4, BACKGROUND 8 (dinput.h); COM stdcall, this first
        level = (2 | 8) if background else (1 | 4)
        self._call(unacquire, 0, device)
        hr = self._eax(coop, 0, device, hwnd, level)
        self.live.link.write(ig + 4, struct.pack('<I', (flags | 8) if background else (flags & ~8)))
        self._call(acquire, 0, device)
        return {'hresult': '0x%08X' % hr, 'cooperativeLevel': 'non-exclusive, background' if background else
                'exclusive, foreground', 'device': '0x%08X' % device}

    def window(self, action='status', persist=None):
        """status | release_mouse | capture_mouse | unpause; persist true/false writes bBackground Mouse to the
        dev profile's Fallout.ini so every launch starts that way."""
        out = {'action': action}
        if persist is not None:
            out['ini'] = self._persist_background_mouse(bool(persist))
        running = bool(self.agent.game.process())
        if not running:
            if action != 'status':
                raise LiveError('NOT_RUNNING', 'The game is not running (persist works without it)')
            out['state'] = {'running': False, 'iniBackgroundMouse': self._read_background_mouse()}
            return out
        self.live.ready()
        hwnd = self._game_window()
        user32 = ctypes.windll.user32
        if action in ('release_mouse', 'capture_mouse'):
            if not hwnd:
                raise LiveError('NO_WINDOW', 'The game window was not found')
            out.update(self._mouse_mode(hwnd, action == 'release_mouse'))
            if action == 'release_mouse':
                user32.ClipCursor(None)
            self.live.journal.add({'kind': 'window', 'action': action, 'undo': None})
        elif action == 'unpause':
            visible = {m['id'] for m in self.agent.menus()['visible']}
            if 1013 in visible:  # StartMenu: the pause menu (opens when the window loses focus)
                self.agent.menu_control('close', menu=1013)
                out['closed'] = 'StartMenu (pause)'
            else:
                out['note'] = 'The pause menu was not open'
        ig = self.live.u32s([self.INPUT_GLOBALS])[0]
        flags = self.live.u32s([ig + 4])[0] if ig else None
        visible = [m['name'] for m in self.agent.menus()['visible']]
        out['state'] = {'running': True, 'backgroundMouse': bool(flags & 8) if flags is not None else None,
                        'gameWindowFocused': bool(hwnd) and user32.GetForegroundWindow() == hwnd,
                        'pauseMenuOpen': 'StartMenu' in visible, 'menus': visible,
                        'iniBackgroundMouse': self._read_background_mouse()}
        out['uses'] = ['OSInputGlobals 0x011F35CC (mouse device +0x30, flags +4 bit 8 = background mouse, as '
                       'MouseInput RecreateDevice 0x00A23C00 sets it)', 'IDirectInputDevice8 Unacquire / '
                       'SetCooperativeLevel(game window, NONEXCLUSIVE|BACKGROUND) / Acquire',
                       'INI bBackground Mouse ([Controls], read at input start-up 0x00A22660)']
        return out

    def _ini_path(self):
        from pathlib import Path
        fnv = self.live.project.fnv
        return Path(fnv.mo2_root) / 'profiles' / fnv.mo2_profile / 'Fallout.ini'

    def _read_background_mouse(self):
        try:
            for line in self._ini_path().read_text(encoding='utf-8', errors='replace').splitlines():
                if line.strip().lower().startswith('bbackground mouse='):
                    return line.split('=', 1)[1].strip() == '1'
        except OSError:
            return None
        return None

    def _persist_background_mouse(self, on):
        import shutil
        path = self._ini_path()
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        value = 'bBackground Mouse=%d' % (1 if on else 0)
        idx = next((i for i, l in enumerate(lines) if l.strip().lower().startswith('bbackground mouse=')), None)
        if idx is not None and lines[idx].strip() == value:
            return {'path': str(path), 'changed': False, 'value': value}
        backup = path.with_name('Fallout.ini.fnvmcp-backup')
        if not backup.exists():
            shutil.copy2(path, backup)
        if idx is not None:
            lines[idx] = value
        else:
            section = next((i for i, l in enumerate(lines) if l.strip().lower() == '[controls]'), None)
            if section is None:
                lines += ['[Controls]', value]
            else:
                lines.insert(section + 1, value)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return {'path': str(path), 'changed': True, 'value': value, 'backup': str(backup),
                'note': 'Takes effect at the next launch (release_mouse applies it to the running game now).'}


# ================================================================== address space (outside the game)
class _MBI(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p), ('AllocationProtect', wintypes.DWORD),
                ('PartitionId', wintypes.WORD), ('RegionSize', ctypes.c_size_t), ('State', wintypes.DWORD),
                ('Protect', wintypes.DWORD), ('Type', wintypes.DWORD)]


def address_space(pid, limit=0x80000000):
    """Committed / reserved / free MB of a 32-bit process's user address space, and the largest free block.
    VirtualQueryEx fails past the end of the process's user space (2 GB, or 4 GB for a large-address-aware exe)."""
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t]
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        return None
    try:
        mbi, address = _MBI(), 0x10000
        committed = reserved = free = largest = 0
        while address < limit:
            if not k32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            size = min(mbi.RegionSize, limit - address)
            if mbi.State == 0x1000:
                committed += size
            elif mbi.State == 0x2000:
                reserved += size
            elif mbi.State == 0x10000:
                free += size
                largest = max(largest, size)
            address += size
        mb = 1048576.0
        return {'committed': round(committed / mb), 'reserved': round(reserved / mb), 'free': round(free / mb),
                'largestFreeBlock': round(largest / mb), 'limit': round(limit / mb),
                'used': round((committed + reserved) / mb)}
    finally:
        k32.CloseHandle(handle)
