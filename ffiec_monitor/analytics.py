"""The "At a glance" analytics at the top of the daily email and PDF:

  - filing-season progress: how many banks have filed the current quarter, of how
    many, and days to the Call Report deadline (30 days after quarter end)
  - daily activity: new filings and refilings logged per day, last 21 days
  - today's new-filing flags by type (capital, earnings, liquidity, brokered/reciprocal)

Charts are PNGs (email clients don't run scripts): inline images in the email,
embedded in the PDF. Palette = the validated default from the dataviz method
(series 1 blue, series 2 orange), same as the rates monitor.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

BLUE, ORANGE = "#2a78d6", "#eb6834"
RED, AMBER = "#c0392b", "#e0a100"            # status: red flag / amber only
INK, MUTED, GRID, SURFACE = "#14181f", "#5b626c", "#e6e8eb", "#ffffff"
DEADLINE_DAYS = 30          # Call Report due 30 days after quarter end (35 for banks with multiple foreign offices)
DAYS_SHOWN = 21

FLAG_GROUPS = [             # (label, screen flag keys)
    ("Capital near the line", {"lev", "rbc"}),
    ("Losses or weak earnings", {"roaa", "nim"}),
    ("Liquidity and funding", {"ltd", "nncf", "pledged", "core"}),
    ("Heavy brokered/reciprocal", {"br", "combo"}),
    ("Charge-offs above peers", {"co"}),
]


@dataclass
class Analytics:
    run_date: Optional[date] = None
    period_end: Optional[date] = None
    filed: int = 0
    universe: int = 0
    deadline: Optional[date] = None
    days: list[tuple[date, int, int]] = field(default_factory=list)     # (day, new filings, refilings)
    error: str = ""

    @property
    def quarter(self) -> str:
        p = self.period_end
        return f"Q{(p.month - 1) // 3 + 1} {p.year}" if p else ""

    @property
    def days_left(self) -> Optional[int]:
        return (self.deadline - self.run_date).days if self.deadline and self.run_date else None

    @property
    def pct(self) -> float:
        return self.filed / self.universe * 100 if self.universe else 0.0


def latest_quarter_end(d: date) -> date:
    """Most recent calendar quarter end on or before d."""
    q_month = ((d.month - 1) // 3) * 3          # 0, 3, 6, 9
    if q_month == 0:
        return date(d.year - 1, 12, 31)
    return date(d.year, q_month, 30 if q_month in (6, 9) else 31)


def compute(conn, run_date: date) -> Analytics:
    """Reads only; never raises (a failure just drops the section)."""
    a = Analytics(run_date=run_date)
    try:
        a.period_end = latest_quarter_end(run_date)
        a.deadline = a.period_end + timedelta(days=DEADLINE_DAYS)
        a.filed = conn.execute("SELECT COUNT(DISTINCT id_rssd) FROM call_report_snapshots WHERE period_end = ?",
                               [a.period_end]).fetchone()[0] or 0
        a.universe = max(conn.execute("SELECT COUNT(DISTINCT id_rssd) FROM call_report_snapshots").fetchone()[0] or 0,
                         a.filed)
        start = run_date - timedelta(days=DAYS_SHOWN - 1)
        rows = conn.execute(
            "SELECT CAST(logged_at AS DATE) d, "
            "COUNT(DISTINCT CASE WHEN trigger_type NOT LIKE 'RESTATEMENT%' THEN id_rssd || CAST(period_end AS VARCHAR) END), "
            "COUNT(DISTINCT CASE WHEN trigger_type LIKE 'RESTATEMENT%' THEN id_rssd || CAST(period_end AS VARCHAR) END) "
            "FROM alert_log WHERE CAST(logged_at AS DATE) >= ? GROUP BY 1", [start]).fetchall()
        by_day = {d: (n, r) for d, n, r in rows}
        a.days = [(start + timedelta(days=i), *by_day.get(start + timedelta(days=i), (0, 0))) for i in range(DAYS_SHOWN)]
    except Exception as e:                       # analytics must never block the email
        a.error = str(e)
    return a


def flag_counts(rows) -> list[tuple[str, int, int]]:
    """(group, banks with a red flag in it, banks with only amber in it) over new-filing rows."""
    out = []
    for label, keys in FLAG_GROUPS:
        red = amber = 0
        for r in rows:
            hits = [f for f in r.flags if f.key in keys]
            if any(f.level == "concern" for f in hits):
                red += 1
            elif hits:
                amber += 1
        out.append((label, red, amber))
    return out


# ------------------------------------------------------------------ charts
def _fig(w, h):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                         "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED})
    fig, ax = plt.subplots(figsize=(w, h), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    return plt, fig, ax


def _png(plt, fig) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout(pad=0.6)
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()


def activity_chart(a: Analytics) -> Optional[bytes]:
    """Stacked daily bars: new filings (blue) and refilings (orange), with a 2px surface gap."""
    if not a.days or not any(n or r for _, n, r in a.days):
        return None
    plt, fig, ax = _fig(4.6, 2.5)
    xs = list(range(len(a.days)))
    new = [n for _, n, _ in a.days]
    ref = [r for _, _, r in a.days]
    ax.bar(xs, new, width=0.72, color=BLUE, label="New filings", edgecolor=SURFACE, linewidth=1.0, zorder=3)
    ax.bar(xs, ref, width=0.72, bottom=new, color=ORANGE, label="Refilings", edgecolor=SURFACE, linewidth=1.0, zorder=3)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.tick_params(axis="both", length=0, labelsize=8.5)
    ticks = [i for i in xs if i % 5 == (len(xs) - 1) % 5]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{a.days[i][0]:%b} {a.days[i][0].day}" for i in ticks])
    last_total = new[-1] + ref[-1]
    ax.annotate(f"{last_total:,}", (xs[-1], last_total), xytext=(0, 3), textcoords="offset points",
                ha="center", va="bottom", fontsize=8.5, color=INK, fontweight="bold")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), frameon=False, fontsize=8.5, ncol=2, handlelength=1,
              handleheight=0.8, borderaxespad=0.2)
    top = max(n + r for n, r in zip(new, ref))
    ax.set_ylim(0, top * 1.15)
    return _png(plt, fig)


def flags_chart(counts: list[tuple[str, int, int]]) -> Optional[bytes]:
    """Horizontal bars per flag type: banks with a red flag (solid) and amber-only (light)."""
    if not counts or not any(r or w for _, r, w in counts):
        return None
    plt, fig, ax = _fig(4.6, 2.5)
    labels = [c[0] for c in counts][::-1]
    red = [c[1] for c in counts][::-1]
    amber = [c[2] for c in counts][::-1]
    ys = list(range(len(labels)))
    # Status colors (matching the red/amber dots in the table), always with a legend label.
    ax.barh(ys, red, height=0.6, color=RED, label="Red flag", edgecolor=SURFACE, linewidth=1.0, zorder=3)
    ax.barh(ys, amber, height=0.6, left=red, color=AMBER, label="Amber only", edgecolor=SURFACE, linewidth=1.0, zorder=3)
    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=8.5, color=INK)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.tick_params(axis="both", length=0, labelsize=8.5)
    ax.spines["bottom"].set_visible(False)
    top = max(r + w for r, w in zip(red, amber))
    for y, r, w in zip(ys, red, amber):
        if r + w:
            ax.text(r + w + top * 0.02, y, f"{r}" + (f" + {w}" if w else ""), va="center", fontsize=8.5, color=INK)
    ax.set_xlim(0, top * 1.3)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), frameon=False, fontsize=8.5, ncol=2, handlelength=1,
              handleheight=0.8, borderaxespad=0.2)
    return _png(plt, fig)
