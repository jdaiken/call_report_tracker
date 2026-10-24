"""Static PNG line charts for the weekly look book (email can't run interactive
charts). Design rules: one y-axis per chart, at most four series in a fixed
color order, 2px lines, quiet grid, a legend plus a direct label with the
latest value at each line's end so color never carries meaning alone.

Palette: the validated default categorical order (blue, orange, aqua, yellow)
on a white surface. Aqua and yellow sit below 3:1 contrast, which the direct
end labels (in text ink, not the series color) compensate for.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.ticker  # noqa: E402,F401
import matplotlib.pyplot as plt  # noqa: E402

SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, QUIET, GRID, SURFACE = "#1f2328", "#5f6670", "#e6e7ea", "#ffffff"


@dataclass
class Line:
    label: str
    points: list[tuple[date, float]]


def _fmt_factory(kind: str) -> Callable[[float], str]:
    return {
        "pct": lambda v: f"{v:.2f}%",
        "points": lambda v: f"{v:+.2f}",
        "billions": lambda v: f"${v / 1000:.2f}T" if abs(v) >= 1000 else f"${v:,.0f}B",
        "index": lambda v: f"{v:.1f}",
        "level": lambda v: f"{v:,.0f}" if abs(v) >= 100 else f"{v:.1f}",
    }.get(kind, lambda v: f"{v:,.2f}")


def index_to_100(points: list[tuple[date, float]], start: date) -> list[tuple[date, float]]:
    """Rescale so the first value on/after `start` is 100: puts different-sized
    series on one honest axis (instead of two y-axes)."""
    window = [(d, v) for d, v in points if d >= start]
    if not window or not window[0][1]:
        return []
    base = window[0][1]
    return [(d, v / base * 100) for d, v in window]


def line_chart(lines: list[Line], kind: str, start: date, end: Optional[date] = None,
               zero_line: bool = False, ref_line: Optional[tuple[float, str]] = None,
               step: bool = False) -> Optional[bytes]:
    """Return PNG bytes, or None if there's nothing to draw."""
    lines = [Line(l.label, [(d, v) for d, v in l.points if d >= start and (end is None or d <= end)])
             for l in lines[:4]]
    lines = [l for l in lines if len(l.points) >= 2]
    if not lines:
        return None
    fmt = _fmt_factory(kind)

    fig, ax = plt.subplots(figsize=(6.4, 2.9), dpi=130)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.tick_params(colors=QUIET, labelsize=8, length=0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: fmt(v)))
    span_days = (max(d for l in lines for d, _ in l.points) - min(d for l in lines for d, _ in l.points)).days
    if span_days > 900:
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    else:
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3 if span_days > 400 else 2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))

    if zero_line:
        ax.axhline(0, color=QUIET, linewidth=1, zorder=1)
    if ref_line:
        ax.axhline(ref_line[0], color=QUIET, linewidth=1, linestyle=(0, (3, 3)), zorder=1)
        if ref_line[1]:
            ax.text(0.005, ref_line[0], f"{ref_line[1]} ", transform=ax.get_yaxis_transform(), color=QUIET,
                    fontsize=7.5, va="bottom", ha="left",
                    bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1.0), zorder=5)

    last_x = max(l.points[-1][0] for l in lines)
    label_ys = []
    for i, l in enumerate(lines):
        xs = [d for d, _ in l.points]
        ys = [v for _, v in l.points]
        color = SERIES_COLORS[i]
        if step:
            ax.step(xs, ys, where="post", color=color, linewidth=2, label=l.label, zorder=3)
        else:
            ax.plot(xs, ys, color=color, linewidth=2, label=l.label, zorder=3, solid_capstyle="round")
        ax.scatter([xs[-1]], [ys[-1]], s=22, color=color, edgecolors=SURFACE, linewidths=1.5, zorder=4)
        label_ys.append((ys[-1], l, color))

    # Direct end labels, nudged apart so they don't collide.
    ymin, ymax = ax.get_ylim()
    min_gap = (ymax - ymin) * 0.07
    placed: list[float] = []
    for y, l, color in sorted(label_ys, key=lambda t: t[0]):
        y_lab = y
        for p in placed:
            if abs(y_lab - p) < min_gap:
                y_lab = p + min_gap
        placed.append(y_lab)
        ax.annotate(fmt(y), xy=(last_x, y), xytext=(last_x + timedelta(days=max(3, span_days // 60)), y_lab),
                    color=INK, fontsize=8, va="center", annotation_clip=False)
    ax.set_xlim(right=last_x + timedelta(days=max(10, span_days // 9)))
    # Don't label dates past the end of the data (the extra room is for end labels).
    fig.canvas.draw()
    cutoff = mdates.date2num(last_x)
    ax.set_xticks([t for t in ax.get_xticks() if t <= cutoff])

    if len(lines) > 1:
        leg = ax.legend(loc="upper left", bbox_to_anchor=(0, 1.16), ncol=len(lines), frameon=False,
                        fontsize=8, handlelength=1.4, columnspacing=1.2)
        for t in leg.get_texts():
            t.set_color(INK)
    fig.tight_layout(pad=0.6)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()
