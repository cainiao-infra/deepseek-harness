"""Descriptive pilot accounting; development data cannot establish acceptance."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def episode_summary(result: dict, *, episode_timeout: float, reserved_attempts: int) -> dict:
    """Count assigned failures even when RoboDojo exits zero without a native result.

    Evaluator time includes cold start and the evaluator's own teardown; it is
    not the formal native-terminal latency. Unknown native outcomes remain
    unsuccessful. Provider usage completeness is reported independently.
    """
    native, timing = result['native_results'], result['timings']
    native_success = False
    if len(native) == 1:
        outcome = native[0]['result']
        details = list(outcome.get('details', {}).values())
        native_success = (outcome.get('eval_time') == 1 and outcome.get('success_rate') == 1
            and outcome.get('score') == 100 and len(details) == 1
            and details[0].get('success') is True and details[0].get('score') == 1)
    timed = (len(timing) == 1 and timing[0]['error'] is None
             and math.isfinite(timing[0]['elapsed_seconds']) and timing[0]['elapsed_seconds'] >= 0)
    success = (native_success and timed and timing[0]['elapsed_seconds'] <= episode_timeout
               and result.get('client_exit') == 0 and not result.get('process_timeout')
               and result.get('error') is None)
    receipts = result['provider_receipts']
    complete = (len(receipts) == reserved_attempts and
                all(r['status'] == 'completed' and r.get('usage') is not None for r in receipts))
    return {'case_id': result['case']['case_id'], 'arm': result['arm'],
            'success': bool(success), 'native_success': native_success,
            'native_result_count': len(native), 'process_timeout': result.get('process_timeout', False),
            'client_exit': result.get('client_exit'),
            'launcher_error': result.get('error'), 'episode_error': timing[0]['error'] if len(timing) == 1 else None,
            'actions': timing[0]['actions'] if len(timing) == 1 else None,
            'evaluator_seconds_including_cold_start_and_teardown': timing[0]['elapsed_seconds'] if len(timing) == 1 else None,
            'capped_evaluator_seconds': timing[0]['elapsed_seconds'] if success else episode_timeout,
            'provider_receipts': len(receipts), 'provider_attempts_reserved': reserved_attempts,
            'provider_usage_complete': complete,
            'total_tokens': sum(r['usage']['total_tokens'] for r in receipts) if complete else None}


def summarize(root: Path) -> dict:
    """Require every preassigned arm; retain failed records and evidence digests.

    No confidence interval or G-condition is inferred from these development
    cases. A missing, duplicate or different assignment rejects the report.
    """
    spec = json.loads((root/'spec.json').read_text())
    if spec.get('schema') != 'dsh-paired-pilot/v1' or spec.get('partition') != 'development':
        raise ValueError('Only development pilot evidence is accepted')
    if not spec['cases'] or any(sorted(c['order']) != ['H0', 'H1', 'N'] for c in spec['cases']):
        raise ValueError('Each pilot case requires all three arms exactly once')
    expected = {(c['case_id'], arm): c for c in spec['cases'] for arm in c['order']}
    if len(expected) != len(spec['cases'])*3:
        raise ValueError('Duplicate case assignments in pilot specification')
    observed = {}
    paths = {}
    versions = set()
    for path in sorted(root.glob('*/result.json')):
        result = json.loads(path.read_text())
        if result.get('schema') != 'dsh-paired-pilot-episode/v1' or result.get('qualification') is not False:
            raise ValueError('Versioned unqualified pilot episode required')
        key = result['case']['case_id'], result['arm']
        if key in observed or key not in expected or result['case'] != expected[key]:
            raise ValueError('Duplicate, unexpected or altered pilot assignment')
        raw_native = [{'path': str(p.relative_to(path.parent)), 'result': json.loads(p.read_text())}
                      for p in sorted((path.parent/'simulation').rglob('_result.json'))]
        raw_timings = [json.loads(line) for p in (path.parent/'simulation').glob('episode-timing.jsonl')
                       for line in p.read_text().splitlines()]
        raw_provider = [json.loads(p.read_text())
                        for p in sorted((path.parent/'provider').glob('http-*/receipt.json'))]
        if (result['native_results'] != raw_native or result['timings'] != raw_timings
                or result['provider_receipts'] != raw_provider):
            raise ValueError('Launcher summary disagrees with retained native/timing/provider evidence')
        reserved = len(list((path.parent/'provider/attempts').glob('call-*.json')))
        observed[key] = episode_summary(result, episode_timeout=spec['episode_timeout_seconds'],
                                        reserved_attempts=reserved)
        if 'layout_id' in result['case']:
            selected = path.parent/'simulation/selected-layout.json'
            layout = json.loads(selected.read_text()) if selected.exists() else None
            if layout != result.get('selected_layout'):
                raise ValueError('Launcher layout selection disagrees with native evidence')
            if layout is None:
                if observed[key]['success']:
                    raise ValueError('Successful episode is missing its selected layout evidence')
            elif (layout['native_layout_id'] != result['case']['layout_id']
                    or layout['layout_sha256'] != result['case']['layout_sha256']):
                raise ValueError('Native layout identity differs from the frozen case')
            elif hashlib.sha256(selected.with_suffix('.snapshot.json').read_bytes()).hexdigest() != layout['layout_sha256']:
                raise ValueError('Runtime layout snapshot differs from the frozen case')
            if layout is not None and 'fresh_layout' in result['case']:
                cohort, source = result['case']['fresh_layout'], layout.get('fresh_source', {})
                if any(source.get(k) != v for k, v in {
                    'manifest_path':cohort['manifest'],'manifest_sha256':cohort['manifest_sha256'],
                    'index':cohort['index'],'split':cohort['split'],'task':result['case']['task']}.items()):
                    raise ValueError('Fresh layout source differs from the frozen case')
            observed[key]['selected_layout'] = layout
        observed[key]['artifact_directory'] = path.parent.name
        interruption = path.parent/'infrastructure-interruption.json'
        if interruption.is_file():
            observed[key]['infrastructure_interruption'] = json.loads(interruption.read_text())
            paths[str(interruption.relative_to(root))] = hashlib.sha256(interruption.read_bytes()).hexdigest()
        versions.add(result['source_commit'])
        paths[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    if set(observed) != set(expected):
        raise ValueError('Pilot incomplete; every assigned arm must have a result')
    if len(versions) != 1:
        raise ValueError('Mixed source versions require separate pilot reports')
    rows = list(observed.values())
    return {'schema': 'dsh-paired-pilot-report/v1', 'qualification': False,
            'partition': 'development', 'formal_heldout_runs': 0,
            'statistical_acceptance': 'insufficient_evidence',
            'rows': rows, 'assigned_episodes': len(expected),
            'source_version_count': len(versions), 'result_sha256': paths,
            'arms': {arm: {'successes': sum(r['success'] for r in rows if r['arm'] == arm),
                           'assigned': sum(r['arm'] == arm for r in rows)} for arm in ('N', 'H0', 'H1')}}
