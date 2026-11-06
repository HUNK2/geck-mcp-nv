"""Engine facts the live fnv_* tools rely on: addresses, offsets and names, each with its evidence.

The engine companion hard-codes no addresses; the bridge sends them. Entries cite the decompilation
(file and the proven function that uses them) and, where they agree, xNVSE or JIP LN source.
Run fnv_engine_facts to see them with their proof status from the engine map.
"""

FACTS = {
    # Console output capture: PrintToConsole (0x00703C00, proven) prints only when the console is active
    # or the "verbose messages" byte is set (toggled by the vanilla Verbose command, Cmd_Verbose_Execute
    # 0x005B6F10 -> FUN_005b6f60/70, decomp tesscriptfunctions.cpp). xNVSE's IsConsoleMode reads the
    # console-visible byte that the main loop sets from IsConsoleVisible (decomp main.cpp, 0x011DEA2E).
    'verbose_print_flag': {'address': 0x011F158C, 'size': 1,
                           'evidence': 'decomp tesscriptfunctions.cpp FUN_005b6f60/FUN_005b6f70, read by PrintToConsole 0x00703C00'},
    'console_visible_flag': {'address': 0x011DEA2E, 'size': 1,
                             'evidence': 'decomp main.cpp (set from IsConsoleVisible each frame); xNVSE GameAPI.cpp IsConsoleMode'},
    # Object layouts (decomp headers, offsets checked by the decomp build; JIP LN GameObjects.h agrees).
    'TESForm.typeID': {'offset': 0x04, 'evidence': 'decomp include/TESForm.h m_ucFormType (checked)'},
    'TESForm.flags': {'offset': 0x08, 'evidence': 'decomp include/TESForm.h m_uiFormFlags (checked)'},
    'TESForm.refID': {'offset': 0x0C, 'evidence': 'decomp include/TESForm.h iFormID (checked)'},
    'TESObjectREFR.extraList': {'offset': 0x44, 'evidence': 'decomp TESObjectREFR m_kExtraDataCollection (checked)'},
    'BaseExtraList.head': {'offset': 0x04, 'evidence': 'decomp include/BaseExtraList.h pHead (checked)'},
    'BSExtraData.type': {'offset': 0x04, 'evidence': 'decomp include/BSExtraData.h m_ucExtraDataType (checked)'},
    'BSExtraData.next': {'offset': 0x08, 'evidence': 'decomp include/BSExtraData.h pNext (checked)'},
    'MobileObject.baseProcess': {'offset': 0x68, 'evidence': 'decomp include/MobileObject.h m_pBaseProcess (checked)'},
    'BaseProcess.processType': {'offset': 0x28, 'evidence': 'decomp include/BaseProcess.h m_eProcessType (checked)'},
    'Actor.magicTarget': {'offset': 0x94, 'evidence': 'Actor bases MobileObject 0x88 + MagicCaster 0xC (decomp sizes, checked); JIP GameObjects.h magicTarget // 094'},
    'MagicTarget.GetEffectList': {'vtableSlot': 2, 'evidence': 'JIP LN GameObjects.h MagicTarget /*008*/ GetEffectList'},
    'TESQuest.script': {'offset': 0x18 + 0x04, 'evidence': 'decomp TESQuest tESScriptableFormComponent at 0x18; TESScriptableForm script pointer at +4'},
    'TESQuest.stages': {'offset': 0x44, 'evidence': 'decomp include/TESQuest.h m_kStageList (checked)'},
    'TESQuest.objectives': {'offset': 0x4C, 'evidence': 'decomp include/TESQuest.h m_kObjectives (checked)'},
    'TESQuest.locals': {'offset': 0x5C, 'evidence': 'decomp include/TESQuest.h m_pScriptLocals (checked)'},
    'Script.variables': {'offset': 0x4C, 'evidence': 'decomp Script m_kVariableList (after m_kReferencedObjectList 0x44)'},
    'ScriptLocals.variables': {'offset': 0x0C, 'evidence': 'decomp ScriptLocals m_pVariableList (checked)'},
}

