"""Engine tools built from the game's own decompiled AI, combat, perk, pathing and world code.

fnv_detection, fnv_reaction, fnv_combat, fnv_perk_entries, fnv_pathing, fnv_raycast and fnv_cell_grid read the
engine's own state and call its own read-only functions (on the main thread through the engine companion).
Addresses, layouts and the safe/unsafe split come from reading the decomp; many of these functions are not
proven names, so every result lists the functions it used ('uses') and each tool was checked in the running game
(docs/FNV-LIVE-TOOLS.md, "Engine AI tools").
"""
import math
import struct

from .fnv_deep import HF_EXIT, HF_RECORD, HookError, decode_prologue
from .fnv_link import hexs, word
from .fnv_live import LiveError

SEL_ARG = {i: 2 + i for i in range(8)}
SEL_STACK8 = 14  # companion selector: address of stack argument 8 (snapshot = arguments 8..31)

# ------------------------------------------------------------------------------------------- detection
CALC_DETECTION = 0x008A0D10       # Actor::CalculateDetectionLevel (probable), thiscall, 7 stack args
DETECTION_FORMULA = 0x00642ED0    # the detection formula (decomp mislabels it ComputeDamageResult), cdecl 25 args
PROC_GET_DETECTION_STATE = 0x504  # HighProcess vtable: GetDetectionState(actor, type) 0x008F6650
PROC_LIGHT = 0x734                # process vtable: actor light level (float)
DETECTION_LEVELS = {0: 'lost', 1: 'unseen', 2: 'noticed', 3: 'seen'}
FORMULA_ARGS = ['perception', 'sneakSkill', 'lineOfSight', 'inViewCone', 'distance', 'light', 'unused',
                'invisible', 'bootWeight', 'targetMoving', 'targetSneaking', 'targetRunning', 'observerSleeping',
                'observerInCombat', 'targetActionCount', 'sizeClass', 'outdoors', 'observerHasOtherTarget', 'ambush',
                'outSound', 'outVisual', 'outSkill', 'observerLevel', 'targetLevel', 'armorPenalty']
FORMULA_FLOATS = {'distance'}  # light is passed as int (call site in 0x008A0D10)
DETECTION_SETTINGS = {
    'fSneakBaseValue': 0x011CD7E4, 'fSneakMaxDistance': 0x011CD7D8, 'fSneakExteriorDistanceMult': 0x011CDCBC,
    'fSneakDistanceAttenuationExponent': 0x011CD304, 'fSneakSoundsMult': 0x011CE024, 'fSneakSoundLosMult': 0x011CD2E8,
    'fSneakLightMult': 0x011CD08C, 'fSneakLightMoveMult': 0x011CDD18, 'fSneakLightRunMult': 0x011CD2DC,
    'fDetectionSneakLightMod': 0x011CDB84, 'fSneakSkillMult': 0x011CDA04, 'fSneakRunningMult': 0x011CD148,
    'fSneakBootWeightBase': 0x011CD258, 'fSneakBootWeightMult': 0x011CDC80, 'fSneakActionMult': 0x011CD080,
    'fSneakSleepBonus': 0x011CD774, 'fSneakAlertMod': 0x011CDFC0, 'fSneakCombatMod': 0x011CDBD4,
    'fSneakAmbushTargetMod': 0x011CDBF8, 'fSneakAmbushNonTargetMod': 0x011CD1E0,
    'fSneakPerceptionSkillMin': 0x011CDF04, 'fSneakPerceptionSkillMax': 0x011CDD9C,
    'fSneakStealthBoyMult': 0x011CD310, 'fDetectionViewCone': 0x011CD668, 'fDetectionNightEyeBonus': 0x011D0F90,
    'fSneakSeenMin': 0x011CDF10, 'fSneakNoticedMin': 0x011CD45C, 'fSneakLostMin': 0x011CDA4C,
    'fSneakUnseenMin': 0x011CDE5C,
}
INT_SETTINGS = {'iSneakLevelBonus': 0x011CD13C, 'iSneakStartBonus': 0x011CD8C0}

# ------------------------------------------------------------------------------------------- reaction
IS_HOSTILE_TO = 0x008B06D0         # Actor::IsHostileTo (decomp; backs GetShouldAttack), thiscall 4 args
FIGHT_REACTION = 0x008B87A0        # Actor::GetFactionFightReaction (probable), thiscall 2 args
FACTION_REACTION = 0x008B7FE0      # Actor::GetFactionReaction (proven), thiscall 4 args
GET_DISPOSITION = 0x0087FD90       # Actor::GetDisposition (proven), thiscall 2 args
IS_ANGRY_WITH_PLAYER = 0x008BFFC0  # Actor::IsAngryWithPlayer (proven)
IS_IN_COMBAT_WITH = 0x008BC700     # Actor::IsInCombatWithActor (proven)
FIGHT = {0: 'neutral', 1: 'enemy', 2: 'ally', 3: 'friend'}
AGGRESSION = {0: 'unaggressive', 1: 'aggressive', 2: 'very aggressive', 3: 'frenzied'}

# ------------------------------------------------------------------------------------------- combat
GET_COMBAT_CONTROLLER = 0x008A02D0  # Actor::GetCombatController (proven)
GET_COMBAT_STYLE = 0x008A7F80       # Actor::GetCombatStyle (decomp)
PROCEDURES = ['attack ranged', 'attack melee', 'attack grenade', 'attack low', 'evade', 'switch weapon', 'move',
              'be in cover', 'activate object', 'hide from target', 'search', 'use combat item', 'engage target']
ACTIONS = ['attack ranged explosive', 'attack ranged explosive from cover', 'attack ranged', 'attack ranged from cover',
           'attack grenade', 'grenade flush target', 'grenade from cover', 'attack melee', 'attack hand to hand', 'move',
           'move and switch to melee', 'move and attack ranged', 'move and attack ranged explosive',
           'move and attack grenade', 'draw weapon', 'switch weapon', 'avoid threat', 'search', 'investigate', 'dodge',
           'ignore blocked target', 'flee', 'activate combat item', 'use combat item', 'acquire line of sight', 'hide',
           'approach target']

