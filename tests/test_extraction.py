"""Tests for the extraction core of SpectrumDigitizer (no Tk window needed).

They use the synthetic figure examples/example_two_panels.png (made by examples/make_example.py,
with its ground-truth data in examples/example_two_panels_truth.csv) and small images drawn here.

    python -m unittest discover -s tests -v
"""
import importlib.machinery
import importlib.util
import os
import unittest

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
loader = importlib.machinery.SourceFileLoader("sd", os.path.join(ROOT, "SpectrumDigitizer.pyw"))
spec = importlib.util.spec_from_loader("sd", loader)
sd = importlib.util.module_from_spec(spec)
loader.exec_module(sd)

EXAMPLE = os.path.join(ROOT, "examples", "example_two_panels.png")
TRUTH = os.path.join(ROOT, "examples", "example_two_panels_truth.csv")


class HelperTests(unittest.TestCase):
    def test_groups(self):
        parts = sd.groups(np.array([1, 2, 3, 7, 8, 12]))
        self.assertEqual([list(p) for p in parts], [[1, 2, 3], [7, 8], [12]])
        self.assertEqual(sd.groups(np.array([], int)), [])

    def test_lin_map_reversed_axis(self):
        # FTIR-style axis: the larger value is on the left
        cal = ((100.0, 4000.0), (900.0, 400.0))
        self.assertAlmostEqual(float(sd.lin_map(cal, 500.0)), 2200.0)

    def test_make_grid_multiples_of_step(self):
        g = sd.make_grid(350.3, 354.9, 1.0)
        np.testing.assert_allclose(g, [351, 352, 353, 354])
        g = sd.make_grid(0.0, 1.0, 0.1)
        self.assertEqual(len(g), 11)

    def test_resample_marks_outside_as_nan(self):
        v = sd.resample(np.array([1.0, 2.0, 3.0]), np.array([10.0, 20.0, 30.0]), np.array([0.0, 2.5, 6.0]), 0.5)
        self.assertTrue(np.isnan(v[0]) and np.isnan(v[2]))
        self.assertAlmostEqual(v[1], 25.0)

    def test_smooth_keeps_short_series(self):
        v = np.array([1.0, 2.0, 3.0])
        np.testing.assert_array_equal(sd.smooth_values(v, 11, 3), v)

    def test_smooth_reduces_noise(self):
        rng = np.random.default_rng(0)
        clean = np.sin(np.linspace(0, 6, 400))
        noisy = clean + rng.normal(0, 0.1, 400)
        out = sd.smooth_values(noisy, 21, 3)
        self.assertLess(np.std(out - clean), np.std(noisy - clean) / 2)


class ExtractionTests(unittest.TestCase):
    def test_coloured_curve_ignores_other_colours(self):
        img = np.full((100, 200, 3), 255.0)
        img[50, 10:120] = (255, 160, 0)       # orange horizontal line
        img[20, 10:120] = (0, 0, 0)         # black line that must be ignored
        xs, ys = sd.extract_points(img, (0, 0, 200, 100), color=(255, 160, 0), tol=60)
        self.assertEqual(len(xs), 110)
        np.testing.assert_allclose(ys, 50.0)
        xs, ys = sd.extract_points(img, (0, 0, 200, 100))   # dark pixels: only the black line
        np.testing.assert_allclose(ys, 20.0)

    def test_exclude_box_removes_pixels(self):
        img = np.full((60, 120, 3), 255.0)
        img[30, 10:70] = 0
        xs, _ = sd.extract_points(img, (0, 0, 120, 60), exclude=[(0, 0, 50, 60)])
        self.assertGreaterEqual(xs.min(), 50)

    def test_empty_roi_gives_no_points(self):
        img = np.full((60, 120, 3), 255.0)
        xs, ys = sd.extract_points(img, (0, 0, 120, 60))
        self.assertEqual((len(xs), len(ys)), (0, 0))

    def test_single_dots_give_one_point_each(self):
        img = np.full((80, 400, 3), 255.0)
        for cx in (50, 100, 150):
            img[38:43, cx - 2:cx + 3] = 0
        xs, ys = sd.extract_points(img, (0, 0, 400, 80), dot_max_frac=0.03)
        np.testing.assert_allclose(xs, [50, 100, 150])
        np.testing.assert_allclose(ys, [40, 40, 40])