# FNV script names of the 77 actor values in code order (decomp src/fallout_shared/actorvalue.cpp,
# RegisterActorValue calls; "SleepDeprevation" is the game's own spelling).
ACTOR_VALUES = (
    'Aggression Confidence Energy Responsibility Mood Strength Perception Endurance Charisma Intelligence Agility '
    'Luck ActionPoints CarryWeight CritChance HealRate Health MeleeDamage DamageResist PoisonResist RadResist '
    'SpeedMult Fatigue Karma XP PerceptionCondition EnduranceCondition LeftAttackCondition RightAttackCondition '
    'LeftMobilityCondition RightMobilityCondition BrainCondition Barter BigGuns EnergyWeapons Explosives Lockpick '
    'Medicine MeleeWeapons Repair Science Guns Sneak Speech Survival Unarmed InventoryWeight Paralysis Invisibility '
    'Chameleon NightEye Turbo FireResist WaterBreathing RadiationRads BloodyMess UnarmedDamage Assistance '
    'ElectricResist FrostResist EnergyResist EmpResist Variable01 Variable02 Variable03 Variable04 Variable05 '
    'Variable06 Variable07 Variable08 Variable09 Variable10 IgnoreCrippledLimbs Dehydration Hunger '
    'SleepDeprevation DamageThreshold').split()
SPECIAL = ACTOR_VALUES[5:12]
SKILLS = ACTOR_VALUES[32:45]

# Extra data type ids (xNVSE GameExtraData.h ExtraDataType).
EXTRA_TYPES = dict((int(k, 16), v) for k, v in (item.split(':') for item in (
    '0x01:Havok 0x02:Cell3D 0x03:CellWaterType 0x04:RegionList 0x05:SeenData 0x07:CellMusicType 0x08:CellClimate '
    '0x09:ProcessMiddleLow 0x0A:CellCanopyShadowMask 0x0B:DetachTime 0x0C:PersistentCell 0x0D:Script 0x0E:Action '
    '0x0F:StartingPosition 0x10:Anim 0x12:UsedMarkers 0x13:DistantData 0x14:RagdollData 0x15:ContainerChanges '
    '0x16:Worn 0x17:WornLeft 0x18:PackageStartLocation 0x19:Package 0x1A:TrespassPackage 0x1B:RunOncePacks '
    '0x1C:ReferencePointer 0x1D:Follower 0x1E:LevCreaModifier 0x1F:Ghost 0x20:OriginalReference 0x21:Ownership '
    '0x22:Global 0x23:Rank 0x24:Count 0x25:Health 0x26:Uses 0x27:TimeLeft 0x28:Charge 0x29:Light 0x2A:Lock '
    '0x2B:Teleport 0x2C:MapMarker 0x2E:LeveledCreature 0x2F:LeveledItem 0x30:Scale 0x31:Seed '
    '0x32:NonActorMagicCaster 0x33:NonActorMagicTarget 0x35:PlayerCrimeList 0x37:EnableStateParent '
    '0x38:EnableStateChildren 0x39:ItemDropper 0x3A:DroppedItemList 0x3B:RandomTeleportMarker '
    '0x3C:MerchantContainer 0x3D:SavedHavokData 0x3E:CannotWear 0x3F:Poison 0x40:Unk040 0x41:LastFinishedSequence '
    '0x42:SavedAnimation 0x43:NorthRotation 0x44:XTarget 0x45:FriendHits 0x46:HeadingTarget 0x48:RefractionProperty '
    '0x49:StartingWorldOrCell 0x4A:Hotkey 0x4C:EditorRefMovedData 0x4D:InfoGeneralTopic 0x4E:HasNoRumors 0x4F:Sound '
    '0x50:TerminalState 0x51:LinkedRef 0x52:LinkedRefChildren 0x53:ActivateRef 0x54:ActivateRefChildren '
    '0x55:TalkingActor 0x56:ObjectHealth 0x57:DecalRefs 0x59:CellImageSpace 0x5A:NavMeshPortal 0x5B:ModelSwap '
    '0x5C:Radius 0x5D:Radiation 0x5E:FactionChanges 0x5F:DismemberedLimbs 0x61:MultiBound 0x62:MultiBoundData '
    '0x63:MultiBoundRef 0x65:ReflectedRefs 0x66:ReflectorRefs 0x67:EmittanceSource 0x68:RadioData 0x69:CombatStyle '
    '0x6B:Primitive 0x6C:OpenCloseActivateRef 0x6D:AnimNoteReciever 0x6E:Ammo 0x6F:PatrolRefData 0x70:PackageData '
    '0x71:OcclusionPlane 0x72:CollisionData 0x73:SayTopicInfoOnceADay 0x74:EncounterZone 0x75:SayToTopicInfo '
    '0x76:OcclusionPlaneRefData 0x77:PortalRefData 0x78:Portal 0x79:Room 0x7A:HealthPerc 0x7B:RoomRefData '
    '0x7C:GuardedRefData 0x7D:CreatureAwakeSound 0x7E:WaterZoneMap 0x80:IgnoredBySandbox 0x81:CellAcousticSpace '
    '0x82:ReservedMarkers 0x83:WeaponIdleSound 0x84:WaterLightRefs 0x85:LitWaterRefs 0x86:WeaponAttackSound '
    '0x87:ActivateLoopSound 0x88:PatrolRefInUseData 0x89:AshPileRef 0x8A:CreatureMovementSound '
    '0x8B:FollowerSwimBreadcrumbs 0x8D:WeaponModFlags 0x92:SpecialRenderFlags').split()))

