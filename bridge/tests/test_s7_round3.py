"""PLAN-3 S7: fixes for the round-3 audit (BUG-LOG Round 3: U, K, W, X)."""
import ast
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

BRIDGE = Path(__file__).resolve().parents[1]


def tool_source(name):
    source = (BRIDGE / 'geck_mcp' / 'fnv_live_tools.py').read_text(encoding='utf-8')
    fn = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    return fn


class NameExportPathTests(unittest.TestCase):
    """X1: fnv_name_export wrote to any path the call named, as a read-only tool."""

    def tools(self, tmp):
        from geck_mcp import fnv_naming
        db_path = Path(tmp) / 'atlas.sqlite'
        db = sqlite3.connect(db_path)
        db.executescript('CREATE TABLE runs (id, started, source, probes, notes);'
                         'CREATE TABLE harvests (id, run, at, probes, functions, notes);'
                         'CREATE TABLE harvest_counts (harvest, address, calls);'
                         "INSERT INTO harvests VALUES (1, 1, 0, '', 1, '');"
                         'INSERT INTO harvest_counts VALUES (1, 4198400, 5);')
        db.close()
        state = Path(tmp) / 'state'
        state.mkdir()
        live = SimpleNamespace(engine=lambda: SimpleNamespace(functions={}, identity=lambda: {}),
                               deep=SimpleNamespace(_image=lambda required=False: None),
                               project=SimpleNamespace(state_dir=state))
        tools = fnv_naming.NamingTools.__new__(fnv_naming.NamingTools)
        tools.live = live
        tools.db = lambda: sqlite3.connect(db_path)
        return tools, state

    def test_a_path_outside_the_trace_and_state_folders_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, _ = self.tools(tmp)
            victim = Path(tmp) / 'Data' / 'MyMod.esp'
            victim.parent.mkdir()
            victim.write_bytes(b'TES4')
            with self.assertRaises(Exception) as caught:
                tools.export(path=str(victim))
            self.assertEqual(getattr(caught.exception, 'code', None), 'BAD_PATH')
            self.assertEqual(victim.read_bytes(), b'TES4')

    def test_dotdot_out_of_the_state_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, state = self.tools(tmp)
            with self.assertRaises(Exception) as caught:
                tools.export(path=str(state / '..' / 'escape.json'))
            self.assertEqual(getattr(caught.exception, 'code', None), 'BAD_PATH')
            self.assertFalse((Path(tmp) / 'escape.json').exists())

    def test_only_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, state = self.tools(tmp)
            with self.assertRaises(Exception) as caught:
                tools.export(path=str(state / 'trace.esp'))
            self.assertEqual(getattr(caught.exception, 'code', None), 'BAD_PATH')

    def test_a_json_under_the_state_folder_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, state = self.tools(tmp)
            want = state / 'traces' / 't.json'
            self.assertEqual(tools._export_path(str(want)), want.resolve())
            with mock.patch.dict(os.environ, {'FNVSYM_ROOT': tmp}):        # no machine default (release Task 4.1)
                default = tools._export_path(None)
            self.assertEqual(default.suffix, '.json', 'the default stays in fnvsym/traces')
            self.assertEqual(default.parent, Path(tmp) / 'traces')

    def test_the_state_folder_itself_is_refused(self):
        # S7 batch-1 review I3: engine_map.json and the other state files live there
        with tempfile.TemporaryDirectory() as tmp:
            tools, state = self.tools(tmp)
            (state / 'engine_map.json').write_text('{"map": 1}', encoding='utf-8')
            with self.assertRaises(Exception) as caught:
                tools._export_path(str(state / 'engine_map.json'))
            self.assertEqual(getattr(caught.exception, 'code', None), 'BAD_PATH')

    def test_an_existing_file_is_overwritten_only_when_it_is_a_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, state = self.tools(tmp)
            (state / 'traces').mkdir()
            other, old = state / 'traces' / 'notes.json', state / 'traces' / 'old.json'
            other.write_text('{"mine": true}', encoding='utf-8')
            old.write_text('{"format": "fnvsym-trace/2"}', encoding='utf-8')
            with self.assertRaises(Exception) as caught:
                tools._export_path(str(other))
            self.assertEqual(getattr(caught.exception, 'code', None), 'BAD_PATH')
            self.assertEqual(tools._export_path(str(old)), old.resolve())

    def test_registered_as_control_not_read(self):
        from geck_mcp import fnv_live_tools
        self.assertEqual(fnv_live_tools.FNV_LIVE_TOOLS['fnv_name_export'][0], 'control')
        fn = tool_source('fnv_name_export')
        self.assertEqual([ast.unparse(d) for d in fn.decorator_list], ['server.tool(annotations=control)'])