class FrameInsideBoxTests(unittest.TestCase):
    """A loosely drawn box includes the frame, ticks outside it and a panel letter: the result must
    still be the curve alone, with exactly one point per column (several points per column made the
    resampled curve jump up and down like noise)."""

    @classmethod
    def setUpClass(cls):
        H, W = 400, 600
        img = np.full((H, W, 3), 255.0)
        img[40:43, 100:500] = 0          # top frame line
        img[340:343, 100:500] = 0        # bottom frame line
        img[40:343, 100:103] = 0         # left frame line
        img[40:343, 497:500] = 0         # right frame line
        img[300:302, 80:100] = 0         # zero tick outside the frame, left
        img[343:355, 300:302] = 0        # axis tick below the frame
        img[55:75, 120:130] = 0          # a panel letter
        cls.x = np.arange(103, 497)
        cls.y = 300 - 200 * np.exp(-0.5 * ((cls.x - 300) / 40.0) ** 2)   # a peak, touching the left frame
        for xx, yy in zip(cls.x, cls.y):
            img[int(round(yy)) - 1:int(round(yy)) + 1, xx] = 0
        cls.img = img

    def test_one_point_per_column_and_only_the_curve(self):
        xs, ys = sd.extract_points(self.img, (60, 20, 540, 380))
        cols = np.round(xs).astype(int)
        self.assertEqual(len(cols), len(np.unique(cols)))
        self.assertGreaterEqual(xs.min(), 100)       # nothing left of the frame (the zero tick)
        self.assertLessEqual(xs.max(), 500)
        truth = np.interp(xs, self.x, self.y)
        inside = (xs > 110) & (xs < 490)
        self.assertLess(np.max(np.abs(ys[inside] - truth[inside])), 2.0)

    def test_one_point_per_column_helper(self):
        pts = np.array([[1.0, 10.0], [2.0, 11.0], [2.0, 90.0], [3.0, 12.0]])
        out = sd.one_point_per_column(pts)
        np.testing.assert_allclose(out, [[1, 10], [2, 11], [3, 12]])


@unittest.skipUnless(os.path.isfile(EXAMPLE), "example image missing")
class ExampleFigureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rgb = np.asarray(Image.open(EXAMPLE).convert("RGB"), float)
        cls.dark = (cls.rgb @ np.array([0.299, 0.587, 0.114])) < 128
        cls.truth = np.genfromtxt(TRUTH, delimiter=",", names=True)
        cls.left, cls.right, cls.rows = sd.find_frame(cls.dark)

    def panel(self, k):
        top, bottom = self.rows[k][-1] + 1, self.rows[k + 1][0]
        zero = sd.find_zero(self.dark, self.left, top, bottom)
        my = int(round(0.03 * (bottom - top)))
        roi = (self.left[-1] + 3, top + my, self.right[0] - 3, bottom - my)
        letter = (roi[0], top, roi[0] + 0.12 * (self.right.mean() - self.left.mean()), top + 0.15 * (bottom - top))
        return roi, zero, letter

    def values(self, k):
        roi, zero, letter = self.panel(k)
        xpx, ypx = sd.extract_points(self.rgb, roi, exclude=[letter], dot_max_frac=0.012)
        xcal = ((float(self.left.mean()), 350.0), (float(self.right.mean()), 900.0))
        x, y = sd.trace_values(xpx, ypx, xcal, None, zero, roi[3])
        return x, y, zero

    def test_frame_has_three_horizontal_lines(self):
        self.assertEqual(len(self.rows), 3)

    def test_zero_ticks_found(self):
        for k in (0, 1):
            self.assertIsNotNone(self.panel(k)[1])

    def check_panel(self, k, column, ylim):
        x, y, zero = self.values(k)
        truth = self.truth[column]
        # pixels -> data units: the panel spans (-0.08 .. ylim) between the centres of its frame lines
        height_px = self.rows[k + 1].mean() - self.rows[k].mean()
        scale = (ylim + 0.08) / height_px
        grid = sd.make_grid(x.min(), x.max(), 1.0)
        got = sd.resample(x, y, grid, 2.0) * scale
        ref = np.interp(grid, self.truth["X"], truth)
        self.assertGreater(len(grid), 500)
        # the peak position must be recovered within a couple of nm
        self.assertLessEqual(abs(grid[np.nanargmax(got)] - grid[np.argmax(ref)]), 3)
        # and the shape within ~2 % of the panel height on average
        self.assertLess(np.nanmean(np.abs(got - ref)) / ylim, 0.02)

    def test_continuous_panel_matches_truth(self):
        self.check_panel(0, "absorbance", 1.15)

    def test_dotted_panel_matches_truth(self):
        self.check_panel(1, "emission", 1.25)


