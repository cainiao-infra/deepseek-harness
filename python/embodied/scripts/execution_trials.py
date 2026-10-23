"""Run preassigned native-only development trials with planner access forbidden."""
from copy import deepcopy
import argparse
import json
from pathlib import Path

from deepseek_harness_embodied.bridge import write_record
from paired_pilot import episode, resolve_inputs, validate_spec


def run(document: dict) -> None:
    """Freeze all assignments before launch and retain every failed native trial."""
    if document.get('schema') != 'dsh-execution-trials/v1':
        raise ValueError('Versioned execution trial specification required')
    pilot = deepcopy(document['pilot'])
    validate_spec(pilot)
    if pilot.get('planner_mode') != 'forbidden':
        raise ValueError('Execution trials require forbidden planning')
    inputs = resolve_inputs(pilot)
    cases = {case['case_id']: case for case in pilot['cases']}
    assignments = document['trials']
    if not assignments or len({row['id'] for row in assignments}) != len(assignments):
        raise ValueError('Unique nonempty execution assignments required')
    for row in assignments:
        if (set(row) != {'id', 'case_id', 'plan'} or row['case_id'] not in cases
                or not isinstance(row['id'], str) or not row['id'].isascii()
                or not row['id'].replace('-', '').isalnum()):
            raise ValueError('Known cases and safe trial identifiers required')
        plan = row['plan']
        if (not isinstance(plan, dict) or set(plan) != {'composition', 'operations', 'rationale'}
                or any(not isinstance(plan[key], str) or not plan[key].strip() for key in ('composition', 'rationale'))
                or not isinstance(plan['operations'], list)
                or any(not isinstance(value, str) or not value for value in plan['operations'])):
            raise ValueError('Explicit native plan fields required')
    root = Path(pilot['output'])
    root.mkdir(parents=True, exist_ok=False)
    write_record(root/'assignments.json', document)
    write_record(root/'resolved-inputs.json', inputs)
    pilot['capture_execution_diagnostics'] = True
    for index, assignment in enumerate(assignments):
        selected = deepcopy(inputs)
        selected['online']['initial_plan'] = assignment['plan']
        case = dict(cases[assignment['case_id']], case_id=assignment['id'])
        result = episode(pilot, case, 'N', index, None, selected)
        print(json.dumps({'trial': assignment['id'], 'native_results': result['native_results'],
                          'error': result.get('error'), 'qualification': False}), flush=True)


def main() -> None:
    """Read the frozen deployment and explicit assignments without API credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True, type=Path)
    args = parser.parse_args()
    run(json.loads(args.spec.read_text()))


if __name__ == '__main__':
    main()
