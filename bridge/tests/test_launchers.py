"""The helper command line: Windows only (decision D5), the helper .exe runs directly."""
from pathlib import Path
import tempfile
import unittest

from geck_mcp.config import Project


def project(**extra):
    root = Path(tempfile.mkdtemp())
    base = dict(project_id='t', game_root=root / 'game', plugin='T.esp', state_dir=root / 'state',
                helper=root / 'Probe.Mcp.exe', spec=root / 't.spec.json')
    base.update(extra)
    return Project(**base), root


class LauncherTests(unittest.TestCase):
    def test_native_runs_helper_directly(self):
        p, root = project(launcher='native')
        self.assertEqual(p.helper_command('serve'), [str(root / 'Probe.Mcp.exe'), 'serve'])
        self.assertEqual(p.helper_env(), {})
        self.assertEqual(p.lock_root, root / 'game')

    def test_native_is_the_default(self):
        p, _ = project()
        self.assertEqual(p.launcher, 'native')

    def test_wine_and_crossover_are_refused(self):
        for extra in ({'launcher': 'crossover'}, {'launcher': 'wine'}, {'wine': Path('/cx/wine')},
                      {'bottle': Path('/b/Steam')}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                project(**extra)


if __name__ == '__main__':
    unittest.main()
