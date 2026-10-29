import { useState } from "react";
import { BUCKET_LABEL, BucketProfile, Chip, Empty, ErrorState, Evidence, PageHead, Segmented, Skeleton } from "../components/ui";
import { num, pct, REGIME_LABEL, regimeTone, rupees, shortDate } from "../lib/format";
import { useDefensive } from "../lib/queries";
import type { Tone } from "../components/ui";

const SORTS = [{ value: "score_desc", label: "Most defensive" }, { value: "score_asc", label: "Least defensive" }, { value: "symbol", label: "A–Z" }];

export function Defensive() {
  const [bucket, setBucket] = useState<number>(0);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("score_desc");
  const d = useDefensive({ bucket: bucket || undefined, q: q || undefined, sort, limit: 150 });

  return (
    <>
      <PageHead
        title="Defensive signal"
        sub={d.data ? `Risk ranking of the liquid universe as of ${shortDate(d.data.as_of)}` : "Risk ranking of the liquid universe"}
        right={<span style={{ display: "flex", gap: 8 }}><Evidence kind="research" />{d.data?.regime && <Chip tone={regimeTone(d.data.regime) as Tone}>Regime: {REGIME_LABEL[d.data.regime] ?? d.data.regime}</Chip>}</span>}
      />
      <div className="notice">
        This ranks names by <strong>risk</strong> (range, volatility, downside, drawdown, liquidity), not by expected return. It is built for portfolio construction and risk warnings, and has not been shown to predict returns.
      </div>

      <div className="card">
        <header>
          <Segmented label="Risk bucket" value={bucket} onChange={setBucket}
            options={[{ value: 0, label: "All" }, ...[5, 4, 3, 2, 1].map((b) => ({ value: b, label: String(b) }))]} />
          <Segmented label="Sort" value={sort} onChange={setSort} options={SORTS} />
          <label className="sr-only" htmlFor="q">Search symbol</label>
          <input id="q" className="input" style={{ maxWidth: 220 }} placeholder="Search symbol" value={q} onChange={(e) => setQ(e.target.value)} />
        </header>
        <p className="muted" style={{ marginTop: -4, fontSize: "0.85rem" }}>Bucket 5 = most defensive, 1 = highest risk. {bucket ? `Showing: ${BUCKET_LABEL[bucket]}.` : ""}</p>

        {d.isLoading && <Skeleton rows={6} />}
        {d.isError && <ErrorState error={d.error} onRetry={() => d.refetch()} />}
        {d.data && (
          <>
            <div className="grid two" style={{ marginBottom: 14 }}>
              <BucketProfile profile={d.data.bucket_profile} />
              <div className="muted" style={{ fontSize: "0.88rem" }}>
                <p><strong>How the score is built.</strong> On each date, every name is percent-ranked on five equally-weighted components and averaged: 14-day average true range, 60-day volatility, 60-day downside deviation, drawdown from the 120-day high, and traded value.</p>
                <p>It uses only data up to the close of that date. A name moves bucket when its own risk moves relative to the rest of the universe.</p>
              </div>
            </div>
            {d.data.rows.length === 0 ? <Empty>No names match.</Empty> : (
              <div className="table-wrap" style={{ maxHeight: 560 }}>
                <table>
                  <thead><tr><th>Symbol</th><th>Defensive score</th><th>Bucket</th><th className="num">Daily range</th><th className="num">Vol (60d)</th><th className="num">From 120d high</th><th className="num">Traded value</th><th>Risk notes</th></tr></thead>
                  <tbody>
                    {d.data.rows.map((r) => (
                      <tr key={r.symbol}>
                        <td><strong>{r.symbol}</strong></td>
                        <td style={{ minWidth: 150 }}><div style={{ display: "flex", gap: 8, alignItems: "center" }}><div className="bar" role="img" aria-label={`score ${num(r.defensive_score)}`}><span style={{ width: `${r.defensive_score * 100}%`, background: r.risk_bucket ? `var(--bucket-${r.risk_bucket})` : undefined }} /></div><span className="num">{num(r.defensive_score)}</span></div></td>
                        <td>{r.risk_bucket ?? "—"}</td>
                        <td className="num">{pct(r.atr_pct)}</td><td className="num">{pct(r.vol60)}</td><td className="num">{pct(r.dd120, 0)}</td><td className="num">{rupees(r.liq)}</td>
                        <td className="wrap">{r.warnings || <span className="muted">—</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="muted" style={{ fontSize: "0.82rem" }}>{d.data.n.toLocaleString("en-IN")} matching names; showing up to 150. {d.data.disclaimer}</p>
          </>
        )}
      </div>
    </>
  );
}
