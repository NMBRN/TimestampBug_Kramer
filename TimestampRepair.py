#!/usr/bin/env python3
"""Repair NSx clock rates using NSx samples; validate against matched NEV comments. Python 3.10+."""
import argparse
import hashlib
import json
import mmap
import struct
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value):
    return Fraction(str(value))


def origin_seconds(raw):
    y, mo, _, d, h, mi, s, ms = struct.unpack('<8H', raw)
    require(ms < 1000, 'Invalid header milliseconds')
    dt = datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=timezone.utc)
    delta = dt - EPOCH
    return Fraction(delta.days * 86400 + delta.seconds) + Fraction(delta.microseconds, 1000000)


def utc(seconds):
    whole = seconds.numerator // seconds.denominator
    fraction_ns = int((seconds - whole) * 1000000000)
    return (EPOCH + timedelta(seconds=whole)).strftime('%Y-%m-%dT%H:%M:%S') + f'.{fraction_ns:09d}Z'


def header(path):
    path = Path(path).resolve()
    with path.open('rb') as f:
        raw = f.read(336)
    require(len(raw) >= 314, f'{path.name}: truncated or unsupported header')
    magic = raw[:8]
    version = tuple(raw[8:10])
    if magic in (b'NEURALEV', b'BREVENTS'):
        require(len(raw) >= 336, 'Truncated NEV header')
        require((magic == b'NEURALEV' and version == (2, 3)) or
                (magic == b'BREVENTS' and version == (3, 0)),
                'Supported NEV formats: NEURALEV 2.3 and BREVENTS 3.0')
        kind, origin_at = 'nev', 28
        header_size, packet_size, resolution = struct.unpack_from('<III', raw, 12)
        extended_count = struct.unpack_from('<I', raw, 332)[0]
        require(header_size == 336 + 32 * extended_count, 'Unexpected NEV header layout')
        channels, period = 0, 0
    elif magic in (b'NEURALCD', b'BRSMPGRP'):
        require((magic == b'NEURALCD' and version in ((2, 2), (2, 3))) or
                (magic == b'BRSMPGRP' and version == (3, 0)),
                'Supported NSx formats: NEURALCD 2.2/2.3 and BRSMPGRP 3.0')
        kind, origin_at = 'nsx', 294
        header_size = struct.unpack_from('<I', raw, 10)[0]
        period, resolution = struct.unpack_from('<II', raw, 286)
        channels = struct.unpack_from('<I', raw, 310)[0]
        packet_size = 0
        require(channels > 0 and period > 0, 'Invalid NSx channel count or period')
        require(header_size == 314 + 66 * channels, 'Unexpected NSx header layout')
    else:
        raise ValueError(f'{path.name}: unsupported file ID {magic!r}')
    width = 8 if magic in (b'BREVENTS', b'BRSMPGRP') else 4
    size = path.stat().st_size
    require(resolution > 0 and header_size <= size, 'Invalid header size or timestamp resolution')
    if kind == 'nev':
        require(packet_size >= width + 6 and (size - header_size) % packet_size == 0,
                'Truncated NEV data or invalid packet size')
    if kind == 'nsx':
        with path.open('rb') as f:
            for channel in range(channels):
                f.seek(314 + 66 * channel)
                require(f.read(2) == b'CC', 'Unsupported NSx extended header')
    # Validate the stored origin even when the caller selects Unix timestamps.
    origin = origin_seconds(raw[origin_at:origin_at + 16])
    return dict(path=path, kind=kind, magic=magic.decode(), version='.'.join(map(str, version)),
                origin=origin, origin_at=origin_at, header_size=header_size,
                resolution=resolution, width=width, fmt='<Q' if width == 8 else '<I',
                packet_size=packet_size, channels=channels, period=period, size=size)


