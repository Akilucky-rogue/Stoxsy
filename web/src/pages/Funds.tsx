import { useState } from "react";
import { Chip, Empty, ErrorState, Evidence, PageHead, Segmented, Skeleton } from "../components/ui";
import { dash, num, signedPct, shortDate } from "../lib/format";
import { useFunds, type FundParams } from "../lib/queries";

const CLASSES = [
  { value: "", label: "All" }, { value: "equity", label: "Equity" }, { value: "debt", label: "Debt" }, { value: "hybrid", label: "Hybrid" },
  { value: "commodity", label: "Gold & silver" }, { value: "international", label: "International" },
];

export function Funds() {
  const [q, setQ] = useState("");
  const [assetClass, setAssetClass] = useState("");
  const [plan, setPlan] = useState("direct");
  const [growthOnly, setGrowth] = useState("growth");
  const [etf, setEtf] = useState("");
  const params: FundParams = { q, assetClass, plan, growthOnly: growthOnly === "growth", etf };
  const f = useFunds(params);

  const cell = (r: NonNullable<ReturnType<typeof useFunds>["data"]>["rows"][number], h: string, annual: boolean) => {
    const x = r.returns[h];
    if (!x) return dash;
    return annual && x.annualised != null ? signedPct(x.annualised) : signedPct(x.cumulative);
  };

  return (
    <>
      <PageHead
        title="Mutual funds & ETFs"
        sub="Scheme master and NAV-based returns from AMFI. A separate asset class: nothing here feeds the stock signal or decision cards."
        right={<Evidence kind="diagnostic" />}
      />
      <div className="notice">
        Returns are computed from published NAVs. <strong>Cumulative</strong> for 1M–6M; <strong>annualised (per year)</strong> for 1Y, 3Y and 5Y — the two are never mixed. Growth options give a total return after expenses;
        payout (IDCW) options give a price return only. Past performance is not a forecast or advice. Expense ratio, holdings/overlap and AUM are not loaded yet.
      </div>

      <div className="card">
        <header>
          <label className="sr-only" htmlFor="fq">Search funds</label>
          <input id="fq" className="input" style={{ maxWidth: 260 }} placeholder="Search fund or fund house" value={q} onChange={(e) => setQ(e.target.value)} />
          <Segmented label="Asset class" value={assetClass} onChange={setAssetClass} options={CLASSES} />
          <Segmented label="Plan" value={plan} onChange={setPlan} options={[{ value: "", label: "Any plan" }, { value: "direct", label: "Direct" }, { value: "regular", label: "Regular" }]} />
          <Segmented label="Option" value={growthOnly} onChange={setGrowth} options={[{ value: "growth", label: "Growth" }, { value: "all", label: "All options" }]} />
          <Segmented label="Vehicle" value={etf} onChange={setEtf} options={[{ value: "", label: "Funds + ETFs" }, { value: "true", label: "ETFs" }, { value: "false", label: "Funds" }]} />
        </header>

        {f.isLoading && <Skeleton rows={6} />}
        {f.isError && <ErrorState error={f.error} onRetry={() => f.refetch()} />}
        {f.data && (f.data.rows.length === 0 ? <Empty>No schemes match.</Empty> : (
          <>
            <p className="muted" style={{ fontSize: "0.85rem" }}>{f.data.n_matching.toLocaleString("en-IN")} matching schemes; showing the first {f.data.rows.length} by name.</p>
            <div className="table-wrap" style={{ maxHeight: 620 }}>
              <table>
                <thead>
                  <tr><th>Scheme</th><th>Type</th><th className="num">NAV</th><th className="num">1M</th><th className="num">3M</th><th className="num">6M</th>
                    <th className="num">1Y /yr</th><th className="num">3Y /yr</th><th className="num">5Y /yr</th><th>Basis</th></tr>
                </thead>
                <tbody>
                  {f.data.rows.map((r) => (
                    <tr key={r.scheme_code}>
                      <td className="wrap"><strong>{r.name}</strong><div className="muted" style={{ fontSize: "0.78rem" }}>{r.amc ?? ""} · {r.category ?? ""} · code {r.scheme_code}</div></td>
                      <td>{r.is_etf ? <Chip>ETF</Chip> : r.is_index ? <Chip>Index</Chip> : <Chip>Fund</Chip>} {r.asset_class && <Chip>{r.asset_class}</Chip>}</td>
                      <td className="num">{num(r.last_nav, 4)}<div className="muted" style={{ fontSize: "0.72rem" }}>{shortDate(r.last_nav_date)}</div></td>
                      <td className="num">{cell(r, "1M", false)}</td><td className="num">{cell(r, "3M", false)}</td><td className="num">{cell(r, "6M", false)}</td>
                      <td className="num">{cell(r, "1Y", true)}</td><td className="num">{cell(r, "3Y", true)}</td><td className="num">{cell(r, "5Y", true)}</td>
                      <td className="wrap" style={{ fontSize: "0.78rem" }}>{r.return_basis}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted" style={{ fontSize: "0.8rem" }}>{f.data.methodology}</p>
          </>
        ))}
      </div>
    </>
  );
}
