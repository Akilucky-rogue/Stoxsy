import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Alerts } from "./Alerts";

const row = { id: 1, scope: "system", rule_id: "ca_unreviewed", severity: "warning", state: "open", title: "151 corporate-action events are applied in research only", detail: "Needs the owner's decision.",
  entity_type: null, entity_id: null, first_seen: "2026-10-20T08:00:00Z", last_seen: "2026-10-20T08:05:00Z", occurrences: 3, acked_by: null, acked_at: null, resolved_at: null, resolved_why: null, payload: {} };

function mockFetch() {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const body = url.includes("/summary") ? { open: { critical: 0, warning: 1, info: 0 }, acknowledged: 0, last_run: "2026-10-20T08:05:00Z", stale: false }
      : url.includes("/refresh") && init?.method === "POST" ? { evaluated: ["global"], skipped_global: false, errors: [] }
        : { n_matching: 1, offset: 0, rows: [row], last_run: null };
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  }));
}

describe("Alerts page", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("lists an open alert with its detail, how long it has held, and an acknowledge action; states that nothing is sent or traded", async () => {
    mockFetch();
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><Alerts /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText(/151 corporate-action events/)).toBeTruthy();
    expect(screen.getByText(/still true on 3 checks/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Acknowledge" })).toBeTruthy();
    expect(screen.getByText(/nothing is sent outside this application/)).toBeTruthy();
  });
});
