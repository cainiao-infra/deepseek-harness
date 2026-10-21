"""Freeze all archived planner requests and run one paired Thinking diagnostic."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import os
from pathlib import Path

from deepseek_harness_embodied.bridge import write_record
from deepseek_harness_embodied.thinking_comparison import execute, prepare, summarize


def main() -> None:
    """Require a new evidence directory; credentials never enter retained configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--caps', type=int, nargs='+', default=[2048, 8192])
    parser.add_argument('--seed', type=int, default=20261021)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--online-variants', action='store_true',
                        help='Compare original/clarified online prompts in the same randomized batch')
    args = parser.parse_args()
    key = args.key_file.read_text().strip() if args.key_file else os.environ['PHYSICALRSI_AGENT_API_KEY'].strip()
    if not key or any(c.isspace() for c in key):
        raise ValueError('A nonempty raw API credential is required')
    protocol = prepare(args.archive, args.sha256, args.output, repeats=args.repeats,
                       caps=args.caps, seed=args.seed, timeout=args.timeout, workers=args.workers,
                       online_variants=args.online_variants)
    print(json.dumps({'assigned': len(protocol['assignments']), 'inputs': len(protocol['inputs'])}), flush=True)
    def run_pair(pair):
        for assignment in pair:
            row = execute(args.output, assignment, protocol, key)
            print(json.dumps({k: row[k] for k in ('id', 'input_id', 'thinking', 'cap', 'status', 'elapsed_seconds')}
                             | {'category': (row.get('assessment') or {}).get('category')}), flush=True)
    assignments = protocol['assignments']
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        list(executor.map(run_pair, [assignments[i:i+2] for i in range(0, len(assignments), 2)]))
    report = summarize(args.output)
    write_record(args.output/'summary.json', report)
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