class BareIdentifierTests(unittest.TestCase):
    """W2: resolve() sent any EditorID-shaped ref to eval, so fnv_ref ref=QuitGame ran QuitGame under read."""

    def live(self, commands):
        from fakes import FakeLive
        fake = FakeLive(engine_control='read')
        self.addCleanup(fake.close)
        live = fake.live

        def engine():
            if commands is None:
                raise RuntimeError('no engine map')
            return SimpleNamespace(commands=commands)
        live.engine = engine
        live.editor_id = lambda edid: None
        return fake, live

    def evals(self, fake):
        return [bytes.fromhex(args[0]).decode('cp1252') for name, args in fake.link.ops if name == 'eval']

    def test_a_command_name_is_never_evaluated(self):
        from geck_mcp.fnv_live import LiveError
        fake, live = self.live([{'name': 'QuitGame', 'short': 'qqq'}, {'name': 'KillAll', 'short': ''}])
        for name in ('QuitGame', 'qqq', 'killall'):
            with self.assertRaises(LiveError) as caught:
                live.resolve(name)
            self.assertEqual(caught.exception.code, 'REF_NOT_FOUND')
        self.assertEqual(self.evals(fake), [])

    def test_without_the_command_list_a_bare_name_is_not_evaluated(self):
        from geck_mcp.fnv_live import LiveError
        fake, live = self.live(None)
        with self.assertRaises(LiveError):
            live.resolve('QuitGame')
        self.assertEqual(self.evals(fake), [])

    def test_a_plugin_command_is_not_evaluated_either(self):
        # S7 batch-1 review I4: plugin commands (JIP LN...) are not in the engine map's command list
        from geck_mcp.fnv_live import LiveError
        fake, live = self.live([{'name': 'QuitGame', 'short': 'qqq'}])
        fake.link.expressions['GetJIPVersion'] = {'type': 'number', 'value': 57}
        with self.assertRaises(LiveError):
            live.resolve('GetJIPVersion')
        self.assertEqual(self.evals(fake), [])

    def test_an_editor_id_resolves_through_the_index(self):
        fake, live = self.live([{'name': 'QuitGame', 'short': 'qqq'}])
        live.editor_id = lambda edid: {'formId': '00104C0C'} if edid == 'DocMitchellRef' else None
        fake.link.expressions[live.form_expr('00104C0C')] = {'type': 'form', 'ptr': '0x12345678', 'formId': '00104C0C'}
        self.assertEqual(live.resolve('DocMitchellRef')['formId'], '00104C0C')
        self.assertEqual(self.evals(fake), [live.form_expr('00104C0C')])


class ExpressionToolsAreActTests(unittest.TestCase):
    """W1: fnv_snapshot, fnv_diff and fnv_watch ran caller console lines and expressions as kind read."""

    def test_every_call_is_act(self):
        from geck_mcp.fnv_live_tools import gated_kind
        for name, action in (('fnv_snapshot', None), ('fnv_diff', None), ('fnv_watch', 'sample'),
                             ('fnv_watch', 'frames')):
            self.assertEqual(gated_kind(name, action, 'read'), 'act', name)

    def test_read_mode_refuses_a_snapshot_with_a_console_item(self):
        from fakes import FakeLive
        from geck_mcp.fnv_live_tools import gated_kind
        from geck_mcp.fnv_scenario import snapshot
        with FakeLive(engine_control='read') as fake:
            out = fake.live.run('fnv_snapshot', lambda: snapshot(fake.live, [{'console': 'player.kill'}], 'x'),
                                gated_kind('fnv_snapshot', None, 'read'))
            self.assertFalse(out.get('ok', True))
            self.assertEqual([name for name, _ in fake.link.ops if name in ('con', 'eval')], [])


