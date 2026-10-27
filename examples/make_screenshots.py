"""Take the README screenshots from the real program on the synthetic figure examples/example_two_panels.png
(no real data involved). Windows only: it grabs the program window from the screen.

    python examples/make_screenshots.py

It writes screenshot.png (Digitize tab), screenshot_clean.png (Clean image tab) and screenshot_result.png
(Output tab with the large result plot and an area analysis) next to the README.
"""
import ctypes
import importlib.machinery
import os
import time
import types

from PIL import ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # real pixels, so the grab and the window coordinates agree
except Exception:
    pass

sd = importlib.machinery.SourceFileLoader("sd", os.path.join(ROOT, "SpectrumDigitizer.pyw")).load_module()
root = sd.TkinterDnD.Tk() if sd.HAS_DND else sd.tk.Tk()
app = sd.SpectrumDigitizer(root)
root.geometry("1400x880+20+20")
E = lambda x, y: types.SimpleNamespace(xdata=x, ydata=y)


def grab(name):
    """The program window, title bar and menu included, from the screen."""
    root.attributes('-topmost', True)
    for _ in range(4):
        root.update()
        time.sleep(0.25)
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    rect = ctypes.wintypes.RECT()
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))   # visible frame
    ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom)).save(os.path.join(ROOT, name), optimize=True)
    root.attributes('-topmost', False)


import ctypes.wintypes  # noqa: E402  (after the DPI call)

app.load_image(os.path.join(HERE, "example_two_panels.png"))
root.update()

# 1. Digitize: the two panels found automatically, the result normalized to the maximum
app._ask_form = lambda *a, **k: ("350", "900")          # the dialog of 'Auto-detect panels'
app.nb.select(1)
app.auto_panels()
app.v_norm.set('max')
app.v_step.set('2')
app._extract_all()
app._set_status("2 panels found: red points = extracted curves; the plot below shows the result.", ok=True, sticky=True)
grab("screenshot.png")

# 2. Clean image: a working area with its handles around the plot, rotated a little
app.nb.select(0)
root.update()
H, W = app.rgb.shape[:2]
app._start_rect('work')
app._on_rect(E(0.12 * W, 0.08 * H), E(0.93 * W, 0.93 * H))
app.v_angle.set('0')
app._sticky = False
app._update_hint()
grab("screenshot_clean.png")

# 3. Output: the large result plot, the area of a band above a straight baseline
app.nb.select(2)
app.v_an_curve.set(app.traces[0]['name'])
app.v_an_lo.set('400')
app.v_an_hi.set('560')
app.v_an_base.set(True)
app.compute_analysis()
app._set_status("Output: step, smoothing and normalization act on the plot; the analysis shades the integrated area.",
                ok=True, sticky=True)
# the Output tab is taller than the window: scroll it to the bottom so the analysis numbers show
root.update()
app.nb.nametowidget(app.nb.tabs()[2]).winfo_children()[0].yview_moveto(1.0)
grab("screenshot_result.png")
root.destroy()
print("screenshots written")
