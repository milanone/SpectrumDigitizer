"""SpectrumDigitizer - turn an image of a spectrum plot (PNG/JPG) into numbers.

Open the image, calibrate the axes by clicking two points of known value on each, draw a box
around every curve to extract, and export a CSV with one column per curve on a regular X grid
(readable by LabSpectrumManager through File -> Open). Curves can be dark lines/dots (Origin-style
plots) or coloured (pick the colour with a click); stacked, framed panels can be found
automatically.

The extraction functions at the top of the file do not need Tk, so the tests can import them.
"""
import csv
import io
import json
import re
import os
import sys
import zipfile

import numpy as np
from PIL import Image, ImageDraw, ImageTk
from scipy import ndimage as ndi
from scipy.signal import savgol_filter

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.patches import Circle, Polygon, Rectangle
from matplotlib.widgets import RectangleSelector

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False

def _system_decimal():
    """The decimal separator of the operating system's number format ('.' if it cannot be read)."""
    import locale
    old = locale.setlocale(locale.LC_NUMERIC)
    try:
        locale.setlocale(locale.LC_NUMERIC, '')
        return locale.localeconv().get('decimal_point') or '.'
    except Exception:
        return '.'
    finally:
        try:
            locale.setlocale(locale.LC_NUMERIC, old)
        except Exception:
            pass


SESSION_EXT = '.sdsession'   # a zip: session.json + image.png
SESSION_VERSION = 1
PALETTE = ['#1f77b4', '#d62728', '#2ca02c', '#ff7f0e', '#9467bd', '#8c564b', '#e377c2', '#17becf']


# ----------------------------------------------------------------------------------------
# Extraction core (no Tk)
# ----------------------------------------------------------------------------------------

def groups(idx):
    """Split a sorted index array into runs of consecutive values."""
    if len(idx) == 0:
        return []
    return np.split(idx, np.where(np.diff(idx) > 1)[0] + 1)


def find_frame(dark, frac=0.3):
    """Find the plot frame of an Origin-style figure.

    `dark` is a boolean image. Long dark lines (more than `frac` of the image size) are the
    frame: returns (left, right, rows) as runs of pixel indices - first and last vertical line,
    every horizontal line (stacked panels share their horizontal lines) - or None."""
    h, w = dark.shape
    cols = groups(np.where(dark.sum(0) > frac * h)[0])
    rows = groups(np.where(dark.sum(1) > frac * w)[0])
    if len(cols) < 2 or len(rows) < 2:
        return None
    return cols[0], cols[-1], rows


def find_zero(dark, left, top, bottom):
    """Pixel row of the zero tick drawn just left of the frame, within one panel (or None)."""
    x1 = left[0] - 3
    x0 = max(0, x1 - 22)
    seg = dark[top:bottom, x0:x1]
    if seg.size == 0:
        return None
    rows = groups(np.where(seg.sum(1) >= 0.7 * seg.shape[1])[0])
    if not rows:
        return None
    # Several ticks: the zero is the main (thickest) one, the lowest if there is a tie.
    g = max(rows, key=lambda r: (len(r), r.mean()))
    return top + g.mean()


def curve_mask(crop, color, tol, thresh):
    """Boolean mask of the curve pixels in an RGB crop, plus a per-pixel weight for centroids.

    color=None selects dark pixels (gray < thresh); color='any' every pixel that is not white (RGB
    distance from white above `tol`: a trace of mixed shades, e.g. after the image was cleaned);
    otherwise pixels whose RGB distance from `color` is below `tol`."""
    if isinstance(color, str) and color == 'any':
        dist = np.sqrt(((255.0 - crop) ** 2).sum(axis=2))
        return dist > tol, dist / 441.7 + 0.05
    if color is None:
        gray = crop @ np.array([0.299, 0.587, 0.114])
        return gray < thresh, 255.0 - gray
    dist = np.sqrt(((crop - np.asarray(color, float)) ** 2).sum(axis=2))
    return dist < tol, (1.0 - dist / tol) + 0.05


def strip_inner_ticks(mask, rule_flags, axis, max_width=4):
    """Erase, in place, the axis ticks that point from a ruling line into the box.

    `rule_flags` marks the (dilated) ruling rows (axis=0) or columns (axis=1) already blanked in `mask`.
    From each side of a ruling line, a tick is a bundle of at most `max_width` adjacent lines (tick thickness grows with the image size) (columns for a
    horizontal rule) whose run of mask pixels, starting right at the rule, is 2..tick_max long. A curve
    that follows the rule gives such runs in every column (wide group): it is left alone."""
    view = mask if axis == 0 else mask.T        # rule lines are rows of `view`
    n, m = view.shape
    tick_max = max(10, int(0.05 * min(mask.shape)))
    for run in groups(np.nonzero(rule_flags)[0]):
        for step, start in ((1, run[-1] + 1), (-1, run[0] - 1)):
            lengths = np.zeros(m, int)
            for k in range(tick_max + 1):
                r = start + step * k
                if r < 0 or r >= n:
                    break
                alive = view[r, :] & (lengths == k)
                lengths[alive] += 1
            cand = np.nonzero((lengths >= 2) & (lengths <= tick_max))[0]
            for g in groups(cand):
                if len(g) > max_width:
                    continue
                lo, hi = g[0], g[-1]
                flank = np.r_[lengths[max(lo - 2, 0):lo], lengths[hi + 1:hi + 3]]
                if len(flank) and flank.max() >= 2:
                    continue            # neighbours also have pixels there: part of a curve, not a stub
                for c in g:
                    rs = [start + step * k for k in range(lengths[c])]
                    view[rs, c] = False


def corridor_mask(shape, guide, x0=0, y0=0):
    """Boolean mask (h, w) of the pixels under the brush strokes in `guide` = [(points, width)], where `points`
    is an (n, 2) array of (x, y) image coordinates and `width` the brush diameter in pixels. (x0, y0) is the
    image position of the mask's top-left pixel, for a crop. A stroke is the union of discs along its path
    (the path is densified first, so a fast mouse move leaves no gap)."""
    h, w = shape
    img = Image.new('L', (w, h), 0)
    draw = ImageDraw.Draw(img)
    for pts, width in guide:
        pts = np.asarray(pts, float)
        if len(pts) == 0:
            continue
        r = max(float(width), 1.0) / 2
        dense = [pts[0]]
        for a, b in zip(pts[:-1], pts[1:]):
            n = max(int(np.hypot(*(b - a)) / max(r / 2, 0.5)), 1)
            dense.extend(a + (b - a) * (k / n) for k in range(1, n + 1))
        for x, y in dense:
            draw.ellipse([x - x0 - r, y - y0 - r, x - x0 + r, y - y0 + r], fill=255)
    return np.asarray(img) > 0


def resample_path(pts, step=2.0):
    """Points spaced `step` pixels along the arc length of the polyline `pts` (n, 2); both ends are kept."""
    pts = np.asarray(pts, float)
    if len(pts) < 2:
        return pts.copy()
    seg = np.hypot(*np.diff(pts, axis=0).T)
    keep = np.r_[True, seg > 1e-9]                   # repeated points would break the interpolation
    pts = pts[keep]
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(pts, axis=0).T))]
    if len(pts) < 2 or s[-1] <= 0:
        return pts[:1].copy()
    t = np.linspace(0, s[-1], max(int(round(s[-1] / step)), 1) + 1)
    return np.c_[np.interp(t, s, pts[:, 0]), np.interp(t, s, pts[:, 1])]


def smooth_path(pts, sigma=2.0):
    """Gaussian smoothing of a path (sigma in samples), the two ends stay where they are: a shaky hand-drawn
    stroke becomes a smooth line."""
    pts = np.asarray(pts, float)
    if len(pts) < 3:
        return pts.copy()
    out = ndi.gaussian_filter1d(pts, sigma, axis=0, mode='nearest')
    out[0], out[-1] = pts[0], pts[-1]
    return out


def reshape_path(pts, i0, d, reach):
    """Pull the vertex `i0` of a path by `d` = (dx, dy); the other vertices follow with a smooth falloff
    (cosine bell) that reaches zero `reach` pixels away along the path, so the rest of the path stays put
    and the bend is rounded."""
    pts = np.asarray(pts, float)
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(pts, axis=0).T))]
    dist = np.abs(s - s[i0])
    w = np.where(dist < reach, 0.5 * (1 + np.cos(np.pi * dist / max(reach, 1e-9))), 0.0)
    return pts + w[:, None] * np.asarray(d, float)


def nice_ticks(lo, hi, n=10):
    """About `n` round values (1, 2 or 5 times a power of ten) between lo and hi, ascending: the lines of the
    check grid."""
    lo, hi = float(min(lo, hi)), float(max(lo, hi))
    if not np.isfinite(lo + hi) or hi <= lo:
        return np.array([])
    raw = (hi - lo) / n
    mag = 10.0 ** np.floor(np.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw * (1 - 1e-9))
    first = np.ceil(lo / step - 1e-9) * step
    return first + step * np.arange(int((hi - first) / step + 1e-9) + 1)


def guided_rules(rgb, color, tol, thresh, y0, x0, h, w):
    """Ruling rows / columns (boolean arrays over the crop that starts at (x0, y0), h x w) for a brush-guided
    extraction: lines of the curve mask that cross at least half of the whole image."""
    H, W = rgb.shape[:2]
    full, _ = curve_mask(rgb, color, tol, thresh)
    rows, cols = np.zeros(h, bool), np.zeros(w, bool)
    for run in groups(np.nonzero(full.sum(axis=1) >= 0.5 * W)[0]):
        rows[max(run[0] - y0, 0):max(min(run[-1] - y0 + 1, h), 0)] = True
    for run in groups(np.nonzero(full.sum(axis=0) >= 0.5 * H)[0]):
        cols[max(run[0] - x0, 0):max(min(run[-1] - x0 + 1, w), 0)] = True
    return rows, cols


def extract_points(rgb, roi, color=None, thresh=128, tol=60, dot_max_frac=0.012, exclude=(), guide=()):
    """Curve points (x_px, y_px) found inside `roi`, sorted by x: see _extract_impl. With a brush `guide` the
    ruling lines are removed first; if that leaves (almost) nothing of what the brush covers, the curve itself
    was the long line (a flat curve traced on purpose), and the extraction is repeated keeping them."""
    xs, ys = _extract_impl(rgb, roi, color, thresh, tol, dot_max_frac, exclude, guide, True)
    if guide:
        span = max(float(np.ptp(np.vstack([np.asarray(g[0], float) for g in guide])[:, 0])), 1.0)
        if len(np.unique(np.round(xs))) < 0.3 * span:
            xs2, ys2 = _extract_impl(rgb, roi, color, thresh, tol, dot_max_frac, exclude, guide, False)
            if len(xs2) > len(xs):
                return xs2, ys2
    return xs, ys


def _extract_impl(rgb, roi, color, thresh, tol, dot_max_frac, exclude, guide, rules):
    """Curve points (x_px, y_px) found inside `roi` = (x0, y0, x1, y1), sorted by x.

    Isolated blobs up to `dot_max_frac` of the image width are single dots and give one
    centroid each; larger blobs (continuous curve, or dots merged together) give one centroid
    per pixel column. Thin vertical slivers (leftover frame/ticks) and `exclude` boxes (panel
    letters...) are ignored. With a `guide` (brush strokes, see corridor_mask) only the pixels under the
    brush count: it separates a curve from the ones crossing it or running close. The box is then only
    the bounding box of the brush, so ruling lines (axes, frame, grid) are looked for in the whole image
    (a row or column of the curve mask over half of the image) and removed like in the box mode when
    `rules` is set: a curve that falls onto an axis must not be replaced by the axis. Returns two empty
    arrays if nothing is found."""
    H, W = rgb.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in roi]
    x0, x1 = max(0, min(x0, x1)), min(W, max(x0, x1))
    y0, y1 = max(0, min(y0, y1)), min(H, max(y0, y1))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return np.array([]), np.array([])
    mask, wgt = curve_mask(rgb[y0:y1, x0:x1], color, tol, thresh)
    for ex0, ey0, ex1, ey1 in exclude:
        a, b = max(int(round(min(ey0, ey1))) - y0, 0), max(int(round(max(ey0, ey1))) - y0, 0)
        c, d = max(int(round(min(ex0, ex1))) - x0, 0), max(int(round(max(ex0, ex1))) - x0, 0)
        mask[a:b, c:d] = False
    # Frame lines and grid lines inside the box: a row (column) that is dark over almost the whole box
    # is a ruling line, not the curve. Removing it also detaches the curve where it touches the frame
    # (otherwise the frame and the curve are one blob and every column gets a spurious point).
    h, w = mask.shape
    if guide:
        rule_rows, rule_cols = guided_rules(rgb, color, tol, thresh, y0, x0, h, w) if rules else (
            np.zeros(h, bool), np.zeros(w, bool))
    else:
        rule_rows = mask.sum(axis=1) >= 0.7 * w
        rule_cols = mask.sum(axis=0) >= 0.7 * h
    # Whatever lies outside a frame line near the border of the box (axis ticks, tick labels) is not
    # the curve either.
    for flags, axis in ((rule_cols, 1), (rule_rows, 0)):
        idx = np.nonzero(flags)[0]
        if len(idx) == 0:
            continue
        n = len(flags)
        runs = groups(idx)
        view = mask if axis == 0 else mask.T      # index along the axis being trimmed comes first
        if runs[0][0] < 0.15 * n:
            view[:runs[0][0], :] = False
        if runs[-1][-1] > 0.85 * n:
            view[runs[-1][-1] + 1:, :] = False
    # one more row/column on each side: the anti-aliased edge of a ruling line
    rule_rows = ndi.binary_dilation(rule_rows) if rule_rows.any() else rule_rows
    rule_cols = ndi.binary_dilation(rule_cols) if rule_cols.any() else rule_cols
    mask[rule_rows, :] = False
    mask[:, rule_cols] = False
    # Ticks pointing into the box (inner ticks) would otherwise be taken for dots or bend the curve.
    tick_w = max(4, int(0.01 * W))
    strip_inner_ticks(mask, rule_rows, 0, tick_w)
    strip_inner_ticks(mask, rule_cols, 1, tick_w)
    if guide:
        mask &= corridor_mask(mask.shape, guide, x0, y0)
    lab, _ = ndi.label(mask)
    dot_max = dot_max_frac * W
    pts = []
    for i, sl in enumerate(ndi.find_objects(lab), 1):
        ys, xs = np.nonzero(lab[sl] == i)
        ys, xs = ys + sl[0].start, xs + sl[1].start
        if len(ys) < 4:
            continue
        w = wgt[ys, xs]
        bw, bh = np.ptp(xs) + 1, np.ptp(ys) + 1
        if bw <= 0.004 * W and bh > dot_max:
            continue  # vertical sliver: leftover of the frame or of a tick
        if max(bw, bh) <= dot_max:  # single dot
            pts.append((np.average(xs, weights=w) + x0, np.average(ys, weights=w) + y0))
        else:  # continuous curve (or merged dots): centroid per column
            for x in np.unique(xs):
                m = xs == x
                pts.append((x + x0, np.average(ys[m], weights=w[m]) + y0))
    if not pts:
        return np.array([]), np.array([])
    pts = one_point_per_column(np.array(sorted(pts)))
    return pts[:, 0], pts[:, 1]


def one_point_per_column(pts):
    """Several blobs can give a point at the same X (a panel letter, a tick, a second line crossing
    the curve): keep, in those columns, the point closest to the nearest column that has a single
    one, i.e. the one that continues the curve. `pts` is an (n, 2) array sorted by x."""
    cols = np.round(pts[:, 0]).astype(int)
    uniq, inv, counts = np.unique(cols, return_inverse=True, return_counts=True)
    if (counts == 1).all():
        return pts
    single = counts[inv] == 1
    if not single.any():  # every column is ambiguous: follow the median height
        ref_x, ref_y = pts[:, 0], np.full(len(pts), np.median(pts[:, 1]))
    else:
        ref_x, ref_y = pts[single, 0], pts[single, 1]
    keep = np.ones(len(pts), bool)
    for col in uniq[counts > 1]:
        idx = np.nonzero(cols == col)[0]
        ref = ref_y[np.argmin(np.abs(ref_x - col))]
        best = idx[np.argmin(np.abs(pts[idx, 1] - ref))]
        keep[idx] = False
        keep[best] = True
    return pts[keep]


