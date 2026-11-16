"""Toolsets chosen at server start (TOOL-MERGE step 1): which families of tools the server registers.

game: the fnv_* tools for the running game and the offline engine knowledge. editor: the geck_* tools. naming: the
function naming tools (fnv_name_*, fnv_probe, fnv_atlas). legacy: the v0.4 geck_runtime_* and geck_console_* tools
(decision D4). The project key `toolsets` (or `--toolsets`) picks them; the default is every one but legacy. A
client keeps the tool list it saw at connect, so a toolset is fixed for a connection."""

TOOLSETS = ('game', 'editor', 'naming', 'legacy')
DEFAULT_TOOLSETS = ('game', 'editor', 'naming')
# Registered whatever the toolsets: fnv_guide find names a hidden tool and the toolset that turns it on.
ALWAYS = frozenset({'fnv_guide'})
NAMING_TOOLS = frozenset({'fnv_probe', 'fnv_atlas'})


def toolset_of(name):
    """The toolset a tool belongs to."""
    from .server import LEGACY_TOOLS
    if name in LEGACY_TOOLS:
        return 'legacy'
    if name.startswith('geck_'):
        return 'editor'
    if name.startswith('fnv_name_') or name in NAMING_TOOLS:
        return 'naming'
    return 'game'


def hidden_note(toolset):
    """What a hidden tool's description says in fnv_guide find."""
    from .server import LEGACY_NOTE
    if toolset == 'legacy':
        return LEGACY_NOTE
    return ('In the %s toolset, off in this server: add "%s" to "toolsets" in the project file (or start the server '
            'with --toolsets) to use it.' % (toolset, toolset))
