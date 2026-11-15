"""Build a reproducible source-only download of fnv-mcp from a committed Git revision.

The archive is fnv-mcp-<version>.zip with one top folder of the same name; the version comes from
bridge/pyproject.toml and must agree with geck_mcp/__init__.py and the first CHANGELOG heading. Source, scenario
(.yaml) and script (.gek) files go in; binaries, private state, game-derived fixtures and internal working notes
stay out."""
import argparse
import ast
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tomllib
import zipfile

NAME = 'fnv-mcp'
ROOT_FILES = {'README.md', 'CHANGELOG.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'}
SOURCE_SUFFIXES = {'.py', '.cs', '.c', '.h', '.json', '.toml', '.txt', '.md', '.yaml', '.yml', '.gek'}
# Working notes that are not for users (they live outside the repository now; kept out if they come back).
INTERNAL = {'HANDOFF.md', 'STATUS.md', 'MCPworkflow.md', 'BUG-LOG.md', 'M1-FAST-BRIDGE.md'}
INTERNAL_DIRS = {'audit3', 'audit4', 'internal'}
# These regression modules require the game-derived fixtures omitted from downloads.
FIXTURE_TESTS = {f'bridge/tests/test_{name}.py' for name in
                 ('authoring', 'build', 'codec', 'records', 'validate')}


def included(name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:
        raise ValueError('unsafe archive path')
    if name in ROOT_FILES:
        return True
    if not p.parts or p.parts[0] not in {'bridge', 'docs', 'tools'}:
        return False
    if p.name in INTERNAL or INTERNAL_DIRS & set(p.parts):
        return False
    if any(part in {'state', 'build', 'evidence', 'photos', 'fixtures', '.venv',
                    '__pycache__', 'backups', 'screenshots', 'history'} for part in p.parts):
        return False
    if name in FIXTURE_TESTS:
        return False
    if p.parts[:2] == ('bridge', 'projects') and not p.name.startswith('example-'):
        return False
    return p.name == 'LICENSE' or p.suffix in SOURCE_SUFFIXES


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def prefix(version):
    return '%s-%s/' % (NAME, version)


def package(repo, revision, output, working_tree=False):
    """output: a new .zip path, or an existing folder to put fnv-mcp-<version>.zip in."""
    repo, output = Path(repo), Path(output)
    folder = output if output.is_dir() else None
    if folder is None and output.suffix.lower() != '.zip':
        raise ValueError('output must be a .zip path or a folder')
    commit = git(repo, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    source = zipfile.ZipFile(io.BytesIO(git(repo, 'archive', '--format=zip', commit)))
    files, excluded = {}, []
    for item in source.infolist():
        if item.is_dir():
            continue
        if not included(item.filename):
            excluded.append(item.filename)
            continue
        if (item.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError('source symlinks are not supported')
        files[item.filename] = source.read(item)
    if working_tree:
        # Explicit local candidate mode: default publication packaging still uses committed source only.
        files = {}
        names = git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').decode().split('\0')
        for name in sorted(set(names) - {''}):
            if not included(name):
                continue
            path = repo / name
            if path.is_symlink():
                raise ValueError('source symlinks are not supported')
            if path.exists():
                files[name] = path.read_bytes()
    version = tomllib.loads(files['bridge/pyproject.toml'].decode())['project']['version']
    module = ast.parse(files['bridge/geck_mcp/__init__.py'].decode())
    declared = [ast.literal_eval(n.value) for n in module.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == '__version__' for t in n.targets)]
    changelog = files.get('CHANGELOG.md', b'').decode()
    heading = next((line for line in changelog.splitlines() if line.startswith('## ')), '')
    if declared != [version] or version not in heading:
        raise ValueError('release version markers disagree (pyproject, geck_mcp.__version__, CHANGELOG)')
    if folder is not None:
        output = folder / ('%s-%s.zip' % (NAME, version))
    manifest_path = output.with_suffix('.json')
    if output.exists() or manifest_path.exists():
        raise FileExistsError('archive or manifest already exists; choose a new output')
    snapshot_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    if working_tree:
        files['CANDIDATE.json'] = (json.dumps({'candidateOnly': True, 'releaseReady': False,
            'baseCommit': commit, 'source': 'uncommitted working tree', 'fileSha256': snapshot_hashes},
            indent=2, sort_keys=True) + '\n').encode()
    with output.open('xb') as target, zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(prefix(version) + name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    manifest = {'name': NAME, 'version': version, 'sourceCommit': commit, 'archive': output.name,
                'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                'size': output.stat().st_size, 'files': len(files),
                'excluded': sorted(excluded), 'publication': 'local-only'}
    if working_tree:
        manifest.update(candidateOnly=True, releaseReady=False, fileSha256=snapshot_hashes)
    with manifest_path.open('x', encoding='utf-8') as target:
        json.dump(manifest, target, indent=2)
        target.write('\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--revision', required=True, help='committed source revision')
    parser.add_argument('--output', type=Path, required=True,
                        help='new .zip path, or a folder for fnv-mcp-<version>.zip; never overwritten')
    parser.add_argument('--working-tree', action='store_true', help='local uncommitted candidate only; embeds releaseReady=false')
    args = parser.parse_args()
    print(json.dumps(package(args.repo, args.revision, args.output, args.working_tree), indent=2))
