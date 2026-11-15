"""Release plan Task 5.2: the fnv-mcp source download, against a disposable Git repository."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location(
    'package_release', Path(__file__).resolve().parents[2] / 'tools/package_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Test')
        self.git('config', 'user.email', 'test@example.invalid')
        for name, data in {
            'README.md': b'# Test',
            'CHANGELOG.md': b'# Changelog\n\n## fnv-mcp 0.3.0 - unreleased\n',
            'LICENSE': b'Synthetic licence',
            'bridge/pyproject.toml': b'[project]\nversion="0.3.0"\n',
            'bridge/geck_mcp/__init__.py': b'__version__="0.3.0"\n',
            'bridge/examples/fnv-tests/a.yaml': b'name: a\n',
            'bridge/examples/q/scripts/Q.gek': b'scn Q\n',
            'bridge/docs/BUG-LOG.md': b'internal',
            'bridge/docs/audit4/notes.md': b'internal',
            'bridge/native/runtime/companion.c': b'/* synthetic source */',
            'bridge/Probe.Mcp.exe': b'fake exe',
            'bridge/native/generated.dll': b'fake dll',
            'bridge/tests/fixtures/test.esp': b'fake plugin',
            'bridge/tests/test_build.py': b'fixture dependent',
            'bridge/projects/private.json': b'private profile',
            'bridge/projects/example-windows.json': b'{}',
            'bridge/state/raw.json': b'private evidence',
            'HANDOFF.md': b'coordination',
        }.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.git('add', '.')
        self.git('commit', '-qm', 'Synthetic source')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL)

    def test_archive_uses_committed_source_and_omits_private_and_binary_files(self):
        (self.repo / 'README.md').write_text('dirty working edit')
        (self.repo / 'bridge/untracked.py').write_text('not committed')
        output = self.root / 'one.zip'
        manifest = release.package(self.repo, 'HEAD', output)
        with zipfile.ZipFile(output) as z:
            self.assertIsNone(z.testzip())
            prefix = release.prefix('0.3.0')
            self.assertEqual(prefix, 'fnv-mcp-0.3.0/')
            self.assertEqual(z.read(prefix + 'README.md'), b'# Test')
            self.assertEqual(set(z.namelist()), {prefix + p for p in (
                'README.md', 'CHANGELOG.md', 'LICENSE', 'bridge/pyproject.toml', 'bridge/geck_mcp/__init__.py',
                'bridge/native/runtime/companion.c', 'bridge/projects/example-windows.json',
                'bridge/examples/fnv-tests/a.yaml', 'bridge/examples/q/scripts/Q.gek')})
        self.assertEqual(manifest['name'], 'fnv-mcp')
        self.assertEqual(manifest, json.loads(output.with_suffix('.json').read_text()))
        self.assertEqual(manifest['sourceCommit'], self.git('rev-parse', 'HEAD').decode().strip())

    def test_reproducible_and_refuses_overwrite(self):
        first, second = self.root / 'one.zip', self.root / 'two.zip'
        release.package(self.repo, 'HEAD', first)
        release.package(self.repo, 'HEAD', second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with self.assertRaises(FileExistsError):
            release.package(self.repo, 'HEAD', first)

    def test_working_candidate_is_explicit_and_binds_uncommitted_source(self):
        (self.repo / 'bridge/new_feature.py').write_bytes(b'synthetic = True\n')
        output = self.root / 'candidate.zip'
        manifest = release.package(self.repo, 'HEAD', output, working_tree=True)
        self.assertTrue(manifest['candidateOnly'])
        self.assertFalse(manifest['releaseReady'])
        with zipfile.ZipFile(output) as archive:
            prefix = release.prefix('0.3.0')
            self.assertEqual(archive.read(prefix + 'bridge/new_feature.py'), b'synthetic = True\n')
            candidate = json.loads(archive.read(prefix + 'CANDIDATE.json'))
            self.assertFalse(candidate['releaseReady'])
            self.assertEqual(candidate['fileSha256'], manifest['fileSha256'])

    def test_mismatched_versions_refused_before_output(self):
        (self.repo / 'bridge/geck_mcp/__init__.py').write_text('__version__="9.9.9"\n')
        self.git('add', 'bridge/geck_mcp/__init__.py')
        self.git('commit', '-qm', 'Synthetic mismatch')
        output = self.root / 'bad.zip'
        with self.assertRaisesRegex(ValueError, 'version markers'):
            release.package(self.repo, 'HEAD', output)
        self.assertFalse(output.exists())

    def test_a_folder_output_gets_the_versioned_name(self):
        folder = self.root / 'out'
        folder.mkdir()
        manifest = release.package(self.repo, 'HEAD', folder)
        self.assertEqual(manifest['archive'], 'fnv-mcp-0.3.0.zip')
        self.assertTrue((folder / 'fnv-mcp-0.3.0.zip').is_file())

    def test_a_changelog_on_another_version_is_refused(self):
        (self.repo / 'CHANGELOG.md').write_text('# Changelog\n\n## fnv-mcp 9.9.9\n')
        self.git('add', 'CHANGELOG.md')
        self.git('commit', '-qm', 'Synthetic changelog')
        with self.assertRaisesRegex(ValueError, 'version markers'):
            release.package(self.repo, 'HEAD', self.root / 'bad.zip')

    def test_the_real_repository_packages(self):
        repo = Path(__file__).resolve().parents[2]
        manifest = release.package(repo, 'HEAD', self.root)
        self.assertEqual(manifest['name'], 'fnv-mcp')
        with zipfile.ZipFile(self.root / manifest['archive']) as z:
            names = z.namelist()
        self.assertFalse([n for n in names if n.lower().endswith(('.exe', '.dll', '.esp', '.nif'))])
        self.assertTrue(any(n.endswith('bridge/run_mcp.py') for n in names))
        self.assertTrue(any(n.endswith('.yaml') for n in names))

    def test_unsafe_paths_refused(self):
        for name in ('../outside.py', '/outside.py', 'bridge/../../outside.py',
                     'bridge\\outside.py', 'bridge/file:stream.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.included(name)
