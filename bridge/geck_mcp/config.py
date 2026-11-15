from pathlib import Path, PureWindowsPath
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    editor_id: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')
    form_id: str = Field(pattern=r'^[0-9A-Fa-f]{8}$')
    record_type: str = Field(default='STAT', pattern=r'^STAT$')


class FnvSettings(BaseModel):
    """Optional settings for the fnv_* tools (engine knowledge, crash doctor). Paths may be relative to the
    project file. Every field can stay unset: engine_map defaults to <state_dir>/engine_map.json and the
    decomp checkout to the path recorded in the map."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    engine_map: Path | None = None
    decomp_root: Path | None = None
    # Symbol database built by engine_map.symdb (default: <state_dir>/symdb.sqlite). See docs/FNV-SYMDB.md.
    symdb: Path | None = None
    # Folder of saved engine research notes (Markdown) that fnv_guide searches.
    research_dir: Path | None = None
    crash_dirs: list[Path] = Field(default_factory=list, max_length=20)
    mo2_root: Path | None = None
    mo2_profile: str | None = Field(default=None, pattern=r'^[^\\/:*?"<>|]{1,64}$')
    # Game control and the engine companion (fnv-mcp milestones 4-6).
    mo2_executable: str = Field(default='NVSE', pattern=r'^[^\\/:*?"<>|]{1,64}$')
    zig: Path | None = None
    engine_pipe: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    # Act tools (memory writes, engine calls, console changes) refuse unless this is "full".
    engine_control: Literal['read', 'full'] = 'read'
    tests_dir: Path | None = None
    # Your own unpacked copy of FalloutNV.exe 1.4.0.525 (the Steam exe's code is encrypted on disk): the
    # reference fnv_patch_map compares the running game's code with. Read locally, never copied.
    clean_exe: Path | None = None


class Project(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    project_id: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    # Windows only (decision D5): the helper .exe runs directly next to GECK. The key stays so existing project
    # files keep loading; the CrossOver and Wine launchers were removed.
    launcher: Literal['native'] = 'native'
    game_root: Path
    plugin: str
    records: list[Record] = Field(default_factory=list, max_length=100)
    source_roots: list[Path] = Field(default_factory=list)
    state_dir: Path
    helper: Path
    helper_timeout_seconds: int = Field(default=45, ge=5, le=90)
    # auto: persistent worker when the helper supports `serve`, else one helper launch per call.
    transport: Literal['auto', 'persistent', 'oneshot'] = 'auto'
    # Milestone 2: JSON spec this project builds from (relative to the project file).
    spec: Path | None = None
    # fnv_* tools (engine map, crash doctor); optional.
    fnv: FnvSettings | None = None

    @field_validator('plugin')
    @classmethod
    def plugin_name(cls, value):
        if not value.lower().endswith('.esp') or any(c in value for c in '/\\:') or '..' in value:
            raise ValueError('plugin must be a single .esp filename')
        return value

    @model_validator(mode='after')
    def identities(self):
        if not self.records and not self.spec:
            raise ValueError('configure at least one record, or a spec')
        if len({r.editor_id for r in self.records}) != len(self.records):
            raise ValueError('duplicate configured EditorID')
        if len({r.form_id.upper() for r in self.records}) != len(self.records):
            raise ValueError('duplicate configured FormID')
        return self

    # The folder of the project file (set by load; None for a Project built in code). safe_paths reads it.
    _project_dir: Path | None = PrivateAttr(default=None)

    @property
    def project_dir(self):
        return self._project_dir

    @property
    def lock_root(self):
        """Everything sharing this path shares one bridge lock and one recovery barrier."""
        return self.game_root

    def editor_path(self, path):
        """A host path as the helper (a Windows program) sees it."""
        return str(path)

    def helper_command(self, *args, helper=None):
        return [self.editor_path(helper or self.helper), *map(str, args)]

    def helper_env(self):
        return {}

    @property
    def plugin_path(self):
        return contained(self.game_root / 'Data', self.plugin)

    def record(self, editor_id):
        for record in self.records:
            if record.editor_id == editor_id:
                return record
        raise ValueError('record is not configured for this project')

    @classmethod
    def load(cls, path):
        path = Path(path).resolve(strict=True)
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        for key in ('game_root', 'state_dir', 'helper'):
            if data.get(key) is None:
                continue
            value = Path(data[key]).expanduser()
            data[key] = (value if value.is_absolute() else path.parent / value).resolve()
        if data.get('spec'):
            value = Path(data['spec']).expanduser()
            data['spec'] = str((value if value.is_absolute() else path.parent / value).resolve())
        data['source_roots'] = [str((path.parent / Path(p).expanduser()).resolve())
                                for p in data.get('source_roots', [])]
        if isinstance(data.get('fnv'), dict):
            fnv = dict(data['fnv'])

            def resolve(value):
                value = Path(value).expanduser()
                return str((value if value.is_absolute() else path.parent / value).resolve())
            for key in ('engine_map', 'decomp_root', 'symdb', 'research_dir', 'mo2_root', 'zig', 'tests_dir', 'clean_exe'):
                if fnv.get(key):
                    fnv[key] = resolve(fnv[key])
            fnv['crash_dirs'] = [resolve(v) for v in fnv.get('crash_dirs', [])]
            data['fnv'] = fnv
        project = cls.model_validate(data)
        project._project_dir = path.parent
        return project


def contained(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('path escapes configured root')
    return path


def model_asset(project, model):
    win = PureWindowsPath(model)
    if (not model or '/' in model or ':' in model or win.is_absolute() or win.drive
            or '..' in win.parts or model.startswith('\\')
            or win.parts[0].lower() == 'meshes' or win.suffix.lower() != '.nif'):
        raise ValueError('model must be a .nif path relative to Data\\meshes')
    return contained(project.game_root / 'Data' / 'meshes', Path(*win.parts))