def packets(h):
    """Yield (packet index, byte offset, ticks, sample count or NEV ID)."""
    with h['path'].open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        pos, index, previous_end = h['header_size'], 0, None
        while pos < h['size']:
            if h['kind'] == 'nev':
                tick = struct.unpack_from(h['fmt'], data, pos)[0]
                value = struct.unpack_from('<H', data, pos + h['width'])[0]
                next_pos = pos + h['packet_size']
                end = tick * 30000
            else:
                need = 1 + h['width'] + 4
                require(pos + need <= h['size'] and data[pos] == 1,
                        f'{h["path"].name}: invalid NSx packet at byte {pos}')
                tick = struct.unpack_from(h['fmt'], data, pos + 1)[0]
                value = struct.unpack_from('<I', data, pos + 1 + h['width'])[0]
                require(value > 0, 'Empty NSx packet; finalized recordings are required')
                next_pos = pos + need + value * h['channels'] * 2
                require(next_pos <= h['size'], 'Truncated NSx sample data')
                end = tick * 30000 + (value - 1) * h['period'] * h['resolution']
            require(previous_end is None or tick * 30000 >= previous_end,
                    f'{h["path"].name}: timestamps decrease/overlap at packet {index}; '
                    'clock reset, wrap or unsupported timing requires separate reconstruction')
            if h['kind'] == 'nsx' and previous_end is not None:
                require(tick * 30000 > previous_end, 'Duplicate NSx sample timestamps')
            previous_end = end
            yield index, pos, tick, value
            pos, index = next_pos, index + 1
        require(index > 0, f'{h["path"].name}: no data packets')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for data in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def copy_count(source, dest, count):
    while count:
        data = source.read(min(count, 8 * 1024 * 1024))
        require(data, 'Unexpected end of file')
        dest.write(data)
        count -= len(data)


def describe(h):
    return dict(path=str(h['path']), file_id=h['magic'], version=h['version'],
                timestamp_resolution=h['resolution'], timestamp_bits=h['width'] * 8,
                header_origin_utc=utc(h['origin']))


def nsx_rate(h):
    """Estimate a clock's tick interval from NSx samples only."""
    require(h['kind'] == 'nsx' and h['magic'] == 'BRSMPGRP' and h['resolution'] == 1000000000,
            'NSx rate repair requires BRSMPGRP 3.0 with nanosecond timestamps')
    first = previous = last = None
    shortest = longest = None
    interval_sum = 0
    knots = []
    count = 0
    for _, _, tick, n in packets(h):
        require(n == 1, 'NSx rate repair requires one timestamped packet per sample')
        if first is None:
            first = tick
        else:
            interval = tick - previous
            interval_sum += interval
            shortest = interval if shortest is None else min(shortest, interval)
            longest = interval if longest is None else max(longest, interval)
        if count % 30000 == 0:
            knots.append((count, tick))
        previous = last = tick
        count += 1
    require(count >= 30001, 'Use at least 30,001 samples to estimate a clock rate')
    knots.append((count - 1, last))
    step = Fraction(interval_sum, count - 1)
    continuous = shortest * 2 > step and longest * 2 < step * 3
    deviation = max(abs(Fraction(t - first) - i * step) for i, t in knots)
    return dict(first=first, last=last, samples=count, step=step,
                linearity_error_ns=deviation, shortest_interval_ns=shortest,
                longest_interval_ns=longest, continuous=continuous)


def nsx_model(reference, target):
    # This function deliberately accepts no NEV data.
    require(reference['period'] == target['period'], 'Reference and target sample periods differ')
    ref, dst = nsx_rate(reference), nsx_rate(target)
    scale = ref['step'] / dst['step']
    return dict(reference=ref, target=dst, scale=scale,
                reference_anchor=ref['first'], target_anchor=dst['first'])


