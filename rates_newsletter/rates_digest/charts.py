"""Static PNG line charts for the newsletter (email can't run interactive
charts), built to be read at a glance:

- large, with big tick labels and thick lines
- one y-axis per chart, at most three series, in a fixed color order
- every line labeled at its end with its name and latest value
- Watch and Risk ranges shaded in light amber and red, labeled in text, so the
  chart shows how close a reading is to trouble (color is never the only cue)
- an optional shaded band (e.g. the Fed's target range)

Palette: the validated default categorical order (blue, orange, aqua) on white.
Aqua sits below 3:1 contrast, which the text end labels compensate for.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402

SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, QUIET, GRID, SURFACE = "#1f2328", "#4a5058", "#e3e5e8", "#ffffff"
BAND_FILL = {"watch": "#fbe8c0", "risk": "#f7d2cf"}
BAND_TEXT = {"watch": "#7a5000", "risk": "#a1231b"}


@dataclass
class Line:
    label: str
    points: list[tuple[date, float]]


@dataclass
class Band:
    """A shaded y-range (a Watch or Risk zone)."""
    lo: Optional[float]
    hi: Optional[float]
    tone: str       # watch | risk
    label: str


def fmt_factory(kind: str) -> Callable[[float], str]:
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


def line_chart(lines: list[Line], kind: str, start: date, *, zero_line: bool = False,
               ref_line: Optional[tuple[float, str]] = None, step: bool = False,
               bands: Optional[list[Band]] = None,
               range_band: Optional[tuple[list[tuple[date, float]], list[tuple[date, float]], str]] = None,
               height: float = 4.05,
               compact: bool = False) -> Optional[bytes]:
    """Return PNG bytes, or None if there's nothing to draw."""
    lines = [Line(l.label, [(d, v) for d, v in l.points if d >= start]) for l in lines[:3]]
    lines = [l for l in lines if len(l.points) >= 2]
    if not lines:
        return None
    fmt = fmt_factory(kind)
    plt.rcParams["font.family"] = "DejaVu Sans"

    # compact = a panel in a 2x2 grid (about 3.5 inches wide in print)
    fs = 9.5 if compact else 10.5          # base font size
    fig, ax = plt.subplots(figsize=(4.9, height) if compact else (7.6, 3.9), dpi=200 if compact else 150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.9)
    ax.tick_params(colors=QUIET, labelsize=fs - (1 if compact else 0), length=0, pad=4 if compact else 6)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: fmt(v)))
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(4 if compact else 5))

    xs_all = [d for l in lines for d, _ in l.points]
    first_x, last_x = min(xs_all), max(xs_all)
    span_days = (last_x - first_x).days
    if span_days > 1500:
        ax.xaxis.set_major_locator(mdates.YearLocator(2 if span_days > 3000 else 1))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    else:
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 7) if span_days > 500 or (compact and span_days > 300)
                                                        else (1, 4, 7, 10)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))

    # Data limits first, so bands only shade what's on screen.
    ys = [v for l in lines for _, v in l.points]
    if range_band:
        ys += [v for d, v in range_band[0] if d >= start] + [v for d, v in range_band[1] if d >= start]
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.12 or abs(hi) * 0.05 or 1
    y0, y1 = lo - pad, hi + pad
    if zero_line and y0 > 0:
        y0 = min(y0, -pad * 0.5)
    ax.set_ylim(y0, y1)

    offscreen = []
    for band in bands or []:
        b_lo = y0 if band.lo is None else band.lo
        b_hi = y1 if band.hi is None else band.hi
        if b_hi <= y0 or b_lo >= y1:
            # The zone is off the chart: say how far away it is instead.
            edge = band.lo if band.lo is not None and band.lo >= y1 else band.hi
            if edge is not None:
                offscreen.append((band, edge))
            continue
        ax.axhspan(max(b_lo, y0), min(b_hi, y1), color=BAND_FILL[band.tone], zorder=0, linewidth=0)
        short = band.label.split(":")[0] if compact else (band.label if len(band.label) <= 34 else band.label.split(" (")[0])
        ax.text(0.995, (max(b_lo, y0) + min(b_hi, y1)) / 2, short, transform=ax.get_yaxis_transform(),
                ha="right", va="center", fontsize=fs - 1.5, color=BAND_TEXT[band.tone], fontweight="bold", zorder=1)
    if offscreen:
        nearest = min(offscreen, key=lambda t: min(abs(t[1] - y0), abs(t[1] - y1)))
        where = "above" if nearest[1] >= y1 else "below"
        # Bottom-left, boxed, so it never collides with a band label on the right.
        word = nearest[0].label.split(":")[0]
        note = (f"{word} at {fmt(nearest[1])} ({where})" if compact
                else f"{word} level starts at {fmt(nearest[1])} ({where} this chart)")
        ax.text(0.01, 0.03, note,
                transform=ax.transAxes, ha="left", va="bottom", fontsize=fs - 1.5, color=BAND_TEXT[nearest[0].tone],
                fontweight="bold", zorder=6, bbox=dict(facecolor=SURFACE, edgecolor=BAND_TEXT[nearest[0].tone],
                                                      boxstyle="round,pad=0.3", linewidth=0.8))

    if range_band:
        lo_pts, hi_pts, label = range_band
        hi_map = dict(hi_pts)
        xs = [d for d, _ in lo_pts if d in hi_map and d >= start]
        if xs:
            lo_map = dict(lo_pts)
            band_x = xs + [max(xs[-1], last_x) + timedelta(days=1)]          # carry the band to the chart's end
            band_lo = [lo_map[d] for d in xs] + [lo_map[xs[-1]]]
            band_hi = [hi_map[d] for d in xs] + [hi_map[xs[-1]]]
            ax.fill_between(band_x, band_lo, band_hi, step="post", color="#e7e9ec", zorder=1, linewidth=0)
            ax.text(xs[len(xs) // 12], hi_map[xs[len(xs) // 12]], f" {label}", fontsize=fs - 1.5, color=QUIET,
                    va="bottom", zorder=2)

    if zero_line:
        ax.axhline(0, color=INK, linewidth=1.1, zorder=2)
    if ref_line:
        ax.axhline(ref_line[0], color=QUIET, linewidth=1.1, linestyle=(0, (4, 3)), zorder=2)
        if ref_line[1]:
            ax.text(0.01, ref_line[0], f"{ref_line[1]} ", transform=ax.get_yaxis_transform(), color=QUIET,
                    fontsize=9, va="bottom", ha="left", bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1.0), zorder=5)

    ends = []
    for i, l in enumerate(lines):
        xs = [d for d, _ in l.points]
        vals = [v for _, v in l.points]
        color = SERIES_COLORS[i]
        if step:
            ax.step(xs, vals, where="post", color=color, linewidth=2.0 if compact else 2.6, zorder=3)
        else:
            ax.plot(xs, vals, color=color, linewidth=1.8 if compact else 2.4, zorder=3, solid_capstyle="round")
        ax.scatter([xs[-1]], [vals[-1]], s=26 if compact else 42, color=color, edgecolors=SURFACE, linewidths=1.4, zorder=4)
        ends.append((vals[-1], l, color))

    # End labels: "Name 3.81%", nudged apart so they never overlap.
    min_gap = (y1 - y0) * 0.09
    placed: list[float] = []
    label_x = last_x + timedelta(days=max(4, span_days // 70))
    for y, l, color in sorted(ends, key=lambda t: t[0]):
        y_lab = y
        for p in placed:
            if abs(y_lab - p) < min_gap:
                y_lab = p + min_gap
        placed.append(y_lab)
        name = f"{l.label}  " if len(lines) > 1 else ""
        ax.annotate(f"{name}{fmt(y)}", xy=(last_x, y), xytext=(label_x, y_lab), color=INK, fontsize=fs,
                    fontweight="bold", va="center", annotation_clip=False,
                    arrowprops=dict(arrowstyle="-", color=color, lw=1.2) if abs(y_lab - y) > min_gap * 0.3 else None)
    longest = max(len(l.label) for l in lines) if len(lines) > 1 else 0
    room = (0.2 + 0.02 * longest) if compact else (0.16 + 0.012 * longest)   # space for end labels
    ax.set_xlim(first_x, last_x + timedelta(days=int(span_days * room)))

    fig.canvas.draw()
    # Always label the latest data point's date at the right end, so the chart visibly runs
    # to the current date (regular ticks alone can make it look like data stops months ago).
    cutoff = mdates.date2num(last_x)
    gap = span_days * (0.16 if compact else 0.1)
    ticks = [t for t in ax.get_xticks() if t <= cutoff - gap]
    fmt_regular = ax.xaxis.get_major_formatter()
    labels = [fmt_regular(t) for t in ticks]
    ticks.append(cutoff)
    # Monthly/quarterly data is dated the 1st of its period, so show the month, not a day.
    pts = sorted(lines[0].points)
    coarse = len(pts) > 2 and (pts[-1][0] - pts[-3][0]).days > 40
    labels.append(last_x.strftime("%b '%y") if span_days > 1500 or coarse else f"{last_x:%b} {last_x.day}")
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels)
    ax.get_xticklabels()[-1].set_fontweight("bold")
    fig.tight_layout(pad=0.4 if compact else 0.7)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()
