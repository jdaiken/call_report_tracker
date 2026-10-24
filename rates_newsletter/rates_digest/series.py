"""The essential rates and market series, all from FRED.

`kind` controls how changes are described:
  rate      -> a percent; changes shown in percentage points
  level     -> a dollar amount or index; changes shown as percent change
  level_abs -> a dollar amount that can sit near zero; changes shown in dollars
  yoy       -> a price index; shown as its year-over-year percent change
  points    -> an index quoted in points (the VIX)

`what_it_is` and `risk` are the plain-English notes shown in the newsletter;
the zones ("ranges to know") live in newsletter.py next to the analysis.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Series:
    id: str
    name: str
    kind: str              # rate | level | level_abs | yoy | points
    unit: str              # "%", "$B", "" (index)
    frequency: str         # daily | weekly | monthly | quarterly
    what_it_is: str
    risk: str
    notable_month_move: float = 0.25
    # FRED reports some Fed balance-sheet items in millions; mark them so the
    # numbers are right even when FRED's units text isn't available.
    millions: bool = False

    @property
    def why_it_matters(self) -> str:   # name used by analysis.Reading
        return self.risk


SERIES: list[Series] = [
    # --- the Fed and overnight funding
    Series("DFEDTARU", "Fed funds target (top of range)", "rate", "%", "daily",
           "The top of the range the Fed sets for overnight lending between banks. Changes only at Fed meetings.",
           "Every deposit rate keys off this. Cuts lower what banks pay depositors, including ICS and CDARS rates; "
           "hikes raise funding costs across IntraFi's network.", 0.01),
    Series("DFEDTARL", "Fed funds target (bottom of range)", "rate", "%", "daily",
           "The bottom of the Fed's target range.", "", 0.01),
    Series("EFFR", "Effective fed funds rate", "rate", "%", "daily",
           "What banks actually paid each other for overnight loans, published daily by the New York Fed.",
           "If it drifts toward or above the top of the Fed's range, overnight cash is getting scarce.", 0.10),
    Series("IORB", "Interest on reserve balances", "rate", "%", "daily",
           "What the Fed pays banks on cash held at the Fed: roughly the floor on what a bank will pay for deposits.", "", 0.10),
    Series("SOFR", "SOFR", "rate", "%", "daily",
           "The overnight cost of borrowing cash against Treasuries; the main benchmark for floating-rate loans and funding.",
           "A jump in SOFR is the earliest sign of a cash squeeze in funding markets.", 0.10),
    # --- Treasuries
    Series("DGS3MO", "3-month Treasury bill", "rate", "%", "daily",
           "The yield on 3-month U.S. government debt: the safe alternative to a bank deposit.",
           "Corporate treasurers compare bank rates directly with this. When bills pay much more, large balances leave banks.", 0.15),
    Series("DGS2", "2-year Treasury", "rate", "%", "daily",
           "The yield on 2-year government debt; the market's best guess at where the Fed is heading.",
           "Well below fed funds means markets expect cuts; above means they expect rates to stay high or rise.", 0.20),
    Series("DGS10", "10-year Treasury", "rate", "%", "daily",
           "The yield on 10-year government debt; drives mortgage rates and the value of bonds banks hold.",
           "A sharp rise causes paper losses on bank bond portfolios (what sank SVB in 2023) and slows lending.", 0.20),
    # --- bank deposit pricing
    Series("MMNDR", "Average bank money market rate", "rate", "%", "monthly",
           "The FDIC's national average rate on bank money market deposit accounts, updated monthly.",
           "The closest bank product to ICS. The further it lags T-bills, the stronger the pull of cash out of banks.", 0.03),
    Series("NDR12MCD", "Average bank 12-month CD rate", "rate", "%", "monthly",
           "The FDIC's national average rate on 12-month CDs, updated monthly.",
           "A benchmark for CDARS-style term deposits; compare with the 1-year Treasury and with what member banks offer.", 0.05),
    # --- deposits
    Series("DPSSCBW027SBOG", "Deposits at small banks", "level", "$B", "weekly",
           "Deposits at all U.S. banks outside the 25 largest (Fed H.8, seasonally adjusted). Most of IntraFi's network.",
           "Falling deposits force small banks to pay up, borrow, or shrink lending.", 1.0),
    Series("DPSLCBW027SBOG", "Deposits at large banks", "level", "$B", "weekly",
           "Deposits at the 25 largest U.S. banks (Fed H.8, seasonally adjusted).",
           "Large banks gaining while small banks lose is the flight-to-size pattern of 2023, the risk IntraFi's products address.", 1.0),
    Series("WRMFNS", "Retail money market fund assets", "level", "$B", "weekly",
           "Money individuals hold in retail money market funds.",
           "Fast growth means savers are moving cash out of bank deposits and into funds.", 2.0),
    # --- funding stress
    Series("H8B3094NSMA", "Borrowings at small banks", "level", "$B", "weekly",
           "Money small banks have borrowed, mostly FHLB advances (Fed H.8). The closest public read on FHLB demand.",
           "Rising borrowing means deposits aren't covering small banks' needs. That same need can be met with one-way placements.",
           3.0, millions=True),
    Series("WLCFLPCL", "Fed discount window borrowing", "level_abs", "$B", "weekly",
           "Emergency loans banks took from the Federal Reserve's discount window (primary credit), each Wednesday.",
           "Banks borrow here only when they need cash fast. It spiked to a record in March 2023, before markets fully saw the stress.",
           5.0, millions=True),
    # --- credit
    Series("BAMLH0A0HYM2", "High-yield credit spread", "rate", "%", "daily",
           "Extra yield investors demand to hold riskier 'junk' corporate bonds instead of Treasuries.",
           "Widening spreads mean investors expect more defaults; they usually widen before bank loan losses show up.", 0.30),
    Series("DRCRELEXFACBS", "Commercial real estate loans past due", "rate", "%", "quarterly",
           "Share of bank commercial real estate loans 30+ days past due, reported quarterly.",
           "CRE is the biggest credit risk for community banks. Rising delinquencies lead losses, capital strain and bank failures.", 0.15),
    # --- stocks and volatility
    Series("SP500", "S&P 500", "level", "", "daily",
           "Stock index of 500 large U.S. companies; the broadest read on the market.",
           "Big drops tighten financial conditions and push depositors toward fully insured options.", 5.0),
    Series("DJIA", "Dow Jones Industrial Average", "level", "", "daily",
           "Stock index of 30 large, well-known U.S. companies.", "Same signal as the S&P 500, the one most people quote.", 5.0),
    Series("NASDAQCOM", "Nasdaq Composite", "level", "", "daily",
           "Index of all Nasdaq stocks, heavy in technology.", "Swings first and hardest when risk appetite changes.", 6.0),
    Series("VIXCLS", "VIX (fear gauge)", "points", "", "daily",
           "Expected stock market volatility over the next month, priced from S&P 500 options.",
           "Spikes come with flights to safety, when depositors look hardest at whether their money is insured.", 4.0),
    # --- economy
    Series("CPIAUCSL", "Inflation (CPI, past 12 months)", "yoy", "%", "monthly",
           "How much consumer prices rose over the past year.",
           "Inflation above the Fed's 2% goal keeps rates, and deposit costs, higher for longer.", 0.20),
    Series("UNRATE", "Unemployment rate", "rate", "%", "monthly",
           "Share of people looking for work who can't find it.",
           "A rising rate usually brings Fed cuts and, later, loan losses at banks.", 0.20),
]

SERIES_BY_ID = {s.id: s for s in SERIES}


@dataclass(frozen=True)
class Spread:
    key: str
    name: str
    a: str          # spread = a - b
    b: str
    what_it_is: str
    reading: str    # the risk note
    notable_month_move: float = 0.15
    max_b_age_days: int = 4   # b may be published less often (monthly FDIC rates vs daily bills)


SPREADS: list[Spread] = [
    Spread("sofr_vs_iorb", "SOFR minus interest on reserves", "SOFR", "IORB",
           "Whether overnight secured funding costs more than the Fed pays banks on reserves.",
           "SOFR running above IORB means cash is getting scarce; in September 2019 this gap blew out and the Fed had to step in.",
           0.05),
    Spread("bill_vs_mm", "T-bill vs. bank money market gap", "DGS3MO", "MMNDR",
           "How much more a depositor earns in 3-month T-bills than in the average bank money market account.",
           "A wide or widening gap pulls large balances out of banks; a narrowing gap helps deposit retention.",
           0.20, max_b_age_days=45),
    Spread("bill_vs_effr", "3-month T-bill minus fed funds", "DGS3MO", "EFFR",
           "How the 3-month bill compares with the overnight rate.",
           "Below zero usually means markets expect a Fed cut soon.", 0.15),
    Spread("curve_10y_2y", "Yield curve (10-year minus 2-year)", "DGS10", "DGS2",
           "The gap between long and short Treasury yields.",
           "Inverted (negative) curves squeeze bank margins, since banks borrow short and lend long, and have often come before recessions.",
           0.20),
]