def sample_count_check(model, maximum, period):
    ref, dst = model['reference'], model['target']
    difference = dst['samples'] - ref['samples']
    return dict(reference_samples_per_channel=ref['samples'],
                target_samples_per_channel=dst['samples'],
                signed_difference_samples=difference,
                absolute_difference_samples=abs(difference),
                difference_percent=100 * abs(difference) / ref['samples'],
                difference_at_nominal_rate_ms=abs(difference) * period / 30,
                maximum_allowed_samples=maximum,
                reference_intervals_continuous=ref['continuous'],
                target_intervals_continuous=dst['continuous'],
                passed=abs(difference) <= maximum and ref['continuous'] and dst['continuous'],
                note='Possible-loss screening, not proof of no acquisition loss. Equal losses in both '
                     'files or different recording boundaries cannot be resolved from totals alone.')


def mapped_tick(tick, model):
    return model['reference_anchor'] + round((tick - model['target_anchor']) * model['scale'])


def unique_comments(h):
    """Read exact flag-0 comments without assuming global NEV packet order."""
    require(h['kind'] == 'nev' and h['magic'] == 'BREVENTS' and h['resolution'] == 1000000000,
            'Comment validation requires BREVENTS 3.0 with nanosecond timestamps')
    found, repeated = {}, set()
    with h['path'].open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        for index, pos in enumerate(range(h['header_size'], h['size'], h['packet_size'])):
            tick, pid = struct.unpack_from('<QH', data, pos)
            if pid != 65535:
                continue
            require(h['packet_size'] >= 17, 'Comment packet is too short')
            charset, flag = struct.unpack_from('<BB', data, pos + 10)
            if flag != 0:
                continue  # Do not count the companion flag-1 comment as another checkpoint.
            require(charset == 0, 'Only ANSI comment text is supported for exact matching')
            text_bytes = bytes(data[pos + 16:pos + h['packet_size']]).split(b'\0', 1)[0]
            key = (charset, text_bytes)
            if key in found:
                repeated.add(key)
            found[key] = dict(packet=index, ticks=tick, text=text_bytes.decode('latin-1'))
    for key in repeated:
        del found[key]  # Repeated text is ambiguous; never pair it by occurrence number.
    return found, len(repeated)


def validate_comments(reference, target, model):
    ref, ref_ambiguous = unique_comments(reference)
    dst, dst_ambiguous = unique_comments(target)
    keys = sorted(ref.keys() & dst.keys(), key=lambda k: ref[k]['ticks'])
    require(len(keys) >= 3, 'Fewer than three unambiguous, identical comments are available')
    rows = []
    for key in keys:
        a, b = ref[key], dst[key]
        corrected = mapped_tick(b['ticks'], model)
        rows.append(dict(reference_packet=a['packet'], target_packet=b['packet'],
                         text=a['text'], reference_ticks=str(a['ticks']),
                         target_ticks=str(b['ticks']), predicted_target_ticks=str(corrected),
                         error_ns=corrected - a['ticks'],
                         outside_nsx_fit_interval=not (model['target']['first'] <= b['ticks'] <=
                                                       model['target']['last'])))
    errors = [row['error_ns'] for row in rows]
    return dict(matched=len(rows), reference_unmatched=len(ref) - len(keys),
                target_unmatched=len(dst) - len(keys), reference_ambiguous=ref_ambiguous,
                target_ambiguous=dst_ambiguous, minimum_error_ms=min(errors) / 1000000,
                maximum_error_ms=max(errors) / 1000000,
                maximum_absolute_error_ms=max(map(abs, errors)) / 1000000,
                validation_span_seconds=(ref[keys[-1]]['ticks'] - ref[keys[0]]['ticks']) / 1e9,
                checkpoints=rows)


