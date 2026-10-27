"""Design system for the Network Exposure report: one palette, one typeface (Inter, bundled under fonts/, SIL OFL)
and shared chart helpers, so every chart and page looks like part of the same document.

Rules the charts follow
  - Navy/blue for data, greys for context, red only for a breach (over the cap, over the special cap, below a line).
  - No chart titles (the page headline says what the chart shows), no boxes, light horizontal or vertical grid only.
  - Direct labels at the end of bars instead of legends wherever possible.
"""
from __future__ import annotations

import io
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "fonts"

NAVY = "#14325c"      # headings, key numbers
BLUE = "#2f64a8"      # primary data
SKY = "#9dbbdf"       # secondary data
INK = "#1d2530"       # body text
MUTED = "#667180"     # captions, axis labels
RULE = "#dde2e8"      # hairlines
GRIDC = "#eceff3"     # chart grid
PANEL = "#f5f7fa"     # light panel background
CONTEXT = "#c9d2dc"   # context bars
RED = "#c2412d"       # breach
AMBER = "#c98a1b"     # caution (used sparingly)
SEQ = ["#e4e9ef", "#c9d8ea", "#97b5d9", "#5d8bc4", "#2f5f9e", RED]   # none, <25, 25-50, 50-80, 80-100, >100

_mpl_ready = False


def mpl():
    """matplotlib, set up with the bundled Inter and the report's defaults."""
    global _mpl_ready
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    if not _mpl_ready:
        from matplotlib import font_manager as fm
        for f in FONT_DIR.glob("Inter-*.ttf"):
            fm.fontManager.addfont(str(f))
        plt.rcParams.update({
            "font.family": "Inter", "font.size": 9, "axes.edgecolor": RULE, "axes.labelcolor": MUTED,
            "axes.labelsize": 8.5, "xtick.color": MUTED, "ytick.color": INK, "xtick.labelsize": 8.5,
            "ytick.labelsize": 9, "axes.titleweight": "semibold", "figure.facecolor": "white",
            "axes.facecolor": "white", "savefig.facecolor": "white"})
        _mpl_ready = True
    return plt


def png(fig) -> bytes:
    plt = mpl()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=220, bbox_inches="tight", pad_inches=0.06)
    plt.close(fig)
    return buf.getvalue()


def clean(ax, grid: str = "x"):
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0, pad=5)
    if grid:
        getattr(ax, f"{grid}axis").grid(True, color=GRIDC, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)


def dollars(text: str) -> str:
    """Escape $ for matplotlib (it starts math mode)."""
    return text.replace("$", "\\$")


_rl_ready = False


def reportlab_fonts():
    """Register Inter with reportlab: Inter (regular), Inter-Medium, Inter-SemiBold, Inter-Bold."""
    global _rl_ready
    if _rl_ready:
        return
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for name, f in (("Inter", "Inter-Regular.ttf"), ("Inter-Medium", "Inter-Medium.ttf"),
                    ("Inter-SemiBold", "Inter-SemiBold.ttf"), ("Inter-Bold", "Inter-Bold.ttf")):
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / f)))
    addMapping("Inter", 0, 0, "Inter")
    addMapping("Inter", 1, 0, "Inter-SemiBold")
    addMapping("Inter", 0, 1, "Inter")
    addMapping("Inter", 1, 1, "Inter-SemiBold")
    _rl_ready = True
