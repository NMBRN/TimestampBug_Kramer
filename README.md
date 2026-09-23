# Fix hub 2 timestamps

Extract this package into a new folder. Requires Python 3.10 or later; no additional packages are needed.

The tool uses hub 1 as ground truth, calculates the differences between adjacent NSx timestamps, and uses the measured rates to correct a copy of hub 2. Matching NEV comments check the result; they never determine the correction. Hub 1 and the NSP remain unchanged.

## Run

1. Put `run_repair.py` and `repair_time.py` in a folder containing one session's files:

   | Required file | Original filename begins with |
   | --- | --- |
   | Hub 1 NS6 | `Hub1-` |
   | Hub 2 NS6 | `Hub2-` |
   | Hub 1 NEV | `Hub1-` |
   | Hub 2 NEV | `Hub2-` |

   Use full NS6 recordings from both hubs, or only part 1 from both. Keep one file of each type and retain the original filenames. NSP and CCF files can stay in the folder but are not needed for this workflow.

2. Open Terminal in that folder. On Mac, type `cd `, drag the folder into Terminal, and press Enter.

3. Run:

   ```bash
   python3 run_repair.py
   ```

To keep the scripts elsewhere, keep both Python files together and set the data folder near the top of `run_repair.py`:

```python
ROOT_DIR = "/Users/yourname/Data/recording"
```

Leave `ROOT_DIR = None` to use the scripts' folder.

## Results

A successful run creates `corrected/Hub2.corrected.ns6` and `corrected/report.json`. Originals are unchanged. The report includes sample totals, possible data-loss checks, timestamp intervals, expected versus measured effective rates, and timing errors for each exact matching NEV comment.

The script aligns the first hub 2 sample to the first hub 1 sample, assuming both recordings share a start. It does not independently measure start latency.

Settings at the top of `run_repair.py`:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `MAX_SAMPLE_DIFFERENCE` | 30 | Maximum sample-count difference per channel; 1 ms at 30 kHz |
| `TOLERANCE_MS` | 2 | Maximum comment timing discrepancy and sampled rate-linearity error |
| `EXPECTED_RATE_DIFFERENCE_PERCENT` | 6.4 | Expected hub 2 rate increase, logged for comparison only |

The sample and timing limits are configurable test settings. Failed checks block the corrected copy. The expected rate is only logged; it never changes the correction or determines whether it passes. Review the report rather than raising a limit just to pass. Move or rename an existing `corrected` folder before rerunning.

For this example, hub 2's elapsed timestamps are about 6.4% compressed. This corresponds to about 6.84% more samples per timestamp-second. Both measurements are reported; the rate is estimated, not fixed at either value.

See `TECHNICAL.md` for the method and validation results.