if __name__ == "__main__":
    unittest.main()


class InnerTickTests(unittest.TestCase):
    """Axis ticks pointing into the box must not become curve points, also where the curve hugs the
    frame (a spectrum that falls to the baseline)."""

    def test_inner_ticks_are_ignored(self):
        H, W = 300, 500
        img = np.full((H, W, 3), 255.0)
        img[30:33, 50:450] = 0            # top frame
        img[250:253, 50:450] = 0          # bottom frame
        img[30:253, 50:53] = 0            # left frame
        img[30:253, 447:450] = 0          # right frame
        for x in range(80, 450, 40):      # inner ticks on the bottom frame, 8 px long
            img[242:250, x:x + 2] = 0
            img[33:41, x:x + 2] = 0       # and on the top frame
        for y in range(60, 250, 40):      # inner ticks on the left frame
            img[y:y + 5, 53:64] = 0     # thick ticks, as in a large figure
        xs0 = np.arange(54, 446)
        ys0 = 250 - 150 * np.exp(-0.5 * ((xs0 - 150) / 25.0) ** 2) - 0.0 * xs0
        ys0 = np.where(xs0 > 300, 247.0, ys0)           # right part hugs the bottom frame
        for xx, yy in zip(xs0, ys0):
            img[int(round(yy)) - 1:int(round(yy)) + 1, xx] = 0
        xs, ys = sd.extract_points(img, (40, 20, 460, 265))
        cols = np.round(xs).astype(int)
        self.assertEqual(len(cols), len(np.unique(cols)))
        truth = np.interp(xs, xs0, ys0)
        ok = (xs > 60) & (xs < 440)
        self.assertLess(np.max(np.abs(ys[ok] - truth[ok])), 3.0)


class AnyColourTests(unittest.TestCase):
    def test_any_colour_follows_a_trace_of_mixed_shades(self):
        img = np.full((100, 400, 3), 255.0)      # the trace spans < 70 % of the box: not taken for a ruling line
        shades = [(0, 0, 0), (60, 60, 200), (200, 80, 220), (235, 170, 240)]
        for k, x0 in enumerate(range(20, 260, 60)):
            img[50:52, x0:x0 + 60] = shades[k % 4]          # one horizontal trace, four shades
        xs, ys = sd.extract_points(img, (0, 0, 400, 100), color='any', tol=40)
        self.assertEqual(len(xs), 240)
        np.testing.assert_allclose(ys, 50.5, atol=0.1)
        xs, _ = sd.extract_points(img, (0, 0, 400, 100))    # dark pixels only: black and blue, not the pale shades
        self.assertEqual(len(xs), 120)


class StraightenAngleTests(unittest.TestCase):
    """Counter-clockwise rotation that makes the clicked line horizontal (or vertical if it is closer to it)."""

    def test_horizontal_lines(self):
        f = sd.SpectrumDigitizer._straightening_angle
        self.assertAlmostEqual(f(0, 0, 100, 0), 0.0)
        self.assertAlmostEqual(f(0, 0, 100, 10), np.degrees(np.arctan(0.1)))    # descends to the right: turn CCW
        self.assertAlmostEqual(f(100, 10, 0, 0), np.degrees(np.arctan(0.1)))    # same line, points swapped

    def test_vertical_lines(self):
        f = sd.SpectrumDigitizer._straightening_angle
        self.assertAlmostEqual(f(0, 0, 0, 100), 0.0)
        self.assertAlmostEqual(f(0, 0, 10, 100), -np.degrees(np.arctan(0.1)))   # bottom to the right: turn CW