class UndoOrderTests(unittest.TestCase):
    """U1: out-of-order undo was refused only for place_adjust, time fixed/speed and hot swaps."""

    def refused(self, older, newer):
        from fakes import FakeLive
        with FakeLive() as fake:
            live = fake.live
            fake.link.put(0x7000, bytes(16))
            first = live.journal.add(dict(older))
            live.journal.add(dict(newer))
            r = live.undo(journal_id=first['id'])['results'][0]
            return r.get('reason') == 'undo the later change first', fake

    def write(self, address, size=1):
        return {'kind': 'memory', 'undo': {'op': 'write', 'address': '0x%08X' % address, 'hex': '00' * size}}

    def test_overlapping_memory_writes(self):
        refused, fake = self.refused(self.write(0x7000, 4), self.write(0x7002, 4))
        self.assertTrue(refused)
        self.assertEqual(fake.link.get(0x7000, 4), bytes(4), 'nothing written by the refused undo')

    def test_disjoint_memory_writes_are_free(self):
        self.assertFalse(self.refused(self.write(0x7000, 4), self.write(0x7004, 4))[0])

    def test_a_patch_list_overlapping_a_write(self):
        older = {'kind': 'rng_seed', 'undo': {'op': 'writes', 'writes': [['0x00007008', '00' * 4]]}}
        self.assertTrue(self.refused(older, self.write(0x7009))[0])

    def test_the_same_setting(self):
        older = {'kind': 'setting', 'undo': {'op': 'console', 'line': 'SetGS fJumpHeightMin 64.0'}}
        newer = {'kind': 'setting', 'undo': {'op': 'console', 'line': 'SetGS fJumpHeightMin 100.0'}}
        other = {'kind': 'setting', 'undo': {'op': 'console', 'line': 'SetGS fOther 1.0'}}
        self.assertTrue(self.refused(older, newer)[0])
        self.assertFalse(self.refused(older, other)[0])

    def test_the_same_ref(self):
        older = {'kind': 'actor', 'ref': 'FF000900', 'undo': {'op': 'console_ref', 'ref': 'FF000900',
                                                              'lines': ['SetAV Health 100']}}
        newer = {'kind': 'actor', 'ref': 'FF000900', 'undo': {'op': 'console_ref', 'ref': 'FF000900',
                                                              'lines': ['SetAV Health 50']}}
        self.assertTrue(self.refused(older, newer)[0])

    # S7 batch-1 review I2: the target is the value, not the whole ref
    def ref_entry(self, ref, *lines, op='console_ref'):
        return {'kind': 'actor', 'ref': ref, 'undo': {'op': op, 'ref': ref, 'lines': list(lines)}}

    def test_different_values_of_one_ref_are_free(self):
        self.assertFalse(self.refused(self.ref_entry('00000014', 'SetAV Health 100'),
                                      self.ref_entry('00000014', 'SetAV Strength 5'))[0])
        self.assertTrue(self.refused(self.ref_entry('00000014', 'SetAV Health 100'),
                                     self.ref_entry('00000014', 'SetAV Health 50'))[0])

    def test_items_are_per_item(self):
        older = {'kind': 'item', 'undo': {'op': 'item_count', 'ref': '00000014', 'item': '0000000F', 'count': 1}}
        newer = {'kind': 'item', 'undo': {'op': 'item_count', 'ref': '00000014', 'item': '00015169', 'count': 1}}
        self.assertFalse(self.refused(older, newer)[0])
        self.assertTrue(self.refused(older, dict(newer, undo=dict(older['undo'])))[0])

    def test_a_spawn_can_be_deleted_after_a_move(self):
        self.assertFalse(self.refused(self.ref_entry('FF000900', 'Disable', 'MarkForDelete'),
                                      self.ref_entry('FF000900', 'SetPos X 0', 'SetPos Y 0', 'SetPos Z 0'))[0])

    def test_objectives_keep_their_index(self):
        def objective(index):
            return {'kind': 'quest', 'undo': {'op': 'console', 'line': 'SetObjectiveCompleted 0001A2B3 %d 0' % index}}
        self.assertFalse(self.refused(objective(10), objective(20))[0])
        self.assertTrue(self.refused(objective(10), objective(10))[0])

    def test_a_player_console_line_meets_the_players_ref(self):
        older = {'kind': 'input', 'undo': {'op': 'console', 'line': 'player.SetAngle Z 90.000'}}
        self.assertTrue(self.refused(older, self.ref_entry('00000014', 'SetAngle Z 10.000'))[0])

    def test_weather_lines_share_one_target(self):
        older = {'kind': 'world_edit', 'undo': {'op': 'console', 'line': 'ForceWeather 0001A2B3 1'}}
        newer = {'kind': 'world_edit', 'undo': {'op': 'console', 'line': 'ReleaseWeatherOverride'}}
        self.assertTrue(self.refused(older, newer)[0])


