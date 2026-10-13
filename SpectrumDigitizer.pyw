"""SpectrumDigitizer - turn an image of a spectrum plot (PNG/JPG) into numbers.

Open the image, calibrate the axes by clicking two points of known value on each, draw a box
around every curve to extract, and export a CSV with one column per curve on a regular X grid
(readable by LabSpectrumManager through File -> Open). Curves can be dark lines/dots (Origin-style
plots) or coloured (pick the colour with a click); stacked, framed panels can be found
automatically.

The extraction functions at the top of the file do not need Tk, so the tests can import them.
"""
import csv
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from scipy.signal import savgol_filter

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
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
    pts = np.array(sorted(pts))
    return pts[:, 0], pts[:, 1]


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

class SpectrumDigitizer:
    def __init__(self, root):
        self.root = root
        root.title("Spectrum Digitizer")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1400, int(sw * 0.9)), min(860, int(sh * 0.8))
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

        self.image_path = None
        self.rgb = None
        self.xcal = None          # ((px1, v1), (px2, v2)) or None
        self.ycal = None
        self.traces = []          # dicts: name, roi, color, zero, exclude, xpx, ypx
        self.mode = None          # 'xcal' | 'ycal' | 'color' | 'zero' | 'roi' | 'excl' | None
        self._cal_pending = []    # points of the calibration being collected: (px, value)
        self._overlay = []
        self._update_job = None

        menu = tk.Menu(root)
        fm = tk.Menu(menu, tearoff=0)
        fm.add_command(label="Open Image...", command=self.open_dialog)
        fm.add_command(label="Export CSV...", command=self.export_csv)
        fm.add_separator()
        fm.add_command(label="Exit", command=root.destroy)
        menu.add_cascade(label="File", menu=fm)
        root.config(menu=menu)
        if HAS_DND:
            root.drop_target_register(DND_FILES)
            root.dnd_bind('<<Drop>>', lambda e: self.load_image(root.tk.splitlist(e.data)[0].strip('{}')))

        paned = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=4)
        paned.pack(fill=tk.BOTH, expand=True)

        f_plot = tk.Frame(paned)
        self.fig = Figure(figsize=(7, 7))
        gs = self.fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.22)
        self.ax_img = self.fig.add_subplot(gs[0])
        self.ax_res = self.fig.add_subplot(gs[1])
        self.fig.subplots_adjust(left=0.08, right=0.98, top=0.98, bottom=0.07)
        self.canvas = FigureCanvasTkAgg(self.fig, master=f_plot)
        self.toolbar = NavigationToolbar2Tk(self.canvas, f_plot)
        self.toolbar.update()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas.mpl_connect('button_press_event', self._on_click)
        self.selector = RectangleSelector(self.ax_img, self._on_rect, useblit=True, button=[1],
                                          interactive=False, minspanx=3, minspany=3,
                                          props=dict(facecolor='none', edgecolor='red', linestyle='--'))
        self.selector.set_active(False)
        paned.add(f_plot, width=1000)

        self.f_right = tk.Frame(paned, bg='#f0f4f7')
        self._build_panel(self.f_right)
        paned.add(self.f_right, width=380)

        self._show_empty()
        self._set_status("Open an image (File -> Open Image, or drop it on the window).")
        if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
            self.load_image(sys.argv[1])

    # ---- panel ----
    def _build_panel(self, parent):
        bg = '#f0f4f7'
        self.status = tk.Label(parent, text="", bg='#fff8e1', wraplength=350, justify='left',
                               anchor='w', font=('Arial', 9), relief=tk.GROOVE, padx=6, pady=6)
        self.status.pack(fill=tk.X, padx=10, pady=(10, 6))

        tk.Label(parent, text="1. CALIBRATION", font=('Arial', 9, 'bold'), bg=bg).pack(anchor='w', padx=10)
        row = tk.Frame(parent, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Button(row, text="Calibrate X axis", command=lambda: self._start_cal('xcal')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Calibrate Y axis", command=lambda: self._start_cal('ycal')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        self.cal_label = tk.Label(parent, text="", bg=bg, font=('Consolas', 8), fg='#555555',
                                  justify='left', anchor='w')
        self.cal_label.pack(fill=tk.X, padx=10)

        tk.Label(parent, text="2. TRACES", font=('Arial', 9, 'bold'), bg=bg).pack(anchor='w', padx=10, pady=(8, 0))
        row = tk.Frame(parent, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Button(row, text="Add trace (draw box)", command=lambda: self._start_rect('roi'),
                  bg='#e8f5e9').pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Auto-detect panels", command=self.auto_panels,
                  bg='#e3f2fd').pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        self.listbox = tk.Listbox(parent, height=6, exportselection=False, font=('Arial', 9))
        self.listbox.pack(fill=tk.X, padx=10, pady=4)
        self.listbox.bind('<<ListboxSelect>>', lambda e: self._redraw())
        self.listbox.bind('<Double-Button-1>', lambda e: self.rename_trace())
        row = tk.Frame(parent, bg=bg)
        row.pack(fill=tk.X, padx=10)
        tk.Button(row, text="Rename", command=self.rename_trace).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Remove", command=self.remove_trace).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        row = tk.Frame(parent, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Button(row, text="Pick curve colour", command=lambda: self._start_point('color')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Dark pixels", command=self.use_dark).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        row = tk.Frame(parent, bg=bg)
        row.pack(fill=tk.X, padx=10, pady=(2, 0))
        tk.Button(row, text="Exclude area", command=lambda: self._start_rect('excl')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Set zero line", command=lambda: self._start_point('zero')).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        tk.Label(parent, text="3. EXTRACTION AND OUTPUT", font=('Arial', 9, 'bold'),
                 bg=bg).pack(anchor='w', padx=10, pady=(8, 0))
        grid = tk.Frame(parent, bg=bg)
        grid.pack(fill=tk.X, padx=10)
        self.v_thresh = tk.StringVar(value='128')
        self.v_tol = tk.StringVar(value='60')
        self.v_dot = tk.StringVar(value='1.2')
        self.v_step = tk.StringVar(value='1')
        self.v_smooth = tk.StringVar(value='0')
        self.v_poly = tk.StringVar(value='3')
        self.v_norm = tk.StringVar(value='none')
        params = [("Dark threshold (0-255)", self.v_thresh, True),
                  ("Colour tolerance", self.v_tol, True),
                  ("Max dot size (% width)", self.v_dot, True),
                  ("X step", self.v_step, False),
                  ("Smoothing window (X units)", self.v_smooth, False),
                  ("Smoothing polynomial order", self.v_poly, False)]
        for r, (label, var, extract) in enumerate(params):
            tk.Label(grid, text=label, bg=bg, anchor='w', font=('Arial', 9)).grid(row=r, column=0, sticky='w', pady=1)
            tk.Entry(grid, textvariable=var, width=8).grid(row=r, column=1, padx=6)
            var.trace_add('write', lambda *_, e=extract: self._schedule(e))
        tk.Label(grid, text="Normalize", bg=bg, anchor='w', font=('Arial', 9)).grid(row=len(params), column=0, sticky='w')
        cb = ttk.Combobox(grid, textvariable=self.v_norm, values=['none', 'max'], width=6, state='readonly')
        cb.grid(row=len(params), column=1, padx=6, pady=1)
        cb.bind('<<ComboboxSelected>>', lambda e: self._redraw())
        self.info = tk.Label(parent, text="", bg=bg, font=('Consolas', 8), fg='#555555',
                             wraplength=350, justify='left', anchor='w')
        self.info.pack(fill=tk.X, padx=10, pady=4)
        tk.Button(parent, text="Export CSV...", command=self.export_csv, bg='#2ecc71', fg='white',
                  font=('Arial', 9, 'bold')).pack(fill=tk.X, padx=12, pady=8)

    def _set_status(self, text):
        self.status.config(text=text)

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
        self.rgb = np.asarray(img, dtype=float)
        self.xcal = self.ycal = None
        self.traces = []
        self._cal_pending = []
        self._set_mode(None)
        self.ax_img.clear()
        self.ax_img.imshow(self.rgb.astype(np.uint8), interpolation='nearest')
        self.ax_img.tick_params(labelsize=7)
        self.root.title(f"Spectrum Digitizer - {os.path.basename(path)}")
        self._refresh_list()
        self._set_status("Step 1: calibrate the X axis (and the Y axis, if you want calibrated ordinates), "
                         "then add the traces. For framed Origin-style plots try 'Auto-detect panels'.")
        self._redraw()
        self.toolbar.update()

    def _show_empty(self):
        self.ax_img.clear()
        self.ax_img.set_axis_off()
        self.ax_res.clear()
        self.canvas.draw_idle()

    # ---- modes and clicks ----
    def _toolbar_off(self):
        """Matplotlib's pan/zoom tool swallows canvas clicks while active."""
        m = getattr(self.toolbar.mode, 'value', self.toolbar.mode)
        if m and 'pan' in str(m).lower():
            self.toolbar.pan()
        elif m and 'zoom' in str(m).lower():
            self.toolbar.zoom()

    def _set_mode(self, mode):
        self.mode = mode
        self.selector.set_active(False)
        self.selector.set_visible(False)
        self.canvas.get_tk_widget().config(cursor='crosshair' if mode else '')

    def _need_image(self):
        if self.rgb is None:
            messagebox.showwarning("Spectrum Digitizer", "Open an image first.")
            return False
        return True

    def _start_cal(self, kind):
        if not self._need_image():
            return
        self._toolbar_off()
        self._cal_pending = []
        self._set_mode(kind)
        axis = 'X' if kind == 'xcal' else 'Y'
        where = "a tick of the X axis" if kind == 'xcal' else "a tick of the Y axis"
        self._set_status(f"Calibrate {axis}: click on {where} (zoom in first for precision), then type its value. "
                         "Then do the same on a second point, far from the first.")

    def _start_rect(self, purpose):
        if not self._need_image():
            return
        if purpose == 'excl' and self._selected() is None:
            messagebox.showwarning("Exclude area", "Add and select a trace first.")
            return
        self._toolbar_off()
        self._set_mode(purpose)
        self.selector.set_active(True)
        self._set_status("Drag a box around the curve (include only the curve: leave out axis labels, "
                         "legends and panel letters)." if purpose == 'roi' else
                         "Drag a box around what must be ignored in the selected trace (a panel letter, a legend...).")

    def _start_point(self, kind):
        tr = self._selected()
        if tr is None:
            messagebox.showwarning("Spectrum Digitizer", "Add and select a trace first.")
            return
        self._toolbar_off()
        self._set_mode(kind)
        self._set_status("Click on a pixel of the curve." if kind == 'color' else
                         "Click on the zero level of this trace (Y = height above this line).")

    def _on_click(self, event):
        if self.mode in (None, 'roi', 'excl') or event.inaxes is not self.ax_img or event.button != 1:
            return
        if getattr(self.toolbar.mode, 'value', self.toolbar.mode):
            return
        x, y = event.xdata, event.ydata
        if self.mode in ('xcal', 'ycal'):
            val = self._ask_form("Calibration", [("Value at this point:", "")])
            if val is None:
                return
            try:
                v = float(val[0].replace(',', '.'))
            except ValueError:
                messagebox.showwarning("Calibration", "Enter a number.")
                return
            self._cal_pending.append((x if self.mode == 'xcal' else y, v))
            if len(self._cal_pending) == 2:
                (p1, v1), (p2, v2) = self._cal_pending
                if abs(p2 - p1) < 5 or v1 == v2:
                    messagebox.showwarning("Calibration", "The two points must differ in both position and value.")
                    self._cal_pending = []
                else:
                    cal = tuple(self._cal_pending)
                    if self.mode == 'xcal':
                        self.xcal = cal
                    else:
                        self.ycal = cal
                    self._cal_pending = []
                    self._set_mode(None)
                    self._set_status("Calibration set. Add the traces (draw a box around each curve).")
                    self._extract_all()
                    return
            else:
                self._set_status("Now click on a second point, far from the first.")
            self._redraw()
        elif self.mode == 'color':
            px = self.rgb[int(round(y)), int(round(x))]
            if px.min() > 235:
                messagebox.showwarning("Pick curve colour", "That pixel is white: click right on the curve.")
                return
            tr = self._selected()
            tr['color'] = tuple(int(v) for v in px)
            self._set_mode(None)
            self._set_status(f"Curve colour set to RGB {tr['color']}.")
            self._extract_all()
        elif self.mode == 'zero':
            tr = self._selected()
            tr['zero'] = float(y)
            self._set_mode(None)
            self._set_status("Zero line set.")
            self._redraw()

    def _on_rect(self, eclick, erelease):
        if self.mode not in ('roi', 'excl') or eclick.xdata is None or erelease.xdata is None:
            return
        box = (min(eclick.xdata, erelease.xdata), min(eclick.ydata, erelease.ydata),
               max(eclick.xdata, erelease.xdata), max(eclick.ydata, erelease.ydata))
        if self.mode == 'roi':
            n = len(self.traces) + 1
            names = {t['name'] for t in self.traces}
            while f"trace{n}" in names:
                n += 1
            self.traces.append(dict(name=f"trace{n}", roi=box, color=None, zero=None, exclude=[],
                                    xpx=np.array([]), ypx=np.array([])))
            self._refresh_list(select=len(self.traces) - 1)
            self._set_status("Trace added. Use 'Pick curve colour' for a coloured curve, 'Exclude area' to "
                             "hide letters or legends, and the parameters below to tune the extraction.")
        else:
            self._selected()['exclude'].append(box)
            self._set_status("Area excluded.")
        self._set_mode(None)
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
            messagebox.showwarning("Rename", f"A trace called '{new}' already exists.")
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
                "Auto-detect panels", "This replaces the current traces. Continue?"):
            return
        try:
            thresh = float(self.v_thresh.get())
        except ValueError:
            thresh = 128.0
        dark = (self.rgb @ np.array([0.299, 0.587, 0.114])) < thresh
        found = find_frame(dark)
        if found is None:
            messagebox.showwarning("Auto-detect panels",
                                   "Frame not found: it needs at least two long vertical and two long "
                                   "horizontal lines. Use the manual calibration and 'Add trace' instead.")
            return
        left, right, rows = found
        res = self._ask_form("Auto-detect panels", [("X value at the left frame edge:", ""),
                                                    ("X value at the right frame edge:", "")])
        if res is None:
            return
        try:
            xmin, xmax = (float(v.replace(',', '.')) for v in res)
        except ValueError:
            messagebox.showwarning("Auto-detect panels", "Enter two numbers.")
            return
        if xmin == xmax:
            messagebox.showwarning("Auto-detect panels", "The two X values must differ.")
            return
        xl, xr = float(left.mean()), float(right.mean())
        self.xcal = ((xl, xmin), (xr, xmax))
        self.ycal = None
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
        self._set_status(f"{len(self.traces)} panel(s) found. Y is in pixels above each panel's zero tick: "
                         "normalize to the maximum, or calibrate the Y axis, as needed.")
        self._extract_all()

    # ---- computation ----
    def _num(self, var, default, lo=None):
        try:
            v = float(var.get().replace(',', '.'))
        except ValueError:
            return default
        return default if (lo is not None and v < lo) else v

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
        if self.xcal is None:
            return None
        step = self._num(self.v_step, 1.0, lo=1e-12)
        if step <= 0:
            return None
        smooth = self._num(self.v_smooth, 0.0, lo=0)
        poly = int(self._num(self.v_poly, 3, lo=1))
        # An edge point is kept up to 2 px outside the data (the pixel size in X units).
        tol = 2 * abs((self.xcal[1][1] - self.xcal[0][1]) / (self.xcal[1][0] - self.xcal[0][0]))
        curves = []
        for t in self.traces:
            if len(t['xpx']) < 2:
                continue
            x, y = trace_values(t['xpx'], t['ypx'], self.xcal, self.ycal, t['zero'], t['roi'][3])
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
        for a in self._overlay:
            try:
                a.remove()
            except Exception:
                pass
        self._overlay = []
        if self.rgb is None:
            return
        ax = self.ax_img
        H, W = self.rgb.shape[:2]
        sel = self._selected()
        for kind, cal in (('x', self.xcal), ('y', self.ycal)):
            if cal is None:
                continue
            for p, v in cal:
                self._overlay.append(ax.axvline(p, color='tab:blue', ls='--', lw=0.8) if kind == 'x'
                                     else ax.axhline(p, color='tab:green', ls='--', lw=0.8))
                self._overlay.append(ax.text(p if kind == 'x' else W * 0.005, H * 0.01 if kind == 'x' else p,
                                             f"{v:g}", color='tab:blue' if kind == 'x' else 'tab:green',
                                             fontsize=8, va='top' if kind == 'x' else 'bottom'))
        for p, v in self._cal_pending:
            line = ax.axvline(p, color='orange', ls=':') if self.mode == 'xcal' else ax.axhline(p, color='orange', ls=':')
            self._overlay.append(line)
        for i, t in enumerate(self.traces):
            col = PALETTE[i % len(PALETTE)]
            x0, y0, x1, y1 = t['roi']
            lw = 2.0 if t is sel else 0.8
            r = Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, ec=col, lw=lw)
            ax.add_patch(r)
            self._overlay.append(r)
            for ex in t['exclude']:
                e = Rectangle((ex[0], ex[1]), ex[2] - ex[0], ex[3] - ex[1], fill=True, fc='gray',
                              alpha=0.35, ec='gray', lw=0.5)
                ax.add_patch(e)
                self._overlay.append(e)
            if t['zero'] is not None and self.ycal is None:
                z, = ax.plot([x0, x1], [t['zero']] * 2, color=col, ls=':', lw=1)
                self._overlay.append(z)
            if len(t['xpx']):
                ln, = ax.plot(t['xpx'], t['ypx'], '.', color='red', ms=2)
                self._overlay.append(ln)
        self.canvas.draw_idle()
        self._redraw_result()

    def _redraw_result(self):
        ax = self.ax_res
        ax.clear()
        ax.tick_params(labelsize=7)
        ax.grid(True, ls=':', alpha=0.5)
        out = self.build_output()
        if out is None:
            msg = "Calibrate the X axis and add a trace to see the result here." if self.xcal is None or not self.traces \
                else "No curve points found: adjust the box, the colour or the threshold."
            ax.text(0.5, 0.5, msg, ha='center', va='center', transform=ax.transAxes, fontsize=8, color='gray')
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
        self.canvas.draw_idle()

    # ---- dialogs ----
    def _ask_form(self, title, fields, width=24):
        """Modal dialog with one text field per (label, initial) pair; returns the strings or None."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        out = []
        entries = []
        for label, initial in fields:
            tk.Label(dlg, text=label, anchor='w').pack(fill=tk.X, padx=12, pady=(10, 0))
            var = tk.StringVar(value=initial)
            e = tk.Entry(dlg, textvariable=var, width=width)
            e.pack(fill=tk.X, padx=12, pady=2)
            entries.append(var)

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
        dlg.winfo_children()[1].focus_set()
        dlg.grab_set()
        self.root.wait_window(dlg)
        return out if out else None

    # ---- export ----
    def export_csv(self):
        if not self._need_image():
            return
        if self.xcal is None:
            messagebox.showwarning("Export CSV", "Calibrate the X axis first.")
            return
        out = self.build_output()
        if out is None:
            messagebox.showwarning("Export CSV", "No curve points to export: add a trace first.")
            return
        grid, cols = out
        default = os.path.splitext(os.path.basename(self.image_path))[0] + ".csv"
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default, initialdir=os.path.dirname(self.image_path),
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        # Comma-separated with the X values in the first column: the layout LabSpectrumManager's
        # generic CSV reader expects; empty cells where a trace has no data.
        with open(path, 'w', newline='', encoding='utf-8') as f:
            wr = csv.writer(f)
            wr.writerow(['X'] + list(cols))
            for i, x in enumerate(grid):
                wr.writerow([f"{x:.6g}"] + ["" if np.isnan(c[i]) else f"{c[i]:.6g}" for c in cols.values()])
        self._set_status(f"Saved {os.path.basename(path)} ({len(grid)} points, {len(cols)} trace(s)).")


if __name__ == "__main__":
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    SpectrumDigitizer(root)
    root.mainloop()