def lin_map(cal, p):
    """Map pixel coordinates to values with a two-point calibration ((p1, v1), (p2, v2))."""
    (p1, v1), (p2, v2) = cal
    return v1 + (np.asarray(p, float) - p1) * (v2 - v1) / (p2 - p1)


def trace_values(xpx, ypx, xcal, ycal, zero, roi_bottom):
    """Convert pixel points to (x, y) values, sorted by ascending x.

    With a Y calibration y is calibrated; otherwise y is the height in pixels above the
    trace's zero line (the zero tick of the panel, or the bottom of its box)."""
    x = lin_map(xcal, xpx)
    if ycal is not None:
        y = lin_map(ycal, ypx)
    else:
        y = (roi_bottom if zero is None else zero) - np.asarray(ypx, float)
    order = np.argsort(x)
    return x[order], y[order]


def make_grid(lo, hi, step):
    """Regular grid of multiples of `step` inside [lo, hi] (integer multiples: no drift)."""
    n0 = int(np.ceil(lo / step - 1e-9))
    n1 = int(np.floor(hi / step + 1e-9))
    return np.arange(n0, n1 + 1) * step


def resample(x, y, grid, tol):
    """Linear resampling onto `grid`; NaN where more than `tol` outside the data."""
    v = np.interp(grid, x, y)
    v[(grid < x.min() - tol) | (grid > x.max() + tol)] = np.nan
    return v


def smooth_values(v, window, poly):
    """Savitzky-Golay smoothing of the finite part of `v` (window in points, adjusted to a
    valid odd size; left untouched if there are too few points)."""
    ok = np.isfinite(v)
    n = int(ok.sum())
    win = int(window)
    if win <= poly:
        win = poly + 1
    if win % 2 == 0:
        win += 1
    if win > n:
        win = n if n % 2 else n - 1
    if win <= poly:
        return v
    out = v.copy()
    out[ok] = savgol_filter(v[ok], win, poly)
    return out


# ----------------------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------------------

LOUPE_SIZE = 168      # pixels of the magnifier widget
LOUPE_RADIUS = 14     # image pixels shown on each side of the cursor


