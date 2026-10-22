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
from test_bridge import spec


@pytest.mark.skipif(os.environ.get('DSH_EMBODIED_INTEGRATION') != '1', reason='requires build:artifacts')
@pytest.mark.parametrize('configured', [False, True])
def test_recorded_loop_preserves_cadence_arrays_and_recovery_history(tmp_path, monkeypatch, recorded_provider, configured):
    root = Path(__file__).resolve().parents[3]
    fixture = json.loads((Path(__file__).parent/'fixtures/online-loop-session.json').read_text())
    png = json.loads((Path(__file__).parent/'fixtures/compatibility-session.json').read_text())['png']
    pixels = base64.b64decode(png)
    url, requests = recorded_provider(fixture['responses'][1:] if configured else fixture['responses'])
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
            self.config, self.catalog = deepcopy(frozen), {'pick':{}}
            self.operation_program, self.preloaded = None, {}
            self.memory = SimpleNamespace(read=lambda:{})
            self.planner = SimpleNamespace(close=lambda:None)
            self.root, self.closed, self.action_queues = tmp_path/str(len(models)), False, {}
            self.observed = []
            models.append(self)
        def update_obs_batch(self, rows):
            self.observed.append(rows)
            self.planner.plan()
            self.sessions = {0:{'skill':SimpleNamespace(describe=lambda:{'name':'pick','revision':'frozen'})}}
        def get_action_batch(self, indices):
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
            missing = len(executed)==1 and not self.lost
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
    assert len(models)==2 and all(m.closed for m in models)
    rows = [json.loads(p.read_text()) for p in sorted((tmp_path/'runner').glob('*/[0-9]*.json'))]
    controls = [json.loads(r['messages'][-1]['content'][0]['text']) for r in requests]
    receipts = [json.loads(p.read_text()) for p in (tmp_path/'evidence').rglob('receipt.json')]
    logs = list((home/'sessions').rglob('*.jsonl'))
    assert len(logs)==1 and len(receipts)==(1 if configured else 2) and all(r['status']=='completed' for r in receipts)
    events = [json.loads(line) for line in logs[0].read_text().splitlines()]
    assert sum(e.get('type')=='assistant/message' for e in events)==(1 if configured else 2)
    assert not any(e.get('type')=='tool/call' for e in events)
    assert all(r['thinking']=={'type':'enabled'} and r['reasoning_effort']=='high' for r in requests)
    actual = {
        'provider_calls':len(requests), 'one_session':len({r['session_id'] for r in receipts})==1,
        'request_phases':[c['phase'] for c in controls],
        'observation_sequences':[c['current_observation_status']['observation_seq'] for c in controls],
        'recovery_trigger_sequence':next(r['observation_seq'] for r in rows if r['type']=='observed_event'),
        'rgb_available':[c['current_observation_status']['rgb_available'] for c in controls],
        'history_roles':[m['role'] for m in requests[-1]['messages']],
        'native_observation_counts_at_first_action':observation_counts,
        'dispatched_actions':[r['action'] for r in rows if r['type']=='action_dispatched'],
        'discarded_tail':next(r['discarded_runner_actions'] for r in rows if r['type']=='stop_acknowledged'),
        'synthetic_terminal_success':rows[-1]['type']=='native_outcome' and rows[-1]['success'],
    }
    assert actual == fixture['configured_expected' if configured else 'expected']
    configured_records = [json.loads(p.read_text()) for p in (tmp_path/'runner').glob('*/configured-plan-*.json')]
    assert len(configured_records)==int(configured)
    if configured:
        receipt = configured_records[0]
        assert receipt['origin']=='deployment' and receipt['model_calls']==0
        assert receipt['plan']==dict(config['initial_plan'], execution='bind')
        context = json.loads(requests[0]['messages'][-1]['content'][1]['text'])
        assert context['instruction']=='pick' and context['previous_plan']['plan']==receipt['plan']
        assert len(list((tmp_path/'budget').glob('call-*.json')))==1
