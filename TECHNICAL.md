# Method and validation

## Reference and scope

Hub 1 is the ground truth for this workflow. Hub 1 and the NSP are treated as the known synchronized reference system; the NSP is not required as a separate input. Only a copy of hub 2 is corrected. The supplied hub 1 and hub 2 part-1 NS6 files and their full NEVs were used for validation.

The runtime package contains only `run_repair.py`, `repair_time.py`, `README.md`, and this document. The obsolete constant-offset workflow and unrelated commands have been removed. The launcher invokes `repair-nsx` logic with the configured limits.

Supported inputs are BRSMPGRP 3.0 NSx recordings with a timestamp for every sample, a timestamp resolution of 1,000,000,000 ticks/s, and equal nominal sample periods between hubs. At least 30,001 samples per channel are required. NEV validation supports BREVENTS 3.0 ANSI comments.

## Sample counts and possible loss

Counts represent samples per channel, not the product of samples and channels. Hub 1's 256 channels and hub 2's 128 channels therefore do not invalidate the comparison.

The script reports the signed count difference, its absolute value and percentage, and its equivalent duration at the nominal sample rate. It blocks writing when the absolute difference exceeds `MAX_SAMPLE_DIFFERENCE`, which defaults to 30 samples. That is a provisional 1 ms allowance at 30 kHz. Both inputs must represent the same recording interval. A large difference can indicate loss or different recording boundaries; it is not automatically attributed to one device.

Packet boundaries, payload lengths and timestamp ordering are also checked. Inter-sample intervals must be greater than half and less than 1.5 times the file's measured mean interval. A failure blocks writing as possible missing samples, a gap, or a clock discontinuity. These checks cannot prove that no acquisition loss occurred: equal losses, small timestamp discontinuities or other errors can escape this screening.

## Effective rate and correction

For each NSx file, the script subtracts every consecutive pair of timestamps, records the minimum and maximum interval, and sums the intervals. With N samples per channel:

`mean interval = sum of all adjacent timestamp differences / (N - 1)`

For a continuous recording, this is mathematically equal to `(last timestamp - first timestamp) / (N - 1)`.

`effective sample rate = 1,000,000,000 / mean interval`

The effective rate means samples per second on the file's recorded timestamp axis. It is not a separate measurement of the physical ADC or clock oscillator. The nominal sample rate is obtained from the header period.

The correction multiplier is:

`scale = hub1 mean interval / hub2 mean interval`

Each hub 2 timestamp is mapped as:

`corrected tick = hub1 first tick + round((hub2 tick - hub2 first tick) * scale)`

The calculation uses exact fractions and integer timestamps to avoid losing precision in large nanosecond values. It retains hub 1's raw timestamp domain and copies hub 1's calendar-origin field. It does not independently establish the absolute calendar accuracy of the reference.

First-sample simultaneity is the start assumption. It must be valid for the chosen inputs. Later split parts must not be independently realigned because equal-sized splits need not start at the same physical instant. Use full recordings or the matching first parts.

Departure from a constant rate is checked against the configured tolerance at every 30,000-sample checkpoint and the file endpoints. This is a sampled linearity check, not proof that no transient occurred between checkpoints. No rate is hard-coded to 6.4%. `EXPECTED_RATE_DIFFERENCE_PERCENT` is report-only metadata. It does not enter the rate calculation, timestamp transform or acceptance gates.

## Interpreting the percentages

The report distinguishes:

| Field | Definition |
| --- | --- |
| `expected_effective_rate_increase_percent` | Expected increase from the configuration, default 6.4% |
| `expected_effective_rate_hz` | Hub 1 measured effective rate multiplied by `1 + expected percent / 100` |
| `actual_minus_expected_percentage_points` | Measured effective-rate increase minus expected increase |
| `hub2_elapsed_time_shortfall_percent` | `100 * (1 - hub2 mean interval / hub1 mean interval)` |
| `hub2_effective_sample_rate_increase_percent` | `100 * (hub2 effective rate / hub1 effective rate - 1)` |

An elapsed-time ratio near 0.936 means approximately 6.4% compression. Its reciprocal is approximately 1.068376, giving approximately 6.84% more samples per recorded timestamp-second. These are different denominators describing the same observed file behavior, not conflicting physical-clock diagnoses.

## NEV validation

The NSx-derived model is fixed before either NEV is read. Neither rate nor offset is fitted to comments.

A validation pair requires exactly identical stored comment text and character set, including any date/time text. Only flag-0 comments are considered, so flag-1 companion copies do not duplicate the evidence. Repeated text is ambiguous and excluded; files are not paired by comment row number. The report lists all exact matches, excluded ambiguous keys and unmatched comments. At least three unambiguous pairs are required.

For each pair, the report provides reference and target packet indexes, comment text, original ticks, predicted corrected target ticks, and the signed discrepancy in nanoseconds. The maximum absolute discrepancy must meet `TOLERANCE_MS`. Equal text supports correspondence between software comments but does not prove identical hardware event-capture latency.

Comments outside the supplied NSx interval are marked. They check extrapolation of the model rather than directly validating the unprovided continuous samples. The supplied full NEVs cover about 199 seconds between matching comments; the supplied NS6 parts cover about 25 seconds.

## Output verification

The source is never modified. A corrected file is staged only after the count, interval, linearity and comment checks pass. The staged file is then read independently. Every byte outside the timestamp fields and the deliberately copied calendar origin must match the source. Sample totals are preserved. Every output timestamp must agree with the exact mapping within half a nanosecond and timestamps must remain increasing. Source and output SHA-256 hashes are recorded, and the source hash is rechecked after writing.

The source hub 2 NEV remains unchanged. The report applies the correction mathematically to its comment timestamps for validation; this tool does not create a rewritten NEV or update TOC/NIX companion files.

## Supplied-data results

| Measurement | Result |
| --- | --- |
| Hub 1 samples per channel | 756,107 |
| Hub 2 samples per channel | 756,109 |
| Sample difference | 2 samples (0.000265%) |
| Hub 1 mean timestamp interval | 33333.317141 ns |
| Hub 2 mean timestamp interval | 31200.004170 ns |
| Hub 1 minimum / maximum interval | 32320 / 34320 ns |
| Hub 2 minimum / maximum interval | 30139 / 32311 ns |
| Hub 1 effective rate | 30000.014573 samples/timestamp-second |
| Expected hub 2 effective-rate increase | 6.400000% |
| Expected hub 2 effective rate | 31920.015506 samples/timestamp-second |
| Measured hub 2 effective rate | 32051.277767 samples/timestamp-second |
| Measured hub 2 effective-rate increase | 6.837541% |
| Actual minus expected increase | 0.437541 percentage points |
| Measured hub 2 elapsed-time compression | 6.399942% |
| Correction multiplier | 1.068375406588174 |
| Unique exact comment matches | 43 |
| Maximum comment discrepancy | 1.233414 ms |
| Non-timing bytes preserved | True |


The test run used a 30-sample count limit and 2 ms timing tolerance. Both are explicit analysis settings rather than approved experiment acceptance criteria. A 1 ms comment tolerance would fail these data. No NEV-derived adjustment was used to reduce the residual error.

All nine targeted automated tests passed. They cover known rates, count-limit boundaries, loss blocking output, gap detection, changing-rate detection, exact comment matching despite packet reordering, ambiguous-text exclusion, independence of the model from NEV validation and the expected-rate setting, and detection of sample-data corruption. The runtime package omits development tests and prior example reports.