class SpectrumDigitizer:
    def __init__(self, root):
        self.root = root
        root.title("Spectrum Digitizer")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1400, int(sw * 0.92)), min(880, int(sh * 0.85))
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max((sh - h) // 3, 0)}")

        self.image_path = None
        self.image_dpi = None
        self.rgb = None
        self.pil = None
        # Calibration: two points per axis; the pixel position is clicked on the image, the value
        # is typed in the panel. 'x1','x2' hold a pixel column, 'y1','y2' a pixel row.
        self.cal_px = {k: None for k in ('x1', 'x2', 'y1', 'y2')}
        self.cal_var = {k: tk.StringVar() for k in self.cal_px}
        self.traces = []          # dicts: name, roi, color, zero, exclude, xpx, ypx
        self.mode = None          # 'pick' | 'color' | 'zero' | 'roi' | 'excl' | 'erase' | 'keep' | 'brush' | 'straighten' | 'guide' | None
        self.rgb_undo = []        # previous states of self.rgb (colour filter), newest last
        self.rgb_redo = []        # states undone, newest last; emptied by any new edit
        self.session_path = None  # file of the session in use (File > Save Session), else None
        self.im = None            # the AxesImage showing self.rgb
        self.work_box = None      # working area (x0, y0, x1, y1) the colour filter is limited to; None = whole image
        self._stroke = None       # brush path being drawn (or continued) for the semi-automatic tracing: list of (x, y)
        self._stroke_src = None   # when it continues a stored stroke: (index, number of old points, end side)
        self._gdrag = None        # a stretch of the path being pulled: dict(k, i, pts0, p0, moved)
        self._guide_target = None # curve whose brush path is being edited (None: the next stroke makes a new curve)
        self._guide_hist = []     # snapshots for the brush 'Undo'
        self._guide_redo = []     # states undone, for the brush 'Redo'; emptied by a new brush edit
        self._guide_cursor = ''
        self.v_bwidth = tk.StringVar(value='14')   # brush width of the semi-automatic tracing, image pixels
        self.v_speck = tk.StringVar(value='6')     # despeckle: blobs with fewer pixels than this are removed
        self.v_grid = tk.BooleanVar(value=False)   # draw the check grid of the calibration over the image
        self.v_reach = tk.StringVar(value='60')    # how far along the path a pulled point drags its neighbours, pixels
        self._straight_pts = []   # points clicked so far for 'Straighten' (image coordinates)
        self._brush_last = None   # last point of the eraser stroke in progress (image coordinates), else None
        self._brush_circle = None # circle showing the eraser size under the cursor
        self.v_brush = tk.StringVar(value='6')   # eraser diameter, image pixels
        self._work_drag = None    # drag of a working-area handle in progress: dict(name, sx, sy, frame, p0)
        self._work_cursor = ''    # cursor currently set by hovering over a handle
        self._angle_busy = False  # True while a drag sets v_angle (the trace must not redraw everything)
        self.v_angle = tk.StringVar(value='0')   # rotation of the working area about its centre, degrees counter-clockwise
        self._filter_pts = []     # clicks / small boxes collected for the next erase/keep: dicts (box or x, y; colors)
        self._press_xy = None     # (display x, y, data x, y) of a press in an erase/keep mode, to tell click from drag
        self._pick_key = None
        self._overlay = []
        self._update_job = None
        self._pan = None
        self._loupe_photo = None
        self._sticky = False      # True: keep the last status message instead of the generic hint
        self._nudge = None        # point the arrow keys move: ('cal', key) or ('zero', trace)
        self._last_xy = None      # last cursor position over the image (for the magnifier)

        menu = tk.Menu(root)
        fm = tk.Menu(menu, tearoff=0)
        fm.add_command(label="Open Image...", command=self.open_dialog)
        fm.add_command(label="Open Session...    Ctrl+O", command=self.open_session_dialog)
        fm.add_command(label="Save Session    Ctrl+S", command=self.save_session)
        fm.add_command(label="Save Session As...", command=lambda: self.save_session(ask=True))
        fm.add_command(label="Close Image", command=self.close_image)
        fm.add_command(label="Save Cleaned Image...", command=self.save_image)
        fm.add_command(label="Export CSV...", command=self.export_csv)
        fm.add_command(label="Copy Table to Clipboard", command=self.copy_table)
        fm.add_separator()
        fm.add_command(label="Exit", command=root.destroy)
        menu.add_cascade(label="File", menu=fm)
        root.config(menu=menu)
        root.bind('<Escape>', lambda e: self._on_escape())
        for key, redo in (('<Control-z>', False), ('<Control-y>', True), ('<Control-Z>', True)):
            root.bind(key, lambda e, redo=redo: self._on_ctrl_z(e, redo))
        root.bind('<Control-s>', lambda e: self.save_session())
        root.bind('<Control-o>', lambda e: self.open_session_dialog())
        root.bind('<Home>', lambda e: None if isinstance(e.widget, (tk.Entry, tk.Spinbox, tk.Text, ttk.Combobox))
                  else self._fit_view())
        # Arrow keys fine-tune the last point that was set (1 px; with Shift 0.2 px).
        for name, dx, dy in (('Left', -1, 0), ('Right', 1, 0), ('Up', 0, -1), ('Down', 0, 1)):
            root.bind(f'<{name}>', lambda e, dx=dx, dy=dy: self._on_arrow(e, dx, dy, False))
            root.bind(f'<Shift-{name}>', lambda e, dx=dx, dy=dy: self._on_arrow(e, dx, dy, True))
        if HAS_DND:
            root.drop_target_register(DND_FILES)
            root.dnd_bind('<<Drop>>', lambda e: self.open_path(root.tk.splitlist(e.data)[0].strip('{}')))

        paned = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=4)
        paned.pack(fill=tk.BOTH, expand=True)

        # Left: image (large) with a small live preview of the result underneath. No matplotlib
        # toolbar: its pan/zoom tools swallow the clicks of the calibration and trace tools, so
        # navigation is done directly (wheel = zoom, drag = move) and never conflicts with them.
        f_plot = tk.Frame(paned)
        # Two views of the left pane: the image (where everything is clicked) and a large plot of the result.
        self.nb_view = ttk.Notebook(f_plot)
        self.nb_view.pack(fill=tk.BOTH, expand=True)
        f_img = tk.Frame(self.nb_view)
        f_resplot = tk.Frame(self.nb_view)
        self.nb_view.add(f_img, text="  Image  ")
        self.nb_view.add(f_resplot, text="  Result plot  ")
        self.nb_view.bind('<<NotebookTabChanged>>', lambda e: self._on_view_changed())
        bar = tk.Frame(f_img)
        bar.pack(fill=tk.X)
        tk.Button(bar, text="Center and fit to window", command=self._fit_view).pack(side=tk.LEFT, padx=4, pady=2)
        tk.Button(bar, text="Zoom +", command=lambda: self._zoom_step(0.7)).pack(side=tk.LEFT, pady=2)
        tk.Button(bar, text="Zoom −", command=lambda: self._zoom_step(1 / 0.7)).pack(side=tk.LEFT, padx=4, pady=2)
        tk.Label(bar, text="Mouse wheel = zoom,  drag = move the image", fg='#666666').pack(side=tk.LEFT, padx=8)
        # Image and result preview are separate canvases in a vertical paned window, so the
        # boundary can be dragged (a tall image needs the room more than the preview does).
        vp = tk.PanedWindow(f_img, orient=tk.VERTICAL, sashrelief=tk.RAISED, sashwidth=6)
        vp.pack(fill=tk.BOTH, expand=True)
        self.fig = Figure(figsize=(7, 6))
        self.ax_img = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
        self.canvas = FigureCanvasTkAgg(self.fig, master=vp)
        vp.add(self.canvas.get_tk_widget(), stretch='always', minsize=150)
        self.fig_res = Figure(figsize=(7, 2))
        self.ax_res = self.fig_res.add_subplot(111)
        self.fig_res.subplots_adjust(left=0.07, right=0.99, top=0.95, bottom=0.2)
        self.canvas_res = FigureCanvasTkAgg(self.fig_res, master=vp)
        vp.add(self.canvas_res.get_tk_widget(), height=190, stretch='never', minsize=70)
        # The large result plot: here the matplotlib toolbar is welcome (zoom, pan, save as image) because no
        # calibration tool needs the clicks of this canvas.
        self.fig_big = Figure(figsize=(7, 5))
        self.ax_big = self.fig_big.add_subplot(111)
        self.canvas_big = FigureCanvasTkAgg(self.fig_big, master=f_resplot)
        tb = NavigationToolbar2Tk(self.canvas_big, f_resplot, pack_toolbar=False)
        tb.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas_big.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        for ev, fn in (('button_press_event', self._on_press), ('button_release_event', self._on_release),
                       ('motion_notify_event', self._on_motion), ('scroll_event', self._on_scroll)):
            self.canvas.mpl_connect(ev, fn)
        self.selector = RectangleSelector(self.ax_img, self._on_rect, useblit=True, button=[1],
                                          interactive=False, minspanx=4, minspany=4, spancoords='pixels',
                                          props=dict(facecolor='none', edgecolor='red', linestyle='--'))
        self.selector.set_active(False)
        paned.add(f_plot, width=980)

        self.f_right = tk.Frame(paned, bg='#f0f4f7')
        self._build_panel(self.f_right)
        paned.add(self.f_right, width=400)

        self._show_empty()
        self._update_hint()
        if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
            self.open_path(sys.argv[1])

    # ---- right panel ----
    def _build_panel(self, parent):
        bg = '#f0f4f7'
        self.status = tk.Label(parent, text="", bg='#fff3c4', wraplength=370, justify='left', anchor='w',
                               font=('Arial', 10), relief=tk.GROOVE, padx=8, pady=8)
        self.status.pack(fill=tk.X, padx=10, pady=(10, 4))
        # Packed first, at the bottom: the Export button must stay visible whatever the window height.
        tk.Button(parent, text="Export CSV...", command=self.export_csv, bg='#2ecc71', fg='white',
                  font=('Arial', 10, 'bold')).pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=8)

        lf = tk.Frame(parent, bg=bg)
        lf.pack(fill=tk.X, padx=10)
        self.loupe = tk.Label(lf, bg='#dddddd', width=LOUPE_SIZE // 8, height=LOUPE_SIZE // 16,
                              relief=tk.SUNKEN)
        self.loupe.pack(side=tk.LEFT)
        self.cursor_label = tk.Label(lf, text="Move the mouse over\nthe image to see\nthe magnifier.",
                                     bg=bg, justify='left', anchor='nw', font=('Consolas', 9), fg='#444444')
        self.cursor_label.pack(side=tk.LEFT, padx=8, anchor='n')

        # Two tabs: clean the image first, then digitize it. The status box, the magnifier and the Export
        # button are shared and stay outside the tabs.
        self.nb = ttk.Notebook(parent)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=6, pady=(8, 0))
        t_clean = tk.Frame(self.nb, bg=bg)
        t_dig = tk.Frame(self.nb, bg=bg)
        self.nb.add(t_clean, text="  Clean image  ")
        self.nb.add(t_dig, text="  Digitize  ")
        t_out = tk.Frame(self.nb, bg=bg)
        self.nb.add(t_out, text="  Output  ")
        self.nb.bind('<<NotebookTabChanged>>', lambda e: self._on_tab_changed())

        # 1. axes
        self.l_axes = tk.Label(t_dig, text="1. AXES", font=('Arial', 10, 'bold'), bg=bg, anchor='w')
        self.l_axes.pack(fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(t_dig, text="Pick a point of known value on the image, then type its value.",
                 bg=bg, fg='#555555', font=('Arial', 8), anchor='w').pack(fill=tk.X, padx=10)
        self.pick_buttons = {}
        self.px_labels = {}
        grid = tk.Frame(t_dig, bg=bg)
        grid.pack(fill=tk.X, padx=10, pady=2)
        self.cal_entries = {}
        names = {'x1': 'X point 1', 'x2': 'X point 2', 'y1': 'Y point 1 (optional)', 'y2': 'Y point 2 (optional)'}
        for r, key in enumerate(('x1', 'x2', 'y1', 'y2')):
            tk.Label(grid, text=names[key], bg=bg, anchor='w', width=19, font=('Arial', 9)).grid(row=r, column=0, sticky='w')
            b = tk.Button(grid, text="Pick", width=5, command=lambda k=key: self._start_pick(k))
            b.grid(row=r, column=1, padx=2, pady=1)
            self.pick_buttons[key] = b
            e = tk.Entry(grid, textvariable=self.cal_var[key], width=9)
            e.grid(row=r, column=2, padx=2)
            self.cal_entries[key] = e
            e.bind('<FocusIn>', lambda ev, k=key: self._set_nudge(('cal', k)))
            lab = tk.Label(grid, text="not set", bg=bg, fg='#999999', font=('Consolas', 8), width=14, anchor='w')
            lab.grid(row=r, column=3, padx=2)
            self.px_labels[key] = lab
            self.cal_var[key].trace_add('write', lambda *_: self._on_cal_edit())
        row = tk.Frame(t_dig, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Checkbutton(row, text="Show check grid on the image", variable=self.v_grid, bg=bg, font=('Arial', 8),
                       command=self._redraw).pack(side=tk.LEFT)
        tk.Button(row, text="Clear Y axis", command=lambda: self._clear_axis('y'), font=('Arial', 8)).pack(
            side=tk.RIGHT)

        # 2. curves
        tk.Label(t_dig, text="2. CURVES", font=('Arial', 10, 'bold'), bg=bg, anchor='w').pack(
            fill=tk.X, padx=10, pady=(6, 0))
        # The brush comes first: paint along the curve (stop and resume, pull back what went astray). The
        # box below is for a curve that has its own space.
        row = tk.Frame(t_dig, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Button(row, text="Brush: new curve", command=lambda: self._start_guide(False),
                  bg='#c8e6c9', font=('Arial', 9, 'bold')).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Brush: edit selected", command=lambda: self._start_guide(True)).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        row = tk.Frame(t_dig, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Label(row, text="width", bg=bg, font=('Arial', 8)).pack(side=tk.LEFT, padx=(2, 1))
        self._make_spin(row, self.v_bwidth, 'bwidth').pack(side=tk.LEFT)
        tk.Label(row, text="reach", bg=bg, font=('Arial', 8)).pack(side=tk.LEFT, padx=(6, 1))
        self._make_spin(row, self.v_reach, 'reach').pack(side=tk.LEFT)
        tk.Button(row, text="Undo", command=self.undo_stroke).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(6, 1))
        tk.Button(row, text="Redo", command=self.redo_stroke).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        tk.Button(row, text="Clear", command=self.clear_guide).pack(side=tk.LEFT, padx=1)
        self.v_bwidth.trace_add('write', lambda *_: self._on_bwidth())
        row = tk.Frame(t_dig, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(4, 0))
        tk.Button(row, text="Add curve (drag a box)", command=lambda: self._start_rect('roi')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Auto-detect panels", command=self.auto_panels,
                  bg='#e3f2fd').pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        self.listbox = tk.Listbox(t_dig, height=3, exportselection=False, font=('Arial', 9))
        self.listbox.pack(fill=tk.X, padx=10, pady=4)
        self.listbox.bind('<<ListboxSelect>>', lambda e: self._on_list_select())
        self.listbox.bind('<Double-Button-1>', lambda e: self.rename_trace())
        row = tk.Frame(t_dig, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Button(row, text="Rename", command=self.rename_trace).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Remove", command=self.remove_trace).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Label(t_dig, text="For the selected curve:", bg=bg, fg='#555555', font=('Arial', 8), anchor='w').pack(
            fill=tk.X, padx=10, pady=(4, 0))
        row = tk.Frame(t_dig, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Button(row, text="Pick its colour", command=lambda: self._start_point('color')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Black curve", command=self.use_dark).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Any colour", command=self.use_any).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        row = tk.Frame(t_dig, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Button(row, text="Ignore an area", command=lambda: self._start_rect('excl')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Set zero line", command=lambda: self._start_point('zero')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        # optional clean-up of the image by colour
        self.v_tol = tk.StringVar(value='60')
        tk.Label(t_clean, text="Optional: clean the image before digitizing it. Limit the tools to a working "
                 "area, erase or keep colours, retouch by hand.", bg=bg, fg='#555555', font=('Arial', 8),
                 anchor='w', justify='left', wraplength=360).pack(fill=tk.X, padx=10, pady=(8, 2))
        tk.Label(t_clean, text="Tilted scan? Straighten first (it clears calibration and curves):", bg=bg,
                 fg='#555555', font=('Arial', 8), anchor='w').pack(fill=tk.X, padx=10)
        row = tk.Frame(t_clean, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Button(row, text="Straighten: 2 points on an axis", command=self._start_straighten).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Straighten to working area", command=self.straighten_to_area).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Label(t_clean, text="Clean the image:", bg=bg, fg='#555555', font=('Arial', 8), anchor='w').pack(
            fill=tk.X, padx=10, pady=(6, 0))
        row = tk.Frame(t_clean, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Button(row, text="Set working area", command=lambda: self._start_rect('work')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Whole image", command=self.clear_work_box).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Erase an area", command=lambda: self._start_rect('wipe')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        row = tk.Frame(t_clean, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Label(row, text="Rotate the working area (°, counter-clockwise):", bg=bg, anchor='w',
                 font=('Arial', 8)).pack(side=tk.LEFT, padx=2)
        tk.Spinbox(row, from_=-180, to=180, increment=0.5, width=6, textvariable=self.v_angle,
                   command=self._redraw).pack(side=tk.LEFT)
        self.v_angle.trace_add('write', lambda *_: None if self._angle_busy else self._redraw())
        row = tk.Frame(t_clean, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Button(row, text="Crop to working area (cut away the outside)", command=self.crop_to_area).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        row = tk.Frame(t_clean, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Button(row, text="Erase colours", command=lambda: self._start_filter('erase')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Keep only colours", command=lambda: self._start_filter('keep')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        self.apply_btn = tk.Button(row, text="Apply", command=self.apply_filter, state=tk.DISABLED)
        self.apply_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        row = tk.Frame(t_clean, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Button(row, text="Undo  (Ctrl+Z)", command=self.undo_filter).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Redo  (Ctrl+Y)", command=self.redo_filter).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        row = tk.Frame(t_clean, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Button(row, text="Eraser (paint white)", command=self._start_brush).pack(side=tk.LEFT, padx=2)
        tk.Label(row, text="size (px)", bg=bg, font=('Arial', 8)).pack(side=tk.LEFT, padx=(6, 2))
        tk.Spinbox(row, from_=1, to=200, increment=1, width=5, textvariable=self.v_brush).pack(side=tk.LEFT)
        tk.Button(row, text="Flatten colours", command=self.flatten_colors).pack(side=tk.LEFT, padx=(10, 2))
        row = tk.Frame(t_clean, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Button(row, text="Despeckle", command=self.despeckle).pack(side=tk.LEFT, padx=2)
        tk.Label(row, text="remove specks under", bg=bg, font=('Arial', 8)).pack(side=tk.LEFT, padx=(6, 2))
        self._make_spin(row, self.v_speck, 'speck').pack(side=tk.LEFT)
        tk.Label(row, text="pixels", bg=bg, font=('Arial', 8)).pack(side=tk.LEFT, padx=2)
        row = tk.Frame(t_clean, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(row, text="Colour tolerance", bg=bg, font=('Arial', 9)).pack(side=tk.LEFT, padx=2)
        self._make_spin(row, self.v_tol, 'tol').pack(side=tk.LEFT)
        tk.Label(t_clean, text="RGB distance within which a shade counts as a clicked colour (or as non-white, "
                 "for 'Flatten colours').", bg=bg, fg='#777777', font=('Arial', 8), anchor='w', justify='left',
                 wraplength=360).pack(fill=tk.X, padx=12)
        tk.Button(t_clean, text="Save Cleaned Image...", command=self.save_image).pack(
            fill=tk.X, padx=12, pady=(12, 2))
        tk.Button(t_clean, text="Next: Digitize  →", command=lambda: self.nb.select(1), bg='#c8e6c9',
                  font=('Arial', 9, 'bold')).pack(fill=tk.X, padx=12, pady=(2, 8))

        # advanced extraction settings stay with the digitizing steps
        self.v_adv = tk.BooleanVar(value=False)
        self.adv_check = tk.Checkbutton(t_dig, text="Advanced extraction settings", variable=self.v_adv,
                                        bg=bg, anchor='w', command=self._toggle_advanced)
        self.adv_check.pack(fill=tk.X, padx=10, pady=(6, 0))
        self.f_adv = tk.Frame(t_dig, bg=bg)
        self.v_thresh = tk.StringVar(value='128')
        self.v_dot = tk.StringVar(value='1.2')
        for r, (label, var, kind) in enumerate((("Dark threshold (0-255)", self.v_thresh, 'thresh'),
                                                ("Colour tolerance", self.v_tol, 'tol'),
                                                ("Max single-dot size (% of width)", self.v_dot, 'dot'))):
            tk.Label(self.f_adv, text=label, bg=bg, anchor='w', font=('Arial', 9)).grid(row=r, column=0, sticky='w', pady=1)
            self._make_spin(self.f_adv, var, kind).grid(row=r, column=1, padx=6)
            var.trace_add('write', lambda *_: self._schedule(True))

        # Output tab: how the extracted points become the CSV (live on the result plot under the image)
        tk.Label(t_out, text="OUTPUT", font=('Arial', 10, 'bold'), bg=bg, anchor='w').pack(
            fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(t_out, text="The result plot under the image follows these settings live; drag the boundary "
                 "between image and plot to give more room to either.", bg=bg, fg='#555555', font=('Arial', 8),
                 anchor='w', justify='left', wraplength=360).pack(fill=tk.X, padx=10)
        self.v_step = tk.StringVar(value='1')
        self.v_smooth = tk.StringVar(value='0')
        self.v_poly = tk.StringVar(value='3')
        self.v_norm = tk.StringVar(value='none')
        self.v_points = tk.BooleanVar(value=True)
        for v in (self.v_step, self.v_smooth, self.v_poly):
            v.trace_add('write', lambda *_: self._schedule(False))
        grid = tk.Frame(t_out, bg=bg)
        grid.pack(fill=tk.X, padx=10, pady=4)
        tk.Label(grid, text="X step", bg=bg, anchor='w', font=('Arial', 9)).grid(row=0, column=0, sticky='w', pady=2)
        self._make_spin(grid, self.v_step, 'step').grid(row=0, column=1, padx=6)
        tk.Label(grid, text="Normalize", bg=bg, anchor='w', font=('Arial', 9)).grid(row=1, column=0, sticky='w')
        cb = ttk.Combobox(grid, textvariable=self.v_norm, values=['none', 'max'], width=6, state='readonly')
        cb.grid(row=1, column=1, padx=6, pady=2)
        cb.bind('<<ComboboxSelected>>', lambda e: self._redraw())
        # The smoothing tools are boxed together: they act on the curves as a group (Savitzky-Golay filter).
        box = tk.LabelFrame(t_out, text=" Smoothing (Savitzky-Golay) ", bg=bg, font=('Arial', 9, 'bold'),
                            fg='#1f4e79', bd=2, relief=tk.GROOVE, padx=8, pady=6)
        box.pack(fill=tk.X, padx=10, pady=(6, 4))
        tk.Label(box, text="Window (X units, 0 = off)", bg=bg, anchor='w', font=('Arial', 9)).grid(
            row=0, column=0, sticky='w', pady=2)
        self._make_spin(box, self.v_smooth, 'window').grid(row=0, column=1, padx=6)
        tk.Label(box, text="Polynomial order", bg=bg, anchor='w', font=('Arial', 9)).grid(
            row=1, column=0, sticky='w', pady=2)
        self._make_spin(box, self.v_poly, 'order').grid(row=1, column=1, padx=6)
        tk.Label(box, text="The arrows change the window by one X step, the order by one.", bg=bg, fg='#777777',
                 font=('Arial', 8), anchor='w', justify='left', wraplength=340).grid(
            row=2, column=0, columnspan=2, sticky='w')
        tk.Checkbutton(t_out, text="Show the extracted points on the image", variable=self.v_points,
                       bg=bg, command=self._redraw, anchor='w').pack(fill=tk.X, padx=10)
        self.info = tk.Label(t_out, text="", bg=bg, font=('Consolas', 9), fg='#555555',
                             wraplength=370, justify='left', anchor='w')
        self.info.pack(fill=tk.X, padx=10, pady=(10, 4))
        tk.Label(t_out, text="Export CSV (button below) writes one column per curve on the X grid.", bg=bg,
                 fg='#555555', font=('Arial', 8), anchor='w', justify='left', wraplength=360).pack(
            fill=tk.X, padx=10)
        row = tk.Frame(t_out, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(10, 0))
        tk.Button(row, text="Copy table to clipboard (for Excel)", command=self.copy_table).pack(side=tk.LEFT, padx=2)
        self.v_comma = tk.BooleanVar(value=_system_decimal() == ',')
        tk.Checkbutton(t_out, text="decimal comma (Excel with an Italian/European setup)", variable=self.v_comma,
                       bg=bg, font=('Arial', 8), anchor='w').pack(fill=tk.X, padx=10)

    def _make_spin(self, parent, var, kind):
        """Entry with up/down arrows (also the arrow keys and press-and-hold) for a number of the Output tab.
        The step depends on the field: 'step' walks the 1-2-5 sequence (0.5, 1, 2, 5, 10...), 'window' moves by
        one X step, 'order' by one. Tk's own increment is 0: the value is set here."""
        cmd = self.root.register(lambda d: self._spin_move(var, kind, 1 if d == 'up' else -1))
        v0 = var.get()
        sp = tk.Spinbox(parent, textvariable=var, width=8, from_=-1e9, to=1e9, increment=0, command=(cmd, '%d'))
        if var.get() != v0:                  # Tk rounds a value like 1.2 to 1 when it takes over the variable
            var.set(v0)
        return sp

    def _spin_move(self, var, kind, direction):
        x = self._to_float(var.get())
        if kind == 'step':
            x = x if x and x > 0 else 1.0
            cands = sorted(m * 10.0 ** e for e in range(int(np.floor(np.log10(x))) - 2, int(np.floor(np.log10(x))) + 3)
                           for m in (1, 2, 5))
            nxt = [c for c in cands if c > x * (1 + 1e-9)] if direction > 0 else [c for c in cands if c < x * (1 - 1e-9)]
            new = (nxt[0] if direction > 0 else nxt[-1]) if nxt else x
        elif kind == 'window':
            inc = self._num(self.v_step, 1.0, lo=1e-12)
            new = max(0.0, (x or 0.0) + direction * inc)
        elif kind == 'order':
            new = min(9, max(1, int(round(x or 3)) + direction))
        else:                                          # simple bounded numbers: (min, max, increment)
            lo, hi, inc = {'thresh': (0, 255, 5), 'tol': (1, 441, 5), 'bwidth': (2, 200, 2), 'dot': (0.1, 50, 0.1),
                           'reach': (10, 400, 10), 'speck': (1, 500, 1)}[kind]
            new = min(hi, max(lo, (lo if x is None else x) + direction * inc))
        var.set(f"{new:.10g}")

    def _clean_tab_active(self):
        nb = getattr(self, 'nb', None)
        return nb is None or nb.index(nb.select()) == 0

    def _on_tab_changed(self):
        """Switching tab ends the tool in progress; the working area (and its handles) belong to the clean tab."""
        self._nudge = None
        self._sticky = False
        self._cancel_tool(keep_status=True)
        # The Output tab acts on the result plot: show it large; the other tabs work on the image.
        want = 1 if self.nb.index(self.nb.select()) == 2 else 0
        if self.nb_view.index(self.nb_view.select()) != want:
            self.nb_view.select(want)           # its tab-changed handler redraws
        else:
            self._redraw()

    def _view_is_result(self):
        nb = getattr(self, 'nb_view', None)
        return nb is not None and nb.index(nb.select()) == 1

    def _on_view_changed(self):
        """A tool waits for a click on the image: leaving the image view ends it."""
        self._nudge = None
        self._cancel_tool(keep_status=True)
        self._redraw()

    def _toggle_advanced(self):
        if self.v_adv.get():
            self.f_adv.pack(fill=tk.X, padx=10, after=self.adv_check)
        else:
            self.f_adv.pack_forget()

    # ---- calibration ----
    @staticmethod
    def _to_float(text):
        """Number at the start of the text; decimal comma accepted, a trailing unit ('400 nm') ignored."""
        m = re.match(r'\s*([-+]?\d*[.,]?\d+(?:[eE][-+]?\d+)?)', text)
        return float(m.group(1).replace(',', '.')) if m else None

    def _cal(self, axis):
        """((p1, v1), (p2, v2)) if the two points and values of the axis are usable, else None."""
        p1, p2 = self.cal_px[axis + '1'], self.cal_px[axis + '2']
        v1, v2 = self._to_float(self.cal_var[axis + '1'].get()), self._to_float(self.cal_var[axis + '2'].get())
        if None in (p1, p2, v1, v2) or abs(p2 - p1) < 3 or v1 == v2:
            return None
        return (p1, v1), (p2, v2)

    def missing_reason(self):
        """Why there is no result yet, in words (None if there is one to compute)."""
        if self.rgb is None:
            return "No image loaded."
        problems = []
        for key in ('x1', 'x2'):
            n = key[1]
            if self.cal_px[key] is None:
                problems.append(f"X point {n} is not picked on the image")
            elif self._to_float(self.cal_var[key].get()) is None:
                problems.append(f"X point {n} has no value (type a number in its box)")
        if not problems:
            if self.xcal is None:
                problems.append("the two X points must be at different positions on the image, "
                                "with different values")
        if problems:
            return "X axis not calibrated: " + "; ".join(problems) + "."
        if not self.traces:
            return "No curve yet: press 'Add curve' and drag a box around a curve."
        if not any(len(t['xpx']) >= 2 for t in self.traces):
            return "No curve points found inside the box: draw it again around the curve."
        return None

    @property
    def xcal(self):
        return self._cal('x')

    @property
    def ycal(self):
        return self._cal('y')

    def _set_cal(self, axis, a, b):
        for key, (p, v) in ((axis + '1', a), (axis + '2', b)):
            self.cal_px[key] = float(p)
            self.cal_var[key].set(f"{v:g}")
        self._update_px_labels()

    def _clear_axis(self, axis):
        for key in (axis + '1', axis + '2'):
            self.cal_px[key] = None
            self.cal_var[key].set('')
        self._update_px_labels()
        self._redraw()

    def _update_px_labels(self):
        for key, lab in self.px_labels.items():
            p = self.cal_px[key]
            lab.config(text="not set" if p is None else f"{p:.1f} px",
                       fg='#999999' if p is None else '#006600')

    # ---- image ----
    def open_dialog(self):
        path = filedialog.askopenfilename(
            title="Open image", filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"), ("All files", "*.*")])
        if path:
            self.load_image(path)

    def open_path(self, path):
        """An image or a saved session, by extension (drop, command line)."""
        if path.lower().endswith(SESSION_EXT):
            self.open_session(path)
        else:
            self.load_image(path)

    def _set_image(self, img, path, dpi, title=None):
        """Show `img` (a PIL RGB image) as the new image: everything tied to the previous one is forgotten."""
        self.image_dpi = dpi            # kept for 'Save Cleaned Image'; None if the file has none
        self.image_path = path
        self.pil = img
        self.rgb = np.asarray(img, dtype=float)
        self.rgb_undo, self.rgb_redo = [], []
        self.work_box = None
        self.v_angle.set('0')
        self.traces = []
        for axis in ('x', 'y'):
            for key in (axis + '1', axis + '2'):
                self.cal_px[key] = None
                self.cal_var[key].set('')
        self._update_px_labels()
        self._nudge = None
        self._cancel_tool()
        self.ax_img.clear()
        self._brush_circle = None
        # The image is shown as it is (default interpolation: no pixel dropping when zoomed out).
        self.im = self.ax_img.imshow(self.rgb.astype(np.uint8))
        self.ax_img.set_aspect('equal', adjustable='box')
        self.ax_img.set_axis_off()
        self._overlay = []
        self.root.title(f"Spectrum Digitizer - {title or os.path.basename(path)}")
        self._refresh_list()
        self._fit_view()
        self._redraw()


    def load_image(self, path):
        try:
            src = Image.open(path)
            dpi = src.info.get('dpi')
            img = src.convert('RGB')
        except Exception as e:
            messagebox.showerror("Open image", f"Cannot open the image:\n{e}")
            return
        self.session_path = None
        self._set_image(img, path, dpi)

    # ---- session: image as it is now + everything that was set on it ----
    def _session_dict(self):
        num = lambda v: None if v is None else float(v)
        traces = []
        for t in self.traces:
            traces.append(dict(
                name=t['name'], roi=[float(v) for v in t['roi']],
                color=list(t['color']) if isinstance(t['color'], tuple) else t['color'], zero=num(t['zero']),
                exclude=[[float(v) for v in b] for b in t['exclude']],
                guide=[dict(points=np.asarray(pts, float).tolist(), width=float(w)) for pts, w in t['guide']]
                if t.get('guide') else None))
        sel = self._selected()
        return dict(
            version=SESSION_VERSION, original_image=self.image_path,
            dpi=[float(v) for v in self.image_dpi] if self.image_dpi else None,
            calibration=dict(px={k: num(v) for k, v in self.cal_px.items()},
                             value={k: self.cal_var[k].get() for k in self.cal_px}),
            work_box=[float(v) for v in self.work_box] if self.work_box else None, work_angle=self.v_angle.get(),
            traces=traces, selected=self.traces.index(sel) if sel in self.traces else None,
            settings={k: v.get() for k, v in self._session_vars().items()},
            view=dict(xlim=[float(v) for v in self.ax_img.get_xlim()], ylim=[float(v) for v in self.ax_img.get_ylim()]))

    def _session_vars(self):
        return dict(step=self.v_step, smooth=self.v_smooth, poly=self.v_poly, norm=self.v_norm, points=self.v_points,
                    thresh=self.v_thresh, tol=self.v_tol, dot=self.v_dot, brush_width=self.v_bwidth,
                    brush_reach=self.v_reach, eraser=self.v_brush)

    def save_session(self, ask=False):
        """One file (a zip): session.json with the calibration, curves, brush paths, working area and settings,
        and image.png, the image as it is now (after any crop, rotation or cleaning). The undo history is not kept."""
        if not self._need_image():
            return
        path = self.session_path
        if ask or not path:
            base = os.path.splitext(os.path.basename(self.image_path or "figure"))[0] + SESSION_EXT
            path = filedialog.asksaveasfilename(
                title="Save session", defaultextension=SESSION_EXT, initialfile=base,
                initialdir=os.path.dirname(self.image_path) if self.image_path else None,
                filetypes=[("Spectrum Digitizer session", "*" + SESSION_EXT)])
            if not path:
                return
        try:
            png = io.BytesIO()
            Image.fromarray(self.rgb.astype(np.uint8)).save(png, format='PNG', compress_level=6)
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('session.json', json.dumps(self._session_dict(), indent=1), zipfile.ZIP_DEFLATED)
                z.writestr('image.png', png.getvalue(), zipfile.ZIP_STORED)
        except Exception as e:
            messagebox.showerror("Save session", f"Cannot save the session:\n{e}")
            return
        self.session_path = path
        self.root.title(f"Spectrum Digitizer - {os.path.basename(path)}")
        self._set_status(f"Session saved: {path} ({os.path.getsize(path) // 1024} kB: image, calibration, "
                         f"{len(self.traces)} curve(s), brush paths, working area, settings).", ok=True, sticky=True)

    def open_session_dialog(self):
        path = filedialog.askopenfilename(title="Open session", filetypes=[
            ("Spectrum Digitizer session", "*" + SESSION_EXT), ("All files", "*.*")])
        if path:
            self.open_session(path)

    def open_session(self, path):
        try:
            with zipfile.ZipFile(path) as z:
                d = json.loads(z.read('session.json').decode('utf-8'))
                img = Image.open(io.BytesIO(z.read('image.png'))).convert('RGB')
            if int(d.get('version', 0)) > SESSION_VERSION:
                raise ValueError(f"saved by a newer version of the program (session format {d.get('version')})")
        except Exception as e:
            messagebox.showerror("Open session", f"Cannot open the session:\n{e}")
            return
        dpi = tuple(d['dpi']) if d.get('dpi') else None
        self._set_image(img, d.get('original_image') or path, dpi, title=os.path.basename(path))
        self.session_path = path
        for k, v in d.get('settings', {}).items():
            var = self._session_vars().get(k)
            if var is not None:
                var.set(v)
        cal = d.get('calibration', {})
        for k in self.cal_px:
            v = cal.get('px', {}).get(k)
            self.cal_px[k] = None if v is None else float(v)
            self.cal_var[k].set(cal.get('value', {}).get(k, ''))
        self._update_px_labels()
        self.traces = []
        for t in d.get('traces', []):
            col = t.get('color')
            self.traces.append(dict(
                name=t['name'], roi=tuple(t['roi']), color=tuple(col) if isinstance(col, list) else col,
                zero=t.get('zero'), exclude=[tuple(b) for b in t.get('exclude', [])],
                xpx=np.array([]), ypx=np.array([]),
                **({'guide': [(np.array(g['points'], float), float(g['width'])) for g in t['guide']]}
                   if t.get('guide') else {})))
        wb = d.get('work_box')
        self.work_box = tuple(wb) if wb else None
        self.v_angle.set(d.get('work_angle', '0'))
        self._guide_hist, self._guide_redo = [], []
        self._refresh_list(select=d.get('selected') if self.traces else None)
        view = d.get('view') or {}
        H, W = self.rgb.shape[:2]
        try:
            xl, yl = view['xlim'], view['ylim']
            if -W <= min(xl) and max(xl) <= 2 * W and -H <= min(yl) and max(yl) <= 2 * H:
                self.ax_img.set_xlim(xl)
                self.ax_img.set_ylim(yl)
        except (KeyError, TypeError):
            pass
        self._extract_all()
        self._set_status(f"Session opened: {os.path.basename(path)} ({len(self.traces)} curve(s)). The undo history "
                         "is not saved with a session.", ok=True, sticky=True)

    def save_image(self):
        """Save the image as it is now (after erasing / keeping colours and areas) as a lossless PNG."""
        if not self._need_image():
            return
        base = os.path.splitext(os.path.basename(self.image_path))[0] + "_clean.png"
        path = filedialog.asksaveasfilename(
            title="Save cleaned image", defaultextension=".png", initialfile=base,
            initialdir=os.path.dirname(self.image_path), filetypes=[("PNG image", "*.png")])
        if not path:
            return
        # PNG is lossless: the pixels written are exactly the ones on screen. 8 bit per channel RGB (as the
        # program holds the image), same size in pixels as the image shown (after any crop or rotation),
        # compression level 9 (the slowest and smallest zlib setting: only the file size changes) and the
        # resolution (dpi) of the source file if it had one.
        extra = {'dpi': tuple(float(v) for v in self.image_dpi)} if self.image_dpi else {}
        try:
            Image.fromarray(self.rgb.astype(np.uint8)).save(path, format='PNG', compress_level=9, **extra)
            size_kb = os.path.getsize(path) / 1024
        except Exception as e:
            messagebox.showerror("Save cleaned image", f"Cannot save the image:\n{e}")
            return
        H, W = self.rgb.shape[:2]
        self._set_status(f"Cleaned image saved: {path}\n{W} x {H} px, PNG lossless, 8-bit RGB, compression 9, "
                         f"{size_kb:.0f} kB" + (f", {self.image_dpi[0]:g} dpi" if self.image_dpi else "")
                         + ". Open it later with File → Open Image.", ok=True, sticky=True)

    def close_image(self):
        """Back to the empty start state: no image, calibration, curves or filter history."""
        if self.rgb is None:
            return
        self.image_path = self.pil = self.rgb = self.im = None
        self.session_path = None
        self.rgb_undo, self.rgb_redo = [], []
        self.work_box = None
        self.v_angle.set('0')
        self.traces = []
        for key in self.cal_px:
            self.cal_px[key] = None
            self.cal_var[key].set('')
        self._update_px_labels()
        self._nudge = None
        self._last_xy = None
        self._overlay = []
        self._cancel_tool(keep_status=True)
        self._refresh_list()
        self._show_empty()
        self.root.title("Spectrum Digitizer")
        self._sticky = False
        self._update_hint()

    def _show_empty(self):
        self.ax_img.clear()
        self.ax_img.set_axis_off()
        self.ax_res.clear()
        self.ax_big.clear()
        self.canvas_big.draw_idle()
        self.canvas.draw_idle()
        self.canvas_res.draw_idle()

    # ---- view: zoom, pan, loupe ----
    def _fit_view(self):
        if self.rgb is None:
            return
        H, W = self.rgb.shape[:2]
        self.ax_img.set_xlim(-0.5, W - 0.5)
        self.ax_img.set_ylim(H - 0.5, -0.5)
        self.canvas.draw_idle()

    def _zoom_at(self, factor, xd, yd):
        ax = self.ax_img
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        ax.set_xlim(xd - (xd - x0) * factor, xd + (x1 - xd) * factor)
        ax.set_ylim(yd - (yd - y0) * factor, yd + (y1 - yd) * factor)
        self.canvas.draw_idle()

    def _zoom_step(self, factor):
        if self.rgb is None:
            return
        x0, x1 = self.ax_img.get_xlim()
        y0, y1 = self.ax_img.get_ylim()
        self._zoom_at(factor, (x0 + x1) / 2, (y0 + y1) / 2)

    def _on_scroll(self, event):
        if self.rgb is None or event.inaxes is not self.ax_img:
            return
        self._zoom_at(0.75 if event.button == 'up' else 1 / 0.75, event.xdata, event.ydata)

    def _on_press(self, event):
        if self.rgb is None or event.inaxes is not self.ax_img or event.xdata is None:
            return
        ax = self.ax_img
        if event.button == 1 and self.mode is None and self.work_box is not None:
            h = self._work_hit(event)
            if h is not None:
                self._start_work_drag(h, event)
                return
        # Drag = move the image, unless a tool is waiting for a click/box (then only the
        # right and middle buttons move it).
        if event.button in (2, 3) or (event.button == 1 and self.mode is None):
            self._pan = (event.x, event.y, ax.get_xlim(), ax.get_ylim(), ax.transData.frozen().inverted())
            self.canvas.get_tk_widget().config(cursor='fleur')
            return
        if event.button != 1:
            return
        x, y = event.xdata, event.ydata
        H, W = self.rgb.shape[:2]
        if self.mode == 'pick':
            key = self._pick_key
            self.cal_px[key] = x if key[0] == 'x' else y
            self._cancel_tool(keep_status=True)
            self._update_px_labels()
            self._nudge = ('cal', key)
            self.cal_entries[key].focus_set()
            self.cal_entries[key].select_range(0, tk.END)
            self._set_status(f"Point set: fine-tune it with the arrow keys ({'←→' if key[0] == 'x' else '↑↓'}, "
                             f"Shift = finer), then type its value in the box ({key[0].upper()} point {key[1]}).",
                             ok=True, sticky=True)
            self._redraw()
        elif self.mode == 'color':
            px = self.rgb[min(max(int(round(y)), 0), H - 1), min(max(int(round(x)), 0), W - 1)]
            if px.min() > 235:
                self._set_status("That pixel is white: click right on the curve.")
                return
            tr = self._selected()
            tr['color'] = tuple(int(v) for v in px)
            self._cancel_tool(keep_status=True)
            self._set_status(f"Curve colour set to RGB {tr['color']}.", ok=True, sticky=True)
            self._extract_all()
        elif self.mode == 'guide':
            self._guide_press(event, x, y)
        elif self.mode == 'straighten':
            self._straight_pts.append((x, y))
            if len(self._straight_pts) == 1:
                self._set_status("Now click a second point on the same line, as far from the first as you can "
                                 "(the longer the baseline, the more exact the angle).")
                self._redraw()
            else:
                (x1, y1), (x2, y2) = self._straight_pts
                self._cancel_tool(keep_status=True)
                if np.hypot(x2 - x1, y2 - y1) < 10:
                    self._set_status("The two points are too close: pick them farther apart.", sticky=True)
                    self._redraw()
                else:
                    self.rotate_image(self._straightening_angle(x1, y1, x2, y2))
        elif self.mode == 'brush':
            self._push_undo()       # one undo step per stroke
            self.rgb = self.rgb.copy()
            self._brush_last = (x, y)
            self._brush_stamp(x, y, x, y)
        elif self.mode in ('erase', 'keep'):
            # A click adds one colour, a drag (the RectangleSelector) adds all the colours inside the box:
            # which one it is is known only on release.
            self._press_xy = (event.x, event.y, x, y)
        elif self.mode == 'zero':
            tr = self._selected()
            tr['zero'] = float(y)
            self._nudge = ('zero', tr)
            self._cancel_tool(keep_status=True)
            self._set_status("Zero line set: fine-tune it with the ↑↓ keys (Shift = finer).", ok=True, sticky=True)
            self._redraw()

    def _add_filter_colors(self, entry):
        """Add a click or a sampled box to the pending colour filter and refresh the status."""
        self._filter_pts.append(entry)
        self.apply_btn.config(state=tk.NORMAL, text=f"Apply ({len(self._filter_pts)})")
        verb = 'erase' if self.mode == 'erase' else 'keep'
        self._set_status(f"{len(self._filter_pts)} selection(s) to {verb}. Click a pixel or drag a small box over "
                         "the trace to add its colours, then press 'Apply'. Esc cancels.")
        self._redraw()

    def _pixel_colors(self, crop):
        """Distinct colours of an RGB crop, ignoring (near) white and merging close shades (steps of 16
        levels): a few tens at most, so 'Apply' stays fast even for a box with thousands of JPEG shades."""
        px = crop.reshape(-1, 3)
        px = px[px.min(axis=1) <= 235]
        if len(px) == 0:
            return []
        q = np.unique((px // 16).astype(int), axis=0) * 16 + 8
        return [tuple(int(v) for v in c) for c in q]

    def _on_release(self, event):
        if self._stroke is not None:
            self._finish_stroke()
            return
        if self._gdrag is not None:
            self._finish_reshape()
            return
        if self._brush_last is not None:
            self._brush_last = None
            self._extract_all()
            return
        if self._work_drag is not None:
            self._work_drag = None
            self.canvas.get_tk_widget().config(cursor='')
            self._work_cursor = ''
            self._redraw()
            return
        press, self._press_xy = self._press_xy, None
        if press is not None and self.mode in ('erase', 'keep') and event.button == 1                 and abs(event.x - press[0]) < 4 and abs(event.y - press[1]) < 4:
            H, W = self.rgb.shape[:2]
            x, y = press[2], press[3]
            px = self.rgb[min(max(int(round(y)), 0), H - 1), min(max(int(round(x)), 0), W - 1)]
            if px.min() > 235:
                self._set_status("That pixel is white: click right on the trace.")
            else:
                self._add_filter_colors(dict(x=x, y=y, colors=[tuple(int(v) for v in px)]))
        if self._pan is not None:
            self._pan = None
            self.canvas.get_tk_widget().config(cursor='crosshair' if self.mode else '')

    def _on_motion(self, event):
        if self.rgb is None:
            return
        if self._work_drag is not None:
            self._work_drag_to(event)
            return
        if self._gdrag is not None:
            self._reshape_to(event)
            return
        if self.mode == 'guide' and self._stroke is None and self._pan is None:
            hit = self._guide_hit(event) if event.inaxes is self.ax_img else None
            cur = {None: 'crosshair', 'extend': 'sizing', 'reshape': 'fleur'}[hit[0] if hit else None]
            if cur != self._guide_cursor:
                self._guide_cursor = cur
                self.canvas.get_tk_widget().config(cursor=cur)
        if self._stroke is not None:
            x, y = self.ax_img.transData.inverted().transform((event.x, event.y))
            if np.hypot(x - self._stroke[-1][0], y - self._stroke[-1][1]) >= 1.0:
                self._stroke.append((float(x), float(y)))
                self._refresh_overlay()
            return
        if self.mode == 'brush' and self._pan is None:
            x, y = self.ax_img.transData.inverted().transform((event.x, event.y))
            if self._brush_last is not None:
                self._brush_stamp(*self._brush_last, x, y)
                self._brush_last = (x, y)
            self._move_brush_circle(x, y)
        if self.mode is None and self._pan is None and self.work_box is not None:
            h = self._work_hit(event) if event.inaxes is self.ax_img else None
            cur = '' if h is None else {'rot': 'exchange', 'size': 'sizing', 'move': 'fleur'}[h[0]]
            if cur != self._work_cursor:
                self._work_cursor = cur
                self.canvas.get_tk_widget().config(cursor=cur)
        if self._pan is not None:
            # Move by the data-space distance between the press point and the mouse, measured with the
            # transform frozen at press time: it handles the inverted Y axis (display Y grows upward, image
            # rows downward), which a plain pixel-to-span scaling got backwards.
            x0, y0, xlim, ylim, inv = self._pan
            (px0, py0), (px1, py1) = inv.transform([(x0, y0), (event.x, event.y)])
            dx, dy = px1 - px0, py1 - py0
            self.ax_img.set_xlim(xlim[0] - dx, xlim[1] - dx)
            self.ax_img.set_ylim(ylim[0] - dy, ylim[1] - dy)
            self.canvas.draw_idle()
            return
        if event.inaxes is self.ax_img and event.xdata is not None:
            self._last_xy = (event.xdata, event.ydata)
            self._update_loupe(event.xdata, event.ydata)

    def _update_loupe(self, x, y):
        H, W = self.rgb.shape[:2]
        r = LOUPE_RADIUS
        cx, cy = int(round(x)), int(round(y))
        crop = Image.new('RGB', (2 * r + 1, 2 * r + 1), (200, 200, 200))
        sx0, sy0 = max(cx - r, 0), max(cy - r, 0)
        sx1, sy1 = min(cx + r + 1, W), min(cy + r + 1, H)
        if sx1 > sx0 and sy1 > sy0:
            crop.paste(self.pil.crop((sx0, sy0, sx1, sy1)), (sx0 - (cx - r), sy0 - (cy - r)))
        big = crop.resize((LOUPE_SIZE, LOUPE_SIZE), Image.NEAREST)
        d = ImageDraw.Draw(big)
        c, step = LOUPE_SIZE / 2, LOUPE_SIZE / (2 * r + 1)
        d.line([(c, 0), (c, LOUPE_SIZE)], fill=(255, 0, 0), width=1)
        d.line([(0, c), (LOUPE_SIZE, c)], fill=(255, 0, 0), width=1)
        d.rectangle([c - step / 2, c - step / 2, c + step / 2, c + step / 2], outline=(0, 120, 255))
        self._loupe_photo = ImageTk.PhotoImage(big)
        self.loupe.config(image=self._loupe_photo, width=LOUPE_SIZE, height=LOUPE_SIZE)
        text = f"pixel  x={x:7.1f}\n       y={y:7.1f}"
        xc, yc = self.xcal, self.ycal
        if xc:
            text += f"\nX = {float(lin_map(xc, x)):.5g}"
        if yc:
            text += f"\nY = {float(lin_map(yc, y)):.5g}"
        self.cursor_label.config(text=text)

    # ---- tools ----
    def _set_status(self, text, ok=False, sticky=False):
        self._sticky = sticky
        self.status.config(text=text, bg='#d9f2d9' if ok else '#fff3c4')

    def _on_cal_edit(self):
        self._sticky = False
        self._schedule(False)

    def _update_hint(self):
        """Tell the user what to do next, from the current state."""
        if self.mode is not None or self._sticky:
            return
        if self.rgb is None:
            self._set_status("Open an image: File → Open Image, or drop it on this window.")
        elif self._clean_tab_active():
            self._set_status("Clean the image (optional): draw a working area, erase or keep colours, retouch with the "
                             "eraser. When it looks right press 'Next: Digitize' (or open the Digitize tab).", ok=True)
        elif self.xcal is None:
            self._set_status("Step 1 - X axis: press 'Pick' next to 'X point 1', click on a tick of the X axis, "
                             "then type its value. Do the same for 'X point 2' on another tick, far from the first. "
                             "(Wheel = zoom for precision.)\n" + self.missing_reason())
        elif not self.traces:
            self._set_status("Step 2 - press 'Brush: new curve' and drag along a curve (stop and resume as you "
                             "like, pull back what strays), or use 'Add curve' to drag a box around one that is "
                             "alone, or 'Auto-detect panels' for framed Origin-style plots.", ok=True)
        elif not any(len(t['xpx']) for t in self.traces):
            self._set_status("No curve points found in the box: draw the box again around the curve, or "
                             "check the curve colour / black-curve setting.")
        else:
            self._set_status("Step 3 - check the red points on the curve and the result below the image, "
                             "then open the Output tab (step, smoothing, normalization) and press 'Export CSV'.",
                             ok=True)

    def _cancel_tool(self, keep_status=False):
        self._brush_last = None
        self._straight_pts = []
        self._stroke = self._stroke_src = self._gdrag = self._guide_target = None
        self._guide_cursor = ''
        if self._brush_circle is not None:
            try:
                self._brush_circle.remove()
            except Exception:
                pass
            self._brush_circle = None
            self.canvas.draw_idle()
        if self._filter_pts:
            self._filter_pts = []
            self.apply_btn.config(state=tk.DISABLED, text="Apply")
        self.mode = None
        self._pick_key = None
        self.selector.set_active(False)
        self.selector.set_visible(False)
        self.canvas.get_tk_widget().config(cursor='')
        if not keep_status:
            self._update_hint()

    def _on_escape(self):
        self._nudge = None
        self._cancel_tool()
        self._redraw()

    def _set_nudge(self, target):
        """Select the point the arrow keys move (only if it has been set)."""
        kind, ref = target
        if kind == 'cal' and self.cal_px[ref] is None:
            return
        self._nudge = target
        self._redraw()

    def _on_arrow(self, event, dx, dy, fine):
        if self._nudge is None or self.rgb is None or self.mode is not None:
            return
        if isinstance(event.widget, (tk.Listbox, ttk.Combobox, tk.Text)):
            return
        amount = 0.2 if fine else 1.0
        kind, ref = self._nudge
        if kind == 'cal':
            axis_step = dx if ref[0] == 'x' else dy
            if axis_step == 0 or self.cal_px[ref] is None:
                return
            self.cal_px[ref] += axis_step * amount
            self._update_px_labels()
            pos = (self.cal_px[ref], (self._last_xy or (0, self.rgb.shape[0] / 2))[1]) if ref[0] == 'x'                 else ((self._last_xy or (self.rgb.shape[1] / 2, 0))[0], self.cal_px[ref])
        else:
            if dy == 0 or ref['zero'] is None or ref not in self.traces:
                return
            ref['zero'] += dy * amount
            pos = (ref['roi'][0], ref['zero'])
        self._update_loupe(*pos)
        self._redraw()
        return 'break'

    def _need_image(self):
        if self.rgb is None:
            messagebox.showwarning("Spectrum Digitizer", "Open an image first.")
            return False
        return True

    def _start_pick(self, key):
        if not self._need_image():
            return
        self._cancel_tool(keep_status=True)
        self._nudge = None
        self.mode, self._pick_key = 'pick', key
        self.canvas.get_tk_widget().config(cursor='crosshair')
        what = "X axis tick" if key[0] == 'x' else "Y axis tick"
        self._set_status(f"Click on the {what} of known value (the magnifier on the right helps). "
                         "Esc cancels.")

    def _start_rect(self, purpose):
        if not self._need_image():
            return
        if purpose == 'excl' and self._selected() is None:
            messagebox.showwarning("Ignore an area", "Add and select a curve first.")
            return
        self._cancel_tool(keep_status=True)
        self._nudge = None
        self.mode = purpose
        self.selector.set_active(True)
        self.canvas.get_tk_widget().config(cursor='crosshair')
        self._set_status("Drag a box around the curve: include only the curve, leave out axis labels, "
                         "legends and panel letters. Esc cancels." if purpose == 'roi' else
                         "Drag a box over what must be removed from the image (a legend, a panel letter...): "
                         "it is painted white, for all curves. 'Undo' restores it. Esc cancels."
                         if purpose == 'wipe' else
                         "Drag the working area: the colour filter will act only inside it. Esc cancels."
                         if purpose == 'work' else
                         "Drag a box over what the selected curve must ignore (a panel letter, a legend...). "
                         "Esc cancels.")

    def _start_point(self, kind):
        if self._selected() is None:
            messagebox.showwarning("Spectrum Digitizer", "Add and select a curve first.")
            return
        self._cancel_tool(keep_status=True)
        self._nudge = None
        self.mode = kind
        self.canvas.get_tk_widget().config(cursor='crosshair')
        self._set_status("Click on a pixel of the curve (not on a white pixel). Esc cancels." if kind == 'color'
                         else "Click on the zero level of this curve (Y = height above that line). Esc cancels.")

    # ---- eraser (hand refinement) ----
    def _start_brush(self):
        if not self._need_image():
            return
        self._cancel_tool(keep_status=True)
        self._nudge = None
        self.mode = 'brush'
        self.canvas.get_tk_widget().config(cursor='crosshair')
        self._set_status("Eraser: drag over what must go (it is painted white); the circle shows the size, set it "
                         "in 'size (px)'. One 'Undo' takes back a whole stroke. Right/middle drag moves the "
                         "image. Esc ends.")

    def _brush_diameter(self):
        return max(1.0, self._to_float(self.v_brush.get()) or 1.0)

    def _brush_stamp(self, x0, y0, x1, y1):
        """Paint white a disc of the eraser size along the segment (x0, y0)-(x1, y1), image coordinates."""
        r = self._brush_diameter() / 2
        H, W = self.rgb.shape[:2]
        n = max(int(np.hypot(x1 - x0, y1 - y0) / max(r / 2, 0.5)), 1)
        for t in np.linspace(0, 1, n + 1):
            cx, cy = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            xa, xb = max(int(np.floor(cx - r)), 0), min(int(np.ceil(cx + r)) + 1, W)
            ya, yb = max(int(np.floor(cy - r)), 0), min(int(np.ceil(cy + r)) + 1, H)
            if xa >= xb or ya >= yb:
                continue
            yy, xx = np.mgrid[ya:yb, xa:xb]
            self.rgb[ya:yb, xa:xb][(xx - cx) ** 2 + (yy - cy) ** 2 <= r * r + 0.25] = 255.0
        self.im.set_data(self.rgb.astype(np.uint8))
        self.canvas.draw_idle()

    def _move_brush_circle(self, x, y):
        r = self._brush_diameter() / 2
        if self._brush_circle is None:
            self._brush_circle = Circle((x, y), r, fill=False, ec='magenta', lw=1.0)
            self.ax_img.add_patch(self._brush_circle)
        self._brush_circle.set_center((x, y))
        self._brush_circle.set_radius(r)
        self.canvas.draw_idle()

    def _start_filter(self, kind):
        if not self._need_image():
            return
        self._cancel_tool(keep_status=True)
        self._nudge = None
        self.mode = kind
        self.selector.set_active(True)
        self.canvas.get_tk_widget().config(cursor='crosshair')
        where = "inside the working area" if self.work_box else "in the whole image"
        self._set_status(("Click on the trace colour(s) to ERASE" if kind == 'erase' else
                          "Click on the trace colour(s) to KEEP (everything else becomes white)")
                         + f", {where}: click a pixel, or drag a small box over the trace to take all the "
                         "colours inside it; repeat if needed, then press 'Apply'. Esc cancels.")

    def _work_angle(self):
        return self._to_float(self.v_angle.get()) or 0.0

    def _work_corners(self):
        """Corners (4 x 2, in image pixels) of the working area: the box rotated about its centre."""
        x0, y0, x1, y1 = self.work_box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        t = np.radians(self._work_angle())
        c, sn = np.cos(t), np.sin(t)
        out = []
        for u, v in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            u, v = u - cx, v - cy
            out.append((cx + u * c + v * sn, cy - u * sn + v * c))   # y points down: counter-clockwise on screen
        return np.array(out)

    def _work_mask(self, shape):
        """Boolean mask of the working area (the whole image if there is none)."""
        if self.work_box is None:
            return np.ones(shape, bool)
        x0, y0, x1, y1 = self.work_box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        t = np.radians(self._work_angle())
        yy, xx = np.mgrid[:shape[0], :shape[1]]
        dx, dy = xx - cx, yy - cy
        u = dx * np.cos(t) - dy * np.sin(t)       # pixel position in the frame of the unrotated box
        v = dx * np.sin(t) + dy * np.cos(t)
        return (np.abs(u) <= (x1 - x0) / 2 + 0.5) & (np.abs(v) <= (y1 - y0) / 2 + 0.5)

    # ---- working-area handles: move (drag inside), resize (8 square handles), rotate (round handle) ----
    def _work_frame(self):
        x0, y0, x1, y1 = self.work_box
        return (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2, np.radians(self._work_angle())

    @staticmethod
    def _to_local(x, y, frame):
        """Image point -> (u, v) in the frame of the unrotated box, origin at its centre."""
        cx, cy, _, _, t = frame
        dx, dy = x - cx, y - cy
        return dx * np.cos(t) - dy * np.sin(t), dx * np.sin(t) + dy * np.cos(t)

    @staticmethod
    def _from_local(u, v, frame):
        cx, cy, _, _, t = frame
        return cx + u * np.cos(t) + v * np.sin(t), cy - u * np.sin(t) + v * np.cos(t)

    def _px_scale(self):
        """Screen pixels per image pixel at the current zoom."""
        tr = self.ax_img.transData
        return abs(tr.transform((1, 0))[0] - tr.transform((0, 0))[0]) or 1.0

    def _work_handles(self):
        """[(name, sx, sy, x, y)] in image coordinates; the rotation handle sits 28 screen px above the top edge."""
        fr = self._work_frame()
        hw, hh = fr[2], fr[3]
        out = [('rot', 0, -2, *self._from_local(0, -hh - 28 / self._px_scale(), fr))]
        for sx, sy in ((-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0)):
            out.append(('size', sx, sy, *self._from_local(sx * hw, sy * hh, fr)))
        return out

    def _work_hit(self, event):
        """Handle under the mouse: a _work_handles() tuple, ('move', 0, 0, x, y) inside the area, or None."""
        if self.work_box is None or not self._clean_tab_active():
            return None
        tr = self.ax_img.transData
        for h in self._work_handles():
            hx, hy = tr.transform((h[3], h[4]))
            if np.hypot(hx - event.x, hy - event.y) <= 9:
                return h
        fr = self._work_frame()
        x, y = tr.inverted().transform((event.x, event.y))
        u, v = self._to_local(x, y, fr)
        if abs(u) <= fr[2] and abs(v) <= fr[3]:
            return ('move', 0, 0, x, y)
        return None

    def _start_work_drag(self, h, event):
        p0 = self.ax_img.transData.inverted().transform((event.x, event.y))
        self._work_drag = dict(name=h[0], sx=h[1], sy=h[2], frame=self._work_frame(), p0=tuple(p0))
        self.canvas.get_tk_widget().config(cursor='fleur')

    def _work_drag_to(self, event):
        d = self._work_drag
        x, y = self.ax_img.transData.inverted().transform((event.x, event.y))
        cx, cy, hw, hh, t = d['frame']
        if d['name'] == 'move':
            dx, dy = x - d['p0'][0], y - d['p0'][1]
            self.work_box = (cx - hw + dx, cy - hh + dy, cx + hw + dx, cy + hh + dy)
        elif d['name'] == 'rot':
            ang = np.degrees(np.arctan2(-(x - cx), -(y - cy)))
            if event.key == 'shift':
                ang = round(ang / 15) * 15
            self._angle_busy = True
            self.v_angle.set(f"{round(float(ang), 1):g}")
            self._angle_busy = False
        else:
            u, v = self._to_local(x, y, d['frame'])
            u0, u1, v0, v1 = -hw, hw, -hh, hh      # the edge opposite to the handle stays where it is
            if d['sx'] == 1:
                u1 = max(u, u0 + 4)
            elif d['sx'] == -1:
                u0 = min(u, u1 - 4)
            if d['sy'] == 1:
                v1 = max(v, v0 + 4)
            elif d['sy'] == -1:
                v0 = min(v, v1 - 4)
            ncx, ncy = self._from_local((u0 + u1) / 2, (v0 + v1) / 2, d['frame'])
            nhw, nhh = (u1 - u0) / 2, (v1 - v0) / 2
            self.work_box = (ncx - nhw, ncy - nhh, ncx + nhw, ncy + nhh)
        self._refresh_overlay()

    def _refresh_overlay(self):
        """Redraw only the overlays (cheap enough to follow a handle drag)."""
        for a in self._overlay:
            try:
                a.remove()
            except Exception:
                pass
        self._overlay = []
        if self.rgb is not None:
            self._draw_overlays()
            self.canvas.draw_idle()

    def clear_work_box(self):
        self.work_box = None
        self.v_angle.set('0')
        self._set_status("Working area removed: the colour filter acts on the whole image.", ok=True, sticky=True)
        self._redraw()

    def apply_filter(self):
        """Paint white the pixels within the colour tolerance of any clicked colour (erase) or all the
        others (keep), inside the working area (or the whole image). Undoable."""
        pts, keep = list(self._filter_pts), self.mode == 'keep'
        if not pts or self.mode not in ('erase', 'keep'):
            return
        self._cancel_tool(keep_status=True)
        tol = self._num(self.v_tol, 60.0, lo=1)
        near = np.zeros(self.rgb.shape[:2], bool)
        colors = {c for e in pts for c in e['colors']}
        for c in colors:
            near |= np.sqrt(((self.rgb - np.asarray(c, float)) ** 2).sum(axis=2)) < tol
        hit = ~near if keep else near
        hit &= self._work_mask(hit.shape)
        n = int(hit.sum())
        if n == 0:
            self._set_status("No pixel changed.", sticky=True)
            self._redraw()
            return
        self._push_undo()
        self.rgb = self.rgb.copy()
        self.rgb[hit] = 255.0
        self.im.set_data(self.rgb.astype(np.uint8))
        self._set_status(f"{'Kept only' if keep else 'Erased'} {len(colors)} colour(s) (tolerance {tol:g}): "
                         f"{n} pixels made white. 'Undo' restores the previous image.", ok=True, sticky=True)
        self._extract_all()

    def despeckle(self):
        """Paint white the isolated specks of a scan: connected groups of non-white pixels (8-connected, further
        from white than the colour tolerance) smaller than the chosen number of pixels, inside the working area.
        A curve is one big group and is left alone; the dots of a scatter plot are bigger than a speck."""
        if not self._need_image():
            return
        self._cancel_tool(keep_status=True)
        tol = self._num(self.v_tol, 60.0, lo=1)
        n_min = int(self._num(self.v_speck, 6.0, lo=1))
        ink = np.sqrt(((255.0 - self.rgb) ** 2).sum(axis=2)) > tol
        lab, n = ndi.label(ink, structure=np.ones((3, 3)))
        small = np.bincount(lab.ravel(), minlength=n + 1) < n_min
        small[0] = False
        hit = small[lab] & self._work_mask(ink.shape)
        specks = len(np.unique(lab[hit]))
        if specks == 0:
            self._set_status(f"No speck smaller than {n_min} pixels.", sticky=True)
            return
        self._push_undo()
        self.rgb = self.rgb.copy()
        self.rgb[hit] = 255.0
        self.im.set_data(self.rgb.astype(np.uint8))
        self._set_status(f"{specks} specks removed ({int(hit.sum())} pixels, groups under {n_min} pixels). "
                         "'Undo' restores them; raise the size to remove larger ones.", ok=True, sticky=True)
        self._extract_all()

    def flatten_colors(self):
        """Turn every non-white pixel (distance from white above the colour tolerance) of the working area, or
        of the whole image, into pure black: a trace drawn in several shades becomes one colour, which the
        default 'Black curve' detection follows entirely. Undoable; kept by 'Save Cleaned Image'."""
        if not self._need_image():
            return
        self._cancel_tool(keep_status=True)
        tol = self._num(self.v_tol, 60.0, lo=1)
        hit = np.sqrt(((255.0 - self.rgb) ** 2).sum(axis=2)) > tol
        hit &= self._work_mask(hit.shape)
        hit &= self.rgb.sum(axis=2) > 0          # already black: nothing to do
        n = int(hit.sum())
        if n == 0:
            self._set_status("No pixel to flatten.", sticky=True)
            return
        self._push_undo()
        self.rgb = self.rgb.copy()
        self.rgb[hit] = 0.0
        self.im.set_data(self.rgb.astype(np.uint8))
        self._set_status(f"{n} non-white pixels made black (tolerance {tol:g} from white). Pale pixels closer to "
                         "white are left as they are; lower the tolerance to include them. 'Undo' restores.",
                         ok=True, sticky=True)
        self._extract_all()

    # ---- semi-automatic tracing with a brush ----
    # A curve's `guide` is a list of strokes (path, width): the paths are editable polylines (about 2 px
    # between points) and the extraction only looks under their corridors. The brush tool stays active, so one
    # can draw, stop, resume at an end of a path, or pull any stretch of it back onto the curve.
    def _guide_width(self):
        return max(2.0, self._to_float(self.v_bwidth.get()) or 14.0)

    def _guide_reach(self):
        return max(10.0, self._to_float(self.v_reach.get()) or 60.0)

    def _start_guide(self, edit):
        if not self._need_image():
            return
        tr = None
        if edit:
            tr = self._selected()
            if tr is None:
                messagebox.showwarning("Brush", "Select a curve first (or use 'Brush: new curve').")
                return
        self._cancel_tool(keep_status=True)
        self._nudge = None
        self.mode = 'guide'
        self._guide_target = tr
        self.canvas.get_tk_widget().config(cursor='crosshair')
        self._set_status(("Brush: drag along the curve" if tr is None else f"Brush on {tr['name']}: drag along the "
                          "curve") + ". Stop whenever you like and resume by starting a new drag at an end of the "
                         "path; drag any stretch of the path (not its ends) to pull it back onto the curve, the "
                         "neighbouring points follow smoothly ('reach' says how far). The wheel zooms. Esc ends.")

    def _on_bwidth(self):
        """A new width applies to the whole brush path of the curve being edited."""
        tr = self._guide_target
        if tr is not None and tr.get('guide') and self.rgb is not None:
            w = self._guide_width()
            tr['guide'] = [(pts, w) for pts, _ in tr['guide']]
            self._update_guide_roi(tr)
            self._schedule(True)

    def _on_list_select(self):
        if self.mode == 'guide' and self._guide_target is not None and self._selected() is not None:
            self._guide_target = self._selected()
        self._redraw()

    def _guide_hit(self, event):
        """What a press here would do on the edited curve's paths: ('extend', k, side) at an end, ('reshape', k, i)
        on the path, or None (a new stroke). Distances in screen pixels; Shift turns an end into a pull point."""
        tr = self._guide_target
        if tr is None or not tr.get('guide'):
            return None
        tf = self.ax_img.transData
        best = None
        for k, (pts, _) in enumerate(tr['guide']):
            d = np.hypot(*(tf.transform(pts) - np.array([event.x, event.y])).T)
            i = int(np.argmin(d))
            if best is None or d[i] < best[0]:
                best = (float(d[i]), k, i, len(pts))
        dpx, k, i, n = best
        if dpx > 14:
            return None
        if dpx <= 10 and i in (0, n - 1) and event.key != 'shift':
            return ('extend', k, 0 if i == 0 else 1)
        return ('reshape', k, i)

    def _guide_push(self, tr, created=False, keep_redo=False):
        """Remember the state of `tr` (or that it was just created) for the brush 'Undo'."""
        self._guide_hist.append(dict(
            trace=tr, created=created, roi=tuple(tr['roi']),
            guide=None if created or not tr.get('guide') else [(p.copy(), w) for p, w in tr['guide']]))
        if not keep_redo:
            self._guide_redo = []

    @staticmethod
    def _guide_state(tr):
        return dict(roi=tuple(tr['roi']), guide=[(p.copy(), w) for p, w in tr['guide']] if tr.get('guide') else None)

    def _guide_press(self, event, x, y):
        hit = self._guide_hit(event)
        tr = self._guide_target
        if hit is None:
            self._stroke, self._stroke_src = [(x, y)], None
        elif hit[0] == 'extend':
            _, k, side = hit
            pts = tr['guide'][k][0]
            seq = pts if side == 1 else pts[::-1]            # continue from the end that was grabbed
            self._guide_push(tr)
            self._stroke = [(float(a), float(b)) for a, b in seq]
            self._stroke_src = (k, len(seq), side)
        else:
            _, k, i = hit
            self._guide_push(tr)
            self._gdrag = dict(k=k, i=i, pts0=tr['guide'][k][0].copy(), p0=(x, y), moved=False)

    def _reshape_to(self, event):
        d = self._gdrag
        tr = self._guide_target
        x, y = self.ax_img.transData.inverted().transform((event.x, event.y))
        new = reshape_path(d['pts0'], d['i'], (x - d['p0'][0], y - d['p0'][1]), self._guide_reach())
        tr['guide'][d['k']] = (new, tr['guide'][d['k']][1])
        d['moved'] = True
        self._refresh_overlay()

    def _finish_reshape(self):
        d, self._gdrag = self._gdrag, None
        if not d['moved']:
            self._guide_hist.pop()           # a click on the path: nothing changed
            return
        self._after_guide_edit("Path reshaped.")

    def _update_guide_roi(self, tr):
        """The box of a brush-traced curve is what its corridors cover."""
        H, W = self.rgb.shape[:2]
        bx = [(p[:, 0].min() - w / 2, p[:, 1].min() - w / 2, p[:, 0].max() + w / 2, p[:, 1].max() + w / 2)
              for p, w in tr['guide']]
        tr['roi'] = (max(0.0, min(b[0] for b in bx)), max(0.0, min(b[1] for b in bx)),
                     min(float(W), max(b[2] for b in bx)), min(float(H), max(b[3] for b in bx)))

    def _after_guide_edit(self, what):
        tr = self._guide_target
        self._update_guide_roi(tr)
        self._extract_all()
        col = "dark pixels" if tr['color'] is None else ("every non-white pixel" if tr['color'] == 'any'
                                                         else f"colour RGB {tr['color']}")
        if len(tr['xpx']) == 0:
            self._set_status(f"{what} Nothing found under the brush ({col}). If the curve is not dark, use 'Pick its "
                             "colour' or 'Any colour' for it.", sticky=True)
        else:
            msg = (f"{what} {tr['name']}: {len(tr['xpx'])} points under the brush ({col}). Pull the path "
                   "back where it strays, resume at an end, or press Esc when done.")
            allp = np.vstack([g[0] for g in tr['guide']])
            span = float(np.ptp(allp[:, 0]))
            cover = len(np.unique(np.round(tr['xpx']))) / span if span > 0 else 1.0
            if cover < 0.8 and tr['color'] != 'any':
                msg += (f"\nOnly {cover * 100:.0f}% of the columns under the brush contain the curve ({col}): the "
                        "rest is interpolated in straight lines, which gives a broken line. If the curve changes "
                        "colour or is pale in places, press 'Any colour' (or 'Flatten colours' in the Clean tab).")
            # where the brush goes on but no curve was found: say it (axis in the way, or another colour)
            path = np.vstack([g[0] for g in tr['guide']])
            slack = 2 * self._guide_width()
            if path[:, 0].max() - tr['xpx'].max() > slack:
                msg += (f"\nNo curve found to the right of x = {tr['xpx'].max():.0f} px although the brush goes on "
                        f"to {path[:, 0].max():.0f}: it may be hidden by the axis, or of another colour than the "
                        "one being followed ('Any colour', or 'Flatten colours' in the Clean tab first).")
            elif tr['xpx'].min() - path[:, 0].min() > slack:
                msg += (f"\nNo curve found to the left of x = {tr['xpx'].min():.0f} px although the brush starts at "
                        f"{path[:, 0].min():.0f}: it may be hidden by the axis, or of another colour.")
            self._set_status(msg, ok=True, sticky=True)

    def _finish_stroke(self):
        pts, src = self._stroke, self._stroke_src
        self._stroke = self._stroke_src = None
        tr = self._guide_target
        width = self._guide_width()
        if src is None:
            if len(pts) < 3:
                self._set_status("Drag along the curve to draw the brush path (a click is not enough).", sticky=True)
                self._redraw()
                return
            path = smooth_path(resample_path(np.array(pts), 2.0), 2.0)
            if tr is None:                                    # the first stroke makes the curve
                H, W = self.rgb.shape[:2]
                n = len(self.traces) + 1
                names = {t['name'] for t in self.traces}
                while f"curve{n}" in names:
                    n += 1
                tr = dict(name=f"curve{n}", roi=(0.0, 0.0, 1.0, 1.0), zero=None, exclude=[],
                          color=self._guess_color([(path, width)]),
                          xpx=np.array([]), ypx=np.array([]), guide=[(path, width)])
                self.traces.append(tr)
                self._guide_target = tr
                self._guide_push(tr, created=True)
                self._refresh_list(select=len(self.traces) - 1)
            else:
                self._guide_push(tr)
                tr.setdefault('guide', []).append((path, width))
        else:
            k, n_old, side = src
            if len(pts) <= n_old + 1:                         # no real extension
                self._guide_hist.pop()
                self._redraw()
                return
            keep = max(n_old - 3, 0)                          # re-smooth the join together with the new part
            tail = smooth_path(resample_path(np.array(pts[keep:]), 2.0), 2.0)
            tr['guide'][k] = (np.vstack([np.array(pts[:keep]), tail]), tr['guide'][k][1])
        self._after_guide_edit("Stroke added." if src is None else "Path extended.")

    def undo_stroke(self):
        while self._guide_hist:
            h = self._guide_hist.pop()
            tr = h['trace']
            if tr not in self.traces:
                continue
            self._guide_redo.append(dict(trace=tr, created=h['created'], **self._guide_state(tr)))
            if h['created']:
                self.traces.remove(tr)
                self._refresh_list()
                if self._guide_target is tr:
                    self._guide_target = None
            else:
                if h['guide']:
                    tr['guide'] = h['guide']
                else:
                    tr.pop('guide', None)
                tr['roi'] = h['roi']
            self._set_status("Brush edit undone.", ok=True, sticky=True)
            self._extract_all()
            return
        self._set_status("No brush edit to undo.", sticky=True)

    def redo_stroke(self):
        while self._guide_redo:
            r = self._guide_redo.pop()
            tr = r['trace']
            if r['created']:
                if tr in self.traces:
                    continue
                tr['guide'], tr['roi'] = r['guide'], r['roi']
                self.traces.append(tr)
                self._guide_hist.append(dict(trace=tr, created=True, roi=tuple(tr['roi']), guide=None))
                self._refresh_list(select=len(self.traces) - 1)
            else:
                if tr not in self.traces:
                    continue
                self._guide_push(tr, keep_redo=True)
                if r['guide']:
                    tr['guide'] = r['guide']
                else:
                    tr.pop('guide', None)
                tr['roi'] = r['roi']
            self._set_status("Brush edit redone.", ok=True, sticky=True)
            self._extract_all()
            return
        self._set_status("No brush edit to redo.", sticky=True)

    def clear_guide(self):
        tr = self._guide_target or self._selected()
        if tr is None or not tr.get('guide'):
            self._set_status("The selected curve has no brush path.", sticky=True)
            return
        self._guide_push(tr)
        tr.pop('guide')
        self._set_status("Brush path removed: the curve is extracted from its whole box again ('Undo' "
                         "brings it back).", ok=True, sticky=True)
        self._extract_all()

    def _guess_color(self, guide):
        """What to extract under a new brush path: the ink colour under it (axes left out), tried first: None (dark
        pixels) for dark ink, else the median colour of the inkiest 30 %, which keeps crossing curves of other
        colours out of the corridor. If that finds the curve in fewer than 80 % of the columns the brush spans (the
        curve changes colour along its way, or is pale in places, which would give a broken line) the answer is 'any'
        (every non-white pixel)."""
        H, W = self.rgb.shape[:2]
        tol = self._num(self.v_tol, 60.0, lo=1)
        thresh = self._num(self.v_thresh, 128.0)
        dist = np.sqrt(((255.0 - self.rgb) ** 2).sum(axis=2))
        rows, cols = guided_rules(self.rgb, 'any', tol, thresh, 0, 0, H, W)
        corridor = corridor_mask((H, W), guide)
        ink = corridor & (dist > 60) & ~rows[:, None] & ~cols[None, :]
        if ink.sum() < 10:
            return None
        d = dist[ink]
        med = np.median(self.rgb[ink][d >= np.percentile(d, 70)], axis=0)
        cand = None if float(med @ np.array([0.299, 0.587, 0.114])) < thresh else tuple(int(v) for v in med)
        ys, xs = np.nonzero(corridor)
        roi = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
        px, _ = extract_points(self.rgb, roi, cand, thresh, tol, self._num(self.v_dot, 1.2, lo=0) / 100.0, (), guide)
        path = np.vstack([np.asarray(g[0], float) for g in guide])
        span = max(float(np.ptp(path[:, 0])), 1.0)
        return cand if len(np.unique(np.round(px))) >= 0.8 * span else 'any'

    def _draw_check_grid(self, ax):
        """Round-valued lines of the calibration over the image: where they do not meet the plot's own ticks, the
        calibration is off. X always, Y when the Y axis is calibrated."""
        H, W = self.rgb.shape[:2]
        for axis, cal, size in (('x', self.xcal, W), ('y', self.ycal, H)):
            if cal is None:
                continue
            (p1, v1), (p2, v2) = cal
            v_lo, v_hi = lin_map(cal, [0.0, size - 1.0])
            for v in nice_ticks(v_lo, v_hi, 10):
                px = p1 + (v - v1) * (p2 - p1) / (v2 - v1)
                if axis == 'x':
                    ln = ax.axvline(px, color='#00acc1', lw=0.6, alpha=0.9, zorder=3)
                    tx = ax.text(px, 2, f" {v:g}", color='#006064', fontsize=7, va='top', ha='left', clip_on=True,
                                 zorder=3)
                else:
                    ln = ax.axhline(px, color='#00acc1', lw=0.6, alpha=0.9, zorder=3)
                    tx = ax.text(2, px, f"{v:g}", color='#006064', fontsize=7, va='bottom', ha='left', clip_on=True,
                                 zorder=3)
                self._overlay += [ln, tx]

    def _draw_guides(self, ax):
        """Translucent overlay of the brush corridors (one colour per curve), the path of the curve being edited
        (centre line and end handles) and the stroke in progress. The corridors are an RGBA image in data
        coordinates, so they follow zoom and pan."""
        H, W = self.rgb.shape[:2]
        rgba = np.zeros((H, W, 4), np.uint8)
        used = False
        for i, t in enumerate(self.traces):
            if t.get('guide'):
                r, g, b = matplotlib.colors.to_rgb(PALETTE[i % len(PALETTE)])
                rgba[corridor_mask((H, W), t['guide'])] = (int(r * 255), int(g * 255), int(b * 255), 70)
                used = True
        if self._stroke and len(self._stroke) > 1:
            rgba[corridor_mask((H, W), [(np.array(self._stroke), self._guide_width())])] = (255, 0, 255, 70)
            used = True
        if used:
            xl, yl = ax.get_xlim(), ax.get_ylim()
            im = ax.imshow(rgba, extent=(-0.5, W - 0.5, H - 0.5, -0.5), interpolation='nearest', zorder=2)
            ax.set_xlim(xl)
            ax.set_ylim(yl)
            self._overlay.append(im)
        tr = self._guide_target
        if self.mode == 'guide' and tr is not None and tr.get('guide'):
            for pts, _ in tr['guide']:
                ln, = ax.plot(pts[:, 0], pts[:, 1], '-', color='magenta', lw=0.9, zorder=3)
                ends, = ax.plot([pts[0, 0], pts[-1, 0]], [pts[0, 1], pts[-1, 1]], 'o', ms=6, mfc='white',
                                mec='magenta', mew=1.3, zorder=4)
                self._overlay += [ln, ends]

    # ---- straighten a tilted scan ----
    def _start_straighten(self):
        if not self._need_image():
            return
        self._cancel_tool(keep_status=True)
        self._nudge = None
        self.mode = 'straighten'
        self.canvas.get_tk_widget().config(cursor='crosshair')
        self._set_status("Click two points on a line that should be horizontal (the X axis, a frame edge) or "
                         "vertical (the Y axis): the image is rotated to make it so. Esc cancels.")

    @staticmethod
    def _straightening_angle(x1, y1, x2, y2):
        """Counter-clockwise rotation (degrees, within +-90) that makes the line through the two points
        horizontal, or vertical if it is closer to vertical. Image rows grow downward, so atan2(dy, dx) is
        the clockwise angle of the line on the screen."""
        a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs((a % 180) - 90) < 45:                      # closer to vertical
            return float(((a - 90 + 90) % 180) - 90)
        return float(((a + 90) % 180) - 90)

    def straighten_to_area(self):
        """Rotate the image so that the (rotated) working area becomes upright: the user fits the area to the
        tilted plot frame with its handles, then straightens."""
        if not self._need_image():
            return
        if self.work_box is None:
            messagebox.showwarning("Straighten", "Draw a working area first and rotate it to follow the plot "
                                                 "frame (round handle), then press this button.")
            return
        angle = -self._work_angle()               # the area is turned counter-clockwise: turn the image back
        if abs(angle) < 0.01:
            self._set_status("The working area is not rotated: nothing to straighten.", sticky=True)
            return
        self.rotate_image(angle)

    def _confirm_geometry_change(self, title, what):
        """Cropping and rotating move the pixels: calibration and curves cannot survive, so ask first."""
        if self.traces or any(v is not None for v in self.cal_px.values()):
            return messagebox.askyesno(title, f"{what} clears the axis calibration and the curves. Continue?")
        return True

    def crop_to_area(self):
        """Keep only the working area: the image is cut to its bounding box and, if the area is rotated, what lies
        in the box but outside the area is painted white. Undoable (the pixel positions change, so calibration,
        curves and working area are cleared, after asking)."""
        if not self._need_image():
            return
        if self.work_box is None:
            messagebox.showwarning("Crop", "Draw a working area around the figure first ('Set working area'; the "
                                           "handles move, resize and rotate it), then press this button.")
            return
        H, W = self.rgb.shape[:2]
        c = self._work_corners()
        x0, y0 = max(int(np.floor(c[:, 0].min())), 0), max(int(np.floor(c[:, 1].min())), 0)
        x1, y1 = min(int(np.ceil(c[:, 0].max())) + 1, W), min(int(np.ceil(c[:, 1].max())) + 1, H)
        if x1 - x0 < 10 or y1 - y0 < 10:
            messagebox.showwarning("Crop", "The working area is too small (or outside the image).")
            return
        if not self._confirm_geometry_change("Crop", "Cropping the image"):
            return
        new = self.rgb.copy()
        if abs(self._work_angle()) > 0.01:
            new[~self._work_mask((H, W))] = 255.0
        self._push_undo()
        self.rgb = new[y0:y1, x0:x1].copy()
        self._new_geometry()
        self._set_status(f"Cropped to {x1 - x0} x {y1 - y0} px (it was {W} x {H}). 'Undo' restores the image. "
                         "Now calibrate on the Digitize tab, or clean further here.", ok=True, sticky=True)
        self._extract_all()

    def rotate_image(self, angle):
        """Rotate the image counter-clockwise by `angle` degrees, keeping all of it (white corners). Pixel
        positions change, so calibration, curves and working area are cleared (asked first if there are any)."""
        if not self._confirm_geometry_change("Straighten", "Rotating the image"):
            self._redraw()
            return
        img = Image.fromarray(self.rgb.astype(np.uint8)).rotate(
            angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))
        self._push_undo()
        self.rgb = np.asarray(img, dtype=float)
        self._new_geometry()
        self._set_status(f"Image rotated by {angle:+.2f}° (counter-clockwise). 'Undo' restores it. "
                         "Now calibrate and digitize on the Digitize tab, or clean further here.",
                         ok=True, sticky=True)
        self._extract_all()

    def _new_geometry(self):
        """The image size changed: show the new array and forget everything tied to pixel positions."""
        H, W = self.rgb.shape[:2]
        self.im.set_data(self.rgb.astype(np.uint8))
        self.im.set_extent((-0.5, W - 0.5, H - 0.5, -0.5))
        self.work_box = None
        self.v_angle.set('0')
        self.traces = []
        for key in self.cal_px:
            self.cal_px[key] = None
            self.cal_var[key].set('')
        self._update_px_labels()
        self._nudge = None
        self._refresh_list()
        self._fit_view()

    def _on_ctrl_z(self, event, redo):
        if isinstance(event.widget, (tk.Entry, tk.Spinbox, tk.Text, ttk.Combobox)):
            return                                   # a text field has its own undo
        if self.rgb is None:
            return
        if self._clean_tab_active():
            (self.redo_filter if redo else self.undo_filter)()
        elif self.nb.index(self.nb.select()) == 1:
            (self.redo_stroke if redo else self.undo_stroke)()
        return 'break'

    def _push_undo(self):
        """Keep the current image as the next 'Undo' step; a new edit ends the redo history."""
        self.rgb_undo.append(self.rgb)
        self.rgb_redo = []

    def undo_filter(self):
        if not self.rgb_undo:
            self._set_status("Nothing to undo.", sticky=True)
            return
        resized = self.rgb_undo[-1].shape != self.rgb.shape
        self.rgb_redo.append(self.rgb)
        self.rgb = self.rgb_undo.pop()
        if resized:                              # undoing a rotation or crop: the pixel positions change again
            self._new_geometry()
        else:
            self.im.set_data(self.rgb.astype(np.uint8))
        self._set_status("Image edit undone." if not resized else "Rotation / crop undone (calibration and curves "
                         "were cleared by it).", ok=True, sticky=True)
        self._extract_all()

    def redo_filter(self):
        if not self.rgb_redo:
            self._set_status("Nothing to redo.", sticky=True)
            return
        resized = self.rgb_redo[-1].shape != self.rgb.shape
        self.rgb_undo.append(self.rgb)
        self.rgb = self.rgb_redo.pop()
        if resized:
            self._new_geometry()
        else:
            self.im.set_data(self.rgb.astype(np.uint8))
        self._set_status("Image edit redone." if not resized else "Rotation / crop redone (calibration and curves "
                         "are cleared by it).", ok=True, sticky=True)
        self._extract_all()

    def _on_rect(self, eclick, erelease):
        if self.mode not in ('roi', 'excl', 'wipe', 'work', 'erase', 'keep') or eclick.xdata is None or erelease.xdata is None:
            return
        box = (min(eclick.xdata, erelease.xdata), min(eclick.ydata, erelease.ydata),
               max(eclick.xdata, erelease.xdata), max(eclick.ydata, erelease.ydata))
        if self.mode in ('erase', 'keep'):
            H, W = self.rgb.shape[:2]
            x0, y0 = max(int(round(box[0])), 0), max(int(round(box[1])), 0)
            x1, y1 = min(int(round(box[2])) + 1, W), min(int(round(box[3])) + 1, H)
            self.selector.set_visible(False)
            colors = self._pixel_colors(self.rgb[y0:y1, x0:x1])
            if not colors:
                self._set_status("That box is all white: drag it over the trace.")
            else:
                self._add_filter_colors(dict(box=box, colors=colors))
            return
        if self.mode == 'work':
            self.work_box = box
            self.v_angle.set('0')
            self._cancel_tool(keep_status=True)
            self._set_status("Working area set: 'Erase colours' / 'Keep only colours' now act only inside it.",
                             ok=True, sticky=True)
            self._redraw()
            return
        if self.mode == 'wipe':
            H, W = self.rgb.shape[:2]
            x0, y0 = max(int(round(box[0])), 0), max(int(round(box[1])), 0)
            x1, y1 = min(int(round(box[2])) + 1, W), min(int(round(box[3])) + 1, H)
            self._push_undo()
            self.rgb = self.rgb.copy()
            self.rgb[y0:y1, x0:x1] = 255.0
            self.im.set_data(self.rgb.astype(np.uint8))
            self._cancel_tool(keep_status=True)
            self._set_status("Area painted white. 'Undo' restores it.", ok=True, sticky=True)
            self._extract_all()
            return
        if self.mode == 'roi':
            n = len(self.traces) + 1
            names = {t['name'] for t in self.traces}
            while f"curve{n}" in names:
                n += 1
            self.traces.append(dict(name=f"curve{n}", roi=box, color=None, zero=None, exclude=[],
                                    xpx=np.array([]), ypx=np.array([])))
            self._refresh_list(select=len(self.traces) - 1)
        else:
            self._selected()['exclude'].append(box)
        self._cancel_tool()
        self._extract_all()

    # ---- traces ----
    def _selected(self):
        sel = self.listbox.curselection()
        if sel:
            return self.traces[sel[0]]
        return self.traces[0] if len(self.traces) == 1 else None

    def _refresh_list(self, select=None):
        keep = self.listbox.curselection()
        self.listbox.delete(0, tk.END)
        for t in self.traces:
            self.listbox.insert(tk.END, t['name'])
        if select is not None:
            self.listbox.selection_set(select)
        elif keep and keep[0] < len(self.traces):
            self.listbox.selection_set(keep[0])

    def rename_trace(self):
        tr = self._selected()
        if tr is None:
            return
        res = self._ask_form("Rename", [("New name:", tr['name'])], width=50)
        if not res:
            return
        new = res[0].strip()
        if not new or new == tr['name']:
            return
        if any(t['name'] == new for t in self.traces):
            messagebox.showwarning("Rename", f"A curve called '{new}' already exists.")
            return
        tr['name'] = new
        self._refresh_list()
        self._redraw()

    def remove_trace(self):
        tr = self._selected()
        if tr is None:
            return
        self.traces.remove(tr)
        self._refresh_list()
        self._redraw()

    def use_any(self):
        """The curve is everything that is not white (its shades differ: black, violet, pink...)."""
        tr = self._selected()
        if tr is None:
            messagebox.showwarning("Spectrum Digitizer", "Add and select a curve first.")
            return
        tr['color'] = 'any'
        self._extract_all()
        self._set_status("The curve is now every non-white pixel in its box (the 'Colour tolerance' in the advanced "
                         "settings is how far from white a pixel must be). Erase what is not the curve first.",
                         ok=True, sticky=True)

    def use_dark(self):
        tr = self._selected()
        if tr is None:
            return
        tr['color'] = None
        self._extract_all()

    def auto_panels(self):
        """Origin-style figures: frame -> panels, X calibration from the frame edges, zero from the tick."""
        if not self._need_image():
            return
        if self.traces and not messagebox.askyesno(
                "Auto-detect panels", "This replaces the current curves. Continue?"):
            return
        thresh = self._to_float(self.v_thresh.get()) or 128.0
        dark = (self.rgb @ np.array([0.299, 0.587, 0.114])) < thresh
        found = find_frame(dark)
        if found is None:
            messagebox.showwarning("Auto-detect panels",
                                   "Frame not found: it needs at least two long vertical and two long "
                                   "horizontal lines. Calibrate the axes and use 'Add curve' instead.")
            return
        left, right, rows = found
        res = self._ask_form("Auto-detect panels",
                             [("X value at the LEFT edge of the frame:", self.cal_var['x1'].get()),
                              ("X value at the RIGHT edge of the frame:", self.cal_var['x2'].get())])
        if res is None:
            return
        xmin, xmax = self._to_float(res[0]), self._to_float(res[1])
        if xmin is None or xmax is None or xmin == xmax:
            messagebox.showwarning("Auto-detect panels", "Enter two different numbers.")
            return
        xl, xr = float(left.mean()), float(right.mean())
        self._set_cal('x', (xl, xmin), (xr, xmax))
        self._clear_axis('y')
        self.traces = []
        x_in0, x_in1 = left[-1] + 3, right[0] - 3
        for k in range(len(rows) - 1):
            top, bottom = rows[k][-1] + 1, rows[k + 1][0]
            my = int(round(0.03 * (bottom - top)))
            zero = find_zero(dark, left, top, bottom)
            letter = (x_in0, top, x_in0 + 0.12 * (xr - xl), top + 0.15 * (bottom - top))
            self.traces.append(dict(name=chr(ord('a') + k) if k < 26 else f"p{k + 1}",
                                    roi=(x_in0, top + my, x_in1, bottom - my), color=None, zero=zero,
                                    exclude=[letter], xpx=np.array([]), ypx=np.array([])))
        self._refresh_list(select=0)
        self._extract_all()
        self._set_status(f"{len(self.traces)} panel(s) found. Y is in pixels above each panel's zero tick: "
                         "use 'Normalize: max', or calibrate the Y axis, as needed.", ok=True, sticky=True)

    # ---- computation ----
    def _num(self, var, default, lo=None):
        v = self._to_float(var.get())
        return default if v is None or (lo is not None and v < lo) else v

    def _schedule(self, extract):
        if self._update_job is not None:
            self.root.after_cancel(self._update_job)
        self._update_job = self.root.after(250, self._extract_all if extract else self._redraw)

    def _extract_all(self):
        self._update_job = None
        if self.rgb is None:
            return
        thresh = self._num(self.v_thresh, 128.0)
        tol = self._num(self.v_tol, 60.0, lo=1)
        dot = self._num(self.v_dot, 1.2, lo=0) / 100.0
        for t in self.traces:
            t['xpx'], t['ypx'] = extract_points(self.rgb, t['roi'], t['color'], thresh, tol, dot, t['exclude'],
                                                   t.get('guide') or ())
        self._redraw()

    def build_output(self):
        """(grid, {name: values}) for all traces, or None if not computable."""
        xcal, ycal = self.xcal, self.ycal
        if xcal is None:
            return None
        step = self._num(self.v_step, 1.0, lo=1e-12)
        if step <= 0:
            return None
        smooth = self._num(self.v_smooth, 0.0, lo=0)
        poly = int(self._num(self.v_poly, 3, lo=1))
        # An edge point is kept up to 2 px outside the data (the pixel size in X units).
        tol = 2 * abs((xcal[1][1] - xcal[0][1]) / (xcal[1][0] - xcal[0][0]))
        curves = []
        for t in self.traces:
            if len(t['xpx']) < 2:
                continue
            x, y = trace_values(t['xpx'], t['ypx'], xcal, ycal, t['zero'], t['roi'][3])
            curves.append((t['name'], x, y))
        if not curves:
            return None
        grid = make_grid(min(c[1].min() for c in curves), max(c[1].max() for c in curves), step)
        if len(grid) < 2:
            return None
        cols = {}
        for name, x, y in curves:
            v = resample(x, y, grid, tol)
            if smooth > 0:
                v = smooth_values(v, round(smooth / step), poly)
            if self.v_norm.get() == 'max' and np.isfinite(v).any():
                top = np.nanmax(v)
                if top != 0:
                    v = v / top
            cols[name] = v
        return grid, cols

    # ---- drawing ----
    def _redraw(self):
        self._update_job = None
        for a in self._overlay:
            try:
                a.remove()
            except Exception:
                pass
        self._overlay = []
        if self.rgb is None:
            self._update_hint()
            return
        try:
            self._draw_overlays()
        except Exception as e:  # keep the result plot alive and say what happened
            self._set_status(f"Display error (the result below is still computed): {e}", sticky=True)
        self.canvas.draw_idle()
        self._redraw_result()
        self._update_hint()

    def _draw_overlays(self):
        ax = self.ax_img
        H, W = self.rgb.shape[:2]
        sel = self._selected()
        for key, p in self.cal_px.items():
            if p is None:
                continue
            is_x = key[0] == 'x'
            col = 'tab:blue' if is_x else 'tab:green'
            active = self._nudge == ('cal', key)
            lw = 1.4 if active else 0.8
            if active:
                col = 'darkorange'
            ln = ax.axvline(p, color=col, ls='--', lw=lw) if is_x else ax.axhline(p, color=col, ls='--', lw=lw)
            v = self.cal_var[key].get().strip()
            lab = f"{key[0].upper()}{key[1]}" + (f" = {v}" if v else "")
            txt = ax.text(p if is_x else 3, 3 if is_x else p, " " + lab, color=col, fontsize=9,
                          fontweight='bold', va='top' if is_x else 'bottom', clip_on=True,
                          ha='right' if key == 'x2' else 'left')
            self._overlay += [ln, txt]
        if self.work_box is not None and self._clean_tab_active():
            r = Polygon(self._work_corners(), closed=True, fill=False, ec='royalblue', ls='--', lw=1.2)
            ax.add_patch(r)
            self._overlay.append(r)
            hs = self._work_handles()
            rot, sizes = hs[0], hs[1:]
            top = next(h for h in sizes if (h[1], h[2]) == (0, -1))
            ln, = ax.plot([top[3], rot[3]], [top[4], rot[4]], color='royalblue', lw=1)
            sq, = ax.plot([h[3] for h in sizes], [h[4] for h in sizes], 's', ms=6, mfc='white',
                          mec='royalblue', mew=1.2)
            rh, = ax.plot([rot[3]], [rot[4]], 'o', ms=7, mfc='white', mec='royalblue', mew=1.2)
            self._overlay += [ln, sq, rh]
        if self.v_grid.get():
            self._draw_check_grid(ax)
        self._draw_guides(ax)
        for px, py in self._straight_pts:
            mk, = ax.plot([px], [py], '+', color='magenta', ms=12, mew=1.5)
            self._overlay.append(mk)
        for e in self._filter_pts:
            if 'box' in e:
                b = e['box']
                mk = Rectangle((b[0], b[1]), b[2] - b[0], b[3] - b[1], fill=False, ec='magenta', lw=1.2)
                ax.add_patch(mk)
            else:
                mk, = ax.plot([e['x']], [e['y']], 'x', color='magenta', ms=8, mew=1.5)
            self._overlay.append(mk)
        for i, t in enumerate(self.traces):
            col = PALETTE[i % len(PALETTE)]
            x0, y0, x1, y1 = t['roi']
            r = Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, ec=col, lw=2.0 if t is sel else 0.9)
            ax.add_patch(r)
            self._overlay.append(r)
            for ex in t['exclude']:
                e = Rectangle((ex[0], ex[1]), ex[2] - ex[0], ex[3] - ex[1], fc='gray', alpha=0.35, ec='gray', lw=0.5)
                ax.add_patch(e)
                self._overlay.append(e)
            if t['zero'] is not None and self.ycal is None:
                z, = ax.plot([x0, x1], [t['zero']] * 2, color=col, ls=':', lw=1)
                self._overlay.append(z)
            if self.v_points.get() and len(t['xpx']):
                ln, = ax.plot(t['xpx'], t['ypx'], '.', color='red', ms=1.5, alpha=0.7)
                self._overlay.append(ln)

    def _redraw_result(self):
        try:
            self._draw_result()
        except Exception as e:
            self._set_status(f"Result plot error: {e}", sticky=True)

    def _draw_result(self):
        out = self.build_output()
        self._plot_result(self.ax_res, out, big=False)
        self.canvas_res.draw_idle()
        if self._view_is_result():        # the large plot is drawn only while it is shown
            self._plot_result(self.ax_big, out, big=True)
            self.canvas_big.draw_idle()
        if out is None:
            self.info.config(text="")
        else:
            grid, cols = out
            self.info.config(text=f"{len(grid)} points, X {grid[0]:g} .. {grid[-1]:g}; Y: {self._y_description()}")

    def _y_description(self):
        if self.ycal is not None:
            return "calibrated"
        return "normalized to the maximum" if self.v_norm.get() == 'max' else "pixels above the zero line"

    def _plot_result(self, ax, out, big):
        """Draw the digitized curves (`out` = build_output()) on `ax`: a small preview or the large plot."""
        ax.clear()
        ax.tick_params(labelsize=10 if big else 7)
        ax.grid(True, ls=':', alpha=0.5)
        if out is None:
            msg = self.missing_reason() or "Nothing to show: check the X step (it must be a positive number)."
            ax.text(0.5, 0.5, msg, ha='center', va='center', transform=ax.transAxes, fontsize=11 if big else 9,
                    color='#aa0000', wrap=True)
            return
        grid, cols = out
        for i, t in enumerate(self.traces):
            if t['name'] in cols:
                ax.plot(grid, cols[t['name']], color=PALETTE[i % len(PALETTE)], lw=1.6 if big else 1, label=t['name'])
        if big:
            ax.set_xlabel("X", fontsize=11)
            ax.set_ylabel(f"Y ({self._y_description()})", fontsize=11)
            if self.image_path:
                ax.set_title(os.path.basename(self.image_path), fontsize=10, color='#555555')
            ax.legend(fontsize=10)
            self.fig_big.tight_layout()
        elif len(cols) > 1:
            ax.legend(fontsize=7)

    # ---- dialogs ----
    def _ask_form(self, title, fields, width=24):
        """Modal dialog with one text field per (label, initial) pair; returns the strings or None."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        out = []
        entries = []
        first = None
        for label, initial in fields:
            tk.Label(dlg, text=label, anchor='w').pack(fill=tk.X, padx=12, pady=(10, 0))
            var = tk.StringVar(value=initial)
            e = tk.Entry(dlg, textvariable=var, width=width)
            e.pack(fill=tk.X, padx=12, pady=2)
            entries.append(var)
            first = first or e

        def ok(*_):
            out.extend(v.get() for v in entries)
            dlg.destroy()

        row = tk.Frame(dlg)
        row.pack(fill=tk.X, padx=12, pady=12)
        tk.Button(row, text="Cancel", width=9, command=dlg.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        tk.Button(row, text="OK", width=9, command=ok).pack(side=tk.RIGHT)
        dlg.bind('<Return>', ok)
        dlg.bind('<Escape>', lambda e: dlg.destroy())
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dlg.winfo_reqheight()) // 3
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        first.focus_set()
        first.select_range(0, tk.END)
        dlg.grab_set()
        self.root.wait_window(dlg)
        return out if out else None

    # ---- export ----
    def copy_table(self):
        """The result table as tab-separated text on the clipboard, ready to paste into Excel (X in the first column,
        one column per curve, empty cells where a curve has no data). With the decimal comma box ticked the numbers
        use ',' like an Italian Excel expects: with a '.' it would take them for text or dates."""
        if not self._need_image():
            return
        if self.xcal is None:
            messagebox.showwarning("Copy table", "Calibrate the X axis first (step 1).")
            return
        out = self.build_output()
        if out is None:
            messagebox.showwarning("Copy table", "No curve points to copy: add a curve first (step 2).")
            return
        grid, cols = out
        fmt = (lambda v: f"{v:.6g}".replace('.', ',')) if self.v_comma.get() else (lambda v: f"{v:.6g}")
        lines = ['\t'.join(['X'] + list(cols))]
        for i, x in enumerate(grid):
            lines.append('\t'.join([fmt(x)] + ["" if np.isnan(c[i]) else fmt(c[i]) for c in cols.values()]))
        self.root.clipboard_clear()
        self.root.clipboard_append('\n'.join(lines) + '\n')
        self._set_status(f"Table copied: {len(grid)} rows, {len(cols)} curve(s) "
                         f"({'decimal comma' if self.v_comma.get() else 'decimal point'}). Paste it in Excel.",
                         ok=True, sticky=True)

    def export_csv(self):
        if not self._need_image():
            return
        if self.xcal is None:
            messagebox.showwarning("Export CSV", "Calibrate the X axis first (step 1).")
            return
        out = self.build_output()
        if out is None:
            messagebox.showwarning("Export CSV", "No curve points to export: add a curve first (step 2).")
            return
        grid, cols = out
        default = os.path.splitext(os.path.basename(self.image_path))[0] + ".csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default, initialdir=os.path.dirname(self.image_path),
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        # Comma-separated with the X values in the first column: the layout LabSpectrumManager's
        # generic CSV reader expects; empty cells where a curve has no data.
        with open(path, 'w', newline='', encoding='utf-8') as f:
            wr = csv.writer(f)
            wr.writerow(['X'] + list(cols))
            for i, x in enumerate(grid):
                wr.writerow([f"{x:.6g}"] + ["" if np.isnan(c[i]) else f"{c[i]:.6g}" for c in cols.values()])
        self._set_status(f"Saved {os.path.basename(path)} ({len(grid)} points, {len(cols)} curve(s)).", ok=True,
                         sticky=True)


if __name__ == "__main__":
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    SpectrumDigitizer(root)
    root.mainloop()
