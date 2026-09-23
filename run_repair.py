#!/usr/bin/env python3
"""Run the NSx-only repair for one recording folder. Python 3.10+."""
from argparse import Namespace
from pathlib import Path
import re
import struct
import sys

import TimestampRepair

# Leave as None to use the folder containing this script.
# Or enter your data folder, for example: ROOT_DIR = "/Users/nathan/Data/recording"
ROOT_DIR = None
TOLERANCE_MS = 2
MAX_SAMPLE_DIFFERENCE = 30
EXPECTED_RATE_DIFFERENCE_PERCENT = 6.4  # Report only; never used to fit or accept the correction.


def select_file(root, hub, extension):
    matches = sorted(p for p in root.iterdir() if p.is_file()
                     and p.suffix.lower() == extension
                     and re.match(rf'^{hub}[-_.]', p.name, re.IGNORECASE))
    if len(matches) != 1:
        names = '\n'.join(f'  {p.name}' for p in matches) or '  None found'
        raise ValueError(f'Expected exactly one {hub} {extension} file in {root}.\n'
                         f'{names}\nUse one session with full NS6 files, or only its part-1 NS6 files.')
    if extension == '.ns6':
        part = re.search(r'\.part(\d+)', matches[0].name, re.IGNORECASE)
        if part and int(part.group(1)) != 1:
            raise ValueError('Use full recordings or part 1. Later parts must not be independently realigned.')
    return matches[0]


def main():
    root = Path(ROOT_DIR).expanduser().resolve() if ROOT_DIR else Path(__file__).resolve().parent
    try:
        if not root.is_dir():
            raise ValueError(f'Data folder does not exist: {root}')
        reference_nsx = select_file(root, 'Hub1', '.ns6')
        target_nsx = select_file(root, 'Hub2', '.ns6')
        reference_nev = select_file(root, 'Hub1', '.nev')
        target_nev = select_file(root, 'Hub2', '.nev')
        ref_part = re.search(r'\.part(\d+)', reference_nsx.name, re.IGNORECASE)
        dst_part = re.search(r'\.part(\d+)', target_nsx.name, re.IGNORECASE)
        if bool(ref_part) != bool(dst_part):
            raise ValueError('Use full NS6 files for both hubs, or part-1 NS6 files for both hubs.')
        output = root / 'corrected'
        if output.exists():
            raise ValueError(f'Output folder already exists: {output}\n'
                             'Move or rename it before running again. Existing results will not be overwritten.')
        print(f'Data folder: {root}')
        print(f'Reference NS6: {reference_nsx.name}')
        print(f'Target NS6: {target_nsx.name}')
        print(f'Reference NEV: {reference_nev.name}')
        print(f'Target NEV: {target_nev.name}')
        print(f'Validation tolerance: {TOLERANCE_MS} ms')
        print(f'Maximum sample-count difference: {MAX_SAMPLE_DIFFERENCE} samples/channel')
        print('Aligning first samples; estimating rate from NSx only. NEVs validate exact comments.', flush=True)
        return TimestampRepair.repair_nsx(Namespace(
            reference_nsx=str(reference_nsx), nsx=str(target_nsx),
            reference_nev=str(reference_nev), target_nev=str(target_nev),
            tolerance_ms=str(TOLERANCE_MS), expected_rate_difference_percent=str(EXPECTED_RATE_DIFFERENCE_PERCENT),
            max_sample_difference=MAX_SAMPLE_DIFFERENCE,
            output=str(output), write=True))
    except (ValueError, OSError, OverflowError, struct.error) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
