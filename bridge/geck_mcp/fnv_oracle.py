"""Oracle matching (NAMING-PLAN.md component E, proof rule 4.2): does a function compute a value the game already
knows, on the same objects?

A candidate's results come either from probe samples (passive, the objects the game passed it) or from calling
it (active, on objects we choose), which is allowed only when its code is proven free of side effects by
`purity`: no write to memory other than its own stack frame, no indirect call or jump out of the function, no
string instructions, every direct callee pure too (3 levels).

Oracles are values with a known meaning, read the same moment from the same objects:
  script  the game's own script commands on the reference (GetLevel, GetScale, GetPos X, GetAV Health, ...)
  header  TESForm fields every form has (typeID +4, flags +8, refID +0xC: xNVSE GameForms.h)
A candidate matches an oracle when it equals it on every object (ints exactly, floats to 1e-4 relative), over at
least 20 objects of at least 2 classes or with at least 10 distinct values, with no mismatch, and no shuffled
pairing of the same values matches (rule 4.2). `calibrate` runs the method blind on proven getters.
"""
import json
import random
import re
import struct
import time
from collections import Counter, defaultdict

from .fnv_conv import ConvError
from .fnv_facts import ACTOR_VALUES
from .fnv_link import word
from .fnv_naming import ancestors, common_ancestor, hx, is_a, return_width

MIN_OBJECTS = 20
MIN_CLASSES = 2
MIN_DISTINCT = 10
MAX_CHANCE = 1e-6  # probability that a random pairing of the same values matches this well
SHUFFLES = 200
ACTOR_CLASSES = ('Actor',)

# Script-command oracles: (name, expression, kind, applies to). Kind: int, float, bool or form (a form id).
REF_ORACLES = [
    ('GetScale', 'GetScale', 'float', 'TESObjectREFR'),
    ('GetPos X', 'GetPos X', 'float', 'TESObjectREFR'),
    ('GetPos Y', 'GetPos Y', 'float', 'TESObjectREFR'),
    ('GetPos Z', 'GetPos Z', 'float', 'TESObjectREFR'),
    ('GetAngle X', 'GetAngle X', 'float', 'TESObjectREFR'),
    ('GetAngle Y', 'GetAngle Y', 'float', 'TESObjectREFR'),
    ('GetAngle Z', 'GetAngle Z', 'float', 'TESObjectREFR'),
    ('GetDisabled', 'GetDisabled', 'bool', 'TESObjectREFR'),
    ('GetDestroyed', 'GetDestroyed', 'bool', 'TESObjectREFR'),
    ('GetLocked', 'GetLocked', 'bool', 'TESObjectREFR'),
    ('GetLockLevel', 'GetLockLevel', 'int', 'TESObjectREFR'),
    ('GetOpenState', 'GetOpenState', 'int', 'TESObjectREFR'),
    ('GetBaseObject', 'GetBaseObject', 'form', 'TESObjectREFR'),
    ('GetParentCell', 'GetParentCell', 'form', 'TESObjectREFR'),
    ('GetDistance player', 'GetDistance player', 'float', 'TESObjectREFR'),
    ('GetLevel', 'GetLevel', 'int', 'Actor'),
    ('GetDead', 'GetDead', 'bool', 'Actor'),
    ('GetIsCreature', 'GetIsCreature', 'bool', 'Actor'),
    ('GetSitting', 'GetSitting', 'int', 'Actor'),
    ('GetSleeping', 'GetSleeping', 'int', 'Actor'),
    ('GetKnockedState', 'GetKnockedState', 'int', 'Actor'),
    ('IsInCombat', 'IsInCombat', 'bool', 'Actor'),
    ('GetUnconscious', 'GetUnconscious', 'bool', 'Actor'),
    ('GetRestrained', 'GetRestrained', 'bool', 'Actor'),
    ('GetPlayerTeammate', 'GetPlayerTeammate', 'bool', 'Actor'),
    ('IsMoving', 'IsMoving', 'int', 'Actor'),
    ('IsRunning', 'IsRunning', 'bool', 'Actor'),
    ('IsSneaking', 'IsSneaking', 'bool', 'Actor'),
    ('IsSwimming', 'IsSwimming', 'bool', 'Actor'),
    ('IsWeaponOut', 'IsWeaponOut', 'bool', 'Actor'),
    ('GetCurrentAIProcedure', 'GetCurrentAIProcedure', 'int', 'Actor'),
    ('GetCurrentAIPackage', 'GetCurrentAIPackage', 'form', 'Actor'),
    ('GetCombatTarget', 'GetCombatTarget', 'form', 'Actor'),
    ('GetHealthPercentage', 'GetHealthPercentage', 'float', 'Actor'),
    ('GetIsSex Female', 'GetIsSex Female', 'bool', 'Actor'),
] + [('GetAV %s' % av, 'GetAV %s' % av, 'float', 'Actor') for av in ACTOR_VALUES] + \
    [('GetBaseAV %s' % av, 'GetBaseAV %s' % av, 'float', 'Actor') for av in ACTOR_VALUES]
