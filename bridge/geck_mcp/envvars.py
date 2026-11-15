"""Environment variables under the one fnv-mcp prefix (release plan Task 5.1).

New documents name FNV_MCP_*; the old names keep working. The data paths (FNV_ENGINE_MAP, FNV_DECOMP, FNV_SYMDB,
FNV_CRASH_DIRS, FNV_RESEARCH_DIR, FNV_CLEAN_EXE) already have the FNV_ prefix and stay as they are; FNVSYM_ROOT is
fnvsym's own name. run_mcp.py and the supervisor read their two variables without importing this package (a broken
edit must not stop them), with the same rule."""
import os

RENAMED = {
    'FNV_MCP_ZIG': 'GECK_MCP_ZIG',
    'FNV_MCP_PIPE': 'FNVMCP_PIPE',
    'FNV_MCP_RELOAD': 'GECK_MCP_RELOAD',
    'FNV_MCP_SUPERVISOR_LOG': 'GECK_SUPERVISOR_LOG',
    'FNV_MCP_TRANSPORT': 'GECK_MCP_TRANSPORT',
}


def get(name, default=None):
    """The new name's value, else the old name's, else default. An empty value counts as set."""
    for key in (name, RENAMED.get(name)):
        if key and key in os.environ:
            return os.environ[key]
    return default
