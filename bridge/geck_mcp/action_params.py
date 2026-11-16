"""The parameters each action of a tool uses (TOOL-MERGE, bug T8).

A merged tool takes the union of its old tools' parameters, and most of them mean nothing to most actions. A tool
listed here refuses a parameter the chosen action does not use, when the call gives it a value other than its
default, so an agent learns its argument had no effect instead of it being ignored. The server checks this before
the tool runs (BridgeServer._handle_call_tool); tests/test_action_params.py keeps the table exact.

Entry: {'by': the parameter that picks the action (default 'action'), 'common': parameters every action takes,
'actions': {action: parameters it takes}}."""

ACTION_PARAMS = {
    'fnv_place': {'actions': {
        'at': {'base', 'position', 'near', 'offset', 'rotation', 'scale', 'snap', 'export', 'count', 'distance',
               'direction'},
        'array': {'base', 'position', 'shape', 'count', 'spacing', 'columns', 'radius', 'rotation', 'snap'},
        'nudge': {'ref', 'offset', 'local'},
        'rotate': {'ref', 'rotation'},
        'scale': {'ref', 'scale'},
        'snap': {'ref', 'align'},
        'check': {'ref'},
        'list': set(),
        'remove': {'ref'},
    }},
    'fnv_ui_do': {'actions': {
        'click': {'target', 'label', 'menu'},
        'button': {'label', 'target', 'menu'},
        'wait': {'hours'},
        'sleep': {'hours'},
        'fast_travel': {'marker', 'target'},
        'message': {'button', 'label'},
        'close': {'menu'},
        'back': {'menu'},
        'type': {'label', 'target'},
        'close_all': set(),
    }},
    # TOOL-MERGE tier 1
    'fnv_crash': {'actions': {
        'explain': {'path', 'max_frames'},
        'latest': {'path', 'max_frames'},
        'history': {'limit', 'include_shutdown'},
        'context': {'limit'},
        'capture_on': set(),
        'capture_off': set(),
        'capture_status': set(),
    }},
    'fnv_trace': {'actions': {
        'start': {'function', 'when', 'filter', 'stack', 'allow_unproven'},
        'read': {'function', 'limit', 'include_calls', 'allow_unproven'},
        'stop': {'function', 'limit', 'include_calls', 'allow_unproven'},
        'list': set(),
    }},
    'fnv_func_find': {'by': 'mode', 'common': {'query', 'limit'}, 'actions': {
        **{mode: {'status', 'offset'} for mode in ('auto', 'name', 'code', 'regex', 'address')},
        'callers': {'depth'},
        'callees': {'depth'},
    }},
    'fnv_time': {'actions': {
        'calendar': {'set_hour', 'advance_hours', 'timescale', 'mode', 'evaluate_packages', 'allow_unproven'},
        'pause': set(), 'resume': set(), 'force_resume': set(), 'release': set(), 'status': set(),
        'hold': {'timeout_seconds'},
        'step': {'frames', 'fps'},
        'fixed': {'fps'},
        'speed': {'x'},
    }},
    'fnv_snapshot': {'actions': {
        'capture': {'name', 'items'},
        'diff': {'before', 'after'},
    }},
    'fnv_game': {'actions': {
        'launch': {'start', 'timeout_seconds'},
        'restart': {'start', 'timeout_seconds'},
        'quit': {'force', 'timeout_seconds'},
    }},
    'fnv_menu': {'actions': {
        'state': set(),
        'tiles': {'menu', 'path', 'depth', 'max_tiles', 'traits'},
    }},
    'fnv_test': {'actions': {
        'run': {'path', 'yaml', 'relaunch_on_crash'},
        'run_all': {'folder'},
        'steps': {'steps', 'name', 'start', 'plugins', 'setup', 'teardown', 'deterministic', 'repeat',
                  'timeout_seconds', 'relaunch_on_crash'},
    }},
}


def unused_arguments(tool, arguments, schema, table=None):
    """(action, [parameters the call sets that this action does not use]); (None, []) for a tool not in the table.
    A parameter counts only when its value differs from its default, so a client that sends defaults is fine."""
    entry = (ACTION_PARAMS if table is None else table).get(tool)
    if not entry:
        return None, []
    props = (schema or {}).get('properties', {})
    by = entry.get('by', 'action')
    action = (arguments or {}).get(by, (props.get(by) or {}).get('default'))
    allowed = set(entry.get('common', ())) | {by} | set(entry['actions'].get(action, ()))
    if action not in entry['actions']:
        return action, []                       # an unknown action is the schema's error to give
    unused = []
    for name, value in (arguments or {}).items():
        if name in allowed or name not in props:
            continue
        if value != (props[name] or {}).get('default'):
            unused.append(name)
    return action, sorted(unused)


def refusal(tool, action, unused, table=None):
    """The INVALID_ARGUMENT sentence for unused parameters."""
    entry = (ACTION_PARAMS if table is None else table)[tool]
    takes = sorted(set(entry.get('common', ())) | set(entry['actions'].get(action, ())))
    return '%s action=%s does not use %s; it takes %s.' % (
        tool, action, ', '.join(unused), ', '.join(takes) if takes else 'no other parameters')