class QuotedNameTests(unittest.TestCase):
    """W4: fnv_formid_live pasted free text into a quoted xNVSE expression."""

    def test_a_quote_in_a_plugin_name_is_refused_before_any_eval(self):
        from fakes import FakeLive
        with FakeLive(engine_control='read') as fake:
            with self.assertRaises(ValueError):
                fake.live.formid_live('a.esp" + QuitGame + "')
            self.assertEqual([n for n, _ in fake.link.ops if n == 'eval'], [])

    def test_the_helper(self):
        from geck_mcp.fnv_link import quoted
        self.assertEqual(quoted('My Mod.esp'), '"My Mod.esp"')
        for bad in ('a"b.esp', 'a\nb.esp', 'a\rb.esp'):
            with self.assertRaises(ValueError):
                quoted(bad)


class AssetPathTests(unittest.TestCase):
    """X2: fnv_asset_check's folder check was lexical (Data\\..\\.. escaped it) and ran after is_file()."""

    def tools(self, root):
        from geck_mcp import fnv_records
        from geck_mcp.fnv_records import RecordsTools
        game_root = root / 'game'
        (game_root / 'Data').mkdir(parents=True)
        fnv = SimpleNamespace(mo2_root=root / 'mo2', mo2_profile='Dev')
        data = SimpleNamespace(project=SimpleNamespace(game_root=game_root, fnv=fnv, state_dir=None),
                               _live_plugins=lambda: None)
        tools = RecordsTools(data)
        tools._asset_index = lambda entries: (SimpleNamespace(find=lambda rel: None, archives=[]), [])
        saved = fnv_records.load_order, fnv_records._plugin_assets
        fnv_records.load_order = lambda project, live=None: []
        fnv_records._plugin_assets = lambda path: {}
        self.addCleanup(lambda: setattr(fnv_records, 'load_order', saved[0]))
        self.addCleanup(lambda: setattr(fnv_records, '_plugin_assets', saved[1]))
        return tools, game_root

    def test_dotdot_out_of_data_is_refused(self):
        from geck_mcp.fnv_data import DataError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools, game_root = self.tools(root)
            outside = root / 'other.esp'
            outside.write_bytes(b'x')
            with self.assertRaises(DataError) as caught:
                tools.asset_check(str(game_root / 'Data' / '..' / '..' / 'other.esp'))
            self.assertEqual(caught.exception.code, 'BAD_PATH')

    def test_a_missing_file_outside_is_bad_path_not_not_found(self):
        from geck_mcp.fnv_data import DataError
        with tempfile.TemporaryDirectory() as tmp:
            tools, _ = self.tools(Path(tmp))
            with self.assertRaises(DataError) as caught:
                tools.asset_check(str(Path(tmp) / 'nowhere' / 'x.esp'))
            self.assertEqual(caught.exception.code, 'BAD_PATH')

    def test_a_file_in_data_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, game_root = self.tools(Path(tmp))
            (game_root / 'Data' / 'Mine.esp').write_bytes(b'x')
            self.assertEqual(tools.asset_check(str(game_root / 'Data' / 'Mine.esp'))['plugin'], 'Mine.esp')


class StaleCopyTests(unittest.TestCase):
    """X3: the record index kept the rows of a plugin path that left the load order (Data copy vs mod copy)."""

    def test_indexing_one_copy_drops_the_other(self):
        from test_bugfix_a_c_s import tes4_plugin
        from geck_mcp.fnv_data import RecordIndex
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            data, mod = root / 'Data' / 'Mine.esp', root / 'mods' / 'Mine' / 'Mine.esp'
            data.parent.mkdir(parents=True)
            mod.parent.mkdir(parents=True)
            tes4_plugin(data, [(0x000800, 'STAT', 'OldRock')])
            tes4_plugin(mod, [(0x000801, 'STAT', 'NewRock')])
            index = RecordIndex(root / 'state')
            index.ensure(data)
            index.ensure(mod)
            db = index.db
            self.assertEqual([r[0] for r in db.execute("select path from files where lower(name) = 'mine.esp'")],
                             [str(mod)])
            self.assertEqual([r[0] for r in db.execute("select edid from records where owner = 'mine.esp'")],
                             ['NewRock'])
            index.ensure(data)       # switching back re-indexes the Data copy
            self.assertEqual([r[0] for r in db.execute("select edid from records where owner = 'mine.esp'")],
                             ['OldRock'])
            db.close()


