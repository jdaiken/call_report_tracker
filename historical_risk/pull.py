"""Bulk UBPR ratio pull from FFIEC's public CDR bulk-download backend (spec §1).

Uses `ffiec_data_collector` (github.com/call-report/data-collector, installed
from PyPI as the `ffiec-data-collector` package) -- talks directly to
cdr.ffiec.gov's ASP.NET WebForms bulk-download backend over HTTP. No FFIEC PWS
account or JWT token needed (spec §1); this is a *different* auth boundary than
ffiec_monitor's rate-limited per-institution REST API.

Product: UBPR_RATIO_FOUR ("PerformanceReportingSeriesFourPeriods"). Range:
2012 Q1 through whatever the latest available period actually is at run time --
`latest_available_period()` reads that off the live period-selector rather than
a hardcoded date, per spec §1.

**Not runnable in this build sandbox**: this environment's network policy blocks
cdr.ffiec.gov (and banks.data.fdic.gov, used by survivorship.py) outbound --
confirmed via a live connection attempt during development, not assumed. Run
this from an environment with real internet access (spec §9 already anticipates
the script moving to a different machine).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator

from ffiec_data_collector import (
    DownloadResult,
    FFIECDownloader,
    FileFormat,
    Product,
    ReportingPeriod,
)

logger = logging.getLogger(__name__)

EARLIEST_PERIOD = date(2012, 1, 1)

# Being a reasonable citizen of a public government server -- spec §1: no need
# to parallelize aggressively, sequential pulls with a small pause are fine.
SECONDS_BETWEEN_PULLS = 2.0


def _period_date(period: ReportingPeriod) -> date:
    return period.date.date()


def available_periods_from(downloader: FFIECDownloader, earliest: date = EARLIEST_PERIOD) -> list[ReportingPeriod]:
    """All UBPR_RATIO_FOUR periods available at run time, oldest first, clamped to
    `earliest` -- this is what makes the pull "whatever the latest filed quarter
    is" rather than a hardcoded end date (spec §1)."""
    periods = downloader.select_product(Product.UBPR_RATIO_FOUR)
    in_range = [p for p in periods if _period_date(p) >= earliest]
    return sorted(in_range, key=_period_date)


def latest_available_period(downloader: FFIECDownloader) -> ReportingPeriod:
    return downloader.get_latest_period(Product.UBPR_RATIO_FOUR)


@dataclass(frozen=True)
class SanityCheckResult:
    period: ReportingPeriod
    size_bytes: int
    file_path: Path


def sanity_check_one_quarter(downloader: FFIECDownloader, download_dir: Path) -> SanityCheckResult:
    """Spec §1: confirm UBPR_RATIO_FOUR is meaningfully smaller than the ~500MB/
    quarter full Call Report product before looping every quarter since 2012 --
    call this once and eyeball the result before calling pull_all_quarters()."""
    latest = latest_available_period(downloader)
    result = downloader.download(Product.UBPR_RATIO_FOUR, latest, FileFormat.TSV, save_to_disk=True)
    assert isinstance(result, DownloadResult)
    if not result.success or result.file_path is None:
        raise RuntimeError(f"Sanity-check pull failed: {result.error_message}")

    size_mb = result.size_bytes / 1_000_000 if result.size_bytes else 0
    logger.info("Sanity check: %s -> %.1f MB (%s)", latest, size_mb, result.file_path)
    if result.size_bytes and result.size_bytes > 500_000_000:
        logger.warning(
            "UBPR_RATIO_FOUR file is %.1f MB -- as large as the full Call Report product "
            "spec §1 expected it to be smaller than. Double-check the product/format "
            "selection before looping all quarters.",
            size_mb,
        )
    return SanityCheckResult(latest, result.size_bytes or 0, result.file_path)


def pull_all_quarters(
    downloader: FFIECDownloader,
    download_dir: Path,
    earliest: date = EARLIEST_PERIOD,
    file_format: FileFormat = FileFormat.TSV,
) -> Iterator[DownloadResult]:
    """Pull one file per available quarter from `earliest` through the latest
    filed period, sequentially (spec §1). Each UBPR_RATIO_FOUR file covers that
    quarter plus its 3 priors, so consecutive quarterly pulls overlap on purpose
    -- cleanup.py's (id_rssd, period_end) primary key makes that idempotent
    rather than something this loop needs to dedupe itself."""
    periods = available_periods_from(downloader, earliest)
    logger.info("Pulling %d quarterly periods from %s through %s", len(periods), periods[0], periods[-1])

    for i, period in enumerate(periods):
        result = downloader.download(Product.UBPR_RATIO_FOUR, period, file_format, save_to_disk=True)
        assert isinstance(result, DownloadResult)
        if not result.success:
            logger.error("Pull failed for %s: %s", period, result.error_message)
        else:
            logger.info(
                "[%d/%d] %s -> %s (%.1f MB)",
                i + 1, len(periods), period, result.file_path,
                (result.size_bytes or 0) / 1_000_000,
            )
        yield result

        if i < len(periods) - 1:
            time.sleep(SECONDS_BETWEEN_PULLS)
