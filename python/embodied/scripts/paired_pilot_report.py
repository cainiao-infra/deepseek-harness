"""Print a read-only development report after every assigned episode has ended."""
import argparse
import json
from pathlib import Path

from deepseek_harness_embodied.pilot_report import summarize

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
args = parser.parse_args()
print(json.dumps(summarize(args.root), indent=2, allow_nan=False))
