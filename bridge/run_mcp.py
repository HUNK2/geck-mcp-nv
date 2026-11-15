"""Entry point of the geck MCP server (stdio).

By default this process is a small supervisor (geck_mcp/supervisor.py) that runs the server as a child process and
reloads it when the code under geck_mcp/ or the project file changes, then tells the client the tool list changed,
so no reconnect is needed after an edit. `--no-reload` (or FNV_MCP_RELOAD=0; the old name GECK_MCP_RELOAD works too) serves directly in this process;
`--serve` is what the supervisor passes to its child.
"""
import importlib.util
import os
import sys
from pathlib import Path


def serve(argv):
    from geck_mcp.server import main
    main(argv)


def supervise(argv):
    # Loaded by path, not as geck_mcp.supervisor: a broken edit in the package must not stop the supervisor.
    path = Path(__file__).resolve().parent / 'geck_mcp' / 'supervisor.py'
    spec = importlib.util.spec_from_file_location('geck_supervisor', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main(__file__, argv)


def check_project(argv):
    """Load the project file once before serving: one stderr line and exit code 2 when it is not valid (Task 4.3).
    A package that does not import is left to the supervisor, which reports it and reloads on the next edit."""
    if '--project' not in argv or argv.index('--project') + 1 >= len(argv):
        return
    path = argv[argv.index('--project') + 1]
    try:
        from geck_mcp.config import Project
        from geck_mcp.server import project_error
    except Exception:  # noqa: BLE001
        return
    try:
        Project.load(path)
    except Exception as e:  # noqa: BLE001
        sys.exit(project_error(path, e))


if __name__ == '__main__':
    args = sys.argv[1:]
    check_project(args)
    if '--serve' in args or '--no-reload' in args or os.environ.get('FNV_MCP_RELOAD', os.environ.get('GECK_MCP_RELOAD')) == '0':
        serve([a for a in args if a not in ('--serve', '--no-reload')])
    else:
        supervise(args)
