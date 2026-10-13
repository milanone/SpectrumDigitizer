# SpectrumDigitizer

Turns an **image of a spectrum plot** (PNG/JPG, e.g. a figure from a paper or an old printout) into
numbers: a CSV with one column per curve on a regular X grid, ready to be opened in
[LabSpectrumManager](https://github.com/milanone/LabSpectrumManager) (File → Open Files) or any other
tool.

![SpectrumDigitizer screenshot](screenshot.png)

(Screenshot: the synthetic test figure in `examples/` — a continuous curve above a dotted one —
after *Auto-detect panels*. The red points are the extracted curves; the plot below the image is the
resulting data, normalized to the maximum.)

## Workflow

1. **Open the image** (File → Open Image, drop it on the window, or pass it on the command line).
2. **Calibrate the axes.** *Calibrate X axis*: click a tick of known value, type the value, then do the
   same on a second tick far from the first. Zoom in with the toolbar first for precision. The Y axis
   is optional (see below). Reversed axes (e.g. FTIR wavenumbers) work.
3. **Add the traces.** *Add trace (draw box)* and drag a box around one curve, leaving out axis
   labels, legends and panel letters. Repeat for each curve. Then, per trace:
   - *Pick curve colour* — click on the curve to extract only pixels of that colour (several coloured
     curves in the same box, coloured lines over a grid...). *Dark pixels* goes back to black curves.
   - *Exclude area* — drag a box over something inside the trace's box that must be ignored.
   - *Set zero line* — click the zero level of the trace (only used without a Y calibration).
   - Rename (also by double-click) or Remove.
4. **Tune and export.** The parameters (dark threshold, colour tolerance, maximum single-dot size,
   output X step, Savitzky-Golay smoothing, normalization) act live on the result plot. *Export CSV*.

**Auto-detect panels** is a shortcut for framed, Origin-style figures with one or more stacked
panels: it finds the frame, creates one trace per panel, calibrates X from the left/right frame edges
(you type the two X values), finds each panel's zero tick and hides the panel letter.

### Y values

- With a **Y calibration** (two clicks on the Y axis), ordinates are calibrated values. It is global:
  use it for single-panel figures.
- **Without it**, ordinates are the height in pixels above the trace's zero line (the zero tick found
  by *Auto-detect panels*, the one set with *Set zero line*, or the bottom of the box). Use
  *Normalize → max* to get a 0-1 scale per curve.

### Output

Comma-separated CSV, UTF-8, first column `X`, then one column per trace (named after the trace), on a
grid of multiples of the chosen step; cells are empty where a trace has no data (more than two pixels
outside its curve). LabSpectrumManager reads it with its generic CSV reader; for X values above 2000 it
treats the spectra as FTIR.

## Limits

- One curve per box: for overlapping curves of the same colour, draw one box per curve where they
  are separate, or distinguish them by colour. Curves that cross are not separated.
- Each X position gets one point per curve (the centroid of its pixels): vertical segments of a curve
  are reduced to their midpoint, and the result is limited by the image resolution (about ±1 pixel).
- Only linear axes. Anti-aliased/JPEG images work in principle; very low-resolution or heavily
  compressed ones give noisy curves (raise the smoothing).
- Dotted curves: dots are recognised by size (*Max dot size*); dots touching each other are treated
  as a continuous line.
- **Verified only on the synthetic test figure in `examples/`**: on it, the extracted curves match the
  data they were drawn from to within 0.2 % of the panel height on average (peak positions to 1 nm
  over a 550 nm axis). It has **not** been tried yet on figures from real publications, scans or
  JPEGs: if one fails, send the image.

## Requirements and run

```
pip install -r requirements.txt
pythonw SpectrumDigitizer.pyw [image]
```

Python 3 with Tkinter, numpy, scipy, Pillow, matplotlib; `tkinterdnd2` is optional (drag and drop).

## Tests

```
python -m unittest discover -s tests -v
```

`tests/test_extraction.py` checks the extraction functions (they do not need a window) on small
drawn images and on `examples/example_two_panels.png`, which `examples/make_example.py` generates
together with its ground-truth data (`example_two_panels_truth.csv`). No real data is used.

## License

MIT — see `LICENSE`.