def write_nsx_rate(h, reference, output, model):
    with h['path'].open('rb') as src, reference['path'].open('rb') as ref, Path(output).open('xb') as dst:
        # Preserve the target header except for the reference calendar origin.
        raw = bytearray(src.read(h['header_size']))
        ref.seek(reference['origin_at'])
        raw[h['origin_at']:h['origin_at'] + 16] = ref.read(16)
        dst.write(raw)
        for _, pos, tick, n in packets(h):
            corrected = mapped_tick(tick, model)
            require(0 <= corrected < 2**64, 'Corrected timestamp overflows uint64')
            src.seek(pos)
            raw = bytearray(src.read(13))
            struct.pack_into('<Q', raw, 1, corrected)
            dst.write(raw)
            copy_count(src, dst, n * h['channels'] * 2)


def verify_nsx_rate(h, reference, output, model):
    new = header(output)
    require(new['size'] == h['size'], 'Output size changed')
    with h['path'].open('rb') as src, Path(output).open('rb') as dst, reference['path'].open('rb') as ref:
        a, b = bytearray(src.read(h['header_size'])), dst.read(new['header_size'])
        ref.seek(reference['origin_at'])
        a[h['origin_at']:h['origin_at'] + 16] = ref.read(16)
        require(a == b, 'A non-origin header byte changed')
        previous = None
        first = last = None
        samples = 0
        for _, pos, tick, n in packets(h):
            src.seek(pos)
            dst.seek(pos)
            length = 13 + 2 * h['channels'] * n
            original, corrected = src.read(length), dst.read(length)
            require(len(corrected) == length, 'Truncated output')
            require(original[:1] == corrected[:1] and original[9:] == corrected[9:],
                    'Sample values or packet metadata changed')
            output_tick = struct.unpack_from('<Q', corrected, 1)[0]
            # Exact rational comparison is independent of mapped_tick's rounding operation.
            exact = model['reference_anchor'] + (tick - model['target_anchor']) * model['scale']
            require(abs(Fraction(output_tick) - exact) <= Fraction(1, 2), 'Incorrect mapped timestamp')
            require(previous is None or output_tick > previous, 'Output timestamps do not increase')
            if first is None:
                first = output_tick
            last = previous = output_tick
            samples += n
    return dict(samples=samples, first_timestamp_ns=str(first), last_timestamp_ns=str(last),
                duration_seconds=(last - first) / 1e9, sha256=digest(output),
                non_timing_bytes_identical=True, timestamp_mapping_verified_to_half_tick=True)


