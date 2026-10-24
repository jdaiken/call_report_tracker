"""Every rate and indicator the digest tracks, with plain-English explanations.

All data comes from FRED (Federal Reserve Bank of St. Louis), which republishes
the Fed's H.15 rates, the NY Fed's EFFR/SOFR, Treasury yields, the Fed's H.8
bank balance sheet data, and the market series below. One source keeps the
pipeline simple: if FRED is up, everything is up.

`kind` controls how changes are described:
  rate     -> a percent; changes shown in percentage points
  level    -> a dollar amount or index; changes shown as percent change
  yoy      -> a price index; shown as its year-over-year percent change
  level_abs-> a dollar amount that can sit near zero; changes shown in dollars
  points   -> an index measured in points where the point change is what
              people quote (the VIX)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Series:
    id: str
    name: str
    section: str
    kind: str              # rate | level | yoy | points
    unit: str              # display unit: "%", "$B", "$", "" (index)
    frequency: str         # daily | weekly | monthly | quarterly
    what_it_is: str
    why_it_matters: str
    # A 1-month move at least this big (pp for rates, % for levels) counts as
    # "notable" and can make the top-of-email summary.
    notable_month_move: float = 0.25
    tags: tuple[str, ...] = field(default_factory=tuple)
    # FRED reports some Fed balance-sheet items in millions; mark them so the
    # numbers are right even when FRED's units text isn't available.
    millions: bool = False


SECTIONS = [
    ("policy", "Fed policy rates",
     "The rates the Federal Reserve sets or steers. Everything else in banking keys off these."),
    ("bankrates", "What banks pay depositors (FDIC national rates)",
     "The FDIC's monthly average of what U.S. banks pay on deposits. Comparing these with Treasury yields shows how "
     "competitive bank deposits are right now."),
    ("cash", "Where large depositors can put cash instead",
     "IntraFi's products compete for the same corporate and public-fund cash as Treasury bills and money market funds. "
     "When these pay a lot more than bank deposits, money tends to leave banks."),
    ("deposits", "Bank deposits and funding",
     "How much money is sitting in U.S. banks and how tight funding is. This is the pool IntraFi's network moves."),
    ("spreads", "Spreads that matter for IntraFi",
     "The gap between two rates often says more than either rate alone."),
    ("stress", "Bank stress signals",
     "Early warnings that banks are short of cash or seeing credit problems. Usually quiet; they matter when they move."),
    ("markets", "General market trends",
     "The broader backdrop any banker should know: the yield curve, credit, stocks, inflation and jobs."),
]


SERIES: list[Series] = [
    # ---------------------------------------------------------------- policy
    Series(
        "DFEDTARU", "Fed funds target range (top)", "policy", "rate", "%", "daily",
        "The upper end of the range the Fed sets for overnight lending between banks. "
        "It only changes at Fed meetings.",
        "This is the anchor for almost every deposit rate. When it moves, banks reprice deposits, "
        "including the rates offered on ICS and CDARS.",
        notable_month_move=0.01, tags=("policy",),
    ),
    Series(
        "DFEDTARL", "Fed funds target range (bottom)", "policy", "rate", "%", "daily",
        "The lower end of the Fed's target range.",
        "Together with the top, it shows where the Fed wants short-term rates to be.",
        notable_month_move=0.01, tags=("policy",),
    ),
    Series(
        "EFFR", "Effective fed funds rate", "policy", "rate", "%", "daily",
        "The average rate banks actually charged each other for overnight loans yesterday, published by the New York Fed.",
        "Shows whether the Fed's target is actually holding in the market. It should sit inside the target range.",
        notable_month_move=0.10,
    ),
    Series(
        "IORB", "Interest on reserve balances", "policy", "rate", "%", "daily",
        "What the Fed pays banks on the cash they keep in their Fed accounts.",
        "A bank can earn this risk-free, so it is roughly the floor on what a bank will pay for new deposits. "
        "If a deposit costs more than IORB, the bank needs a use for the money.",
        notable_month_move=0.10,
    ),
    Series(
        "SOFR", "SOFR", "policy", "rate", "%", "daily",
        "The Secured Overnight Financing Rate: the cost of borrowing cash overnight with Treasuries as collateral. "
        "Published daily by the New York Fed.",
        "The main benchmark for floating-rate loans and funding. A jump in SOFR is an early sign of tight funding markets.",
        notable_month_move=0.10,
    ),
    Series(
        "DPRIME", "Bank prime loan rate", "policy", "rate", "%", "daily",
        "The rate most large banks charge their best business borrowers. It moves in step with the Fed, normally sitting 3 points above the top of the fed funds range.",
        "Sets the price of many small-business and home-equity loans, which drives how much funding member banks need.",
        notable_month_move=0.10,
    ),
    # ------------------------------------------------------------- bankrates
    Series(
        "SNDR", "National savings rate", "bankrates", "rate", "%", "monthly",
        "The FDIC's average rate U.S. banks pay on savings accounts, updated monthly.",
        "The baseline for what an ordinary depositor earns at a bank.",
        notable_month_move=0.03,
    ),
    Series(
        "MMNDR", "National money market deposit rate", "bankrates", "rate", "%", "monthly",
        "The FDIC's average rate on bank money market deposit accounts.",
        "The closest bank product to ICS. If it lags far behind T-bills, large balances have a reason to leave.",
        notable_month_move=0.03,
    ),
    Series(
        "NDR6MCD", "National 6-month CD rate", "bankrates", "rate", "%", "monthly",
        "The FDIC's average rate on 6-month CDs.",
        "A benchmark for CDARS-style term deposits.",
        notable_month_move=0.05,
    ),
    Series(
        "NDR12MCD", "National 12-month CD rate", "bankrates", "rate", "%", "monthly",
        "The FDIC's average rate on 12-month CDs.",
        "The most quoted CD benchmark.",
        notable_month_move=0.05,
    ),
    Series(
        "MMNRC", "Rate cap: money market", "bankrates", "rate", "%", "monthly",
        "The most a bank that is not well capitalized may pay on money market deposits under FDIC rules.",
        "Weaker banks can't outbid this, which limits how they compete for deposits and which placements they can take.",
        notable_month_move=0.05,
    ),
    Series(
        "NRC12MCD", "Rate cap: 12-month CD", "bankrates", "rate", "%", "monthly",
        "The most a bank that is not well capitalized may pay on a 12-month CD under FDIC rules.",
        "Same idea as above, for term deposits.",
        notable_month_move=0.05,
    ),
    # ------------------------------------------------------------------ cash
    Series(
        "DGS1MO", "1-month Treasury bill", "cash", "rate", "%", "daily",
        "The yield on U.S. government debt that matures in one month.",
        "The safest short-term alternative to a bank deposit. Corporate treasurers compare this directly to deposit rates.",
        notable_month_move=0.15,
    ),
    Series(
        "DGS3MO", "3-month Treasury bill", "cash", "rate", "%", "daily",
        "The yield on U.S. government debt that matures in three months.",
        "The most-watched short-term safe yield. It also reflects what the market expects the Fed to do in the next few months.",
        notable_month_move=0.15,
    ),
    Series(
        "DGS6MO", "6-month Treasury bill", "cash", "rate", "%", "daily",
        "The yield on government debt that matures in six months.",
        "Comparable to a 6-month CD, including CDARS terms. If this is well above bank CD rates, CD money can leak to Treasuries.",
        notable_month_move=0.15,
    ),
    Series(
        "DGS1", "1-year Treasury", "cash", "rate", "%", "daily",
        "The yield on government debt that matures in one year.",
        "The benchmark for 12-month CD pricing.",
        notable_month_move=0.15,
    ),
    Series(
        "WRMFNS", "Retail money market fund assets", "cash", "level", "$B", "weekly",
        "Total money individuals hold in retail money market funds.",
        "Steady growth here means savers are choosing funds over bank deposits. Big inflows usually happen when fund yields beat bank rates.",
        notable_month_move=2.0,
    ),
    # -------------------------------------------------------------- deposits
    Series(
        "DPSACBW027SBOG", "Deposits at all U.S. commercial banks", "deposits", "level", "$B", "weekly",
        "Total deposits across all commercial banks, from the Fed's weekly H.8 report (seasonally adjusted).",
        "The overall size of the pool IntraFi's network draws from. Shrinking deposits mean more competition for the money that is left.",
        notable_month_move=1.0,
    ),
    Series(
        "DPSLCBW027SBOG", "Deposits at large banks", "deposits", "level", "$B", "weekly",
        "Deposits at the 25 largest domestically chartered banks.",
        "If large banks gain while small banks lose, money is moving toward 'too big to fail' banks, as it did after SVB failed in 2023. "
        "That is exactly the flight IntraFi's products help smaller banks prevent.",
        notable_month_move=1.0,
    ),
    Series(
        "DPSSCBW027SBOG", "Deposits at small banks", "deposits", "level", "$B", "weekly",
        "Deposits at all domestically chartered banks outside the largest 25. This is most of IntraFi's network.",
        "The most direct read on the health of IntraFi's core customers' funding.",
        notable_month_move=1.0,
    ),
    Series(
        "LTDSCBW027SBOG", "Large time deposits at small banks", "deposits", "level", "$B", "weekly",
        "CDs of $100,000 or more held at small banks.",
        "Big CDs are the kind of money that needs extra FDIC coverage, so this is close to the market CDARS serves.",
        notable_month_move=2.0,
    ),
    Series(
        "WRESBAL", "Bank reserves at the Fed", "deposits", "level", "$B", "weekly",
        "Total cash banks keep in their accounts at the Federal Reserve.",
        "When reserves fall, banks have less spare cash and compete harder for deposits. Very low reserves can make funding markets jumpy.",
        notable_month_move=3.0,
    ),
    Series(
        "RRPONTSYD", "Fed reverse repo usage", "deposits", "level_abs", "$B", "daily",
        "Cash money market funds and others park overnight at the Fed's reverse repo facility.",
        "A cushion of idle cash in the system. When it is near zero, extra Treasury borrowing pulls money out of bank reserves instead.",
        notable_month_move=25.0,
    ),
    # ---------------------------------------------------------------- stress
    Series(
        "WLCFLPCL", "Fed discount window borrowing", "stress", "level_abs", "$B", "weekly",
        "Loans banks took from the Federal Reserve's discount window (primary credit), as of each Wednesday.",
        "Banks only borrow here when they need cash fast. A sudden jump was the first public sign of trouble in March 2023.",
        notable_month_move=5.0, millions=True,
    ),
    Series(
        "H8B3094NSMA", "Borrowings at small banks", "stress", "level", "$B", "weekly",
        "Money small banks have borrowed, mostly FHLB advances, from the Fed's weekly H.8 report (seasonally adjusted).",
        "Rising borrowings mean small banks need more funding than their deposits provide. "
        "It is the closest public read on FHLB demand, and that same need can be met with one-way placements.",
        notable_month_move=3.0, millions=True,
    ),
    Series(
        "WTREGEN", "Treasury's cash account at the Fed", "stress", "level_abs", "$B", "weekly",
        "Cash the U.S. Treasury keeps at the Federal Reserve (the Treasury General Account).",
        "When the Treasury builds up this account, the money comes out of the banking system, shrinking reserves and deposits.",
        notable_month_move=150.0, millions=True,
    ),
    Series(
        "DRCRELEXFACBS", "Commercial real estate loan delinquencies", "stress", "rate", "%", "quarterly",
        "Share of commercial real estate loans at U.S. banks that are 30 or more days past due. Reported quarterly.",
        "CRE is the biggest credit worry for community banks, which are most of IntraFi's network. "
        "Rising delinquencies come before losses and bank stress.",
        notable_month_move=0.15,
    ),
    # --------------------------------------------------------------- markets
    Series(
        "DGS2", "2-year Treasury", "markets", "rate", "%", "daily",
        "The yield on government debt that matures in two years.",
        "The best single read on where the market thinks the Fed is heading over the next two years.",
        notable_month_move=0.20,
    ),
    Series(
        "DGS5", "5-year Treasury", "markets", "rate", "%", "daily",
        "The yield on government debt that matures in five years.",
        "A benchmark for medium-term bank lending and for longer CDs and term funding.",
        notable_month_move=0.20,
    ),
    Series(
        "DGS10", "10-year Treasury", "markets", "rate", "%", "daily",
        "The yield on government debt that matures in ten years.",
        "Drives mortgage rates and the value of the bonds banks hold. A sharp rise means paper losses on bank bond portfolios, the problem that sank SVB.",
        notable_month_move=0.20,
    ),
    Series(
        "DGS30", "30-year Treasury", "markets", "rate", "%", "daily",
        "The yield on the government's longest bond.",
        "Reflects long-run inflation and government borrowing worries. A big jump pressures the value of long-term bank assets.",
        notable_month_move=0.20,
    ),
    Series(
        "MORTGAGE30US", "30-year mortgage rate", "markets", "rate", "%", "weekly",
        "Freddie Mac's weekly average rate on a 30-year fixed mortgage.",
        "A read on housing activity and on the loan demand member banks are seeing.",
        notable_month_move=0.20,
    ),
    Series(
        "BAMLH0A0HYM2", "High-yield credit spread", "markets", "rate", "%", "daily",
        "How much extra yield investors demand to hold riskier 'junk' corporate bonds instead of Treasuries.",
        "A widening spread means investors are getting nervous about defaults. It often rises before bank credit losses do.",
        notable_month_move=0.30,
    ),
    Series(
        "BAMLC0A0CM", "Investment-grade credit spread", "markets", "rate", "%", "daily",
        "The extra yield on high-quality corporate bonds over Treasuries.",
        "A calmer, broader read on credit conditions than the high-yield spread.",
        notable_month_move=0.15,
    ),
    Series(
        "VIXCLS", "VIX (stock market fear gauge)", "markets", "points", "", "daily",
        "Expected stock market volatility over the next month. Around 12 to 20 is calm; above 30 is stress.",
        "Spikes in the VIX often come with flights to safety, when depositors look hardest at whether their money is insured.",
        notable_month_move=4.0,
    ),
    Series(
        "DJIA", "Dow Jones Industrial Average", "markets", "level", "", "daily",
        "The stock index of 30 large, well-known U.S. companies. The oldest and most quoted market index.",
        "A headline read on market mood that bank customers and boards will have seen.",
        notable_month_move=5.0,
    ),
    Series(
        "NASDAQCOM", "Nasdaq Composite", "markets", "level", "", "daily",
        "The index of all stocks on the Nasdaq exchange, heavy in technology companies.",
        "Tech-driven swings show up here first. Sharp drops tend to come with a flight to safety.",
        notable_month_move=6.0,
    ),
    Series(
        "SP500", "S&P 500", "markets", "level", "", "daily",
        "The stock index of 500 large U.S. companies.",
        "General market mood. A big drop can trigger demand for fully insured deposit products.",
        notable_month_move=5.0,
    ),
    Series(
        "DCOILWTICO", "Oil (WTI crude)", "markets", "level", "$", "daily",
        "Price of a barrel of U.S. crude oil.",
        "Feeds into inflation, and therefore into how long the Fed keeps rates where they are.",
        notable_month_move=10.0,
    ),
    Series(
        "DTWEXBGS", "U.S. dollar index", "markets", "level", "", "daily",
        "The dollar's value against a basket of major trading partners' currencies.",
        "A stronger dollar tightens financial conditions and tends to cool inflation.",
        notable_month_move=2.0,
    ),
    Series(
        "CPIAUCSL", "Inflation (CPI, past year)", "markets", "yoy", "%", "monthly",
        "How much consumer prices rose over the past 12 months.",
        "The Fed's target is about 2%. Inflation above that keeps rates higher for longer, which keeps deposit costs high.",
        notable_month_move=0.20,
    ),
    Series(
        "UNRATE", "Unemployment rate", "markets", "rate", "%", "monthly",
        "The share of people looking for work who can't find it.",
        "Rising unemployment usually leads the Fed to cut rates and eventually shows up as loan losses at banks.",
        notable_month_move=0.20,
    ),
    Series(
        "TOTLL", "Loans at all commercial banks", "markets", "level", "$B", "weekly",
        "Total loans and leases on commercial bank books (Fed H.8, seasonally adjusted).",
        "Loan growth faster than deposit growth means banks need more funding, which is when they reach for IntraFi's one-way placements.",
        notable_month_move=1.0,
    ),
]

SERIES_BY_ID = {s.id: s for s in SERIES}


@dataclass(frozen=True)
class Spread:
    key: str
    name: str
    a: str          # series id
    b: str          # series id; spread = a - b
    what_it_is: str
    reading: str    # how to read positive vs negative, in plain English
    notable_month_move: float = 0.15
    # b can be published less often than a (monthly FDIC rates vs daily T-bills):
    # each a-date uses the latest b value at most this many days old.
    max_b_age_days: int = 4


SPREADS: list[Spread] = [
    Spread(
        "bill_vs_mm", "3-month T-bill minus bank money market rate", "DGS3MO", "MMNDR",
        "How much more a depositor earns in Treasuries than in the average bank money market account.",
        "A wide gap gives large depositors a reason to move cash out of banks. A narrowing gap is good news for deposit retention.",
        notable_month_move=0.20, max_b_age_days=45,
    ),
    Spread(
        "bill6_vs_cd6", "6-month T-bill minus 6-month bank CD", "DGS6MO", "NDR6MCD",
        "The same comparison for term money.",
        "When positive and wide, CD buyers can do better in Treasuries, which pressures banks to raise CD rates.",
        notable_month_move=0.20, max_b_age_days=45,
    ),
    Spread(
        "bill_vs_effr", "3-month T-bill minus fed funds", "DGS3MO", "EFFR",
        "How the 3-month bill compares with the overnight rate banks pay each other.",
        "Below zero usually means markets expect the Fed to cut rates soon. Above zero means they expect rates to stay put or rise.",
    ),
    Spread(
        "bill_vs_iorb", "3-month T-bill minus IORB", "DGS3MO", "IORB",
        "Whether a depositor can do better in Treasuries than a bank earns on its own spare cash.",
        "Above zero means Treasuries pay more than banks earn risk-free at the Fed. Banks then have to stretch to keep large deposits.",
    ),
    Spread(
        "sofr_vs_iorb", "SOFR minus IORB", "SOFR", "IORB",
        "Whether overnight secured funding costs more than the Fed pays on reserves.",
        "Near or below zero is normal. SOFR running above IORB is a sign cash is getting scarce in funding markets.",
        notable_month_move=0.05,
    ),
    Spread(
        "curve_10y_2y", "Yield curve: 10-year minus 2-year", "DGS10", "DGS2",
        "The classic yield-curve measure.",
        "Negative ('inverted') has historically come before recessions and squeezes bank margins, since banks borrow short and lend long. "
        "Positive and rising is healthier for bank earnings.",
        notable_month_move=0.20,
    ),
    Spread(
        "curve_10y_3m", "Yield curve: 10-year minus 3-month", "DGS10", "DGS3MO",
        "The Fed's own preferred yield-curve recession signal.",
        "Negative has preceded every U.S. recession since the 1960s, though with long and variable lags.",
        notable_month_move=0.20,
    ),
]
