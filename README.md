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

The panel on the right always says what to do next, and a magnifier follows the mouse so that points
can be placed precisely (it also shows the pixel and, once calibrated, the X/Y value under the cursor).

- **Move around the image:** mouse wheel = zoom, drag = move; *Fit to window* resets the view. These
  never interfere with the tools below. `Esc` cancels the tool that is waiting for a click.
- **Open the image** (File → Open Image, drop it on the window, or pass it on the command line). File → Close Image clears the image, calibration and curves. File → Save Cleaned Image writes the image as it is after the clean-up (PNG), to open again later.
- **1. Axes.** Press *Pick* next to *X point 1*, click a tick of known value, then type its value in the
  box; do the same for *X point 2* on a tick far from the first. The Y axis is optional (see below).
  Reversed axes (e.g. FTIR wavenumbers) work. A point can be re-picked at any time.
- **Fine-tuning with the keyboard:** after picking a point (or setting a zero line) the arrow keys move
  it by 1 pixel (`Shift` = 0.2 pixel): ← → for X points, ↑ ↓ for Y points and zero lines. Clicking in a
  value box selects that point for the arrows; the selected point is drawn in orange. The magnifier
  follows the point being moved.
- **2. Curves.** *Add curve* and drag a box around one curve, leaving out axis labels, legends and
  panel letters; repeat for each curve. For the selected curve:
  - *Pick its colour* — click on the curve to extract only pixels of that colour (several coloured
    curves in the same box, coloured lines over a grid...). *Black curve* goes back to dark pixels.
    *Any colour* takes every non-white pixel in the box as the curve (for a trace of mixed shades: black, violet,
    pink... after cleaning the image); the *Colour tolerance* of the advanced settings is then how far from white
    a pixel must be (lower it, e.g. to 15, to follow a very pale tail).
  - *Set working area* — drag a box (shown dashed blue); the colour tools below act only inside it.
    *Whole image* removes it. The area has handles, like a shape in a drawing program: drag inside it to
    move it, drag a square handle on a corner or an edge to resize it (the opposite side stays put), drag the
    round handle above the top edge to rotate it (Shift = steps of 15°); the *Rotate the working area* box
    (degrees, counter-clockwise) gives the exact angle, for tilted scans. Move the image itself with the
    right or middle mouse button while the area is shown.
  - *Erase colours* / *Keep only colours* — click a pixel of the trace, or drag a small box over it to take
    all the colours inside the box (best for curves drawn with slightly different shades); repeat if
    needed (selections are marked in magenta), then press *Apply*: every pixel within the colour tolerance
    of any selected colour is painted white, or every pixel that is not of those colours. *Undo* restores
    the previous state. Where another curve of the same colour crosses the erased one it gets a gap.
  - *Eraser (paint white)* — a brush for the final touches: drag over what must go; the circle shows the
    size (*size (px)* box, diameter in image pixels). Each stroke is one *Undo* step. Esc ends.
  - *Flatten colours* — turns every non-white pixel (further from white than the colour tolerance) of the working
    area, or of the whole image, into black: a trace of mixed shades becomes one colour that the default
    detection follows, and the saved cleaned image is uniform. *Undo* restores.
  - *Erase an area* — drag a box over a legend or a panel letter: it is painted white for all curves
    (*Undo* restores it). Do this first, so the legend is never mistaken for part of a spectrum.
  - *Ignore an area* — drag a box over something inside the curve's box that must be skipped (a
    panel letter, a legend: they show up as spikes in the result plot).
  - *Set zero line* — click the zero level of the curve (only used without a Y calibration).
  - Rename (also by double-click) or Remove.
- **3. Output.** X step, smoothing and normalization act live on the result plot under the image (drag the boundary between them to give more room to the image);
  *Show the extracted points* toggles the red dots on the image, and *Advanced extraction settings*
  has the dark threshold, colour tolerance and single-dot size. *Export CSV*.

**Auto-detect panels** is a shortcut for framed, Origin-style figures with one or more stacked
panels: it finds the frame, creates one curve per panel, calibrates X from the left/right frame edges
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
- The box does not have to be tight: frame lines, ticks outside the frame and a panel letter inside the
  box are ignored automatically (a row or column that is dark over 70 % of the box is taken for a
  ruling line, and where several blobs give a point at the same X the one that continues the curve is
  kept). The flip side: a **perfectly flat curve** covering more than 70 % of the box width is read as a
  ruling line too; draw the box around the part that is not flat.
- Each X position gets one point per curve (the centroid of its pixels): vertical segments of a curve
  are reduced to their midpoint, and the result is limited by the image resolution (about ±1 pixel).
- Only linear axes. Anti-aliased/JPEG images work in principle; very low-resolution or heavily
  compressed ones give noisy curves (raise the smoothing).
- Dotted curves: dots are recognised by size (*Max dot size*); dots touching each other are treated
  as a continuous line.
- **Verified only on the synthetic test figure in `examples/`** (the whole workflow, with real mouse events
sent to the window: zoom, pan, axis points, boxes, ignoring an area, Y calibration, export): on it, the extracted curves match the
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
