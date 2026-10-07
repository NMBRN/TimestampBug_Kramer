#!/usr/bin/env python3
"""Plot NSx packet timestamps for each device in a session.

Needs matplotlib, unlike TimestampRepair itself. Reads only; nothing is written
unless OUTPUT_PNG is set.
"""
import sys
from pathlib import Path

import matplotlib.pyplot as plt

import TimestampRepair

# Folder holding the recording files. None = the folder containing this script.
DATA_DIR = None
# Session base name, e.g. '20260722-144417-144454-NBack-SUM-001'.
# None = the only session present; the script lists them if there is more than one.
SESSION = None
# Device whose clock rate is the baseline. It plots flat by construction; every other
# device's slope is its rate offset from this one.
# None = baseline each device against its own nominal sample rate instead, which measures
# its PHC against its own ADC crystal and so does not assume any device is correct.
REFERENCE_DEVICE = None
# Devices to plot, e.g. ['NSP', 'Hub1']. None = all of them. A railed device is four
# orders of magnitude larger than a drifting one and will flatten the rest onto the axis.
DEVICES = ['Hub1', 'Hub2',]
# Timestamps plotted per device. The series is decimated to roughly this many points.
MAX_POINTS = 20000
# None = open a window. Otherwise a path to write the figure to.
OUTPUT_PNG = None


def primary_nsx(files):
    """The device's fastest NSx stream, chosen the same way TimestampRepair does."""
    headers = [TimestampRepair.read_nsx_header(files[e]) for e in sorted(files) if e.startswith('ns')]
    return min(headers, key=lambda h: (h['period'], -h['packets'])) if headers else None


def measured_rate(header):
    """Device clock ns per nominal ns, end to end."""
    ts = TimestampRepair.read_nsx_timestamps(header)
    span = (len(ts) - 1) * header['nominal_period_ns']
    return (ts[-1] - ts[0]) / span if span > 0 else float('nan')


def wander(x, y):
    """Worst deviation from a straight line through the series, in ms.

    A clock with a constant rate error is a straight line and gives ~0 here however
    large its drift. Anything above the PHC read jitter is the clock wandering.
    """
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    slope = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / sxx if sxx > 0 else 0.0
    return max(abs(y[i] - my - slope * (x[i] - mx)) for i in range(n))


def series(header, baseline_rate):
    """(elapsed seconds, timestamp minus the baseline rate's ramp in ms), decimated."""
    ts = TimestampRepair.read_nsx_timestamps(header)
    nominal = header['nominal_period_ns']
    step = max(1, len(ts) // MAX_POINTS)
    ks = range(0, len(ts), step)
    x = [k * nominal / TimestampRepair.NS_PER_S for k in ks]
    y = [((ts[k] - ts[0]) - k * nominal * baseline_rate) / 1e6 for k in ks]
    return x, y, len(ts)


def main():
    root = Path(DATA_DIR).expanduser().resolve() if DATA_DIR else Path(__file__).resolve().parent
    sessions = TimestampRepair.discover(root)
    if not sessions:
        print(f'no NSP-/HubN- files found in {root}', file=sys.stderr)
        return 1
    base = SESSION
    if base is None:
        if len(sessions) > 1:
            print('more than one session here; set SESSION to one of:', file=sys.stderr)
            for b in sorted(sessions):
                print(f'  {b}', file=sys.stderr)
            return 1
        base = next(iter(sessions))
    if base not in sessions:
        print(f'session {base} not found in {root}', file=sys.stderr)
        return 1

    devices = []
    for dev in sorted(sessions[base], key=lambda d: (d != REFERENCE_DEVICE, d)):
        if DEVICES and dev not in DEVICES and dev != REFERENCE_DEVICE:
            continue
        header = primary_nsx(sessions[base][dev])
        if header is None:
            print(f'{dev}: no NSx file, skipped')
            continue
        devices.append((dev, header))
    if not devices:
        print(f'no NSx files for session {base}', file=sys.stderr)
        return 1
    if REFERENCE_DEVICE is None:
        baseline_rate = 1.0
    elif devices[0][0] != REFERENCE_DEVICE:
        print(f'{REFERENCE_DEVICE} has no NSx file in this session, so there is no baseline',
              file=sys.stderr)
        return 1
    else:
        baseline_rate = measured_rate(devices[0][1])

    frame = REFERENCE_DEVICE if REFERENCE_DEVICE else "each device's own nominal sample rate"
    fig, ax = plt.subplots(figsize=(10, 5.5))
    for dev, header in devices:
        x, y, n = series(header, baseline_rate)
        drift_ppm = (y[-1] - y[0]) / (x[-1] - x[0]) * 1000 if x[-1] > x[0] else float('nan')
        off_line = wander(x, y)
        print(f'{dev}: {header["path"].name}, {n} samples, '
              f'{y[-1] - y[0]:+.3f} ms end to end vs {frame} ({drift_ppm:+.1f} ppm), '
              f'wander {off_line:.3f} ms off a straight line')
        ax.plot(x, y, linewidth=0.8,
                label=f'{dev}  {drift_ppm:+.1f} ppm, wander {off_line:.3f} ms')
    ax.set_xlabel('elapsed time at nominal sample rate (s)')
    ax.set_ylabel(f'ms ahead of {frame}')
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    fig.suptitle(f'{base}\nclock rate relative to {frame}'
                 " (each device from its own first sample)", fontsize=10)
    fig.tight_layout()
    if OUTPUT_PNG:
        fig.savefig(OUTPUT_PNG, dpi=150)
        print(f'wrote {OUTPUT_PNG}')
    else:
        plt.show()
    return 0


if __name__ == '__main__':
    sys.exit(main())
