"""Background cut-outs of GECK Render Window captures (geck_image_cutout, geck_render_capture cutout=true).
The capture itself is wincapture.py (Windows).
"""
from pathlib import Path


class ImagingError(RuntimeError):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code, self.evidence = code, evidence


# -- pixel work (Pillow + NumPy) ------------------------------------------------------------
def _np():
    try:
        import numpy as np
        from PIL import Image
    except ImportError as e:
        raise ImagingError('MISSING_DEPENDENCY', 'Install pillow and numpy into the bridge venv') from e
    return np, Image


def load_rgb(path, top_crop=0):
    """RGB int16 array. Transparent pixels (a window capture's rounded corners) take the colour of
    the opaque bottom band, so they read as background instead of black subject pixels."""
    np, Image = _np()
    image = Image.open(path)
    if top_crop:
        image = image.crop((0, int(top_crop), image.width, image.height))
    if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
        rgba = np.asarray(image.convert('RGBA')).astype(np.int16)
        rgb, opaque = rgba[..., :3].copy(), rgba[..., 3] >= 250
        if not opaque.all():
            band = rgb[-max(2, rgb.shape[0] // 50):][opaque[-max(2, rgb.shape[0] // 50):]]
            fill = np.median(band.reshape(-1, 3), axis=0) if len(band) else np.median(rgb[opaque], axis=0)
            rgb[~opaque] = fill.astype(np.int16)
        return rgb, image
    return np.asarray(image.convert('RGB')).astype(np.int16), image


def estimate_background(rgb):
    """Median colour of the image border (left, right and bottom bands)."""
    np, _ = _np()
    h, w, _c = rgb.shape
    band = max(2, min(h, w) // 50)
    border = np.concatenate([rgb[:, :band].reshape(-1, 3), rgb[:, -band:].reshape(-1, 3),
                             rgb[-band:, :].reshape(-1, 3)])
    return [int(v) for v in np.median(border, axis=0)]


def background_mask(rgb, bg, tolerance=30, chroma_tolerance=22):
    """True where a pixel is background: close to bg and with bg's hue balance (kills the glow halo)."""
    np, _ = _np()
    bg = np.array(bg, dtype=np.int16)
    distance = np.abs(rgb - bg).max(axis=2)
    tint = rgb - rgb.mean(axis=2, keepdims=True)
    bg_tint = bg - bg.mean()
    chroma = np.abs(tint - bg_tint).max(axis=2)
    return (distance < tolerance) & (chroma < chroma_tolerance), distance, chroma


def _shift_or(mask, radius):
    np, _ = _np()
    out = mask.copy()
    padded = np.pad(mask, radius)
    h, w = mask.shape
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            out |= padded[radius + dy:radius + dy + h, radius + dx:radius + dx + w]
    return out


def dilate(mask, radius=1):
    return _shift_or(mask, radius)


def erode(mask, radius=1):
    return ~_shift_or(~mask, radius)


def box_count(mask, radius):
    np, _ = _np()
    m = np.pad(mask.astype(np.int32), radius + 1)
    c = m.cumsum(0).cumsum(1)
    k = 2 * radius + 1
    h, w = mask.shape
    return (c[k:k + h, k:k + w] - c[:h, k:k + w] - c[k:k + h, :w] + c[:h, :w])


def subject_bbox(rgb, bg=None, tolerance=30, chroma_tolerance=22, min_neighbours=12):
    """Bounding box (x0, y0, x1, y1) of non-background pixels, ignoring isolated specks."""
    np, _ = _np()
    bg = bg or estimate_background(rgb)
    fg = ~background_mask(rgb, bg, tolerance, chroma_tolerance)[0]
    fg &= box_count(fg, 3) >= min_neighbours
    ys, xs = np.nonzero(fg)
    if len(xs) == 0:
        return None, bg
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1), bg


def cutout(src, dst, top_crop=0, bg=None, tolerance=30, chroma_tolerance=22, pad=10):
    """Write an RGBA PNG containing only the subject; background and glow become transparent."""
    np, Image = _np()
    rgb, _ = load_rgb(src, top_crop)
    bg = bg or estimate_background(rgb)
    bglike, distance, chroma = background_mask(rgb, bg, tolerance, chroma_tolerance)
    keep = ~bglike
    keep &= box_count(keep, 3) >= 12            # drop specks and JPEG-like noise
    keep = keep | (erode(dilate(keep, 1), 1) & ~bglike)   # close 1px cracks, never re-add background
    keep = keep | (box_count(keep, 3) >= 40)            # fill tiny interior holes (eye glints), not real gaps
    inner = erode(keep, 1)
    outer = dilate(keep, 1)
    score = np.clip(np.maximum((distance - tolerance / 2.0) / (tolerance * 1.3),
                               (chroma - chroma_tolerance / 2.0) / (chroma_tolerance)), 0, 1)
    alpha = np.where(inner, 1.0, np.where(keep, np.maximum(score, 0.6), np.where(outer, score * 0.8, 0.0)))
    a = alpha[..., None]
    bgv = np.array(bg, dtype=np.float64)
    colour = np.clip(np.where(a > 0, (rgb - (1 - a) * bgv) / np.maximum(a, 1e-3), 0), 0, 255)
    rgba = np.dstack([colour, alpha * 255]).astype(np.uint8)
    image = Image.fromarray(rgba, 'RGBA')
    box = image.getbbox()
    if box is None:
        raise ImagingError('EMPTY_CUTOUT', 'No subject pixels differ from the background', {'background': bg})
    box = (max(box[0] - pad, 0), max(box[1] - pad, 0), min(box[2] + pad, image.width), min(box[3] + pad, image.height))
    image = image.crop(box)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    image.save(dst)
    return {'path': str(dst), 'size': [image.width, image.height], 'background': bg,
            'cropBox': list(box), 'mode': 'RGBA'}


def crop_png(src, dst, box):
    _, Image = _np()
    image = Image.open(src)
    image.crop(tuple(int(v) for v in box)).save(dst)
    return {'path': str(dst), 'size': [int(box[2] - box[0]), int(box[3] - box[1])]}
