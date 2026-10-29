"""Raw-first landing zone: every fetch is stored byte-for-byte BEFORE it is parsed.

    <root>/<source>/<yyyy>/<mm>/<dd>/<HHMMSS>-<sha8>.<ext>  +  <same>.meta.json (sidecar with fetch metadata)

Files are content-addressed by time and hash and never overwritten or edited. Replaying a parser fix reads from here.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from qsde.stream.base import RawItem
from qsde.config.paths import data_path

DEFAULT_ROOT = Path(os.getenv("STOXSY_LANDING_DIR", str(data_path("landing"))))


@dataclass(frozen=True)
class LandingRecord:
    source: str
    path: Path
    sha256: str
    fetched_at: datetime
    published_at: Optional[datetime]
    meta: dict
    ext: str


class LandingZone:
    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = Path(root)

    def _dir(self, source: str, t: datetime) -> Path:
        t = t.astimezone(timezone.utc)
        return self.root / source / f"{t:%Y}" / f"{t:%m}" / f"{t:%d}"

    def write(self, source: str, item: RawItem) -> LandingRecord:
        sha = hashlib.sha256(item.payload).hexdigest()
        d = self._dir(source, item.fetched_at)
        d.mkdir(parents=True, exist_ok=True)
        stem = f"{item.fetched_at.astimezone(timezone.utc):%H%M%S}-{sha[:8]}"
        path = d / f"{stem}.{item.ext}"
        side = d / f"{stem}.meta.json"           # never collides with a JSON payload
        if path.exists():                                   # same second, same bytes: already landed, never rewrite
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise FileExistsError(f"landing collision with different content: {path}")
        else:
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(item.payload)
            tmp.replace(path)                               # atomic: a reader never sees a half-written file
        if not side.exists():
            side.write_text(json.dumps({
                "source": source, "sha256": sha, "bytes": len(item.payload), "ext": item.ext,
                "fetched_at": item.fetched_at.astimezone(timezone.utc).isoformat(),
                "published_at": item.published_at.astimezone(timezone.utc).isoformat() if item.published_at else None,
                "meta": item.meta}, sort_keys=True), encoding="utf-8")
        return LandingRecord(source, path, sha, item.fetched_at, item.published_at, item.meta, item.ext)

    def records(self, source: str, since: Optional[datetime] = None) -> Iterator[LandingRecord]:
        """Landed records in fetch order (the sidecars are the index)."""
        base = self.root / source
        if not base.exists():
            return
        for side in sorted(base.rglob("*.json")):
            try:
                m = json.loads(side.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if not (isinstance(m, dict) and "sha256" in m and "fetched_at" in m):
                continue                                   # a JSON PAYLOAD, not a sidecar
            fetched = datetime.fromisoformat(m["fetched_at"])
            if since is not None and fetched <= since:
                continue
            stem = side.name[: -len(".meta.json")] if side.name.endswith(".meta.json") else side.name[: -len(".json")]
            data = side.with_name(stem + "." + m["ext"])
            if not data.exists():
                continue
            pub = datetime.fromisoformat(m["published_at"]) if m["published_at"] else None
            yield LandingRecord(source, data, m["sha256"], fetched, pub, m["meta"], m["ext"])

    @staticmethod
    def read(rec: LandingRecord) -> RawItem:
        payload = rec.path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != rec.sha256:
            raise ValueError(f"landed file changed after landing: {rec.path}")
        return RawItem(payload, rec.fetched_at, rec.published_at, rec.meta, rec.ext)
