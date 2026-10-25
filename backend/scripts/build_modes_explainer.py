"""Build the standalone Trader / Investor explainer (docs/design/trader_investor_modes.html) with the measured history embedded."""
import json
from pathlib import Path
from qsde.config.paths import data_path

root = Path(__file__).resolve().parents[2]
hist = json.loads(data_path("reports", "modes_history.json").read_text())
html = (root / "docs" / "design" / "trader_investor_modes_template.html").read_text(encoding="utf-8").replace("/*HISTORY*/null", json.dumps(hist))
out = root / "docs" / "design" / "trader_investor_modes.html"
out.write_text(html, encoding="utf-8")
print(out, f"{out.stat().st_size / 1024:.0f} KB")
