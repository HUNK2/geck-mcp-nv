"""fnv_replay (Plan 2, D-time.md section 6): record the input the bridge sends, frame by frame, and play it again.

record name      save the game (the replay's start), seed the random number generator (fnv_rng) and the frame
                 counter's start; from then on every input job (fnv_input, fnv_play, fnv_test steps: they all go through the
                 injector) is kept with its frame offset
stop             write state/fnv/replays/<name>.json: save, seed, jobs with frame offsets
play name        load the save, seed the generator again, hold the frame gate and step to each job's frame, queue the
                 job, and step on to the end; the game is left held at the last frame (release with fnv_time)
list             the saved replays

Only input sent through the bridge is recorded; a person's own key presses are not (the injector's own format, as the
plan says). Timing is exact to the frame under the gate; what the game does with the same input in the same frames
depends on everything else being equal (deterministic mode, fnv_time).
"""
import json
import time
from pathlib import Path

from .fnv_conv import ConvError, verdict
from .fnv_inject import Injector, shared_time
from .fnv_time import _frame, frame_ops

DEFAULT_SEED = 20261111


class ReplayTools:
    def __init__(self, live, agent=None):
        self.live = live
        self.agent = agent
        self.recording = None

    @property
    def folder(self):
        path = Path(self.live.project.state_dir) / 'fnv' / 'replays'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _frame(self):
        return _frame(self.live.link.batch(frame_ops())[1])

    def record(self, name, seed=None):
        self.live.in_game()
        if self.recording:
            raise ConvError('ALREADY_RECORDING', 'A replay (%s) is being recorded. Next: stop.' % self.recording['name'])
        if not name or not name.replace('_', '').replace('-', '').isalnum():
            raise ConvError('BAD_ARGUMENTS', 'name uses letters, digits, _ and - only.')
        save = 'replay_%s' % name
        out = self.live.console('save %s' % save)
        time.sleep(1.0)
        seed = DEFAULT_SEED if seed is None else int(seed)
        if self.agent is not None:
            self.agent.rng(seed)
        start = self._frame()
        self.recording = {'name': name, 'save': save, 'seed': seed, 'startFrame': start, 'jobs': [],
                          'started': time.time()}

        def keep(op, frame):
            if self.recording is not None:
                self.recording['jobs'].append({'offset': int(frame or self._frame()) - start, 'op': list(op)})
        self.live.replay_recorder = keep
        return verdict('Recording replay %s from frame %d' % (name, start),
                       'The game was saved as %s and the random number generator seeded with %d. Input sent through '
                       'the bridge (fnv_input, fnv_play, fnv_test steps) is kept with its frame. stop writes the replay.'
                       % (save, seed), save=save, seed=seed, saveOutput=out.get('output'))

    def stop(self):
        if not self.recording:
            raise ConvError('NOT_RECORDING', 'No replay is being recorded.')
        rec = self.recording
        self.recording = None
        self.live.replay_recorder = None
        try:
            rec['endOffset'] = self._frame() - rec['startFrame']
        except Exception:  # noqa: BLE001 - the last job's frame is the end
            rec['endOffset'] = max([j['offset'] for j in rec['jobs']] or [0])
        path = self.folder / ('%s.json' % rec['name'])
        path.write_text(json.dumps(rec, indent=1), encoding='utf-8')
        return verdict('Replay %s: %d input jobs over %d frames' % (rec['name'], len(rec['jobs']), rec['endOffset']),
                       'Written to %s.' % path, path=str(path), jobs=len(rec['jobs']), frames=rec['endOffset'])

    def play(self, name, load=True, timeout=180):
        path = self.folder / ('%s.json' % name)
        if not path.is_file():
            raise ConvError('NO_REPLAY', 'No replay %s (see list).' % name)
        rec = json.loads(path.read_text(encoding='utf-8'))
        if load:
            self.live.link.schedule(0, 'load "%s"' % rec['save'])
            time.sleep(1.0)
            self.live.game.wait_in_game(time.monotonic() + timeout, start='replay:%s' % name)
        if self.agent is not None:
            self.agent.rng(int(rec['seed']))
        control, _ = shared_time(self.live)
        control.hold()
        injector = Injector.of(self.live)
        injector.ensure()
        done, at = [], 0
        try:
            for job in sorted(rec['jobs'], key=lambda j: j['offset']):
                gap = int(job['offset']) - at
                if gap > 0:
                    control.step(gap)
                    at += gap
                result = injector.batch([tuple(job['op'])])[0]
                done.append({'offset': job['offset'], 'op': ' '.join(job['op'][:2]), 'ok': bool(result.get('ok'))})
            rest = int(rec.get('endOffset') or at) - at
            if rest > 0:
                control.step(rest)
                at += rest
        except Exception:
            control.release()
            raise
        return verdict('Replayed %s: %d jobs over %d frames; the game is held at the end' % (name, len(done), at),
                       'Loaded %s, seeded the generator with %s, then stepped the frame gate to each job. Inspect the '
                       'state now; fnv_time release lets the game run.' % (rec['save'], rec['seed']),
                       jobs=done, frames=at)

    def list(self):
        rows = []
        for p in sorted(self.folder.glob('*.json')):
            try:
                rec = json.loads(p.read_text(encoding='utf-8'))
            except ValueError:
                continue
            rows.append({'name': rec.get('name'), 'jobs': len(rec.get('jobs') or []), 'frames': rec.get('endOffset'),
                         'save': rec.get('save')})
        return verdict('%d replays' % len(rows), None, replays=rows)

    def run(self, action, **a):
        if action == 'record':
            return self.record(a.get('name'), a.get('seed'))
        if action == 'stop':
            return self.stop()
        if action == 'play':
            if not a.get('name'):
                raise ConvError('BAD_ARGUMENTS', 'play needs name.')
            return self.play(a['name'], a.get('load', True))
        if action == 'list':
            return self.list()
        raise ConvError('UNKNOWN_ACTION', 'Unknown action %r. Valid: record, stop, play, list.' % action)
