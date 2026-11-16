"""Regression tests for the 2026-11-12 bug log: server core (A), offline data (C) and the
stress-test findings (S) that can be checked offline."""
import io
import json
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_live, fnv_save, supervisor, service  # noqa: E402
from geck_mcp.esp import codec, formids  # noqa: E402
from geck_mcp.fnv_data import RecordIndex  # noqa: E402
from geck_mcp.fnv_state import GameIdentity  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def tes4_plugin(path, records, masters=()):
    """A minimal plugin: a TES4 header (with MAST entries) and one flat list of records with EDIDs."""
    header = b''
    for m in masters:
        data = m.encode('cp1252') + b'\0'
        header += b'MAST' + struct.pack('<H', len(data)) + data
        header += b'DATA' + struct.pack('<H', 8) + b'\0' * 8
    body = b'TES4' + struct.pack('<IIII', len(header), 0, 0, 0) + struct.pack('<I', 0) + header
    for form_id, rtype, edid in records:
        sub = edid.encode('cp1252') + b'\0'
        rec_body = b'EDID' + struct.pack('<H', len(sub)) + sub
        body += rtype.encode() + struct.pack('<III', len(rec_body), 0, form_id) + b'\0' * 8 + rec_body
    Path(path).write_bytes(body)


class JournalTests(unittest.TestCase):
    """A1, A3, A7: the act journal under parallel calls, and undo across game processes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / 'journal.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    def test_parallel_adds_and_marks_keep_every_entry(self):
        journal = fnv_live.Journal(self.path)
        big = 'x' * 9000  # entries over 8 KB tore the file before

        def adds(k):
            for i in range(150):
                journal.add({'kind': 'test', 'n': (k, i), 'pad': big if i % 10 == 0 else ''})

        def marks():
            for _ in range(30):
                ids = [e['id'] for e in journal.entries()[:5]]
                journal.mark_undone(ids)

        threads = [threading.Thread(target=adds, args=(k,)) for k in range(4)] + [threading.Thread(target=marks)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entries = fnv_live.Journal(self.path).entries()
        self.assertEqual(len(entries), 600)
        self.assertEqual(len({tuple(e['n']) for e in entries}), 600)

    def test_entries_are_stamped_with_the_game_process(self):
        journal = fnv_live.Journal(self.path, game=lambda: {'pid': 42, 'started': 7})
        entry = journal.add({'kind': 'test', 'undo': {'op': 'console', 'line': 'x'}})
        self.assertEqual((entry['pid'], entry['started']), (42, '7'))   # stored as the companion sends it: text
        self.assertEqual(fnv_live.Journal(self.path, game=lambda: None).add({'kind': 't'}).get('pid'), None)

    def _live(self, hello):
        live = fnv_live.LiveService(SimpleNamespace(state_dir=self.tmp.name))
        live._game = SimpleNamespace(link=SimpleNamespace(hello=hello, ping=lambda: hello))
        return live

    def test_undo_skips_entries_of_other_or_unknown_processes(self):
        live = self._live({'pid': 5, 'started': 100})
        same = live.journal.add({'kind': 'a', 'undo': {'op': 'console', 'line': 'one'}})
        # the identity is the journal's to stamp (R19): an earlier process, and no known game (an orphan)
        live.journal.add({'kind': 'a', 'undo': {'op': 'console', 'line': 'old'}}, GameIdentity(5, 99))
        live.journal.add({'kind': 'a', 'undo': {'op': 'console', 'line': 'nopid'}}, None)
        ran = []
        live.console = lambda line, ref=0: ran.append(line) or {'accepted': True, 'output': []}
        live.ready = lambda: None
        out = live.revert_all()
        self.assertEqual(ran, ['one'])
        self.assertEqual(out['reverted'], 1)
        self.assertTrue(next(e for e in live.journal.entries() if e['id'] == same['id'])['undone'])

    def test_a_failing_undo_does_not_lose_the_ones_already_applied(self):
        live = self._live({'pid': 5, 'started': 100})
        for line in ('first', 'boom', 'last'):
            live.journal.add({'kind': 'a', 'undo': {'op': 'console', 'line': line}})
        ran = []

        def console(line, ref=0):
            if line == 'boom':
                raise fnv_live.LiveError('COMPANION_TIMEOUT', 'no reply')
            ran.append(line)
            return {'accepted': True, 'output': []}
        live.console, live.ready = console, (lambda: None)
        out = live.revert_all()
        self.assertEqual(ran, ['last', 'first'])
        self.assertEqual([r['undone'] for r in out['results']], [True, False, True])
        self.assertEqual(out['results'][1]['error']['code'], 'COMPANION_TIMEOUT')
        undone = {e['undo']['line']: e['undone'] for e in live.journal.entries()}
        self.assertEqual(undone, {'first': True, 'boom': False, 'last': True})

    def test_one_game_object_under_parallel_first_use(self):
        live = fnv_live.LiveService(SimpleNamespace(state_dir=self.tmp.name))
        made = []
        real = fnv_live.Game

        class SlowGame:
            def __init__(self, project):
                time.sleep(0.05)
                made.append(self)
        fnv_live.Game = SlowGame
        try:
            threads = [threading.Thread(target=lambda: live.game) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            fnv_live.Game = real
        self.assertEqual(len(made), 1)


class FakeChild:
    def __init__(self):
        self.alive, self.sent = True, []
        self.proc = SimpleNamespace(pid=1, poll=lambda: 1)

    def send(self, line):
        self.sent.append(line)


class SupervisorTests(unittest.TestCase):
    """A5, A10, S6."""

    def make(self):
        out = io.BytesIO()
        sup = supervisor.Supervisor(['python'], [], out=out, inp=io.BytesIO())
        return sup, out

    def test_cancel_clears_the_call(self):
        sup, _ = self.make()
        sup.child = FakeChild()
        sup.code = 'x'
        sup.from_client(json.dumps({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {}}))
        self.assertIn('5', sup.in_flight)
        sup.from_client(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                                    'params': {'requestId': 5}}))
        self.assertEqual(sup.in_flight, {})
        self.assertEqual(len(sup.child.sent), 2)  # the cancel still reaches the server

    def test_calls_of_a_replaced_child_still_fail(self):
        sup, out = self.make()
        old, new = FakeChild(), FakeChild()
        sup.child = new
        sup.in_flight = {'7': old, '8': new}
        sup.child_exited(old)
        self.assertEqual(list(sup.in_flight), ['8'])
        self.assertIs(sup.child, new)
        reply = json.loads(out.getvalue().decode().strip())
        self.assertEqual((reply['id'], reply['error']['code']), (7, -32603))

    def test_log_goes_to_the_projects_state_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'st'
            state.mkdir()
            project = Path(tmp) / 'p.json'
            project.write_text(json.dumps({'state_dir': 'st'}), encoding='utf-8')
            saved = supervisor.os.environ.pop('GECK_SUPERVISOR_LOG', None)
            try:
                # resolved on both sides: a temp folder can have an 8.3 short name (Windows CI) or a symlink (macOS)
                self.assertEqual(supervisor.log_path(ROOT, project).resolve(), (state / 'supervisor.log').resolve())
                supervisor.os.environ['GECK_SUPERVISOR_LOG'] = ''
                self.assertIsNone(supervisor.log_path(ROOT, project))
            finally:
                supervisor.os.environ.pop('GECK_SUPERVISOR_LOG', None)
                if saved is not None:
                    supervisor.os.environ['GECK_SUPERVISOR_LOG'] = saved


class ServiceLockTests(unittest.TestCase):
    """A4: threads of one process queue for the editor lock instead of failing at once."""

    def test_second_thread_waits_for_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []

            def hold():
                with service.process_lock(tmp):
                    order.append('a in')
                    time.sleep(0.2)
                    order.append('a out')
            t = threading.Thread(target=hold)
            t.start()
            time.sleep(0.05)
            with service.process_lock(tmp, wait=5):
                order.append('b in')
            t.join()
            self.assertEqual(order, ['a in', 'a out', 'b in'])

    def test_busy_is_retry_safe(self):
        svc = service.Service.__new__(service.Service)
        svc.project = SimpleNamespace(project_id='p')
        result = svc.failure(RuntimeError('EDITOR_BUSY: another bridge operation holds the bottle lock'), 'op', 'x', True)
        self.assertEqual(result['error']['code'], 'EDITOR_BUSY')
        self.assertTrue(result['error']['retrySafe'])
        self.assertEqual(result['outcome'], 'failed_before_change')


class RecordIndexTests(unittest.TestCase):
    """C1, C2, C3."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.tmp.name)
        self.plugin = self.dir / 'Mine.esp'
        tes4_plugin(self.plugin, [(0x000800, 'STAT', 'MyRock')])

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_rebuild_rolls_back_and_unlocks(self):
        index = RecordIndex(self.dir)
        first = index.ensure(self.plugin)
        self.plugin.write_bytes(b'NOPE not a plugin')
        with self.assertRaises(Exception):
            index.ensure(self.plugin)  # the scan fails before any write
        tes4_plugin(self.plugin, [(0x000800, 'STAT', 'MyRock'), (0x000801, 'STAT', 'Other')])

        def broken(db, value):
            raise RuntimeError('insert failed')
        index._set_last_id = broken  # a failure inside the transaction, after the delete
        with self.assertRaises(RuntimeError):
            index.ensure(self.plugin)
        del index._set_last_id
        self.assertFalse(index.db.in_transaction)
        other = RecordIndex(self.dir)
        result = []
        t = threading.Thread(target=lambda: result.append(
            other.db.execute('select id from files where id = ?', (first,)).fetchone()))
        t.start()
        t.join()
        self.assertEqual(result, [(first,)])  # the old row survived the failed rebuild
        # another thread can write at once (no stale write lock)
        t = threading.Thread(target=lambda: (other.db.execute("insert into files(path) values ('z')"),
                                             other.db.commit()))
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive())

    def test_parallel_first_use_indexes_once(self):
        index = RecordIndex(self.dir)
        ids, errors = [], []

        def go():
            try:
                ids.append(index.ensure(self.plugin))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 1)

    def test_rebuilt_plugin_gets_a_new_id_and_drops_its_refs(self):
        index = RecordIndex(self.dir)
        first = index.ensure(self.plugin)
        index.db.executescript('create table if not exists ref_files(file integer primary key, version integer, '
                               'built real, refs integer); create table if not exists refs(file integer, from_owner '
                               'text, from_local integer, from_type text, offset integer, tag text, to_owner text, '
                               'to_local integer);')
        index.db.execute('insert into ref_files values (?, 3, 0, 1)', (first,))
        index.db.execute("insert into refs values (?, 'mine.esp', 1, 'REFR', 0, 'NAME', 'mine.esp', 2048)", (first,))
        index.db.commit()
        time.sleep(0.02)
        tes4_plugin(self.plugin, [(0x000800, 'STAT', 'MyRock'), (0x000801, 'STAT', 'MyRock2')])
        second = index.ensure(self.plugin)
        self.assertNotEqual(first, second)
        self.assertEqual(index.db.execute('select count(*) from refs where file = ?', (first,)).fetchone()[0], 0)
        self.assertEqual(index.db.execute('select count(*) from ref_files where file = ?', (first,)).fetchone()[0], 0)


