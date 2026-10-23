"""Audit a capability's retained development execution evidence, without inference."""
import argparse
import json
from pathlib import Path

from deepseek_harness_embodied.bridge import write_record
from deepseek_harness_embodied.qualification import audit_qualification


def main() -> None:
    """Write an immutable report; exit two when the requested effects lack coverage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit_qualification(args.manifest.parent, json.loads(args.manifest.read_text()))
    write_record(args.output, result)
    print(json.dumps({'capability_id': result['capability_id'], 'covered': result['covered'],
                      'effects': result['effects'], 'qualification': False}, ensure_ascii=False))
    if not result['covered']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
