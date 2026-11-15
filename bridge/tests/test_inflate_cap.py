"""Release plan Phase 7 (L4): compressed plugin records and BSA files inflate to at most the size they declare,
and a declared size above MAX_INFLATE is refused, so a small crafted file cannot fill memory."""
import re
import struct
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp.esp import codec  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1] / 'geck_mcp'
BOMB = zlib.compress(b'\0' * 5_000_000, 9)          # about 5 KB that inflates to 5 MB


class InflateTests(unittest.TestCase):
    def test_inflates_within_the_declared_size(self):
        data = b'hello' * 100
        self.assertEqual(codec.inflate(zlib.compress(data), len(data)), data)

    def test_more_than_declared_is_refused(self):
        with self.assertRaises(zlib.error):
            codec.inflate(BOMB, 100)

    def test_a_declared_size_above_the_cap_is_refused(self):
        with self.assertRaises(zlib.error):
            codec.inflate(BOMB, codec.MAX_INFLATE + 1)

    def test_a_truncated_stream_is_refused(self):
        with self.assertRaises(zlib.error):
            codec.inflate(zlib.compress(bytes(range(256)) * 4)[:20], 1024)

    def test_a_record_bomb_is_a_format_error(self):
        with self.assertRaises(codec.FormatError):
            codec.decompress(struct.pack('<I', 100) + BOMB)

    def test_no_uncapped_decompress_left(self):
        # every reader goes through codec.inflate; a bounded decompressobj().decompress(data, n) is fine too
        found = []
        for path in PACKAGE.rglob('*.py'):
            for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if re.search(r'zlib\.decompress\(', line):
                    found.append('%s:%d' % (path.relative_to(PACKAGE), n))
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
