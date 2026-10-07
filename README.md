# Hub timestamp repair

Repairs the timestamps of Blackrock Gemini devices affected by the PTP `step_threshold` defect. An affected hub's PTP hardware clock (PHC) can sit hours away from the grandmaster while running at the 6.4% slew limit, and the hub stamps its NSx samples and NEV events with that clock. The hub's ADC sample clock is not affected.

The tool puts an affected device's NSx and NEV files on the reference device's clock (Hub1 as configured). Every value the correction uses is measured from each session's own files: the device's sample period on the reference clock, its offset, and the number of samples lost in any gap. It does not assume the 6.4% rail, the nominal sample rate, or that the two hubs started recording together. It writes corrected copies and a JSON report. Original files are never modified.

`TECHNICAL.md` describes the method, its checks and the results on the 2026-07-22 sessions. This is version 3.0.0. Each report records the version and the SHA-256 of the scripts that produced it.

## Requirements

- Python 3.10 or later. `TimestampRepair.py` and `run_repair.py` use only the standard library.
- `plot_timestamps.py` also needs matplotlib.
- PTP recordings: BRSMPGRP 3.0 NSx and BREVENTS 3.0 NEV files with 1 ns timestamp resolution, under their original filenames.

## Files

| File | Purpose |
| --- | --- |
| `TimestampRepair.py` | The method, its checks and the file writer. Can also be run from the command line. |
| `run_repair.py` | Launcher holding the settings used for this dataset. |
| `plot_timestamps.py` | Plots one session's NSx timestamps. Does not modify data files. |
| `TECHNICAL.md` | Method, checks and validation results. |

## Data folder

Files are recognised by name: `<device>-<session>.<extension>`, where the device is `NSP` or `Hub` followed by a number and the extension is `nev` or `ns1` to `ns9`, for example `Hub2-20260722-144417-144454-NBack-SUM-001.ns6`. Files with the same session name form one session. A folder can hold any number of sessions; each is fitted on its own. Other files (CCF and so on) are ignored and not copied. A device may have only one file per extension in a session.

For each session:

- The reference device (Hub1) needs its NEV. Its fastest NSx file, if present, is scanned for gaps and its sample interval is measured. Problems there produce warnings in the log and the report; they do not block the repair.
- A device to be repaired needs its NEV, sharing comments with the reference NEV by identical text. Texts that repeat within one file don't count. Comments that reach the reference within 2 ms of each other count as one burst, and at least 6 bursts are needed. All of the device's NSx files are corrected. The one with the highest sample rate (NS6) supplies the sample index.
- Every NEV in the session is read, including those of skipped devices. An NEV that fails the format checks stops that session.

## Run

1. Open Terminal in the folder holding the scripts and the data. On a Mac, type `cd `, drag the folder into Terminal, and press Enter.
2. Run:

   ```bash
   python3 run_repair.py
   ```

Results go to `corrected/` inside the data folder. That folder must be absent or empty, so move or rename it before every run, including after a dry run.

To keep the scripts elsewhere, keep `run_repair.py` and `TimestampRepair.py` together and set the data folder near the top of `run_repair.py`:

```python
ROOT_DIR = "/Users/yourname/Data/recording"
```

Reading NSx files is the slow part. Version 3 also reads the reference's fastest NSx file in full, so runs take longer than in version 2; how much longer depends on that file's size.

## Settings in run_repair.py

| Setting | As shipped | Meaning |
| --- | --- | --- |
| `ROOT_DIR` | `None` | Data folder. `None` means the scripts' folder. |
| `OUTPUT_DIR` | `None` | Output folder. `None` means `ROOT_DIR/corrected`. |
| `REFERENCE` | `'Hub1'` | Device whose clock is trusted. `'auto'` picks the NSP, or else the lowest-numbered hub, whose comment times pass the grandmaster check (see `TECHNICAL.md`). |
| `DEVICES` | `None` | Evaluate only these devices, e.g. `['Hub2']`. `None` means every device. |
| `SKIP` | `['NSP']` | Never repaired, but reported with a reason. The NSP's comment receipt times scatter by about 200 ms, which is too much for a fit built on them. |
| `DRY_RUN` | `False` | Run every check and write the report, but no corrected files. |
| `FORCE` | `False` | Also repair devices whose clock looks unaffected. |

