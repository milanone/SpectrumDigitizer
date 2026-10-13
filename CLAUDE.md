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
  shows them); calibrations are two points (pixel, value) per axis, linear, so reversed axes work.
- **Traces** (`self.traces`, dicts): `roi` box, optional `color` (None = dark pixels), `zero` (pixel row),
  `exclude` boxes, and the cached extracted points `xpx`/`ypx`. `_extract_all()` re-extracts (after a
  change of threshold, colour or box); `_redraw()` only rebuilds the output (step, smoothing,
  normalization). Parameter changes are debounced (`_schedule`).
- **Y without calibration** is pixels above the trace's zero line; with a Y calibration it is
  calibrated and global to all traces.
- **Output grid** = integer multiples of the step spanning all traces (`make_grid`: no accumulated
  floating-point drift); a trace gets NaN (empty cell) more than 2 pixels (in X units) outside its data.
- **Mode handling:** `self.mode` selects what a canvas click or box means. `_toolbar_off()` switches off
  matplotlib's pan/zoom before entering a mode, because an active toolbar tool silently swallows canvas
  clicks (same pitfall as in LabSpectrumManager's deconvolution). The `RectangleSelector` is only active
  in the `roi`/`excl` modes.
- **CSV format** is the one LabSpectrumManager's generic reader expects (`pd.read_csv(path,
  index_col=0)`: comma, X first). Do not switch to `;` without changing that reader.
- **PlotStyleKit is not used:** the figure is a working view of an image, not a publication plot.

## Verified / not verified

Verified on the synthetic figure (`examples/example_two_panels.png`, made by `examples/make_example.py`):
auto-detect panels, extraction of a continuous and a dotted curve against the ground truth, manual X
calibration with simulated clicks, CSV export, and reading that CSV back in LabSpectrumManager
(File → Open: two spectra, UV-Vis, NaN rows dropped). NOT verified: real publication figures, JPEG/scans,
coloured curves on real images, Y calibration on real images, box drawing with a real mouse.

## Editing conventions

Surgical changes; no refactoring or renames outside an explicit request. Comments explain the *why*.
After a change run the tests and try the feature in the real window (the clicks, not just the function).