class BrushGuideTests(unittest.TestCase):
    """Semi-automatic tracing: a brush stroke along one curve keeps the extraction on it where another curve
    crosses it."""

    @classmethod
    def setUpClass(cls):
        W, H = 500, 300
        cls.x = np.arange(40, 460)
        cls.ya = 150 - 70 * np.sin((cls.x - 40) / 420 * 2 * np.pi)       # the curve to follow
        yb = 60 + (cls.x - 40) * 0.45                                    # a line crossing it twice
        img = np.full((H, W, 3), 255.0)
        for xx, a, b in zip(cls.x, cls.ya, yb):
            img[int(round(a)) - 1:int(round(a)) + 1, xx] = 0
            img[int(round(b)) - 1:int(round(b)) + 1, xx] = 0
        cls.img = img
        rng = np.random.default_rng(3)
        cls.stroke = np.c_[cls.x[::7], cls.ya[::7] + rng.normal(0, 2, len(cls.x[::7]))]   # a shaky hand-drawn path

    def test_corridor_covers_the_path_and_not_far_pixels(self):
        m = sd.corridor_mask((100, 200), [(np.array([[20.0, 50.0], [180.0, 50.0]]), 10)])
        self.assertTrue(m[50, 100] and m[53, 100])
        self.assertFalse(m[60, 100])
        self.assertTrue(m[50, 20] and not m[50, 5])

    def test_without_guide_both_curves_are_mixed(self):
        xs, ys = sd.extract_points(self.img, (30, 30, 470, 270))
        self.assertGreater(np.max(np.abs(ys - np.interp(xs, self.x, self.ya))), 20)

    def test_guide_follows_the_stroked_curve_through_the_crossings(self):
        xs, ys = sd.extract_points(self.img, (30, 30, 470, 270), guide=[(self.stroke, 12)])
        cols = np.round(xs).astype(int)
        self.assertEqual(len(cols), len(np.unique(cols)))
        err = np.abs(ys - np.interp(xs, self.x, self.ya))
        self.assertLess(err.mean(), 1.5)
        self.assertLess(err.max(), 8.0)
        self.assertGreater(len(xs), 400)


class EditablePathTests(unittest.TestCase):
    """The brush path can be resampled, smoothed and pulled back onto the curve with a smooth falloff."""

    def test_resample_keeps_ends_and_spacing(self):
        p = sd.resample_path(np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]), 2.0)
        np.testing.assert_allclose(p[0], [0, 0])
        np.testing.assert_allclose(p[-1], [10, 10])
        self.assertTrue(np.allclose(np.hypot(*np.diff(p, axis=0).T), 2.0))
        self.assertEqual(len(sd.resample_path(np.array([[1.0, 1.0], [1.0, 1.0]]), 2.0)), 1)   # a click

    def test_smooth_keeps_ends_and_reduces_jitter(self):
        rng = np.random.default_rng(1)
        x = np.linspace(0, 100, 80)
        noisy = np.c_[x, 20 + rng.normal(0, 2, len(x))]
        out = sd.smooth_path(noisy, 2.0)
        np.testing.assert_allclose(out[[0, -1]], noisy[[0, -1]])
        self.assertLess(np.std(out[5:-5, 1] - 20), np.std(noisy[5:-5, 1] - 20) / 2)

    def test_reshape_moves_the_grabbed_point_fully_and_far_ones_not_at_all(self):
        p = np.c_[np.arange(0.0, 200.0, 2.0), np.zeros(100)]
        q = sd.reshape_path(p, 50, (0.0, 10.0), 40.0)
        self.assertAlmostEqual(q[50, 1], 10.0)                    # grabbed vertex follows the mouse
        self.assertAlmostEqual(q[0, 1], 0.0)                      # beyond the reach: untouched
        self.assertAlmostEqual(q[-1, 1], 0.0)
        bump = q[50:71, 1]
        self.assertTrue(np.all(np.diff(bump) <= 1e-9))            # decays smoothly, no jump
        self.assertLess(np.max(np.abs(np.diff(q[:, 1]))), 1.0)