`LIMITS` holds the acceptance limits. Review the report rather than loosening a limit to get a pass.

| Limit | Default | Effect |
| --- | ---: | --- |
| `min_bursts` | 6 | Blocks if fewer comment bursts are shared with the reference. Also the number of shared comments the three-way check needs before it runs. |
| `min_bursts_per_stretch` | 2 | Blocks unless every continuous stretch of samples holds this many bursts. A stretch is a run of samples between breaks that are not dropped samples. |
| `burst_gap_ms` | 2.0 | Comments that reach the reference within this time of the previous one form one burst. |
| `max_band_ms` | 0.10 | Blocks if the 95 % uncertainty band of the corrected time exceeds this anywhere in the recording. |
| `alpha` | 0.01 | False-positive rate of each model test: the step scan and the curvature test. |
| `min_effect_ms` | 0.10 | A significant step or curvature blocks only if it moves the corrected time by more than this. |
| `gap_tolerance_samples` | 0.1 | A timestamp gap counts as dropped samples when, measured with the sample interval on each side, it comes to the same whole number of samples within this tolerance. |

These defaults in `TimestampRepair.DEFAULTS` are not listed in `LIMITS`. Add one to `LIMITS` to change it.

| Limit | Default | Effect |
| --- | ---: | --- |
| `max_period_ppm` | 100.0 | Blocks if the fitted sample period differs from nominal by more than this. A sanity bound; the nominal rate is not used in the correction. |
| `affected_rate_ppm` | 50.0 | A PHC rate error above this marks a device as affected. |
| `affected_offset_ms` | 1.0 | A PHC offset above this at the first sample marks a device as affected. |
| `max_extrapolation_s` | 1.0 | Blocks if NEV events fall further than this outside the primary NSx sample range. The first and last samples of the other NSx streams are held to the same limit while writing, so a violation there gives `failed`. |
| `chunk_seconds` | 10.0 | Resolution of the PHC rate diagnostic. |

A key in `LIMITS` that the tool does not know stops the run with an error rather than being ignored.

### Upgrading from version 2

| Version 2 setting | Version 3 |
| --- | --- |
| `min_pairs` | `min_bursts` |
| `min_pairs_per_segment` | `min_bursts_per_stretch` |
| `step_alpha`, `min_step_ms` | `alpha`, `min_effect_ms`, now shared by the step scan and the curvature test |
| `max_bias_ms`, `max_rms_ms`, `max_abs_ms` | Removed. `max_band_ms` limits the uncertainty of the correction instead of the scatter of individual comments. |
| `outlier_sigma`, `outlier_floor_fraction`, `outlier_ceiling_fraction` | Removed. The Huber fit down-weights outlying bursts without dropping them. |
| `max_three_way_ms`, `max_three_way_skipped_ms` | Removed. The three-way check is reported only. |
| (none) | New: `burst_gap_ms`, `max_band_ms`, `gap_tolerance_samples` |

## Results

The output folder receives:

- corrected copies of each repaired device's NSx files and NEV, under their original names;
- `timestamp_repair_report.json`, with one entry per session and device.

Each device gets one status:

| Status | Meaning |
| --- | --- |
| `reference (unchanged)` | The reference device. |
| `repaired` | Every check passed; the corrected files were written and verified. |
| `blocked` | A check failed and `issues` says which. Nothing is written for the device. |
| `not affected (unchanged)` | PHC rate error and offset are within `affected_rate_ppm` and `affected_offset_ms`. `FORCE` repairs it anyway. |
| `would repair (dry run)` | Every check passed during a dry run. |
| `skipped (excluded from repair)` | Listed in `SKIP`. `probable_cause` gives the reason. |
| `skipped (not selected)` | Not listed in `DEVICES`. |
| `not evaluated: no NEV, so no shared comments` | The device has no NEV in this session. |
| `failed` | Writing or verifying a file failed, or another NSx stream fell outside `max_extrapolation_s`. Partial files are deleted; files already completed for that device stay in the output folder. |

