#!/usr/bin/env python3
"""Repair Gemini timestamps affected by the PTP step_threshold defect (DAQS-1403).

Python 3.10+, standard library only.

Background
----------
With ``step_threshold 0`` a hub's PTP hardware clock (PHC) may step only once. Any
later offset from the grandmaster is slewed out at the Ethernet MAC's limit of
+/-6.4 % (max_adj = 64,000,000 ppb), so every timestamp the hub writes (NSx
samples, NEV spikes, comments, digital events) is on a clock that runs 6.4 % fast
or slow and can be hours off. The hub's ADC sample clock is NOT affected.

Method
------
Time is modelled as a function of the affected device's own sample index, which
is immune to whatever its PHC did:

    reference_time(sample k) = origin + a[stretch] + P * k

"P" is the device's true sample period on the reference clock and "a" is the
offset of each continuous stretch of samples. A gap in the timestamps that comes
to a whole number of sample intervals is counted as dropped samples and the index
skips over it, so the samples either side keep one offset. P and a are Huber
M-estimates from Central comments that the affected device and a correctly
synchronized reference device both logged (identical text; each device stamps its
own receipt time). Comments that arrive together share one delivery latency, so
they are averaged into bursts and each burst counts as one observation. Anything
else stamped with the affected PHC (NEV packets, lower-rate NSx streams) is
converted to a sample index by interpolating the device's highest-rate NSx
timestamps and mapped with the same formula.

A device is repaired only if the 95 % band of its corrected time stays within
`max_band_ms` across the whole recording and the comment residuals show neither a
lasting step nor curvature larger than `min_effect_ms`. The gates limit the error of
the correction, not the scatter of individual comments.

The reference is chosen automatically unless named: flag-1 comment packets carry the
grandmaster time at which Central issued the comment, so a device whose receipt
times sit a few milliseconds after those times, at the same rate, is on the
grandmaster clock.
"""
import argparse
import bisect
import math
import hashlib
import json
import mmap
import os
import re
import struct
import sys
from array import array
from statistics import NormalDist
from pathlib import Path

VERSION = '3.0.0'
NS_PER_S = 1_000_000_000
COMMENT_ID = 0xFFFF
SYSTEM_ID_MIN = 0xFFF0          # 0xFFF0..0xFFFE: recording/log/config/video/tracking events
RAIL = 0.064                    # Cadence GEM (macb) max_adj, 64,000,000 ppb
HUBER_K = 1.345                 # Huber tuning constant: 95 % efficiency for normally distributed errors
GM_MIN_IN_RANGE = 0.9           # share of flag-1 latencies that must lie in 0..1 s for a grandmaster clock
MAD_TO_SD = 1 / NormalDist().inv_cdf(0.75)   # 1.4826: median absolute deviation to standard deviation
FILE_RE = re.compile(r'^(?P<device>NSP|Hub\d+)-(?P<base>.+)\.(?P<ext>nev|ns[1-9])$', re.IGNORECASE)

DEFAULTS = dict(
    min_bursts=6,                # comment bursts (independent observations) needed for a fit
    min_bursts_per_stretch=2,    # in every continuous stretch that has its own offset
    burst_gap_ms=2.0,            # comments this close together on the reference form one burst
    max_band_ms=0.10,            # 95 % band of the corrected time, anywhere in the recording
    alpha=0.01,                  # false-positive rate of each model test (step, curvature)
    min_effect_ms=0.10,          # a step or curvature smaller than this is reported, not blocked
    gap_tolerance_samples=0.1,   # a gap this close to a whole number of samples counts as dropped samples
    max_period_ppm=100.0,        # fitted sample period vs nominal
    affected_rate_ppm=50.0,      # PHC rate error that marks a device as affected
    affected_offset_ms=1.0,      # PHC offset that marks a device as affected
    max_extrapolation_s=1.0,     # PHC-stamped events allowed outside the NSx sample range
    chunk_seconds=10.0,          # PHC rate diagnostic resolution
)


class RepairError(ValueError):
    """A check failed; nothing is written for the device concerned."""


def require(condition, message):
    if not condition:
        raise RepairError(message)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(16 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def int32(value):
    return value - (1 << 32) if value >= (1 << 31) else value


def median(values):
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def mean(values):
    values = list(values)
    return sum(values) / len(values)


def summarize_errors(errors_ns):
    e = [x / 1e6 for x in errors_ns]
    return dict(n=len(e), mean_ms=mean(e), rms_ms=(mean(x * x for x in e)) ** 0.5,
                max_abs_ms=max(abs(x) for x in e), min_ms=min(e), max_ms=max(e))


def middle(s):
    """Median of an already sorted list."""
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def percentile(values, q):
    """Percentile by linear interpolation between order statistics, q in 0..100."""
    s = sorted(values)
    pos = (len(s) - 1) * q / 100
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def beta_inc(a, b, x):
    """Regularized incomplete beta function I_x(a, b), by Lentz's continued fraction
    (Numerical Recipes 6.4)."""
    if x <= 0 or x >= 1:
        return 0.0 if x <= 0 else 1.0
    if x > (a + 1) / (a + b + 2):
        return 1 - beta_inc(b, a, 1 - x)
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)) / a
    tiny, c, d = 1e-300, 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        for num in (m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
                    -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))):
            d = 1 + num * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + num / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return front * h


def t_quantile(p, df):
    """Student t quantile for p > 0.5: bisection on the exact upper tail,
    P(T > t) = I_{df / (df + t^2)}(df / 2, 1 / 2) / 2."""
    tail = 1 - p
    lo, hi = 0.0, 1.0
    while beta_inc(df / 2, 0.5, df / (df + hi * hi)) / 2 > tail:
        hi *= 2
    for _ in range(200):
        mid = (lo + hi) / 2
        if beta_inc(df / 2, 0.5, df / (df + mid * mid)) / 2 > tail:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-12 * hi:
            break
    return (lo + hi) / 2


# --------------------------------------------------------------------------- file readers

def read_nsx_header(path):
    path = Path(path)
    with path.open('rb') as f:
        raw = f.read(314)
    require(len(raw) == 314 and raw[:8] == b'BRSMPGRP' and tuple(raw[8:10]) == (3, 0),
            f'{path.name}: only BRSMPGRP 3.0 (PTP) NSx files are supported')
    header_size = struct.unpack_from('<I', raw, 10)[0]
    period, resolution = struct.unpack_from('<II', raw, 286)
    channels = struct.unpack_from('<I', raw, 310)[0]
    require(resolution == NS_PER_S, f'{path.name}: timestamp resolution must be 1 ns')
    require(channels > 0 and period > 0 and header_size == 314 + 66 * channels,
            f'{path.name}: unexpected NSx header layout')
    size = path.stat().st_size
    packet_size = 13 + 2 * channels
    require(size > header_size and (size - header_size) % packet_size == 0,
            f'{path.name}: data does not end on a packet boundary (truncated copy, or packets '
            'holding more than one sample, which PTP recordings never use)')
    return dict(path=path, kind='nsx', header_size=header_size, period=period, channels=channels,
                packet_size=packet_size, packets=(size - header_size) // packet_size, size=size,
                nominal_period_ns=period * NS_PER_S / 30000)


def read_nsx_timestamps(h):
    """Every packet timestamp; checks each packet is a one-sample data packet."""
    fmt = f'<BQI{2 * h["channels"]}x'
    ts, bad = array('Q'), 0
    with h['path'].open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        view = memoryview(m)[h['header_size']:h['size']]
        it = struct.iter_unpack(fmt, view)
        try:
            for head, tick, count in it:
                if head != 1 or count != 1:
                    bad += 1
                ts.append(tick)
        finally:
            del it
            view.release()
    require(bad == 0, f'{h["path"].name}: {bad} packets are not one-sample data packets')
    require(len(ts) >= 2, f'{h["path"].name}: fewer than two samples')
    return ts