# Base-form oracles: xNVSE/JIP commands that read the base form of the reference they run on. Objects are the
# base forms; each is evaluated through one placed reference of it (`evalPtr`).
BASE_ORACLES = [
    ('GetRace', 'GetRace', 'form', 'TESNPC'),
    ('GetHair', 'GetHair', 'form', 'TESNPC'),
    ('GetHairLength', 'GetHairLength', 'float', 'TESNPC'),
    ('GetEyes', 'GetEyes', 'form', 'TESNPC'),
    ('GetNPCWeight', 'GetNPCWeight', 'float', 'TESNPC'),
    ('GetNPCHeight', 'GetNPCHeight', 'float', 'TESNPC'),
    ('GetCreatureType', 'GetCreatureType', 'int', 'TESCreature'),
    ('GetCreatureReach', 'GetCreatureReach', 'int', 'TESCreature'),
    ('GetActorBaseFlagsLow', 'GetActorBaseFlagsLow', 'int', 'TESActorBase'),
    ('GetActorBaseFlagsHigh', 'GetActorBaseFlagsHigh', 'int', 'TESActorBase'),
    ('GetBaseNumFactions', 'GetBaseNumFactions', 'int', 'TESActorBase'),
    ('GetIsSex Female (base)', 'GetIsSex Female', 'bool', 'TESActorBase'),
    ('GetScript', 'GetScript', 'form', 'TESForm'),
    ('GetWeight', 'GetWeight', 'float', 'TESForm'),
]
# Form oracles: commands that take the form itself by EditorID (vanilla and JIP LN), for forms that are not
# references: quests, cells, worldspaces, packages, weapons. {e} is the form id (the script compiler does not take EditorIDs).
FORM_ORACLES = [
    ('GetStage', 'GetStage {e}', 'int', 'TESQuest'),
    ('GetQuestCompleted', 'GetQuestCompleted {e}', 'bool', 'TESQuest'),
    ('GetQuestRunning', 'GetQuestRunning {e}', 'bool', 'TESQuest'),
] + [('GetQuestFlag %d' % b, 'GetQuestFlag {e} %d' % b, 'bool', 'TESQuest') for b in range(8)] + [
    ('GetCellWaterHeight', 'GetCellWaterHeight {e}', 'float', 'TESObjectCELL'),
    ('GetCellLightingTemplate', 'GetCellLightingTemplate {e}', 'form', 'TESObjectCELL'),
    ('GetCellClimate', 'GetCellClimate {e}', 'form', 'TESObjectCELL'),
    ('GetCellWaterForm', 'GetCellWaterForm {e}', 'form', 'TESObjectCELL'),
    ('GetCellImageSpace', 'GetCellImageSpace {e}', 'form', 'TESObjectCELL'),
] + [('GetCellFlag %d' % b, 'GetCellFlag {e} %d' % b, 'bool', 'TESObjectCELL') for b in range(8)] + [
    ('GetWorldspaceClimate', 'GetWorldspaceClimate {e}', 'form', 'TESWorldSpace'),
    ('GetWorldspaceParentWorldspace', 'GetWorldspaceParentWorldspace {e}', 'form', 'TESWorldSpace'),
    ('GetWorldspacePersistentCell', 'GetWorldspacePersistentCell {e}', 'form', 'TESWorldSpace'),
    ('GetWorldspaceWaterType', 'GetWorldspaceWaterType {e}', 'form', 'TESWorldSpace'),
    ('GetWorldspaceImagespace', 'GetWorldspaceImagespace {e}', 'form', 'TESWorldSpace'),
] + [('GetWorldspaceFlag %d' % b, 'GetWorldspaceFlag {e} %d' % b, 'bool', 'TESWorldSpace') for b in range(8)] + \
    [('GetPackageFlag %d' % b, 'GetPackageFlag {e} %d' % b, 'bool', 'TESPackage') for b in range(32)] + [
    # xNVSE weapon commands with the base form as their argument (xNVSE Commands_Inventory.cpp)
    ('GetWeaponIsAutomatic', 'GetWeaponIsAutomatic {e}', 'bool', 'TESObjectWEAP'),
    ('GetWeaponHasScope', 'GetWeaponHasScope {e}', 'bool', 'TESObjectWEAP'),
    ('GetWeaponAmmoUse', 'GetWeaponAmmoUse {e}', 'int', 'TESObjectWEAP'),
    ('GetWeaponClipRounds', 'GetWeaponClipRounds {e}', 'int', 'TESObjectWEAP'),
    ('GetWeaponNumProjectiles', 'GetWeaponNumProjectiles {e}', 'int', 'TESObjectWEAP'),
    ('GetWeaponType', 'GetWeaponType {e}', 'int', 'TESObjectWEAP'),
    ('GetWeaponHandGrip', 'GetWeaponHandGrip {e}', 'int', 'TESObjectWEAP'),
    ('GetWeaponSkill', 'GetWeaponSkill {e}', 'int', 'TESObjectWEAP'),
    ('GetWeaponMinRange', 'GetWeaponMinRange {e}', 'float', 'TESObjectWEAP'),
    ('GetWeaponMaxRange', 'GetWeaponMaxRange {e}', 'float', 'TESObjectWEAP'),
    ('GetWeaponFireRate', 'GetWeaponFireRate {e}', 'float', 'TESObjectWEAP'),
    ('GetWeaponActionPoints', 'GetWeaponActionPoints {e}', 'float', 'TESObjectWEAP'),
    ('GetWeaponSpread', 'GetWeaponSpread {e}', 'float', 'TESObjectWEAP'),
    ('GetWeaponMinSpread', 'GetWeaponMinSpread {e}', 'float', 'TESObjectWEAP'),
    ('GetWeaponSightFOV', 'GetWeaponSightFOV {e}', 'float', 'TESObjectWEAP'),
    ('GetWeaponFlags1', 'GetWeaponFlags1 {e}', 'int', 'TESObjectWEAP'),
    ('GetWeaponFlags2', 'GetWeaponFlags2 {e}', 'int', 'TESObjectWEAP'),
    ('GetWeight (form)', 'GetWeight {e}', 'float', 'TESObjectWEAP'),
    ('GetValue (form)', 'GetValue {e}', 'int', 'TESObjectWEAP'),
    ('GetHealth (form)', 'GetHealth {e}', 'int', 'TESObjectWEAP'),
]
# JIP LN trait and flag commands that read one field of a base form (refs/JIP-LN-NVSE/functions_jip/jip_fn_*.h):
# (command, class, trait ids, flag word traits). Each numeric trait is offered as an int and as a float oracle,
# since the match only compares like with like; a flag command is one bool oracle per bit.
JIP_TRAITS = [
    ('GetImpactDataTrait', 'BGSImpactData', range(18), (16,)),
    ('GetWeatherTraitNumeric', 'TESWeather', range(21), (13,)),
    ('GetClimateTraitNumeric', 'TESClimate', range(7), (5,)),
    ('GetGrassTraitNumeric', 'TESGrass', range(10), (9,)),
    ('GetLandTextureTraitNumeric', 'TESLandTexture', range(4), ()),
    ('GetSoundTraitNumeric', 'TESSound', range(13), ()),
    ('GetAmmoTraitNumeric', 'TESAmmo', range(4), ()),
]
JIP_FLAGS = [('GetProjectileFlag', 'BGSProjectile', 16), ('GetLightFlag', 'TESObjectLIGH', 32),
             ('GetSoundFlag', 'TESSound', 32)]
for _cmd, _cls, _ids, _bits in JIP_TRAITS:
    for _t in _ids:
        FORM_ORACLES.append(('%s %d' % (_cmd, _t), '%s {e} %d' % (_cmd, _t), 'int', _cls))
        FORM_ORACLES.append(('%s %d (float)' % (_cmd, _t), '%s {e} %d' % (_cmd, _t), 'float', _cls))
for _cmd, _cls, _n in JIP_FLAGS:
    for _b in range(_n):
        FORM_ORACLES.append(('%s %d' % (_cmd, _b), '%s {e} %d' % (_cmd, _b), 'bool', _cls))
FORM_ORACLES.append(('GetMessageFlags', 'GetMessageFlags {e}', 'int', 'BGSMessage'))
FORM_TYPES = {'QUST': 'TESQuest', 'CELL': 'TESObjectCELL', 'WRLD': 'TESWorldSpace', 'PACK': 'TESPackage',
              'WEAP': 'TESObjectWEAP', 'IPCT': 'BGSImpactData', 'WTHR': 'TESWeather', 'CLMT': 'TESClimate',
              'GRAS': 'TESGrass', 'LTEX': 'TESLandTexture', 'SOUN': 'TESSound', 'AMMO': 'TESAmmo',
              'PROJ': 'BGSProjectile', 'LIGH': 'TESObjectLIGH', 'MESG': 'BGSMessage'}