# ------------------------------------------------------------------------------------------- perks
APPLY_PERK_MODIFIERS = 0x005E58F0   # ApplyPerkModifiers (proven), cdecl varargs (ep, actor, filters..., args...)
APPLY_FUNCTION = 0x005E5B40         # BGSEntryPointFunction::ApplyFunction (decomp), cdecl 6 args
PERK_PREFIX = 'fnvmcp_perk_'
ENTRY_POINTS = [
    ('Calculate Weapon Damage', 3), ('Calculate My Critical Hit Chance', 3), ('Calculate My Critical Hit Damage', 3),
    ('Calculate Weapon Attack AP Cost', 2), ('Calculate Mine Explode Chance', 2), ('Adjust Range Penalty', 2),
    ('Adjust Limb Damage', 3), ('Calculate Weapon Range', 2), ('Calculate To Hit Chance', 3),
    ('Adjust Experience Points', 1), ('Adjust Gained Skill Points', 1), ('Adjust Book Skill Points', 1),
    ('Modify Recovered Health', 1), ('Calculate Inventory AP Cost', 1), ('Get Disposition', 2),
    ('Get Should Attack', 2), ('Get Should Assist', 3), ('Calculate Buy Price', 2), ('Get Bad Karma', 1),
    ('Get Good Karma', 1), ('Ignore Locked Terminal', 1), ('Add Leveled List On Death', 2), ('Get Max Carry Weight', 1),
    ('Modify Addiction Chance', 1), ('Modify Addiction Duration', 1), ('Modify Positive Chem Duration', 1),
    ('Adjust Drinking Radiation', 1), ('Activate', 2), ('Mysterious Stranger', 1), ('Has Paralyzing Palm', 1),
    ('Hacking Science Bonus', 1), ('Ignore Running During Detection', 1), ('Ignore Broken Lock', 1),
    ('Has Concentrated Fire', 1), ('Calculate Gun Spread', 2), ('Player Kill AP Reward', 3),
    ('Modify Enemy Critical Hit Chance', 3), ('Reload Speed', 2), ('Equip Speed', 2), ('Action Point Regen', 2),
    ('Action Point Cost', 2), ('Miss Fortune', 1), ('Modify Run Speed', 1), ('Modify Attack Speed', 2),
    ('Modify Radiation Consumed', 1), ('Has Pip Hacker', 1), ('Has Meltdown', 1), ('See Enemy Health', 1),
    ('Has Jury Rigging', 1), ('Modify Threat Range', 2), ('Modify Threat', 2), ('Has Fast Travel Always', 1),
    ('Knockdown Chance', 2), ('Modify Weapon Strength Req', 2), ('Modify Aiming Move Speed', 2),
    ('Modify Light Items', 1), ('Modify Damage Threshold (defender)', 3), ('Modify Chance for Ammo Item', 2),
    ('Modify Damage Threshold (attacker)', 3), ('Modify Throwing Velocity', 2), ('Chance for Item On Fire', 2),
    ('Has Unarmed Forward Power Attack', 1), ('Has Unarmed Back Power Attack', 1),
    ('Has Unarmed Crouched Power Attack', 1), ('Has Unarmed Counter Attack', 1), ('Has Unarmed Left Power Attack', 1),
    ('Has Unarmed Right Power Attack', 1), ('VATS HelperChance', 1), ('Modify Item Damage', 1),
    ('Has Improved Detection', 1), ('Has Improved Spotting', 1), ('Has Improved Item Detection', 1),
    ('Adjust Explosion Radius', 2), ('Adjust Heavy Weapon Weight', 1)]
NON_VALUE_EPS = {21: 'leveled list', 27: 'activate'}
PERK_FUNCTIONS = {1: 'set', 2: 'add', 3: 'multiply', 4: 'add range', 5: 'add actor value mult', 6: 'absolute',
                  7: 'negative absolute', 8: 'add leveled list', 9: 'add activate choice'}

# ------------------------------------------------------------------------------------------- pathing / world
G_TES = 0x011DEA10
TES_PICK = 0x00458420              # TES::Pick wrapper (bytes checked), thiscall (bhkPickData*)
PICK_CTOR, PICK_FROM, PICK_TO, PICK_FILTER = 0x004A3C20, 0x004A3DA0, 0x004A3EB0, 0x004A3F70
FIND_REF_FOR_3D = 0x0056F930       # TESObjectREFR::FindReferenceFor3D (proven), cdecl
LAND_HEIGHT = 0x004572E0           # TES::GetLandHeightAt (probable)
LINE_OF_SIGHT = 0x0088B880         # Actor::LineOfSight (proven)
CELL_GRID_X, CELL_GRID_Y = 0x00544C30, 0x00544C60
PATH_STATES = {1: 'initializing', 2: 'moving', 3: 'moving while waiting', 4: 'waiting for updated path',
               5: 'goal reached', 6: 'rotating without path', 7: 'rotating without path'}
AVOID_STATES = {1: 'ok', 2: 'waiting', 3: 'repathing (building)', 4: 'repath ignore', 5: 'failed'}
MOVER_STATES = {0: 'none', 1: 'building path', 3: 'path received', 4: 'moving', 5: 'done', 6: 'idle', 7: 'path failed',
                8: 'keep'}
CELL_STATES = {0: 'not loaded', 1: 'not loaded', 2: 'loading', 3: 'loading', 4: 'loading', 5: 'attaching',
               6: 'attached'}
SETTING_GRIDS, SETTING_INTERIOR_BUFFER, SETTING_EXTERIOR_BUFFER = 0x011C63CC, 0x011C3E38, 0x011C3C90


def _f(bits):
    return struct.unpack('<f', struct.pack('<I', bits & 0xFFFFFFFF))[0]


def _fw(value):
    return struct.unpack('<I', struct.pack('<f', float(value)))[0]


def _r(x, n=3):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, n)