def read_nev(path):
    path = Path(path)
    with path.open('rb') as f:
        raw = f.read(336)
    require(len(raw) == 336 and raw[:8] == b'BREVENTS' and tuple(raw[8:10]) == (3, 0),
            f'{path.name}: only BREVENTS 3.0 (PTP) NEV files are supported')
    header_size, packet_size, resolution = struct.unpack_from('<III', raw, 12)
    extended = struct.unpack_from('<I', raw, 332)[0]
    require(resolution == NS_PER_S, f'{path.name}: timestamp resolution must be 1 ns')
    require(header_size == 336 + 32 * extended and packet_size >= 17, f'{path.name}: unexpected NEV header')
    size = path.stat().st_size
    require((size - header_size) % packet_size == 0, f'{path.name}: truncated NEV data')
    ts, ids, comments = array('Q'), array('H'), []
    with path.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        view = memoryview(m)[header_size:size]
        it = struct.iter_unpack(f'<QH{packet_size - 10}x', view)
        try:
            for tick, pid in it:
                ts.append(tick)
                ids.append(pid)
        finally:
            del it
            view.release()
        for i, pid in enumerate(ids):
            if pid == COMMENT_ID:
                pos = header_size + i * packet_size
                charset, flag, data = struct.unpack_from('<BBI', m, pos + 10)
                text = bytes(m[pos + 16:pos + packet_size]).split(b'\0', 1)[0]
                comments.append(dict(index=i, ts=ts[i], charset=charset, flag=flag, data=data, text=text))
    return dict(path=path, kind='nev', header_size=header_size, packet_size=packet_size, size=size,
                ts=ts, ids=ids, comments=comments)


# --------------------------------------------------------------------------- comments

def unique_receipts(nev):
    """Flag-0 comment receipt ticks keyed by exact (charset, text); repeated text is dropped."""
    found, repeated = {}, set()
    for c in nev['comments']:
        if c['flag'] != 0:
            continue
        key = (c['charset'], c['text'])
        if key in found:
            repeated.add(key)
        found[key] = c['ts']
    for key in repeated:
        del found[key]
    return found, len(repeated)


def matched_pairs(target_nev, reference_nev):
    tgt, tgt_rep = unique_receipts(target_nev)
    ref, ref_rep = unique_receipts(reference_nev)
    keys = sorted(tgt.keys() & ref.keys(), key=lambda k: ref[k])
    pairs = [dict(text=k[1].decode('latin-1'), target_ts=tgt[k], reference_ts=ref[k]) for k in keys]
    return pairs, dict(matched=len(keys), target_only=len(tgt) - len(keys), reference_only=len(ref) - len(keys),
                       target_repeated_texts=tgt_rep, reference_repeated_texts=ref_rep)


def three_way_check(nevs, ref, target, model, settings):
    """Comments carried by every device, compared with the reference after correction. Reported only.

    The target's receipts are mapped through the model and only their median offset is removed,
    so its row shows its comment residuals. Every other device keeps its own clock, so both its
    offset and its rate relative to the reference are removed first; a hub that is still railed
    then shows how much its comment receipts scatter rather than how far its clock has drifted.
    No row tests the target's correction beyond what the fit already shows, so nothing here
    blocks a repair.
    """
    receipts = {d: unique_receipts(nev)[0] for d, nev in nevs.items()}
    shared = sorted(set.intersection(*(set(r) for r in receipts.values())),
                    key=lambda k: receipts[ref][k])
    result = dict(devices=sorted(nevs), comments_on_every_device=len(shared))
    if len(shared) < settings['min_bursts']:
        result['note'] = f'only {len(shared)} comments appear on every device; alignment not checked'
        return result
    result['per_device'] = {}
    for dev in sorted(nevs):
        if dev == ref:
            continue
        if dev == target:
            deltas = [float(model.map_tick(receipts[dev][k]) - receipts[ref][k]) for k in shared]
            offset = median(deltas)
            result['per_device'][dev] = dict(treatment='corrected; median offset removed',
                                             alignment=summarize_errors([d - offset for d in deltas]))
            continue
        x0, y0 = receipts[ref][shared[0]], receipts[dev][shared[0]]
        try:
            line = huber_fit([(float(receipts[ref][k] - x0), 0, float(receipts[dev][k] - y0)) for k in shared], 1)
        except RepairError as exc:
            result['per_device'][dev] = dict(note=str(exc))
            continue
        result['per_device'][dev] = dict(treatment='own clock; offset and rate removed',
                                         rate_vs_reference_ppm=(line['slope'] - 1) * 1e6,
                                         alignment=summarize_errors(line['residuals']))
    return result


def skip_reason(check):
    """Why a device excluded from repair could not have been repaired anyway."""
    if not check or not check.get('usable'):
        return 'no usable flag-1 comments, so this device\'s clock state cannot be judged'
    spread = check['latency_ms_p95'] - check['latency_ms_p5']
    return (f'the middle 90 % of comment receipt latencies spans {spread:.1f} ms (median '
            f'{check["latency_ms_median"]:.1f} ms). The fit is driven by comment receipt times, '
            f'so scatter of this size sets the floor on any correction derived from them.')


def grandmaster_check(nev):
    """Is this device's clock the grandmaster's? Uses flag-1 comments (receipt - issue time, in us).

    The clock counts as on the grandmaster when at least GM_MIN_IN_RANGE of the latencies lie
    between 0 and 1 s and a Huber line of receipt time against issue time has a slope within
    100 ppm of 1. Comments whose latency is out of range are listed rather than allowed to decide
    the result on their own.
    """
    rows = [(c['ts'], int32(c['data']), c['text']) for c in nev['comments']
            if c['flag'] == 1 and c['charset'] != 255]
    if len(rows) < 3:
        return dict(usable=False, consistent=None, comments=len(rows),
                    note='fewer than three flag-1 comments; cannot judge the clock')
    lat = [d for _, d, _ in rows]
    inside = [0 <= d <= 1_000_000 for d in lat]
    x0, y0 = rows[0][0] - rows[0][1] * 1000, rows[0][0]
    points = [(float(t - d * 1000 - x0), 0, float(t - y0)) for t, d, _ in rows]
    line = huber_fit(points, 1) if len({x for x, _, _ in points}) > 1 else None
    in_range = sum(inside) / len(rows)
    consistent = in_range >= GM_MIN_IN_RANGE and line is not None and abs(line['slope'] - 1) < 100e-6
    odd = [dict(text=text.decode('latin-1')[:80], latency_ms=d / 1000)
           for (_, d, text), ok in zip(rows, inside) if not ok]
    return dict(usable=True, consistent=consistent, comments=len(rows), in_range_fraction=round(in_range, 4),
                latency_ms_min=min(lat) / 1000, latency_ms_max=max(lat) / 1000,
                latency_ms_median=median(lat) / 1000,
                latency_ms_p5=percentile(lat, 5) / 1000, latency_ms_p95=percentile(lat, 95) / 1000,
                rate_vs_issue_time_ppm=(line['slope'] - 1) * 1e6 if line else None,
                out_of_range=len(odd), out_of_range_comments=odd[:20])


# --------------------------------------------------------------------------- PHC tick -> sample index

