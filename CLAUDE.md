# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What it is

`SpectrumDigitizer.pyw`: a Tkinter app that digitizes images of spectrum plots into a CSV on a regular
X grid. It grew out of `digitalizza_spettri.py`, a command-line script by the same author (it only
handled Origin-style framed figures; its frame / zero-tick / dot-vs-curve logic became
`find_frame()`, `find_zero()` and `extract_points()` here). Everything is in English.

Run: `pythonw SpectrumDigitizer.pyw [image]`. Tests: `python -m unittest discover -s tests -v`.

## Structure (single file, as in the other lab apps)

- **Extraction core, no Tk** (top of the file, imported by the tests via `SourceFileLoader`):
  `find_frame`, `find_zero`, `curve_mask`, `extract_points`, `lin_map`, `trace_values`, `make_grid`,
  `resample`, `smooth_values`. Keep it free of Tk so it stays testable.
- **`SpectrumDigitizer` class**: left pane = matplotlib figure with two axes (the image in pixel
  coordinates, and a live preview of the output); right pane = steps and parameters.

## Key decisions

- **Coordinates:** everything is in image pixel coordinates (pixel centres at integers, as `imshow`
  shows them). Calibration = two points per axis (`cal_px`: clicked pixel, `cal_var`: typed value);
  `xcal`/`ycal` are properties built from them (None until usable), so editing a value in the panel
  updates everything. Linear, so reversed axes work.
- **Traces** (`self.traces`, dicts): `roi` box, optional `color` (None = dark pixels), `zero` (pixel row),
  `exclude` boxes, and the cached extracted points `xpx`/`ypx`. `_extract_all()` re-extracts (after a
  change of threshold, colour or box); `_redraw()` only rebuilds the output (step, smoothing,
  normalization). Parameter changes are debounced (`_schedule`).
- **Y without calibration** is pixels above the trace's zero line; with a Y calibration it is
  calibrated and global to all traces.
- **Frame lines in the box:** `extract_points()` removes rows/columns that are dark over >= 70 % of the
  box (ruling lines, also when the curve touches them: otherwise frame and curve are one blob and every
  column yields several points, which showed up as 'noise' in the preview), drops everything outside a
  frame line within 15 % of the box border (ticks), and `one_point_per_column()` resolves any remaining
  column with several points by continuity. Tests: `FrameInsideBoxTests`.
- **Output grid** = integer multiples of the step spanning all traces (`make_grid`: no accumulated
  floating-point drift); a trace gets NaN (empty cell) more than 2 pixels (in X units) outside its data.
- **Navigation and tools:** no matplotlib toolbar. Its pan/zoom tools swallow canvas clicks (the cause of
  "I cannot calibrate": a user zooming with the toolbar after pressing Calibrate got no points), so the
  view is moved directly: wheel = zoom at the cursor, left drag (when no tool is waiting) or middle/right
  drag = pan. `self.mode` ('pick', 'color', 'zero', 'roi', 'excl') says what a click/box means; the
  `RectangleSelector` is active only in 'roi'/'excl'; Esc cancels. The magnifier is a Tk label updated
  from `_on_motion`.
- **Arrow keys:** `self._nudge` = ('cal', key) or ('zero', trace) is the point the arrows move (set after a
  pick, when a calibration box gets focus, after 'Set zero line'; cleared by Esc or a new tool). The arrow
  bindings are on the root window and ignore Listbox/Combobox/Text focus, so the value boxes (entries)
  lose arrow-key cursor movement on purpose: after a pick the focus is in the value box.
- **Layout:** image and result preview are two canvases in a vertical `PanedWindow` (draggable sash).
- **Status box:** `_update_hint()` derives the next step from the state; `_set_status(..., sticky=True)`
  keeps a one-off message until the user edits something (`_sticky`).
- **Image display:** default matplotlib interpolation, `adjustable='box'` (with 'datalim' matplotlib
  warns about fixed limits on every zoom); never draw over the image more than thin overlays (box,
  calibration lines, small red points, toggled by a checkbox).
- **CSV format** is the one LabSpectrumManager's generic reader expects (`pd.read_csv(path,
  index_col=0)`: comma, X first). Do not switch to `;` without changing that reader.
- **PlotStyleKit is not used:** the figure is a working view of an image, not a publication plot.

## Verified / not verified

Verified on the synthetic figure (`examples/example_two_panels.png`, made by `examples/make_example.py`):
auto-detect panels, extraction of a continuous and a dotted curve against the ground truth, and the manual
workflow driven with real Tk mouse events (`event_generate` on the canvas: wheel zoom, pan, picking axis
points, drawing a box, ignoring an area, Y calibration, Esc), CSV export, and reading that CSV back in LabSpectrumManager
(File → Open: two spectra, UV-Vis, NaN rows dropped). NOT verified: real publication figures, JPEG/scans,
coloured curves on real images, Y calibration on real images, a physical mouse on screen (the events were injected, not clicked by a person).

## Editing conventions

Surgical changes; no refactoring or renames outside an explicit request. Comments explain the *why*.
After a change run the tests and try the feature in the real window (the clicks, not just the function).
