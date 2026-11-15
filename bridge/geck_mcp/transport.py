import json
import os
import subprocess


class BridgeError(RuntimeError):
    def __init__(self, code, message, evidence=None, uncertain=False):
        super().__init__(message)
        self.code, self.evidence, self.uncertain = code, evidence, uncertain




class Transport:
    def __init__(self, project):
        self.project = project

    def call(self, operation, *args, session=None, expected_model=None):
        p = self.project
        env = dict(os.environ)
        # Never inherit a stale guard from the invoking shell.
        for key in list(env):
            if key.startswith('GECK_BRIDGE_'):
                del env[key]
        env.update(GECK_BRIDGE_PLUGIN=p.plugin,
                   GECK_BRIDGE_RECORDS='|'.join(r.editor_id for r in p.records),
                   GECK_BRIDGE_MCP='1', **p.helper_env())
        if session:
            env['GECK_BRIDGE_SESSION'] = session
        if expected_model is not None:
            env['GECK_BRIDGE_EXPECTED_MODEL'] = expected_model
        command = p.helper_command(operation, *args)
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace',
                                    timeout=p.helper_timeout_seconds)
        except subprocess.TimeoutExpired as e:
            raise BridgeError('OUTCOME_UNKNOWN', 'Helper exceeded deadline; inspect before retry',
                              uncertain=True) from e
        except OSError as e:
            raise BridgeError('HELPER_UNAVAILABLE', str(e)) from e
        messages = []
        for line in result.stdout.splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict) and 'ok' in value:
                    messages.append(value)
            except ValueError:
                pass
        if len(messages) != 1:
            raise BridgeError('HELPER_PROTOCOL_ERROR', 'Expected one helper result',
                              {'stderr': result.stderr[-2000:]}, uncertain=True)
        return interpret(messages[0], operation, result.returncode)


def interpret(value, operation, returncode=0, evidence_extra=None):
    """Map one helper result object to its `after` payload or a typed BridgeError."""
    if value.get('operation') != operation:
        raise BridgeError('HELPER_PROTOCOL_ERROR', 'Helper operation mismatch', value, True)
    if not value['ok'] or returncode:
        message = value.get('error') or 'helper failed'
        code = 'EDITOR_ERROR'
        for phrase, candidate in [('timed out', 'EDITOR_UNRESPONSIVE'),
                                  ('STALE_SESSION', 'STALE_SESSION'),
                                  ('STALE_STATE', 'STALE_STATE'),
                                  ('dialog', 'MODAL_BLOCKED'),
                                  ('unexpected plugin', 'WRONG_PLUGIN')]:
            if phrase in message:
                code = candidate
                break
        evidence = dict(value, **evidence_extra) if evidence_extra else value
        raise BridgeError(code, message, evidence)
    return value['after']