class TwoServerPauseTests(unittest.TestCase):
    """U2: a pause undone from another server process was marked undone while the clock stayed paused."""

    def test_the_other_servers_undo_resumes_and_the_pauser_sees_it(self):
        import struct
        from fakes import FakeLive
        from geck_mcp import fnv_time
        from geck_mcp.fnv_time import TimeControl
        with FakeLive() as fake:
            fake.link.put(fnv_time.TIME_GLOBAL, struct.pack('<If', 0, 0.0))
            counter = {'n': 0}
            original = fake.link._op

            def op(name, args):          # Disable/Enable move the clock counter like the engine does
                if name == 'call' and int(args[0], 16) in (0x00AA0000, 0x00AA0010):
                    counter['n'] += 1 if int(args[0], 16) == 0x00AA0000 else -1
                    fake.link.put(fnv_time.TIME_GLOBAL, struct.pack('<I', counter['n']))
                return original(name, args)
            fake.link._op = op
            fns = {'disable': 0x00AA0000, 'enable': 0x00AA0010, 'clamp': 0x00AA0020}
            server_a, server_b = TimeControl(fake.live), TimeControl(fake.live)
            server_a.functions = server_b.functions = lambda: fns
            server_b.require_game()                                  # B caches "not paused" for this game
            server_a.pause()
            self.assertEqual(counter['n'], 1)
            fake.live.time_control = server_b                       # B runs the undo
            pause = [e for e in fake.live.journal.entries() if (e.get('undo') or {}).get('what') == 'pause'][0]
            result = fake.live.undo(journal_id=pause['id'])['results'][0]
            self.assertEqual(counter['n'], 0, 'the clock runs again')
            self.assertTrue(result['undone'])
            out = server_a.resume()                                 # A must not Enable a second time
            self.assertFalse(out.get('changed'))
            self.assertEqual(counter['n'], 0)


class SwapUndoTests(unittest.TestCase):
    """U3: a hot-swap undo whose reply was lost after it landed swapped the code back again on the retry."""

    def setup(self, fake):
        import struct
        from geck_mcp.fnv_data import SCRIPT_DATA, SCRIPT_HEADER, SCRIPT_REFS, SCRIPT_VARS
        target, holder = 0x9000, 0x9800
        for base, data in ((target, 0x1111), (holder, 0x2222)):
            fake.link.put(base + SCRIPT_HEADER, bytes(0x10))
            fake.link.put(base + SCRIPT_DATA, struct.pack('<I', data))
            fake.link.put(base + SCRIPT_REFS, bytes(8))
            fake.link.put(base + SCRIPT_VARS, bytes(8))
        return target, holder, SCRIPT_DATA

    def test_an_undo_that_already_landed_is_not_swapped_back(self):
        import struct
        from fakes import FakeLive
        with FakeLive() as fake:
            target, holder, data_off = self.setup(fake)
            # the state after a swap and a landed undo: the target holds its original code 0x1111 again
            entry = fake.live.journal.add({'kind': 'script_hot_swap', 'undo': {
                'op': 'swap_script', 'target': '0x%08X' % target, 'holder': '0x%08X' % holder,
                'data': '0x00001111'}})
            r = fake.live.undo(journal_id=entry['id'])['results'][0]
            self.assertTrue(r['undone'])
            self.assertEqual(struct.unpack('<I', fake.link.get(target + data_off, 4))[0], 0x1111)

    def test_a_swap_is_undone(self):
        import struct
        from fakes import FakeLive
        with FakeLive() as fake:
            target, holder, data_off = self.setup(fake)
            entry = fake.live.journal.add({'kind': 'script_hot_swap', 'undo': {
                'op': 'swap_script', 'target': '0x%08X' % target, 'holder': '0x%08X' % holder,
                'data': '0x00002222'}})     # before the swap the target held 0x2222; now it holds 0x1111
            r = fake.live.undo(journal_id=entry['id'])['results'][0]
            self.assertTrue(r['undone'])
            self.assertEqual(struct.unpack('<I', fake.link.get(target + data_off, 4))[0], 0x2222)

    def test_the_hot_swap_records_the_targets_code(self):
        import inspect
        from geck_mcp import fnv_data
        source = inspect.getsource(fnv_data)
        self.assertIn("'data': '0x%08X' % old_data", source)


