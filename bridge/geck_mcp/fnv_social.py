"""Dialogue, AI packages and live-versus-plugin form tools (the last P1 tools of the fnv-mcp plan).

  fnv_dialogue_tree    every response an NPC could give for a topic, each with its conditions evaluated
  fnv_dialogue_choose  start a conversation and pick responses, for testing dialogue end to end (act)
  fnv_ai_packages      every package on an actor's list with schedule and conditions: which runs and why
  fnv_form_live_diff   a form in memory now versus its winning plugin record

How conditions are evaluated. A condition item (TESConditionItem, 0x1C bytes) names a script function by its
index in the game's command table, two parameters, a comparison and a value or global. The engine evaluates it in
TESConditionItem::ContainsTrait (0x00681600): it picks the reference the function runs on (subject, target, a
reference, the combat target or the linked reference), calls the command's eval handler from the game's own
command table (CommandInfo.eval at +0x20, cdecl (ref, param1, param2, double &result)), and compares the result.
These tools do the same per item, calling the handler the running game has in its table, so every condition shows
its own value. The final answer for a dialogue response is the engine's own TESTopicInfo::CheckCondition
(0x0061E600, proven), which also applies say-once, quest conditions and the dummy-node rules.
"""
import struct
import time

from . import fnv_facts
from .fnv_facts import FACTS
from .fnv_link import hexs, word
from .fnv_live import LiveError, _fid

CHECK_CONDITION = 0x0061E600        # TESTopicInfo::CheckCondition(bool &, TESQuest *, ref speaker, ref listener), proven
HANDLE_SELECTION = 0x007624F0       # DialogMenu::HandleSelection(int id, Tile *), class proven: what a click calls
FORM_HEAP_ALLOCATE = 0x00401000     # proven
DIALOG_MENU = 0x011D9510            # DialogMenu * set by DialogMenu::Create (0x00761A20, proven)
DIALOG_MENU_ID = 1009

COMPARISONS = ['==', '!=', '>', '>=', '<', '<=']
RUN_ON = ['subject', 'target', 'reference', 'combat_target', 'linked_reference']
REFERENCE_PARAMS = {4, 6, 24, 26}   # ObjectRef, Actor, MapMarker, Container (engine map param type ids)
NUMBER_PARAMS = {0, 1, 2, 5, 8, 10, 18, 22, 23, 28, 32, 41, 45, 51, 52, 55}
PACKAGE_TYPES = ['find', 'follow', 'escort', 'eat', 'sleep', 'wander', 'travel', 'accompany', 'use_item_at', 'ambush',
                 'flee_not_combat', 'cast_magic', 'sandbox', 'patrol', 'guard', 'dialogue', 'use_weapon']
LOCATION_TYPES = ['near_reference', 'in_cell', 'near_current_location', 'near_editor_location', 'object_id',
                  'object_type', 'near_linked_reference', 'at_package_location']
DAYS = {-1: 'any', 0: 'Sunday', 1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 5: 'Friday', 6: 'Saturday',
        7: 'weekdays', 8: 'weekends', 9: 'Monday, Wednesday, Friday', 10: 'Tuesday, Thursday'}
PACKAGE_FLAGS = {0x1: 'offers_services', 0x4: 'must_complete', 0x8: 'maintain_speed_at_goal', 0x40: 'unlock_doors_at_start',
                 0x80: 'unlock_doors_at_end', 0x200: 'continue_if_pc_near', 0x400: 'once_per_day', 0x2000: 'always_run',
                 0x20000: 'always_sneak', 0x40000: 'allow_swimming', 0x100000: 'ignore_combat', 0x200000: 'weapons_unequipped',
                 0x800000: 'weapon_drawn', 0x8000000: 'no_combat_alert', 0x20000000: 'wear_sleep_outfit'}
INFO_FLAGS = {0x01: 'goodbye', 0x02: 'random', 0x04: 'say_once', 0x08: 'run_immediately', 0x10: 'info_refusal',
              0x20: 'random_end', 0x40: 'run_for_rumors', 0x80: 'speech_challenge'}
INFO_FLAGS2 = {0x01: 'say_once_a_day', 0x02: 'always_darken', 0x10: 'low_intelligence', 0x20: 'high_intelligence'}
SPEAKER_FUNCTIONS = {'GetIsID', 'GetIsReference', 'GetIsVoiceType', 'GetIsRace', 'GetIsClass', 'GetInFaction',
                     'GetIsSex', 'GetIsCreature', 'GetIsCreatureType', 'GetIsClassDefault', 'GetIsAlignment'}
CHALLENGE = {0: None, 1: 'very_easy', 2: 'easy', 3: 'average', 4: 'hard', 5: 'very_hard'}


def _bits(value, names):
    return [n for b, n in names.items() if value & b]


