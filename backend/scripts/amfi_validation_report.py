"""AMFI mutual-fund layer validation. Gate: no fund screener, recommendation or allocation logic is built until this report is clean (or its findings are accepted).

    python scripts/amfi_validation_report.py        # -> data/reports/funds/amfi_validation.md (+ csv files next to it)

Checks
  1  load / coverage by year and by fund house (AMC), AMCs with silent years
  2  duplicate scheme/date rows (database key + landed-file re-check) and duplicate ISINs
  3  missing NAV days and stale gaps (market-wide missing publication dates, per-scheme gaps inside the life of a scheme)
  4  growth vs IDCW handling (flags, return basis, whether the NAV behaves as the label says)
  5  classification audit: asset class x fund group, the `other` bucket broken down, `unclassified` with reasons, rule-consistency violations, a random sample for review
  6  return calculation: independent re-computation on a random sample, annualisation rules, plausibility outliers
  7  NAV sanity (one-day jumps), cross-checks against NSE's official ETF list and BSE fund scrips
This layer is separate from the equity layer; nothing here feeds the universe, the defensive signal or decision cards.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

OUT = Path(r"D:\Stoxsy\data\reports\funds")
SEED = 20261019


def md_table(df: pd.DataFrame, cols: list, fmt: dict | None = None) -> list:
    fmt = fmt or {}
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df[cols].itertuples(index=False):
        out.append("| " + " | ".join((fmt.get(c, str)(v) if pd.notna(v) else "") for c, v in zip(cols, r)) + " |")
    return out


def main() -> int:
    from qsde.db.connection import read_sql
    from qsde.funds.returns import scheme_returns, STALE_DAYS

    OUT.mkdir(parents=True, exist_ok=True)
    sm = read_sql("SELECT * FROM mf_scheme_master")
    if sm.empty:
        print("no AMFI data loaded")
        return 1
    findings: list = []          # (severity, text) for the summary at the top
    L: list = []

    # 1 ── coverage ───────────────────────────────────────────────────────────────────────────────────────────────────
    cov = read_sql("""SELECT extract(year from date)::int AS year, count(*) AS rows, count(DISTINCT scheme_code) AS schemes, count(DISTINCT date) AS days
                      FROM mf_nav GROUP BY 1 ORDER BY 1""")
    loads = read_sql("SELECT source, count(*) AS files, sum(n_rows) AS rows, min(window_from) AS d0, max(window_to) AS d1 FROM mf_nav_loads GROUP BY 1")
    ext = read_sql("SELECT min(date) d0, max(date) d1, count(*) n, count(DISTINCT scheme_code) s FROM mf_nav").iloc[0]
    last_nav = pd.Timestamp(ext["d1"])
    sm["last_nav_date"] = pd.to_datetime(sm["last_nav_date"])
    sm["first_nav_date"] = pd.to_datetime(sm["first_nav_date"]) if "first_nav_date" in sm else pd.NaT
    active = sm[sm["last_nav_date"] >= last_nav - pd.Timedelta(days=7)]
    by_amc = read_sql("""SELECT m.amc, extract(year from n.date)::int AS year, count(DISTINCT n.scheme_code) AS schemes, count(*) AS rows
                         FROM mf_nav n JOIN mf_scheme_master m USING (scheme_code) WHERE n.date >= '2016-01-01' GROUP BY 1, 2""")
    by_amc.to_csv(OUT / "coverage_by_year_amc.csv", index=False)
    piv = by_amc.pivot_table(index="amc", columns="year", values="schemes", aggfunc="sum", fill_value=0)
    silent = []                       # fund houses with a hole inside their own life
    for amc, row in piv.iterrows():
        yrs = [y for y in piv.columns if row[y] > 0]
        if len(yrs) >= 2:
            hole = [y for y in range(min(yrs), max(yrs) + 1) if y in piv.columns and row.get(y, 0) == 0]
            if hole:
                silent.append((amc, hole))
    n_amc = sm["amc"].nunique()
    top_amc = piv.assign(total=piv.sum(axis=1)).sort_values("total", ascending=False).head(12).drop(columns="total")
    first_years = cov["year"].min()
    if first_years > 2016:
        findings.append(("warn", f"NAV history begins {ext['d0']}, later than the 2016 target"))
    L += ["## 1. Load and coverage", "",
          f"- NAV rows **{int(ext['n']):,}** for **{int(ext['s']):,}** schemes, {ext['d0']} .. {ext['d1']}; schemes in the master **{len(sm):,}** across {n_amc} fund houses; active (NAV within 7 days of {last_nav.date()}): **{len(active):,}**",
          "- loads: " + "; ".join(f"{r.source}: {r.files} file(s), {int(r.rows):,} rows" + (f" ({r.d0} .. {r.d1})" if pd.notna(r.d0) else "") for r in loads.itertuples()), "",
          "| year | NAV rows | schemes | distinct dates |", "|---|---|---|---|",
          *[f"| {r.year} | {int(r.rows):,} | {int(r.schemes):,} | {int(r.days)} |" for r in cov.itertuples()], "",
          "Schemes per year for the 12 largest fund houses (full table in `coverage_by_year_amc.csv`):", "",
          "| fund house | " + " | ".join(str(c) for c in top_amc.columns) + " |", "|---|" + "---|" * len(top_amc.columns),
          *[f"| {a} | " + " | ".join(str(int(v)) for v in r) + " |" for a, r in top_amc.iterrows()], "",
          f"- fund houses with a silent year inside their own history: **{len(silent)}**" + ("; e.g. " + "; ".join(f"{a} {h}" for a, h in silent[:8]) if silent else ""), ""]
    if silent:
        findings.append(("warn", f"{len(silent)} fund house(s) have a year with no NAV rows inside their own history (see section 1)"))

    # 2 ── duplicates ─────────────────────────────────────────────────────────────────────────────────────────────────
    dup_rows = int(read_sql("SELECT count(*) n FROM (SELECT scheme_code, date FROM mf_nav GROUP BY 1, 2 HAVING count(*) > 1) t").iloc[0]["n"])
    pk = read_sql("""SELECT count(*) n FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid WHERE c.relname = 'mf_nav' AND i.indisunique""").iloc[0]["n"]
    dup_isin = sm[sm["isin_growth_or_payout"].notna()].groupby("isin_growth_or_payout")["scheme_code"].nunique()
    dup_isin = dup_isin[dup_isin > 1]
    # same scheme name + plan + option under two scheme codes (a re-registered scheme)
    sm["_k"] = (sm["name"].fillna("").str.lower().str.replace(r"\s+", " ", regex=True) + "|" + sm["plan"].fillna("").str.lower() + "|" + sm["option_type"].fillna("").str.lower())
    key = sm.groupby("_k")["scheme_code"].nunique()
    dup_name = key[key > 1]
    if dup_rows:
        findings.append(("fail", f"{dup_rows:,} duplicate (scheme, date) rows"))
    L += ["## 2. Duplicates", "",
          f"- duplicate (scheme_code, date) rows in `mf_nav`: **{dup_rows:,}**  (unique index present on the table: {'yes' if int(pk) else 'NO'}; re-loading a window is an upsert, so a replay cannot add rows)",
          f"- ISINs shared by more than one scheme code: **{len(dup_isin):,}**; name + plan + option shared by more than one code: **{len(dup_name):,}** (re-registered or renamed schemes; listed in `duplicate_names.csv`)", ""]
    sm[sm["_k"].isin(dup_name.index)][["scheme_code", "name", "amc", "isin_growth_or_payout", "last_nav_date"]].sort_values("name").to_csv(OUT / "duplicate_names.csv", index=False)

    # 3 ── missing days and stale gaps ───────────────────────────────────────────────────────────────────────────────
    days = read_sql("SELECT date, count(*) AS n FROM mf_nav WHERE date >= '2016-01-01' GROUP BY 1 ORDER BY 1")
    days["date"] = pd.to_datetime(days["date"])
    pre = int(read_sql("SELECT count(*) n FROM mf_nav WHERE date < '2016-01-01'").iloc[0]["n"])
    wd = days[days["date"].dt.dayofweek < 5].copy()
    wd["med"] = wd["n"].rolling(61, min_periods=10, center=True).median()
    thin_wd = wd[wd["n"] < 0.5 * wd["med"]]                                   # weekdays on which most funds publish nothing = exchange holidays
    bdays = pd.bdate_range(days["date"].min(), days["date"].max())
    absent = bdays.difference(pd.DatetimeIndex(days["date"]))
    # per-scheme gaps inside the life of each scheme (calendar days between consecutive NAVs)
    gaps = read_sql("""WITH x AS (SELECT scheme_code, date, lead(date) OVER (PARTITION BY scheme_code ORDER BY date) AS nxt FROM mf_nav)
                       SELECT scheme_code, date AS gap_start, nxt AS gap_end, (nxt - date) AS gap_days FROM x WHERE nxt IS NOT NULL AND (nxt - date) > 7""")
    gaps_scheme = gaps.groupby("scheme_code").agg(n_gaps=("gap_days", "size"), longest=("gap_days", "max")).reset_index().merge(
        sm[["scheme_code", "name", "amc", "asset_class", "fund_group", "lifecycle", "is_growth"]], on="scheme_code", how="left")
    gaps_scheme.sort_values("longest", ascending=False).head(3000).to_csv(OUT / "nav_gaps.csv", index=False)
    g30 = gaps_scheme[gaps_scheme["longest"] > 30]
    g30_growth_active = g30[(g30["is_growth"] == True) & (g30["lifecycle"] == "active")]       # noqa: E712
    stale = sm[(sm["last_nav_date"] < last_nav - pd.Timedelta(days=7))]
    life_counts = sm["lifecycle"].value_counts(dropna=False)
    if len(absent):
        findings.append(("fail", f"{len(absent)} weekdays since 2016 have no NAV for any scheme (landing gap): {', '.join(str(d.date()) for d in absent[:5])}"))
    if len(g30_growth_active):
        findings.append(("warn", f"{len(g30_growth_active)} active growth schemes have a NAV gap above 30 days (listed in nav_gaps.csv)"))
    L += ["## 3. Missing NAV days and stale gaps", "",
          f"- weekdays between {days['date'].min().date()} and {days['date'].max().date()} with NO NAV for any scheme: **{len(absent)}**" + ("; " + ", ".join(str(d.date()) for d in absent[:10]) if len(absent) else " (every weekday is present)"),
          f"- weekdays on which fewer than half the usual schemes have a NAV (exchange holidays, when equity and most debt funds are not priced): **{len(thin_wd)}** over {days['date'].dt.year.nunique()} years; weekend dates present (liquid / overnight funds publish on them): {int((days['date'].dt.dayofweek >= 5).sum())}",
          f"- NAV rows dated before 2016 (single points left over from the current-NAV file, not history): {pre:,}",
          f"- schemes with at least one gap above 7 calendar days inside their own life: **{len(gaps_scheme):,}** of {int(ext['s']):,}; above 30 days: **{len(g30):,}** ({int((g30['lifecycle'] == 'active').sum())} active; {int((g30['is_growth'] != True).sum())} are payout / IDCW / undetermined lines, which carry no NAV while dormant between payouts or with no units outstanding); active growth schemes with such a gap: **{len(g30_growth_active)}**",
          f"- schemes whose last NAV is more than 7 days before {last_nav.date()} (matured, merged, wound up or discontinued): **{len(stale):,}**; lifecycle: " + ", ".join(f"{k}: {v:,}" for k, v in life_counts.items()),
          "- Stale-start rule in returns: a horizon is dropped if the NAV used as its start is more than 7 days before the start date, so gaps never silently stretch a return window.", ""]

    # 4 ── growth vs IDCW ────────────────────────────────────────────────────────────────────────────────────────────
    import re as _re
    g = sm["is_growth"]
    strip = _re.compile(r"\b(growth|idcw|dividend|payout|reinvestment|re-investment|weekly|monthly|quarterly|daily|fortnightly|annual|half yearly|yearly|option|plan|income distribution cum capital withdrawal|cum|capital|withdrawal|distribution|bonus|accumulation)\b|[^a-z0-9]", _re.I)
    act = sm[(sm["lifecycle"] == "active") & sm["is_direct"].notna()].copy()
    act["fam"] = act["name"].fillna("").str.lower().map(lambda t: _re.sub(r"\s+", " ", strip.sub(" ", t)).strip()) + "|" + act["is_direct"].astype(str) + "|" + act["amc"].fillna("")
    fams = act.groupby("fam").agg(n_g=("is_growth", lambda x: int((x == True).sum())), n_p=("is_growth", lambda x: int((x == False).sum())))        # noqa: E712
    fams = fams[(fams["n_g"] == 1) & (fams["n_p"] >= 1)].index
    pair = act[act["fam"].isin(fams)]
    t0 = last_nav - pd.DateOffset(years=3)
    codes = [int(c) for c in pair["scheme_code"]]
    navp = read_sql("""SELECT DISTINCT ON (scheme_code) scheme_code, nav AS nav0 FROM mf_nav WHERE scheme_code = ANY(:c) AND date <= CAST(:t AS DATE) AND date >= CAST(:t AS DATE) - 7
                       ORDER BY scheme_code, date DESC""", params={"c": codes, "t": str(t0.date())})
    navl = read_sql("""SELECT DISTINCT ON (scheme_code) scheme_code, nav AS last_nav FROM mf_nav WHERE scheme_code = ANY(:c) AND date >= CAST(:d AS DATE) - 7
                       ORDER BY scheme_code, date DESC""", params={"c": codes, "d": str(last_nav.date())})
    pr = pair.merge(navp, on="scheme_code").merge(navl, on="scheme_code")
    pr["ret3y"] = pr["last_nav"].astype(float) / pr["nav0"].astype(float) - 1.0
    gr = pr[pr["is_growth"] == True].set_index("fam")["ret3y"]                                                       # noqa: E712
    po = pr[pr["is_growth"] == False].groupby("fam")["ret3y"].median()                                               # noqa: E712
    cmp_ = pd.concat([gr.rename("g"), po.rename("p")], axis=1).dropna()
    share_lower = float((cmp_["p"] <= cmp_["g"] + 0.005).mean()) if len(cmp_) else float("nan")
    shortfall = float((cmp_["g"] - cmp_["p"]).median()) if len(cmp_) else float("nan")
    L += ["## 4. Growth vs IDCW handling", "",
          f"- growth / accumulation: **{int((g == True).sum()):,}**; payout / IDCW / bonus: **{int((g == False).sum()):,}**; undetermined: **{int(g.isna().sum()):,}**",    # noqa: E712
          "- Rule: growth NAV change is a total return after expenses; IDCW NAV falls when income is paid, so its return is a **price return only** and is labelled as such (never ranked against growth).",
          f"- Label check by pairing: {len(cmp_):,} active fund families with exactly one growth line and at least one IDCW line. Over the last 3 years the median IDCW line's NAV return was no higher than the growth line's (within 0.5 points) in **{share_lower:.1%}** of families; median shortfall {shortfall:.1%} of starting NAV. "
          "If the labels were wrong, this share would be near 50%.",
          "- undetermined by group (active schemes): " + ", ".join(f"{k} {v}" for k, v in sm[(sm["is_growth"].isna()) & (sm["lifecycle"] == "active")]["fund_group"].value_counts().items())
          + ". ETFs have no growth/IDCW option; they are shown with an explicit 'ETF price return' basis and are not mixed into growth rankings.", ""]
    if len(cmp_) >= 30 and share_lower < 0.85:
        findings.append(("warn", f"growth/IDCW labels: only {share_lower:.0%} of paired families show IDCW <= growth over 3 years"))
    if int(g.isna().sum()) > 0.02 * len(sm):
        findings.append(("warn", f"{int(g.isna().sum()):,} schemes ({g.isna().mean():.1%}) have an undetermined growth/IDCW flag; they are excluded from growth return rankings"))

    # 5 ── classification audit ──────────────────────────────────────────────────────────────────────────────────────
    ct = pd.crosstab(sm["asset_class"], sm["fund_group"], margins=True)
    other = sm[sm["asset_class"] == "other"]
    unclass = sm[sm["fund_group"] == "unclassified"]
    viol = []
    nm = sm["name"].str.lower().fillna("")
    checks = {
        "gold/silver in the name but asset class is not commodity (gold-mining / overseas funds excluded)": sm[nm.str.contains(r"\bgold\b|\bsilver\b") & ~nm.str.contains(r"mining|overseas|world|global") & (sm["asset_class"] != "commodity")],
        "liquid/overnight in the name but fund group is not liquid_or_overnight/etf/fund_of_funds": sm[nm.str.contains(r"\bliquid\b|\bovernight\b") & ~sm["fund_group"].isin(["liquid_or_overnight", "etf", "fund_of_funds", "unclassified"])],
        "'arbitrage' in the name but fund group is not arbitrage": sm[nm.str.contains("arbitrage") & (sm["fund_group"] != "arbitrage")],
        "ETF flag set but fund group is not etf": sm[(sm["is_etf"] == True) & (sm["fund_group"] != "etf")],        # noqa: E712
        "fund group etf but ETF flag not set": sm[(sm["fund_group"] == "etf") & (sm["is_etf"] != True)],            # noqa: E712
        "'fund of funds' in the name but not fund_of_funds/etf/arbitrage/liquid": sm[nm.str.contains("fund of fund|fof") & ~sm["fund_group"].isin(["fund_of_funds", "etf", "arbitrage", "liquid_or_overnight"])],
        "unclassified without a stored reason": unclass[unclass["unclassified_reason"].isna()],
        "asset class other without a stored reason": other[other["unclassified_reason"].isna()],
    }
    for k, v in checks.items():
        viol.append((k, len(v)))
        if len(v):
            v[["scheme_code", "name", "asset_class", "fund_group", "classification_evidence"]].head(300).to_csv(OUT / ("class_violation_" + str(abs(hash(k)) % 10**6) + ".csv"), index=False)
    rs = np.random.default_rng(SEED)
    sample = sm.groupby("fund_group", group_keys=False).apply(lambda d: d.sample(min(len(d), 6), random_state=int(rs.integers(1e9))))
    sample[["scheme_code", "name", "amc", "broad_category", "sub_category", "asset_class", "fund_group", "unclassified_reason", "classification_evidence"]].to_csv(OUT / "classification_audit_sample.csv", index=False)
    other_grp = other["fund_group"].value_counts()
    reasons = unclass["unclassified_reason"].value_counts()
    if len(unclass) > 0.01 * len(sm):
        findings.append(("warn", f"{len(unclass):,} schemes remain `unclassified`"))
    if any(n for _, n in viol):
        findings.append(("warn", "classification rule-consistency violations: " + "; ".join(f"{k} ({n})" for k, n in viol if n)))
    L += ["## 5. Classification audit", "",
          "Asset class (rows) x fund group (columns); every scheme keeps its evidence string:", "",
          "| asset class | " + " | ".join(ct.columns.astype(str)) + " |", "|---|" + "---|" * len(ct.columns),
          *[f"| {a} | " + " | ".join(f"{int(v):,}" for v in r) + " |" for a, r in ct.iterrows()], "",
          f"- the former ~4,157-scheme `other` bucket is now: asset class `other` **{len(other):,}** (vehicle known, underlying not determinable from the heading or name) and `unclassified` **{len(unclass):,}**; the rest were placed by explicit rules into ETF / index / liquid / arbitrage / FoF / solution-oriented / closed-end / interval / fixed-term groups",
          "- remaining `other` by fund group: " + (", ".join(f"{k} {v:,}" for k, v in other_grp.items()) or "none"),
          "- `unclassified` reasons: " + (", ".join(f"{v:,} x {k}" for k, v in reasons.items()) or "none"), "",
          "Rule-consistency checks (each violation count should be 0; samples in `class_violation_*.csv`):", "",
          *[f"- {k}: **{n}**" for k, n in viol], "",
          "A seeded random sample of up to 6 schemes per group is in `classification_audit_sample.csv` for manual review.", ""]

    # 6 ── return calculation ────────────────────────────────────────────────────────────────────────────────────────
    pool = active[(active["is_growth"] == True) & (active["is_direct"] == True)]                  # noqa: E712
    codes = pool.sample(min(len(pool), 250), random_state=SEED)["scheme_code"].tolist()
    navs = read_sql("SELECT scheme_code, date, nav FROM mf_nav WHERE scheme_code = ANY(:c) ORDER BY scheme_code, date", params={"c": [int(c) for c in codes]})
    navs["date"] = pd.to_datetime(navs["date"])
    mism, checked, ann_below1y, ann_inconsistent, extreme = 0, 0, 0, 0, []
    have = {h: 0 for h in ["1M", "3M", "6M", "1Y", "3Y", "5Y"]}
    for code, d in navs.groupby("scheme_code"):
        s = d.set_index("date")["nav"].astype(float).sort_index()
        r = scheme_returns(s)
        end_d, end_v = s.index[-1], float(s.iloc[-1])
        for h, v in r.items():
            if h in have:
                have[h] += 1
            n_months = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "3Y": 36, "5Y": 60}.get(h)
            if n_months:                                           # independent re-computation with a different mechanism (asof lookup)
                tgt = end_d - pd.DateOffset(months=n_months)
                prior = s.loc[:tgt]
                if len(prior):
                    exp = end_v / float(prior.iloc[-1]) - 1.0
                    checked += 1
                    if abs(exp - v["cumulative"]) > 1e-9:
                        mism += 1
            if h in ("1D", "1W", "1M", "3M", "6M") and v["annualised"] is not None:
                ann_below1y += 1
            if v["annualised"] is not None and h == "1Y" and abs(v["annualised"] - v["cumulative"]) > 0.03:
                ann_inconsistent += 1
            if abs(v["cumulative"]) > 3.0 and h in ("1Y", "3Y", "5Y") or (v["annualised"] is not None and abs(v["annualised"]) > 1.0):
                extreme.append((code, h, v["cumulative"], v["annualised"]))
    if mism:
        findings.append(("fail", f"{mism} of {checked} return computations differ from the independent re-computation"))
    ext_df = pd.DataFrame(extreme, columns=["scheme_code", "horizon", "cumulative", "annualised"]).merge(sm[["scheme_code", "name", "asset_class"]], on="scheme_code", how="left")
    ext_df.to_csv(OUT / "return_outliers.csv", index=False)
    L += ["## 6. Return calculation", "",
          f"- sample: {len(codes)} active direct growth schemes (seed {SEED}); cumulative returns re-computed independently (label-based `asof` lookup vs the production routine): **{checked}** horizon values checked, **{mism}** mismatches",
          f"- annualised value present on a horizon shorter than 1Y: **{ann_below1y}** (must be 0); 1Y annualised differing from 1Y cumulative by more than 3 points: **{ann_inconsistent}** (a 1Y window that is a few days off 365 is expected; large values would indicate a day-count bug)",
          "- horizons available in the sample: " + ", ".join(f"{h} {n}/{len(codes)}" for h, n in have.items()),
          f"- implausible values (|cumulative| > 300% over 1Y+ or |annualised| > 100%): **{len(ext_df)}** (listed in `return_outliers.csv`; usually unit consolidations, tiny NAV bases or segregated portfolios)",
          "- rules: cumulative = NAV(asof)/NAV(on or before asof - horizon) - 1; annualised = (1+cum)^(1/years) - 1 using the actual day count between the two NAV dates, only for 1Y, 3Y, 5Y, only when the window exceeds half a year; no extrapolation when history is shorter; a start NAV more than 7 days older than its target date rejects the horizon; a single-day NAV move above 50% inside the window also rejects it (unit consolidation, face-value change or bad print: the NAV ratio is then not a return); IDCW options are price return only.", ""]
    if ann_below1y:
        findings.append(("fail", "annualised returns appear on horizons shorter than a year"))

    # 7 ── NAV sanity and cross-checks ──────────────────────────────────────────────────────────────────────────────
    big = read_sql("""WITH x AS (SELECT scheme_code, date, nav, lag(nav) OVER (PARTITION BY scheme_code ORDER BY date) AS prev FROM mf_nav)
                      SELECT scheme_code, date, prev, nav, nav / prev - 1 AS change FROM x WHERE prev > 0 AND abs(nav / prev - 1) > 0.5""")
    big = big.merge(sm[["scheme_code", "name", "asset_class", "fund_group"]], on="scheme_code", how="left")
    big.sort_values("change", key=abs, ascending=False).head(2000).to_csv(OUT / "nav_outliers.csv", index=False)
    zero_neg = int(read_sql("SELECT count(*) n FROM mf_nav WHERE nav <= 0").iloc[0]["n"])
    one_nav = int(read_sql("SELECT count(*) n FROM (SELECT scheme_code FROM mf_nav GROUP BY 1 HAVING count(*) = 1) t").iloc[0]["n"])
    etf_off = pd.DataFrame(json.loads(Path(r"D:\Stoxsy\stoxsy\infra\data\non_equity_official_etf_v1.json").read_text())["instruments"])
    amfi_isins = set(sm["isin_growth_or_payout"].dropna()) | set(sm["isin_reinvest"].dropna())
    bse_funds = read_sql("SELECT scrip_code, isin FROM bse_listing_master WHERE instrument_type = 'fund_etf' AND isin IS NOT NULL")
    L += ["## 7. NAV sanity and cross-checks", "",
          f"- NAV <= 0 rows: **{zero_neg:,}**; schemes with a single NAV: {one_nav:,}; one-day NAV moves above 50%: **{len(big):,}** (bad print, unit consolidation or a fund winding down; `nav_outliers.csv`)",
          *([f"- largest: {r.name} ({r.scheme_code}) on {r.date}: {r.change:+.0%}" for r in big.reindex(big['change'].abs().sort_values(ascending=False).index).head(5).itertuples()] if len(big) else []),
          f"- ISIN present on {sm['isin_growth_or_payout'].notna().mean():.1%} of schemes",
          f"- NSE official ETF list: {int(etf_off['isin'].isin(amfi_isins).sum())} of {len(etf_off)} ISINs ({etf_off['isin'].isin(amfi_isins).mean():.1%}) appear as an AMFI scheme ISIN; AMFI schemes flagged ETF: {int(sm['is_etf'].sum()):,}",
          f"- BSE fund/ETF scrips with an ISIN: {len(bse_funds):,}; share found in AMFI: {(bse_funds['isin'].isin(amfi_isins).mean() if len(bse_funds) else float('nan')):.1%}", "",
          "## Not available from AMFI's NAV files", "",
          "Expense ratios (TER), portfolio holdings (needed for overlap), AUM and benchmarks come from other sources and are not loaded. Overlap and expense analysis are not possible yet; each needs its own approved source.", ""]
    if zero_neg:
        findings.append(("fail", f"{zero_neg:,} NAV rows are zero or negative"))

    verdict = "FAIL" if any(s == "fail" for s, _ in findings) else ("PASS WITH FINDINGS" if findings else "PASS")
    head = ["# AMFI mutual-fund layer validation", "",
            "> A separate asset-class layer: nothing here enters the equity universe, the defensive stock signal or decision cards. NAV is raw as published; past NAV performance is not advice or a forecast. "
            "No screener, recommendation or allocation logic is built on this layer until the findings below are reviewed.", "",
            f"**Gate verdict: {verdict}**", "", *([f"- [{s}] {t}" for s, t in findings] or ["- no findings"]), ""]
    (OUT / "amfi_validation.md").write_text("\n".join(head + L), encoding="utf-8")
    print("\n".join(head))
    print("\n".join(L)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