class BrushGuideAxisTests(unittest.TestCase):
    """With a brush the box is only the corridor's bounding box, so the axes are found over the whole image:
    a curve that falls onto the x axis must not be replaced by the axis."""

    @staticmethod
    def _plot():
        img = np.full((300, 500, 3), 255.0)
        img[250:254, 50:450] = 0                        # x axis: 4 px thick, across 80 % of the image
        img[40:254, 50:54] = 0                          # y axis
        for x in range(60, 440, 40):                    # inner ticks on the x axis
            img[240:250, x:x + 3] = 0
        xs = np.arange(60, 440)
        ys = 249 - 180 * np.exp(-(xs - 60) / 45.0)      # falls from the top towards the axis and hugs it
        for x, y in zip(xs, ys):
            img[int(round(y)) - 1:int(round(y)) + 1, x] = (200, 0, 0)
        return img, xs, ys

    def test_axis_is_not_taken_for_the_curve(self):
        img, xs, ys = self._plot()
        path = np.c_[xs[::4], ys[::4] + 3]              # a hand a little low: the corridor reaches the axis
        for color in (None, (200, 0, 0), 'any'):
            px, py = sd.extract_points(img, (xs[0] - 10, 30, xs[-1] + 10, 270), color, 128, 60,
                                       guide=[(path, 24)])
            self.assertGreater(len(px), 100, color)
            self.assertLess(py.max(), 250, color)       # nothing on the axis rows (250..253)

    def test_a_flat_curve_traced_on_purpose_is_kept(self):
        img = np.full((300, 500, 3), 255.0)
        img[149:151, 80:380] = 0                        # a flat curve across 60 % of the image
        path = np.c_[np.arange(80.0, 380.0, 4), np.full(75, 150.0)]
        px, py = sd.extract_points(img, (70, 130, 390, 170), guide=[(path, 12)])
        self.assertGreater(len(px), 250)
        np.testing.assert_allclose(py, 150.0, atol=1.0)


class NiceTicksTests(unittest.TestCase):
    def test_round_values_inside_the_range(self):
        t = sd.nice_ticks(396.2, 802.5)
        np.testing.assert_allclose(t, np.arange(400, 801, 50))
        np.testing.assert_allclose(sd.nice_ticks(0.03, 0.81), np.arange(1, 9) / 10)

    def test_reversed_range_and_degenerate_input(self):
        np.testing.assert_allclose(sd.nice_ticks(4000, 400)[:3], [500, 1000, 1500])    # FTIR-style order
        self.assertEqual(len(sd.nice_ticks(5, 5)), 0)
        self.assertEqual(len(sd.nice_ticks(np.nan, 5)), 0)