def repair_nsx(args):
    reference, target = header(args.reference_nsx), header(args.nsx)
    require(reference['path'] != target['path'], 'Reference and target NSx files must differ')
    tolerance_ns = number(args.tolerance_ms) * 1000000
    require(tolerance_ns > 0, 'Tolerance must be positive')
    expected_rate_percent = number(args.expected_rate_difference_percent)
    max_difference = args.max_sample_difference
    require(max_difference >= 0, 'Maximum sample difference must be nonnegative')
    model = nsx_model(reference, target)
    # Freeze the model before reading either NEV. NEVs can only pass/fail validation.
    validation = validate_comments(header(args.reference_nev), header(args.target_nev), model)
    issues = []
    sample_check = sample_count_check(model, max_difference, reference['period'])
    if not sample_check['passed']:
        issues.append('Sample totals or packet intervals indicate possible data loss or different recording lengths')
    if max(abs(row['error_ns']) for row in validation['checkpoints']) > tolerance_ns:
        issues.append('Like-for-like comment discrepancy exceeds the selected tolerance')
    for label in ('reference', 'target'):
        error = model[label]['linearity_error_ns']
        if label == 'target':
            error *= model['scale']
        if error > tolerance_ns:
            issues.append(f'{label} NSx shows a changing rate exceeding tolerance')
    report = dict(status='blocked' if issues else 'checked', method='NSx-only rate and first-sample anchor',
                  reference=describe(reference), target=describe(target),
                  first_sample_alignment_assumed=True,
                  anchor_note='First samples are placed at the same instant; this is a recording-start '
                              'assumption, not a measured cross-device latency.',
                  scale_exact=str(model['scale']), scale=float(model['scale']),
                  reference_anchor_ns=str(model['reference_anchor']),
                  target_anchor_ns=str(model['target_anchor']), tolerance_ms=float(number(args.tolerance_ms)),
                  nev_used_for_fitting=False, sample_count_check=sample_check,
                  rate_comparison=dict(
                      expected_effective_rate_increase_percent=float(expected_rate_percent),
                      expected_effective_rate_hz=float((Fraction(1000000000) / model['reference']['step']) *
                                                       (1 + expected_rate_percent / 100)),
                      actual_minus_expected_percentage_points=float((model['scale'] - 1) * 100 - expected_rate_percent),
                      expected_value_used_for_fitting=False,
                      hub2_elapsed_time_shortfall_percent=float((1 - 1 / model['scale']) * 100),
                      hub2_effective_sample_rate_increase_percent=float((model['scale'] - 1) * 100)),
                  nsx={}, validation=validation, issues=issues)
    for label in ('reference', 'target'):
        v = model[label]
        report['nsx'][label] = dict(samples=v['samples'], duration_seconds=(v['last'] - v['first']) / 1e9,
                                   mean_tick_interval_ns=float(v['step']),
                                   timestamp_intervals=v['samples'] - 1,
                                   minimum_tick_interval_ns=v['shortest_interval_ns'],
                                   maximum_tick_interval_ns=v['longest_interval_ns'],
                                   nominal_sample_rate_hz=30000 / reference['period'],
                                   effective_sample_rate_hz=float(Fraction(1000000000) / v['step']),
                                   continuous_packet_intervals=v['continuous'],
                                   linearity_error_ms=float(v['linearity_error_ns'] / 1000000))
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    if args.write and not issues:
        try:
            with tempfile.TemporaryDirectory(prefix='.repair-', dir=out) as temporary:
                staged = Path(temporary) / ('Hub2.corrected' + target['path'].suffix)
                before = digest(target['path'])
                write_nsx_rate(target, reference, staged, model)
                report['file_verification'] = verify_nsx_rate(target, reference, staged, model)
                require(before == digest(target['path']), 'Source changed during correction')
                report['original_sha256'] = before
                destination = out / staged.name
                require(not destination.exists(), 'Output already exists')
                staged.rename(destination)
                report['output'] = str(destination)
            report['status'] = 'repaired'
        except Exception as exc:
            report['status'] = 'failed'
            report['issues'].append(str(exc))
            (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            raise
    (out / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(status=report['status'], scale=report['scale'],
                         matched_comments=validation['matched'],
                         max_comment_error_ms=validation['maximum_absolute_error_ms'],
                         report=str(out / 'report.json'), issues=issues), indent=2))
    return 2 if issues else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    rate = commands.add_parser('repair-nsx', help='Estimate rate from NSx only; validate exact NEV comments')
    rate.add_argument('--reference-nsx', required=True, help='Hub 1 NSx with the same recording start')
    rate.add_argument('--nsx', required=True, help='Affected hub 2 NSx')
    rate.add_argument('--reference-nev', required=True, help='Hub 1 NEV, validation only')
    rate.add_argument('--target-nev', required=True, help='Hub 2 NEV, validation only')
    rate.add_argument('--tolerance-ms', required=True, help='Maximum comment error and rate-linearity error')
    rate.add_argument('--expected-rate-difference-percent', default='6.4',
                      help='Expected hub 2 effective-rate increase, report only (default: 6.4)')
    rate.add_argument('--max-sample-difference', type=int, default=30,
                      help='Maximum allowed sample-count difference per channel (default: 30)')
    rate.add_argument('--output', required=True, help='New directory for report and optional corrected copy')
    rate.add_argument('--write', action='store_true', help='Create a corrected copy after validation passes')
    rate.set_defaults(run=repair_nsx)
    args = parser.parse_args()
    try:
        return args.run(args) or 0
    except (ValueError, OSError, OverflowError, struct.error, KeyError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