A session that could not be evaluated at all, for example because the reference has no NEV, has an `error` entry instead.

`run_repair.py` exits with:

- 0 when every session was evaluated and no device was blocked or failed;
- 2 when a device was blocked or failed, or a session could not be evaluated;
- 1 when the run stopped before writing a report: output folder not empty, no matching files, duplicate files, an unknown setting, or a file system error. The message says which.

### What to look at in the report

For each repaired device:

- `band_95`: the 95 % uncertainty of the corrected time at both ends of every stretch. `max_ms` is the gated value.
- `interval_check`: inter-sample intervals measured from both hubs' timestamps. It gives the device's raw interval on its own PHC and the corrected interval on the reference clock. Their ratio is the PHC's measured rate, compared with the nearest of the two 6.4 % rails or lock. It also compares the corrected interval with the reference's own measured interval, and gives the fitted interval's standard error for judging both.
- `fit`: the Huber fit. `lowest_weights` lists the most down-weighted bursts, such as comment stalls.
- `timeline`: gaps in the primary NSx timestamps, how many dropped samples each held, and any break that started a new stretch, with the break's fitted length.
- `step_test` and `curvature_test`: the model tests. `contiguous_cv`: each half of the session predicted from the other half.
- `phc`: the PHC's offset at the first sample and its rate in runs of similar rate.
- `sample_clock_vs_nominal_ppm` and `sample_clock_ppm_se`: the device's ADC rate on the reference clock.
- `three_way_comment_check`, `nev_plan`, `outputs` (hashes, first and last timestamps, step range) and `post_repair`.

At session level, `reference.warning` appears when the reference fails its own grandmaster check, and `reference.nsx_check` holds the reference's own NSx scan and measured sample interval. At the top level, `tool_sha256` and `launcher` identify the scripts, and `cross_session` holds the checks across sessions of one device (see `TECHNICAL.md`).

## Command line

`TimestampRepair.py` can also be run directly:

```bash
python3 TimestampRepair.py --data DATA_FOLDER --out NEW_FOLDER --reference Hub1 --devices Hub2
```

Other options are `--dry-run`, `--force`, and one option per limit with hyphens in place of underscores, e.g. `--max-band-ms 0.1`. The command line defaults to `--reference auto` and has no skip option, so unless `--devices` narrows the list, every device other than the reference is evaluated, the NSP included. Use a new, empty output folder. Runs from the command line record no launcher hash.

## Plotting timestamps

`plot_timestamps.py` plots the primary NSx timestamps of one session's devices, minus a straight ramp, against elapsed time at the nominal sample rate. It imports `TimestampRepair.py`, so keep the two together. Its settings are at the top of the script:

| Setting | Meaning |
| --- | --- |
| `DATA_DIR` | Data folder. `None` means the script's folder. |
| `SESSION` | Session name. `None` works when the folder holds one session; otherwise the script lists them. |
| `REFERENCE_DEVICE` | `None` plots each device against its own nominal sample rate, so the slope is that device's PHC rate against its own ADC. A device name plots every device against that device's measured rate, which makes it flat. |
| `DEVICES` | Devices to plot. A railed device dwarfs a drifting one and flattens the others onto the axis. |
| `MAX_POINTS` | Approximate number of points plotted per device after decimation. |
| `OUTPUT_PNG` | `None` opens a window; a path saves the figure instead. |

For each device the script prints the end-to-end drift in ms and ppm and the "wander", the largest deviation from a straight line. The plot is decimated, so the sample-level continuity test is the repair tool's interval scan, reported under `timeline` for each device and `reference.nsx_check` for the reference.
