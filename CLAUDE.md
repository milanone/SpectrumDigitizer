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
  updates everything. Linear (or logarithmic, see below), so reversed axes work.
- **Traces** (`self.traces`, dicts): `roi` box, optional `color` (None = dark pixels, `'any'` = every non-white pixel, distance from white > the colour tolerance), `zero` (pixel row),
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
- **Colour filter** ('Erase colours' / 'Keep only colours', modes `erase`/`keep`): each click, or each small box dragged
  with the `RectangleSelector` (a click is told from a drag on release by the display distance, <4 px;
  `_pixel_colors()` merges the box's shades in steps of 16), appends an entry to `_filter_pts`; 'Apply' (`apply_filter()`) paints white the
  pixels within the colour tolerance of any of them (or all the others), inside `work_box` (the working area,
  set first with 'Set working area', mode `work`; None = whole image), rotated about its centre by `v_angle` degrees
  counter-clockwise (`_work_mask()`, `_work_corners()`; the angle resets when a new box is drawn). It replaces `self.rgb` (the old array
  goes on `rgb_undo`; 'Undo' pops it) and re-extracts. Where another curve crosses the erased one it gets a gap.
  `_cancel_tool` drops the pending clicks. 'Erase an area' (mode `wipe`) whitens a box with the same undo
  stack: for legends, which 'Ignore an area' handles only per curve and after the box exists. Verified on
  synthetic images only.
- **Working-area handles** (no tool active, `mode is None`): `_work_hit()` tests, in display pixels, the round
  rotation handle, the 8 square handles and then the inside (move); `_work_drag_to()` works in the local frame
  of the unrotated box (`_to_local`/`_from_local`, the same convention as `_work_mask`) so resizing a rotated box
  keeps the opposite edge fixed. A drag only redraws the overlays (`_refresh_overlay`); `_angle_busy` stops the
  `v_angle` trace from doing a full `_redraw`. Because a press on a handle or inside the area starts a drag,
  panning with the left button does not work over the area: right/middle drag pans.
- **Straighten** (mode `straighten`, `rotate_image()`): rotates `self.rgb` with PIL (`expand=True`, white fill, bicubic)
  by the angle from two clicked points (`_straightening_angle`: the line is made horizontal, or vertical if it is
  closer to vertical) or by minus the working area's angle (`straighten_to_area`). The size changes, so
  `_new_geometry()` clears calibration, curves and working area and refits the view (`im.set_extent`); an `undo_filter`
  across a size change calls it too. Chosen instead of a calibration with tilted axes (as PlotDigitizer does): the
  rest of the program keeps assuming straight axes. Test: `StraightenAngleTests`; checked on a synthetic plot tilted by 7°.
- **Flatten colours** (`flatten_colors()`): non-white pixels (distance from white > colour tolerance, inside the working
  area) become black, undoable. Alternative to the trace mode `'any'`; flattening also changes the saved image.
- **Dashed lines** (core `erase_mask_along()`, `join_dashes()`; methods `erase_trace()`, `join_dashed_lines()`): a dashed curve
  next to a continuous one was lost (the continuous one wins `one_point_per_column`; the dashes only exist as separate blobs).
  Workflow: digitize the continuous curve, `erase_trace()` whitens the non-white pixels (distance from white > 10, so the
  pale anti-aliased edge goes too) within `v_erase` px of its polyline (consecutive points over 25 px apart are not
  joined) except ruling lines (`guided_rules` over the whole image; unprotected when > 30 % of the curve's points lie on
  that line: a flat baseline looks like an axis), marks the trace `fixed` (so `_extract_all` keeps its points: a
  re-extraction from the erased image would find nothing) and pushes `rgb_undo`, as one undo action (`_begin_group`). `join_dashes()` labels the ink (8-connected), takes the two ends of each group along its main axis
  (SVD) with the local direction at each tip (pixels within 8 px: a dash bent over a peak keeps its bend), and joins pairs of
  ends of different groups at most `v_gap` px apart whose connecting line leaves both ends within `JOIN_MAX_ANGLE` (50) degrees
  of their own direction, cheapest first (cost = gap x (3 - cos a - cos b): a dash straight ahead beats a nearer one on the
  other flank of a peak, which the first version, nearest-first with 70 degrees, joined with a flat bar across the apex), each
  end once and two groups once; the join is a cubic Hermite curve (`HERMITE_TENSION`) leaving along the end directions, not a
  straight chord (chords cut the peaks); it has the darkest shade and the thickness of the dash. `v_gap` is set to 3 % of the image width when an image is loaded (not part of the
  session). Ends were first taken as the extreme columns: that made horizontal stubs on steep dashes (the peak at 800 nm of
  the test figure), hence the principal axis. Limits: dashes that touch the other curve are one group (erase first), a gap
  wider than the limit stays, text and ticks inside the working area get joined. Tests: `tests/test_dashes.py` (synthetic
  plot with a continuous and a dashed curve, core and app). Tried on a real figure (not in the repo): a black continuous
  and a black dashed absorption spectrum; after erasing and joining 62 of ~78 gaps were closed, the rest are the
  crossings, where the erased stretch is longer than the limit.
- **Manual link** (mode `link`, `_start_link()`, core `draw_link()`): the fix for what `join_dashes()` cannot decide. Press on one end,
  drag (rubber band `_link_line`, a `Line2D` removed on release/cancel), release: `_finish_link()` calls `draw_link()`, which snaps
  each end to the nearest ink pixel within 6 px, takes the colour (darkest third) and thickness (area / span) of the ink component
  in a 41 px window around the first end that has one, and draws a straight PIL line; no ink near either end: nothing drawn and a
  status message. One `_push_undo()` per link, the tool stays on until Esc. Tests: `LinkTests` and two `AppTests` (injected
  MouseEvents) in `tests/test_dashes.py`; also checked with real Tk events (press, two motions, release, Ctrl+Z) on the Fiedor figure.
- **Eraser** (mode `brush`): press copies `self.rgb` onto `rgb_undo` (one undo step per stroke), motion stamps
  white discs along the path (`_brush_stamp`), release re-extracts. Not clipped to the working area. The size
  circle is a separate artist (`_brush_circle`), not part of `_overlay`.
- **Inner ticks:** after the ruling lines are blanked, `strip_inner_ticks()` erases the stubs pointing from a
  ruling line into the box (a bundle of at most ~1 % of the image width of adjacent columns/rows with a run of
  2..tick_max pixels starting at the rule and clear flanks). A curve following the rule gives a wide group and is
  left alone. Tests: `InnerTickTests` (thick ticks, curve hugging the bottom frame). Checked on a real publication figure (not in the repo; ticks gone).
- **Save Cleaned Image** writes `self.rgb` as PNG: lossless, 8-bit RGB (the program converts every image to RGB on
  load, so palette/alpha/ICC of the source are not kept), the size of the image on screen, `compress_level=9`
  (Pillow's default is 6; level 9 only makes the file ~2 % smaller) and the source `dpi` (`self.image_dpi`, read at load).
  The session (calibration, boxes) is not saved.
- **Crop** (`crop_to_area()`): bounding box of the working area (rotated: outside the rotated rectangle painted white),
  same `_new_geometry()` / `_confirm_geometry_change()` path as Straighten: calibration, curves and working area are cleared.
- **Three tabs** (`self.nb`, a `ttk.Notebook` in the right pane; status box, magnifier and Export stay outside it):
  'Clean image' (working area, colour tools, eraser, flatten, save) and 'Digitize' (axes, curves, output,
  advanced extraction settings) and 'Output' (step, smoothing, normalization, show points, result info; the Export button
  stays outside the tabs). `_on_tab_changed()` ends the tool in progress. The working area and its handles exist only in the
  clean tab (`_clean_tab_active()` gates `_work_hit()` and the overlay), so in the Digitize tab the left drag pans as
  usual. `v_tol` is shared: an entry in each tab, one variable.
- **Left pane views** (`self.nb_view`): 'Image' (the image canvas with the small preview `ax_res` under it, as before) and
  'Result plot' (`ax_big`, its own canvas with the matplotlib toolbar: no calibration tool needs the clicks of that
  canvas, unlike the image canvas, where the toolbar stays out). `_draw_result` computes `build_output()` once,
  draws the preview always and the large plot only while its view is shown (`_view_is_result()`); `_on_tab_changed`
  shows the Result plot when the Output tab opens. Leaving the image view cancels a tool waiting for a click.
- **Semi-automatic brush** (mode `guide`, listed before the box in the panel because it is the primary way to make a
  curve): a trace's `guide` is a list of strokes `(path, width)`; a path is an editable polyline (2 px between points,
  Gaussian-smoothed, ends kept: `resample_path`, `smooth_path`, `reshape_path`, all core/no Tk). The tool stays active
  until Esc. On press `_guide_hit()` decides, in screen pixels, between continuing a path (press within 10 px of an
  end: `_stroke_src` holds which one, the new part is re-smoothed together with the last 3 old points), pulling a
  stretch (within 14 px of the path: `_gdrag`; `reshape_path` moves the grabbed vertex fully and the others with a
  cosine falloff over `v_reach` px, always from the vertices stored at press) or drawing a new stroke. Shift turns an
  end into a pull point. The first stroke of 'new curve' creates the trace (colour from `_guess_color`: the ink colour under the path (None for dark ink, else the median of the inkiest 30 %) is tried with the real extraction, and if it covers fewer than 80 % of the columns the brush spans the trace gets 'any': a trace that changes colour or is pale in places stops early and, because `resample` interpolates between sparse points, comes out as a polyline). `_update_guide_roi` makes the roi the corridors' bounding box.
  Every edit pushes a snapshot on `_guide_hist` ('Undo brush'); the width spin rewrites the widths of the curve being
  edited. `corridor_mask()` (PIL discs) is ANDed with the curve mask in `extract_points(guide=...)` just before
  labelling and the ruling lines are found over the whole image instead of the box (`guided_rules()`: curve-mask rows/columns across >= 50 % of the image; if that leaves fewer than 30 % of the columns the brush spans, `extract_points` repeats the extraction keeping them: the curve itself was the long line, e.g. a flat baseline traced on purpose), then blanked and the inner ticks stripped as in the box mode;
  crossings inside the corridor are still resolved by `one_point_per_column`. The extraction runs on release, the
  overlay (RGBA corridors, centre line, end circles) follows the drag. Like PlotDigitizer's semi-auto line, but the
  corridor limits our own extraction instead of an external vectorizer. Tests: `BrushGuideTests`, `EditablePathTests`.
- **Session** (`.sdsession`, a zip: `session.json` + `image.png`, `SESSION_VERSION` = 1): `_session_dict()` / `open_session()`. The
  image is the current `self.rgb` (not a path: crops, rotations and cleaning are in it); calibration, traces (roi, colour,
  zero, exclude, brush paths at full float precision so the re-extraction is identical), work box and angle, view, and the
  `_session_vars()` settings are in the JSON. The extracted points are not stored: `_extract_all()` recomputes them. Undo/redo
  history is not saved. `_set_image()` is the shared part of `load_image()` and `open_session()`. A reopened session gave the
  same points and CSV output on the test image; a newer `version` is refused.
- **Undo / Redo** (two buttons above the tabs, Ctrl+Z / Ctrl+Y in any tab, text fields keep their own undo): the image edits
  (`rgb_undo` / `rgb_redo`, `_push_undo()`) and the curve edits (`_guide_hist` / `_guide_redo`, `_guide_push()`: brush, points,
  erase) keep their own stacks and `_ops` / `_ops_redo` record the order, one tuple of kinds per user action. `_log_op()` is
  called by both push functions (not while `_replaying`, i.e. during a redo, which re-pushes by itself); `_begin_group()` /
  `_end_group()` make several pushes one action (`erase_trace()`: pixels + `fixed` flag); `_guide_drop()` takes back a snapshot
  of a click that changed nothing. `undo_any()` pops actions until one actually shrinks its stack (an entry whose curve was
  removed is skipped by `undo_stroke`). Reset with the image (`_set_image`, `close_image`, `open_session`). Tests: `tests/test_undo.py`.
- **Despeckle** (`despeckle()`): `ndi.label` (8-connectivity) of the non-white mask, groups under `v_speck` pixels painted white
  inside the working area, undoable. **Check grid** (`_draw_check_grid`, core `nice_ticks`): round values of the calibration as
  lines across the image. **Copy table** (`copy_table()`): tab-separated text; decimal comma option because Excel in an Italian
  locale reads '0.5' as text or a date (`_system_decimal()` sets the default; the CSV export always uses the point).
- **Logarithmic axes**: `xcal`/`ycal` are `AxisCal` objects (a `tuple` subclass, so `(p1, v1), (p2, v2) = cal` and `lin_map`
  still work, with a `log` flag set from the `v_xlog` / `v_ylog` checkboxes; values <= 0 make the calibration unusable).
  `lin_map` maps on a log scale when `cal.log`; `to_pixel` is its inverse (check grid: `log_ticks`). `build_output` does the
  edge tolerance, resampling and smoothing in the linear space of each axis (log10 of the values on a log axis), then converts back;
  the X grid is `make_grid` on log10(X) with step 1/`v_ppd` (points per decade), shown instead of the X step by
  `_update_step_rows()`. The result plots set log scales. The Y zero line is only used without a Y calibration, so it does
  not meet the log axis. Verified on synthetic plots (a decay over 3 decades on a log Y axis: max relative error 1.2 %, the
  size of one pixel; a sine over 3 decades on a log X axis: error 0.3 % of the range); not on a real log plot.
- **Hand-edited points** (mode `points`, `_start_points()`): the selected trace is marked `fixed` and `_extract_all()` skips fixed
  traces, so its `xpx`/`ypx` are the edited points until `reextract()`. Press on a point (<= 9 screen px): drag = move one point,
  Shift = `reshape_path` pull of the neighbours (arc-length falloff `v_reach`, from the points stored at press), Ctrl = delete,
  a click selects (Delete key deletes it); press on empty space: a click inserts a point, a drag is a delete box. Points stay
  sorted by X (`_pts_sorted`). 'Curve by points' creates a fixed trace with a whole-image roi (only used for the default zero;
  the roi rectangle is not drawn for fixed traces without a guide). The edits use the brush's snapshot stacks: `_guide_push`,
  `_guide_state` and the undo/redo also keep `(xpx, ypx, fixed)`, so Ctrl+Z in the Digitize tab undoes both. Modifier keys are read
  from the Tk event state (`_mods`): matplotlib's `event.key` is only set if the canvas had the keyboard focus. The session stores
  `fixed` + `points`. The Digitize tab is a scrollable frame (`_scrollable`: canvas + scrollbar shown only when needed).
- **Analysis** (Output tab, `compute_analysis()` switches `self._analysis` on): `curve_metrics()` (core, no Tk) on the output grid values:
  trapezoid area, peak, length, limits interpolated, optional straight baseline between the limits. `_draw_result` re-runs
  `_run_analysis` on every redraw (live) and `_plot_result` shades the area with `fill_between`. Pixel length is the polyline of
  the extracted points between the limits. PlotDigitizer's 'polygon area' is covered by the baseline option (peak area above the
  line between two limits); closed polygons of clicks are not implemented. Verified on a Gaussian peak on a slope: area within
  0.6 % of the analytic value, with and without baseline.
