"""A dashed curve under a continuous one: erase the digitized curve from the image, then join the dashes.

Synthetic plot with known curves: axes, a continuous curve and a dashed one that cross each other.

    python -m unittest discover -s tests -v
"""
import importlib.machinery
import importlib.util
import os
import tkinter as tk
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("sd_dashes", os.path.join(HERE, "..", "SpectrumDigitizer.pyw"))
spec = importlib.util.spec_from_loader("sd_dashes", loader)
sd = importlib.util.module_from_spec(spec)
loader.exec_module(sd)

W, H = 420, 260
X0, X1, Y0, Y1 = 40, 400, 20, 220          # the axes: left and bottom lines


def solid_y(x):
    return 120 - 70 * np.exp(-((x - 160) / 40.0) ** 2)


def dashed_y(x):
    return 160 - 90 * np.exp(-((x - 250) / 35.0) ** 2) + 0.05 * (x - 250)


def make_plot():
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.line([(X0, Y0), (X0, Y1), (X1, Y1)], fill="black", width=3)
    xs = np.arange(X0 + 6, X1 - 2, 1.0)
    d.line(list(zip(xs, solid_y(xs))), fill="black", width=3)
    # dashes of 14 px along the curve with gaps of 9 px (measured along x here: the curve is gentle)
    for a in np.arange(X0 + 6, X1 - 20, 23.0):
        seg = np.arange(a, a + 14, 1.0)
        d.line(list(zip(seg, dashed_y(seg))), fill="black", width=3)
    return np.asarray(img).astype(float), xs


def coverage(xp):
    """Fraction of the columns between the ends of the points that have a point."""
    return len(np.unique(np.round(xp))) / max(np.ptp(xp) + 1, 1)


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rgb, cls.xs = make_plot()
        cls.xp, cls.yp = cls.xs, solid_y(cls.xs)           # the digitized continuous curve

    def test_erase_removes_the_curve_and_keeps_axes_and_the_other_line(self):
        hit = sd.erase_mask_along(self.rgb, self.xp, self.yp, 4)
        out = self.rgb.copy()
        out[hit] = 255.0
        ink = out.sum(axis=2) < 600
        band = np.abs(np.arange(H)[:, None] - solid_y(np.arange(W))[None, :]) < 3
        band &= ~(np.abs(np.arange(H)[:, None] - dashed_y(np.arange(W))[None, :]) < 8)   # not where they meet
        band[:, :X0 + 6] = False
        band[:, X1 - 2:] = False
        far = np.abs(np.arange(H)[:, None] - dashed_y(np.arange(W))[None, :]) < 2
        far &= ~(np.abs(np.arange(H)[:, None] - solid_y(np.arange(W))[None, :]) < 8)     # not where they meet
        far[:, :X0 + 6] = False
        self.assertLess(ink[band].mean(), 0.02)                                          # the curve is gone
        self.assertGreater((self.rgb.sum(axis=2) < 600)[far].sum() * 0.9, 0)
        self.assertGreater(ink[far].mean(), 0.4)                                         # the dashed one stays
        self.assertTrue(ink[Y0:Y1, X0 - 1:X0 + 2].all())                                 # left axis intact
        self.assertTrue(ink[Y1 - 1:Y1 + 2, X0:X1].all())                                 # bottom axis intact

    def test_join_makes_the_dashed_curve_continuous(self):
        hit = sd.erase_mask_along(self.rgb, self.xp, self.yp, 4)
        er = self.rgb.copy()
        er[hit] = 255.0
        box = (X0 + 6, Y0, X1 - 2, Y1 - 4)
        before = sd.extract_points(er, box, None, 128, 60, 0.012)
        joined, n = sd.join_dashes(er, 60, 30)      # a crossing leaves a longer gap
        after = sd.extract_points(joined, box, None, 128, 60, 0.012)
        self.assertGreater(n, 10)
        self.assertGreater(coverage(after[0]), 0.95)            # the apex of the peak stays open: its flanks do not point at each other
        self.assertGreater(coverage(after[0]), coverage(before[0]))
        # and it follows the truth
        err = np.abs(np.interp(after[0], after[0], after[1]) - dashed_y(after[0]))
        self.assertLess(np.median(err), 2.0)

    def test_gap_limit(self):
        _, n = sd.join_dashes(self.rgb, 60, 3)
        self.assertEqual(n, 0)

    def test_nothing_to_join_in_a_continuous_curve(self):
        img = Image.new("RGB", (200, 100), "white")
        ImageDraw.Draw(img).line([(10, 50), (190, 50)], fill="black", width=3)
        _, n = sd.join_dashes(np.asarray(img).astype(float), 60, 20)
        self.assertEqual(n, 0)


