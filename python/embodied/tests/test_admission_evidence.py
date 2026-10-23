"""Retained development evidence distinguishes executable recovery from safe rejection."""
import hashlib
import json
from pathlib import Path

import pytest

from deepseek_harness_embodied.admission import AdmissionPolicy, validate_decision
from deepseek_harness_embodied.bridge import digest


ROOT=Path(__file__).parents[1]/'evaluations/2026-10-23-admissible-recovery'


def read(reference):
    raw=(ROOT/reference['path']).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==reference['sha256']
    return json.loads(raw)


def test_retained_enrollment_recomputes_both_independent_execution_qualifications():
    policy=AdmissionPolicy(ROOT,json.loads((ROOT/'admission-policy.json').read_text()))
    assert {r['entry']['id'] for r in policy.entries}=={'registered-pickup','visual-history-pickup'}
    assert all(r['report']['covered'] and r['report']['formal_heldout_runs']==0 for r in policy.entries)
    code=json.loads((ROOT/'qualified-code.report.json').read_text())
    assert code==next(r['report'] for r in policy.entries if r['entry']['id']=='registered-pickup')
    assert code['trials'][0]['actions']==110 and code['trials'][0]['planner_attempts']==0


@pytest.mark.parametrize('identifier',['switch-success','no-alternative','unavailable-choice',
    'stale-choice','explicit-stop','unknown-condition','live-deepseek-switch'])
def test_real_simulator_cases_preserve_admission_and_stop_invariants(identifier):
    summary=json.loads((ROOT/'summary.json').read_text())
    row=next(r for r in summary['cases'] if r['id']==identifier)
    evidence={key:read(value) for key,value in row['evidence'].items()}
    chain=evidence['chain'];candidates=evidence['candidates']
    for snapshot in candidates:
        assert snapshot['revision']==digest({k:v for k,v in snapshot.items() if k!='revision'})
    for saved in evidence['decisions']:
        snapshot=next(s for s in candidates if s['revision']==saved['candidate_set_revision'])
        assert validate_decision(saved['decision'],snapshot)==saved['decision']
    actions=[r for r in chain if r['type']=='action_dispatched']
    assert len(actions)==row['actions']
    if identifier in ('switch-success','live-deepseek-switch'):
        assert row['native_success'] and row['actions']==110 and row['provider_calls']==1
        audit=read(row['audit'])
        assert audit['confirmed_successful_chain'] and audit['replacements']==audit['skill_failures']==1
        assert audit['actions']==110 and audit['model_calls']==audit['session_count']==1
        kinds=[r['type'] for r in chain]
        assert kinds.index('skill_failure')<kinds.index('stop_acknowledged')<kinds.index('fresh_observation')<kinds.index('action_dispatched')
        assert kinds[-1]=='native_outcome' and chain[-1]['success']
        assert evidence['native_result']['success_rate']==1
    else:
        assert row['actions']==0 and not row['native_success'] and row['error']
        assert not any(r['type']=='native_outcome' for r in chain)
        assert row['provider_calls']==int(identifier not in ('no-alternative','unknown-condition'))
    recovery=[s for s in candidates if s['phase']=='recovery']
    for snapshot in recovery:
        failure=next(s for s in snapshot['excluded'] if s['id']=='visual-history-pickup')
        assert 'persistent_failure' in failure['reasons']
        assert 'visual-history-pickup' not in [c['id'] for c in snapshot['candidates']]
    if identifier=='unknown-condition':
        assert not candidates[0]['candidates']
        assert all(s['status']=='undetermined' for s in candidates[0]['excluded'])
    for projection in evidence['model_projection']:
        control=projection['control'];snapshot=control['candidate_set']
        assert 'excluded' not in snapshot
        assert [c['id'] for c in snapshot['candidates']]==['registered-pickup']
        assert snapshot['phase']=='recovery' and snapshot['observation_seq']==2
    if identifier=='live-deepseek-switch':
        receipt=evidence['provider_receipts'][0]
        assert receipt['status']=='completed' and receipt['http_status']==200
        assert receipt['usage']['total_tokens']>0


def test_frozen_assignments_failures_attribution_and_archive_imports_remain_complete():
    summary=json.loads((ROOT/'summary.json').read_text())
    assert summary['qualification'] is False and summary['formal_heldout_runs']==0
    for batch,source,count in [('06-controlled','controlled',6),('07-live','live-deepseek',1)]:
        assigned=json.loads((ROOT/(batch+'-assignments.json')).read_text())['trials']
        assert len(assigned)==count
        assert {r['case']['case_id'] for r in assigned}=={r['id'] for r in summary['cases'] if r['decision_source']==source}
    assert len(summary['prior_batches'])==5
    assert all(r['retained_in_archive'] for r in summary['prior_batches'])
    changed=next(r for r in summary['prior_batches'] if r['batch']=='01-qualification')
    assert any(not r['native_success'] for r in changed['results'])
    assert summary['cleanup']['owned_live_processes']==[]
    attribution=json.loads((ROOT/'A-provider-attribution.json').read_text())
    assert attribution['patched_physical_qualification']=='unverified'
    assert attribution['loaded_source_records']
    assert all(v['sha256']==attribution['original_config_sha256']
        for row in attribution['loaded_source_records'].values() for v in row.values())
    archive=json.loads((ROOT/'archive.json').read_text())
    assert archive['sha256']==summary['archive_sha256'] and archive['verified_payload_members']==1622
    for reference in archive['imports']:read(reference)
