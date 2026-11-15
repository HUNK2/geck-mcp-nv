"""BUG-LOG P1: fnv_place snap/check found the ceiling, not the surface below, in low interiors.

The downward ray starts 200 units above the point (250 for snap and check) and the first hit was taken as the
surface, so in a room whose ceiling is lower than that the ceiling won. Now a point takes the first surface at or
under z + 64 (a curb, a stair tread, a slope it is in; round-4 audit H1), one above only when nothing is under it
(inside a hill); an object skips the surfaces hit before its own collision (overhead), and check names a close one.
A surface close over an object with the ground well under it is buried-or-under-a-ceiling: the player's eye decides
(round-4 V2, EyeSideTests), else it stays ambiguous. Room numbers are NPCTextureTest's (floor 16704, ceiling 16960) and the bar built there. Fakes only; no game."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_create  # noqa: E402


class PlaneRays:
    """Horizontal surfaces at fixed heights (z, ref), unbounded or (z, ref, x_min, x_max), two-sided like the pick
    ray's triangles. A ray from a to b hits the surface between them nearest a, inclusive at the start: going down
    the highest, going up the lowest; a slanted ray hits where it crosses that height."""

    def __init__(self, planes):
        self.planes = planes
        self.casts = []

    def cast(self, segments, layer=0x26, refs=False):
        out = []
        for a, b in segments:
            self.casts.append((a, b, layer))
            down = b[2] <= a[2]
            hits = []
            for plane in self.planes:
                z, ref = plane[:2]
                if a[2] == b[2] or not (b[2] <= z <= a[2] if down else a[2] <= z <= b[2]):
                    continue
                f = (z - a[2]) / (b[2] - a[2])
                x = a[0] + (b[0] - a[0]) * f
                if len(plane) == 4 and not plane[2] <= x <= plane[3]:
                    continue
                hits.append((z, ref, f))
            if not hits:
                out.append({'hit': False})
                continue
            z, ref, f = max(hits) if down else min(hits)
            out.append({'hit': True, 'fraction': f, 'point': tuple(a[i] + (b[i] - a[i]) * f for i in range(3)),
                        'ref': ref})
        return out


def tools(planes):
    rays = PlaneRays(planes)
    return fnv_create.PlaceTools(live=None, hands=SimpleNamespace(rays=rays)), rays


CEILING, SHELF, FLOOR = 16960.0, 16831.0, 16700.0
BAR = [(CEILING, 0x10), (SHELF, 0x11), (FLOOR, 0x12)]


class GroundTests(unittest.TestCase):
    def test_a_shelf_under_a_low_ceiling_wins(self):
        t, _ = tools(BAR)
        self.assertEqual(t._ground(0, 0, 16835.0), SHELF)          # the array repro: just above the shelf

    def test_a_point_on_the_floor_finds_the_floor(self):
        t, _ = tools(BAR)
        self.assertEqual(t._ground(500, 0, FLOOR + 2), FLOOR)

    def test_a_point_just_under_a_curb_takes_the_curb(self):
        t, _ = tools([(12.0, 0x50), (-1.0, 0x51)])                 # round-4 H1: a sidewalk over the terrain
        self.assertEqual(t._ground(0, 0, 0.0), 12.0)

    def test_a_point_at_the_foot_of_stairs_takes_the_tread(self):
        t, _ = tools([(40.0, 0x52), (0.0, 0x53)])                  # round-4 H1: a tread over the floor
        self.assertEqual(t._ground(0, 0, 0.0), 40.0)

    def test_a_floor_it_is_sunk_into_still_counts(self):
        t, _ = tools([(130.0, 0x30)])                               # nothing under the floor
        self.assertEqual(t._ground(0, 0, 0.0), 130.0)

    def test_a_point_in_mid_air_drops_to_the_floor_not_the_nearer_ceiling(self):
        t, _ = tools([(16960.0, 0x10), (16704.0, 0x12)])           # live: 125 under the ceiling, 131 over the floor
        self.assertEqual(t._ground(0, 0, 16835.0), 16704.0)

    def test_an_object_sunk_into_a_floor_with_a_cellar_far_below(self):
        t, _ = tools([(40.0, 0x99), (30.0, 0x30), (-400.0, 0x31)])  # its top pokes out of the floor 30 above it
        self.assertEqual(t._ground(0, 0, 0.0, ignore=0x99, reach=250.0, sink=50.0), 30.0)

    def test_an_object_with_no_collision_sunk_less_than_the_sink(self):
        t, _ = tools([(30.0, 0x30), (-400.0, 0x31)])
        self.assertEqual(t._ground(0, 0, 0.0, ignore=0x99, reach=250.0, sink=50.0), 30.0)

    def test_a_low_ceiling_over_an_object_is_skipped(self):
        t, _ = tools([(SHELF + 60, 0x20), (SHELF + 12, 0x99), (SHELF, 0x11)])   # a shelf 60 over a bottle
        self.assertEqual(t._ground(0, 0, SHELF, ignore=0x99, reach=250.0, sink=50.0), SHELF)

    def test_only_a_ceiling_over_the_object_is_nothing_under_it(self):
        t, _ = tools([(CEILING, 0x10), (SHELF + 12, 0x99)])
        self.assertIsNone(t._ground(0, 0, SHELF, ignore=0x99, reach=250.0, sink=50.0))

    def test_its_own_collision_is_passed_through(self):
        t, _ = tools([(SHELF + 12, 0x99), (CEILING, 0x10), (SHELF, 0x11)])
        self.assertEqual(t._ground(0, 0, SHELF, ignore=0x99, reach=250.0), SHELF)

    def test_nothing_under_it(self):
        t, _ = tools([])
        self.assertIsNone(t._ground(0, 0, 0.0))

    def test_the_ray_still_starts_reach_above(self):
        t, rays = tools([(100.0, 0x40)])
        self.assertEqual(t._ground(0, 0, 0.0), 100.0)              # lifted out of a hill 100 above, as before
        self.assertEqual(rays.casts[0][0][2], 200.0)


class CheckTests(unittest.TestCase):
    def check(self, planes, z):
        t, _ = tools(planes)
        t.live = SimpleNamespace(in_game=lambda: None, resolve=lambda ref: {'ptr': 0x99, 'formId': '0x00ABCDEF'})
        t._pose = lambda ptr: {'position': [0.0, 0.0, z], 'rotation': [0, 0, 0], 'scale': 1.0}
        return t.check('0x00ABCDEF')

    def test_a_bottle_on_the_shelf_is_fine(self):
        out = self.check(BAR, 16831.0)                              # the check repro
        self.assertEqual(out['surface'], SHELF)
        self.assertEqual(out['issues'], [])

    def test_a_bottle_sunk_into_the_shelf_is_reported(self):
        out = self.check(BAR, SHELF - 20)
        self.assertEqual(out['surface'], SHELF)
        self.assertIn('below the surface under it by 20 units', out['issues'])

    def test_a_floating_bottle_is_reported(self):
        out = self.check(BAR, SHELF + 60)
        self.assertEqual(out['surface'], SHELF)
        self.assertIn('floats 60 units above the surface under it', out['issues'])


class SnapTests(unittest.TestCase):
    def test_snap_moves_onto_the_shelf_not_the_ceiling(self):
        t, _ = tools(BAR)
        t.live = SimpleNamespace(in_game=lambda: None, resolve=lambda ref: {'ptr': 0x99, 'formId': '0x00ABCDEF'})
        t._pose = lambda ptr: {'position': [0.0, 0.0, SHELF + 4], 'rotation': [0, 0, 0], 'scale': 1.0}
        moves = []
        t.adjust = lambda ref, move=None, rotate=None: moves.append(move) or {'ref': ref}
        out = t.snap('0x00ABCDEF')
        self.assertTrue(out['snapped'])
        self.assertEqual(out['ground'], SHELF)
        self.assertEqual(moves, [[0, 0, -4.0]])



class BuriedTests(unittest.TestCase):
    """Round-4 M1: a wholly buried object cannot be told from one under a ceiling by vertical rays; it counts as over
    what is under it, and check names the surface just over it."""

    def check(self, planes, z):
        t, _ = tools(planes)
        t.live = SimpleNamespace(in_game=lambda: None, resolve=lambda ref: {'ptr': 0x99, 'formId': '0x00ABCDEF'})
        t._pose = lambda ptr: {'position': [0.0, 0.0, z], 'rotation': [0, 0, 0], 'scale': 1.0}
        return t.check('0x00ABCDEF')

    def test_a_buried_object_is_named_as_under_a_surface(self):
        out = self.check([(60.0, 0x30), (10.0, 0x99)], 0.0)
        self.assertIsNone(out['surface'])
        self.assertIn('a surface 60 units over it: a low ceiling or shelf, or it is buried under it', out['issues'])

    def test_a_bottle_under_a_high_ceiling_gets_no_overhead_note(self):
        out = self.check([(CEILING, 0x10), (SHELF + 12, 0x99), (SHELF, 0x11)], SHELF)
        self.assertEqual(out['issues'], [])

    # round-4 review I1/M1: an overhead surface close over it with the ground well under it is ambiguous
    def test_buried_over_a_cellar_is_ambiguous_not_floating(self):
        out = self.check([(60.0, 0x30), (10.0, 0x99), (-400.0, 0x31)], 0.0)
        self.assertFalse([i for i in out['issues'] if i.startswith('floats')], out['issues'])
        self.assertTrue([i for i in out['issues'] if 'ambiguous' in i], out['issues'])

    def test_an_item_on_a_table_under_a_shelf_has_no_issues(self):
        out = self.check([(50.0, 0x20), (10.0, 0x99), (0.0, 0x21)], 0.0)
        self.assertEqual(out['issues'], [])


class AmbiguousSnapTests(unittest.TestCase):
    def snap(self, planes, z):
        t, _ = tools(planes)
        t.live = SimpleNamespace(in_game=lambda: None, resolve=lambda ref: {'ptr': 0x99, 'formId': '0x00ABCDEF'})
        t._pose = lambda ptr: {'position': [0.0, 0.0, z], 'rotation': [0, 0, 0], 'scale': 1.0}
        moves = []
        t.adjust = lambda ref, move=None, rotate=None: moves.append(move) or {'ref': ref}
        return t.snap('0x00ABCDEF'), moves

    def test_snap_does_not_push_a_buried_object_into_the_cellar(self):
        out, moves = self.snap([(60.0, 0x30), (10.0, 0x99), (-400.0, 0x31)], 0.0)
        self.assertFalse(out['snapped'])
        self.assertEqual(moves, [])
        self.assertIn('nudge', out['note'])

    def test_snap_does_not_push_an_object_sunk_into_a_slab_down_to_the_terrain(self):
        # no own hit: the slab is 60 over it, the terrain 1 under it (as for an object resting under a low shelf):
        # at most the harmless 1-unit settle, never a move down through what is under it
        out, moves = self.snap([(60.0, 0x30), (-1.0, 0x31)], 0.0)
        self.assertTrue(all(abs(m[2]) <= 5 for m in moves), moves)
        out, moves = self.snap([(60.0, 0x30), (-300.0, 0x31)], 0.0)        # the same slab over a deep hollow
        self.assertFalse(out['snapped'])
        self.assertEqual(moves, [])

    def test_snap_still_drops_an_object_from_far_under_a_ceiling(self):
        out, moves = self.snap([(CEILING, 0x10), (16862.0, 0x99), (16704.0, 0x12)], 16850.0)   # 110 under it
        self.assertTrue(out['snapped'])
        self.assertEqual(moves, [[0, 0, 16704.0 - 16850.0]])


PLAYER = 0x14


class EyeSideTests(unittest.TestCase):
    """Round-4 V2: buried under a surface or floating under a ceiling, told apart from the player's eye (a point in
    open air). The object hangs in the player's air when the segment eye -> object meets nothing: snap drops it. It is
    buried when the eye sees the point just over the surface above it and exactly one surface lies between the eye
    and the object: snap lifts it onto that surface. Anything else stays ambiguous. Room numbers are NPCTextureTest's
    (floor 16704, ceiling 16960); the eye stands on the floor, 300 units away."""

    EYE = (300.0, 0.0, 16804.0)

    def make(self, planes, z, eye=EYE):
        t, rays = tools(planes)
        t.live = SimpleNamespace(in_game=lambda: None, resolve=lambda ref: {'ptr': 0x99, 'formId': '0x00ABCDEF'})
        t._pose = lambda ptr: {'position': [0.0, 0.0, z], 'rotation': [0, 0, 0], 'scale': 1.0}
        t._eye = lambda: (PLAYER, eye)
        moves = []
        t.adjust = lambda ref, move=None, rotate=None: moves.append(move) or {'ref': ref}
        return t, moves

    UNDER_CEILING = [(CEILING, 0x10), (16912.0, 0x99), (16805.0, PLAYER, 280.0, 320.0),
                     (16704.0, 0x12)]                                          # 60 under it
    UNDER_FLOOR = [(16704.0, 0x12), (16672.0, 0x99), (16400.0, 0x31)]                         # 44 under it

    def test_snap_drops_an_object_floating_under_a_low_ceiling(self):
        t, moves = self.make(self.UNDER_CEILING, 16900.0)
        out = t.snap('0x00ABCDEF')
        self.assertTrue(out['snapped'], out)
        self.assertEqual(out['ground'], 16704.0)
        self.assertEqual(moves, [[0, 0, 16704.0 - 16900.0]])

    def test_snap_lifts_a_buried_object_onto_the_floor_over_it(self):
        t, moves = self.make(self.UNDER_FLOOR, 16660.0)
        out = t.snap('0x00ABCDEF')
        self.assertTrue(out['snapped'], out)
        self.assertTrue(out.get('buried'), out)
        self.assertEqual(out['ground'], 16704.0)
        self.assertEqual(moves, [[0, 0, 44.0]])

    def test_snap_lifts_an_object_buried_under_terrain_with_nothing_under_it(self):
        t, moves = self.make([(60.0, 0x30), (10.0, 0x99)], 0.0, eye=(300.0, 0.0, 160.0))
        out = t.snap('0x00ABCDEF')
        self.assertTrue(out['snapped'], out)
        self.assertEqual(moves, [[0, 0, 60.0]])

    def test_check_names_a_buried_object(self):
        t, _ = self.make(self.UNDER_FLOOR, 16660.0)
        out = t.check('0x00ABCDEF')
        self.assertEqual(out['surface'], 16704.0)
        self.assertEqual(out['issues'], ['buried 44 units under the surface over it'])   # live: once, not twice

    def test_check_names_an_object_floating_under_a_ceiling(self):
        t, _ = self.make(self.UNDER_CEILING, 16900.0)
        out = t.check('0x00ABCDEF')
        self.assertEqual(out['surface'], 16704.0)
        self.assertTrue([i for i in out['issues'] if i.startswith('floats 196 units above the surface under it')],
                        out['issues'])
        self.assertFalse([i for i in out['issues'] if 'ambiguous' in i or 'buried' in i], out['issues'])

    def test_a_closed_table_top_between_eye_and_object_stays_ambiguous(self):
        # the eye looks through both faces of a table top at an object floating under it: not one surface
        planes = [(40.0, 0x20), (36.0, 0x20), (12.0, 0x99), (-20.0, 0x21)]
        t, moves = self.make(planes, 0.0, eye=(300.0, 0.0, 80.0))
        out = t.snap('0x00ABCDEF')
        self.assertFalse(out['snapped'], out)
        self.assertEqual(moves, [])
        self.assertTrue([i for i in t.check('0x00ABCDEF')['issues'] if 'ambiguous' in i])

    def test_never_lifted_to_a_ceiling_the_eye_cannot_see_over(self):
        # LV1: a shelf hides the bottle from the eye; the point over the ceiling is not seen either: no move up
        planes = [(CEILING, 0x10), (16912.0, 0x99), (16850.0, 0x40), (16704.0, 0x12)]
        t, moves = self.make(planes, 16900.0)
        out = t.snap('0x00ABCDEF')
        self.assertFalse(out['snapped'], out)
        self.assertEqual(moves, [])

    def test_without_a_player_it_stays_ambiguous(self):
        t, moves = self.make(self.UNDER_FLOOR, 16660.0)
        t._eye = lambda: None
        out = t.snap('0x00ABCDEF')
        self.assertFalse(out['snapped'], out)
        self.assertEqual(moves, [])


if __name__ == '__main__':
    unittest.main()