class LogAxisTests(unittest.TestCase):
    """Calibration on a logarithmic axis: log10 of the value is linear in the pixel."""

    def test_pixel_to_value_and_back(self):
        cal = sd.AxisCal(100.0, 1.0, 500.0, 1000.0, log=True)        # 3 decades over 400 px
        self.assertAlmostEqual(float(sd.lin_map(cal, 300.0)), 10 ** 1.5)
        np.testing.assert_allclose(sd.lin_map(cal, [100.0, 200.0, 500.0]), [1.0, 10 ** 0.75, 1000.0])
        np.testing.assert_allclose(cal.to_pixel(sd.lin_map(cal, [123.0, 456.0])), [123.0, 456.0])
        self.assertEqual(cal[0], (100.0, 1.0))                        # still a plain pair of points

    def test_reversed_log_axis(self):
        cal = sd.AxisCal(50.0, 1.0, 350.0, 1e-3, log=True)           # a decay plot: Y falls with the row
        self.assertAlmostEqual(float(sd.lin_map(cal, 200.0)), 10 ** -1.5)

    def test_linear_calibration_is_unchanged(self):
        cal = sd.AxisCal(100.0, 400.0, 900.0, 800.0)
        self.assertAlmostEqual(float(sd.lin_map(cal, 500.0)), 600.0)
        self.assertAlmostEqual(float(cal.to_pixel(600.0)), 500.0)

    def test_trace_values_on_log_axes(self):
        xcal = sd.AxisCal(0.0, 1.0, 300.0, 1000.0, log=True)
        ycal = sd.AxisCal(0.0, 1.0, 100.0, 100.0, log=True)
        x, y = sd.trace_values(np.array([300.0, 0.0, 150.0]), np.array([100.0, 0.0, 50.0]), xcal, ycal, None, 0)
        np.testing.assert_allclose(x, [1.0, 10 ** 1.5, 1000.0])
        np.testing.assert_allclose(y, [1.0, 10.0, 100.0])

    def test_grid_regular_in_decades(self):
        g = sd.make_grid(np.log10(1.2), np.log10(900.0), 1 / 10)       # what build_output does: step in log10
        r = 10.0 ** g
        np.testing.assert_allclose(r[1:] / r[:-1], 10 ** 0.1)
        self.assertGreaterEqual(r[0], 1.2 * (1 - 1e-9))

    def test_log_ticks(self):
        np.testing.assert_allclose(sd.log_ticks(0.8, 12000.0), [1, 10, 100, 1000, 10000])       # > 4 decades: powers only
        np.testing.assert_allclose(sd.log_ticks(3, 400), [5, 10, 20, 50, 100, 200])             # 2 decades: 1, 2, 5
        self.assertEqual(len(sd.log_ticks(-1, 10)), 0)                                          # no log scale below 0


class CurveMetricsTests(unittest.TestCase):
    def test_area_peak_and_length_of_a_triangle(self):
        m = sd.curve_metrics([0, 1, 2], [0, 1, 0])
        self.assertAlmostEqual(m['area'], 1.0)
        self.assertAlmostEqual(m['peak_x'], 1.0)
        self.assertAlmostEqual(m['peak_height'], 1.0)
        self.assertAlmostEqual(m['length'], 2 * np.sqrt(2))

    def test_limits_are_interpolated(self):
        m = sd.curve_metrics([0, 1, 2], [0, 1, 0], lo=0.5, hi=1.5)       # limits not on the data points
        np.testing.assert_allclose(m['xs'], [0.5, 1.0, 1.5])
        self.assertAlmostEqual(m['area'], 0.75)

    def test_straight_baseline_is_subtracted(self):
        x = np.linspace(0, 10, 101)
        y = 2 + 0.5 * x + np.exp(-((x - 5) / 1.0) ** 2)                   # a Gaussian peak on a slope
        plain = sd.curve_metrics(x, y)['area']
        m = sd.curve_metrics(x, y, baseline=True)
        self.assertAlmostEqual(m['area'], np.sqrt(np.pi), delta=0.01)      # the Gaussian alone (limits far in its tails)
        self.assertAlmostEqual(plain - m['area'], 45.0, delta=0.05)       # what lies under the baseline: 2*10 + 0.5*10^2/2
        self.assertAlmostEqual(m['peak_x'], 5.0, delta=0.11)
        self.assertAlmostEqual(sd.curve_metrics(x, 0.5 * x, baseline=True)['area'], 0.0, places=9)

    def test_nan_and_degenerate_input(self):
        m = sd.curve_metrics([0, 1, 2, 3], [0, np.nan, 2, 3])
        self.assertAlmostEqual(m['area'], 4.5)                             # the NaN point is skipped, not zero
        self.assertIsNone(sd.curve_metrics([0, 1], [1, 1], lo=5, hi=6))
        self.assertIsNone(sd.curve_metrics([0.0], [1.0]))
        self.assertIsNone(sd.curve_metrics([0, 1, 2], [1, 1, 1], lo=2, hi=1))