class SocialTools:
    def __init__(self, live, data=None):
        self.live = live
        self.data = data
        self._commands = None
        self._scratch = None

    # ------------------------------------------------------------------ helpers
    @property
    def link(self):
        return self.live.link

    def commands(self):
        if self._commands is None:
            self._commands = {}
            for c in self.live.engine().commands:
                if c.get('table') == 'script' and c.get('opcode'):
                    self._commands[int(c['opcode'], 16)] = c
        return self._commands

    def scratch(self):
        """A small buffer in game memory for out parameters (double results, bool &), kept per game process."""
        process = self.live.game.process()
        pid = process[0] if process else None
        if not self._scratch or self._scratch[0] != pid:
            call = self.link.one('call', word(FORM_HEAP_ALLOCATE), 'i', '0', '0', word(0x800))
            address = int(call['eax'], 16) if call.get('ok') else 0
            if not address:
                raise LiveError('ALLOC_FAILED', 'FormHeap_Allocate returned null')
            self._scratch = (pid, address)
        return self._scratch[1]

    def _string(self, raw, at=0):
        """BSStringT<char> {char *, u16 length, u16 capacity} inside raw bytes."""
        pointer, length = struct.unpack_from('<IH', raw, at)
        return pointer, length

    def _texts(self, pointers, limit=400):
        live = [p for p in pointers if p]
        results = self.link.batch([('rs', word(p), str(limit)) for p in live]) if live else []
        texts = {p: (r.get('text') if r.get('ok') else None) for p, r in zip(live, results)}
        return [texts.get(p) if p else None for p in pointers]

    def _form_ptr(self, form_id):
        if not form_id or form_id in ('00000000',):
            return 0
        try:
            return self.live.resolve(form_id)['ptr']
        except LiveError:
            return 0

    def _ref_values(self, ref_ptr, expressions):
        return self.live.values(expressions, ref_ptr)

    # ------------------------------------------------------------------ conditions
    @staticmethod
    def _item(ptr, raw):
        flags = raw[0]
        function, = struct.unpack_from('<H', raw, 8)
        param1, param2, run_on, reference = struct.unpack_from('<IIiI', raw, 0x0C)
        return {'ptr': ptr, 'flags': flags, 'or': bool(flags & 1), 'useGlobal': bool(flags & 4),
                'comparison': COMPARISONS[(flags & 0xE0) >> 5] if (flags & 0xE0) >> 5 < 6 else (flags >> 5),
                'value': struct.unpack_from('<f', raw, 4)[0], 'globalPtr': struct.unpack_from('<I', raw, 4)[0] if flags & 4 else 0,
                'function': function, 'param1': param1, 'param2': param2, 'runOn': run_on, 'reference': reference}

    def read_conditions(self, list_address, limit=200):
        return self.read_condition_lists([list_address], limit)[0]

    def read_condition_lists(self, addresses, limit=64):
        """Condition items of many lists: BSSimpleList walks and item reads batched (a few frames for thousands)."""
        heads = []
        for chunk in _chunks(list(addresses), 200):
            results = self.link.batch([('ls', word(a), str(limit)) for a in chunk])
            for a, r in zip(chunk, results):
                if r.get('ok'):
                    heads.append([int(x, 16) for x in r.get('items', [])])
                else:
                    heads.append(self.live.walk_list(a, limit))
        flat = sorted({p for h in heads for p in h})
        raws = {}
        for chunk in _chunks(flat, 400):
            for p, raw in zip(chunk, self.live.reads([(p, 0x1C) for p in chunk])):
                raws[p] = raw
        return [[self._item(p, raws[p]) for p in h if raws.get(p)] for h in heads]

    def evaluate_conditions(self, list_address, subject_ptr, target_ptr):
        """Each condition of a list evaluated like TESConditionItem::ContainsTrait, and the list combined
        (OR binds the item to the next one; AND between groups)."""
        items = self.read_conditions(list_address)
        if not items:
            return {'conditions': [], 'result': True, 'note': 'no conditions'}
        return self.evaluate_jobs([(items, subject_ptr, target_ptr)])[0]

    def evaluate_jobs(self, jobs, describe=True):
        """jobs: [(items, subject ptr, target ptr)] -> [{'conditions', 'result'}], every handler call batched."""
        commands = self.commands()
        functions = sorted({i['function'] for items, _, _ in jobs for i in items})
        records = {}
        for f in functions:
            c = commands.get(0x1000 + f)
            if c and c.get('record'):
                records[f] = (c, int(c['record'], 16))
        raw_records = dict(zip(records, self.live.reads([(r, 0x24) for _, r in records.values()]))) if records else {}
        # Combat target / linked reference per subject, only when a condition runs on them.
        extra = {}
        for items, subject, _ in jobs:
            if subject and subject not in extra and any(i['runOn'] in (3, 4) for i in items):
                cid, lid = self._ref_values(subject, ['GetCombatTarget', 'GetLinkedRef'])
                extra[subject] = (self._form_ptr(cid) if isinstance(cid, str) else 0,
                                  self._form_ptr(lid) if isinstance(lid, str) else 0)
        gptrs = sorted({i['globalPtr'] for items, _, _ in jobs for i in items if i['useGlobal'] and i['globalPtr']})
        globals_ = {}
        for g, raw in zip(gptrs, self.live.reads([(g + 0x24, 4) for g in gptrs]) if gptrs else []):
            globals_[g] = struct.unpack('<f', raw)[0] if raw else None
        scratch = self.scratch()
        pending, out = [], []
        for items, subject, target in jobs:
            entries = []
            for n, item in enumerate(items):
                c, _ = records.get(item['function'], (None, None))
                raw = raw_records.get(item['function'])
                entry = {'index': n, 'function': c['name'] if c else 'function #%d' % item['function'],
                         'runOn': RUN_ON[item['runOn']] if 0 <= item['runOn'] < 5 else item['runOn'],
                         'comparison': item['comparison'], 'or': item['or']}
                run, other = subject, target
                combat, linked = extra.get(subject, (0, 0))
                if item['runOn'] == 1:
                    run, other = target, subject
                elif item['runOn'] == 2:
                    run = item['reference']
                elif item['runOn'] == 3:
                    run, other = combat, subject
                elif item['runOn'] == 4:
                    run, other = linked, 0
                params = (c or {}).get('params') or []
                p1 = item['param1']
                if not p1 and params and params[0].get('typeId') in REFERENCE_PARAMS:
                    p1 = other or 0
                entry['_params'] = (params, [p1, item['param2']])
                compare = globals_.get(item['globalPtr']) if item['useGlobal'] else item['value']
                entry['compareTo'] = compare
                if item['useGlobal']:
                    entry['global'] = '0x%08X' % item['globalPtr']
                entries.append(entry)
                if raw is None:
                    entry.update({'evaluated': False, 'passed': False, 'why': 'not in the command table'})
                    continue
                needs_ref, = struct.unpack_from('<B', raw, 0x10)
                handler, = struct.unpack_from('<I', raw, 0x20)
                if not handler:
                    entry.update({'evaluated': False, 'passed': False, 'why': 'the command has no condition handler'})
                elif needs_ref and not run:
                    entry.update({'evaluated': False, 'value': None, 'passed': False,
                                  'why': 'needs a reference to run on and there is none (%s)' % entry['runOn']})
                else:
                    pending.append((entry, handler, run or 0, p1, item['param2'], compare))
            out.append(entries)
        # 250 handler calls per frame: write 0, call, read the double back.
        for chunk in _chunks(pending, 250):
            ops = []
            for k, (entry, handler, run, p1, p2, _) in enumerate(chunk):
                slot = scratch + 8 * k
                ops += [('wr', word(slot), '0000000000000000'),
                        ('call', word(handler), 'i', '0', '0', word(run), word(p1), word(p2), word(slot)),
                        ('rd', word(slot), '8')]
            results = self.link.batch(ops)
            for k, (entry, _, _, _, _, compare) in enumerate(chunk):
                call, read = results[3 * k + 1], results[3 * k + 2]
                handled = call.get('ok') and bool(int(call.get('eax', '0'), 16) & 0xFF)
                entry['evaluated'] = bool(handled)
                if not handled or not read.get('ok'):
                    entry.update({'value': None, 'passed': False, 'why': 'the handler declined (wrong reference type?)'})
                    continue
                value = float(struct.unpack('<f', struct.pack('<f', struct.unpack('<d', bytes.fromhex(read['hex']))[0]))[0])
                entry['value'] = round(value, 4)
                entry['passed'] = _compare(entry['comparison'], value, compare)
        names = {}
        if describe:
            forms = sorted({v for entries in out for e in entries
                            for spec, v in zip(*e['_params']) if v and spec.get('typeId') not in NUMBER_PARAMS
                            and spec.get('typeId') != 5})
            for chunk in _chunks(forms, 400):
                for p, h in zip(chunk, self.live.form_ids(chunk)):
                    names[p] = h['formId'] if h else None
        results = []
        for entries in out:
            for e in entries:
                params, values = e.pop('_params')
                if describe:
                    e['parameters'] = self._params(params, values, names)
                    e['text'] = _describe(e)
            results.append({'conditions': entries, 'result': _combine(entries)})
        return results

    def _params(self, params, values, names):
        out = []
        for spec, value in zip(params, values):
            kind = spec.get('typeId')
            label = spec.get('label') or spec.get('type')
            if kind == 5:
                av = fnv_facts.ACTOR_VALUES
                out.append({'label': label, 'value': av[value] if value < len(av) else value})
            elif kind in NUMBER_PARAMS:
                if kind == 2:
                    value = round(struct.unpack('<f', struct.pack('<I', value))[0], 4)
                elif kind == 8:
                    value = chr(value) if 0x58 <= value <= 0x5A else value
                out.append({'label': label, 'value': value})
            else:
                out.append({'label': label, 'form': names.get(value) if value else None})
        return out

    # ------------------------------------------------------------------ fnv_dialogue_tree
    def dialogue_tree(self, speaker, topic='GREETING', include_text=True, limit=80, scope='speaker'):
        """Responses for a topic. scope: speaker (responses written for this speaker: their speaker conditions
        pass, plus any the engine allows now), available (only those the engine allows now), all."""
        if scope not in ('speaker', 'available', 'all'):
            raise ValueError('scope must be speaker, available or all')
        self.live.in_game()
        s = self.live.resolve(speaker)
        player = self.live.resolve('player')
        t = self.live.resolve(topic)
        head = self.live.form_ids([t['ptr']])[0]
        if not head or head['formType'] != 0x45:
            raise LiveError('NOT_A_TOPIC', '%s is not a dialogue topic (DIAL)' % topic)
        topic_name = self._texts([self.live.u32s([t['ptr'] + 0x18 + 4])[0]])[0]
        quest_infos = self.live.walk_list(t['ptr'] + 0x2C, 1000)
        qraws = self.live.reads([(q, 0x1C) for q in quest_infos]) if quest_infos else []
        arrays = []
        for raw in qraws:
            if raw:
                quest_ptr, = struct.unpack_from('<I', raw, 0)
                base, _, size = struct.unpack_from('<III', raw, 8)
                arrays.append((quest_ptr, base, min(max(size, 0), 4000)))
        datas = self.live.reads([(b, 4 * n) for _, b, n in arrays if b and n]) if arrays else []
        data_iter = iter(datas)
        groups = []
        for quest_ptr, base, n in arrays:
            data = next(data_iter) if base and n else b''
            data = data or b''
            groups.append((quest_ptr, [p for p in struct.unpack('<%dI' % (len(data) // 4), data) if p]))
        quest_ptrs = [q for q, _ in groups]
        quest_data = dict(zip(quest_ptrs, self.live.reads([(q + 0x3C, 2) for q in quest_ptrs]) if quest_ptrs else []))
        infos = []
        for group, (quest_ptr, ptrs) in enumerate(groups):
            qd = quest_data.get(quest_ptr)
            for order, info in enumerate(ptrs):
                infos.append({'ptr': info, 'questPtr': quest_ptr, 'priority': qd[1] if qd else 0, 'order': order,
                              'group': group})
        # The engine's verdict for every response (TESTopicInfo::CheckCondition), batched.
        deferred = self.scratch() + 0x7F0
        for chunk in _chunks(infos, 300):
            results = self.link.batch([('call', word(CHECK_CONDITION), 'i', word(i['ptr']), '0', word(deferred),
                                        word(i['questPtr']), word(s['ptr']), word(player['ptr'])) for i in chunk])
            for i, r in zip(chunk, results):
                i['available'] = bool(r.get('ok')) and bool(int(r.get('eax', '0'), 16) & 0xFF)
        ranked = sorted(infos, key=lambda i: (-i['priority'], i['group'], i['order']))
        pick = next((i for i in ranked if i['available']), None)
        if scope == 'all':
            chosen = ranked
        elif scope == 'available':
            chosen = [i for i in ranked if i['available']]
        else:
            lists = self.read_condition_lists([i['ptr'] + 0x18 for i in infos])
            commands = self.commands()
            jobs, owners = [], []
            for info, items in zip(infos, lists):
                info['items'] = items
                ident = [it for it in items if it['runOn'] == 0 and
                         (commands.get(0x1000 + it['function']) or {}).get('name') in SPEAKER_FUNCTIONS]
                info['identity'] = ident
                if ident:
                    jobs.append((ident, s['ptr'], player['ptr']))
                    owners.append(info)
            for info, result in zip(owners, self.evaluate_jobs(jobs, describe=False)):
                passed = {it['ptr']: e.get('passed') for it, e in zip(info['identity'], result['conditions'])}
                # Only a positive test (GetIsID X == 1) names a speaker; "== 0" tests exclude others.
                named = any(passed[it['ptr']] and _positive(it) for it in info['identity'])
                info['forSpeaker'] = named and _speaker_match(info['items'], passed)
            chosen = [i for i in ranked if i['available'] or i.get('forSpeaker')]
        selected = chosen[:limit]
        full = self.evaluate_jobs([(i['items'] if 'items' in i else self.read_conditions(i['ptr'] + 0x18),
                                    s['ptr'], player['ptr']) for i in selected]) if selected else []
        heads = self.live.form_ids([i['ptr'] for i in selected])
        raws = self.live.reads([(i['ptr'], 0x50) for i in selected]) if selected else []
        prompts = self._texts([struct.unpack_from('<I', r, 0x28)[0] if r else 0 for r in raws])
        qheads = dict(zip(quest_ptrs, self.live.form_ids(quest_ptrs)))
        names = self.live.names(sorted({qheads[i['questPtr']]['formId'] for i in selected if qheads.get(i['questPtr'])}))
        out = []
        for info, conditions, ihead, raw, prompt in zip(selected, full, heads, raws, prompts):
            if raw is None:
                continue
            flags, flags2 = raw[0x25], raw[0x26]
            difficulty, = struct.unpack_from('<I', raw, 0x44)
            qh = qheads.get(info['questPtr'])
            entry = {'info': ihead['formId'] if ihead else None, 'quest': qh['formId'] if qh else None,
                     'questName': names.get(qh['formId']) if qh else None, 'questPriority': info['priority'],
                     'order': info['order'], 'engineSays': 'available' if info['available'] else 'blocked',
                     'prompt': prompt or None, 'flags': _bits(flags, INFO_FLAGS) + _bits(flags2, INFO_FLAGS2),
                     'goodbye': bool(raw[0x22]),
                     # The level is kept on every response; it only applies with the speech-challenge flag.
                     'speechChallenge': CHALLENGE.get(difficulty, difficulty) if flags & 0x80 else None,
                     'conditions': conditions['conditions'], 'conditionsPass': conditions['result']}
            if info['available'] != conditions['result']:
                entry['note'] = ('the per-condition check says %s, the engine says %s: say-once, quest conditions or '
                                 'the dummy-node rules decide' % ('pass' if conditions['result'] else 'fail',
                                                                  entry['engineSays']))
            out.append(entry)
        if include_text and self.data:
            for entry in out:
                if entry['info']:
                    entry['responses'] = self._responses(entry['info'])
        pick_head = self.live.form_ids([pick['ptr']])[0] if pick else None
        return {'speaker': s['formId'], 'topic': t['formId'], 'topicName': topic_name, 'scope': scope,
                'totalResponses': len(infos), 'availableNow': sum(1 for i in infos if i['available']),
                'matched': len(chosen), 'responses': out, 'count': len(out), 'truncated': len(chosen) > limit,
                'wouldSay': pick_head['formId'] if pick_head else None,
                'note': ('wouldSay: the first available response of the highest-priority quest, in list order (over all '
                         'responses of the topic). Random responses are picked among the available consecutive random '
                         'ones at run time. scope=speaker keeps responses whose speaker conditions (GetIsID, '
                         'GetIsVoiceType, GetInFaction...) pass for this speaker, and any available now.'),
                'uses': ['TESTopic quest info list and TopicInfoArray (decomp layouts, checked)',
                         'TESTopicInfo::CheckCondition (0x0061E600, proven) for each response',
                         "each condition's eval handler from the game's command table (TESConditionItem::ContainsTrait "
                         'logic)', 'response text from the winning plugin record (NAM1)']}

    def _editor_id(self, form_id):
        if not self.data:
            return None
        try:
            hits = self.data.record_find(form_id, limit=1)['results']
        except Exception:
            return None
        return hits[0].get('editorId') if hits else None

    def _responses(self, info_id):
        try:
            record = self.data.record_show(info_id, all_versions=False)
        except Exception:
            return None
        return [f.get('value') for f in record['fields'] if f['tag'] == 'NAM1' and isinstance(f.get('value'), str)]

    # ------------------------------------------------------------------ fnv_dialogue_choose
    def _menu(self):
        open_, mode = self.live.values(['MenuMode %d' % DIALOG_MENU_ID, 'GetActiveMenuMode'])
        if not open_:
            return None
        menu = self.live.u32s([DIALOG_MENU])[0]
        return menu or None

    def menu_state(self):
        menu = self._menu()
        if not menu:
            return {'open': False}
        raw = self.live.reads([(menu, 0x108)])[0]
        state, goodbye = struct.unpack_from('<II', raw, 0x28)
        controller, = struct.unpack_from('<I', raw, 0x70)
        speaker, = struct.unpack_from('<I', raw, 0x80)
        label = raw[0x84:0x104].split(b'\0')[0].decode('cp1252', 'replace')
        options = []
        if controller:
            list_ptr = self.live.u32s([controller + 4])[0]
            topics = self.live.walk_list(list_ptr, 100) if list_ptr else []
            # TopicMenuController's selection (0x0083E430) skips the list's first node: options start at the second.
            raws = self.live.reads([(t, 0x2C) for t in topics]) if topics else []
            texts = self._texts([struct.unpack_from('<I', r, 0)[0] if r else 0 for r in raws])
            items = self._listbox(menu)
            index_tiles = {data: tile for tile, data in items}
            for n, (tptr, r, text) in enumerate(zip(topics, raws, texts)):
                if n == 0 or r is None:
                    continue
                info, = struct.unpack_from('<I', r, 0x18)
                tp, = struct.unpack_from('<I', r, 0x28)
                ih, th = self.live.form_ids([info, tp])
                options.append({'index': n - 1, 'prompt': text, 'info': ih['formId'] if ih else None,
                                'topic': th['formId'] if th else None, 'refusal': bool(r[9]),
                                'new': bool(r[0x25]), 'tile': '0x%08X' % index_tiles[n - 1] if n - 1 in index_tiles else None})
        sh = self.live.form_ids([speaker])[0] if speaker else None
        return {'open': True, 'state': ['camera_zoom_in', 'load_speech', 'speech_idle', 'play_speech',
                                        'camera_zoom_out', 'service_fade_out'][state] if state < 6 else state,
                'goodbye': ['in_dialogue', 'natural_goodbye', 'force_goodbye', 'invalid_choice_list'][goodbye]
                if goodbye < 4 else goodbye, 'speaker': sh['formId'] if sh else None, 'topicLabel': label or None,
                'options': options}

    def _listbox(self, menu):
        """ListBox<int> items of the response list (DialogMenu +0x40; its BSSimpleList at +4): (tile, index)."""
        items = self.live.walk_list(menu + 0x44, 100)
        raws = self.live.reads([(i, 8) for i in items]) if items else []
        return [struct.unpack('<Ii', r) for r in raws if r]

    def _wait(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            state = self.menu_state()
            if predicate(state):
                return state
            time.sleep(0.25)
        return state

    def dialogue_choose(self, speaker=None, choices=None, start=True, timeout=30, close=False):
        """Open a conversation (the player activates the speaker) and pick options in order. Each choice is an
        option index (0 = first shown), a prompt substring, or an info form id. Returns each step's options, the
        pick and what the speaker answered."""
        self.live.in_game()
        steps = []
        if start:
            if not speaker:
                raise ValueError('speaker is required to start a conversation')
            state = self.menu_state()
            if not state['open']:
                s = self.live.resolve(speaker)
                self.live.changing('dialogue_choose')
                self.live.console('Activate player', s['ptr'])
                self.live.journal.add({'kind': 'dialogue', 'action': 'start', 'speaker': s['formId'], 'undo': None})
                state = self._wait(lambda st: st['open'], min(timeout, 15))
                if not state or not state['open']:
                    raise LiveError('NO_DIALOGUE', 'The dialogue menu did not open (is %s able to talk?)' % speaker,
                                    {'state': state})
        for choice in choices or []:
            state = self._wait(lambda st: not st['open'] or (st['state'] == 'speech_idle' and st['options']), timeout)
            if not state['open']:
                steps.append({'choice': choice, 'error': 'the conversation ended', 'state': state})
                break
            if state['state'] != 'speech_idle' or not state['options']:
                steps.append({'choice': choice, 'error': 'no options to choose from in time', 'state': state})
                break
            option = _match(state['options'], choice)
            if option is None:
                steps.append({'choice': choice, 'error': 'no option matches', 'options': state['options']})
                break
            if not option.get('tile'):
                steps.append({'choice': choice, 'error': 'the option has no tile in the response list', 'options': state['options']})
                break
            menu = self._menu()
            time.sleep(0.6)  # HandleSelection ignores clicks within 500 ms of the list being filled
            result = self.link.one('call', word(HANDLE_SELECTION), 'v', word(menu), '0', word(0xFFFFFFFF), option['tile'][2:])
            self.live.journal.add({'kind': 'dialogue', 'action': 'choose', 'info': option['info'],
                                   'prompt': option['prompt'], 'undo': None})
            step = {'choice': choice, 'picked': option, 'options': state['options'], 'accepted': bool(result.get('ok'))}
            after = self._wait(lambda st: not st['open'] or st['state'] != 'speech_idle' or st['topicLabel'] != state['topicLabel'], 5)
            step['after'] = {k: after.get(k) for k in ('open', 'state', 'topicLabel', 'goodbye')} if after else None
            if self.data and option.get('info'):
                step['response'] = self._responses(option['info'])
            steps.append(step)
        final = self._wait(lambda st: not st['open'] or st['state'] == 'speech_idle', min(timeout, 20))
        if close and final and final['open']:
            self.live.console('CloseActiveMenu')
            final = self.menu_state()
        return {'steps': steps, 'final': final,
                'uses': ['Activate (the player talks to the speaker)', 'DialogMenu layout and TopicMenuController list '
                         '(decomp, checked)', 'DialogMenu::HandleSelection (0x007624F0, class proven): the call a click '
                         'on a response makes']}

    # ------------------------------------------------------------------ fnv_ai_packages
    def _base_offset(self, cls, component, depth=0):
        layout = self.live._layout(cls)
        if not layout or depth > 8:
            return None
        offset = 0
        for base in layout.get('bases') or []:
            if base == component:
                return offset
            inner = self._base_offset(base, component, depth + 1)
            if inner is not None:
                return offset + inner
            size = (self.live._layout(base) or {}).get('size') or (0x18 if base == 'TESForm' else 0)
            if not size:
                return None
            offset += size
        return None

    def ai_packages(self, actor):
        self.live.in_game()
        a = self.live.resolve(actor)
        base_id, current, procedure = self.live.values(['GetBaseObject', 'GetCurrentPackage', 'GetCurrentAIProcedure'], a['ptr'])
        base_ptr = self._form_ptr(base_id) if isinstance(base_id, str) else 0
        if not base_ptr:
            raise LiveError('NO_BASE', 'No base form for %s' % actor)
        cls = self.live.rtti([base_ptr])[base_ptr]['class']
        offset = self._base_offset(cls, 'TESAIForm') if cls else None
        if offset is None:
            raise LiveError('NO_LAYOUT', 'No TESAIForm in the layout of %s' % cls)
        packages = self.live.walk_list(base_ptr + offset + 0x18, 200)
        hour, day, month, weekday = self.live.values(['GameHour', 'GameDay', 'GameMonth', 'GetDayOfWeek'])
        heads = self.live.form_ids(packages)
        raws = self.live.reads([(p, 0x48) for p in packages]) if packages else []
        out = []
        for n, (ptr, head, raw) in enumerate(zip(packages, heads, raws)):
            if raw is None:
                continue
            flags, = struct.unpack_from('<I', raw, 0x1C)
            ptype = struct.unpack_from('<b', raw, 0x20)[0]
            location_ptr, target_ptr = struct.unpack_from('<II', raw, 0x2C)
            sched = raw[0x38:0x40]
            s_month, s_dow, s_date, s_time = struct.unpack_from('<bbBb', sched)
            s_duration, = struct.unpack_from('<i', sched, 4)
            schedule = {'month': s_month if s_month >= 0 else 'any', 'dayOfWeek': DAYS.get(s_dow, s_dow),
                        'date': s_date or 'any', 'hour': s_time if s_time >= 0 else 'any', 'durationHours': s_duration}
            schedule_ok, why = _schedule_ok(s_month, s_dow, s_date, s_time, s_duration, hour, day, month, weekday)
            conditions = self.evaluate_conditions(ptr + 0x40, a['ptr'], 0)
            location = None
            if location_ptr:
                lraw = self.live.reads([(location_ptr, 0xC)])[0]
                if lraw:
                    # LocationDescriptor: type at +0, radius at +4 (GetRadius 0x0067F1C0), the reference, cell,
                    # object or object type at +8 (GetLocationReference 0x0067F390).
                    ltype, = struct.unpack_from('<B', lraw, 0)
                    radius, ldata = struct.unpack_from('<iI', lraw, 4)
                    location = {'type': LOCATION_TYPES[ltype] if ltype < len(LOCATION_TYPES) else ltype, 'radius': radius}
                    if ltype in (0, 1, 4) and ldata:
                        f = self.live.form_ids([ldata])[0]
                        location['form'] = f['formId'] if f else None
                    elif ltype == 5:
                        location['objectType'] = ldata
            entry = {'position': n, 'package': head['formId'] if head else None,
                     'editorId': self._editor_id(head['formId']) if head else None,
                     'type': PACKAGE_TYPES[ptype] if 0 <= ptype < len(PACKAGE_TYPES) else ptype,
                     'flags': _bits(flags, PACKAGE_FLAGS), 'schedule': schedule, 'scheduleNow': schedule_ok,
                     'conditions': conditions['conditions'], 'conditionsPass': conditions['result'],
                     'location': location, 'running': bool(head) and head['formId'] == current}
            if schedule_ok and conditions['result']:
                entry['verdict'] = 'eligible'
            else:
                entry['verdict'] = 'not_now'
                entry['why'] = [w for w in ([why] if not schedule_ok else []) +
                                (['conditions fail'] if not conditions['result'] else []) if w]
            out.append(entry)
        first = next((e for e in out if e['verdict'] == 'eligible'), None)
        for e in out:
            if e['verdict'] == 'eligible' and first and e is not first:
                e['why'] = ['an earlier package on the list is eligible (%s)' % first['package']]
                e['verdict'] = 'shadowed'
        on_list = any(e['running'] for e in out)
        return {'actor': a['formId'], 'base': base_id, 'baseClass': cls, 'current': current,
                'currentProcedure': procedure, 'currentOnList': on_list,
                'expected': first['package'] if first else None,
                'matchesEngine': (first['package'] == current) if first and on_list else None,
                'clock': {'hour': hour, 'day': day, 'month': month, 'dayOfWeek': DAYS.get(int(weekday), weekday)
                          if weekday is not None else None},
                'packages': out,
                'note': ('The engine runs the first package on the list whose schedule and conditions allow it. '
                         'currentOnList false: the running package comes from elsewhere (a script added it, AddScriptPackage, '
                         'a quest-forced package, combat, dialogue, or a template NPC\'s list).'),
                'uses': ['TESAIForm package list and TESPackage layout (decomp, checked)',
                         "each condition's eval handler from the game's command table", 'GetCurrentPackage, GameHour, '
                         'GameDay, GameMonth, GetDayOfWeek']}

    # ------------------------------------------------------------------ fnv_form_live_diff
    COMPONENTS = {'TESFullName': ('name', 'string'), 'TESValueForm': ('value', 'u32'), 'TESWeightForm': ('weight', 'f32'),
                  'TESHealthForm': ('health', 'u32'), 'TESAttackDamageForm': ('damage', 'u16'),
                  'TESScriptableForm': ('script', 'ptr')}

    def form_live_diff(self, form):
        self.live.in_game()
        if not self.data:
            raise LiveError('NO_DATA', 'Plugin records are not available')
        f = self.live.resolve(form)
        head = self.live.form_ids([f['ptr']])[0]
        record = self.data.record_show(f['formId'], all_versions=False)
        cls = self.live.rtti([f['ptr']])[f['ptr']]['class']
        live, comps = {}, []
        for comp, (field, kind) in self.COMPONENTS.items():
            offset = self._base_offset(cls, comp) if cls else None
            if offset is None:
                continue
            comps.append(comp)
            raw = self.live.reads([(f['ptr'] + offset + 4, 8)])[0]
            if raw is None:
                continue
            if kind == 'string':
                live[field] = self._texts([struct.unpack_from('<I', raw)[0]])[0] or ''
            elif kind == 'u32':
                live[field] = struct.unpack_from('<i', raw)[0]
            elif kind == 'u16':
                live[field] = struct.unpack_from('<H', raw)[0]
            elif kind == 'f32':
                live[field] = round(struct.unpack_from('<f', raw)[0], 3)
            elif kind == 'ptr':
                p = struct.unpack_from('<I', raw)[0]
                h = self.live.form_ids([p])[0] if p else None
                live[field] = h['formId'] if h else None
        if record['type'] == 'GLOB':
            raw = self.live.reads([(f['ptr'] + 0x24, 4)])[0]
            live['value'] = round(struct.unpack('<f', raw)[0], 6) if raw else None
        if record['type'] == 'QUST':
            raw = self.live.reads([(f['ptr'] + 0x3C, 2)])[0]
            if raw:
                live['flags'], live['priority'] = raw[0], raw[1]
        plugin = _record_values(record)
        rows, same = [], []
        for field in sorted(set(plugin) | set(live)):
            a, b = plugin.get(field), live.get(field)
            if field not in live or field not in plugin:
                continue
            if _equal(a, b):
                same.append(field)
            else:
                rows.append({'field': field, 'plugin': a, 'live': b})
        form_flags = '0x%08X' % head['flags'] if head else None
        return {'form': f['formId'], 'type': record['type'], 'editorId': record.get('editorId'), 'class': cls,
                'winner': record['winner'], 'changed': rows, 'same': same, 'liveFormFlags': form_flags,
                'notCompared': sorted(set(plugin) - set(live)),
                'note': ('changed: values that differ between the winning plugin record and memory now (scripts, '
                         'console commands or the engine changed them). Form flags also carry runtime bits.'),
                'uses': ['component layouts from the decomp (TESFullName, TESValueForm, TESWeightForm, TESHealthForm, '
                         'TESAttackDamageForm, TESScriptableForm, TESGlobal, TESQuest)', 'fnv_record_show']}


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _speaker_match(items, passed):
    """Is a response written for this speaker? Its speaker conditions (passed: item ptr -> bool) pass, OR groups
    counted like the engine does; a group that mixes in other conditions is not held against it."""
    if not passed:
        return False
    group, ok = [], True
    for item in items:
        group.append(item)
        if not item['or']:
            mine = [g for g in group if g['ptr'] in passed]
            if mine and len(mine) == len(group) and not any(passed[g['ptr']] for g in mine):
                ok = False
            group = []
    if group:
        mine = [g for g in group if g['ptr'] in passed]
        if mine and len(mine) == len(group) and not any(passed[g['ptr']] for g in mine):
            ok = False
    return ok


def _positive(item):
    value = item['value']
    return (item['comparison'] in ('==', '>=') and value >= 1) or (item['comparison'] == '!=' and value == 0) or         (item['comparison'] == '>' and value >= 0)


def _combine(entries):
    """Consecutive ORed items form a group; the list passes when every group passes."""
    result, group = True, []
    for entry in entries:
        group.append(bool(entry.get('passed')))
        if not entry['or']:
            result = result and any(group)
            group = []
    if group:
        result = result and any(group)
    return result


def _record_values(record):
    out = {}
    for f in record['fields']:
        tag, value = f['tag'], f.get('value')
        if tag == 'FULL' and isinstance(value, str):
            out['name'] = value
        elif tag == 'SCRI':
            out['script'] = (f.get('form') or {}).get('formId')
        elif tag == 'DATA' and isinstance(value, dict):
            for k in ('value', 'weight', 'health', 'damage'):
                if k in value:
                    out[k] = value[k]
            if record['type'] == 'QUST':
                out['flags'], out['priority'] = value.get('flags'), value.get('priority')
        elif tag == 'FLTV' and record['type'] == 'GLOB':
            out['value'] = value
    return out


def _equal(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-3
        except (TypeError, ValueError):
            return False
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().upper() == b.strip().upper() if len(a) == 8 and len(b) == 8 else a == b
    return a == b


def _compare(op, value, compare):
    if compare is None or value is None:
        return False
    a, b = float(value), float(compare)
    return {'==': a == b, '!=': a != b, '>': a > b, '>=': a >= b, '<': a < b, '<=': a <= b}.get(op, False)


def _describe(entry):
    args = ' '.join(str(p.get('form') or p.get('value')) for p in entry.get('parameters') or [] if
                    p.get('form') or p.get('value') not in (None, 0))
    text = '%s%s %s %s' % (entry['function'], (' ' + args) if args else '', entry['comparison'],
                           _fmt(entry.get('compareTo')))
    if entry['runOn'] != 'subject':
        text = '%s (on %s)' % (text, entry['runOn'])
    if entry.get('or'):
        text += ' OR'
    return text


def _fmt(v):
    if isinstance(v, float) and v == int(v):
        return '%d' % v
    return str(v)


def _schedule_ok(month, dow, date, start, duration, hour, day, game_month, weekday):
    if None in (hour, day, game_month, weekday):
        return True, None
    if month >= 0 and int(game_month) != month:
        return False, 'scheduled for month %d, it is month %d' % (month, game_month)
    if date and int(day) != date:
        return False, 'scheduled for day %d, it is day %d' % (date, day)
    w = int(weekday)
    if dow >= 0:
        allowed = {7: {1, 2, 3, 4, 5}, 8: {0, 6}, 9: {1, 3, 5}, 10: {2, 4}}.get(dow, {dow})
        if w not in allowed:
            return False, 'scheduled for %s, it is %s' % (DAYS.get(dow, dow), DAYS.get(w, w))
    if start >= 0:
        if duration <= 0:
            return int(hour) == start, None if int(hour) == start else 'starts at %d:00 only, it is %.1f' % (start, hour)
        span = (float(hour) - start) % 24
        if span >= duration:
            return False, 'runs %d:00 for %d h, it is %.1f' % (start, duration, hour)
    return True, None


def _match(options, choice):
    if isinstance(choice, int) or (isinstance(choice, str) and choice.isdigit()):
        index = int(choice)
        return next((o for o in options if o['index'] == index), None)
    text = str(choice).strip()
    up = text.upper().removeprefix('0X')
    by_id = next((o for o in options if o.get('info') and len(up) == 8 and o['info'].upper() == up), None)
    if by_id:
        return by_id
    return next((o for o in options if o.get('prompt') and text.lower() in o['prompt'].lower()), None)