# int oracles that are bit fields: each bit set on some object becomes its own bool oracle "<name> & 0xN"
BIT_ORACLES = ('GetWeaponFlags1', 'GetWeaponFlags2', 'GetMessageFlags') + tuple(
    '%s %d' % (_cmd, _t) for _cmd, _cls, _ids, _bits in JIP_TRAITS for _t in _bits)
HEADER_ORACLES = [('TESForm.typeID', 4, 'byte'), ('TESForm.flags', 8, 'dword'), ('TESForm.refID', 12, 'dword')]
FLAG_NAMES = {  # TESForm/TESObjectREFR flag bits named by community headers (cited); other bits stay unnamed
    0x8: 'Initialized (xNVSE GameForms.h:320 kFormFlags_Initialized)',
    0x20: 'Deleted (xNVSE GameForms.h:321 kFormFlags_Deleted; GameObjects.h:107)',
    0x400: 'Persistent / QuestItem (xNVSE GameObjects.h:108 kFlags_Persistent, GameForms.h:322 kFormFlags_QuestItem)',
    0x800: 'Disabled (community TESObjectREFR kFlags_Disabled = 0x800)',
    0x4000: 'Temporary (xNVSE GameForms.h:323 kFormFlags_Temporary)',
    0x40000: 'Compressed (xNVSE GameForms.h:324 kFormFlags_Compressed)',
}
ORACLE_SOURCES = {
    'script': "the game's own script command on the same reference, read the same moment",
    'header': 'TESForm header field (xNVSE GameForms.h: typeID +4, flags +8, refID +0xC)',
    'base': 'xNVSE/JIP command reading the base form, run on a placed reference of that base',
}


# ===================================================================================== purity
WRITES_X87 = re.compile(r'^(fst|fstp|fist|fistp|fisttp|fbstp|fnstsw|fnstcw|fstsw|fstcw|fnsave|fsave|fnstenv|fstenv)$')
NEVER = ('int', 'int3', 'into', 'syscall', 'sysenter', 'in', 'out', 'ins', 'outs', 'cli', 'sti', 'hlt', 'lock',
         'cmpxchg', 'cmpxchg8b', 'xadd', 'rdtsc', 'cpuid', 'wait', 'leave_never')
STRING_WRITE = ('stos', 'movs', 'rep', 'repne')


class Purity:
    """Is a function free of side effects? Decided from its machine code in the clean exe."""

    def __init__(self, engine, image):
        import capstone
        self.engine, self.image = engine, image
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        self.md.detail = True
        self.cache = {}

    def check(self, address, depth=3, max_size=400):
        key = (address, depth)
        if key in self.cache:
            return self.cache[key]
        self.cache[key] = {'pure': False, 'why': 'recursion'}  # a cycle is not proven pure
        r = self._check(address, depth, max_size)
        self.cache[key] = r
        return r

    def _check(self, address, depth, max_size):
        from capstone import x86
        f = self.engine.functions.get(address)
        if not f:
            return {'pure': False, 'why': 'not a known function'}
        size = int(f.get('size') or 0)
        if not size or size > max_size:
            return {'pure': False, 'why': 'size %d (limit %d)' % (size, max_size)}
        code = self.image.read(address, size)
        if not code:
            return {'pure': False, 'why': 'outside the exe image'}
        frame = False
        pops, calls, reads = set(), [], set()
        end = address + size
        insns = list(self.md.disasm(code, address))
        if not insns or insns[-1].address + insns[-1].size != end:
            # trailing padding is fine; an undecodable body is not
            if not insns or end - (insns[-1].address + insns[-1].size) > 16:
                return {'pure': False, 'why': 'body does not decode'}
        if len(insns) >= 2 and insns[0].mnemonic == 'push' and insns[0].op_str == 'ebp' and \
                insns[1].mnemonic == 'mov' and insns[1].op_str == 'ebp, esp':
            frame = True
        stack_regs = ('esp', 'ebp') if frame else ('esp',)
        for i in insns:
            m = i.mnemonic
            if m in NEVER or m.startswith(STRING_WRITE) or (i.prefix and i.prefix[0] == 0xF0):
                return {'pure': False, 'why': '%s at %s' % (m, hx(i.address))}
            if m in ('ret', 'retn'):
                pops.add(int(i.op_str, 0) if i.op_str else 0)
                continue
            if m == 'call' or (m == 'jmp' and i.operands and i.operands[0].type == x86.X86_OP_IMM):
                op = i.operands[0]
                if op.type != x86.X86_OP_IMM:
                    return {'pure': False, 'why': 'indirect call at %s' % hx(i.address)}
                target = op.imm & 0xFFFFFFFF
                if m == 'jmp' and address <= target < end:
                    continue
                if depth <= 0:
                    return {'pure': False, 'why': 'callee %s beyond the depth limit' % hx(target)}
                sub = self.check(target, depth - 1, max_size)
                if not sub['pure']:
                    return {'pure': False, 'why': 'callee %s: %s' % (hx(target), sub['why'])}
                calls.append(hx(target))
                continue
            if m == 'jmp':  # indirect: only a switch table inside the function
                op = i.operands[0]
                if op.type == x86.X86_OP_MEM and op.mem.index and op.mem.scale == 4:
                    continue
                return {'pure': False, 'why': 'indirect jump at %s' % hx(i.address)}
            for k, op in enumerate(i.operands):
                if op.type != x86.X86_OP_MEM:
                    continue
                base = i.reg_name(op.mem.base) if op.mem.base else None
                writes = bool(op.access & 2) or (WRITES_X87.match(m) is not None) or \
                    (m == 'pop') or (m.startswith('set') and k == 0) or (m == 'xchg')
                if writes and op.mem.segment and i.reg_name(op.mem.segment) == 'fs':
                    continue  # the SEH frame link (mov fs:[0], ...), restored before the function returns
                if writes and base not in stack_regs:
                    return {'pure': False, 'why': 'writes %s at %s' % (i.op_str, hx(i.address))}
                if not writes and base == 'ecx' and not op.mem.index:
                    reads.add(op.mem.disp)
        if len(pops) > 1:
            return {'pure': False, 'why': 'returns popping different byte counts %s' % sorted(pops)}
        return {'pure': True, 'pops': next(iter(pops)) if pops else 0, 'calls': calls,
                'thisReads': sorted(x for x in reads if x >= 0)}


