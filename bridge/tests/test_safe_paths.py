"""Release plan Task 1.1 and 1.5 (audit B1, H5): the one check for names and paths."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp.safe_paths import check_includes, inside, no_unc, plain_name, read_roots, write_roots  # noqa: E402

BAD = ['', '..', '.', 'a/b', 'a\\b', '..\\..\\decoy', 'C:\\Windows', 'a:b', 'x.', 'x ', ' x', '-x', 'CON',
       'nul.txt', 'COM1', 'a' * 61, 'a\nb', 'a..b', 'lpt9.log', 'Aux']
GOOD = ['CoolWorld', 'Cool World', 'my-mod_1.2', 'A', 'a' * 60, 'CONSOLE', 'COM10']


class PlainNameTests(unittest.TestCase):
    def test_plain_name_refuses(self):
        for name in BAD:
            with self.subTest(name=name), self.assertRaises(ValueError):
                plain_name(name)

    def test_plain_name_accepts(self):
        for name in GOOD:
            with self.subTest(name=name):
                self.assertEqual(plain_name(name), name)

    def test_refusal_names_the_argument(self):
        with self.assertRaisesRegex(ValueError, 'mod_name'):
            plain_name('..', 'mod_name')

    def test_not_a_string_is_refused(self):
        with self.assertRaises(ValueError):
            plain_name(None)


class PathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'
        (self.state / 'sub').mkdir(parents=True)
        (self.state / 'sub' / 'a.txt').write_text('x', encoding='utf-8')
        self.outside = self.root / 'outside'
        self.outside.mkdir()

    def test_unc_is_refused_without_touching_the_file_system(self):
        def boom(*a, **k):
            raise AssertionError('the file system was touched')
        with mock.patch.object(Path, 'resolve', boom), mock.patch.object(Path, 'exists', boom), \
                mock.patch.object(os.path, 'realpath', boom):
            for path in ['//server/share/x', '\\\\server\\share\\x', '\\/server/x', '\\\\?\\UNC\\server\\x',
                         Path('//server/share/x')]:
                with self.subTest(path=path):
                    with self.assertRaises(ValueError):
                        no_unc(path)
                    with self.assertRaises(ValueError):
                        inside(path, [self.state])

    def test_no_unc_accepts_local_paths(self):
        self.assertEqual(no_unc('C:/x/y.txt'), Path('C:/x/y.txt'))
        self.assertEqual(no_unc('rel\\y.txt'), Path('rel/y.txt'))

    def test_dotdot_out_of_the_root_is_refused(self):
        with self.assertRaises(ValueError):
            inside(str(self.state) + '/../outside', [self.state])
        with self.assertRaises(ValueError):
            inside(self.outside / 'f.txt', [self.state])

    def test_forward_slashes_and_case_inside_are_accepted(self):
        mixed = str(self.state / 'sub' / 'a.txt').replace('\\', '/')
        if os.name == 'nt':
            mixed = mixed.upper()
        got = inside(mixed, [self.state])
        self.assertEqual(str(got).lower(), str(self.state / 'sub' / 'a.txt').lower())

    def test_a_root_itself_and_a_second_root(self):
        self.assertEqual(inside(self.outside, [self.state, self.outside]), self.outside)

    def test_no_roots_refuses(self):
        with self.assertRaises(ValueError):
            inside(self.state / 'sub', [])

    def test_a_junction_inside_that_points_outside_is_refused(self):
        link = self.state / 'link'
        try:
            import _winapi
            _winapi.CreateJunction(str(self.outside), str(link))
        except (ImportError, AttributeError, OSError):
            try:
                os.symlink(self.outside, link, target_is_directory=True)
            except OSError:
                self.skipTest('cannot make a junction or symlink here')
        with self.assertRaises(ValueError):
            inside(link / 'f.txt', [self.state])

    def test_roots_leave_out_missing_settings(self):
        project = SimpleNamespace(state_dir=self.state, game_root=self.root / 'game', fnv=None)
        self.assertEqual(write_roots(project), [self.state])
        self.assertIn(self.root / 'game', read_roots(project))
        fnv = SimpleNamespace(mo2_root=self.root / 'mo2', tests_dir=None, decomp_root=None, crash_dirs=[self.outside])
        roots = read_roots(SimpleNamespace(state_dir=self.state, game_root=self.root / 'game', fnv=fnv))
        self.assertIn(self.root / 'mo2', roots)
        self.assertIn(self.outside, roots)
        self.assertNotIn(None, roots)


class IncludeTests(unittest.TestCase):
    BAD = ['#include "C:/x"', '#include "../x"', '#include NAME', '#include "/etc/x"', '#include "\\\\srv\\x.h"',
           '#include "a/../../x.h"', '  #  include "..\\x.h"', '#include_next "../x"', '#import "../x"',
           '#embed "../secret"', '#/* c */include "../x"', '#inc\\\nlude "../x"', '??=include "../x"',
           '/*\n*/#include "../x"', '#include <C:/x>', '#include', '#include "x.h" junk']
    GOOD = ['#include "fnvmcp_module.h"', '#include <string.h>', '#include <sys/types.h>', '  # include "a_b.h"',
            '#include "fnv_engine.h" // the map', 'int x; /* #include "../x" */',
            'const char *s = "#include \\"../x\\"";', '// #include "../x"', "char c = '#';"]

    def test_bad_includes_are_refused(self):
        for source in self.BAD:
            with self.subTest(source=source), self.assertRaises(ValueError):
                check_includes('int a;\n' + source + '\nint b;\n')

    def test_good_includes_are_accepted(self):
        for source in self.GOOD:
            with self.subTest(source=source):
                self.assertIsNone(check_includes('int a;\n' + source + '\nint b;\n'))


if __name__ == '__main__':
    unittest.main()