class EngineAITools:
    def __init__(self, agent):
        self.agent = agent

    @property
    def live(self):
        return self.agent.live

    @property
    def link(self):
        return self.agent.link

    # ------------------------------------------------------------------ helpers
    def _actor(self, spec):
        r = self.live.resolve(spec)
        cls = (self.live.rtti([r['ptr']]).get(r['ptr']) or {}).get('class')
        if cls not in ('Actor', 'Character', 'Creature', 'PlayerCharacter'):
            raise LiveError('NOT_AN_ACTOR', '%s is a %s, not an actor reference' % (spec, cls or 'non-object'))
        r['class'] = cls
        return r

    def _call(self, fn, ecx=0, *args, ret='i'):
        result = self.link.one('call', word(fn), ret, word(ecx), '0', *[word(a) for a in args])
        if not result.get('ok'):
            raise LiveError('CALL_FAILED', 'Engine call 0x%08X failed: %s' % (fn, result.get('error')), result)
        return result

    def _eax(self, fn, ecx=0, *args):
        return int(self._call(fn, ecx, *args)['eax'], 16)

    def _floats(self, addresses):
        return [None if v is None else _f(v) for v in self.live.u32s(addresses)]

    def _describe(self, pointers):
        d = self.agent.describe_pointers([p for p in pointers if p])
        return {p: {k: v for k, v in (d.get(p) or {}).items() if k in ('formId', 'editorId', 'name', 'class')} or None
                for p in pointers}

    def _names(self, pointers):
        d = self._describe(pointers)
        return lambda p: d.get(p) if p else None

    def _settings(self, table, integer=False):
        values = self.live.u32s([a + 4 for a in table.values()])
        out = {}
        for (name, _), v in zip(table.items(), values):
            out[name] = None if v is None else (struct.unpack('<i', struct.pack('<I', v))[0] if integer else _r(_f(v), 4))
        return out

    def _hook(self, address, label, nargs, deref=(0, 0), snap=(0, 0), filt=(0, 0)):
        for h in self.agent.deep.hooks():
            if int(h['target'], 16) == address:
                if h['label'] == label:
                    self.agent.deep.remove(h['id'])
                    break
                raise HookError('ALREADY_HOOKED', '0x%08X already carries the "%s" hook' % (address, h['label']))
        raw = self.live.reads([(address, 24)])[0]
        if raw is None:
            raise HookError('UNREADABLE', 'Cannot read 0x%08X' % address)
        stolen, relocs = decode_prologue(raw)
        result = self.link.one('hook', word(address), word(stolen), ','.join('%x' % r for r in relocs) or '-',
                               word(HF_RECORD | HF_EXIT), word(filt[0]), word(filt[1]), '0', word(nargs),
                               word(deref[0]), word(deref[1]), word(snap[0]), word(snap[1]), hexs(label))
        if not result.get('ok'):
            raise HookError('HOOK_REFUSED', 'The companion refused the hook on 0x%08X: %s' % (address, result.get('error')),
                            result)
        return result['hook']

    def _seq(self, h):
        return self.live.u32s([int(h['seqAddress'], 16)])[0] or 0

    def _unhook(self, h):
        try:
            self.agent.deep.remove(h['id'])
        except Exception:
            pass

    # ================================================================== fnv_detection
    def detection(self, observer, target='player', recompute=True):
        self.live.in_game()
        obs, tgt = self._actor(observer), self._actor(target)
        o, t = obs['ptr'], tgt['ptr']
        procs = self.live.u32s([o + 0x68, t + 0x68])
        records = {}
        uses = ['HighProcess::GetDetectionState (class_proven, process vtable +0x504)',
                'DetectionViewData layout (decomp ctor 0x0066E590)']

        def state(proc, other, kind):
            if not proc:
                return None
            vt = self.live.u32s([proc])[0]
            fn = self.live.u32s([vt + PROC_GET_DETECTION_STATE])[0] if vt else None
            if not fn:
                return None
            rec = self._eax(fn, proc, other, kind)
            if not rec:
                return None
            raw = self.live.reads([(rec, 0x24)])[0]
            if not raw:
                return None
            level, score = struct.unpack_from('<ii', raw, 4)
            x, y, z, when = struct.unpack_from('<4f', raw, 0x0C)
            return {'state': DETECTION_LEVELS.get(level, level), 'score': None if score == 0x7FFFFFFF else score,
                    'lineOfSightInViewCone': bool(raw[0x1E]), 'lineOfSightAnyDirection': bool(raw[0x1C]),
                    'targetWasInCombat': bool(raw[0x1D]), 'noiseScore': struct.unpack_from('<i', raw, 0x20)[0],
                    'lastSeenAt': {'x': _r(x, 1), 'y': _r(y, 1), 'z': _r(z, 1)}, 'timestamp': _r(when, 2)}

        records['observerDetectsTarget'] = state(procs[0], t, 0)
        records['targetDetectsObserver'] = state(procs[1], o, 0)
        values = self.live.values(['GetAV Perception', 'GetLevel', 'IsInCombat', 'GetSleeping', 'GetDetected %s' % tgt['expr']
                                   if tgt.get('expr') else 'GetDetected player'], o)
        tvalues = self.live.values(['GetAV Sneak', 'GetLevel', 'IsSneaking', 'IsMoving', 'IsRunning',
                                    'GetAV Chameleon', 'GetAV Invisibility', 'GetDistance %s' % obs['expr']
                                    if obs.get('expr') else 'GetDistance player'], t)
        light = None
        if procs[1]:
            vt = self.live.u32s([procs[1]])[0]
            fn = self.live.u32s([vt + PROC_LIGHT])[0] if vt else None
            if fn:
                light = self._call(fn, procs[1], ret='f').get('st0')
        settings = self._settings(DETECTION_SETTINGS)
        settings.update(self._settings(INT_SETTINGS, integer=True))
        out = {'observer': {'ref': obs['formId'], 'class': obs['class'], 'perception': values[0], 'level': values[1],
                            'inCombat': bool(values[2]), 'sleepState': values[3], 'highProcess': bool(procs[0])},
               'target': {'ref': tgt['formId'], 'class': tgt['class'], 'sneakSkill': tvalues[0], 'level': tvalues[1],
                          'sneaking': bool(tvalues[2]), 'moving': bool(tvalues[3]), 'running': bool(tvalues[4]),
                          'chameleon': tvalues[5], 'invisibility': tvalues[6], 'light': _r(light, 2)},
               'distance': _r(tvalues[7], 1), 'getDetected': bool(values[4]),
               'committed': records, 'settings': settings}
        if recompute and obs['class'] != 'PlayerCharacter':
            out['recomputed'] = self._recompute(o, t)
            uses += ['Actor::CalculateDetectionLevel 0x008A0D10 (probable; calc=1, no record writes for NPC observers)',
                     'detection formula 0x00642ED0 (decomp; hooked to capture its 25 inputs)']
        elif recompute:
            out['recomputedNote'] = ('Recomputing with the player as observer writes detection records, so it is not '
                                     'done; the committed records are shown.')
        out['explanation'] = self._explain_detection(out)
        out['uses'] = uses
        return out

    def _recompute(self, o, t):
        buf = self.agent._buffer() + 0x200
        h = self._hook(DETECTION_FORMULA, 'fnvmcp_detection', 8, snap=(SEL_STACK8, 96))
        try:
            since = self._seq(h)
            main = self.link.ping().get('mainThreadId')
            self.link.one('wr', word(buf), '0000000000000000')
            level = struct.unpack('<i', struct.pack('<I', self._eax(CALC_DETECTION, o, 1, t, buf, 0, 0, 0, buf + 4)))[0]
            flags = self.live.reads([(buf, 8)])[0] or b'\0' * 8
            seq, recs, _ = self.agent.deep.records(h, since, limit=64)
        finally:
            self._unhook(h)
        mine = [r for r in recs if r[2] == main] or recs
        out = {'level': level, 'inViewCone': bool(flags[0]), 'lineOfSight': bool(flags[4])}
        if not mine:
            out['formula'] = None
            out['formulaNote'] = 'The formula was not reached (out of range, no 3D, or a teammate rule returned early).'
            return out
        r = mine[-1]
        words = list(r[12:20]) + list(struct.unpack('<24I', struct.pack('<24I', *r[24:48])))
        inputs = {}
        for name, w in zip(FORMULA_ARGS, words):
            if name.startswith('out') or name == 'unused':
                continue
            if name in FORMULA_FLOATS:
                inputs[name] = _r(_f(w), 2)
            else:
                value = struct.unpack('<i', struct.pack('<I', w))[0]
                inputs[name] = bool(value & 0xFF) if name in ('lineOfSight', 'inViewCone', 'invisible', 'targetMoving',
                                                              'targetSneaking', 'targetRunning', 'observerSleeping',
                                                              'observerInCombat', 'outdoors', 'observerHasOtherTarget') \
                    else value
        result = struct.unpack('<i', struct.pack('<I', r[8]))[0]
        out['formula'] = {'inputs': inputs, 'result': result,
                          'note': 'Inputs exactly as the game passed them to its detection formula, named from its call site in '
                                  'CalculateDetectionLevel 0x008A0D10 (distance is a float, the rest ints/bools).'}
        return out

    @staticmethod
    def _explain_detection(out):
        why = []
        rec = (out.get('committed') or {}).get('observerDetectsTarget')
        s = out['settings']
        if not out['observer']['highProcess']:
            why.append('The observer is not in high process: only high-process actors run detection.')
        if rec:
            why.append('The game currently records the target as %s by the observer (score %s; seen above %s, '
                       'noticed above %s).' % (rec['state'], rec['score'], s.get('fSneakSeenMin'), s.get('fSneakNoticedMin')))
            if not rec['lineOfSightInViewCone']:
                why.append('No line of sight inside the observer\'s view cone (%s degrees), so only sound counts.'
                           % s.get('fDetectionViewCone'))
        else:
            why.append('There is no detection record for this pair yet (not in range, or detection has not run).')
        rc = out.get('recomputed') or {}
        f = (rc.get('formula') or {}).get('inputs') or {}
        if f:
            maxd = (s.get('fSneakMaxDistance') or 0) * ((s.get('fSneakExteriorDistanceMult') or 1) if f.get('outdoors') else 1)
            if f.get('distance') is not None and maxd and f['distance'] > maxd:
                why.append('Distance %.0f is beyond the detection range %.0f.' % (f['distance'], maxd))
            if f.get('targetSneaking'):
                why.append('The target is sneaking: their Sneak skill %s is subtracted.' % f.get('sneakSkill'))
            if f.get('invisible'):
                why.append('The target is invisible (Stealth Boy / chameleon): visual detection is reduced.')
            if f.get('targetRunning'):
                why.append('The target is running: movement noise is multiplied.')
            if f.get('light') is not None:
                why.append('Target light level %.0f feeds the visual term.' % f['light'])
            why.append('Recomputed score now: %s.' % rc.get('level'))
        return why

    # ================================================================== fnv_reaction
    def reaction(self, actor, other='player'):
        self.live.in_game()
        a, b = self._actor(actor), self._actor(other)
        p, q = a['ptr'], b['ptr']
        buf = self.agent._buffer() + 0x240
        self.link.one('wr', word(buf), '00' * 32)
        hostile = bool(self._eax(IS_HOSTILE_TO, p, q, 0, buf, 0) & 0xFF)
        relation_out = self.live.u32s([buf])[0]
        fight = self._eax(FIGHT_REACTION, p, q, buf + 4)
        numeric = struct.unpack('<i', struct.pack('<I', self._eax(FACTION_REACTION, p, q, buf + 8, buf + 12, buf + 16)))[0]
        found, fa, fb = self.live.u32s([buf + 8, buf + 12, buf + 16])
        pc_enemy = (self.live.reads([(buf + 4, 1)])[0] or b'\0')[0]
        disposition = self._eax(GET_DISPOSITION, p, q, 0) if b['class'] in ('PlayerCharacter', 'Character', 'Actor') else None
        angry = bool(self._eax(IS_ANGRY_WITH_PLAYER, p) & 0xFF) if b['class'] == 'PlayerCharacter' else None
        b_fights_a = bool(self._eax(IS_IN_COMBAT_WITH, q, p) & 0xFF)
        teammate = (self.live.reads([(p + 0x18D, 1), (q + 0x18D, 1)]))
        av = self.live.values(['GetAV Aggression', 'GetAV Confidence', 'GetAV Assistance', 'GetAV Responsibility',
                               'IsInCombat', 'GetCombatTarget'], p)
        names = self._names([fa, fb])
        aggr = int(av[0]) if av[0] is not None else None
        rel = fight & 0xFF
        rule = self._hostility_rule(aggr, rel, b_fights_a, angry, bool(teammate[0] and teammate[0][0]),
                                    bool(teammate[1] and teammate[1][0]), hostile)
        return {'actor': a['formId'], 'other': b['formId'],
                'hostile': hostile, 'fightReaction': FIGHT.get(rel, rel), 'fightReactionFromHostilityCheck':
                    FIGHT.get(relation_out & 0xFF, relation_out),
                'otherIsPlayerEnemyOfFaction': bool(pc_enemy),
                'factionReaction': {'value': numeric,
                                    'decidedBy': [names(fa), names(fb)]},
                'disposition': disposition, 'angryWithPlayerFromCrime': angry, 'otherInCombatWithActor': b_fights_a,
                'playerTeammate': {'actor': bool(teammate[0] and teammate[0][0]), 'other': bool(teammate[1] and teammate[1][0])},
                'aiData': {'aggression': AGGRESSION.get(aggr, aggr), 'confidence': av[1], 'assistance': av[2],
                           'responsibility': av[3]},
                'inCombat': bool(av[4]), 'combatTarget': av[5], 'rule': rule,
                'uses': ['Actor::IsHostileTo 0x008B06D0 (decomp; backs GetShouldAttack and CanAttackActor)',
                         'Actor::GetFactionFightReaction 0x008B87A0 (probable)', 'Actor::GetFactionReaction 0x008B7FE0 (proven)',
                         'Actor::GetDisposition 0x0087FD90 (proven)', 'Actor::IsAngryWithPlayer 0x008BFFC0 (proven)',
                         'Actor::IsInCombatWithActor 0x008BC700 (proven)', 'player-teammate flag Actor+0x18D (decomp)']}

    @staticmethod
    def _hostility_rule(aggr, rel, forced, angry, teammate, other_teammate, hostile):
        """The branch of IsHostileTo (0x008B06D0) that decides, re-derived from its inputs."""
        if teammate:
            return 'The actor is a player teammate: teammates follow the player (hostile to the player only when angry, ' \
                   'to others only when they fight the player or the teammate).'
        if other_teammate:
            return 'The other actor is a player teammate: the check is made against the player instead.'
        if aggr == 3:
            return 'Aggression is frenzied (3): hostile to everyone.'
        if angry:
            return 'The actor is angry with the player because of a crime (ExtraPlayerCrimeList): hostile.'
        if forced:
            return 'The other actor is already in combat with this actor: hostile unless they are friends or allies ' \
                   '(fight reaction is %s).' % FIGHT.get(rel, rel)
        if aggr == 2:
            return 'Aggression very aggressive (2): attacks neutrals and enemies; fight reaction is %s, so %s.' % (
                FIGHT.get(rel, rel), 'hostile' if rel <= 1 else 'not hostile')
        if aggr == 1:
            return 'Aggression aggressive (1): attacks enemies only; fight reaction is %s, so %s.' % (
                FIGHT.get(rel, rel), 'hostile' if rel == 1 else 'not hostile')
        if aggr == 0:
            return 'Aggression unaggressive (0): never starts a fight on its own%s.' % (
                ' (a perk entry made it hostile)' if hostile else '')
        return 'Result %s.' % ('hostile' if hostile else 'not hostile')

    # ================================================================== fnv_combat
    def combat(self, actor):
        self.live.in_game()
        a = self._actor(actor)
        p = a['ptr']
        av = self.live.values(['IsInCombat', 'GetCombatTarget', 'GetAV Aggression', 'GetAV Confidence', 'GetAV Health',
                               'GetEquippedObject 5', 'GetAV Perception'], p)
        style = self._eax(GET_COMBAT_STYLE, p)
        out = {'actor': a['formId'], 'inCombat': bool(av[0]), 'combatTarget': av[1],
               'aggression': AGGRESSION.get(int(av[2]), av[2]) if av[2] is not None else None, 'confidence': av[3],
               'health': av[4], 'equippedWeapon': av[5]}
        uses = ['Actor::GetCombatController 0x008A02D0 (proven)', 'Actor::GetCombatStyle 0x008A7F80 (decomp)',
                'CombatController layout (decomp ctor 0x0097D3A0 and LoadGame 0x00981D00)']
        if a['class'] == 'PlayerCharacter':
            out['controller'] = None
            out['note'] = 'The player has no combat controller.'
            out['uses'] = uses
            return out
        ctrl = self._eax(GET_COMBAT_CONTROLLER, p)
        out['combatStyle'] = self._describe([style]).get(style) if style else None
        if not ctrl:
            out['controller'] = None
            out['note'] = 'No combat controller: the actor is not running a combat package.'
            out['uses'] = uses
            return out
        raw = self.live.reads([(ctrl + 0x80, 0x78)])[0]
        group, proc, queued = struct.unpack_from('<III', raw, 0)
        action, goal = struct.unpack_from('<II', raw, 0x28)
        me, target = struct.unpack_from('<II', raw, 0x3C)
        weapon, cstyle = struct.unpack_from('<II', raw, 0x70)
        classes = self.live.rtti([x for x in (ctrl, proc, queued, action, goal) if x])

        def kind(ptr, prefix):
            cls = (classes.get(ptr) or {}).get('class') if ptr else None
            if not cls:
                return None
            words = cls[len(prefix):] if cls.startswith(prefix) else cls
            return ''.join(' ' + ch.lower() if ch.isupper() else ch for ch in words).strip()
        procedure = None
        if proc:
            pr = self.live.reads([(proc, 0x10)])[0] or b'\0' * 16
            procedure = {'type': kind(proc, 'CombatProcedure'),
                         'status': {0: 'active', 1: 'failed', 2: 'finished'}.get(struct.unpack_from('<I', pr, 8)[0])}
        act = {'type': kind(action, 'CombatAction')} if action else None
        targets, members = [], []
        if group:
            graw = self.live.reads([(group, 0x30)])[0]
            tbuf, tsize = struct.unpack_from('<II', graw, 0x0C)
            mbuf, msize = struct.unpack_from('<II', graw, 0x1C)
            if tbuf and 0 < tsize < 64:
                traw = self.live.reads([(tbuf, 0x68 * tsize)])[0] or b''
                for k in range(tsize):
                    e = traw[k * 0x68:(k + 1) * 0x68]
                    if len(e) < 0x68:
                        break
                    x, y, z = struct.unpack_from('<3f', e, 8)
                    targets.append({'ptr': struct.unpack_from('<I', e, 0)[0], 'attackers': struct.unpack_from('<H', e, 0x4A)[0],
                                    'lastKnownPosition': {'x': _r(x, 1), 'y': _r(y, 1), 'z': _r(z, 1)},
                                    'lastDetected': _r(_f(struct.unpack_from('<I', e, 0x50)[0]), 2)})
            if mbuf and 0 < msize < 64:
                mraw = self.live.reads([(mbuf, 0x14 * msize)])[0] or b''
                members = [struct.unpack_from('<I', mraw, k * 0x14)[0] for k in range(msize) if len(mraw) >= (k + 1) * 0x14]
        names = self._names([me, target, weapon, cstyle] + [t['ptr'] for t in targets] + members)
        for t in targets:
            t['actor'] = names(t.pop('ptr'))
        out['controller'] = {'self': names(me), 'target': names(target), 'weapon': names(weapon),
                             'combatStyle': names(cstyle), 'procedure': procedure,
                             'queuedProcedure': kind(queued, 'CombatProcedure'), 'action': act,
                             'goal': kind(goal, 'CombatGoal'),
                             'group': {'targets': targets, 'members': [names(m) for m in members if names(m)]}}
        out['uses'] = uses + ['CombatController +0x84 procedure, +0x88 queued, +0xA8 action, +0xAC goal, +0xBC self, '
                              '+0xC0 target, +0xF0 weapon, +0xF4 style (checked in game by RTTI)',
                              'ActorCombatGroup targets (+8, stride 0x68) and members (+0x18)']
        return out

    # ================================================================== fnv_perk_entries
    def perk_entries(self, action='start', entry_point=None, limit=50, value=None, actor='player', filters=None):
        self.live.in_game()
        labels = (PERK_PREFIX + 'apm', PERK_PREFIX + 'fn')
        if action == 'evaluate':
            return self._perk_evaluate(entry_point, value, actor, filters or [])
        if action == 'stop':
            removed = 0
            for h in self.agent.deep.hooks():
                if h['label'] in labels:
                    self._unhook(h)
                    removed += 1
            return {'stopped': removed}
        if action == 'start':
            filt = (SEL_ARG[0], int(entry_point)) if entry_point is not None else (0, 0)
            # Result pointer: argument 1 + filter count (ep, actor, filters..., result); dereference both candidates
            # and snapshot the first-filter-argument case so the value before is known for 1-, 2- and 3-filter points.
            h1 = self._hook(APPLY_PERK_MODIFIERS, labels[0], 6, deref=(SEL_ARG[3], SEL_ARG[4]), snap=(SEL_ARG[2], 4),
                            filt=filt)
            h2 = self._hook(APPLY_FUNCTION, labels[1], 6)
            self._perk_since = (self._seq(h1), self._seq(h2))
            return {'started': True, 'entryPoint': entry_point,
                    'note': 'Recording every perk entry-point check. Let the game run (fire, barter, talk...), then call '
                            'with action=read.'}
        if action != 'read':
            raise ValueError('action is start, read or stop')
        hooks = {h['label']: h for h in self.agent.deep.hooks() if h['label'] in labels}
        if len(hooks) < 2:
            raise LiveError('NOT_RECORDING', 'Start perk recording first (action=start)')
        since = getattr(self, '_perk_since', (0, 0))
        _, calls, _ = self.agent.deep.records(hooks[labels[0]], since[0], limit=4096)
        _, applies, _ = self.agent.deep.records(hooks[labels[1]], since[1], limit=4096)
        self._perk_since = (self._seq(hooks[labels[0]]), self._seq(hooks[labels[1]]))
        perk_of = self._perk_entry_map()
        out_calls = []
        applied_total = 0
        for r in calls:
            ep = r[12]
            if ep >= len(ENTRY_POINTS):
                continue
            name, filters = ENTRY_POINTS[ep]
            t0 = r[6] | (r[7] << 32)
            t1 = t0 + (r[10] | (r[11] << 32))
            mine = [x for x in applies if x[2] == r[2] and t0 <= (x[6] | (x[7] << 32)) <= t1]
            if not mine:
                continue
            applied_total += 1
            arg_words = list(r[12:18])
            before_bits = {1: r[24], 2: r[20], 3: r[21]}.get(filters)
            before = _f(before_bits) if ep not in NON_VALUE_EPS and before_bits is not None else None
            value = before
            entries = []
            for x in mine:
                data = x[17]
                info = perk_of.get(data) or {}
                fval = info.get('values', [None])[0]
                fn = info.get('function')
                if value is not None and fval is not None:
                    value = {1: lambda v: fval, 2: lambda v: v + fval, 3: lambda v: v * fval,
                             6: lambda v: abs(v), 7: lambda v: -abs(v)}.get(fn, lambda v: v)(value)
                entries.append({'perk': info.get('perk'), 'rank': info.get('rank'),
                                'function': PERK_FUNCTIONS.get(fn, fn), 'values': info.get('values')})
            out_calls.append({'entryPoint': {'id': ep, 'name': name}, 'actor': arg_words[1],
                              'filters': arg_words[2:1 + filters], 'valueBefore': _r(before, 4),
                              'valueAfter': _r(value, 4), 'appliedEntries': entries, 'thread': r[2]})
        pointers = set()
        for c in out_calls:
            pointers.add(c['actor'])
            pointers.update(c['filters'])
        names = self._names(list(pointers))
        for c in out_calls:
            c['actor'] = names(c['actor'])
            c['filters'] = [names(f) or ('0x%08X' % f) for f in c['filters']]
        return {'checks': len(calls), 'checksWithPerks': applied_total, 'calls': out_calls[-limit:],
                'note': 'Each call is one ApplyPerkModifiers check that had at least one matching perk entry: the value '
                        'before, the entries that applied (perk, function, value) and the value after, recomputed with '
                        'the entry functions (set/add/multiply).',
                'uses': ['ApplyPerkModifiers 0x005E58F0 (proven)', 'BGSEntryPointFunction::ApplyFunction 0x005E5B40 (decomp)',
                         'entry-point table (static initializer of ms_kEntryPointTable 0x01196EE0)',
                         'player perk ranks +0x87C, BGSPerk entries +0x48, BGSEntryPointPerkEntry layout (decomp)']}

    def _perk_evaluate(self, ep, value, actor, filters):
        """Run one value entry point now through the game's dispatcher and show which perks change the value."""
        if ep is None or not 0 <= int(ep) < len(ENTRY_POINTS):
            raise ValueError('entry_point 0..73 is required')
        ep = int(ep)
        if ep in NON_VALUE_EPS:
            raise ValueError('entry point %d (%s) is not a value entry point; it changes lists, so it is not run'
                             % (ep, ENTRY_POINTS[ep][0]))
        name, nfilters = ENTRY_POINTS[ep]
        if len(filters) != nfilters - 1:
            raise ValueError('%s takes %d filter form(s) after the owner, got %d' % (name, nfilters - 1, len(filters)))
        owner = self.live.resolve(actor)['ptr']
        fptrs = [self.live.resolve(f)['ptr'] for f in filters]
        buf = self.agent._buffer() + 0x800
        start = 0.0 if value is None else float(value)
        self.perk_entries('start')
        try:
            self.link.one('wr', word(buf), struct.pack('<f', start).hex())
            self._call(APPLY_PERK_MODIFIERS, 0, ep, owner, *fptrs, buf)
            result = self._floats([buf])[0]
            recorded = self.perk_entries('read', None, 20)
        finally:
            self.perk_entries('stop')
        mine = [c for c in recorded['calls'] if c['entryPoint']['id'] == ep]
        return {'entryPoint': {'id': ep, 'name': name}, 'valueBefore': start, 'valueAfter': _r(result, 4),
                'appliedEntries': mine[-1]['appliedEntries'] if mine else [],
                'note': "Run now with the game's own ApplyPerkModifiers: only the player (and teammates, through the "
                        'player) have perk entries. No entry applied means no perk of the owner changes this value.',
                'uses': recorded['uses']}

    def _perk_entry_map(self):
        """FunctionData pointer -> {perk, rank, function, values} for the player's perks (and teammate perks)."""
        player = self.live.resolve('player')['ptr']
        out = {}
        for base in (0x87C, 0xAD4):
            ranks = self.live.walk_list(player + base, 400)
            if not ranks:
                continue
            raws = self.live.reads([(r, 8) for r in ranks])
            perks = [(struct.unpack_from('<I', x, 0)[0], x[4]) for x in raws if x]
            names = self._names([p for p, _ in perks])
            for perk, rank in perks:
                for entry in self.live.walk_list(perk + 0x48, 64):
                    e = self.live.reads([(entry, 0x14)])[0]
                    if not e:
                        continue
                    data = struct.unpack_from('<I', e, 0x0C)[0]
                    if not data:
                        continue
                    vals = self.live.reads([(data, 12)])[0] or b''
                    two = e[9] in (4, 5)  # add range / add actor value mult carry two values (TwoValue data)
                    out[data] = {'perk': names(perk), 'rank': rank, 'function': e[9],
                                 'values': [_r(_f(struct.unpack_from('<I', vals, 4)[0]), 4)] +
                                           ([_r(_f(struct.unpack_from('<I', vals, 8)[0]), 4)] if two and len(vals) >= 12 else [])}
        return out

    # ================================================================== fnv_pathing
    def pathing(self, actor):
        self.live.in_game()
        a = self._actor(actor)
        p = a['ptr']
        mover = self.live.u32s([p + 0x190])[0]
        out = {'actor': a['formId']}
        uses = ['ActorMover layout (Actor+0x190; decomp ctor 0x009DAD00, SaveGame, Actor::DidPathingFail 0x008B3B90)',
                'DetailedActorPathHandler layout (decomp ctor 0x009DFF40, PrintCurrentStatus 0x009E8B50)',
                'PathingRequest / PathingLocation / PathingSolution layouts (decomp ctors)']
        if not mover:
            out['mover'] = None
            out['why'] = ['The actor has no mover (not loaded in high or middle process).']
            out['uses'] = uses
            return out
        m = self.live.reads([(mover, 0x88)])[0]
        req, sol, handler = struct.unpack_from('<III', m, 0x1C)
        mstate = struct.unpack_from('<i', m, 0x6C)[0]
        failed, waiting = bool(m[0x70]), bool(m[0x74])
        out['mover'] = {'state': MOVER_STATES.get(mstate, mstate), 'pathingFailed': failed, 'waitingForPath': waiting,
                        'lookAt': [_r(v, 1) for v in struct.unpack_from('<3f', m, 4)]}
        why = []
        if req:
            r = self.live.reads([(req, 0xB0)])[0]
            vt = struct.unpack_from('<I', r, 0)[0]
            fn = self.live.u32s([vt + 0x10])[0]

            def loc(off):
                x, y, z = struct.unpack_from('<3f', r, off + 4)
                navmesh, cands, cell, world, key = struct.unpack_from('<5I', r, off + 0x10)
                tri = struct.unpack_from('<H', r, off + 0x24)[0]
                return {'position': {'x': _r(x, 1), 'y': _r(y, 1), 'z': _r(z, 1)}, 'navmesh': navmesh,
                        'triangle': None if tri == 0xFFFF else tri, 'cell': cell, 'world': world,
                        'hasNavmeshCandidates': bool(cands)}
            origin, dest = loc(0x0C), loc(0x34)
            names = self._names([origin['cell'], dest['cell'], origin['world'], dest['world']])
            for lo in (origin, dest):
                lo['cell'], lo['world'] = names(lo['cell']), names(lo['world'])
                lo['navmesh'] = '0x%08X' % lo['navmesh'] if lo['navmesh'] else None
            out['request'] = {'class': (self.live.rtti([req]).get(req) or {}).get('class'), 'origin': origin,
                              'destination': dest, 'allowIncompletePath': bool(r[0x9B]), 'cantOpenDoors': bool(r[0x98]),
                              'canUseDoors': bool(r[0xA2]), 'ignoreLocks': bool(r[0xA4]),
                              'pathingRadius': _r(struct.unpack_from('<f', r, 0x8C)[0], 1)}
            for lo in (origin, dest):
                lo.pop('triangle')  # the request keeps unresolved copies; the path builder resolves its own
            for label, lo in (('start', origin), ('goal', dest)):
                if lo['navmesh'] is None and not lo['hasNavmeshCandidates']:
                    why.append('The path %s has no navmesh at all (its cell or worldspace has none there).' % label)
            if r[0x98]:
                why.append('The request cannot open doors.')
            _ = fn
        if sol:
            s = self.live.reads([(sol, 0x100)])[0]
            vcount, dcount = struct.unpack_from('<I', s, 0x10)[0], struct.unpack_from('<I', s, 0x28)[0]
            out['solution'] = {'virtualNodes': vcount, 'detailedNodes': dcount, 'valid': bool(s[0xFC])}
            # Nodes are {u32, PathingLocation}: the position sits at +8 (virtual 0x30 bytes, detailed 0x3C bytes).
            data_off, count, stride = (0x24, dcount, 0x3C) if dcount else (0x0C, vcount, 0x30)
            if count and count < 4096:
                base = struct.unpack_from('<I', s, data_off)[0]
                n = min(count, 60)
                nodes = self.live.reads([(base, stride * n)])[0] or b''
                out['solution']['points'] = [[_r(v, 1) for v in struct.unpack_from('<3f', nodes, k * stride + 8)]
                                             for k in range(n) if len(nodes) >= k * stride + 20]
        if handler:
            hcls = (self.live.rtti([handler]).get(handler) or {}).get('class')
            hr = self.live.reads([(handler, 0xE4)])[0]
            if hcls and 'Detailed' in hcls and hr:
                pstate, avoid = struct.unpack_from('<i', hr, 0xC8)[0], struct.unpack_from('<i', hr, 0xD0)[0]
                out['handler'] = {'class': hcls, 'state': PATH_STATES.get(pstate, pstate),
                                  'avoidance': AVOID_STATES.get(avoid, avoid), 'blocked': bool(hr[0xDE]),
                                  'rotating': bool(hr[0xDC]), 'pathPointIndex': struct.unpack_from('<I', hr, 0x64)[0]}
                if hr[0xDE]:
                    why.append('Movement is blocked (static avoidance).')
                if avoid == 5:
                    why.append('Actor avoidance failed: another actor blocks the path.')
                if pstate == 4:
                    why.append('Waiting for an updated path.')
            else:
                out['handler'] = {'class': hcls}
        if failed or mstate == 7:
            why.insert(0, 'The last path failed (mover state "path failed"). The engine does not store the reason code: '
                          'run fnv_trace on 0x009DC5E0 (ActorMover HandlePathResult) to catch it: 1 cannot move, 2 cannot '
                          'reach height, 3 cannot use door, 4 fell off path.')
        if not why:
            why.append('No pathing problem is visible.')
        out['why'] = why
        out['uses'] = uses
        return out

    # ================================================================== fnv_raycast
    def raycast(self, start=None, end=None, from_ref=None, to_ref=None, layer='line_of_sight'):
        self.live.in_game()
        layers = {'line_of_sight': 0x25, 'path_pick': 0x26, 'projectile': 6}
        if layer not in layers:
            raise ValueError('layer is line_of_sight, path_pick or projectile')
        if from_ref:
            fr = self.live.resolve(from_ref)
            v = self.live.values(['GetPos X', 'GetPos Y', 'GetPos Z'], fr['ptr'])
            start = {'x': v[0], 'y': v[1], 'z': v[2] + 100}
        if to_ref:
            tr = self.live.resolve(to_ref)
            v = self.live.values(['GetPos X', 'GetPos Y', 'GetPos Z'], tr['ptr'])
            end = {'x': v[0], 'y': v[1], 'z': v[2] + 60}
        if not start or not end:
            raise ValueError('give start and end positions {x, y, z} or from_ref and to_ref')
        out = {'start': start, 'end': end, 'layer': layer}
        base = self.agent._buffer() + 0x400
        pick = (base + 15) & ~15
        pts = pick + 0xC0
        tes = self.live.u32s([G_TES])[0]
        ops = [('wr', word(pick), '00' * 0xB0),
               ('wr', word(pts), struct.pack('<6f', start['x'], start['y'], start['z'], end['x'], end['y'], end['z']).hex()),
               ('call', word(PICK_CTOR), 'i', word(pick), '0'),
               ('call', word(PICK_FROM), 'i', word(pick), '0', word(pts)),
               ('call', word(PICK_TO), 'i', word(pick), '0', word(pts + 12)),
               ('call', word(PICK_FILTER), 'i', word(pick), word(layers[layer])),  # __fastcall (self, filter)
               ('call', word(TES_PICK), 'i', word(tes), '0', word(pick)),
               ('rd', word(pick), word(0xB0))]
        replies = self.link.batch(ops, timeout=20)
        hit3d = int(replies[6].get('eax', '0x0'), 16) if replies[6].get('ok') else 0
        raw = bytes.fromhex(replies[7]['hex']) if replies[7].get('ok') else b''
        throttled = bool(raw[0xAC]) if len(raw) > 0xAC else None
        collidable = struct.unpack_from('<I', raw, 0x80)[0] if len(raw) >= 0x84 else 0
        out['hit'] = bool(hit3d or collidable)
        out['throttled'] = throttled
        if out['hit']:
            fraction = struct.unpack_from('<f', raw, 0x40)[0]
            if 0 <= fraction <= 1:
                out['hitFraction'] = _r(fraction, 4)
                out['hitPoint'] = {k: _r(start[k] + (end[k] - start[k]) * fraction, 1) for k in 'xyz'}
                out['distanceToHit'] = _r(fraction * math.dist([start[k] for k in 'xyz'], [end[k] for k in 'xyz']), 1)
        if hit3d:
            ref = self._eax(FIND_REF_FOR_3D, 0, hit3d)
            name = self.link.one('rs', word(self.live.u32s([hit3d + 8])[0] or 0), '80')
            out['hitObject'] = {'node': name.get('text') if name.get('ok') else None,
                                'reference': (self._describe([ref]).get(ref) if ref else None)}
        if throttled:
            out['note'] = 'The physics world refused the ray this frame (per-frame pick budget); try again.'
        out['uses'] = ['TES::Pick 0x00458420 (bytes checked)', 'bhkPickData ctor/SetFrom/SetTo/SetFilter (proven)',
                       'bhkPickData output: hit fraction +0x40, root collidable +0x80 (Havok ray-cast output at +0x30)',
                       'TESObjectREFR::FindReferenceFor3D 0x0056F930 (proven)']
        return out

    def ground_height(self, x, y, z=0.0):
        self.live.in_game()
        buf = self.agent._buffer() + 0x600
        tes = self.live.u32s([G_TES])[0]
        self.link.one('wr', word(buf), struct.pack('<4f', x, y, z, -1e9).hex())
        ok = bool(self._eax(LAND_HEIGHT, tes, buf, buf + 12) & 0xFF)
        h = self._floats([buf + 12])[0]
        return {'x': x, 'y': y, 'terrainHeight': _r(h, 2) if ok else None, 'found': ok,
                'note': None if ok else 'No loaded terrain under this point (interiors and unloaded cells have none).',
                'uses': ['TES::GetLandHeightAt 0x004572E0 (probable)']}

    # ================================================================== fnv_cell_grid
    def cell_grid(self):
        self.live.in_game()
        tes = self.live.u32s([G_TES])[0]
        t = self.live.reads([(tes, 0xC4)])[0]
        grid_arr = struct.unpack_from('<I', t, 0x08)[0]
        cx, cy = struct.unpack_from('<ii', t, 0x24)
        interior, ibuf, ebuf = struct.unpack_from('<III', t, 0x34)
        world = struct.unpack_from('<I', t, 0x88)[0]
        grids, ibuf_n, ebuf_n = self.live.u32s([SETTING_GRIDS + 4, SETTING_INTERIOR_BUFFER + 4, SETTING_EXTERIOR_BUFFER + 4])
        cells = []
        if grid_arr:
            g = self.live.reads([(grid_arr, 0x14)])[0]
            dim, arr = struct.unpack_from('<I', g, 0x0C)[0], struct.unpack_from('<I', g, 0x10)[0]
            if arr and 0 < dim <= 15:
                slots = self.live.u32s([arr + 4 * i for i in range(dim * dim)])
                for i, c in enumerate(slots):
                    if c:
                        cells.append({'ptr': c, 'reason': 'loaded grid (within uGridsToLoad of the player)',
                                      'gridSlot': [i // dim, i % dim]})
        else:
            dim = None
        if interior:
            cells.append({'ptr': interior, 'reason': 'the player\'s current interior'})
        for base, n, reason in ((ibuf, ibuf_n, 'interior buffer (recently left interior)'),
                                (ebuf, ebuf_n, 'exterior buffer (recently loaded, kept for reuse)')):
            if base and n and n < 512:
                for k, c in enumerate(self.live.u32s([base + 4 * i for i in range(n)])):
                    if c and not any(x['ptr'] == c for x in cells):
                        cells.append({'ptr': c, 'reason': reason, 'bufferIndex': k})
        ptrs = [c['ptr'] for c in cells]
        states = self.live.reads([(p + 0x26, 1) for p in ptrs]) if ptrs else []
        names = self._names(ptrs + [world])
        coords = []
        for p in ptrs:
            xr = self._call(CELL_GRID_X, p)
            yr = self._call(CELL_GRID_Y, p)
            coords.append((int(xr['eax'], 16), int(yr['eax'], 16)))
        for c, st, (x, y) in zip(cells, states, coords):
            c['cell'] = names(c.pop('ptr'))
            state = st[0] if st else None
            c['state'] = CELL_STATES.get(state, state)
            if 'gridSlot' in c or 'exterior' in c['reason']:
                c['cellXY'] = [struct.unpack('<i', struct.pack('<I', x))[0], struct.unpack('<i', struct.pack('<I', y))[0]]
        return {'worldspace': names(world), 'centerCell': [cx, cy], 'interior': bool(interior),
                'settings': {'uGridsToLoad': grids, 'uInteriorCellBuffer': ibuf_n, 'uExteriorCellBuffer': ebuf_n},
                'gridDimension': dim, 'cells': cells,
                'uses': ['TES layout (g_TES 0x011DEA10: grid +0x08, center +0x24, interior +0x34, buffers +0x38/+0x3C)',
                         'GridCellArray (+0x0C dimension, +0x10 cells; GetCell 0x004BA490 proven)',
                         'cell load state byte +0x26 (TESObjectCELL::GetState 0x00450FD0 proven; 3 loaded, 5 attaching, 6 attached)',
                         'TESObjectCELL::GetCellGridX 0x00544C30 / GetDataY 0x00544C60']}