# ===================================================================================== the tool
class OracleTools:
    def __init__(self, live, naming, agent):
        self.live, self.naming, self.agent = live, naming, agent
        self._purity = None

    @property
    def link(self):
        return self.live.link

    def purity(self):
        engine = self.live.engine()
        if self._purity is None or self._purity.engine is not engine:
            self._purity = Purity(engine, self.live.deep._image(required=True))
        return self._purity

    # ------------------------------------------------------------------ objects
    def forms(self, per_type=150, seed=11):
        """Quests, cells, worldspaces, packages and weapons from FalloutNV.esm with EditorIDs, as live objects."""
        import sqlite3
        path = self.live.project.state_dir / 'fnv' / 'records.sqlite'
        missing = ConvError('RECORDS_INDEX_MISSING', 'The record index has no FalloutNV.esm records yet: run '
                            'fnv_record action=find once (any query) to build it, then call again.')
        if not path.is_file():
            raise missing
        db = sqlite3.connect(str(path), timeout=30)
        try:
            try:
                rows = db.execute("SELECT r.type, r.local, r.edid FROM records r JOIN files f ON f.id = r.file WHERE "
                              "f.name = 'FalloutNV.esm' AND r.type IN (%s) AND" % ','.join("'%s'" % t for t in FORM_TYPES) + " r.edid IS NOT NULL "
                              "AND r.edid != '' AND (r.flags & 0x20) = 0").fetchall()
            except sqlite3.OperationalError:     # an empty or older cache: no records table yet
                raise missing from None
        finally:
            db.close()
        if not rows:
            raise missing
        rnd = random.Random(seed)
        by_type = defaultdict(list)
        for t, local, edid in rows:
            if re.fullmatch(r'[A-Za-z0-9_]{1,120}', edid):
                by_type[t].append(('%08X' % local, edid))
        out = {}
        for t, items in by_type.items():
            for fid, edid in rnd.sample(items, min(per_type, len(items))):
                try:
                    ptr = self.live.resolve(fid)['ptr']
                except Exception:  # noqa: BLE001 - a form not loaded
                    continue
                out[ptr] = {'formId': fid, 'edid': edid, 'form': True, 'type': t}
        classes = self.live.rtti(list(out))
        engine = self.live.engine()
        for p, v in out.items():
            v['class'] = (classes.get(p) or {}).get('class')
        return {p: v for p, v in out.items() if v['class'] and is_a(engine, v['class'], FORM_TYPES[v['type']])}

    def objects(self, radius=8000.0, limit=400, bases=True, forms=True):
        """Live references around the player (all of them, actors marked) as {ptr: {formId, class, actor}}."""
        self.live.in_game()
        out = {}
        for ft, actor in ((200, True), (0, False)):
            near = self.live.refs_near(radius=radius, form_type=ft, limit=limit)
            for r in near['refs']:
                if r['formId'] in {v['formId'] for v in out.values()}:
                    continue
                try:
                    ptr = self.live.resolve(r['formId'])['ptr']
                except Exception:  # noqa: BLE001 - a ref that unloaded between the two reads
                    continue
                out[ptr] = {'formId': r['formId'], 'actor': actor, 'name': r.get('name')}
        player = self.live.resolve('player')
        out[player['ptr']] = {'formId': '00000014', 'actor': True, 'name': 'player'}
        classes = self.live.rtti(list(out))
        for p, v in out.items():
            v['class'] = (classes.get(p) or {}).get('class')
        if bases:
            from .fnv_link import value_of
            refs = list(out)
            values = [value_of(r) for r in _batch(self.link, [('eval', _hexs('GetBaseObject'), word(p)) for p in refs])]
            base_of = {}
            for p, v in zip(refs, values):
                fid = v.get('formId') if isinstance(v, dict) else v
                if fid and fid not in base_of:
                    base_of[fid] = p
            for fid, ref_ptr in base_of.items():
                try:
                    bp = self.live.resolve(fid)['ptr']
                except Exception:  # noqa: BLE001 - a base form that cannot be looked up
                    continue
                if bp not in out:
                    out[bp] = {'formId': str(fid).upper().zfill(8), 'actor': False, 'base': True, 'evalPtr': ref_ptr}
            bclasses = self.live.rtti([p for p, v in out.items() if v.get('base')])
            for p, v in out.items():
                if v.get('base'):
                    v['class'] = (bclasses.get(p) or {}).get('class')
        if forms:
            out.update({p: v for p, v in self.forms().items() if p not in out})
        return out

    # ------------------------------------------------------------------ oracle values
    def oracle_values(self, objects, families=('script', 'header')):
        """{oracle: {ptr: value}} for every applicable oracle, read in one batch per object."""
        engine = self.live.engine()
        out = defaultdict(dict)
        if 'script' in families:
            ops, keys = [], []
            for ptr, o in objects.items():
                if o.get('edid') or (o.get('form') and o.get('formId')):
                    for name, expr, kind, applies in FORM_ORACLES:
                        if o.get('class') and is_a(engine, o['class'], applies):
                            ops.append(('eval', _hexs(expr.format(e=o['formId'])), word(0)))
                            keys.append((name, kind, ptr))
                    continue
                table, target = (BASE_ORACLES, o['evalPtr']) if o.get('base') else (REF_ORACLES, ptr)
                for name, expr, kind, applies in table:
                    if o.get('class') and is_a(engine, o['class'], applies):
                        ops.append(('eval', _hexs(expr), word(target)))
                        keys.append((name, kind, ptr))
            from .fnv_link import value_of
            results = _batch(self.link, ops) if ops else []
            for (name, kind, ptr), r in zip(keys, results):
                if not r.get('ok'):
                    continue
                v = value_of(r)
                if isinstance(v, dict):
                    v = v.get('formId')
                if v is None:
                    continue
                if kind == 'form':
                    v = str(v).upper().zfill(8)
                elif kind == 'float':
                    v = float(v)
                else:
                    v = int(round(float(v)))
                out[name][ptr] = v
            for name in BIT_ORACLES:
                vals = out.get(name) or {}
                seen = 0
                for v in vals.values():
                    seen |= v
                for b in range(32):
                    if seen & (1 << b):
                        out['%s & 0x%X' % (name, 1 << b)] = {p: 1 if v & (1 << b) else 0 for p, v in vals.items()}
        if 'header' in families:
            ptrs = [p for p, o in objects.items() if o.get('class') and is_a(engine, o['class'], 'TESForm')]
            words = self.live.u32s([p + 4 for p in ptrs] + [p + 8 for p in ptrs] + [p + 12 for p in ptrs]) if ptrs else []
            n = len(ptrs)
            bits = 0
            for k, p in enumerate(ptrs):
                out['TESForm.typeID'][p] = words[k] & 0xFF
                out['TESForm.flags'][p] = words[n + k]
                out['TESForm.refID'][p] = words[2 * n + k]
                out['TESForm.refID & 0xFFFFFF'][p] = words[2 * n + k] & 0xFFFFFF
                bits |= words[n + k]
            for b in range(32):
                if bits & (1 << b):
                    name = 'TESForm.flags & 0x%X' % (1 << b)
                    for k, p in enumerate(ptrs):
                        out[name][p] = 1 if words[n + k] & (1 << b) else 0
        return dict(out)

    # ------------------------------------------------------------------ candidate values
    def call_values(self, address, objects, kind=None):
        """Call a pure thiscall getter on each object: {ptr: {'int':..., 'float':...}}."""
        engine = self.live.engine()
        f = engine.functions[address]
        p = self.purity().check(address)
        if not p['pure']:
            raise ValueError('%s is not provably free of side effects (%s): use the passive mode' %
                             (f['name'], p['why']))
        if p['pops']:
            raise ValueError('%s pops %d argument bytes: only zero-argument getters are called' % (f['name'], p['pops']))
        sig = f.get('signature') or ''
        bits = return_width(sig)
        floaty = kind == 'float' or re.match(r'^(public|private|protected)?:?\s*(virtual\s+)?(float|double)\b', sig.strip())
        code = 'f' if floaty else 'i'
        ops = [('call', word(address), code, word(ptr), word(0)) for ptr in objects]
        out = {}
        for ptr, r in zip(objects, _batch(self.link, ops)):
            if not r.get('ok'):
                continue
            eax = int(r.get('eax', '0'), 16) if isinstance(r.get('eax'), str) else int(r.get('eax') or 0)
            out[ptr] = {'int': eax & ((1 << bits) - 1), 'raw': eax, 'float': r.get('st0') if floaty else None}
        return out

    def sampled_values(self, address, harvest=None):
        """{this ptr: {'int', 'float'}} from harvested probe samples that captured the result."""
        rt = self.naming.runtime(address, harvest)
        bits = return_width(self.live.engine().functions[address].get('signature'))
        out = {}
        for s in rt['samples']:
            if s[8] in (2, 3) and s[0]:
                out[s[0]] = {'int': (s[9] or 0) & ((1 << bits) - 1), 'raw': s[9], 'float': s[10] if s[8] == 3 else None}
        return out

    # ------------------------------------------------------------------ matching
    def match(self, values, oracles, objects, address=None):
        """Every oracle the candidate equals on all shared objects, with the 4.2 criteria and the shuffled control."""
        engine = self.live.engine()
        results = []
        # a float result is in st0 (eax then holds whatever the function left there) and an int, bool or pointer
        # result is in eax: compare each only with oracles of its own kind
        floaty = any(v.get('float') is not None for v in values.values())
        formids = {}  # per call: two parallel matches must never read each other's pointers
        if not floaty:  # result pointers -> form ids, read once (not once per object per form oracle)
            ptrs = sorted({v['raw'] for v in values.values() if v.get('raw') and 0x10000 <= v['raw'] < 0x7FFF0000
                           and not v['raw'] & 3})
            for k in range(0, len(ptrs), 500):
                chunk = ptrs[k:k + 500]
                try:
                    ids = self.live.u32s([q + 12 for q in chunk])
                except Exception:  # noqa: BLE001 - one unreadable pointer: read them singly
                    ids = []
                    for q in chunk:
                        try:
                            ids.append(self.live.u32s([q + 12])[0])
                        except Exception:  # noqa: BLE001
                            ids.append(None)
                for q, fid in zip(chunk, ids):
                    formids[q] = fid
        for name, ov in oracles.items():
            shared = [p for p in values if p in ov]
            if len(shared) < 3:
                continue
            kind = _oracle_kind(name)
            if (kind == 'float') != floaty:
                continue
            pairs = [(_candidate(values[p], kind, formids, engine), ov[p]) for p in shared]
            same = [_equal(a, b, kind) for a, b in pairs]
            mismatches = same.count(False)
            if mismatches:
                continue
            classes = {objects.get(p, {}).get('class') for p in shared}
            distinct = len({_bucket(b, kind) for _, b in pairs})
            p_chance = chance(pairs, kind)
            enough = len(shared) >= MIN_OBJECTS and p_chance <= MAX_CHANCE
            rnd = random.Random(hash((name, address)) & 0xFFFFFFFF)
            truth = [b for _, b in pairs]
            cand = [a for a, _ in pairs]
            control = 0
            # shuffles only where they can still decide: a match already failing the object count or the chance
            # limit (an always-0 result, say) stays a non-match whatever they show
            for _ in range(SHUFFLES if enough else 0):
                perm = truth[:]
                rnd.shuffle(perm)
                # identical shuffles count too: a match a random pairing gives is a coincidence (one '1' among
                # 35 objects is matched by 1 shuffle in 35)
                if all(_equal(a, b, kind) for a, b in zip(cand, perm)):
                    control += 1
            meaning = None
            if name.startswith('TESForm.flags & '):
                meaning = FLAG_NAMES.get(int(name.split('& ')[1], 16), 'no community name for this bit')
            src = 'header' if name.startswith('TESForm.') else 'base' if any(name == b[0] for b in BASE_ORACLES) else 'form' if any(name == f[0] for f in FORM_ORACLES) else 'script'
            results.append({'oracle': name, 'source': src,
                            'meaning': meaning,
                            'objects': len(shared), 'classes': sorted(c for c in classes if c), 'distinct': distinct,
                            'mismatches': 0, 'chance': p_chance, 'shuffledMatches': control, 'shuffles': SHUFFLES,
                            'meetsRule42': enough and control == 0,
                            'why': None if enough and control == 0 else
                            ('only %d objects' % len(shared) if len(shared) < MIN_OBJECTS else
                             'one value only' if distinct <= 1 else
                             'a random pairing matches with probability %.2g (limit %g)' % (p_chance, MAX_CHANCE)
                             if p_chance > MAX_CHANCE else 'shuffled control matched %d times' % control)})
        results.sort(key=lambda r: (not r['meetsRule42'], -r['distinct'], -r['objects']))
        return results

    # ------------------------------------------------------------------ one function
    def run(self, ref, mode='auto', harvest=None, radius=8000.0, objects=None, families=('script', 'header'),
            oracles=None):
        engine = self.live.engine()
        address = engine.resolve(ref)
        f = engine.functions[address]
        if objects is None:
            unpause = self.live.game.keep_unpaused(600)
            try:
                return self.run(ref, mode, harvest, radius, self.objects(radius), families)
            finally:
                self.live.game.stop_task(unpause)
        objs = objects
        p = self.purity().check(address)
        used = mode
        if mode == 'auto':
            used = 'call' if p['pure'] and not p['pops'] and '__thiscall' in (f.get('signature') or '') else 'sampled'
        if used == 'call':
            cls = self._this_class(address, harvest)
            chosen = {ptr: o for ptr, o in objs.items() if o.get('class') and (not cls or is_a(engine, o['class'], cls))}
            values = self.call_values(address, chosen)
            objs = chosen
        else:
            values = self.sampled_values(address, harvest)
            missing = [ptr for ptr in values if ptr not in objs]
            if missing:
                classes = self.live.rtti(missing)
                for ptr in missing:
                    objs[ptr] = {'class': (classes.get(ptr) or {}).get('class'), 'formId': None}
        if oracles is None:
            oracles = self.oracle_values({p_: objs[p_] for p_ in values if p_ in objs}, families)
        matches = self.match(values, oracles, objs, address)
        out = {'function': engine.summary(address), 'mode': used, 'purity': p, 'objects': len(values),
               'classes': dict(Counter(objs[p_].get('class') for p_ in values if p_ in objs).most_common(8)),
               'distinctResults': len({json.dumps(v, sort_keys=True) for v in values.values()}),
               'matches': matches[:12], 'oraclesTried': len(oracles),
               'method': {'rule': 'PROOF 4.2 oracle match', 'minObjects': MIN_OBJECTS,
                          'maxChance': MAX_CHANCE, 'shuffles': SHUFFLES, 'sources': ORACLE_SOURCES}}
        self._store(address, out)
        out['_values'] = values
        out['_objects'] = objs
        return out

    def _this_class(self, address, harvest):
        engine = self.live.engine()
        rt = self.naming.runtime(address, harvest)
        observed = [s[2] for s in rt['samples'] if s[2]]
        if observed:
            return common_ancestor(engine, observed)
        name = engine.functions[address]['name']
        if '::' in name:
            cls = name.split('::')[0]
            if cls in (engine.rtti or {}):
                return cls
        slots = self.naming.index().slots().get(address)
        if slots:
            return common_ancestor(engine, [s['class'] for s in slots])
        return None

    def _store(self, address, out):
        db = self.naming.db()
        try:
            db.execute('CREATE TABLE IF NOT EXISTS oracle_results (at REAL, address INTEGER, mode TEXT, objects INTEGER, '
                       'oracle TEXT, meets INTEGER, result TEXT, calibration INTEGER)')
            best = out['matches'][0] if out['matches'] else None
            db.execute('INSERT INTO oracle_results VALUES (?,?,?,?,?,?,?,?)',
                       (time.time(), address, out['mode'], out['objects'], best['oracle'] if best else None,
                        1 if best and best['meetsRule42'] else 0, json.dumps(out, default=str), 0))
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------------ blind calibration
    def sweep(self, limit=3000, radius=20000.0, statuses=('unnamed', 'decomp', 'class_proven', 'probable', 'rejected'),
              harvest=None):
        """The oracle over every not-proven zero-argument thiscall getter whose code is pure and whose `this` class is
        known (from probe samples or vtable slots) and a form class: results stored for the dossier and the export."""
        engine = self.live.engine()
        pur = self.purity()
        cands = []
        for a in engine.addresses:
            f = engine.functions[a]
            if f['status'] not in statuses or '__thiscall' not in (f.get('signature') or ''):
                continue
            p = pur.check(a)
            if not p['pure'] or p['pops']:
                continue
            cls = self._this_class(a, harvest)
            if cls and cls in (engine.rtti or {}) and is_a(engine, cls, 'TESForm'):
                cands.append(a)
        unpause = self.live.game.keep_unpaused(3600)
        found = []
        try:
            objs = self.objects(radius)
            oracles = self.oracle_values(objs)
            for a in cands[:limit]:
                try:
                    r = self.run(hx(a), mode='call', objects=dict(objs), oracles=oracles, harvest=harvest)
                except Exception as e:  # noqa: BLE001 - one function failing must not stop the sweep
                    found.append({'address': hx(a), 'error': str(e)[:160]})
                    continue
                best = [m for m in r['matches'] if m['meetsRule42']]
                if best:
                    found.append({'address': hx(a), 'name': engine.functions[a]['name'],
                                  'status': engine.functions[a]['status'], 'matches': [m['oracle'] for m in best[:4]],
                                  'objects': best[0]['objects'], 'distinct': best[0]['distinct']})
        finally:
            self.live.game.stop_task(unpause)
        return {'candidates': len(cands), 'run': min(len(cands), limit),
                'matched': [f for f in found if 'matches' in f], 'errors': sum(1 for f in found if 'error' in f)}

    def calibration_set(self, limit=200):
        """Proven zero-argument thiscall getters on reference/actor classes whose code is pure: the candidates
        for a blind run. Returned without names (the answer key is kept apart)."""
        engine = self.live.engine()
        pur = self.purity()
        out = []
        for a in engine.addresses:
            f = engine.functions[a]
            if f['status'] != 'proven' or '__thiscall' not in (f.get('signature') or '') or '(void)' not in (f.get('signature') or ''):
                continue
            cls = f['name'].split('::')[0] if '::' in f['name'] else None
            if not cls or cls not in (engine.rtti or {}) or not is_a(engine, cls, 'TESObjectREFR') and \
                    not is_a(engine, cls, 'TESForm'):
                continue
            leaf = f['name'].split('::')[-1]
            if not re.match(r'(Get|Is|Has|Can)[A-Z]', leaf):
                continue
            p = pur.check(a)
            if p['pure'] and not p['pops']:
                out.append(a)
        rnd = random.Random(20261108)
        rnd.shuffle(out)
        return out[:limit]

    def calibrate(self, limit=200, radius=8000.0, key=None):
        """Blind run: the matcher sees addresses only; then each verdict is scored against the answer key (the
        expected oracle for each proven name, `ANSWER_KEY`), written before any run."""
        engine = self.live.engine()
        unpause = self.live.game.keep_unpaused(3600)  # the pause menu stops the main loop serving calls
        try:
            objs = self.objects(radius)
            oracles = self.oracle_values(objs)  # once: the objects stand still (restrained) for the whole run
            cases = self.calibration_set(limit)
            rows = self._blind_rows(cases, objs, oracles)
        finally:
            self.live.game.stop_task(unpause)
        return self._finish(rows, engine, key)

    def passive_set(self):
        """Proven zero-argument thiscall getters on form classes that call mode cannot run (their code is not
        provably free of side effects): the blind set for the passive mode, scored from probe samples."""
        engine = self.live.engine()
        pur = self.purity()
        out = []
        for a in engine.addresses:
            f = engine.functions[a]
            sig = f.get('signature') or ''
            if f['status'] != 'proven' or '__thiscall' not in sig or '(void)' not in sig:
                continue
            cls = f['name'].split('::')[0] if '::' in f['name'] else None
            if not cls or cls not in (engine.rtti or {}) or not is_a(engine, cls, 'TESForm'):
                continue
            if not re.match(r'(Get|Is|Has|Can)[A-Z]', f['name'].split('::')[-1]):
                continue
            p = pur.check(a)
            if not (p['pure'] and not p['pops']):
                out.append(a)
        return out

    def calibrate_passive(self, harvest, key=None, min_objects=MIN_OBJECTS):
        """Blind run in the passive mode: each getter's results are the ones the game itself got while probed
        (harvest `harvest`, this game session, so the `this` pointers are still the same objects). The oracles are
        read once, now, from the union of the sampled objects; the answer key is the same as for `calibrate`."""
        engine = self.live.engine()
        cases, per = [], {}
        for a in self.passive_set():
            v = self.sampled_values(a, harvest)
            if len(v) >= min_objects:
                cases.append(a)
                per[a] = v
        ptrs = sorted({p for v in per.values() for p in v})
        unpause = self.live.game.keep_unpaused(3600)
        try:
            classes = self.live.rtti(ptrs) if ptrs else {}
            objs = {}
            for p in ptrs:
                c = (classes.get(p) or {}).get('class')
                if c and is_a(engine, c, 'TESForm'):
                    objs[p] = {'class': c}
            ids = self.live.u32s([p + 12 for p in objs]) if objs else []
            form_classes = set(FORM_TYPES.values())
            for (p, o), fid in zip(objs.items(), ids):
                o['formId'] = '%08X' % fid
                o['form'] = any(is_a(engine, o['class'], c) for c in form_classes)
            oracles = self.oracle_values(objs)
            rows = self._blind_rows(cases, objs, oracles, mode='sampled', harvest=harvest)
        finally:
            self.live.game.stop_task(unpause)
        return self._finish(rows, engine, key, prefix='oracle_passive', method='oracle (passive: probe samples, '
                                                                                'harvest %s)' % harvest,
                            extra={'passiveSet': len(self.passive_set()), 'withEnoughObjects': len(cases)})

    def _blind_rows(self, cases, objs, oracles=None, mode='call', harvest=None):
        rows = []
        for a in cases:
            try:
                r = self.run(hx(a), mode=mode, objects=dict(objs), oracles=oracles, harvest=harvest)
            except Exception as e:  # noqa: BLE001 - one getter failing must not stop the run
                rows.append({'address': hx(a), 'error': str(e)[:200]})
                continue
            best = next((m for m in r['matches'] if m['meetsRule42']), None)
            # control: the same results paired with the wrong objects (a derangement of the object order)
            ptrs = list(r['_values'])
            control = None
            if len(ptrs) >= 2:
                rnd = random.Random(a)
                order = ptrs[:]
                while True:
                    rnd.shuffle(order)
                    if all(x != y for x, y in zip(ptrs, order)):
                        break
                shuffled = {q: r['_values'][p_] for p_, q in zip(ptrs, order)}
                cm = next((m for m in self.match(shuffled, oracles or {}, r['_objects'], a) if m['meetsRule42']), None)
                control = cm['oracle'] if cm else None
            rows.append({'address': hx(a), 'claim': best['oracle'] if best else None, 'objects': r['objects'],
                         'distinct': r['distinctResults'], 'otherMatches': [m['oracle'] for m in r['matches'][1:4]
                                                                            if m['meetsRule42']],
                         'controlClaim': control})
        return rows

    def _finish(self, rows, engine, key, prefix='oracle_calibration', method=None, extra=None):
        # scoring happens after: names come in only now
        scored = score(rows, engine, key or ANSWER_KEY)
        if method:
            scored['method'] = method
        scored.update(extra or {})
        path = self.live.project.state_dir / ('%s_%s.json' % (prefix, time.strftime('%Y%m%d-%H%M%S')))
        path.write_text(json.dumps(scored, indent=1, default=str), encoding='utf-8')
        scored['file'] = str(path)
        db = self.naming.db()
        try:
            db.execute('CREATE TABLE IF NOT EXISTS calibrations (at REAL, method TEXT, cases INTEGER, claims INTEGER, '
                       'correct INTEGER, wrong INTEGER, precision REAL, detail TEXT)')
            db.execute('INSERT INTO calibrations VALUES (?,?,?,?,?,?,?,?)',
                       (time.time(), 'oracle', scored['cases'], scored['claims'], scored['correct'], scored['wrong'],
                        scored['precision'], json.dumps(scored, default=str)))
            db.commit()
        finally:
            db.close()
        return scored