class SampleTimeline:
    """Converts a PHC tick of one device into a sample index of its primary NSx samples.

    The timestamps split into runs wherever an interval falls outside 0.5 to 1.5 times the
    median interval. The gap at each split is measured in samples twice, with the mean interval
    just before it and just after it. If both give the same whole number within `tolerance`,
    the gap is that many dropped samples: the index skips them and the stretch continues, so the
    samples either side share one offset. Anything else (a clock jump, a restart, a gap too long
    to count exactly) starts a new stretch, which gets its own offset in the fit.
    """

    def __init__(self, ts, name, tolerance):
        self.ts, self.n, self.name = ts, len(ts), name
        sample = [ts[i + 1] - ts[i] for i in range(0, self.n - 1, max(1, (self.n - 1) // 50000))]
        sample.sort()
        self.median_step = sample[len(sample) // 2]
        breaks, prev = [], ts[0]
        lo, hi = self.median_step * 0.5, self.median_step * 1.5
        for i in range(1, self.n):
            cur = ts[i]
            d = cur - prev
            if d <= 0:
                raise RepairError(f'{name}: timestamps do not increase at sample {i} (backward clock step); '
                                  'such files need manual segmentation')
            if d < lo or d > hi:
                breaks.append(i)
            prev = cur
        self.starts = [0] + breaks
        self.ends = breaks + [self.n]
        self.steps = []
        for s, e in zip(self.starts, self.ends):
            if e - s >= 2:
                m = min(30000, e - s - 1)
                first = (ts[s + m] - ts[s]) / m
                last = (ts[e - 1] - ts[e - 1 - m]) / m
            else:
                first = last = float(self.median_step)
            self.steps.append((first, last))
        self.shift, self.stretch, self.gaps = [0], [0], []
        for r, i in enumerate(breaks):
            d = ts[i] - ts[i - 1]
            before, after = d / self.steps[r][1] - 1, d / self.steps[r + 1][0] - 1
            whole = round(before)
            dropped = (whole >= 1 and whole == round(after)
                       and abs(before - whole) <= tolerance and abs(after - whole) <= tolerance)
            self.shift.append(self.shift[-1] + (whole if dropped else 0))
            self.stretch.append(self.stretch[-1] + (0 if dropped else 1))
            self.gaps.append(dict(sample=i, jump_ns=d, samples_by_interval_before=round(before, 3),
                                  samples_by_interval_after=round(after, 3),
                                  dropped_samples=whole if dropped else None,
                                  kind='dropped samples' if dropped else 'new stretch'))
        self.stretches = self.stretch[-1] + 1
        self.dropped_samples = self.shift[-1]

    def runs(self):
        """(first sample, end, index shift, stretch) of each run of regular intervals."""
        return zip(self.starts, self.ends, self.shift, self.stretch)

    def virtual(self, k):
        """(index of sample k counting dropped samples, its stretch)."""
        r = bisect.bisect_right(self.starts, k) - 1
        return k + self.shift[r], self.stretch[r]

    def stretch_ends(self):
        """(first sample, its index, last sample, its index) of each stretch, counting dropped samples."""
        ends = {}
        for s, e, shift, st in self.runs():
            k0, v0 = ends[st][:2] if st in ends else (s, s + shift)
            ends[st] = (k0, v0, e - 1, e - 1 + shift)
        return [ends[st] for st in range(self.stretches)]

    def mean_interval(self):
        """Measured timestamp interval per sample (ns), from the first to the last sample of each
        stretch with dropped samples counted, so gaps do not inflate it."""
        dt = dv = 0
        for k0, v0, k1, v1 in self.stretch_ends():
            dt += self.ts[k1] - self.ts[k0]
            dv += v1 - v0
        return dt / dv if dv else float(self.median_step)

    def ranges(self):
        """(first, last) index of each stretch, counting dropped samples."""
        out = {}
        for s, e, shift, st in self.runs():
            lo, hi = s + shift, e - 1 + shift
            out[st] = (min(out[st][0], lo), max(out[st][1], hi)) if st in out else (lo, hi)
        return [out[st] for st in range(self.stretches)]

    def index_of(self, tick):
        """(fractional index, stretch, distance outside recorded samples in ns)."""
        ts, n, last = self.ts, self.n, len(self.starts) - 1
        j = bisect.bisect_right(ts, tick) - 1
        if j < 0:
            return self.shift[0] + (tick - ts[0]) / self.steps[0][0], self.stretch[0], ts[0] - tick
        if j >= n - 1:
            return (n - 1 + self.shift[last] + (tick - ts[n - 1]) / self.steps[last][1], self.stretch[last],
                    max(0, tick - ts[n - 1]))
        r = bisect.bisect_right(self.starts, j) - 1
        if r == last or self.starts[r + 1] != j + 1:
            return j + self.shift[r] + (tick - ts[j]) / (ts[j + 1] - ts[j]), self.stretch[r], 0
        v0, v1 = j + self.shift[r], j + 1 + self.shift[r + 1]
        if self.stretch[r + 1] == self.stretch[r]:
            # Dropped samples: the index runs on through the gap.
            return v0 + (tick - ts[j]) * (v1 - v0) / (ts[j + 1] - ts[j]), self.stretch[r], 0
        # Inside a break between stretches: extrapolate from the nearer side.
        if tick - ts[j] <= ts[j + 1] - tick:
            return v0 + (tick - ts[j]) / self.steps[r][1], self.stretch[r], tick - ts[j]
        return v1 - (ts[j + 1] - tick) / self.steps[r + 1][0], self.stretch[r + 1], ts[j + 1] - tick


class PhcTimeline:
    """Fallback for a device with an NEV but no NSx: the PHC tick itself is the abscissa."""
    stretches, gaps, dropped_samples, name = 1, [], 0, 'PHC ticks (no NSx)'

    def __init__(self, t0, t1):
        self.t0, self.t1 = t0, t1

    def index_of(self, tick):
        return float(tick - self.t0), 0, 0

    def ranges(self):
        return [(0.0, float(self.t1 - self.t0))]


# --------------------------------------------------------------------------- model

def weighted_fit(points, weights, stretches):
    """Weighted least squares: shared slope, one intercept per stretch. points: (x, stretch, y)."""
    sw, sx, sy = [0.0] * stretches, [0.0] * stretches, [0.0] * stretches
    for (x, s, y), w in zip(points, weights):
        sw[s] += w
        sx[s] += w * x
        sy[s] += w * y
    xm = [sx[s] / sw[s] for s in range(stretches)]
    ym = [sy[s] / sw[s] for s in range(stretches)]
    sxx = sxy = 0.0
    for (x, s, y), w in zip(points, weights):
        dx = x - xm[s]
        sxx += w * dx * dx
        sxy += w * dx * (y - ym[s])
    require(sxx > 0, 'comments do not span enough time to fit a rate')
    slope = sxy / sxx
    return slope, [ym[s] - slope * xm[s] for s in range(stretches)]


def huber_factor(residuals, scale, p):
    """H1 covariance factor of a Huber M-estimate (Huber 1981; statsmodels RLM 'H1').

    Multiplied by (X'X)^-1 it gives the covariance of the coefficients:
    kappa^2 * [sum(psi^2) / (n - p)] / mean(psi')^2 * scale^2, kappa = 1 + p/n * var(psi') / mean(psi')^2.
    """
    n = len(residuals)
    u = [r / scale for r in residuals]
    psi = [max(-HUBER_K, min(HUBER_K, v)) for v in u]
    m = sum(1 for v in u if abs(v) <= HUBER_K) / n
    kappa = 1 + p / n * (1 - m) / m
    return kappa ** 2 * (sum(v * v for v in psi) / (n - p)) / m ** 2 * scale ** 2


def huber_fit(points, stretches):
    """Huber M-estimate of the shared slope and per-stretch intercepts. points: (x, stretch, y).

    Iteratively reweighted least squares; the scale is re-estimated each round as 1.4826 times
    the median absolute residual. Residuals are fitted minus observed. `factor`, `count`, `xmean`
    and `sxx` give the covariance of fitted values (see fitted_variance).
    """
    n, p = len(points), stretches + 1
    span = max(x for x, _, _ in points) - min(x for x, _, _ in points)
    weights, slope, icpt = [1.0] * n, None, None
    for iteration in range(1, 101):
        new_slope, new_icpt = weighted_fit(points, weights, stretches)
        settled = (slope is not None and abs(new_slope - slope) * span < 1e-3
                   and all(abs(a - b) < 1e-3 for a, b in zip(new_icpt, icpt)))
        slope, icpt = new_slope, new_icpt
        residuals = [icpt[s] + slope * x - y for x, s, y in points]
        scale = MAD_TO_SD * median([abs(r) for r in residuals])
        if scale <= 0:
            weights = [1.0] * n
            break
        weights = [1.0 if abs(r) <= HUBER_K * scale else HUBER_K * scale / abs(r) for r in residuals]
        if settled:
            break
    count, xmean = [0] * stretches, [0.0] * stretches
    for x, s, _ in points:
        count[s] += 1
        xmean[s] += x
    xmean = [xmean[s] / count[s] for s in range(stretches)]
    sxx = sum((x - xmean[s]) ** 2 for x, s, _ in points)
    factor = huber_factor(residuals, scale, p) if scale > 0 and n > p else 0.0
    return dict(slope=slope, intercepts=icpt, residuals=residuals, weights=weights, scale=scale,
                iterations=iteration, factor=factor, count=count, xmean=xmean, sxx=sxx, df=n - p)


def fitted_variance(fit, x, stretch):
    """Variance of the fitted value at abscissa x in a stretch: factor * [(X'X)^-1 quadratic form]."""
    return fit['factor'] * (1 / fit['count'][stretch] + (x - fit['xmean'][stretch]) ** 2 / fit['sxx'])


def prediction_band(fit, ranges):
    """Half-width of the 95 % band of the fitted time at both ends of every stretch, in ms.

    The variance of a fitted value grows with distance from the stretch's mean comment
    position, so within each stretch the band is widest at one of its ends.
    """
    q = t_quantile(0.975, fit['df'])
    rows = [dict(stretch=s, first_sample_ms=q * math.sqrt(fitted_variance(fit, lo, s)) / 1e6,
                 last_sample_ms=q * math.sqrt(fitted_variance(fit, hi, s)) / 1e6)
            for s, (lo, hi) in enumerate(ranges)]
    return dict(max_ms=max(max(r['first_sample_ms'], r['last_sample_ms']) for r in rows),
                t_quantile=round(q, 4), df=fit['df'], per_stretch=rows)


def group_bursts(points, reference_ticks, gap_ns):
    """Group comments, in reference-time order, into bursts.

    A comment that reached the reference within gap_ns of the previous one, in the same stretch,
    joins its burst. Comments delivered together share one latency, so a burst is one
    observation: the mean index and the mean reference time of its members.
    """
    bursts, last = [], None
    for i, ((x, s, y), t) in enumerate(zip(points, reference_ticks)):
        if bursts and t - last <= gap_ns and s == bursts[-1]['stretch']:
            bursts[-1]['members'].append(i)
        else:
            bursts.append(dict(stretch=s, members=[i]))
        last = t
    for b in bursts:
        m = b['members']
        b['x'] = sum(points[i][0] for i in m) / len(m)
        b['y'] = sum(points[i][2] for i in m) / len(m)
    return bursts


def two_term_fit(rows, weights, stretches):
    """Weighted least squares with two shared terms (u, v) and one intercept per stretch.
    rows: (u, v, stretch, y). Returns (c_u, c_v, intercepts), or None if the terms cannot be separated."""
    sw, su, sv, sy = ([0.0] * stretches for _ in range(4))
    for (u, v, s, y), w in zip(rows, weights):
        sw[s] += w
        su[s] += w * u
        sv[s] += w * v
        sy[s] += w * y
    um, vm, ym = ([a[s] / sw[s] for s in range(stretches)] for a in (su, sv, sy))
    a11 = a12 = a22 = b1 = b2 = 0.0
    for (u, v, s, y), w in zip(rows, weights):
        du, dv, dy = u - um[s], v - vm[s], y - ym[s]
        a11 += w * du * du
        a12 += w * du * dv
        a22 += w * dv * dv
        b1 += w * du * dy
        b2 += w * dv * dy
    det = a11 * a22 - a12 * a12
    if det <= 1e-12 * a11 * a22:
        return None
    c1, c2 = (a22 * b1 - a12 * b2) / det, (a11 * b2 - a12 * b1) / det
    return c1, c2, [ym[s] - c1 * um[s] - c2 * vm[s] for s in range(stretches)]


def huber_two_terms(rows, stretches):
    """Huber M-estimate with two shared terms and per-stretch intercepts; None if degenerate.

    Returns the coefficients, residuals (fitted minus observed), scale, and the unweighted
    (X'X)^-1 element of the second term, for its H1 variance.
    """
    n = len(rows)
    weights, coef = [1.0] * n, None
    for _ in range(100):
        new = two_term_fit(rows, weights, stretches)
        if new is None:
            return None
        settled = (coef is not None and abs(new[0] - coef[0]) < 1e-3 and abs(new[1] - coef[1]) < 1e-3
                   and all(abs(a - b) < 1e-3 for a, b in zip(new[2], coef[2])))
        coef = new
        residuals = [coef[2][s] + coef[0] * u + coef[1] * v - y for u, v, s, y in rows]
        scale = MAD_TO_SD * median([abs(r) for r in residuals])
        if scale <= 0:
            return None
        weights = [1.0 if abs(r) <= HUBER_K * scale else HUBER_K * scale / abs(r) for r in residuals]
        if settled:
            break
    count, um, vm = [0] * stretches, [0.0] * stretches, [0.0] * stretches
    for u, v, s, _ in rows:
        count[s] += 1
        um[s] += u
        vm[s] += v
    um, vm = [um[s] / count[s] for s in range(stretches)], [vm[s] / count[s] for s in range(stretches)]
    a11 = a12 = a22 = 0.0
    for u, v, s, _ in rows:
        du, dv = u - um[s], v - vm[s]
        a11 += du * du
        a12 += du * dv
        a22 += dv * dv
    return dict(c_u=coef[0], c_v=coef[1], intercepts=coef[2], residuals=residuals, scale=scale,
                inv_vv=a11 / (a11 * a22 - a12 * a12))


def curvature_test(points, stretches, linear, ranges, settings):
    """Test whether the sample period drifts within the recording.

    The Huber fit is repeated with a quadratic term in the sample index, scaled to -1..1 over the
    recording. Its coefficient is tested against zero with the H1 standard error and a two-sided
    Student t critical value at `alpha`. As with the step test, significance alone does not
    block: the quadratic fit must also move the corrected time, somewhere in the recording, by
    more than `min_effect_ms` from the linear fit (the "bow").
    """
    n, p = len(points), stretches + 2
    result = dict(n=n, alpha=settings['alpha'])
    lo, hi = min(r[0] for r in ranges), max(r[1] for r in ranges)
    c0, h = (lo + hi) / 2, (hi - lo) / 2
    if n < 10 or n - p < 3 or h <= 0:
        result['note'] = f'only {n} bursts; too few to test for curvature'
        return result, False
    rows = [((x - c0) / h, ((x - c0) / h) ** 2, s, y) for x, s, y in points]
    quad = huber_two_terms(rows, stretches)
    if quad is None:
        result['note'] = 'comment bursts do not spread enough to test for curvature'
        return result, False
    se = math.sqrt(huber_factor(quad['residuals'], quad['scale'], p) * quad['inv_vv'])
    t = quad['c_v'] / se
    critical = t_quantile(1 - settings['alpha'] / 2, n - p)
    bow = 0.0
    for s, (v0, v1) in enumerate(ranges):
        # Quadratic minus linear fit, as a quadratic in u: d0 + d1 u + d2 u^2.
        d0 = quad['intercepts'][s] - linear['intercepts'][s] - linear['slope'] * c0
        d1, d2 = quad['c_u'] - linear['slope'] * h, quad['c_v']
        us = [(v0 - c0) / h, (v1 - c0) / h]
        if d2 and us[0] < -d1 / (2 * d2) < us[1]:
            us.append(-d1 / (2 * d2))
        bow = max(bow, max(abs(d0 + d1 * u + d2 * u * u) for u in us))
    result.update(quadratic_ns=quad['c_v'], se_ns=se, t=round(t, 3), critical_t=round(critical, 3), df=n - p,
                  bow_ms=bow / 1e6, min_effect_ms=settings['min_effect_ms'])
    result['significant'] = abs(t) > critical
    result['blocking'] = bool(result['significant'] and bow > settings['min_effect_ms'] * 1e6)
    return result, result['blocking']


def contiguous_cv(points, stretches, folds=2):
    """Fit on all but one contiguous block of bursts (in time order) and predict that block.

    Unlike interleaved folds this predicts across a long stretch of time, so it shows how well
    the fitted line extrapolates within the recording. Reported only.
    """
    n, errors, skipped = len(points), [], 0
    for f in range(folds):
        lo, hi = f * n // folds, (f + 1) * n // folds
        train = points[:lo] + points[hi:]
        index = {s: i for i, s in enumerate(sorted({s for _, s, _ in train}))}
        if len(train) <= len(index) + 1:
            skipped += hi - lo
            continue
        try:
            fit = huber_fit([(x, index[s], y) for x, s, y in train], len(index))
        except RepairError:
            skipped += hi - lo
            continue
        for x, s, y in points[lo:hi]:
            if s in index:
                errors.append(fit['intercepts'][index[s]] + fit['slope'] * x - y)
            else:
                skipped += 1
    if not errors:
        return dict(folds=folds, note='too few bursts to predict a block')
    return dict(folds=folds, predicted=len(errors), skipped=skipped, **summarize_errors(errors))


class Model:
    def __init__(self, origin, slope, intercepts, timeline):
        self.origin, self.slope, self.intercepts, self.timeline = origin, slope, intercepts, timeline

    def value(self, x, stretch):
        return self.intercepts[stretch] + self.slope * x

    def map_tick(self, tick):
        x, s, _ = self.timeline.index_of(tick)
        return self.origin + round(self.value(x, s))

    def map_index(self, k):
        v, s = self.timeline.virtual(k)
        return self.origin + round(self.value(v, s))


def phc_diagnostics(model, primary, settings):
    """PHC rate relative to the reference clock, in chunks; offset at the first sample."""
    tl = model.timeline
    if not isinstance(tl, SampleTimeline):
        rate = 1 / model.slope
        return dict(offset_at_first_sample_ms=None, runs=[dict(rate=rate, label=label(rate))],
                    max_rate_error_ppm=abs(rate - 1) * 1e6)
    ts, P = tl.ts, model.slope
    chunk = max(2, round(settings['chunk_seconds'] * NS_PER_S / P))
    rates = []
    for s, e, shift, _ in tl.runs():
        k = s
        while k < e - 1:
            k2 = min(e - 1, k + chunk)
            rates.append((k + shift, k2 + shift, (ts[k2] - ts[k]) / (P * (k2 - k))))
            k = k2
    runs = []
    for k, k2, r in rates:
        if runs and abs(r - runs[-1]['rate']) < 20e-6:
            run = runs[-1]
            run['_end'], run['_w'] = k2, run['_w'] + (k2 - k)
            run['rate'] += (r - run['rate']) * (k2 - k) / run['_w']
        else:
            runs.append(dict(_start=k, _end=k2, _w=k2 - k, rate=r))
    for run in runs:
        run['from_s'] = round(run.pop('_start') * P / NS_PER_S, 3)
        run['to_s'] = round(run.pop('_end') * P / NS_PER_S, 3)
        run.pop('_w')
        run['label'] = label(run['rate'])
        run['frequency_offset_percent'] = (run['rate'] - 1) * 100
    offset = ts[0] - model.map_index(0)
    return dict(offset_at_first_sample_ms=offset / 1e6, runs=runs,
                max_rate_error_ppm=max((abs(r - 1) for _, _, r in rates), default=0.0) * 1e6)


def label(rate):
    if abs(rate - (1 - RAIL)) < 50e-6:
        return 'railed at -6.4% (clock ahead of grandmaster, slewing down)'
    if abs(rate - (1 + RAIL)) < 50e-6:
        return 'railed at +6.4% (clock behind grandmaster, slewing up)'
    if abs(rate - 1) < 50e-6:
        return 'running at grandmaster rate'
    return 'slewing (rate between rail and lock)'


def build_model(device, files, reference_nev, settings):
    """Fit the model for one device. Returns (model, analysis, nev, nsx, primary, bursts); raises RepairError.

    `bursts` holds each burst's member ticks, (target ticks, reference ticks), for the
    cross-session check.
    """
    nev = read_nev(files['nev'])
    nsx = [read_nsx_header(files[e]) for e in sorted(files) if e.startswith('ns')]
    primary = min(nsx, key=lambda h: (h['period'], -h['packets'])) if nsx else None
    if primary:
        timeline = SampleTimeline(read_nsx_timestamps(primary), primary['path'].name,
                                  settings['gap_tolerance_samples'])
    else:
        keep = {(pid, t) for pid, t in zip(reference_nev['ids'], reference_nev['ts']) if SYSTEM_ID_MIN <= pid < COMMENT_ID}
        own = [t for pid, t in zip(nev['ids'], nev['ts']) if not (SYSTEM_ID_MIN <= pid < COMMENT_ID and (pid, t) in keep)]
        timeline = PhcTimeline(min(own), max(own))
    pairs, pair_stats = matched_pairs(nev, reference_nev)
    require(pairs, f'{device}: no comments shared with the reference')
    origin = pairs[0]['reference_ts']
    points, worst_extrap = [], 0
    for p in pairs:
        x, s, ex = timeline.index_of(p['target_ts'])
        worst_extrap = max(worst_extrap, ex)
        points.append((x, s, float(p['reference_ts'] - origin)))
    bursts = group_bursts(points, [p['reference_ts'] for p in pairs], settings['burst_gap_ms'] * 1e6)
    require(len(bursts) >= settings['min_bursts'],
            f'{device}: only {len(bursts)} comment bursts shared with the reference (need {settings["min_bursts"]})')
    per_stretch = [sum(1 for b in bursts if b['stretch'] == s) for s in range(timeline.stretches)]
    require(all(c >= settings['min_bursts_per_stretch'] for c in per_stretch),
            f'{device}: comment bursts per continuous stretch {per_stretch}; each needs '
            f'{settings["min_bursts_per_stretch"]} (a break that is not a whole number of dropped samples '
            'cannot be bridged without comments on both sides)')
    require(len(bursts) > timeline.stretches + 1, f'{device}: too few comment bursts for {timeline.stretches} stretches')
    burst_points = [(b['x'], b['stretch'], b['y']) for b in bursts]
    fit = huber_fit(burst_points, timeline.stretches)
    model = Model(origin, fit['slope'], fit['intercepts'], timeline)
    ranges = timeline.ranges()
    boundaries = []
    for g in timeline.gaps:
        if g['kind'] == 'new stretch':
            (v0, s0), (v1, s1) = timeline.virtual(g['sample'] - 1), timeline.virtual(g['sample'])
            g['implied_interval_ns'] = round(model.value(v1, s1) - model.value(v0, s0))
            boundaries.append(g['implied_interval_ns'])
    step, _ = step_test(burst_points, timeline.stretches, fit, ranges, settings)
    curvature, _ = curvature_test(burst_points, timeline.stretches, fit, ranges, settings)
    comment_residuals = [model.value(x, s) - y for x, s, y in points]
    burst_of = {i: n for n, b in enumerate(bursts) for i in b['members']}
    weakest = sorted((w, n) for n, w in enumerate(fit['weights']) if w < 1)[:20]
    period_se = math.sqrt(fit['factor'] / fit['sxx'])
    analysis = dict(
        files={e: str(files[e]) for e in sorted(files)},
        primary_nsx=primary['path'].name if primary else None,
        model='sample-index' if primary else 'PHC-linear (no NSx)',
        comment_pairs=pair_stats,
        bursts=dict(count=len(bursts), comments=len(pairs), gap_ms=settings['burst_gap_ms'],
                    largest=max(len(b['members']) for b in bursts)),
        timeline=(dict(samples=timeline.n, stretches=timeline.stretches, gaps=len(timeline.gaps),
                       dropped_samples=timeline.dropped_samples,
                       shortest_stretch_break_ns=min(boundaries) if boundaries else None,
                       discontinuities=timeline.gaps[:20])
                  if primary else dict(note='no NSx; PHC ticks used directly')),
        bursts_per_stretch=per_stretch,
        fit=dict(method=f'Huber M-estimate (k = {HUBER_K}) on burst means, MAD scale, H1 covariance',
                 iterations=fit['iterations'], scale_us=fit['scale'] / 1e3,
                 downweighted_bursts=sum(1 for w in fit['weights'] if w < 1),
                 lowest_weights=[dict(burst=n, weight=round(w, 3), comments=len(bursts[n]['members']),
                                      residual_us=round(fit['residuals'][n] / 1e3, 3),
                                      text=pairs[bursts[n]['members'][0]]['text'][:80]) for w, n in weakest]),
        fitted_sample_period_ns=fit['slope'] if primary else None,
        sample_period_se_ns=period_se if primary else None,
        nominal_sample_period_ns=primary['nominal_period_ns'] if primary else None,
        sample_clock_vs_nominal_ppm=(primary['nominal_period_ns'] / fit['slope'] - 1) * 1e6 if primary else None,
        sample_clock_ppm_se=(primary['nominal_period_ns'] * period_se / fit['slope'] ** 2 * 1e6) if primary else None,
        reference_ns_per_phc_ns=None if primary else fit['slope'],
        intercepts_ns={str(s): v for s, v in enumerate(fit['intercepts'])},
        origin_reference_tick=str(origin),
        band_95=prediction_band(fit, ranges),
        burst_residual=summarize_errors(fit['residuals']),
        comment_residual=summarize_errors(comment_residuals),
        step_test=step,
        curvature_test=curvature,
        contiguous_cv=contiguous_cv(burst_points, timeline.stretches),
        comment_extrapolation_ms=worst_extrap / 1e6,
        comment_table=[dict(text=p['text'][:80], reference_ts=str(p['reference_ts']), target_ts=str(p['target_ts']),
                            burst=burst_of[i], weight=round(fit['weights'][burst_of[i]], 3),
                            corrected_minus_reference_us=round(r / 1e3, 3))
                       for i, (p, r) in enumerate(zip(pairs, comment_residuals))],
    )
    analysis['phc'] = phc_diagnostics(model, primary, settings)
    analysis['grandmaster_check_raw'] = grandmaster_check(nev)
    member_ticks = [([pairs[i]['target_ts'] for i in b['members']], [pairs[i]['reference_ts'] for i in b['members']])
                    for b in bursts]
    return model, analysis, nev, nsx, primary, member_ticks


def step_test(points, stretches, linear, ranges, settings, min_side=5):
    """Scan the comment bursts, in time order, for a lasting change in level at an unknown point.

    A stall in comment handling displaces one burst and recovers, leaving the level unchanged.
    Samples lost without a trace in the timestamps, or a step in the reference clock, shift every
    later burst instead, so a persistent step is the signature worth refusing.

    A step cannot be read off the residuals of the straight-line fit, because that fit tilts to
    absorb most of it. Each candidate split is instead tested as an added step term in the fitted
    model, with the slope still free: the Huber score of the step indicator, after projecting the
    indicator off the intercepts and the slope, divided by its H1 standard deviation gives an
    approximately normal z. Huber's bounded psi keeps a single displaced burst near either end
    from posing as a step.

    The scan reports the largest |z| over the K admissible splits (at least `min_side` bursts each
    side), compared against a Bonferroni-corrected critical value, Phi^-1(1 - alpha / (2K)), so
    `alpha` is the false-positive rate for the whole scan rather than for one split. The K
    statistics are strongly correlated, so Bonferroni errs conservative here. The step is then
    measured by refitting the Huber model with the step term at the best split.

    Significance alone does not block. With a thousand bursts a step of a few microseconds is
    easily significant and physically meaningless, so the measured step must also exceed
    `min_effect_ms`. Steps are reported in the residual sign, corrected minus reference.
    """
    n, p = len(points), stretches + 1
    result = dict(n=n, alpha=settings['alpha'])
    if n < 2 * min_side + 2 or n - p - 1 < 1:
        result['note'] = f'only {n} bursts; too few to scan for a step'
        return result, False
    sigma = linear['scale']
    if sigma <= 0:
        result['note'] = 'residuals are identical; no scale to test against'
        return result, False
    u = [-r / sigma for r in linear['residuals']]
    psi = [max(-HUBER_K, min(HUBER_K, v)) for v in u]
    m = sum(1 for v in u if abs(v) <= HUBER_K) / n
    kappa = 1 + (p + 1) / n * (1 - m) / m
    spread = math.sqrt(sum(v * v for v in psi) / (n - p - 1))
    count, xmean, sxx = linear['count'], linear['xmean'], linear['sxx']
    xc = [x - xmean[s] for x, s, _ in points]
    psi_s = [0.0] * stretches
    for (x, s, _), v in zip(points, psi):
        psi_s[s] += v
    psi_x = sum(v * c for v, c in zip(psi, xc))
    after_n, after_xc, after_psi = list(count), [0.0] * stretches, sum(psi)
    for (_, s, _), c in zip(points, xc):
        after_xc[s] += c
    best, k = (0.0, None), 0
    for i in range(1, n - min_side + 1):
        s = points[i - 1][1]
        after_n[s] -= 1
        after_xc[s] -= xc[i - 1]
        after_psi -= psi[i - 1]
        if i < min_side:
            continue
        slope_d = sum(after_xc) / sxx
        sdd = (sum(after_n) - sum(after_n[t] ** 2 / count[t] for t in range(stretches)) - slope_d ** 2 * sxx)
        if sdd <= 1e-9 * n:
            continue
        k += 1
        g = after_psi - sum(after_n[t] / count[t] * psi_s[t] for t in range(stretches)) - slope_d * psi_x
        z = g / (kappa * spread * math.sqrt(sdd))
        if abs(z) > abs(best[0]):
            best = (z, i)
    if not k:
        result['note'] = 'no split away from a stretch boundary to test'
        return result, False
    critical = NormalDist().inv_cdf(1 - settings['alpha'] / (2 * k))
    lo, hi = min(r[0] for r in ranges), max(r[1] for r in ranges)
    c0, h = (lo + hi) / 2, max((hi - lo) / 2, 1.0)
    refit = huber_two_terms([((x - c0) / h, 1.0 if j >= best[1] else 0.0, s, y)
                             for j, (x, s, y) in enumerate(points)], stretches)
    step = -refit['c_v'] if refit else None
    result.update(residual_sigma_us=sigma / 1e3, splits_tested=k, critical_z=round(critical, 3),
                  max_abs_z=round(abs(best[0]), 3), at_burst=best[1],
                  step_ms=step / 1e6 if step is not None else None, min_effect_ms=settings['min_effect_ms'])
    result['significant'] = abs(best[0]) > critical
    result['blocking'] = bool(result['significant'] and step is not None
                              and abs(step) > settings['min_effect_ms'] * 1e6)
    if result['significant'] and step is None:
        result['blocking'] = True
        result['note'] = 'significant step that could not be measured; blocked to be safe'
    return result, result['blocking']


def gate(analysis, settings):
    issues = []
    shortest = analysis['timeline'].get('shortest_stretch_break_ns')
    if shortest is not None and shortest <= 0:
        issues.append(f'the fitted offsets of two stretches overlap by {-shortest / 1e3:.1f} us at a break, so the '
                      'corrected timestamps would run backwards; the comments cannot place a break this short')
    band = analysis['band_95']
    if band['max_ms'] > settings['max_band_ms']:
        issues.append(f'95 % band of the corrected time reaches {band["max_ms"]:.3f} ms, above the '
                      f'{settings["max_band_ms"]} ms limit')
    ppm = analysis['sample_clock_vs_nominal_ppm']
    if ppm is not None and abs(ppm) > settings['max_period_ppm']:
        issues.append(f'fitted sample clock is {ppm:+.1f} ppm from nominal (limit {settings["max_period_ppm"]})')
    st = analysis['step_test']
    if st.get('blocking'):
        size = f'by {st["step_ms"]:+.3f} ms ' if st.get('step_ms') is not None else ''
        issues.append(f'comment residuals step {size}at burst {st["at_burst"]} of {st["n"]} '
                      f'(|z| {st["max_abs_z"]} against a Bonferroni critical value of {st["critical_z"]} over '
                      f'{st["splits_tested"]} splits); a lasting shift of this size points to lost samples '
                      'or a step in the reference clock')
    cv = analysis['curvature_test']
    if cv.get('blocking'):
        issues.append(f'the sample period drifts: a quadratic term moves the corrected time by up to '
                      f'{cv["bow_ms"]:.3f} ms (|t| {abs(cv["t"])} against {cv["critical_t"]})')
    return issues


def is_affected(analysis, settings):
    phc = analysis['phc']
    off = phc['offset_at_first_sample_ms']
    return (phc['max_rate_error_ppm'] > settings['affected_rate_ppm'] or
            (off is not None and abs(off) > settings['affected_offset_ms']))


# --------------------------------------------------------------------------- writers

def nsx_new_timestamps(model, h, primary, old_ts=None):
    if primary:
        new = array('Q')
        for s, e, shift, stretch in model.timeline.runs():
            new.extend(model.origin + round(model.value(k + shift, stretch)) for k in range(s, e))
        return new
    old_ts = old_ts if old_ts is not None else read_nsx_timestamps(h)
    return array('Q', (model.map_tick(t) for t in old_ts))


def write_nsx(h, new_ts, out_path):
    pkt, n, k = h['packet_size'], h['packets'], 0
    with h['path'].open('rb') as src, open(out_path, 'xb') as dst:
        dst.write(src.read(h['header_size']))
        while k < n:
            m = min(65536, n - k)
            buf = bytearray(src.read(m * pkt))
            require(len(buf) == m * pkt, 'unexpected end of NSx file')
            for i in range(m):
                struct.pack_into('<Q', buf, i * pkt + 1, new_ts[k + i])
            dst.write(buf)
            k += m


def nev_plan(model, nev, reference_nev, settings):
    """New timestamp (or None to keep) for each packet, plus new flag-1 data fields."""
    keep_keys = {(pid, t) for pid, t in zip(reference_nev['ids'], reference_nev['ts'])
                 if SYSTEM_ID_MIN <= pid < COMMENT_ID}
    new_ts, kept, mapped, worst = [], {}, 0, 0
    for pid, t in zip(nev['ids'], nev['ts']):
        if SYSTEM_ID_MIN <= pid < COMMENT_ID and (pid, t) in keep_keys:
            new_ts.append(None)
            kept[hex(pid)] = kept.get(hex(pid), 0) + 1
            continue
        x, s, ex = model.timeline.index_of(t)
        worst = max(worst, ex)
        new_ts.append(model.origin + round(model.value(x, s)))
        mapped += 1
    data = {}
    for c in nev['comments']:
        if c['flag'] == 1 and new_ts[c['index']] is not None:
            shift_us = round((new_ts[c['index']] - c['ts']) / 1000)
            data[c['index']] = (c['data'] + shift_us) % (1 << 32)
    return new_ts, data, dict(mapped=mapped, kept_on_reference_clock=kept,
                              worst_extrapolation_ms=worst / 1e6, flag1_fields_updated=len(data))


def nev_expected_bytes(nev, new_ts, data):
    with nev['path'].open('rb') as f:
        buf = bytearray(f.read())
    hs, pkt = nev['header_size'], nev['packet_size']
    for i, t in enumerate(new_ts):
        if t is not None:
            struct.pack_into('<Q', buf, hs + i * pkt, t)
    for i, d in data.items():
        struct.pack_into('<I', buf, hs + i * pkt + 12, d)
    return buf


# --------------------------------------------------------------------------- verification

def verify_nsx(h, out_path, expected, primary):
    """Re-read the output: header identical, sample bytes identical, timestamps as expected."""
    pkt, n = h['packet_size'], h['packets']
    with h['path'].open('rb') as src, open(out_path, 'rb') as dst:
        require(src.read(h['header_size']) == dst.read(h['header_size']), f'{out_path}: header changed')
        k, prev, steps = 0, None, [None, None]
        while k < n:
            m = min(65536, n - k)
            a, b = bytearray(src.read(m * pkt)), dst.read(m * pkt)
            for i in range(m):
                struct.pack_into('<Q', a, i * pkt + 1, expected[k + i])
            require(a == b, f'{out_path}: bytes differ from source-with-expected-timestamps near sample {k}')
            for i in range(m):
                t = struct.unpack_from('<Q', b, i * pkt + 1)[0]
                if prev is not None:
                    d = t - prev
                    require(d > 0 or not primary, f'{out_path}: output timestamps do not increase at {k + i}')
                    steps[0] = d if steps[0] is None else min(steps[0], d)
                    steps[1] = d if steps[1] is None else max(steps[1], d)
                prev = t
            k += m
        require(dst.read(1) == b'', f'{out_path}: output longer than source')
    return dict(samples=n, first_timestamp=str(expected[0]), last_timestamp=str(expected[-1]),
                duration_s=(expected[-1] - expected[0]) / NS_PER_S,
                min_step_ns=steps[0], max_step_ns=steps[1], bytes_identical_except_timestamps=True)


def post_repair_checks(out_nev_path, reference_nev):
    fixed = read_nev(out_nev_path)
    pairs, _ = matched_pairs(fixed, reference_nev)
    errors = [p['target_ts'] - p['reference_ts'] for p in pairs]
    return dict(comment_pairs=summarize_errors(errors), grandmaster_check=grandmaster_check(fixed))


# --------------------------------------------------------------------------- orchestration

def discover(folder):
    sessions = {}
    for p in sorted(Path(folder).iterdir()):
        m = FILE_RE.match(p.name) if p.is_file() else None
        if m:
            dev = m['device'][0].upper() + m['device'][1:].lower() if m['device'].lower() != 'nsp' else 'NSP'
            files = sessions.setdefault(m['base'], {}).setdefault(dev, {})
            ext = m['ext'].lower()
            require(ext not in files, f'duplicate {dev} .{ext} files for session {m["base"]}')
            files[ext] = p
    return sessions


def choose_reference(devices, requested, nevs):
    checks = {d: grandmaster_check(nevs[d]) for d in nevs}
    if requested and requested != 'auto':
        require(requested in nevs, f'reference {requested} has no NEV file in this session')
        return requested, checks
    good = [d for d, c in checks.items() if c['consistent']]
    require(good, 'no device has an NEV whose comment times are consistent with the grandmaster; '
                  'set REFERENCE explicitly')
    order = sorted(good, key=lambda d: (d != 'NSP', int(d[3:]) if d.startswith('Hub') else 0))
    return order[0], checks


def reference_nsx_check(files, settings):
    """Scan the reference's own primary NSx timestamps the same way as a target's. Reported only."""
    try:
        nsx = [read_nsx_header(files[e]) for e in sorted(files) if e.startswith('ns')]
        if not nsx:
            return dict(note='the reference has no NSx file in this session; its continuity is not checked',
                        warnings=[])
        h = min(nsx, key=lambda h: (h['period'], -h['packets']))
        tl = SampleTimeline(read_nsx_timestamps(h), h['path'].name, settings['gap_tolerance_samples'])
    except RepairError as exc:
        return dict(error=str(exc), warnings=[f'NSx check failed: {exc}'])
    clock_ppm = [(h['nominal_period_ns'] * (v1 - v0) / (tl.ts[k1] - tl.ts[k0]) - 1) * 1e6 if k1 > k0 else None
                 for k0, v0, k1, v1 in tl.stretch_ends()]
    warnings = []
    if tl.dropped_samples:
        warnings.append(f'{h["path"].name}: {tl.dropped_samples} dropped samples in '
                        f'{sum(1 for g in tl.gaps if g["kind"] == "dropped samples")} gaps')
    if tl.stretches > 1:
        warnings.append(f'{h["path"].name}: timestamps break into {tl.stretches} stretches '
                        '(a clock jump or a restart)')
    return dict(primary_nsx=h['path'].name, samples=tl.n, stretches=tl.stretches, gaps=len(tl.gaps),
                dropped_samples=tl.dropped_samples, discontinuities=tl.gaps[:20],
                median_interval_ns=tl.median_step, mean_interval_ns=tl.mean_interval(),
                sample_clock_vs_nominal_ppm=clock_ppm, warnings=warnings)


def interval_check(model, reference_check, period_se_ppm):
    """Compare inter-sample intervals measured from both hubs' timestamps. Reported only.

    The target's raw interval is measured on its own PHC and the corrected interval is the fitted
    sample period on the reference clock, so raw / corrected is the PHC's rate as measured, which
    shows how closely the clock sat on the 6.4 % rail (or in lock) without the correction assuming
    it. The corrected interval is also compared with the reference's own measured interval: two
    independent ADC crystals differ by a few ppm, while an uncorrected railed hub is 6.4 % off.
    Both ratios carry the fitted period's own uncertainty, given as corrected_interval_se_ppm.
    """
    raw = model.timeline.mean_interval()
    rate = raw / model.slope
    nearest = min((1 - RAIL, 1.0, 1 + RAIL), key=lambda r: abs(rate - r))
    result = dict(target_raw_interval_ns=raw, corrected_interval_ns=model.slope,
                  corrected_interval_se_ppm=period_se_ppm, phc_rate=rate,
                  nearest_state=label(nearest), phc_rate_vs_nearest_state_ppm=(rate / nearest - 1) * 1e6)
    ref = reference_check.get('mean_interval_ns')
    if ref:
        result.update(reference_interval_ns=ref, corrected_vs_reference_ppm=(model.slope / ref - 1) * 1e6,
                      uncorrected_vs_reference_percent=(raw / ref - 1) * 100)
    else:
        result['note'] = 'no reference NSx interval to compare with'
    return result


def cross_session_checks(collect):
    """Leave-one-session-out check for each device repaired in several sessions. Reported only.

    Meaningful when the device's PHC ran on at one rate through all its sessions without
    re-stepping. One straight line from the device's PHC ticks to reference time is then fitted
    to the comment bursts of every other session and used to predict the held-out session's
    bursts. `offset_line` shows whether the PHC offset at each session's first sample lies on
    one line in reference time, which is what that requires.
    """
    out = {}
    for dev in sorted({c['device'] for c in collect}):
        items = [c for c in collect if c['device'] == dev]
        entry = out[dev] = dict(sessions=len(items))
        if len(items) < 3:
            entry['note'] = 'fewer than three sessions passed the gates; not checked'
            continue
        if len({c['reference'] for c in items}) > 1:
            entry['note'] = 'sessions use different reference devices; not checked'
            continue
        labels = {r['label'] for c in items for r in c['phc']['runs']}
        entry['phc_labels'] = sorted(labels)
        entry['one_phc_rate_throughout'] = len(labels) == 1 and all(len(c['phc']['runs']) == 1 for c in items)
        try:
            t0 = items[0]['first_reference_tick']
            line = [(float(c['first_reference_tick'] - t0), 0, c['phc']['offset_at_first_sample_ms'] * 1e6)
                    for c in items]
            slope, icpt = weighted_fit(line, [1.0] * len(line), 1)
            resid = [icpt[0] + slope * x - y for x, _, y in line]
            entry['offset_line'] = dict(slope_s_per_s=slope,
                                        residual_us={c['session']: round(r / 1e3, 1) for c, r in zip(items, resid)},
                                        max_abs_residual_us=round(max(abs(r) for r in resid) / 1e3, 1))
            x0, y0 = items[0]['bursts'][0][0][0], items[0]['bursts'][0][1][0]
            points = [[(sum(t - x0 for t in tt) / len(tt), 0, sum(r - y0 for r in rr) / len(rr))
                       for tt, rr in c['bursts']] for c in items]
            entry['leave_one_session_out'] = {}
            for j, c in enumerate(items):
                fit = huber_fit([p for i, ps in enumerate(points) if i != j for p in ps], 1)
                errors = [fit['intercepts'][0] + fit['slope'] * x - y for x, _, y in points[j]]
                entry['leave_one_session_out'][c['session']] = summarize_errors(errors)
        except RepairError as exc:
            entry['note'] = f'not checked: {exc}'
    return out


def repair_session(base, devices, out_dir, settings, reference='auto', only=None, force=False, write=True, log=print,
                   skip=None, collect=None):
    report = dict(session=base, tool=f'TimestampRepair {VERSION}', settings=settings, devices={})
    nevs = {d: read_nev(f['nev']) for d, f in devices.items() if 'nev' in f}
    ref, checks = choose_reference(devices, reference, nevs)
    report['reference'] = dict(device=ref, grandmaster_checks=checks)
    log(f'[{base}] reference: {ref}')
    if not checks[ref]['consistent']:
        report['reference']['warning'] = 'reference comment times are not consistent with the grandmaster'
        log(f'[{base}] WARNING: {ref} comment times are not consistent with the grandmaster '
            '(see reference.grandmaster_checks in the report)')
    report['reference']['nsx_check'] = reference_nsx_check(devices[ref], settings)
    for warning in report['reference']['nsx_check']['warnings']:
        log(f'[{base}] WARNING: reference {ref}: {warning}')
    for dev in sorted(devices):
        entry = report['devices'].setdefault(dev, {})
        if dev == ref:
            entry['status'] = 'reference (unchanged)'
            continue
        if only and dev not in only:
            entry['status'] = 'skipped (not selected)'
            continue
        if skip and dev in skip:
            entry.update(status='skipped (excluded from repair)',
                         grandmaster_check=checks.get(dev),
                         probable_cause=skip_reason(checks.get(dev)))
            log(f'[{base}] {dev}: skipped - {entry["probable_cause"]}')
            continue
        if 'nev' not in devices[dev]:
            entry['status'] = 'not evaluated: no NEV, so no shared comments'
            log(f'[{base}] {dev}: no NEV, cannot evaluate')
            continue
        try:
            model, analysis, nev, nsx, primary, bursts = build_model(dev, devices[dev], nevs[ref], settings)
        except RepairError as exc:
            entry.update(status='blocked', issues=[str(exc)])
            log(f'[{base}] {dev}: BLOCKED - {exc}')
            continue
        entry.update(analysis)
        if primary:
            entry['interval_check'] = interval_check(model, report['reference']['nsx_check'],
                                                     analysis['sample_clock_ppm_se'])
        affected = is_affected(analysis, settings)
        entry['affected'] = affected
        phc = analysis['phc']
        log(f'[{base}] {dev}: PHC offset at first sample {phc["offset_at_first_sample_ms"]} ms, '
            f'max rate error {phc["max_rate_error_ppm"]:.1f} ppm -> {"AFFECTED" if affected else "ok"}')
        if not affected and not force:
            entry['status'] = 'not affected (unchanged)'
            continue
        issues = gate(analysis, settings)
        if issues:
            entry.update(status='blocked', issues=issues)
            log(f'[{base}] {dev}: BLOCKED - ' + '; '.join(issues))
            continue
        entry['three_way_comment_check'] = three_way_check(nevs, ref, dev, model, settings)
        plan = nev_plan(model, nev, nevs[ref], settings)
        entry['nev_plan'] = plan[2]
        if plan[2]['worst_extrapolation_ms'] > settings['max_extrapolation_s'] * 1000:
            entry.update(status='blocked', issues=[
                f'NEV events up to {plan[2]["worst_extrapolation_ms"]:.1f} ms outside the NSx sample range'])
            continue
        if collect is not None and primary:
            collect.append(dict(device=dev, session=base, reference=ref, phc=phc,
                                first_reference_tick=model.map_index(0), bursts=bursts))
        if not write:
            entry['status'] = 'would repair (dry run)'
            continue
        entry['outputs'] = {}
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            for h in nsx:
                is_primary = h is primary
                expected = nsx_new_timestamps(model, h, is_primary)
                if not is_primary:
                    own = read_nsx_timestamps(h)
                    worst = max(model.timeline.index_of(t)[2] for t in (own[0], own[-1]))
                    require(worst <= settings['max_extrapolation_s'] * NS_PER_S,
                            f'{h["path"].name}: samples far outside the primary NSx range')
                before = sha256(h['path'])
                dest = out_dir / h['path'].name
                require(not dest.exists(), f'{dest} already exists')
                tmp = dest.with_name(dest.name + '.partial')
                write_nsx(h, expected, tmp)
                check = verify_nsx(h, tmp, expected, is_primary)
                require(sha256(h['path']) == before, f'{h["path"].name} changed during repair')
                os.replace(tmp, dest)
                check.update(source_sha256=before, output_sha256=sha256(dest))
                entry['outputs'][dest.name] = check
                log(f'[{base}] {dev}: wrote {dest.name}')
            new_ts, data, _ = plan
            expected_bytes = nev_expected_bytes(nev, new_ts, data)
            before = sha256(nev['path'])
            dest = out_dir / nev['path'].name
            require(not dest.exists(), f'{dest} already exists')
            tmp = dest.with_name(dest.name + '.partial')
            with open(tmp, 'xb') as f:
                f.write(expected_bytes)
            with open(tmp, 'rb') as f:
                require(f.read() == expected_bytes, f'{tmp}: re-read mismatch')
            require(sha256(nev['path']) == before, f'{nev["path"].name} changed during repair')
            os.replace(tmp, dest)
            entry['outputs'][dest.name] = dict(source_sha256=before, output_sha256=sha256(dest),
                                               packets=len(new_ts), **plan[2])
            entry['post_repair'] = post_repair_checks(dest, nevs[ref])
            entry['status'] = 'repaired'
            log(f'[{base}] {dev}: wrote {dest.name}; 95 % band {analysis["band_95"]["max_ms"]:.3f} ms; '
                f'post-repair comment error {entry["post_repair"]["comment_pairs"]["mean_ms"]:+.3f} ms mean, '
                f'{entry["post_repair"]["comment_pairs"]["max_abs_ms"]:.3f} ms max')
        except (RepairError, OSError) as exc:
            entry.update(status='failed', issues=[str(exc)])
            log(f'[{base}] {dev}: FAILED - {exc}')
            for p in out_dir.glob('*.partial'):
                p.unlink()
    return report


def repair_folder(data_dir, out_dir, reference='auto', only=None, force=False, write=True, log=print, skip=None,
                  launcher=None, **overrides):
    unknown = sorted(set(overrides) - set(DEFAULTS))
    require(not unknown, f'unknown settings {unknown}; the valid ones are listed in TimestampRepair.DEFAULTS')
    settings = dict(DEFAULTS, **overrides)
    data_dir, out_dir = Path(data_dir).resolve(), Path(out_dir).resolve()
    require(data_dir != out_dir, 'output folder must differ from the data folder')
    sessions = discover(data_dir)
    require(sessions, f'no NSP-/HubN- NSx or NEV files found in {data_dir}')
    reports, collect = [], []
    for base, devs in sessions.items():
        try:
            reports.append(repair_session(base, devs, out_dir, settings, reference, only, force, write, log, skip,
                                          collect))
        except RepairError as exc:
            reports.append(dict(session=base, tool=f'TimestampRepair {VERSION}', settings=settings,
                                devices={}, error=str(exc)))
            log(f'[{base}] SESSION NOT EVALUATED - {exc}')
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / 'timestamp_repair_report.json'
    require(not path.exists(), f'{path} already exists')
    report = dict(data_folder=str(data_dir), tool=f'TimestampRepair {VERSION}', tool_sha256=sha256(__file__),
                  launcher=dict(name=Path(launcher).name, sha256=sha256(launcher)) if launcher else None,
                  sessions=reports, cross_session=cross_session_checks(collect))
    path.write_text(json.dumps(report, indent=2, default=str) + '\n')
    log(f'report: {path}')
    statuses = [d.get('status', '') for r in reports for d in r['devices'].values()]
    return 2 if any('error' in r for r in reports) or \
                any(s.startswith(('blocked', 'failed')) for s in statuses) else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--data', required=True, help='folder with the session files (original names)')
    ap.add_argument('--out', required=True, help='new folder for corrected files and the report')
    ap.add_argument('--reference', default='auto', help='reference device, e.g. Hub1 (default: auto)')
    ap.add_argument('--devices', nargs='*', help='only evaluate these devices, e.g. Hub2 Hub3')
    ap.add_argument('--force', action='store_true', help='repair devices even if they look unaffected')
    ap.add_argument('--dry-run', action='store_true', help='analyse and report only')
    for key, value in DEFAULTS.items():
        ap.add_argument('--' + key.replace('_', '-'), type=type(value), default=value)
    a = ap.parse_args(argv)
    overrides = {k: getattr(a, k) for k in DEFAULTS}
    try:
        return repair_folder(a.data, a.out, a.reference, a.devices, a.force, not a.dry_run, **overrides)
    except (RepairError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