class LinkTests(unittest.TestCase):
    def two_dashes(self):
        img = Image.new("RGB", (120, 60), "white")
        d = ImageDraw.Draw(img)
        d.line([(10, 30), (40, 30)], fill=(200, 0, 0), width=3)
        d.line([(70, 30), (100, 30)], fill=(200, 0, 0), width=3)
        return np.asarray(img).astype(float)

    def test_the_gap_is_filled_with_the_colour_and_thickness_of_the_line(self):
        rgb = self.two_dashes()
        out, ends = sd.draw_link(rgb, 60, (42, 33), (68, 27))            # a little off the ends: they snap
        self.assertEqual(tuple(out[30, 55]), (200.0, 0.0, 0.0))
        self.assertEqual(tuple(out[29, 55]), (200.0, 0.0, 0.0))          # same thickness (3 px)
        self.assertEqual(tuple(out[34, 55]), (255.0, 255.0, 255.0))
        self.assertAlmostEqual(ends[0][0], 40, delta=1)
        self.assertAlmostEqual(ends[1][0], 70, delta=1)

    def test_far_from_any_line_nothing_is_drawn(self):
        self.assertIsNone(sd.draw_link(self.two_dashes(), 60, (50, 5), (55, 5)))

    def test_one_end_is_enough(self):
        out, _ = sd.draw_link(self.two_dashes(), 60, (40, 30), (55, 45))   # the second end is in the white
        self.assertEqual(tuple(out[37, 47]), (200.0, 0.0, 0.0))


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = sd.TkinterDnD.Tk() if sd.HAS_DND else tk.Tk()
        except tk.TclError as e:
            raise unittest.SkipTest("Tk not available: %s" % e)
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        with mock.patch.object(sd.messagebox, "showwarning"), mock.patch.object(sd.messagebox, "askokcancel"):
            self.app = sd.SpectrumDigitizer(self.root)
        rgb, xs = make_plot()
        self.app._set_image(Image.fromarray(rgb.astype(np.uint8)), "synthetic.png", None, title="synthetic")
        self.original = self.app.rgb.copy()
        self.tr = dict(name="solid", roi=(X0 + 6.0, 0.0, X1 - 2.0, float(Y1 - 4)), color=None, zero=None, exclude=[],
                       xpx=xs, ypx=solid_y(xs))
        self.app.traces.append(self.tr)
        self.app._refresh_list(select=0)

    def test_the_gap_follows_the_image_width(self):
        self.assertEqual(self.app.v_gap.get(), str(round(0.03 * W)))

    def test_erase_keeps_the_points_and_can_be_undone(self):
        self.app.erase_trace()
        self.assertTrue(self.tr.get('fixed'))                         # the points stay: Re-extract would find nothing
        self.assertEqual(len(self.tr['xpx']), len(self.original[0]) and len(self.tr['xpx']))
        self.assertLess((self.app.rgb.sum(axis=2) < 600).sum(), (self.original.sum(axis=2) < 600).sum())
        self.app.undo_filter()
        np.testing.assert_array_equal(self.app.rgb, self.original)

    def test_join_then_undo(self):
        self.app.erase_trace()
        erased = self.app.rgb.copy()
        self.app.v_gap.set('15')
        self.app.join_dashed_lines()
        self.assertGreater((self.app.rgb.sum(axis=2) < 600).sum(), (erased.sum(axis=2) < 600).sum())
        self.app.undo_filter()
        np.testing.assert_array_equal(self.app.rgb, erased)

    def mouse(self, name, x, y, button=1):
        from matplotlib.backend_bases import MouseEvent
        px, py = self.app.ax_img.transData.transform((x, y))
        return MouseEvent(name, self.app.canvas, px, py, button=button)

    def test_link_by_drag_is_one_undo_step(self):
        a = self.app
        a._start_link()
        before = a.rgb.copy()
        # a gap in the dashed curve: end of the dash at x=X0+6+14, next dash starts 9 px later
        xa, xb = X0 + 6 + 14, X0 + 6 + 23
        ya, yb = float(dashed_y(xa)), float(dashed_y(xb))
        a._on_press(self.mouse('button_press_event', xa, ya))
        self.assertIsNotNone(a._link_line)                              # the rubber band
        a._on_motion(self.mouse('motion_notify_event', xb, yb))
        a._on_release(self.mouse('button_release_event', xb, yb))
        self.assertIsNone(a._link_line)
        self.assertFalse(np.array_equal(a.rgb, before))
        self.assertEqual(a.mode, 'link')                                # the tool stays on
        a.undo_any()
        np.testing.assert_array_equal(a.rgb, before)

    def test_a_click_alone_links_nothing(self):
        a = self.app
        a._start_link()
        before = a.rgb.copy()
        a._on_press(self.mouse('button_press_event', 100, 100))
        a._on_release(self.mouse('button_release_event', 100, 100))
        np.testing.assert_array_equal(a.rgb, before)
        self.assertEqual(a.rgb_undo, [])

    def test_join_with_nothing_to_join_changes_nothing(self):
        self.app.v_gap.set('1')
        self.app.join_dashed_lines()
        np.testing.assert_array_equal(self.app.rgb, self.original)
        self.assertEqual(self.app.rgb_undo, [])


if __name__ == "__main__":
    unittest.main()