- **Number arrows** in the Output tab: `_make_spin()` is a `tk.Spinbox` with Tk's own increment set to 0 and the
  command registered with `%d` ('up'/'down'); `_spin_move()` computes the new value (X step: 1-2-5 sequence; window:
  one X step; polynomial order: 1..9). Works with the mouse, press-and-hold and the Up/Down keys. Also used for the advanced
  extraction numbers, the colour tolerance and the brush width (kinds 'thresh', 'tol', 'dot', 'bwidth').
- **Pan** converts the mouse positions with the transform frozen at press time (`_pan` holds it): display Y grows
  upward while image rows grow downward, and the earlier pixel-to-span scaling moved the image the wrong way vertically.
  'Center and fit to window' (`_fit_view`, also `Home`) restores the full view; the axes box is centred (aspect 'box').
- **Layout:** image and result preview are two canvases in a vertical `PanedWindow` (draggable sash).
- **Status box:** `_update_hint()` derives the next step from the state; `_set_status(..., sticky=True)`
  keeps a one-off message until the user edits something (`_sticky`).
- **Image display:** default matplotlib interpolation, `adjustable='box'` (with 'datalim' matplotlib
  warns about fixed limits on every zoom); never draw over the image more than thin overlays (box,
  calibration lines, small red points, toggled by a checkbox).
