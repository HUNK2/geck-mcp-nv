"""Background cut-out of a Render Window capture (geck_image_cutout, geck_render_capture cutout=true). No GECK needed."""
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from geck_mcp import imaging


def synthetic(path, bg=(110, 110, 110)):
    a = np.zeros((300, 200, 3), np.uint8)
    a[:] = bg
    yy, xx = np.mgrid[0:300, 0:200]
    ring = (np.hypot(xx - 100, yy - 150) < 80)
    a[ring] = (118, 104, 100)                        # faint reddish glow halo
    a[80:220, 70:130] = (40, 35, 30)                 # dark subject
    a[60:80, 85:115] = (170, 110, 80)                # skin-toned head
    a[140:200, 20:40] = bg                           # pocket of background (should stay clear)
    Image.fromarray(a).save(path)


class CutoutTests(unittest.TestCase):
    def test_subject_opaque_background_and_halo_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / 'a.png', Path(tmp) / 'a-cutout.png'
            synthetic(src)
            info = imaging.cutout(src, dst, pad=5)
            out = np.asarray(Image.open(dst))
            self.assertEqual(out.shape[2], 4)
            self.assertEqual(info['background'], [110, 110, 110])
            # crop box: head top 60 - pad, subject bottom 220 + pad; x 70-5 .. 130+5
            self.assertEqual(info['cropBox'], [65, 55, 135, 225])
            x0, y0 = info['cropBox'][:2]
            self.assertEqual(out[150 - y0, 100 - x0, 3], 255)   # subject centre opaque
            self.assertEqual(out[70 - y0, 100 - x0, 3], 255)    # head opaque
            self.assertEqual(out[2, 2, 3], 0)                   # halo corner transparent

    def test_bbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'a.png'
            synthetic(src)
            rgb, _ = imaging.load_rgb(src)
            box, bg = imaging.subject_bbox(rgb)
            self.assertEqual(box, (70, 60, 130, 220))


class CornerTests(unittest.TestCase):
    def test_transparent_corners_are_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'w.png'
            a = np.zeros((300, 200, 4), np.uint8)
            a[:] = (110, 110, 110, 255)
            a[80:220, 70:130] = (40, 35, 30, 255)
            a[-6:, :6] = (0, 0, 0, 0)            # transparent corners
            a[-6:, -6:] = (0, 0, 0, 0)
            Image.fromarray(a, 'RGBA').save(src)
            rgb, _ = imaging.load_rgb(src)
            box, _ = imaging.subject_bbox(rgb)
            self.assertEqual(box, (70, 80, 130, 220))
            info = imaging.cutout(src, Path(tmp) / 'c.png', pad=0)
            self.assertEqual(info['cropBox'], [70, 80, 130, 220])


if __name__ == '__main__':
    unittest.main()
