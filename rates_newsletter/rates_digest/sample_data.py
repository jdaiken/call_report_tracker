"""Made-up data for previewing the email layout and running tests offline.

None of these numbers are real. `run.py --sample` stamps every preview with a
"SAMPLE DATA" banner so a preview can't be mistaken for a live digest.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from .fetch import Headline, Observations
from .series import SERIES, SPREADS

# Rough starting level and daily wobble for each series (fictional).
_PROFILE = {
    "DFEDTARU": (4.25, 0), "DFEDTARL": (4.00, 0), "EFFR": (4.08, 0.005), "IORB": (4.15, 0),
    "SOFR": (4.10, 0.01), "DPRIME": (7.25, 0),
    "SNDR": (0.40, 0.003), "MMNDR": (0.62, 0.004), "NDR6MCD": (1.50, 0.006), "NDR12MCD": (1.70, 0.006),
    "MMNRC": (1.37, 0.004), "NRC12MCD": (5.00, 0.01),
    "DGS1MO": (4.05, 0.02), "DGS3MO": (3.95, 0.02), "DGS6MO": (3.80, 0.02), "DGS1": (3.65, 0.025),
    "WRMFNS": (2900.0, 6.0), "DPSACBW027SBOG": (18500.0, 25.0), "DPSLCBW027SBOG": (10400.0, 20.0),
    "DPSSCBW027SBOG": (5650.0, 10.0), "LTDSCBW027SBOG": (1150.0, 4.0), "WRESBAL": (3200.0, 30.0),
    "RRPONTSYD": (40.0, 8.0), "DGS2": (3.55, 0.04), "DGS10": (4.15, 0.04), "MORTGAGE30US": (6.30, 0.05),
    "BAMLH0A0HYM2": (3.10, 0.03), "BAMLC0A0CM": (0.85, 0.01), "VIXCLS": (17.0, 0.8), "SP500": (6500.0, 35.0), "DJIA": (45000.0, 220.0), "NASDAQCOM": (21500.0, 160.0),
    "DGS5": (3.75, 0.04), "DGS30": (4.70, 0.04),
    "DCOILWTICO": (68.0, 1.0), "DTWEXBGS": (121.0, 0.25), "CPIAUCSL": (320.0, 0.0), "UNRATE": (4.3, 0.05),
    "TOTLL": (12900.0, 15.0),
}
_FREQ = {s.id: s.frequency for s in SERIES}


def _dates(today: date, freq: str, days: int) -> list[date]:
    d0 = today - timedelta(days=days)
    out, d = [], d0
    while d < today:
        if freq == "daily" and d.weekday() < 5:
            out.append(d)
        elif freq == "weekly" and d.weekday() == 2:
            out.append(d)
        elif freq == "monthly" and d.day == 1:
            out.append(d)
        d += timedelta(days=1)
    return out


def sample_observations(today: date, days: int = 800, seed: int = 7) -> dict[str, Observations]:
    rng = random.Random(seed)
    ids = sorted({s.id for s in SERIES} | {sp.a for sp in SPREADS} | {sp.b for sp in SPREADS})
    out = {}
    for sid in ids:
        start, wobble = _PROFILE.get(sid, (1.0, 0.01))
        freq = _FREQ.get(sid, "daily")
        pts, v = [], start
        dates = _dates(today, freq, days)
        for i, d in enumerate(dates):
            if sid == "CPIAUCSL":
                v = start * (1.028 ** (i / 12))          # ~2.8% a year
            elif wobble:
                v = max(0.0, v + rng.gauss(0, wobble) + 0.03 * (start - v))  # wanders, then drifts back
            pts.append((d, round(v, 4)))
        # A fictional quarter-point Fed cut five days ago, so the preview shows that path.
        if sid in ("DFEDTARU", "DFEDTARL", "IORB", "DPRIME", "EFFR", "SOFR"):
            cut_day = today - timedelta(days=5)
            pts = [(d, round(x - 0.25, 4) if d >= cut_day else x) for d, x in pts]
        out[sid] = Observations(sid, pts, units="Billions of U.S. Dollars" if sid in ("WRESBAL",) else None)
    return out


def sample_headlines(today: date) -> list[Headline]:
    return [
        Headline("Monetary policy", "SAMPLE: Federal Reserve issues FOMC statement", "https://www.federalreserve.gov/",
                 today - timedelta(days=5)),
        Headline("Bank regulation", "SAMPLE: Agencies request comment on proposed rule", "https://www.federalreserve.gov/",
                 today - timedelta(days=2)),
    ]