- **CSV format** is the one LabSpectrumManager's generic reader expects (`pd.read_csv(path,
  index_col=0)`: comma, X first). Do not switch to `;` without changing that reader.
- **PlotStyleKit** (the first host of its integration module `plotstylekit.py`, API 1; see that repo's README): the file carries the
  common bootstrap text between the `PlotStyleKit bootstrap` comment lines (generated from PlotStyleKit's README: change it there,
  not here) and `PSK_MIN_API`. `psk` is the module or None, `PSK_PROBLEM` says why. At start, if `psk is None` and the user has not
  declined (`_psk_config()['declined']`), `_offer_plotstylekit()` asks for the folder after 400 ms; File > PlotStyleKit...
  (`plotstylekit_dialog()`) changes it later; `_adopt_plotstylekit()` starts it without restarting. With it, the big result plot
  gets `psk.style()` + `psk.screen_fonts()` in `_plot_result` (the preview and the on-screen curve colours stay `PALETTE`, to match
  the boxes on the image), and `_result_figure()` builds a separate title-less `Figure` for `save_figure_image()` /
  `save_figure_pickle()` / `edit_figure()`, which go through `psk.*_dialog()` (publication copy at 9 x 6.75 cm). **Fallbacks without
  it**: plain look, plain `savefig` / pickle of that figure, and 'Edit Figure' offers to locate PlotStyleKit. Verified by driving all
  of this with patched dialogs (found, not found + chosen, cancelled + remembered, wrong folder, other copy).

## Icon

`SpectrumDigitizer.ico` (16, 24, 32, 48 and 64 px, each shrunk by hand from a 1024 px drawing, written as classic 32-bit BMP frames by `write_ico()`: Pillow's own ICO writer makes PNG-compressed frames, which XYplorer's toolbar button does not read) is made by `python examples/make_icon.py` (white tile with black axes and an orange curve; `blue` and `light` are other styles); the program loads it
with `iconbitmap(default=...)` inside a try, so a missing file or another platform just keeps the default icon. To use it for a
shortcut, point the shortcut's icon at this file.

## Screenshots

`screenshot.png`, `screenshot_clean.png` and `screenshot_result.png` come from the real window on the synthetic
`examples/example_two_panels.png` (no real data) through `python examples/make_screenshots.py` (Windows only: DPI-aware
screen grab of the window frame, the window forced on top for a second). Rerun it after a change of the interface and look at
the three images before publishing: the title bar shows only the example file name.

## Verified / not verified

Verified with real Tk events injected on the canvas (`event_generate`: clicks, drags, wheel, keys) and with
synthetic images whose truth is known (`examples/example_two_panels.png`, plus plots drawn in the tests and in
throw-away scripts): auto-detect panels, extraction of continuous and dotted curves against the ground truth, calibration
(linear; logarithmic X, Y and both, errors of about one pixel), the brush (drawing, resuming, pulling a stretch, crossing
curves, a curve falling onto the axis), hand-edited points, the clean-up tools (colours, working area and handles, eraser,
despeckle, straighten a 7° tilt, crop, flatten, undo/redo), sessions (save, reopen: identical image, points and CSV),
the area/peak analysis (a Gaussian peak: 0.6 % from the analytic area), CSV export, and reading that CSV back in
LabSpectrumManager (File → Open: two spectra, UV-Vis, NaN rows dropped).

On one real publication figure (not in the repo; a four-curve absorption spectrum as JPG, then a cleaned PNG of it): the inner
ticks are ignored, the brush follows a black curve that turns violet and pale pink and falls onto the x axis (without
taking the axis for the curve; the last ~20 nm, where the curve is inside the axis line, are lost), and 'Flatten colours' /
'Any colour' give the same points.

NOT verified: a physical mouse on screen (the events were injected, not clicked by a person); other real figures and scans
(JPEG artefacts, low resolution, grids, coloured curves, dotted curves on real images); Y calibration and logarithmic axes on
real plots; hand-edited points and the analysis on real data; any comparison of the digitized values with another
digitizer's (PlotDigitizer) on the same figure; the layout on a small or high-DPI screen (the Digitize and Output tabs scroll when
they do not fit); the screenshot script on anything but Windows 11.

## Editing conventions

Surgical changes; no refactoring or renames outside an explicit request. Comments explain the *why*.
After a change run the tests and try the feature in the real window (the clicks, not just the function).