# ===================================================================================== answer key and scoring
# What each proven getter name means, as oracle names: written from the names alone, before any run. A claim is
# correct when it is one of the listed oracles (aliases: several oracles can carry the same value).
ANSWER_KEY = [
    (r'::GetLevel$', ['GetLevel']),
    (r'::(IsDead|GetDead)$', ['GetDead']),
    (r'::(IsDisabled|GetDisabled)$', ['GetDisabled', 'TESForm.flags & 0x800']),
    (r'::(IsCreature|GetIsCreature)$', ['GetIsCreature']),
    (r'::GetScale$', ['GetScale']),
    (r'::(GetBaseForm|GetBaseObject|GetObjectReference)$', ['GetBaseObject']),
    (r'::(GetParentCell|GetCell)$', ['GetParentCell']),
    (r'::(GetFormType|GetTypeID)$', ['TESForm.typeID']),
    (r'::(GetFormID|GetRefID|GetId)$', ['TESForm.refID']),
    (r'::(GetFlags|GetFormFlags)$', ['TESForm.flags']),
    (r'::(IsInCombat|GetIsInCombat)$', ['IsInCombat']),
    (r'::(IsSneaking|GetIsSneaking)$', ['IsSneaking']),
    (r'::(IsSwimming)$', ['IsSwimming']),
    (r'::(IsRunning)$', ['IsRunning']),
    (r'::(IsWeaponOut|GetWeaponOut)$', ['IsWeaponOut']),
    (r'::(IsUnconscious|GetUnconscious)$', ['GetUnconscious']),
    (r'::(IsRestrained|GetRestrained)$', ['GetRestrained']),
    (r'::(IsPlayerTeammate|GetPlayerTeammate)$', ['GetPlayerTeammate']),
    (r'::(GetSitSleepState|GetSitting)$', ['GetSitting', 'GetSleeping']),
    (r'::(GetKnockedState)$', ['GetKnockedState']),
    (r'::(IsDestroyed|GetDestroyed)$', ['GetDestroyed']),
    (r'::(IsLocked|GetLocked)$', ['GetLocked']),
    (r'::(GetPosX|GetPositionX)$', ['GetPos X']), (r'::(GetPosY|GetPositionY)$', ['GetPos Y']),
    (r'::(GetPosZ|GetPositionZ)$', ['GetPos Z']),
    (r'::(GetAngleX|GetRotationX)$', ['GetAngle X']), (r'::(GetAngleY|GetRotationY)$', ['GetAngle Y']),
    (r'::(GetAngleZ|GetRotationZ|GetHeading)$', ['GetAngle Z']),
    (r'::(IsPersistent|GetRefPersists|GetPersistent|IsQuestItem|GetQuestObject)$', ['TESForm.flags & 0x400']),
    (r'::(GetDelete|GetDeleted|IsDeleted)$', ['TESForm.flags & 0x20']),
    (r'::(GetTemporary|IsTemporary)$', ['TESForm.flags & 0x4000']),
    (r'::(GetInitialized|IsInitialized)$', ['TESForm.flags & 0x8']),
    (r'::(GetFormIDWithoutIndex)$', ['TESForm.refID & 0xFFFFFF']),
    (r'^TESNPC::GetRace$', ['GetRace']), (r'^TESNPC::GetHair$', ['GetHair']),
    (r'^TESNPC::GetHairLength$', ['GetHairLength']), (r'^TESNPC::GetEyes$', ['GetEyes']),
    (r'^TESNPC::GetWeight$', ['GetNPCWeight']), (r'^TESNPC::GetHeight$', ['GetNPCHeight']),
    (r'^TESCreature::GetCreatureType$', ['GetCreatureType']), (r'^TESCreature::GetReach$', ['GetCreatureReach']),
    (r'^TESActorBase::(GetSex|IsFemale)$', ['GetIsSex Female (base)']),
    (r'::GetScript$', ['GetScript']),
    (r'^(TESObjectARMO|TESObjectWEAP|TESObjectMISC|TESAmmo|AlchemyItem|TESObjectBOOK)::GetWeight$',
     ['GetWeight', 'GetWeight (form)']),
    # added 2026-11-08 with the weapon form oracles, from the names, before the run that uses them
    (r'^TESObjectWEAP::(IsAutomaticWeapon|IsAutomatic|GetIsAutomatic)$', ['GetWeaponIsAutomatic']),
    (r'^TESObjectWEAP::(HasScope|GetHasScope)$', ['GetWeaponHasScope']),
    (r'^TESObjectWEAP::(GetAmmoPerShot|GetAmmoUse)$', ['GetWeaponAmmoUse']),
    (r'^TESObjectWEAP::(GetClipRounds|GetClipSize)$', ['GetWeaponClipRounds']),
    (r'^TESObjectWEAP::(GetNumProjectiles)$', ['GetWeaponNumProjectiles']),
    (r'^TESObjectWEAP::(GetWeaponType|GetType)$', ['GetWeaponType']),
    (r'^TESQuest::(GetCurrentStage|GetStage)$', ['GetStage']),
    (r'^TESQuest::(GetCompleted|IsCompleted)$', ['GetQuestCompleted']),
    (r'^TESQuest::(IsRunning|GetRunning)$', ['GetQuestRunning']),
    (r'^TESObjectCELL::GetWaterHeight$', ['GetCellWaterHeight']),
    (r'^TESObjectCELL::GetLightingTemplate$', ['GetCellLightingTemplate']),
    (r'^TESObjectCELL::GetClimate$', ['GetCellClimate']), (r'^TESWorldSpace::GetClimate$', ['GetWorldspaceClimate']),
    (r'^TESWorldSpace::(GetParentWorldspace|GetParentWorld)$', ['GetWorldspaceParentWorldspace']),
    (r'^TESWorldSpace::GetPersistentCell$', ['GetWorldspacePersistentCell']),
]


