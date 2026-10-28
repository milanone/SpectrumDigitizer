"""One Undo / Redo for the three tabs: image edits and curve edits are taken back in the order they were made.

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
loader = importlib.machinery.SourceFileLoader("sd_undo", os.path.join(HERE, "..", "SpectrumDigitizer.pyw"))
spec = importlib.util.spec_from_loader("sd_undo", loader)
sd = importlib.util.module_from_spec(spec)
loader.exec_module(sd)


class UndoTests(unittest.TestCase):
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
        img = Image.new("RGB", (200, 120), "white")
        d = ImageDraw.Draw(img)
        d.line([(10, 100), (60, 40), (110, 70), (190, 20)], fill=(200, 0, 0), width=3)
        self.app._set_image(img, "t.png", None, title="t")
        self.start = self.app.rgb.copy()

    def add_trace(self, name):
        tr = dict(name=name, roi=(0.0, 0.0, 200.0, 120.0), color=None, zero=None, exclude=[],
                  xpx=np.array([10.0, 60.0]), ypx=np.array([100.0, 40.0]))
        self.app.traces.append(tr)
        self.app._guide_push(tr, created=True)             # what the brush does for a new curve
        self.app._refresh_list(select=len(self.app.traces) - 1)
        return tr

    def test_undo_follows_the_order_across_kinds(self):
        a = self.app
        a.flatten_colors()                                   # image edit (Clean tab)
        flat = a.rgb.copy()
        self.add_trace("c1")                                 # curve edit (Digitize tab)
        self.assertEqual(len(a.traces), 1)
        a.undo_any()                                         # the curve first, the image untouched
        self.assertEqual(len(a.traces), 0)
        np.testing.assert_array_equal(a.rgb, flat)
        a.undo_any()                                         # then the image
        np.testing.assert_array_equal(a.rgb, self.start)
        a.redo_any()
        np.testing.assert_array_equal(a.rgb, flat)
        self.assertEqual(len(a.traces), 0)
        a.redo_any()
        self.assertEqual(len(a.traces), 1)

    def test_a_new_edit_ends_the_redo_history(self):
        a = self.app
        a.flatten_colors()
        a.undo_any()
        self.add_trace("c1")
        a.redo_any()                                         # nothing: the new edit cleared it
        np.testing.assert_array_equal(a.rgb, self.start)

    def test_erase_is_one_step(self):
        a = self.app
        tr = self.add_trace("c1")
        tr['xpx'], tr['ypx'] = np.arange(10.0, 60.0), 100 - 1.2 * (np.arange(10.0, 60.0) - 10)
        a.erase_trace()
        self.assertTrue(tr.get('fixed'))
        self.assertFalse(np.array_equal(a.rgb, self.start))
        a.undo_any()                                         # pixels and the fixed flag together
        np.testing.assert_array_equal(a.rgb, self.start)
        self.assertFalse(tr.get('fixed'))
        a.redo_any()
        self.assertTrue(tr.get('fixed'))
        self.assertFalse(np.array_equal(a.rgb, self.start))

    def test_nothing_to_undo_is_harmless(self):
        self.app.undo_any()
        self.app.redo_any()
        np.testing.assert_array_equal(self.app.rgb, self.start)

    def test_ctrl_z_works_in_any_tab(self):
        a = self.app
        a.flatten_colors()
        a.nb.select(1)                                       # Digitize tab
        ev = mock.Mock(widget=a.root)
        a._on_ctrl_z(ev, False)
        np.testing.assert_array_equal(a.rgb, self.start)
        a.nb.select(2)                                       # Output tab
        a._on_ctrl_z(ev, True)
        self.assertFalse(np.array_equal(a.rgb, self.start))

    def test_extracted_points_checkbox_is_in_the_digitize_tab(self):
        def parent_tab(w):
            while w is not None and w not in (self.app.nb,):
                w = w.master
            return w
        boxes = []
        def walk(w):
            if isinstance(w, tk.Checkbutton) and 'extracted points' in w.cget('text'):
                boxes.append(w)
            for c in w.winfo_children():
                walk(c)
        walk(self.app.nb)
        self.assertEqual(len(boxes), 1)
        tabs = [self.app.nb.nametowidget(t) for t in self.app.nb.tabs()]
        owner = boxes[0]
        while owner.master not in tabs and owner.master is not None:
            owner = owner.master
        self.assertIs(owner.master, tabs[1])


if __name__ == "__main__":
    unittest.main()
