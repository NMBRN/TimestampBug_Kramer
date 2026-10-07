# Method and validation

This describes `TimestampRepair.py` version 3.0.0 as run by `run_repair.py`. Setting names refer to `LIMITS` in `run_repair.py` and to `TimestampRepair.DEFAULTS`; defaults are given in parentheses.

## Problem

A Gemini hub stamps its NSx samples and NEV events (spikes, digital events, comments) with its PTP hardware clock (PHC), which the Precision Time Protocol keeps aligned to a network grandmaster. With `step_threshold 0`, a hub's PHC may step only once. Any later offset is slewed out at the Ethernet MAC's frequency-adjustment limit of ±6.4% (Cadence GEM, `max_adj` 64,000,000 ppb). After a large error the PHC therefore runs 6.4% fast or slow until the offset is gone, which can take days. The hub's ADC sample clock does not depend on the PHC and is not affected.

In the 2026-07-22 sessions, Hub2's PHC was 43,251 s (12.0 h) ahead of Hub1 at the start of the first session and ran at 0.936 times Hub1's rate in every session.

## What is measured and what is assumed

The correction does not apply a 6.4% factor. The rail holds the PHC at exactly 6.4% from its own crystal, and that crystal is itself off by a fraction of a ppm from the grandmaster. Against Hub1, Hub2's PHC ran between 0.47 ppm slow and 0.22 ppm fast of exactly 0.936. Every quantity the correction uses is measured from each session's own files:

- the target's sample period on the reference clock, `P`;
- the offset of each continuous stretch of samples, `a[s]`;
- the number of samples lost in each timestamp gap;
- the sample index of every PHC-stamped event, by interpolation between the target's own sample timestamps.

The PHC's rate, the reference's own sample interval and the rail are also measured, but only reported.

Fixed values in the code do not enter the corrected timestamps. The nominal sample period from the NSx header (period field × 1/30,000 s) is used only for the `max_period_ppm` sanity bound and for reporting ppm. The 6.4% rail is used only to label the measured PHC rate. The remaining constants are statistical (the Huber tuning constant, the MAD-to-SD factor) or are limits listed in `README.md`.

The model does rest on assumptions, listed with their checks under Assumptions and limitations. The central one is that both devices stamp a given comment at the same moment on average.

## Model

The target's sample count does not depend on its PHC, so time is modelled as a function of the sample index of its highest-rate NSx stream:

```
reference_time(k) = origin + a[s] + P * v(k)
```

| Symbol | Meaning |
| --- | --- |
| `k` | Sample (packet) index in the primary NSx file |
| `v(k)` | The same index with dropped samples counted, so it advances by one per sample period of the ADC |
| `P` | The target's sample period, in reference-clock nanoseconds |
| `s` | The continuous stretch containing sample `k`; there is one stretch unless the timestamps show a break that is not dropped samples |
| `a[s]` | Offset of stretch `s` |
| `origin` | Earliest reference receipt tick among the matched comments, a constant that keeps the numbers small |

`P` and `a` are estimated from Central comments that both the target and the reference logged; each device stamps a comment with its own receipt time. Every other timestamp the target wrote, in its NEV and its lower-rate NSx streams, is first converted to a sample index of the primary stream and then mapped with the same formula. That conversion interpolates between the target's own sample timestamps, which carry the same PHC as the event, so the PHC's rate cancels over each 31 µs interval whether it is on a rail, in lock or changing. Only target devices get corrected files. The reference, and any device that is skipped or found unaffected, are left as they are.

## Inputs

Files are grouped by session name and device (see `README.md` for the naming). On reading:

- NSx: `BRSMPGRP` version 3.0, 1 ns timestamp resolution, a header of 314 + 66 × channels bytes, data that is a whole number of packets of 13 + 2 × channels bytes, and every packet a data packet holding one sample.
- NEV: `BREVENTS` version 3.0, 1 ns timestamp resolution, a header of 336 + 32 × extended headers bytes, and data that is a whole number of packets.

An NSx file that fails these checks blocks its device, except that the per-packet check of non-primary streams runs while writing and gives `failed`. Every NEV in a session is read before any device is evaluated, so an NEV that fails these checks stops the session.