class SmallDataTests(unittest.TestCase):
    def test_codec_keeps_cp1252_undefined_bytes(self):  # C13
        text = codec.cstring(b'a\x81b\x9d\0')
        self.assertEqual(codec.zstring(text), b'a\x81b\x9d\0')

    def test_master_index_leaves_out_overrides(self):  # C6
        import gzip
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'DLC.esm.json.gz'
            with gzip.open(path, 'wt', encoding='utf-8') as f:
                json.dump({'master': 'DLC.esm', 'entries': {
                    'OwnThing': ['STAT', '01000801', None, None],
                    'VanillaEdit': ['STAT', '00012345', None, None]}}, f)
            index = formids.MasterIndex([path])
            self.assertEqual(index.lookup('ownthing')[0][2], 0x01000801)
            self.assertEqual(index.lookup('vanillaedit'), [])

    def test_truncated_save_is_a_format_error(self):  # C10
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cut.fos'
            path.write_bytes(fnv_save.MAGIC + struct.pack('<I', 500) + b'\0' * 20)
            with self.assertRaises(fnv_save.SaveFormatError):
                fnv_save.Save(path)
            buffer = fnv_save.Buffer(b'\x03\0\0\0|')
            with self.assertRaises(fnv_save.SaveFormatError):
                buffer.count()

    def test_reference_flags_after_spaced_bytes_decode(self):  # C9
        save = fnv_save.Save.__new__(fnv_save.Save)
        save.ids = [0x00001234]
        body = b'\0\0\x01' + b'\0' * 24 + b'|' + b'\x10|\x02|\x04|' + struct.pack('<I', 0x49) + b'|'
        out = save.decode({'type': 'REFR', 'flags': 0x5, 'form': 0x00012345, 'body': body, 'version': 0})
        self.assertEqual(out['formFlags'], '00000049')

    def test_moving_reference_flags_after_havok_blocks_decode(self):  # C9 (layout checked against the live game)
        save = fnv_save.Save.__new__(fnv_save.Save)
        save.ids = [0x00001234]
        initial = b'\0\0\x01' + b'\0' * 24 + b'|'
        # A bottle disabled mid-flight: state 78 03 04, linear velocity, angular velocity, then form flags 0x80B
        # (the value read from the reference in the game when the save was made).
        body = initial + b'\x78|\x03|\x04|' + struct.pack('<3f', 0, 0, 193.85) + b'|' + struct.pack('<3f', 0, 0, 0) + \
            b'|' + struct.pack('<I', 0x80B) + b'|'
        out = save.decode({'type': 'REFR', 'flags': 0x5, 'form': 0x000EEE0B, 'body': body, 'version': 0})
        self.assertEqual(out['formFlags'], '0000080B')
        self.assertEqual(out['extraBytes'], [0x78, 3, 4])
        self.assertEqual(len(out['havokBlocks']), 2)
        # Position and quaternion blocks (state 88), then one more byte (state 90) before the flags.
        body = initial + b'\x90|\x04|\x04|' + struct.pack('<3f', 1, 2, 3) + b'|' + struct.pack('<4f', 1, 0, 0, 0) + \
            b'|\x02|' + struct.pack('<I', 0x9) + b'|'
        out = save.decode({'type': 'REFR', 'flags': 0x5, 'form': 0x00012345, 'body': body, 'version': 0})
        self.assertEqual(out['formFlags'], '00000009')
        self.assertEqual(out['havokBlocks'][1], [1.0, 0.0, 0.0, 0.0])

    def test_projectile_flags_after_its_state_byte_decode(self):  # C9
        save = fnv_save.Save.__new__(fnv_save.Save)
        save.ids = []
        body = b'\xff|' + struct.pack('<I', 0x01020809) + b'|'
        out = save.decode({'type': 'PGRE', 'flags': 0x1, 'form': 0x00141195, 'body': body, 'version': 27})
        self.assertEqual(out['formFlags'], '01020809')
        # A plain reference without initial data reads its flags straight away.
        body = struct.pack('<I', 0x0809) + b'|'
        out = save.decode({'type': 'REFR', 'flags': 0x1, 'form': 0x00012345, 'body': body, 'version': 27})
        self.assertEqual(out['formFlags'], '00000809')
        self.assertNotIn('extraBytes', out)

    def test_project_file_with_bom(self):  # A13
        from geck_mcp.config import Project
        with tempfile.TemporaryDirectory() as tmp:
            src = ROOT / 'projects' / 'example-windows.json'
            text = src.read_text(encoding='utf-8')
            dst = Path(tmp) / 'p.json'
            dst.write_bytes(b'\xef\xbb\xbf' + text.encode('utf-8'))
            shutil.copy(src, Path(tmp) / 'plain.json')
            try:
                Project.load(Path(tmp) / 'plain.json')
            except Exception as exc:  # the example itself does not load here: nothing to compare
                self.skipTest('example project does not load in a temp folder: %s' % exc)
            self.assertEqual(Project.load(dst).project_id, Project.load(Path(tmp) / 'plain.json').project_id)


