"""Release plan Task 5.2: when the decomp's compare module cannot be imported, the symbol database build answers
NO_DECOMP_TOOLS up front and says what is missing, instead of falling back unit by unit."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine_map import symdb  # noqa: E402


def checkout(folder, compare=None):
    root = Path(folder) / 'decomp'
    (root / 'build').mkdir(parents=True)
    (root / 'build' / 'units.json').write_text('[]', encoding='utf-8')
    if compare is not None:
        (root / 'tools').mkdir()
        (root / 'tools' / 'compare.py').write_text(compare, encoding='utf-8')
    image = Path(folder) / 'FalloutNV.exe'
    image.write_bytes(b'MZ')
    return root, image


def run(argv):
    err, out = io.StringIO(), io.StringIO()
    with redirect_stderr(err), redirect_stdout(out):
        code = symdb.main(argv)
    return code, err.getvalue() + out.getvalue()


class NoDecompToolsTests(unittest.TestCase):
    def test_no_compare_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, image = checkout(tmp)
            code, text = run(['--decomp', str(root), '--out', str(Path(tmp) / 's.sqlite'), '--image', str(image)])
        self.assertEqual(code, 2)
        self.assertIn('NO_DECOMP_TOOLS', text)
        self.assertIn('compare.py', text)

    def test_a_compare_module_that_does_not_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, image = checkout(tmp, 'import a_module_nobody_has\n')
            self.assertIn('a_module_nobody_has', symdb.decomp_tools_missing(root))

    def test_a_compare_module_without_the_functions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, image = checkout(tmp, 'x = 1\n')
            self.assertIn('compare, Pe', symdb.decomp_tools_missing(root))

    def test_a_good_compare_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, image = checkout(tmp, 'def compare(*a): return []\nclass Pe: pass\n')
            self.assertIsNone(symdb.decomp_tools_missing(root))


if __name__ == '__main__':
    unittest.main()
