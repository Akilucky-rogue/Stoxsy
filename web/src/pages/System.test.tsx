import { describe, expect, it } from "vitest";
import { nightlyTile } from "./System";

describe("nightly routine tile", () => {
  it("says when the routine last ran, from its own heartbeat", () => {
    const t = nightlyTile({ last_beat: "2026-10-30T13:45:01+00:00", status: "ok", age_hours: 18.5 }, true);
    expect(t.value).toBe("Ran 30 Oct 2026");
    expect(t.tone).toBe("ok");
    expect(String(t.sub)).toContain("19 h ago");
  });
  it("turns red when the last run failed or degraded", () => {
    expect(nightlyTile({ last_beat: "2026-10-30T13:45:01+00:00", status: "failed", age_hours: 2 }, true).tone).toBe("bad");
    expect(nightlyTile({ last_beat: "2026-10-30T13:45:01+00:00", status: "degraded", age_hours: 2 }, true).value).toContain("with problems");
  });
  it("warns when the last run is too old to be the current schedule", () => {
    const t = nightlyTile({ last_beat: "2026-10-20T13:45:01+00:00", status: "ok", age_hours: 240 }, false);
    expect(t.value).toContain("Overdue");
    expect(t.tone).toBeUndefined();
    expect(String(t.sub)).toContain("10 days");
  });
  it("says it has not seen the routine rather than claiming downloads are off", () => {
    const t = nightlyTile(null, false);
    expect(t.value).toBe("Not seen");
    expect(String(t.sub)).toMatch(/no run recorded/i);
  });
});