class ExitNoteTests(unittest.TestCase):
    """S4: an exit note older than the last launch means that game left no note (terminated from outside)."""

    def test_note_of_an_older_game_is_not_for_the_last_launch(self):
        import os
        from unittest import mock
        from geck_mcp import fnv_game
        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / 'fnvmcp'
            notes.mkdir()
            note = notes / 'exit-100.json'
            note.write_text(json.dumps({'pid': 100, 'how': 'console'}), encoding='utf-8')
            os.utime(note, (1000, 1000))
            game = fnv_game.Game.__new__(fnv_game.Game)
            game.project = SimpleNamespace(state_dir=tmp)
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': tmp}):
                self.assertFalse(game.last_exit(2000)['forLastLaunch'])
                self.assertTrue(game.last_exit(500)['forLastLaunch'])
                self.assertIsNone(game.last_exit()['forLastLaunch'])


class EngineNameTests(unittest.TestCase):
    """S8: Class::Method names from class proofs."""

    def test_qualified_names(self):
        from geck_mcp import fnv_engine
        from geck_mcp.config import Project
        try:
            project = Project.load(ROOT / 'projects' / 'hellowasteland.windows.json')
            engine = fnv_engine.load(*fnv_engine.default_paths(project))
        except Exception as exc:
            self.skipTest('no engine map here: %s' % exc)
        try:
            address = engine.resolve('Actor::GetPos')
        except fnv_engine.EngineMapError as exc:
            self.skipTest('this engine map has no Actor::GetPos: %s' % exc)
        self.assertIn(address, engine.functions)
        with self.assertRaises(fnv_engine.EngineMapError) as caught:
            engine.resolve('NoSuchClass::GetPos')
        self.assertTrue(caught.exception.evidence['candidates'])


if __name__ == '__main__':
    unittest.main()