class Batch2ReviewTests(unittest.TestCase):
    """S7 batch-2 review I1-I3."""

    def test_i1_capture_without_a_mode_is_act(self):
        fn = tool_source('fnv_warnings')
        text = ast.unparse(fn)
        self.assertIn("mode == 'status'", text)
        self.assertNotIn("mode in ('start', 'stop')", text)

    def test_i2_the_index_version_rebuilds_old_two_copy_indexes(self):
        from geck_mcp import fnv_data
        self.assertGreaterEqual(fnv_data.INDEX_VERSION, 3)

    def test_i3_a_half_done_undo_is_repaired_by_its_retry(self):
        import struct
        from fakes import FakeLive
        from geck_mcp.fnv_data import SCRIPT_DATA, SCRIPT_HEADER, SCRIPT_REFS, SCRIPT_VARS
        spans = [(SCRIPT_HEADER, 0x10), (SCRIPT_DATA, 4), (SCRIPT_REFS, 8), (SCRIPT_VARS, 8)]
        target, holder = 0x9000, 0x9800

        def put(base, fill):
            for off, n in spans:
                fake.link.put(base + off, bytes([fill]) * n)

        def get(base):
            return [fake.link.get(base + off, n) for off, n in spans]
        with FakeLive() as fake:
            put(target, 0x11)
            put(holder, 0x22)
            before = {'target': [b.hex() for b in get(target)], 'holder': [b.hex() for b in get(holder)]}
            # after the swap, then an undo that wrote header and data but not refs and vars
            put(target, 0x22)
            put(holder, 0x11)
            fake.link.put(target + SCRIPT_HEADER, b'\x11' * 0x10)
            fake.link.put(target + SCRIPT_DATA, b'\x11' * 4)
            entry = fake.live.journal.add({'kind': 'script_hot_swap', 'undo': {
                'op': 'swap_script', 'target': '0x%08X' % target, 'holder': '0x%08X' % holder,
                'data': '0x' + before['target'][1], 'spans': before}})
            r = fake.live.undo(journal_id=entry['id'])['results'][0]
            self.assertTrue(r['undone'], r)
            self.assertEqual([b.hex() for b in get(target)], before['target'])
            self.assertEqual([b.hex() for b in get(holder)], before['holder'])


class FlowStaleStopTests(unittest.TestCase):
    """K4: fnv_flow trace took an old parked stop of a cleared break with the same bp id as its own hit."""

    def flow(self, stops):
        from geck_mcp.fnv_flow import FlowTools
        flow = FlowTools.__new__(FlowTools)
        flow.dbg = SimpleNamespace(_stops=lambda: list(stops))
        times = iter(range(1000))
        flow.clock = lambda: next(times) * 0.05
        flow.sleep = lambda s: None
        return flow

    def test_an_old_stop_of_the_same_id_is_not_taken(self):
        old = {'kind': 'bp', 'id': 0, 'seq': 5, 'eip': '0x00401000'}
        flow = self.flow([old])
        self.assertIsNone(flow._await_bp(0, 1.0, addr=0x00402000, seen={5}))

    def test_the_new_hit_at_the_function_is_taken(self):
        old = {'kind': 'bp', 'id': 0, 'seq': 5, 'eip': '0x00401000'}
        new = {'kind': 'bp', 'id': 0, 'seq': 9, 'eip': '0x00402000'}
        self.assertEqual(self.flow([old, new])._await_bp(0, 1.0, addr=0x00402000, seen={5})['seq'], 9)

    def test_a_free_id_skips_ids_parked_stops_hold(self):
        from geck_mcp.fnv_debugger import Debugger
        dbg = Debugger.__new__(Debugger)
        dbg.breaks = {}
        dbg.live = SimpleNamespace(link=SimpleNamespace(one=lambda *a: {'bps': []}))
        dbg._stops = lambda: [{'kind': 'bp', 'id': 0, 'seq': 5}]
        self.assertEqual(dbg._free_bp_id(), 1)


if __name__ == '__main__':
    unittest.main()
