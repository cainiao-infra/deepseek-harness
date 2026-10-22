"""Recorded DSH recovery conversation with a synthetic world; no benchmark scores."""
from copy import deepcopy
from dataclasses import replace
import base64
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from deepseek_harness_embodied import PlannerBridge
from deepseek_harness_embodied import online_model, robodojo
from deepseek_harness_embodied.profile import install_source_profile
from deepseek_harness_embodied.online_evidence import audit_online_episode
from test_bridge import spec


@pytest.mark.skipif(os.environ.get('DSH_EMBODIED_INTEGRATION') != '1', reason='requires build:artifacts')
@pytest.mark.parametrize('configured,lost_rgb,fault_operation', [
    (False, True, None),(True, True, None),(True, False, None),
    (True, False, 'bind'),(True, False, 'observe'),(True, False, 'actions')])
def test_recorded_loop_preserves_cadence_arrays_and_recovery_history(tmp_path, monkeypatch, recorded_provider, configured, lost_rgb, fault_operation):
    root = Path(__file__).resolve().parents[3]
    fixture = json.loads((Path(__file__).parent/'fixtures/online-loop-session.json').read_text())
    png = json.loads((Path(__file__).parent/'fixtures/compatibility-session.json').read_text())['png']
    pixels = base64.b64decode(png)
    recovering = bool(lost_rgb or fault_operation)
    replies = [fixture['skill_failure_response']] if fault_operation else (fixture['responses'][1:] if configured and lost_rgb else ([] if configured else fixture['responses']))
    url, requests = recorded_provider(replies)
    home = tmp_path/'home'
    install_source_profile(home, root/'packages/experimental/embodied-planner')
    frozen = {'skills':{}}
    skill_config = tmp_path/'skills.json'
    skill_config.write_text(json.dumps(frozen))
    config = json.loads((Path(__file__).parents[1]/'configs/robodojo-online.example.json').read_text())
    config.update(evidence_root=str(tmp_path/'runner'), episode_timeout_seconds=120,
                  rpc_timeout_seconds=90, call_budget={'root':str(tmp_path/'budget'),'limit':3},
                  restart_guards={'pick':[{'path':['state','gripper'],'minimum':.9,'maximum':1}]})
    config['recovery']['observation_max_age_seconds'] = 120
    if configured:
        config['initial_plan'] = {'composition':'pick','rationale':'deployment binding'}
    if fault_operation:
        config['recover_skill_failures'] = True
        config['restart_guards']['place'] = config['restart_guards']['pick']
    config_path = tmp_path/'online.json'
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv('PHYSICALRSI_SKILL_CONFIG', str(skill_config))
    monkeypatch.setenv('DSH_ROBODOJO_ONLINE_CONFIG', str(config_path))
    monkeypatch.setenv('PHYSICALRSI_AGENT_API_KEY', 'recorded-test-key')

    def transport(*, reserve_call):
        bridge = PlannerBridge(spec=replace(spec(), base_url=url, deepseek_reasoning='high',
            context_window=65536, calls_per_runtime=3), dsh_home=home,
            evidence_root=tmp_path/'evidence', dsh_bin=str(root/'apps/cli/lib/bin.js'),
            runtime_cwd=str(root), reserve_call=reserve_call)
        return SimpleNamespace(bridge=bridge, run_id='keyless-loop', timeout=60, close=bridge.close)
    monkeypatch.setattr(online_model, 'PhysicalRSITransport', transport)
    models, observation_counts, executed = [], [], []

    class Native:
        def __init__(self, deployment):
            self.config, self.catalog = deepcopy(frozen), {'pick':{}, 'place':{}}
            self.operation_program, self.preloaded = None, {}
            self.memory = SimpleNamespace(read=lambda:{})
            self.planner = SimpleNamespace(close=lambda:None)
            self.root, self.closed, self.action_queues = tmp_path/str(len(models)), False, {}
            self.observed = []
            models.append(self)
        def update_obs_batch(self, rows):
            self.observed.append(rows)
            plan = self.planner.plan()
            if models.index(self)==0 and ((fault_operation=='bind' and len(self.observed)==1) or (fault_operation=='observe' and len(self.observed)==2)):
                raise ValueError('private diagnostic must not reach model')
            self.selected = plan['composition']
            self.sessions = {0:{'skill':SimpleNamespace(describe=lambda:{'name':'pick','revision':'frozen'})}}
        def get_action_batch(self, indices):
            if models.index(self)==0 and fault_operation=='actions':
                raise ValueError('private diagnostic must not reach model')
            observation_counts.append(len(self.observed))
            value = .25 if models.index(self)==0 else .5
            return [[{'joint':np.asarray([value])} for _ in range(3 if value==.25 else 1)]]
        def close(self):
            self.closed = True

    native = ModuleType('PhysicalRSI_baselines.robodojo.skill_model')
    native.Model = Native
    planner = ModuleType('PhysicalRSI_baselines.robodojo.agent_planner')
    def messages(**kwargs):
        context = {'instruction':kwargs['instruction'], 'observation':{
            'state':kwargs['observation']['state'],'vision':{'head':{'color':{'image_index':0}}}},
            'previous_plan':kwargs['previous_plan']}
        return [{'role':'system','content':'Return JSON with exactly composition and rationale as nonempty strings.'},
                {'role':'user','content':[{'type':'text','text':json.dumps(context)},
                 {'type':'image_url','image_url':{'url':'data:image/png;base64,'+png}}]}]
    def validate(value, **kwargs):
        assert set(value)=={'composition','rationale'} and value['composition'] in kwargs['compositions']
        return value
    planner.build_planner_messages, planner.validate_planner_result = messages, validate
    monkeypatch.setitem(sys.modules, native.__name__, native)
    monkeypatch.setitem(sys.modules, planner.__name__, planner)

    class World:
        num_envs, success, lost = 1, [False], False
        robot_manager = SimpleNamespace(control_manager=SimpleNamespace(
            control_queue=[SimpleNamespace(is_empty=lambda:True)]))
        def get_obs(self):
            missing = lost_rgb and len(executed)==1 and not self.lost
            self.lost |= missing
            return {'instruction':'pick','state':{'joint':[len(executed)],'gripper':1},
                    'vision':{} if missing else {'head':{'color':pixels}}}
        def take_action(self, action):
            assert isinstance(action['joint'], np.ndarray)
            executed.append(action)
            self.success = [len(executed)==2]
        def is_episode_end(self):
            return len(executed)==2
    model = online_model.OnlineModel({})
    class Client:
        def call(self, *, func_name, obs=None):
            return getattr(model, func_name)() if func_name=='reset' else getattr(model, func_name)(obs)
    try:
        model.reset()
        assert len(models)==1 and not models[0].closed
        robodojo.eval_one_episode(World(), Client())
    finally:
        model.close()
    assert len(models)==(2 if recovering else 1) and all(m.closed for m in models)
    rows = [json.loads(p.read_text()) for p in sorted((tmp_path/'runner').glob('*/[0-9]*.json'))]
    controls = [json.loads(r['messages'][-1]['content'][0]['text']) for r in requests]
    receipts = [json.loads(p.read_text()) for p in (tmp_path/'evidence').rglob('receipt.json')]
    logs = list((home/'sessions').rglob('*.jsonl'))
    assert len(logs)==int(recovering or not configured) and len(receipts)==(int(recovering) if configured else 2) and all(r['status']=='completed' for r in receipts)
    events = [json.loads(line) for log in logs for line in log.read_text().splitlines()]
    assert sum(e.get('type')=='assistant/message' for e in events)==(int(recovering) if configured else 2)
    assert not any(e.get('type')=='tool/call' for e in events)
    assert all(r['thinking']=={'type':'enabled'} and r['reasoning_effort']=='high' for r in requests)
    actual = {
        'provider_calls':len(requests), 'one_session':len({r['session_id'] for r in receipts})==1,
        'request_phases':[c['phase'] for c in controls],
        'observation_sequences':[c['current_observation_status']['observation_seq'] for c in controls],
        'recovery_trigger_sequence':next((r['observation_seq'] for r in rows if r['type']=='observed_event'), None),
        'rgb_available':[c['current_observation_status']['rgb_available'] for c in controls],
        'history_roles':[m['role'] for m in requests[-1]['messages']] if requests else [],
        'native_observation_counts_at_first_action':observation_counts,
        'dispatched_actions':[r['action'] for r in rows if r['type']=='action_dispatched'],
        'discarded_tail':next((r['discarded_runner_actions'] for r in rows if r['type']=='stop_acknowledged'), 0),
        'synthetic_terminal_success':rows[-1]['type']=='native_outcome' and rows[-1]['success'],
    }
    assert actual == (fixture['skill_failure_expected'][fault_operation] if fault_operation else fixture['zero_call_expected' if not lost_rgb else ('configured_expected' if configured else 'expected')])
    configured_records = [json.loads(p.read_text()) for p in (tmp_path/'runner').glob('*/configured-plan-*.json')]
    assert len(configured_records)==int(configured)
    if configured:
        receipt = configured_records[0]
        assert receipt['origin']=='deployment' and receipt['model_calls']==0
        assert receipt['plan']==dict(config['initial_plan'], execution='bind')
        if recovering:
            context = json.loads(requests[0]['messages'][-1]['content'][1]['text'])
            assert context['instruction']=='pick' and context['previous_plan']['plan']==receipt['plan']
        assert len(list((tmp_path/'budget').glob('call-*.json')))==int(recovering)

    episode = next(p for p in (tmp_path/'runner').iterdir() if p.is_dir())
    audit = audit_online_episode(episode, tmp_path/'evidence', initial_plan=config.get('initial_plan'))
    assert audit['model_calls']==len(requests) and audit['replacements']==int(recovering)
    assert audit['configured_plans']==int(configured) and audit['session_count']==int(recovering or not configured)
    numeric = sorted(episode.glob('[0-9]*.json'))
    mutations = {
        'stop_acknowledged':lambda row:row.update(queued_actions=1),
        'fresh_observation':lambda row:row['state'].update(observation_seq=1),
        'action_dispatched':lambda row:row.update(revision_id='uncommitted'),
        'binding_applied':lambda row:row.update(revision_id='uncommitted'),
        'native_outcome':lambda row:row.update(success=False),
        'native_success':lambda row:row.update(type='unrecognized_success'),
        'plan_committed':lambda row:row.update(request_id='../different'),
        **({'skill_failure':lambda row:row.update(operation='unknown')} if fault_operation else {}),
    }
    for kind, change in mutations.items():
        matches = [p for p in numeric if json.loads(p.read_text())['type']==kind]
        if not matches:
            assert not recovering and kind in {'stop_acknowledged','fresh_observation'}
            continue
        path = matches[0]
        original = path.read_bytes()
        row = json.loads(original)
        change(row)
        try:
            path.write_text(json.dumps(row))
            with pytest.raises(ValueError):
                audit_online_episode(episode, tmp_path/'evidence', initial_plan=config.get('initial_plan'))
        finally:
            path.write_bytes(original)
    if configured:
        with pytest.raises(ValueError, match='differs from frozen deployment'):
            audit_online_episode(episode, tmp_path/'evidence', initial_plan={'composition':'different'})

    if fault_operation:
        failure = next(r for r in rows if r['type']=='skill_failure')
        assert failure['discarded_runner_actions']==(2 if fault_operation=='observe' else 0)
        assert audit['skill_failures']==1
        assert 'private diagnostic' not in json.dumps(requests)
        assert 'diagnostic_id' not in json.dumps(requests)
        context = json.loads(requests[0]['messages'][-1]['content'][1]['text'])
        assert context['previous_plan']['execution_event']['kind']=='skill_failed'
        assert context['previous_plan']['execution_event']['facts']=={'kind':'skill_inference_failed','operation':fault_operation}
        assert models[-1].selected=='place'
        assert [r['plan']['composition'] for r in rows if r['type']=='plan_committed']==['pick','place']
        diagnostic = episode/('skill-failure-'+failure['diagnostic_id']+'.json')
        original = diagnostic.read_bytes()
        changed = json.loads(original)
        changed['inference_cleanup_confirmed'] = False
        try:
            diagnostic.write_text(json.dumps(changed))
            with pytest.raises(ValueError, match='cleanup confirmation'):
                audit_online_episode(episode, tmp_path/'evidence', initial_plan=config['initial_plan'])
        finally:
            diagnostic.write_bytes(original)