## Comment pairs and bursts

- Only flag-0 comment packets (packet ID 0xFFFF) are used. A comment's key is its character set plus its text bytes up to the first NUL. Log-style comments carry a date-time stamp in their text, which keeps most keys unique.
- A key that occurs more than once in one device's NEV is dropped for that device.
- Pairs are the keys present in both the target and reference NEVs, ordered by reference receipt time. On each device, the receipt tick is that comment's NEV packet timestamp.
- Comments delivered together share one delivery latency, so they are not independent observations. A comment that reaches the reference within `burst_gap_ms` (2.0 ms) of the previous one, in the same stretch, joins its burst. Each burst becomes one observation: the mean sample index and the mean reference time of its members. In the 2026-07-22 data, gaps between consecutive comments were either under 0.5 ms or over 5 ms, so any threshold between those values gives the same bursts.
- At least `min_bursts` (6) bursts are required, and every stretch needs `min_bursts_per_stretch` (2).

## Sample timeline and dropped samples

- The primary stream is the target's NSx file with the shortest sample period (ties: most samples).
- Every primary timestamp is read. The median interval is taken from about 50,000 evenly spaced intervals. An interval below 0.5 or above 1.5 times the median marks a break. An interval of zero or less (a backward step or a repeated timestamp) stops the device; such a file needs manual segmentation.
- At each break, the gap is converted to a number of samples twice: with the mean interval of the last 30,000 samples before it, and with that of the first 30,000 after it. If both give the same whole number of at least one, each within `gap_tolerance_samples` (0.1) of it, the gap is that many dropped samples. The index `v` skips them and the stretch continues, so the samples on both sides share one offset and no comments are needed to bridge it.
- Any other break starts a new stretch with its own offset. That covers a clock jump, an acquisition restart, a PHC rate change across the gap, or a gap too long to count exactly, where the two estimates disagree. The report gives each break's fitted length (`implied_interval_ns`).
- A PHC tick is converted to a fractional index by linear interpolation between the two primary timestamps around it. Inside a dropped-sample gap the index runs on through the gap. Ticks before the first or after the last sample are extrapolated with the local mean interval, and ticks inside a break between stretches are extrapolated from the nearer side. The largest distance outside the recorded samples is reported as `comment_extrapolation_ms` and `nev_plan.worst_extrapolation_ms`.
- A target with an NEV but no NSx uses the PHC tick itself as the abscissa (`model: PHC-linear (no NSx)`), and `P` is then reference ns per PHC ns.

## Fit

Each burst gives a point: `x` is its mean index, `s` its stretch, and `y` its mean reference receipt minus `origin`. All stretches share one slope and each has its own intercept. The coefficients are a Huber M-estimate. Bursts with residuals beyond 1.345 scale units are down-weighted in proportion to their distance, which keeps 95 % efficiency for normally distributed errors while bounding the influence of a delayed burst. The fit is computed by iteratively reweighted least squares:

```
weighted least squares within stretches:
P    = sum(w (x - mean_w,s(x)) (y - mean_w,s(y))) / sum(w (x - mean_w,s(x))^2)
a[s] = mean_w,s(y) - P * mean_w,s(x)
scale = 1.4826 * median(|r|)              r = fitted - y
w     = 1 if |r| <= 1.345 scale, else 1.345 scale / |r|
```

The iteration stops when the coefficients change the fitted time by less than 0.001 ns across the span of the bursts. Nothing is dropped. `fit.lowest_weights` lists the most down-weighted bursts, and `comment_table` gives every comment's residual and the weight of its burst.

