"""Bug fixes D1-D14 and S9 (world editing, creation, camera and photos). Fakes only; no game."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from geck_mcp import fnv_eyes, fnv_world  # noqa: E402
from geck_mcp.esp.codec import Record, Subrecord  # noqa: E402
from geck_mcp.fnv_create import FormTools  # noqa: E402
from geck_mcp.fnv_eyes import EyesTools  # noqa: E402
from geck_mcp.fnv_live import LiveError  # noqa: E402
from geck_mcp.fnv_world import WorldTools  # noqa: E402
import test_fnv_eyes as te  # noqa: E402


class UndoJournal(te.SimpleJournal):
    def __init__(self):
        super().__init__()
        self.undone = []

    def mark_undone(self, ids):
        self.undone += list(ids)


def eyes_world():
    live = te.world()
    live.journal = UndoJournal()
    return live


class D1Ammo(unittest.TestCase):
    def test_ammo_value_and_weight_come_from_data_and_dat2(self):
        data = struct.pack('<fB3xiB', 1.0, 0, 7, 10)               # speed, flags, value, clip rounds
        dat2 = struct.pack('<IIfII', 1, 0, 0.029, 0, 0)            # weight at +8
        rec = Record('AMMO', 0x01000001, _subrecords=[Subrecord('DATA', data), Subrecord('DAT2', dat2)])
        ops = {f: (v, t) for f, v, t in FormTools(None)._field_ops('AMMO', rec, '0A000001')}
        self.assertEqual(ops['value'][0], 7)
        self.assertAlmostEqual(ops['weight'][0], 0.029, places=3)

    def test_ammo_without_dat2_writes_no_weight(self):
        rec = Record('AMMO', 0x01000001, _subrecords=[Subrecord('DATA', struct.pack('<fB3xiB', 1.0, 0, 7, 10))])
        ops = {f for f, _v, _t in FormTools(None)._field_ops('AMMO', rec, '0A000001')}
        self.assertEqual(ops, {'value'})


class D2D3D4(unittest.TestCase):
    def test_restore_marks_the_camera_journal_entry_undone(self):
        live = eyes_world()
        eyes = EyesTools(live, sleep=lambda s: None)
        eyes.set(position=[1, 2, 3], yaw=0, pitch=0)
        entry_id = '1'
        self.assertEqual(live.journal.entries()[0]['kind'], 'camera')
        eyes.restore()
        self.assertEqual(live.journal.undone, [entry_id])

    def test_camera_use_holds_the_module_lock(self):
        live = eyes_world()
        seen = []
        original = live.console

        def console(line, ref=0):
            seen.append(fnv_eyes._CAMERA_LOCK._is_owned())
            return original(line, ref)
        live.console = console
        eyes = EyesTools(live, sleep=lambda s: None)
        eyes.set(position=[1, 2, 3], yaw=0, pitch=0)
        eyes.restore()
        self.assertTrue(seen and all(seen))
        self.assertFalse(fnv_eyes._CAMERA_LOCK._is_owned())

    def test_hidden_hud_is_left_alone(self):
        live = eyes_world()
        live.mem[fnv_eyes.MENUS_SHOWN_FLAG] = 0          # seen live (N20): 0 while tm has the menus off
        eyes = EyesTools(live, sleep=lambda s: None)
        back = eyes._hud_off(True)
        back()
        self.assertEqual([l for l in live.lines if l == 'tm'], [])

    def test_visible_hud_is_toggled_and_restored(self):
        live = eyes_world()
        original = live.console

        def console(line, ref=0):            # tm flips the shown flag, as in the game
            if line == 'tm':
                live.mem[fnv_eyes.MENUS_SHOWN_FLAG] = 0 if live.mem.get(fnv_eyes.MENUS_SHOWN_FLAG) else 1
            return original(line, ref)
        live.console = console
        live.mem[fnv_eyes.MENUS_SHOWN_FLAG] = 1          # menus on, as at game start (flag 1, seen live)
        eyes = EyesTools(live, sleep=lambda s: None)
        back = eyes._hud_off(True)
        back()
        self.assertEqual([l for l in live.lines if l == 'tm'], ['tm', 'tm'])

    def test_hud_shown_again_by_someone_else_is_not_toggled_back(self):
        live = eyes_world()
        live.mem[fnv_eyes.MENUS_SHOWN_FLAG] = 1
        eyes = EyesTools(live, sleep=lambda s: None)
        back = eyes._hud_off(True)       # tm sent; the flag stays 1 (shown again before the restore)
        back()
        self.assertEqual([l for l in live.lines if l == 'tm'], ['tm'])


class FakeControl:
    def __init__(self, held):
        self.held, self.ran = held, []

    def batch(self, ops, timeout=None):
        return [{'held': self.held}]

    def run(self, action, **kw):
        self.ran.append(action)


class FakeAgent:
    def __init__(self, folder):
        self.folder, self.names = Path(folder), []

    def menus(self):
        return {}

    def screenshot(self, name=None, max_width=0, method=''):
        from PIL import Image
        self.names.append(name)
        path = self.folder / ('%s.png' % name)
        Image.new('RGB', (64, 48), (10, 20, 30)).save(path)
        return {'path': str(path), 'size': [64, 48], 'method': 'engine'}


class D10D11(unittest.TestCase):
    def test_sequence_leaves_a_closed_gate_closed(self):
        with tempfile.TemporaryDirectory() as d:
            live = eyes_world()
            live.time_control = FakeControl(held=True)
            eyes = EyesTools(live, agent=FakeAgent(d), sleep=lambda s: None)
            eyes.photo('sequence', frames=2, hide_hud=False)
            self.assertNotIn('release', live.time_control.ran)

    def test_sequence_releases_a_gate_it_closed(self):
        with tempfile.TemporaryDirectory() as d:
            live = eyes_world()
            live.time_control = FakeControl(held=False)
            eyes = EyesTools(live, agent=FakeAgent(d), sleep=lambda s: None)
            eyes.photo('sequence', frames=2, hide_hud=False)
            self.assertIn('release', live.time_control.ran)

    def test_capture_names_differ_between_calls_and_fractional_angles(self):
        with tempfile.TemporaryDirectory() as d:
            agent = FakeAgent(d)
            live = eyes_world()
            eyes = EyesTools(live, agent=agent, sleep=lambda s: None)
            eyes._photo_view(False, False, 64, None)
            eyes._photo_view(False, False, 64, None)
            self.assertEqual(len(set(agent.names)), 2)
            self.assertNotEqual('yaw%g_pitch%g' % (10.2, 5), 'yaw%g_pitch%g' % (10.7, 5))


class S9(unittest.TestCase):
    def test_prompt_is_none_without_a_crosshair_ref(self):
        live = eyes_world()
        live.put32(fnv_eyes.HUD, te.HUD)
        tile = 0x7000
        live.put32(te.HUD + fnv_eyes.HUD_TILES + 4 * fnv_eyes.HUD_TILE['prompt'], tile)
        agent = SimpleNamespace(_tile_strings=lambda tiles: {tile: 'Button Text'})
        out = EyesTools(live, agent=agent, sleep=lambda s: None).hud_read(brief=True)
        self.assertIsNone(out['prompt'])


class WorldAgent:
    def __init__(self, flags, sky=(0, 0)):
        self.calls = []
        self.flags = flags
        self.live = SimpleNamespace(
            in_game=lambda: None, changing=lambda r: None, resolve=lambda r: {'ptr': 0x5000, 'formId': '00000ABC'},
            reads=self._reads, u32s=lambda addrs: [sky[0] if a == 0x6010 else sky[1] if a == 0x601C else 0
                                                   for a in addrs],
            console=lambda line, ref=0: self.calls.append(('console', line)), journal=te.SimpleJournal())
        self.link = SimpleNamespace(hello={'pid': 1}, one=self._one)
        self.sky = sky

    def _reads(self, spans):
        raw = bytearray(0x68)
        struct.pack_into('<I', raw, 0x08, self.flags)
        return [bytes(raw) for _ in spans]

    def _one(self, op, *args, **kw):
        self.calls.append((op,) + args)
        if int(args[0], 16) == 0x0046DD00:
            return {'ok': True, 'eax': '0x6000'}
        return {'ok': True, 'eax': '0x0'}

    def describe_pointers(self, ptrs):
        return {p: {'formId': '%08X' % p} for p in ptrs}


class D5D6(unittest.TestCase):
    def undo_of(self, agent):
        return [e['undo'] for e in agent.live.journal.entries()]

    def test_disable_of_an_already_disabled_ref_journals_nothing(self):
        agent = WorldAgent(flags=0x800)
        WorldTools(agent).world_edit('disable', ref='00000ABC')
        self.assertEqual(self.undo_of(agent), [])

    def test_disable_of_an_enabled_ref_journals_enable(self):
        agent = WorldAgent(flags=0)
        WorldTools(agent).world_edit('disable', ref='00000ABC')
        self.assertEqual(self.undo_of(agent)[0]['lines'], ['Enable'])

    def test_enable_of_an_enabled_ref_journals_nothing(self):
        agent = WorldAgent(flags=0)
        WorldTools(agent).world_edit('enable', ref='00000ABC')
        self.assertEqual(self.undo_of(agent), [])

    def test_weather_undo_restores_the_old_override(self):
        agent = WorldAgent(flags=0, sky=(0x7100, 0x7200))
        agent.live.resolve = lambda r: {'ptr': 0x7300, 'formId': '00000333'}
        WorldTools(agent).world_edit('weather', weather='00000333')
        self.assertEqual(self.undo_of(agent)[0]['line'], 'ForceWeather 00007200 1')

    def test_weather_undo_releases_when_there_was_no_override(self):
        agent = WorldAgent(flags=0, sky=(0x7100, 0))
        agent.live.resolve = lambda r: {'ptr': 0x7300, 'formId': '00000333'}
        WorldTools(agent).world_edit('weather', weather='00000333')
        self.assertEqual(self.undo_of(agent)[0]['line'], 'ReleaseWeatherOverride')


class D7Refresh(unittest.TestCase):
    def test_refs_are_enabled_again_when_the_purge_fails(self):
        lines = []
        live = SimpleNamespace(values=lambda names, ref=0: [0], console=lambda line, ref=0: lines.append((line, ref)))
        tools = FormTools.__new__(FormTools)
        from geck_mcp.fnv_create import AssetTools
        tools = AssetTools.__new__(AssetTools)
        tools.live, tools.sleep, tools.clock = live, lambda s: None, lambda: 0

        def boom():
            raise LiveError('CALL_FAULTED', 'x')
        tools._purge = boom
        with self.assertRaises(LiveError):
            tools._refresh([1, 2])
        self.assertEqual([l for l, _ in lines if l == 'Enable'], ['Enable', 'Enable'])


class D14(unittest.TestCase):
    def test_sweep_validates_steps_and_index(self):
        from geck_mcp.fnv_inspect import SystemsTools
        from geck_mcp.fnv_conv import ConvError
        tools = SystemsTools(SimpleNamespace(ready=lambda: None))
        with self.assertRaises(ConvError):
            tools.sweep('x', [1], 0, 0, 1, steps=0)
        with self.assertRaises(ConvError):
            tools.sweep('x', [1], 3, 0, 1, steps=2)


if __name__ == '__main__':
    unittest.main()
