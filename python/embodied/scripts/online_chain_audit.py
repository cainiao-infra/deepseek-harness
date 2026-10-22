"""Audit one completed pilot H1 episode independently of its launcher summary."""
import argparse
import json
from pathlib import Path

from deepseek_harness_embodied.online_evidence import audit_online_episode

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
args = parser.parse_args()
inputs = json.loads((args.root.parent/'resolved-inputs.json').read_text())
configured = inputs['online'].get('initial_plan')
online = json.loads((args.root/'online.json').read_text())
if online.get('initial_plan') != configured:
    raise ValueError('Episode initial plan differs from frozen pilot configuration')
roots = [p for p in (args.root/'runner').iterdir() if p.is_dir() and list(p.glob('[0-9]*.json'))]
if len(roots) != 1:
    raise ValueError('Exactly one recorded episode is required')
print(json.dumps(audit_online_episode(roots[0], args.root/'model-evidence',
                                    initial_plan=configured), indent=2, allow_nan=False))
