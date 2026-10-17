"""The Scorecard (Reality Engine) — honest predicted-vs-actual measurement.

ARCHITECTURE.md §2. metrics = pure stats (expectancy/calibration); fidelity =
our-vs-market correctness; tracker = idea->outcome aggregation. No engine gets
real capital until its live-paper Scorecard clears a net-of-cost bar.
"""
from qsde.scorecard import fidelity, metrics, tracker

__all__ = ["metrics", "fidelity", "tracker"]