The covariance of the coefficients is Huber's H1 estimate, the default in statsmodels' RLM. It is `factor` × (X'X)^-1, with X the design matrix of stretch indicators and `x`, and

```
factor = kappa^2 * [sum(psi(u)^2) / (n - p)] / mean(psi'(u))^2 * scale^2
kappa  = 1 + (p / n) * (1 - mean(psi'(u))) / mean(psi'(u))
```

where `u = r / scale`, `psi` clips `u` to ±1.345, `n` is the number of bursts and `p` the number of coefficients. `sample_period_se_ns` and `sample_clock_ppm_se` follow from it.

## Uncertainty band

The variance of the corrected time at index `v` in stretch `s` is `factor × (1 / n_s + (v - mean_s(x))^2 / Sxx)`. Here `n_s` is the number of bursts in the stretch and `Sxx` the pooled within-stretch sum of squares of `x`. The band is that standard error times the Student t quantile at 97.5 % with `n - p` degrees of freedom. The quantile is computed exactly, from the regularized incomplete beta function. Within a stretch the band is widest at one of its ends, so it is evaluated at the first and last sample of every stretch (`band_95`). The device is blocked if the largest value exceeds `max_band_ms` (0.10 ms). This gate limits the uncertainty of the corrected timestamps themselves, not the scatter of individual comments, which is mostly delivery-latency noise.

## Step test

A lasting step in the comment residuals is the signature of samples lost without a trace in the timestamps, or of a step in the reference clock. A stall in comment handling displaces one burst and recovers.

A step cannot be read off the residuals of the straight-line fit, because the fit tilts to absorb most of it. In a test case, a 0.3 ms step at mid-recording left only 0.075 ms visible in the residuals. Each candidate split, with at least 5 bursts on each side, is therefore tested as an added step term in the fitted model, with the slope still free. The Huber score of the step indicator, after projecting the indicator off the intercepts and the slope, is divided by its H1 standard deviation to give an approximately normal z. Huber's bounded psi keeps a single displaced burst near either end from posing as a step.

The largest |z| over the K splits is compared with the Bonferroni critical value Φ⁻¹(1 − `alpha` / (2K)). `alpha` (0.01) is therefore the false-positive rate of the whole scan, and Bonferroni is conservative here because the K statistics are strongly correlated. The step is then measured by refitting the Huber model with the step term at the best split. Significance alone does not block, because with many bursts a step of a few microseconds is significant but physically meaningless. The measured step must also exceed `min_effect_ms` (0.10 ms). Splits at a stretch boundary are not tested, since the stretch's own offset absorbs any step there. Steps are reported in the residual sign, corrected minus reference.

## Curvature test

`P` is one constant per recording, so a drifting ADC rate would bend the relation. The Huber fit is repeated with a quadratic term in the sample index, scaled to −1 to 1 over the recording. Its coefficient is tested with the H1 standard error against the two-sided Student t critical value at `alpha`. As with the step test, significance alone does not block. The quadratic fit must also move the corrected time from the linear fit by more than `min_effect_ms` somewhere in the recording (`bow_ms`). The test needs at least 10 bursts.

## Breaks between stretches

The corrected time of the first sample after a break minus that of the last sample before it is the break's fitted length, `implied_interval_ns`. If any is zero or negative, the two stretches' offsets overlap and the corrected timestamps would run backwards. That happens when a break is shorter than the uncertainty of the offsets, or when comment latency changed across it. The device is then blocked before anything is written.

## Contiguous cross-validation

Each half of the bursts, in time order, is predicted by a Huber fit to the other half. Interleaved folds would test prediction between neighbouring comments. Contiguous halves test extrapolation across half the recording, which is how a rate error would show. The result is reported as `contiguous_cv` and does not block.

## Interval check

`interval_check` compares inter-sample intervals measured from both hubs' timestamps. It is reported only.

- `target_raw_interval_ns`: the target's mean interval on its own PHC, from the first to the last sample of each stretch with dropped samples counted.
- `corrected_interval_ns`: the fitted `P`, on the reference clock, with its standard error `corrected_interval_se_ppm`.
- `phc_rate` = raw / corrected: the PHC's rate as measured, against the nearest of the −6.4 % rail, lock and the +6.4 % rail (`phc_rate_vs_nearest_state_ppm`).
- `reference_interval_ns`: the reference's own mean interval, measured the same way from its NSx. `corrected_vs_reference_ppm` compares the two ADC clocks on the same clock, and `uncorrected_vs_reference_percent` shows what the target's raw interval would give.

Two independent ADC crystals differ by a few ppm, while an uncorrected railed hub is 6.4 % off. So the check confirms that the correction removed the rail and that the corrected rate is physically plausible. It cannot validate the rate more finely than the two crystals' real difference, about 1 ppm here, and it says nothing about the offset. That is why the intervals validate the correction rather than replace the comments. Using the reference's interval as the target's period would drift by that crystal difference, about 0.85 ms over the 20-minute session.

## Affected devices

The PHC rate relative to the reference clock is measured over consecutive chunks of `chunk_seconds` (10 s) of samples, as PHC ticks elapsed divided by `P` times the samples elapsed. A chunk within 20 ppm of the current run's mean rate joins that run (`phc.runs`). A run within 50 ppm of 0.936 or 1.064 is labelled railed at −6.4 % or +6.4 %; within 50 ppm of 1, running at grandmaster rate; anything else, slewing. `phc.offset_at_first_sample_ms` is the PHC tick of the first sample minus its corrected time. A device is affected if any chunk's rate differs from 1 by more than `affected_rate_ppm` (50 ppm), or the offset exceeds `affected_offset_ms` (1.0 ms). An unaffected device is reported and left unchanged unless `FORCE` is set.

## Acceptance gates

A target is repaired only if, in this order:

1. Fitting succeeds: enough bursts, enough in every stretch, and more bursts than coefficients.
2. The device is affected, or `FORCE` is set. An unaffected device stops here with status `not affected (unchanged)`.
3. No two stretches overlap at a break.
4. The 95 % band stays within `max_band_ms` (0.10 ms).
5. The fitted sample period is within `max_period_ppm` (100 ppm) of nominal.
6. The step test does not block.
7. The curvature test does not block.
8. Every NEV event maps to within `max_extrapolation_s` (1.0 s) of the primary NSx sample range.

Gates 3 to 7 are evaluated together and every failing reason is listed. Apart from gate 2, any failure marks the device `blocked`, with the reason in `issues`, and nothing is written for it.

## Three-way comment check

This uses the comments (flag-0, unique) present in every NEV of the session, including those of skipped devices, and is reported only. For the target, each receipt is mapped through the model and the median difference from the reference is removed, so its row shows its comment residuals. Every other device keeps its own clock, so a Huber line removes both its offset and its rate relative to the reference. What remains is the scatter of its comment receipt times, so a second hub that is still railed shows its scatter rather than its drift. No row tests the target's correction beyond what the fit already shows. With fewer than `min_bursts` shared comments the check is not run.

## Reference checks

The grandmaster check reads the data field of a flag-1 comment (character set other than 255) as the receipt time minus the grandmaster time at which Central issued the comment, in microseconds (signed 32-bit). A device's clock counts as on the grandmaster when:

- it has at least three such comments;
- at least 90 % of the latencies lie between 0 and 1 s;
- a Huber line of receipt time against issue time has a slope within 100 ppm of 1.

Comments with out-of-range latencies are listed (`out_of_range_comments`) rather than allowed to decide the result on their own. With `REFERENCE = 'auto'`, the reference is the NSP if it is consistent, otherwise the lowest-numbered consistent hub. If no device is consistent, the session is not evaluated. A named reference is used even when it is inconsistent; the report then carries `reference.warning` and the log shows a warning.

The reference's own fastest NSx file is scanned the same way as a target's (`reference.nsx_check`). The scan gives its stretches, its dropped samples, its mean interval and its sample clock against nominal on its own clock. Dropped samples or extra stretches produce warnings in the log; they do not block, because the target is mapped onto the reference's clock, not onto its sample count.

## Cross-session check

For each device that passed the gates in at least three sessions with the same reference, `cross_session` reports two checks, neither of which blocks:

- **Offset line:** the PHC offset at each session's first sample against reference time. It lies on one straight line if the PHC ran on at one rate without re-stepping.
- **Leave-one-session-out:** one straight line from the device's PHC ticks to reference time is fitted to the comment bursts of every other session, and used to predict the held-out session's bursts.

Both checks are meaningful only when the device's PHC ran at one rate across all its sessions (`one_phc_rate_throughout`).

## Corrected files

- Primary NSx: packet `k` receives `origin + round(a[s] + P * v(k))`. Within each stretch the corrected timestamps are a straight line in sample index, skipping any dropped samples. The original PHC values do not enter, so PHC jitter and slewing do not carry over.
- Other NSx streams: each original timestamp is converted to a primary-stream index and mapped. Their first and last samples must lie within `max_extrapolation_s` of the primary range. This is checked while writing, so a violation gives status `failed` rather than `blocked`, and a dry run does not check it.
- NEV: each packet timestamp is mapped the same way, with one exception. System packets (IDs 0xFFF0 to 0xFFFE) whose ID and timestamp exactly match a packet in the reference NEV are taken to be on the reference clock already and keep their timestamp. For flag-1 comments, the data field is shifted by the same amount in µs (modulo 2^32), so the implied issue time, timestamp minus data, is unchanged.
- All other bytes are copied unchanged, including the file headers, so any time-origin field in them keeps its original value. Companion files are not read or written.

## Write verification

Each output is first written to a `.partial` file. For NSx, the copy is re-read: the header must be identical, every packet must equal the source packet carrying its planned timestamp, the length must match, and the primary stream's timestamps must strictly increase. Other streams are not checked for order. For NEV, the whole file is re-read and compared with the planned bytes. The source's SHA-256 must be unchanged after writing. Only then is the `.partial` file renamed, and the source and output hashes are recorded in `outputs`. After the NEV is written, `post_repair` pairs its comments against the reference again and reruns the grandmaster check on it. These confirm the written file and do not block.

The report also records `tool_sha256`, the SHA-256 of `TimestampRepair.py`. When run through `run_repair.py` it also records `launcher`, with that file's name and SHA-256. The settings used are recorded in every session.

## Assumptions and limitations

- **Equal receipt latency.** The fit assumes that, on average, the target and the reference stamp a given comment at the same moment. A constant difference between their latencies goes into `a[s]` and becomes a constant error in every corrected timestamp. No check in the tool can detect it, because it is constant, and comments alone cannot measure it. Bounding it needs an independent signal, such as a shared sync pulse or artifacts recorded on both hubs, or a session in which both hubs were locked.
- **One sample period per recording.** The curvature test bounds any drift; drift too small to block is reported.
- **Independent bursts.** The band and tests treat bursts as independent. In the 2026-07-22 data, the lag-1 correlation of consecutive burst residuals was between −0.3 and +0.22, with no consistent sign.
- **Comment coverage.** Corrected times between comments are interpolated by the fitted line, and times before the first or after the last comment are extrapolated by it. The band includes that extrapolation.
- **Ambiguous jumps.** A PHC jump that is exactly a whole number of sample intervals, with no samples lost, cannot be told apart from dropped samples by timestamps alone. It would be counted as dropped samples, and a step of that size would then appear in the step test.
- **Long gaps.** A gap whose two sample counts disagree becomes a new stretch and needs comments on both sides.
- **The reference is trusted.** Its clock is judged by the grandmaster check, which only warns when the reference is named explicitly, and its NSx scan reports but does not block.
- **Repeated or backward primary timestamps** stop the device rather than being repaired.
- **Flag-1 data fields.** A field that did not agree with its own packet timestamp before repair still disagrees afterwards. `post_repair.grandmaster_check` then shows the out-of-range comment without blocking.
- **Calendar epoch.** Corrected timestamps take on the reference's clock, including its calendar epoch. Header time-origin fields are not changed.

## Validation of the implementation

These tests were run during development on synthetic data; they are not shipped with the tool.

- **Student t quantile:** within 2.3 × 10⁻¹⁰ of scipy for 1 to 100,000 degrees of freedom at the levels used.
- **Huber fit:** agrees with statsmodels' RLM (Huber t = 1.345, MAD scale, H1 covariance), with coefficients within 0.004 ns across a 20-minute recording and fitted-value variances within 6 × 10⁻⁹ relative.
- **Band coverage:** the 95 % band covered the true time at the last sample in 95.8 % of 400 simulated recordings.
- **False alarms:** 1,000 simulated recordings each, at 20, 60 and 200 bursts with 5 % of bursts given 4 times the scatter. The step scan was significant in at most 0.2 %. The curvature test was significant in 1.1 % to 1.4 % and blocked in at most 0.6 %.

Synthetic Gemini files with known true sample times were also tested: 30 s sessions, 120 comment events, a PHC railed at −6.4 % and 40 µs latency scatter per hub.

- **Outlier burst:** a three-comment burst displaced by 0.56 ms was down-weighted to 0.15; the session was repaired with a largest error of 17 µs (band 25 µs).
- **Dropped samples:** gaps of 1, 7, 3,000 and 2 samples, the last after the final comment, were all counted exactly; largest error 9 µs. Version 2 blocks the same file.
- **Restart:** 0.5 s lost plus a 40 µs PHC jump gave a second stretch, with a fitted break of 499.989 ms (true 500.034 ms); largest error 24 µs.
- **Tight restart:** a 40 µs PHC jump with nothing lost was repaired in 7 of 7 noise draws. A 100 µs latency change at the same break was blocked as an overlap before writing.
- **Reference clock step:** a 0.3 ms step in Hub1's clock at mid-session was blocked (measured −0.28 ms).
- **ADC drift:** a 0.5 ms bow from ADC rate drift was blocked (bow 0.59 ms).
- **Reference gap:** one dropped Hub1 sample was reported and logged; Hub2 was repaired.
- **Second railed hub and NSP:** a second hub railed at +6.4 % and an NSP with 75 ms scatter were added. Both hubs were repaired; the NSP was skipped.
- **Interval check:** the measured PHC rate was 1.2 standard errors from the true 0.936, and the uncorrected interval read −6.400 % against Hub1.
- **Four sessions on one continuous PHC:** leave-one-session-out mean errors were at most 10 µs, and every session was within 16 µs of truth.

## Results on the 2026-07-22 sessions

Hub2's raw files were not available for this document. The table applies version 3's burst grouping, fit, band, step test, curvature test and cross-validation to the 2,567 comment pairs stored in `timestamp_repair_report.json`. That report was produced by an earlier version 2.0.0 build without outlier rejection. Each comment's sample index was recovered from the stored residual, intercept and slope, which reproduces the stored residuals to 0.1 ns. The timeline scan, file writing, verification and reference NSx scan were not run, so a full version 3 run on the original files should replace this table.

Facts from the version 2 report that do not depend on the fit:

- **Hub2 PHC:** railed at −6.4 % throughout every session, one rate run per session. Its offset at the first sample fell from 43,250.7 s in the first session to 42,919.0 s in the last.
- **Hub2 timestamps:** every Hub2 NS6 file was one continuous stretch, with no discontinuities.
- **Comment pairs:** 2,567 in total, with no unmatched or repeated texts.
- **Hub1 grandmaster check:** in `144851-144922-NBack-SUM-001`, `145521-145722-NBack-SUM-001` and `152438-152547-StroopInterleaved-SUM-001`, at least one flag-1 comment reported a latency of 991.6 s, 1,025.9 s and 1,196.1 s respectively. The cause is not known. Version 2's check failed on any single out-of-range value; version 3 lists such comments.
- **NSP:** about 1,271 s ahead of the grandmaster, with comment receipts scattering by 61 to 99 ms RMS after its offset is removed.

Version 3 on the recovered comment pairs, for Hub2:

| Session | Span (s) | Comments | Bursts | 95 % band (µs) | Sample clock (ppm, ± SE) | Step \|z\| (critical) | Curvature \|t\| (critical) | Half-session CV RMS (µs) | Rail vs 6.4 % (ppm) | v2 | v3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 144417-144454-NBack-SUM-001 | 224 | 168 | 98 | 34 | -0.02 ± 0.12 | 2.24 (3.86) | 0.32 (2.63) | 77 | +0.11 | repaired | passes |
| 144851-144922-NBack-SUM-001 | 202 | 165 | 92 | 29 | -0.05 ± 0.11 | 2.00 (3.85) | 0.54 (2.63) | 60 | +0.08 | repaired | passes |
| 145521-145722-NBack-SUM-001 | 209 | 165 | 85 | 28 | +0.08 ± 0.11 | 2.62 (3.82) | 1.07 (2.64) | 66 | +0.22 | repaired | passes |
| 150357-150401-NBack-SUM-001 | 203 | 165 | 84 | 25 | -0.27 ± 0.10 | 2.34 (3.82) | 1.47 (2.64) | 63 | -0.14 | repaired | passes |
| 150816-150824-NBack-SUM-001 | 352 | 168 | 84 | 54 | -0.18 ± 0.10 | 1.99 (3.82) | 0.78 (2.64) | 73 | -0.05 | repaired | passes |
| 151435-151502-NBack-SUM-001 | 224 | 165 | 79 | 37 | -0.27 ± 0.13 | 2.58 (3.80) | 1.20 (2.64) | 76 | -0.14 | repaired | passes |
| 152041-152044-StroopInterleaved-SUM-001 | 173 | 91 | 61 | 55 | -0.30 ± 0.20 | 1.58 (3.73) | 0.25 (2.66) | 84 | -0.16 | repaired | passes |
| 152438-152547-StroopInterleaved-SUM-001 | 1216 | 1,209 | 773 | 8 | -0.22 ± 0.01 | 1.76 (4.36) | 0.20 (2.58) | 66 | -0.09 | blocked | passes |
| 154808-154857-CenterOutTrain-SUM-001 | 47 | 18 | 17 | 43 | -0.60 ± 0.60 | 1.34 (3.23) | 0.83 (2.98) | 165 | -0.47 | repaired | passes |
| 154808-155010-CenterOutTrain-SUM-002 | 207 | 62 | 44 | 34 | -0.16 ± 0.12 | 2.45 (3.63) | 1.08 (2.70) | 86 | -0.03 | repaired | passes |
| 154808-155511-CenterOutTrain-SUM-003 | 206 | 55 | 40 | 27 | -0.24 ± 0.10 | 1.65 (3.60) | 0.21 (2.71) | 72 | -0.11 | repaired | passes |
| 154808-160134-CenterOutTrain-SUM-004 | 207 | 45 | 30 | 45 | -0.13 ± 0.17 | 1.67 (3.49) | 0.44 (2.77) | 74 | -0.03 | repaired | passes |
| 154808-160640-CenterOutTrain-SUM-005 | 205 | 48 | 35 | 37 | -0.46 ± 0.13 | 1.81 (3.55) | 0.24 (2.74) | 151 | -0.32 | repaired | passes |
| 154808-161116-CenterOutTrain-SUM-006 | 202 | 43 | 34 | 31 | -0.36 ± 0.11 | 1.06 (3.54) | 0.01 (2.74) | 49 | -0.23 | repaired | passes |

Session names omit the `20260722-` prefix. Span is the reference-clock time covered by the NS6 samples. Rail vs 6.4 % is the measured PHC rate (raw interval over corrected interval) against exactly 0.936.

In summary:

- **Gates:** all 14 sessions pass, including the Stroop session version 2 blocked.
- **Band:** the 95 % band at its widest is 8 to 55 µs.
- **Model tests:** no step or curvature test comes near significance.
- **Mapping:** differs from version 2's least-squares mapping by 3 to 12 µs anywhere in a recording.
- **Interval check:** the raw Hub2 interval was 31,200.0041 ns (31,200.0032 in one session) and the corrected interval 33,333.33 to 33,333.35 ns. The measured rail is 0.47 ppm slow to 0.22 ppm fast of exactly 6.4 %, which is Hub2's crystal error plus fit uncertainty. A fixed 6.4 % factor would carry it as up to 0.11 ms of drift per session.
- **Against Hub1:** the corrected interval differs from Hub1's measured interval (33,333.317 ns, from one file of the earlier analysis) by +0.40 to +1.09 ppm. A full run measures Hub1 per session.
- **Across sessions:** Hub2's PHC kept one rate throughout. The offset line has slope −0.0640001 s/s with residuals within 54 µs, and leave-one-session-out mean errors are −57 to +44 µs, with RMS of 45 to 85 µs.
