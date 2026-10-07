#!/usr/bin/env python3
"""Find and repair devices affected by the Gemini PTP step_threshold defect. Python 3.10+, no packages."""
import sys
from pathlib import Path

import TimestampRepair

# Folder holding the recording files with their original names (NSP-..., Hub1-..., Hub2-...).
# None = the folder containing this script.
ROOT_DIR = None
# Where corrected copies and timestamp_repair_report.json are written. None = ROOT_DIR/corrected
OUTPUT_DIR = None
# Device whose clock is trusted. 'auto' picks the NSP or lowest-numbered hub whose comment
# times agree with the grandmaster. Hub1 is set explicitly here: the 2026-07-22 report shows
# it locked to the grandmaster (10.8 ms median receipt latency, rate within 2.4 ppm), while
# the NSP is free-running and a long way off it.
REFERENCE = 'Hub1'
# Limit the repair to some devices, e.g. ['Hub2']. None = check every device.
DEVICES = None
# Devices never repaired, but still reported with the reason. The NSP's comment receipt times
# scatter by 220+ ms, which is the quantity the fit is built from, so it cannot be corrected
# by this method however the limits are set.
SKIP = ['NSP']
# Analyse and write the report only; no corrected files.
DRY_RUN = False
# Repair devices even if their clock looks fine.
FORCE = False

# Acceptance limits. Review the report rather than loosening these to pass.
LIMITS = dict(
    min_bursts=6,
    min_bursts_per_stretch=2,
    burst_gap_ms=2.0,
    max_band_ms=0.10,
    alpha=0.01,
    min_effect_ms=0.10,
    gap_tolerance_samples=0.1,
)


def main():
    root = Path(ROOT_DIR).expanduser().resolve() if ROOT_DIR else Path(__file__).resolve().parent
    out = Path(OUTPUT_DIR).expanduser().resolve() if OUTPUT_DIR else root / 'corrected'
    print(f'Data folder:   {root}')
    print(f'Output folder: {out}')
    try:
        if out.exists() and any(out.iterdir()):
            raise TimestampRepair.RepairError(f'{out} is not empty. Move or rename it first; '
                                              'existing results are never overwritten.')
        return TimestampRepair.repair_folder(root, out, reference=REFERENCE, only=DEVICES, force=FORCE,
                                             write=not DRY_RUN, skip=SKIP, launcher=Path(__file__), **LIMITS)
    except (TimestampRepair.RepairError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
