import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { Chip, Empty, ErrorState, PageHead, Segmented, Skeleton } from "../components/ui";
import { bytes, shortDate } from "../lib/format";
import { useReport, useReports } from "../lib/queries";

const GROUPS: { value: string; label: string }[] = [
  { value: "", label: "All" }, { value: "research", label: "Research" }, { value: "model_cards", label: "Model cards" },
  { value: "defensive", label: "Defensive" }, { value: "adjustment", label: "Adjustments" }, { value: "corp_actions", label: "Corporate actions" }, { value: "data_quality", label: "Data quality" },
];

export function Research() {
  const [group, setGroup] = useState("");
  const [q, setQ] = useState("");
  const r = useReports(group || undefined);
  const rows = useMemo(() => (r.data ?? []).filter((x) => !q || (x.title + x.name).toLowerCase().includes(q.toLowerCase())), [r.data, q]);
  return (
    <>
      <PageHead title="Reports & models" sub="Every model result, data-quality report and corporate-action review the system has produced." />
      <div className="card">
        <header>
          <Segmented label="Report group" value={group} onChange={setGroup} options={GROUPS} />
          <label className="sr-only" htmlFor="rq">Filter reports</label>
          <input id="rq" className="input" style={{ maxWidth: 240 }} placeholder="Filter" value={q} onChange={(e) => setQ(e.target.value)} />
        </header>
        {r.isLoading && <Skeleton rows={6} />}
        {r.isError && <ErrorState error={r.error} onRetry={() => r.refetch()} />}
        {r.data && (rows.length === 0 ? <Empty>No reports match.</Empty> : (
          <div className="table-wrap" style={{ maxHeight: 620 }}>
            <table>
              <thead><tr><th>Report</th><th>Group</th><th>Updated</th><th className="num">Size</th></tr></thead>
              <tbody>
                {rows.map((x) => (
                  <tr key={x.group + x.name}>
                    <td className="wrap"><Link to={`/research/${x.group}/${x.name}`}>{x.title.slice(0, 120)}</Link>
                      {x.superseded && <> <Chip tone="bad">SUPERSEDED</Chip></>}</td>
                    <td><Chip>{x.group}</Chip></td><td>{shortDate(x.modified)}</td><td className="num">{bytes(x.bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
    </>
  );
}

export function renderMarkdown(md: string): string {
  return DOMPurify.sanitize(marked.parse(md, { async: false, gfm: true }) as string);
}

export function ReportView() {
  const { group = "", name = "" } = useParams();
  const r = useReport(group, name);
  const html = useMemo(() => (r.data ? renderMarkdown(r.data.markdown) : ""), [r.data]);
  return (
    <>
      <PageHead title={name} sub={<Link to="/research">← All reports</Link>} right={<Chip>{group}</Chip>} />
      <div className="card">
        {r.isLoading && <Skeleton rows={8} />}
        {r.isError && <ErrorState error={r.error} onRetry={() => r.refetch()} />}
        {r.data && <article className="md" dangerouslySetInnerHTML={{ __html: html }} />}
      </div>
    </>
  );
}
