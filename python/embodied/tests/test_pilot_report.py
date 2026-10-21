"""Pilot denominators survive zero-exit failures, missing usage and incomplete runs."""
from copy import deepcopy
import hashlib
import json

import pytest

from deepseek_harness_embodied.pilot_report import episode_summary, summarize


def result(arm='N'):
    return {'schema': 'dsh-paired-pilot-episode/v1', 'qualification': False,
            'case': {'case_id': 'example', 'order': ['N', 'H0', 'H1']}, 'arm': arm,
            'source_commit': 'fixture-version', 'client_exit': 0, 'process_timeout': False,
            'native_results': [{'path': 'simulation/_result.json', 'result': {'eval_time': 1, 'success_rate': 1, 'score': 100,
                'details': {'0': {'success': True, 'score': 1}}}}],
            'timings': [{'elapsed_seconds': 30, 'actions': 110, 'error': None}],
            'provider_receipts': [{'status': 'completed', 'usage': {'total_tokens': 12}}]}


def retain(directory, row):
    simulation = directory/'simulation'
    simulation.mkdir()
    for native in row['native_results']:
        (directory/native['path']).write_text(json.dumps(native['result']))
    (simulation/'episode-timing.jsonl').write_text('\n'.join(json.dumps(t) for t in row['timings'])+'\n')
    for index, receipt in enumerate(row['provider_receipts']):
        root = directory/'provider'/f'http-{index:04d}'
        root.mkdir(parents=True)
        (root/'receipt.json').write_text(json.dumps(receipt))


def test_pinned_layout_report_rejects_mutated_snapshot(tmp_path):
    layout=b'{"position":[1,2,3]}'
    case=dict(result()['case'],layout_id=2,layout_sha256=hashlib.sha256(layout).hexdigest())
    spec={'schema':'dsh-paired-pilot/v1','partition':'development','episode_timeout_seconds':30,'cases':[case]}
    (tmp_path/'spec.json').write_text(json.dumps(spec))
    for arm in case['order']:
        directory=tmp_path/arm
        directory.mkdir()
        row=result(arm)
        row['case']=case
        row['selected_layout']={'native_layout_id':2,'layout_sha256':case['layout_sha256']}
        (directory/'result.json').write_text(json.dumps(row))
        retain(directory,row)
        (directory/'simulation/selected-layout.json').write_text(json.dumps(row['selected_layout']))
        (directory/'simulation/selected-layout.snapshot.json').write_bytes(layout)
    assert all(r['success'] for r in summarize(tmp_path)['rows'])
    (tmp_path/'H1/simulation/selected-layout.snapshot.json').write_bytes(b'{}')
    with pytest.raises(ValueError,match='snapshot differs'):
        summarize(tmp_path)


@pytest.mark.parametrize('change', [
    {'native_results': []}, {'process_timeout': True}, {'client_exit': 1},
    {'error': {'type': 'TimeoutError'}},
    {'timings': [{'elapsed_seconds': 31, 'actions': 110, 'error': None}]},
    {'timings': [{'elapsed_seconds': -1, 'actions': 110, 'error': None}]},
    {'timings': [{'elapsed_seconds': float('nan'), 'actions': 110, 'error': None}]},
    {'timings': [{'elapsed_seconds': 3, 'actions': 0, 'error': {'type': 'WsError'}}]},
])
def test_failed_assigned_run_is_unsuccessful_and_uses_common_cap(change):
    summary = episode_summary(dict(result(), **change), episode_timeout=30, reserved_attempts=1)
    assert summary['success'] is False
    assert summary['capped_evaluator_seconds'] == 30


def test_native_aggregate_cannot_override_failed_case():
    row = result()
    row['native_results'][0]['result']['details']['0']['success'] = False
    assert not episode_summary(row, episode_timeout=30, reserved_attempts=1)['success']


def test_unknown_cost_does_not_erase_physical_outcome():
    row = result()
    row['provider_receipts'][0]['usage'] = None
    summary = episode_summary(row, episode_timeout=30, reserved_attempts=1)
    assert summary['success'] is True
    assert summary['provider_usage_complete'] is False and summary['total_tokens'] is None


def test_missing_attempt_receipt_is_unknown_cost():
    summary = episode_summary(result(), episode_timeout=30, reserved_attempts=2)
    assert summary['provider_attempts_reserved'] == 2 and summary['provider_receipts'] == 1
    assert summary['provider_usage_complete'] is False and summary['total_tokens'] is None


def test_report_requires_all_assignments_and_never_qualifies_development(tmp_path):
    spec = {'schema': 'dsh-paired-pilot/v1', 'partition': 'development',
            'episode_timeout_seconds': 30, 'cases': [result()['case']]}
    (tmp_path/'spec.json').write_text(json.dumps(spec))
    for arm in ['N', 'H0']:
        d = tmp_path/arm
        d.mkdir()
        (d/'result.json').write_text(json.dumps(result(arm)))
        retain(d, result(arm))
    with pytest.raises(ValueError, match='incomplete'):
        summarize(tmp_path)
    d = tmp_path/'H1'
    d.mkdir()
    row = result('H1')
    row['native_results'] = []
    (d/'result.json').write_text(json.dumps(row))
    retain(d, row)
    report = summarize(tmp_path)
    assert report['assigned_episodes'] == 3 and len(report['result_sha256']) == 3
    assert report['arms']['H1'] == {'successes': 0, 'assigned': 1}
    assert report['qualification'] is False and report['formal_heldout_runs'] == 0
    assert report['statistical_acceptance'] == 'insufficient_evidence'
    changed = deepcopy(row)
    changed['source_commit'] = 'another-fixture-version'
    (d/'result.json').write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='Mixed'):
        summarize(tmp_path)
    (d/'result.json').write_text(json.dumps(row))
    duplicate = tmp_path/'duplicate'
    duplicate.mkdir()
    (duplicate/'result.json').write_text(json.dumps(deepcopy(row)))
    with pytest.raises(ValueError, match='Duplicate'):
        summarize(tmp_path)


@pytest.mark.parametrize('cases', [
    [], [{'case_id': 'example', 'order': ['N', 'H0']}],
    [result()['case'], result()['case']],
])
def test_invalid_expected_matrix_rejects_before_reading_results(tmp_path, cases):
    (tmp_path/'spec.json').write_text(json.dumps({'schema': 'dsh-paired-pilot/v1',
        'partition': 'development', 'episode_timeout_seconds': 30, 'cases': cases}))
    with pytest.raises(ValueError, match='three arms|Duplicate case'):
        summarize(tmp_path)
