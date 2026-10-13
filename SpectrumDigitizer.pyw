"""SpectrumDigitizer - turn an image of a spectrum plot (PNG/JPG) into numbers.

Open the image, calibrate the axes by clicking two points of known value on each, draw a box
around every curve to extract, and export a CSV with one column per curve on a regular X grid
(readable by LabSpectrumManager through File -> Open). Curves can be dark lines/dots (Origin-style
plots) or coloured (pick the colour with a click); stacked, framed panels can be found
automatically.

The extraction functions at the top of the file do not need Tk, so the tests can import them.
"""
import csv
import re
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageTk
from scipy import ndimage as ndi
from scipy.signal import savgol_filter

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.patches import Rectangle
from matplotlib.widgets import RectangleSelector

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False

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

    color=None selects dark pixels (gray < thresh); otherwise pixels whose RGB distance from
    `color` is below `tol`."""
    if color is None:
        gray = crop @ np.array([0.299, 0.587, 0.114])
        return gray < thresh, 255.0 - gray
    dist = np.sqrt(((crop - np.asarray(color, float)) ** 2).sum(axis=2))
    return dist < tol, (1.0 - dist / tol) + 0.05


def extract_points(rgb, roi, color=None, thresh=128, tol=60, dot_max_frac=0.012, exclude=()):
    """Curve points (x_px, y_px) found inside `roi` = (x0, y0, x1, y1), sorted by x.

    Isolated blobs up to `dot_max_frac` of the image width are single dots and give one
    centroid each; larger blobs (continuous curve, or dots merged together) give one centroid
    per pixel column. Thin vertical slivers (leftover frame/ticks) and `exclude` boxes (panel
    letters...) are ignored. Returns two empty arrays if nothing is found."""
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
        self.rgb = None
        self.pil = None
        # Calibration: two points per axis; the pixel position is clicked on the image, the value
        # is typed in the panel. 'x1','x2' hold a pixel column, 'y1','y2' a pixel row.
        self.cal_px = {k: None for k in ('x1', 'x2', 'y1', 'y2')}
        self.cal_var = {k: tk.StringVar() for k in self.cal_px}
        self.traces = []          # dicts: name, roi, color, zero, exclude, xpx, ypx
        self.mode = None          # 'pick' | 'color' | 'zero' | 'roi' | 'excl' | None
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
        fm.add_command(label="Export CSV...", command=self.export_csv)
        fm.add_separator()
        fm.add_command(label="Exit", command=root.destroy)
        menu.add_cascade(label="File", menu=fm)
        root.config(menu=menu)
        root.bind('<Escape>', lambda e: self._on_escape())
        # Arrow keys fine-tune the last point that was set (1 px; with Shift 0.2 px).
        for name, dx, dy in (('Left', -1, 0), ('Right', 1, 0), ('Up', 0, -1), ('Down', 0, 1)):
            root.bind(f'<{name}>', lambda e, dx=dx, dy=dy: self._on_arrow(e, dx, dy, False))
            root.bind(f'<Shift-{name}>', lambda e, dx=dx, dy=dy: self._on_arrow(e, dx, dy, True))
        if HAS_DND:
            root.drop_target_register(DND_FILES)
            root.dnd_bind('<<Drop>>', lambda e: self.load_image(root.tk.splitlist(e.data)[0].strip('{}')))

        paned = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=4)
        paned.pack(fill=tk.BOTH, expand=True)

        # Left: image (large) with a small live preview of the result underneath. No matplotlib
        # toolbar: its pan/zoom tools swallow the clicks of the calibration and trace tools, so
        # navigation is done directly (wheel = zoom, drag = move) and never conflicts with them.
        f_plot = tk.Frame(paned)
        bar = tk.Frame(f_plot)
        bar.pack(fill=tk.X)
        tk.Button(bar, text="Fit to window", command=self._fit_view).pack(side=tk.LEFT, padx=4, pady=2)
        tk.Button(bar, text="Zoom +", command=lambda: self._zoom_step(0.7)).pack(side=tk.LEFT, pady=2)
        tk.Button(bar, text="Zoom −", command=lambda: self._zoom_step(1 / 0.7)).pack(side=tk.LEFT, padx=4, pady=2)
        tk.Label(bar, text="Mouse wheel = zoom,  drag = move the image", fg='#666666').pack(side=tk.LEFT, padx=8)
        # Image and result preview are separate canvases in a vertical paned window, so the
        # boundary can be dragged (a tall image needs the room more than the preview does).
        vp = tk.PanedWindow(f_plot, orient=tk.VERTICAL, sashrelief=tk.RAISED, sashwidth=6)
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
        for ev, fn in (('button_press_event', self._on_press), ('button_release_event', self._on_release),
                       ('motion_notify_event', self._on_motion), ('scroll_event', self._on_scroll)):
            self.canvas.mpl_connect(ev, fn)
        self.selector = RectangleSelector(self.ax_img, self._on_rect, useblit=True, button=[1],
                                          interactive=False, minspanx=3, minspany=3,
                                          props=dict(facecolor='none', edgecolor='red', linestyle='--'))
        self.selector.set_active(False)
        paned.add(f_plot, width=980)

        self.f_right = tk.Frame(paned, bg='#f0f4f7')
        self._build_panel(self.f_right)
        paned.add(self.f_right, width=400)

        self._show_empty()
        self._update_hint()
        if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
            self.load_image(sys.argv[1])

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

        # 1. axes
        self.l_axes = tk.Label(parent, text="1. AXES", font=('Arial', 10, 'bold'), bg=bg, anchor='w')
        self.l_axes.pack(fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(parent, text="Pick a point of known value on the image, then type its value.",
                 bg=bg, fg='#555555', font=('Arial', 8), anchor='w').pack(fill=tk.X, padx=10)
        self.pick_buttons = {}
        self.px_labels = {}
        grid = tk.Frame(parent, bg=bg)
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
        tk.Button(parent, text="Clear Y axis", command=lambda: self._clear_axis('y'), font=('Arial', 8)).pack(
            anchor='e', padx=10)

        # 2. curves
        tk.Label(parent, text="2. CURVES", font=('Arial', 10, 'bold'), bg=bg, anchor='w').pack(
            fill=tk.X, padx=10, pady=(6, 0))
        row = tk.Frame(parent, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Button(row, text="Add curve (drag a box)", command=lambda: self._start_rect('roi'),
                  bg='#c8e6c9', font=('Arial', 9, 'bold')).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Auto-detect panels", command=self.auto_panels,
                  bg='#e3f2fd').pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        self.listbox = tk.Listbox(parent, height=3, exportselection=False, font=('Arial', 9))
        self.listbox.pack(fill=tk.X, padx=10, pady=4)
        self.listbox.bind('<<ListboxSelect>>', lambda e: self._redraw())
        self.listbox.bind('<Double-Button-1>', lambda e: self.rename_trace())
        row = tk.Frame(parent, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Button(row, text="Rename", command=self.rename_trace).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Remove", command=self.remove_trace).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Label(parent, text="For the selected curve:", bg=bg, fg='#555555', font=('Arial', 8), anchor='w').pack(
            fill=tk.X, padx=10, pady=(4, 0))
        row = tk.Frame(parent, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Button(row, text="Pick its colour", command=lambda: self._start_point('color')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Black curve", command=self.use_dark).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        row = tk.Frame(parent, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Button(row, text="Ignore an area", command=lambda: self._start_rect('excl')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Set zero line", command=lambda: self._start_point('zero')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        # 3. output
        tk.Label(parent, text="3. OUTPUT", font=('Arial', 10, 'bold'), bg=bg, anchor='w').pack(
            fill=tk.X, padx=10, pady=(6, 0))
        grid = tk.Frame(parent, bg=bg)
        grid.pack(fill=tk.X, padx=10)
        self.v_step = tk.StringVar(value='1')
        self.v_smooth = tk.StringVar(value='0')
        self.v_norm = tk.StringVar(value='none')
        self.v_points = tk.BooleanVar(value=True)
        for r, (label, var) in enumerate((("X step", self.v_step), ("Smoothing window (X units, 0 = off)", self.v_smooth))):
            tk.Label(grid, text=label, bg=bg, anchor='w', font=('Arial', 9)).grid(row=r, column=0, sticky='w', pady=1)
            tk.Entry(grid, textvariable=var, width=8).grid(row=r, column=1, padx=6)
            var.trace_add('write', lambda *_: self._schedule(False))
        tk.Label(grid, text="Normalize", bg=bg, anchor='w', font=('Arial', 9)).grid(row=2, column=0, sticky='w')
        cb = ttk.Combobox(grid, textvariable=self.v_norm, values=['none', 'max'], width=6, state='readonly')
        cb.grid(row=2, column=1, padx=6, pady=1)
        cb.bind('<<ComboboxSelected>>', lambda e: self._redraw())
        tk.Checkbutton(parent, text="Show the extracted points on the image", variable=self.v_points,
                       bg=bg, command=self._redraw, anchor='w').pack(fill=tk.X, padx=10)

        self.v_adv = tk.BooleanVar(value=False)
        self.adv_check = tk.Checkbutton(parent, text="Advanced extraction settings", variable=self.v_adv,
                                        bg=bg, anchor='w', command=self._toggle_advanced)
        self.adv_check.pack(fill=tk.X, padx=10)
        self.f_adv = tk.Frame(parent, bg=bg)
        self.v_thresh = tk.StringVar(value='128')
        self.v_tol = tk.StringVar(value='60')
        self.v_dot = tk.StringVar(value='1.2')
        self.v_poly = tk.StringVar(value='3')
        for r, (label, var, extract) in enumerate((("Dark threshold (0-255)", self.v_thresh, True),
                                                   ("Colour tolerance", self.v_tol, True),
                                                   ("Max single-dot size (% of width)", self.v_dot, True),
                                                   ("Smoothing polynomial order", self.v_poly, False))):
            tk.Label(self.f_adv, text=label, bg=bg, anchor='w', font=('Arial', 9)).grid(row=r, column=0, sticky='w', pady=1)
            tk.Entry(self.f_adv, textvariable=var, width=8).grid(row=r, column=1, padx=6)
            var.trace_add('write', lambda *_, e=extract: self._schedule(e))

        self.info = tk.Label(parent, text="", bg=bg, font=('Consolas', 8), fg='#555555',
                             wraplength=370, justify='left', anchor='w')
        self.info.pack(fill=tk.X, padx=10, pady=4)

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

    def load_image(self, path):
        try:
            img = Image.open(path).convert('RGB')
        except Exception as e:
            messagebox.showerror("Open image", f"Cannot open the image:\n{e}")
            return
        self.image_path = path
        self.pil = img
        self.rgb = np.asarray(img, dtype=float)
        self.traces = []
        for axis in ('x', 'y'):
            for key in (axis + '1', axis + '2'):
                self.cal_px[key] = None
                self.cal_var[key].set('')
        self._update_px_labels()
        self._nudge = None
        self._cancel_tool()
        self.ax_img.clear()
        # The image is shown as it is (default interpolation: no pixel dropping when zoomed out).
        self.ax_img.imshow(self.rgb.astype(np.uint8))
        self.ax_img.set_aspect('equal', adjustable='box')
        self.ax_img.set_axis_off()
        self._overlay = []
        self.root.title(f"Spectrum Digitizer - {os.path.basename(path)}")
        self._refresh_list()
        self._fit_view()
        self._redraw()

    def _show_empty(self):
        self.ax_img.clear()
        self.ax_img.set_axis_off()
        self.ax_res.clear()
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
        # Drag = move the image, unless a tool is waiting for a click/box (then only the
        # right and middle buttons move it).
        if event.button in (2, 3) or (event.button == 1 and self.mode is None):
            self._pan = (event.x, event.y, ax.get_xlim(), ax.get_ylim())
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
        elif self.mode == 'zero':
            tr = self._selected()
            tr['zero'] = float(y)
            self._nudge = ('zero', tr)
            self._cancel_tool(keep_status=True)
            self._set_status("Zero line set: fine-tune it with the ↑↓ keys (Shift = finer).", ok=True, sticky=True)
            self._redraw()

    def _on_release(self, event):
        if self._pan is not None:
            self._pan = None
            self.canvas.get_tk_widget().config(cursor='crosshair' if self.mode else '')

    def _on_motion(self, event):
        if self.rgb is None:
            return
        if self._pan is not None:
            x0, y0, xlim, ylim = self._pan
            bb = self.ax_img.get_window_extent()
            sx = (xlim[1] - xlim[0]) / bb.width
            sy = (ylim[1] - ylim[0]) / bb.height
            dx, dy = (event.x - x0) * sx, (event.y - y0) * sy
            self.ax_img.set_xlim(xlim[0] - dx, xlim[1] - dx)
            self.ax_img.set_ylim(ylim[0] + dy, ylim[1] + dy)
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
        elif self.xcal is None:
            self._set_status("Step 1 - X axis: press 'Pick' next to 'X point 1', click on a tick of the X axis, "
                             "then type its value. Do the same for 'X point 2' on another tick, far from the first. "
                             "(Wheel = zoom for precision.)\n" + self.missing_reason())
        elif not self.traces:
            self._set_status("Step 2 - press 'Add curve' and drag a box around one curve "
                             "(or try 'Auto-detect panels' for framed Origin-style plots).", ok=True)
        elif not any(len(t['xpx']) for t in self.traces):
            self._set_status("No curve points found in the box: draw the box again around the curve, or "
                             "check the curve colour / black-curve setting.")
        else:
            self._set_status("Step 3 - check the red points on the curve and the result below the image, "
                             "adjust step / smoothing if needed, then press 'Export CSV'.", ok=True)

    def _cancel_tool(self, keep_status=False):
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

    def _on_rect(self, eclick, erelease):
        if self.mode not in ('roi', 'excl') or eclick.xdata is None or erelease.xdata is None:
            return
        box = (min(eclick.xdata, erelease.xdata), min(eclick.ydata, erelease.ydata),
               max(eclick.xdata, erelease.xdata), max(eclick.ydata, erelease.ydata))
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
            t['xpx'], t['ypx'] = extract_points(self.rgb, t['roi'], t['color'], thresh, tol, dot, t['exclude'])
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
        ax = self.ax_res
        ax.clear()
        ax.tick_params(labelsize=7)
        ax.grid(True, ls=':', alpha=0.5)
        out = self.build_output()
        if out is None:
            msg = self.missing_reason() or "Nothing to show: check the X step (it must be a positive number)."
            ax.text(0.5, 0.5, msg, ha='center', va='center', transform=ax.transAxes, fontsize=9,
                    color='#aa0000', wrap=True)
            self.info.config(text="")
        else:
            grid, cols = out
            for i, t in enumerate(self.traces):
                if t['name'] in cols:
                    ax.plot(grid, cols[t['name']], color=PALETTE[i % len(PALETTE)], lw=1, label=t['name'])
            if len(cols) > 1:
                ax.legend(fontsize=7)
            ycal = "calibrated" if self.ycal is not None else \
                ("normalized to the maximum" if self.v_norm.get() == 'max' else "pixels above the zero line")
            self.info.config(text=f"{len(grid)} points, X {grid[0]:g} .. {grid[-1]:g}; Y: {ycal}")
        self.canvas_res.draw_idle()

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
