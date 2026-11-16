"""The record index is used from the MCP server's worker threads: a query on a thread other than the one that
opened the index must work (it failed with "SQLite objects created in a thread can only be used in that same
thread" when fnv_record action=find calls landed on different workers)."""
import tempfile
import threading
import unittest

from geck_mcp.fnv_data import RecordIndex


class RecordIndexThreadTests(unittest.TestCase):
    def test_query_from_other_threads(self):
        # Finished threads' connections close when collected; Windows may still hold the file at cleanup.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            index = RecordIndex(tmp)
            index.db.execute("insert into records(file, owner, local, type, edid) values (1, 'A.esm', 7, 'STAT', 'X')")
            index.db.commit()
            results, errors = [], []

            def query():
                try:
                    results.append(index.db.execute('select edid from records where local = 7').fetchone()[0])
                except Exception as exc:          # the failure this test guards against
                    errors.append(exc)

            threads = [threading.Thread(target=query) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(results, ['X'] * 4)


if __name__ == '__main__':
    unittest.main()
