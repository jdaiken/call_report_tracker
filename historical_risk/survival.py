"""Kaplan-Meier survival curves (spec §5): what fraction of banks are still
under an active order after 1/2/4 years, by order type. No Cox regression --
spec §7's explicit scope boundary.

**Deviation from spec §6: Python (`lifelines`), not R (`survival`/`survminer`).**
The spec's build-approach section calls for R, but this build sandbox has no R
runtime at all (checked: neither `R` nor `Rscript` resolves, and this is a
Python/DuckDB project throughout -- see ffiec_monitor/). `lifelines` is the
standard, well-documented Python equivalent for exactly this use case (KM
estimator + right-censoring + Cox if ever needed later), so it's used here
instead rather than blocking the whole analysis on installing an R toolchain
this project doesn't otherwise use. Flag this to the user if R output
specifically is required; the underlying censoring/inputs are identical either
way (spec §5's "R packages" note is a means, not the actual requirement).

Inputs (spec §5): order effective_date (start), terminated_date (end, NULL =
still active = censored), order_type. All three already live in
enforcement_orders -- no ratio data needed for this piece.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb
import pandas as pd

try:
    from lifelines import KaplanMeierFitter
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "lifelines is required for survival analysis (spec §5's R substitute -- "
        "see module docstring). pip install lifelines (already in requirements.txt)."
    ) from e


def load_order_durations(conn: duckdb.DuckDBPyConnection, as_of: date | None = None) -> pd.DataFrame:
    """One row per order: duration_days, event_observed (1 = terminated/cured,
    0 = still active = right-censored as of `as_of`, default today), order_type.
    An order still open is censored, never dropped or treated as cured (spec
    §5's explicit handling instruction)."""
    as_of = as_of or date.today()
    df = conn.execute(
        """
        SELECT id_rssd, order_type, effective_date, terminated_date, status
        FROM enforcement_orders
        WHERE effective_date IS NOT NULL
        """
    ).df()

    df["end_date"] = df["terminated_date"].fillna(pd.Timestamp(as_of))
    df["duration_days"] = (pd.to_datetime(df["end_date"]) - pd.to_datetime(df["effective_date"])).dt.days
    df["event_observed"] = df["terminated_date"].notna().astype(int)
    return df[df["duration_days"] >= 0]


@dataclass(frozen=True)
class SurvivalSummary:
    order_type: str
    n_orders: int
    frac_active_at_1y: float
    frac_active_at_2y: float
    frac_active_at_4y: float


def fit_by_order_type(durations: pd.DataFrame) -> tuple[dict[str, KaplanMeierFitter], list[SurvivalSummary]]:
    """One KM fit per order_type. Returns the fitted models (for plotting) and a
    summary table of fraction-still-active at 1/2/4 years -- spec §5's headline
    output, "a genuinely useful, non-obvious finding a simple average would
    hide"."""
    fitters: dict[str, KaplanMeierFitter] = {}
    summaries: list[SurvivalSummary] = []

    for order_type, group in durations.groupby("order_type"):
        kmf = KaplanMeierFitter(label=order_type)
        kmf.fit(group["duration_days"], event_observed=group["event_observed"])
        fitters[order_type] = kmf

        def frac_active_at(days: int) -> float:
            return float(kmf.survival_function_at_times(days).iloc[0])

        summaries.append(SurvivalSummary(
            order_type=order_type,
            n_orders=len(group),
            frac_active_at_1y=frac_active_at(365),
            frac_active_at_2y=frac_active_at(730),
            frac_active_at_4y=frac_active_at(1460),
        ))

    return fitters, summaries


def plot_survival_curves(fitters: dict[str, "KaplanMeierFitter"], out_path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 5))
    for order_type, kmf in fitters.items():
        kmf.plot_survival_function(ax=ax)
    ax.set_xlabel("Days under order")
    ax.set_ylabel("Fraction still under an active order")
    ax.set_title("Enforcement order survival by type (Kaplan-Meier)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
