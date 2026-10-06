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

    reference_time(sample k) = origin + a[segment] + P * k

"P" is the device's true sample period on the reference clock and "a" is the
offset of each continuous stretch of samples. Both are least-squares estimates
from Central comments that the affected device and a correctly synchronized
reference device both logged (identical text; each device stamps its own
receipt time). Anything else stamped with the affected PHC (NEV packets,
lower-rate NSx streams) is converted to a fractional sample index by
interpolating the device's highest-rate NSx timestamps and mapped with the same
formula. A two-fold hold-out on the comments must show no bias before any file
is written.

The reference is chosen automatically: flag-1 comment packets carry the
grandmaster time at which Central issued the comment, so a device whose receipt
times sit a few milliseconds after those times, at the same rate, is on the
grandmaster clock.
"""
import argparse
import bisect
import hashlib
import json
import mmap
import os
import re
import struct
import sys
from array import array
from pathlib import Path

VERSION = '2.0.0'
NS_PER_S = 1_000_000_000
COMMENT_ID = 0xFFFF
SYSTEM_ID_MIN = 0xFFF0          # 0xFFF0..0xFFFE: recording/log/config/video/tracking events
RAIL = 0.064                    # Cadence GEM (macb) max_adj, 64,000,000 ppb
FILE_RE = re.compile(r'^(?P<device>NSP|Hub\d+)-(?P<base>.+)\.(?P<ext>nev|ns[1-9])$', re.IGNORECASE)

DEFAULTS = dict(
    min_pairs=6,                 # matched comments needed in total
    min_pairs_per_segment=2,     # per continuous stretch of samples (1 to fit, 2 for hold-out)
    max_bias_ms=0.10,            # hold-out mean error limit
    max_rms_ms=0.25,             # hold-out RMS error limit
    max_abs_ms=0.50,             # hold-out worst-case error limit
    max_period_ppm=100.0,        # fitted sample period vs nominal
    affected_rate_ppm=50.0,      # PHC rate error that marks a device as affected
    affected_offset_ms=1.0,      # PHC offset that marks a device as affected
    max_three_way_ms=5.0,        # comment misalignment allowed on devices taking part in the repair
    max_three_way_skipped_ms=1000.0,   # ... and on devices excluded from it, whose receipts are noisy
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


def mean(values):
    values = list(values)
    return sum(values) / len(values)


def summarize_errors(errors_ns):
    e = [x / 1e6 for x in errors_ns]
    return dict(n=len(e), mean_ms=mean(e), rms_ms=(mean(x * x for x in e)) ** 0.5,
                max_abs_ms=max(abs(x) for x in e), min_ms=min(e), max_ms=max(e))


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


def three_way_check(nevs, ref, target, model, settings, skip=None):
    """Comments carried by every device, checked for alignment after the target is corrected.

    Every device receives a given comment at about the same moment, so once each device's
    own constant clock offset is removed the receipts should coincide. A device whose clock
    is merely wrong, by 12 hours or anything else, subtracts out and does not fail here.
    What survives is misalignment that varies comment to comment, which is what lost samples
    or a mispaired comment produce.

    Devices excluded from the repair are held to a looser limit, because their receipt times
    may be noisy in their own right; that noise would otherwise set the floor for everyone.
    """
    receipts = {d: unique_receipts(nev)[0] for d, nev in nevs.items()}
    shared = sorted(set.intersection(*(set(r) for r in receipts.values())),
                    key=lambda k: receipts[ref][k])
    result = dict(devices=sorted(nevs), comments_on_every_device=len(shared))
    if len(shared) < settings['min_pairs']:
        result['note'] = (f'only {len(shared)} comments appear on every device '
                          f'(need {settings["min_pairs"]}); alignment not checked')
        return result, []
    result['per_device'], exceeded = {}, []
    for dev in sorted(nevs):
        deltas = [float((model.map_tick(receipts[dev][k]) if dev == target else receipts[dev][k])
                        - receipts[ref][k]) for k in shared]
        offset = sorted(deltas)[len(deltas) // 2]
        worst = summarize_errors([d - offset for d in deltas])
        excluded = bool(skip) and dev in skip
        limit = settings['max_three_way_skipped_ms' if excluded else 'max_three_way_ms']
        result['per_device'][dev] = dict(clock_offset_ms=offset / 1e6, alignment=worst,
                                         limit_ms=limit, in_repair=not excluded)
        if worst['max_abs_ms'] > limit:
            exceeded.append(f'{dev} by {worst["max_abs_ms"]:.1f} ms (limit {limit:.1f})')
    result['worst_alignment_ms'] = max(v['alignment']['max_abs_ms']
                                       for v in result['per_device'].values())
    return result, exceeded


def skip_reason(check):
    """Why a device excluded from repair could not have been repaired anyway."""
    if not check or not check.get('usable'):
        return 'no usable flag-1 comments, so this device\'s clock state cannot be judged'
    spread = check['latency_ms_max'] - check['latency_ms_min']
    return (f'comment receipt latency spans {spread:.1f} ms (median '
            f'{check["latency_ms_median"]:.1f} ms). The fit is driven by comment receipt times, '
            f'so scatter of this size sets the floor on any correction derived from them.')


def grandmaster_check(nev):
    """Is this device's clock the grandmaster's? Uses flag-1 comments (receipt - issue time, in us)."""
    rows = [(c['ts'], int32(c['data'])) for c in nev['comments'] if c['flag'] == 1 and c['charset'] != 255]
    if len(rows) < 3:
        return dict(usable=False, consistent=None, comments=len(rows),
                    note='fewer than three flag-1 comments; cannot judge the clock')
    lat = [d for _, d in rows]
    issued = [t - d * 1000 for t, d in rows]
    x0, y0 = issued[0], rows[0][0]
    xs = [float(x - x0) for x in issued]
    ys = [float(t - y0) for t, _ in rows]
    xm, ym = mean(xs), mean(ys)
    sxx = sum((x - xm) ** 2 for x in xs)
    slope = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / sxx if sxx > 0 else float('nan')
    in_range = all(0 <= d <= 1_000_000 for d in lat)
    consistent = in_range and sxx > 0 and abs(slope - 1) < 100e-6
    return dict(usable=True, consistent=consistent, comments=len(rows),
                latency_ms_min=min(lat) / 1000, latency_ms_max=max(lat) / 1000,
                latency_ms_median=sorted(lat)[len(lat) // 2] / 1000,
                rate_vs_issue_time_ppm=(slope - 1) * 1e6 if sxx > 0 else None)


# --------------------------------------------------------------------------- PHC tick -> sample index

class SampleTimeline:
    """Converts a PHC tick of one device into a fractional index of its primary NSx samples."""

    def __init__(self, ts, name):
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
        self.discontinuities = [dict(sample=i, jump_ns=ts[i] - ts[i - 1]) for i in breaks]

    @property
    def segments(self):
        return len(self.starts)

    def segment_of_index(self, k):
        return bisect.bisect_right(self.starts, k) - 1

    def index_of(self, tick):
        """(fractional index, segment, distance outside recorded samples in ns)."""
        ts, n = self.ts, self.n
        j = bisect.bisect_right(ts, tick) - 1
        if j < 0:
            return (tick - ts[0]) / self.steps[0][0], 0, ts[0] - tick
        if j >= n - 1:
            return n - 1 + (tick - ts[n - 1]) / self.steps[-1][1], self.segments - 1, max(0, tick - ts[n - 1])
        sj = self.segment_of_index(j)
        if self.segment_of_index(j + 1) == sj:
            return j + (tick - ts[j]) / (ts[j + 1] - ts[j]), sj, 0
        # Inside a discontinuity: extrapolate from the nearer side.
        if tick - ts[j] <= ts[j + 1] - tick:
            return j + (tick - ts[j]) / self.steps[sj][1], sj, tick - ts[j]
        return j + 1 - (ts[j + 1] - tick) / self.steps[sj + 1][0], sj + 1, ts[j + 1] - tick


class PhcTimeline:
    """Fallback for a device with an NEV but no NSx: the PHC tick itself is the abscissa."""
    segments, discontinuities, name = 1, [], 'PHC ticks (no NSx)'

    def __init__(self, t0):
        self.t0 = t0

    def index_of(self, tick):
        return float(tick - self.t0), 0, 0


# --------------------------------------------------------------------------- model

def fit(points):
    """Shared slope, one intercept per segment. points: (x, segment, y)."""
    groups = {}
    for x, s, y in points:
        groups.setdefault(s, []).append((x, y))
    sxx = sxy = 0.0
    means = {}
    for s, g in groups.items():
        xm, ym = mean(x for x, _ in g), mean(y for _, y in g)
        means[s] = (xm, ym)
        for x, y in g:
            sxx += (x - xm) ** 2
            sxy += (x - xm) * (y - ym)
    require(sxx > 0, 'comments do not span enough time to fit a rate')
    slope = sxy / sxx
    return slope, {s: ym - slope * xm for s, (xm, ym) in means.items()}


def holdout(points, segments):
    """Two-fold hold-out: fit on alternate comments, predict the others."""
    folds = (points[0::2], points[1::2])
    errors = []
    for train, test in ((folds[0], folds[1]), (folds[1], folds[0])):
        covered = {s for _, s, _ in train}
        require(all(s in covered for s in range(segments)),
                'too few comments to hold out: every continuous stretch needs at least two')
        slope, icpt = fit(train)
        errors += [slope * x + icpt[s] - y for x, s, y in test]
    return errors


class Model:
    def __init__(self, origin, slope, intercepts, timeline):
        self.origin, self.slope, self.intercepts, self.timeline = origin, slope, intercepts, timeline

    def value(self, x, segment):
        return self.intercepts[segment] + self.slope * x

    def map_tick(self, tick):
        x, s, _ = self.timeline.index_of(tick)
        return self.origin + round(self.value(x, s))

    def map_index(self, k):
        return self.origin + round(self.value(k, self.timeline.segment_of_index(k)))


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
    for s, e in zip(tl.starts, tl.ends):
        k = s
        while k < e - 1:
            k2 = min(e - 1, k + chunk)
            rates.append((k, k2, (ts[k2] - ts[k]) / (P * (k2 - k))))
            k = k2
    runs = []
    for k, k2, r in rates:
        if runs and abs(r - runs[-1]['rate']) < 20e-6 and runs[-1]['_end'] == k:
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
                max_rate_error_ppm=max(abs(r - 1) for _, _, r in rates) * 1e6)


def label(rate):
    if abs(rate - (1 - RAIL)) < 50e-6:
        return 'railed at -6.4% (clock ahead of grandmaster, slewing down)'
    if abs(rate - (1 + RAIL)) < 50e-6:
        return 'railed at +6.4% (clock behind grandmaster, slewing up)'
    if abs(rate - 1) < 50e-6:
        return 'running at grandmaster rate'
    return 'slewing (rate between rail and lock)'


def build_model(device, files, reference_nev, settings):
    """Fit the model for one device. Returns (model, analysis dict); raises RepairError on failure."""
    nev = read_nev(files['nev'])
    nsx = [read_nsx_header(files[e]) for e in sorted(files) if e.startswith('ns')]
    primary = min(nsx, key=lambda h: (h['period'], -h['packets'])) if nsx else None
    if primary:
        timeline = SampleTimeline(read_nsx_timestamps(primary), primary['path'].name)
    else:
        timeline = PhcTimeline(nev['ts'][0])
    pairs, pair_stats = matched_pairs(nev, reference_nev)
    require(len(pairs) >= settings['min_pairs'],
            f'{device}: only {len(pairs)} unambiguous comments shared with the reference '
            f'(need {settings["min_pairs"]})')
    origin = pairs[0]['reference_ts']
    points, worst_extrap = [], 0
    for p in pairs:
        x, s, ex = timeline.index_of(p['target_ts'])
        worst_extrap = max(worst_extrap, ex)
        points.append((x, s, float(p['reference_ts'] - origin)))
    per_segment = [sum(1 for _, s, _ in points if s == seg) for seg in range(timeline.segments)]
    require(all(c >= settings['min_pairs_per_segment'] for c in per_segment),
            f'{device}: comments per continuous stretch {per_segment}; each needs '
            f'{settings["min_pairs_per_segment"]} (a sample gap without comments on both sides cannot be bridged)')
    slope, intercepts = fit(points)
    model = Model(origin, slope, intercepts, timeline)
    residuals = [model.value(x, s) - y for x, s, y in points]
    ho = holdout(points, timeline.segments)
    analysis = dict(
        files={e: str(files[e]) for e in sorted(files)},
        primary_nsx=primary['path'].name if primary else None,
        model='sample-index' if primary else 'PHC-linear (no NSx)',
        comment_pairs=pair_stats,
        continuous_stretches=timeline.segments,
        discontinuities=timeline.discontinuities[:20],
        comment_pairs_per_stretch=per_segment,
        fitted_sample_period_ns=slope if primary else None,
        nominal_sample_period_ns=primary['nominal_period_ns'] if primary else None,
        sample_clock_vs_nominal_ppm=(primary['nominal_period_ns'] / slope - 1) * 1e6 if primary else None,
        reference_ns_per_phc_ns=None if primary else slope,
        intercepts_ns={str(k): v for k, v in intercepts.items()},
        origin_reference_tick=str(origin),
        fit_residual=summarize_errors(residuals),
        holdout=summarize_errors(ho),
        comment_extrapolation_ms=worst_extrap / 1e6,
        comment_table=[dict(text=p['text'][:80], reference_ts=str(p['reference_ts']), target_ts=str(p['target_ts']),
                            corrected_minus_reference_us=round(r / 1e3, 3)) for p, r in zip(pairs, residuals)],
    )
    analysis['phc'] = phc_diagnostics(model, primary, settings)
    analysis['grandmaster_check_raw'] = grandmaster_check(nev)
    return model, analysis, nev, nsx, primary


def gate(analysis, settings):
    issues = []
    ho = analysis['holdout']
    if abs(ho['mean_ms']) > settings['max_bias_ms']:
        issues.append(f'hold-out bias {ho["mean_ms"]:+.3f} ms exceeds {settings["max_bias_ms"]} ms')
    if ho['rms_ms'] > settings['max_rms_ms']:
        issues.append(f'hold-out RMS {ho["rms_ms"]:.3f} ms exceeds {settings["max_rms_ms"]} ms')
    if ho['max_abs_ms'] > settings['max_abs_ms']:
        issues.append(f'hold-out worst error {ho["max_abs_ms"]:.3f} ms exceeds {settings["max_abs_ms"]} ms')
    ppm = analysis['sample_clock_vs_nominal_ppm']
    if ppm is not None and abs(ppm) > settings['max_period_ppm']:
        issues.append(f'fitted sample clock is {ppm:+.1f} ppm from nominal (limit {settings["max_period_ppm"]})')
    return issues


def is_affected(analysis, settings):
    phc = analysis['phc']
    off = phc['offset_at_first_sample_ms']
    return (phc['max_rate_error_ppm'] > settings['affected_rate_ppm'] or
            (off is not None and abs(off) > settings['affected_offset_ms']))


# --------------------------------------------------------------------------- writers

def nsx_new_timestamps(model, h, primary, old_ts=None):
    if primary:
        return array('Q', (model.map_index(k) for k in range(h['packets'])))
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


def repair_session(base, devices, out_dir, settings, reference='auto', only=None, force=False, write=True, log=print,
                   skip=None):
    report = dict(session=base, tool=f'TimestampRepair {VERSION}', settings=settings, devices={})
    nevs = {d: read_nev(f['nev']) for d, f in devices.items() if 'nev' in f}
    ref, checks = choose_reference(devices, reference, nevs)
    report['reference'] = dict(device=ref, grandmaster_checks=checks)
    if not checks[ref]['consistent']:
        report['reference']['warning'] = 'reference comment times are not consistent with the grandmaster'
    log(f'[{base}] reference: {ref}')
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
            model, analysis, nev, nsx, primary = build_model(dev, devices[dev], nevs[ref], settings)
        except RepairError as exc:
            entry.update(status='blocked', issues=[str(exc)])
            log(f'[{base}] {dev}: BLOCKED - {exc}')
            continue
        entry.update(analysis)
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
        three_way, exceeded = three_way_check(nevs, ref, dev, model, settings, skip)
        entry['three_way_comment_check'] = three_way
        if exceeded:
            issue = ('comments shared by every device stay misaligned after correction: '
                     + '; '.join(exceeded) + '. A clock offset cancels here, so this points to '
                     'lost samples or a mispaired comment')
            entry.update(status='blocked', issues=[issue])
            log(f'[{base}] {dev}: BLOCKED - {issue}')
            continue
        plan = nev_plan(model, nev, nevs[ref], settings)
        entry['nev_plan'] = plan[2]
        if plan[2]['worst_extrapolation_ms'] > settings['max_extrapolation_s'] * 1000:
            entry.update(status='blocked', issues=[
                f'NEV events up to {plan[2]["worst_extrapolation_ms"]:.1f} ms outside the NSx sample range'])
            continue
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
            log(f'[{base}] {dev}: wrote {dest.name}; post-repair comment error '
                f'{entry["post_repair"]["comment_pairs"]["mean_ms"]:+.3f} ms mean, '
                f'{entry["post_repair"]["comment_pairs"]["max_abs_ms"]:.3f} ms max')
        except (RepairError, OSError) as exc:
            entry.update(status='failed', issues=[str(exc)])
            log(f'[{base}] {dev}: FAILED - {exc}')
            for p in out_dir.glob('*.partial'):
                p.unlink()
    return report


def repair_folder(data_dir, out_dir, reference='auto', only=None, force=False, write=True, log=print, skip=None,
                  **overrides):
    settings = dict(DEFAULTS, **overrides)
    data_dir, out_dir = Path(data_dir).resolve(), Path(out_dir).resolve()
    require(data_dir != out_dir, 'output folder must differ from the data folder')
    sessions = discover(data_dir)
    require(sessions, f'no NSP-/HubN- NSx or NEV files found in {data_dir}')
    reports = []
    for base, devs in sessions.items():
        try:
            reports.append(repair_session(base, devs, out_dir, settings, reference, only, force, write, log, skip))
        except RepairError as exc:
            reports.append(dict(session=base, tool=f'TimestampRepair {VERSION}', settings=settings,
                                devices={}, error=str(exc)))
            log(f'[{base}] SESSION NOT EVALUATED - {exc}')
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / 'timestamp_repair_report.json'
    require(not path.exists(), f'{path} already exists')
    path.write_text(json.dumps(dict(data_folder=str(data_dir), sessions=reports), indent=2, default=str) + '\n')
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