def expected_oracles(name, key):
    for pattern, oracles in key:
        if re.search(pattern, name, re.IGNORECASE):
            return oracles
    return None


def score(rows, engine, key):
    """Correct: the claim is an oracle the name means. Wrong: a claim the name does not mean (a false name). No
    claim: abstained (coverage, not precision). Names with no key entry count only as abstentions or as claims
    to judge by hand (`unkeyed`)."""
    correct = wrong = 0
    detail, unkeyed = [], []
    for r in rows:
        if 'error' in r:
            detail.append(dict(r, verdict='error'))
            continue
        name = engine.functions[int(r['address'], 16)]['name']
        expected = expected_oracles(name, key)
        if not r['claim']:
            verdict = 'abstained'
        elif expected is None:
            verdict = 'unkeyed'
            unkeyed.append({'address': r['address'], 'name': name, 'claim': r['claim']})
        elif r['claim'] in expected or any(o in expected for o in r.get('otherMatches') or []):
            verdict = 'correct'
            correct += 1
        else:
            verdict = 'wrong'
            wrong += 1
        detail.append(dict(r, name=name, expected=expected, verdict=verdict))
    claims = correct + wrong
    return {'method': 'oracle (call mode, script + header oracles)', 'cases': len(rows), 'claims': claims,
            'correct': correct, 'wrong': wrong, 'precision': round(correct / claims, 4) if claims else None,
            'abstained': sum(1 for d in detail if d['verdict'] == 'abstained'), 'unkeyed': unkeyed,
            'errors': sum(1 for d in detail if d['verdict'] == 'error'),
            'shuffledControlClaims': sum(1 for d in detail if d.get('controlClaim')),
            'shuffledControlExamples': [{'address': d['address'], 'oracle': d['controlClaim']} for d in detail
                                        if d.get('controlClaim')][:10],
            'passesGate41': claims >= 50 and claims and correct / claims >= 0.98 and
            not any(d.get('controlClaim') for d in detail),
            'detail': detail}


