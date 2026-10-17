"""Single source of truth for `factor_pit.factor_version` tags.

  raw_v0  level-form factors exactly as computed (pre-fix rows, and any write
          where fracdiff failed). Never read by training or serving.
  ffd_v1  RETIRED. Fractionally differenced at ingest, but the daily EOD job
          differenced only a ~10-day tail slice with an auto-widened kernel,
          so tail rows used a different kernel than backfilled rows
          (train/serve skew). Rows with this tag must not be read.
  ffd_v2  Fractionally differenced over each symbol's FULL history with one
          fixed kernel (d=0.4, thres=1e-4, no auto-widen), then sliced to the
          rows being persisted. A value at date T depends only on bars <= T
          and is identical whether it was written by a backfill or a daily run.

Train (`qsde.models.dataset`) and serve (`qsde.models.signal_generator`) both
import FFD_VERSION from here so they can never read different versions.
"""

RAW_VERSION = "raw_v0"
FFD_VERSION = "ffd_v2"

# Kernel parameters for ingest-time fractional differencing. Changing either
# changes every stored value: bump FFD_VERSION when you do.
FFD_D = 0.4
FFD_THRES = 1e-4
