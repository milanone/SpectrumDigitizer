"""Draw the synthetic test figure examples/example_two_panels.png (Origin-style, two stacked
panels sharing the X axis: a continuous curve and a dotted one). No real data is involved.

    python examples/make_example.py
"""
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
x = np.arange(350, 901, 1.0)


def band(x0, w, a):
    return a * np.exp(-0.5 * ((x - x0) / w) ** 2)


absorbance = 0.08 + band(480, 35, 0.9) + band(620, 60, 0.4) + band(780, 40, 0.15)
emission = 0.05 + band(560, 30, 1.0) + band(690, 45, 0.55)

fig, axes = plt.subplots(2, 1, sharex=True, figsize=(8.5, 6), dpi=200,
                         gridspec_kw=dict(hspace=0))
axes[0].plot(x, absorbance, color='black', lw=1.6)
axes[1].plot(x[::6], emission[::6], 'o', color='black', ms=2.8)
for ax, top, letter in zip(axes, (1.15, 1.25), 'ab'):
    ax.set_xlim(350, 900)
    ax.set_ylim(-0.08, top)   # zero tick inside the frame, as in Origin plots
    ax.set_yticks([0])
    ax.set_yticklabels(['0'])
    ax.tick_params(direction='out', length=9, width=1.5, labelsize=11)
    for s in ax.spines.values():
        s.set_linewidth(1.5)
    ax.text(0.03, 0.88, letter, transform=ax.transAxes, fontsize=16, fontweight='bold')
axes[1].set_xlabel("Wavelength (nm)", fontsize=12)
fig.subplots_adjust(left=0.1, right=0.97, top=0.97, bottom=0.12)
fig.savefig(os.path.join(HERE, 'example_two_panels.png'))
np.savetxt(os.path.join(HERE, 'example_two_panels_truth.csv'),
           np.column_stack([x, absorbance, emission]), delimiter=',',
           header='X,absorbance,emission', comments='')