# ===================================================================================== helpers
def _bucket(v, kind):
    if kind == 'float':
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def chance(pairs, kind):
    """Exact probability that a uniformly random permutation of the oracle values equals the candidate's values
    everywhere, given they match: the permutations that keep the match are those inside groups of equal values,
    prod(count_v!) / n!. One '1' among 35 gives 1/35; always the same value gives 1."""
    import math
    counts = Counter(_bucket(b, kind) for _a, b in pairs)
    n = len(pairs)
    log = sum(math.lgamma(c + 1) for c in counts.values()) - math.lgamma(n + 1)
    return math.exp(log)


def _batch(link, ops, size=250):
    """link.batch in chunks: one request per chunk keeps each under the companion's reply timeout."""
    out = []
    for k in range(0, len(ops), size):
        out.extend(link.batch(ops[k:k + size]))
    return out


def _hexs(text):
    return text.encode('utf-8').hex().upper()


def _oracle_kind(name):
    for n, _e, kind, _a in REF_ORACLES + BASE_ORACLES + FORM_ORACLES:
        if n == name:
            return kind
    if name.startswith('TESForm.flags & ') or any(name.startswith(b + ' & ') for b in BIT_ORACLES):
        return 'bool'
    return 'int'


def _candidate(v, kind, formids, engine):
    if kind == 'float':
        return v.get('float') if v.get('float') is not None else struct.unpack('<f', struct.pack('<I', v['raw'] & 0xFFFFFFFF))[0]
    if kind == 'form':
        ptr = v['raw']
        if not ptr:
            return '00000000'
        fid = (formids or {}).get(ptr)
        return '%08X' % fid if fid is not None else None
    if kind == 'bool':
        # a bool result is 0 or 1: anything else (a pointer's low byte, say) is not a bool and matches nothing
        return v['int'] if v['int'] in (0, 1) else None
    i = v['int']
    return i - (1 << 32) if i >= 1 << 31 else i


def _equal(a, b, kind):
    if a is None or b is None:
        return False
    if kind == 'float':
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            return False
        return abs(a - b) <= 1e-4 * max(1.0, abs(b))
    if kind == 'form':
        return str(a).upper().zfill(8) == str(b).upper().zfill(8)
    return int(a) == int(b)
