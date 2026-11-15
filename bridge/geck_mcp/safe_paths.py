"""One check for every name and path argument a tool takes (release plan Phase 1, audit B1 and H5).

Every helper raises ValueError, which an fnv_* tool answers as INVALID_ARGUMENT."""
import os
import re
from pathlib import Path

_PLAIN = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _.\-]*\Z')
_DEVICES = {'CON', 'PRN', 'AUX', 'NUL'} | {'COM%d' % i for i in range(1, 10)} | {'LPT%d' % i for i in range(1, 10)}


def plain_name(value, what='name', max_length=60):
    """value unchanged when it is a plain file name: letters, digits, space, _ - and dots, starting with a letter or
    digit, no '..', no trailing dot or space, not a Windows device name. Else ValueError."""
    if not isinstance(value, str) or not value:
        raise ValueError('%s must be a plain name (letters, digits, space, _ - .)' % what)
    if len(value) > max_length:
        raise ValueError('%s is longer than %d characters' % (what, max_length))
    if not _PLAIN.match(value) or '..' in value or value[-1] in '. ':
        raise ValueError('%s must be a plain name: letters, digits, space, _ - and single dots, starting with a '
                         'letter or digit and not ending in a dot or space (no folders, no drive, no "..")' % what)
    if value.split('.')[0].rstrip(' ').upper() in _DEVICES:
        raise ValueError('%s is a Windows device name (%s); pick another name' % (what, value))
    return value


def no_unc(path, what='path'):
    """Path(path), or ValueError for a network path (two slashes of either kind first: \\\\server\\share, //server,
    \\\\?\\UNC\\...). Looks at the text only: nothing touches the file system or the network."""
    text = os.fspath(path)
    if not isinstance(text, str):
        raise ValueError('%s must be a path' % what)
    if len(text) >= 2 and text[0] in '\\/' and text[1] in '\\/':
        raise ValueError('%s is a network or device path (it starts with two slashes); use a local path' % what)
    return Path(text)


def inside(path, roots, what='path'):
    """The resolved path when it lies inside one of roots (resolved too, so .., junctions, symlinks, forward slashes
    and letter case are settled first). Else ValueError naming the allowed folders."""
    candidate = no_unc(path, what).expanduser()
    roots = [Path(r) for r in roots if r]
    resolved = candidate.resolve()
    for root in roots:
        try:
            base = no_unc(root, 'root').resolve()
        except (OSError, ValueError):
            continue
        if os.path.normcase(str(resolved)) == os.path.normcase(str(base)) or _below(resolved, base):
            return resolved
    raise ValueError('%s must be inside one of these folders: %s' % (what, ', '.join(str(r) for r in roots) or
                                                                     '(none set in the project file)'))


def _below(path, root):
    path_parts = [p.lower() for p in path.parts] if os.name == 'nt' else list(path.parts)
    root_parts = [p.lower() for p in root.parts] if os.name == 'nt' else list(root.parts)
    return len(path_parts) > len(root_parts) and path_parts[:len(root_parts)] == root_parts


def _project_folder(project):
    folder = getattr(project, 'project_dir', None)
    return Path(folder) if folder else None


def read_roots(project):
    """Folders a path argument may read from: the project file's folder, the state folder, the game, the MO2 root, the
    tests folder, the decomp checkout, the crash log folders and the companion's %LOCALAPPDATA%\\fnvmcp. Unset ones are
    left out."""
    fnv = getattr(project, 'fnv', None)
    roots = [_project_folder(project), getattr(project, 'state_dir', None), getattr(project, 'game_root', None)]
    if fnv is not None:
        roots += [getattr(fnv, 'mo2_root', None), getattr(fnv, 'tests_dir', None), getattr(fnv, 'decomp_root', None)]
        roots += list(getattr(fnv, 'crash_dirs', None) or [])
    if os.environ.get('LOCALAPPDATA'):
        roots.append(Path(os.environ['LOCALAPPDATA']) / 'fnvmcp')
    out = []
    for root in roots:
        if root and Path(root) not in out:
            out.append(Path(root))
    return out


def write_roots(project):
    """Folders a path argument may write next to: the state folder and the project file's folder."""
    return [Path(r) for r in (getattr(project, 'state_dir', None), _project_folder(project)) if r]


_DIRECTIVE = re.compile(r'[ \t]*#[ \t]*(include_next|include|import|embed)\b(.*)', re.DOTALL)
_TARGET = re.compile(r'[ \t]*(?:"([^"\n]+)"|<([^>\n]+)>)[ \t]*\Z')


def _code_only(source):
    """The C text as the preprocessor sees directives: trigraphs ??= and ??/ replaced, backslash-newlines spliced,
    comments turned into a space, string and character literals kept as they are."""
    text = source.replace('??=', '#').replace('??/', '\\')
    text = re.sub(r'\\\r?\n', '', text)
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in '"\'':
            j = i + 1
            while j < n and text[j] != c and text[j] != '\n':
                j += 2 if text[j] == '\\' else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            end = n if end < 0 else end + 2
            out.append(' ' + '\n' * text.count('\n', i, end))
            i = end
        elif text.startswith('//', i):
            end = text.find('\n', i)
            i = n if end < 0 else end
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def check_includes(source):
    """ValueError unless every #include (and #include_next, #import, #embed) names a file in the literal form "name"
    or <name>, with no drive, no leading slash and no '..': C source cannot pull in files from elsewhere."""
    for number, line in enumerate(_code_only(source or '').split('\n'), 1):
        match = _DIRECTIVE.match(line)
        if not match:
            continue
        target = _TARGET.match(match.group(2))
        name = target and (target.group(1) or target.group(2))
        if not name:
            raise ValueError('line %d: #%s must name a header as "name.h" or <name.h> (no macros)' % (number,
                                                                                                 match.group(1)))
        parts = re.split(r'[\\/]', name)
        if ':' in name or name[0] in '\\/' or '..' in parts:
            raise ValueError('line %d: #%s "%s" reaches outside the include folders (no drive, no leading slash, '
                             'no ..)' % (number, match.group(1), name))
