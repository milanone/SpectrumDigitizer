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