# How to decode the first field (at +0x0C) of common extra data (xNVSE GameExtraData.h layouts).
EXTRA_FIELDS = {
    0x0D: ('script', 'ptr'), 0x1C: ('reference', 'form'), 0x20: ('original', 'form'), 0x21: ('owner', 'form'),
    0x22: ('global', 'form'), 0x23: ('rank', 'i32'), 0x24: ('count', 'u16'), 0x25: ('health', 'f32'),
    0x26: ('uses', 'u32'), 0x27: ('timeLeft', 'f32'), 0x28: ('charge', 'f32'), 0x2A: ('lockData', 'ptr'),
    0x2B: ('teleportData', 'ptr'), 0x2E: ('leveledBase', 'form'), 0x2F: ('leveledItem', 'form'), 0x30: ('scale', 'f32'),
    0x37: ('enableParent', 'form'), 0x3F: ('poison', 'form'), 0x51: ('linkedRef', 'form'), 0x53: ('activateRef', 'ptr'),
    0x56: ('health', 'f32'), 0x5C: ('radius', 'f32'), 0x5D: ('radiation', 'f32'), 0x6E: ('ammo', 'form'),
    0x74: ('encounterZone', 'form'), 0x7A: ('healthPercent', 'f32'),
}

# Form flags (TESForm m_uiFormFlags) that matter for tests; xNVSE GameForms.h kFormFlags_*.
FORM_FLAGS = {0x20: 'deleted', 0x40: 'castShadows', 0x400: 'persistent', 0x800: 'initiallyDisabled',
              0x1000: 'ignored', 0x4000: 'temporary', 0x10000: 'visibleWhenDistant', 0x20000: 'destroyed'}

# Game control codes (JIP LN GameOSDepend.h ControlCode) used by fnv_input with xNVSE GetControl.
CONTROLS = {'forward': 0, 'backward': 1, 'left': 2, 'right': 3, 'attack': 4, 'activate': 5, 'aim': 6,
            'ready': 7, 'sneak': 8, 'run': 9, 'always_run': 10, 'auto_move': 11, 'jump': 12, 'pov': 13,
            'pipboy': 14, 'wait': 15, 'vats': 16, 'hotkey1': 17, 'ammo_swap': 18, 'quicksave': 25,
            'quickload': 26, 'grab': 27}

# Script block types: g_scriptBlocks (decomp tesscriptrunmanager.cpp), a ScriptFunctionInfo[38] table at
# 0x0118E2F0 (0x28 bytes each, name pointer first; the console command table follows at 0x0118E8E0). Names read
# from the exe. ScriptRunner::EvaluateLine reads the block id as the short at the BEGIN statement's data offset.
SCRIPT_BLOCKS_ADDRESS = 0x0118E2F0
SCRIPT_BLOCKS = (
    'GameMode', 'MenuMode', 'OnActivate', 'OnAdd', 'OnEquip', 'OnUnequip', 'OnDrop', 'SayToDone', 'OnHit',
    'OnHitWith', 'OnDeath', 'OnMurder', 'OnCombatEnd', 'Unused', 'Unused2', 'OnPackageStart', 'OnPackageDone',
    'ScriptEffectStart', 'ScriptEffectFinish', 'ScriptEffectUpdate', 'OnPackageChange', 'OnLoad', 'OnMagicEffectHit',
    'OnSell', 'OnTrigger', 'OnStartCombat', 'OnTriggerEnter', 'OnTriggerLeave', 'OnActorEquip', 'OnActorUnequip',
    'OnReset', 'OnOpen', 'OnClose', 'OnGrab', 'OnRelease', 'OnDestructionStageChange', 'OnFire', 'OnNPCActivate')
