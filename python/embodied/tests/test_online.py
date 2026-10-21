"""Runner checkpoints discard real cached tails and block unknown outcomes."""
import json
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from deepseek_harness_embodied.budget import CallBudget
from deepseek_harness_embodied.online_model import execution_key
from deepseek_harness_embodied.recovery import ExecutionEvent, PlanRevision, RecoveryBudget, RecoveryCoordinator
from deepseek_harness_embodied.robodojo import ObservedEvents, RoboDojoRuntime


def test_call_budget_survives_reopening_and_refuses_changed_limit(tmp_path):
    assert CallBudget(tmp_path, 2).reserve({'request': 'unknown'}) == 0
    assert CallBudget(tmp_path, 2).reserve({'request': 'success'}) == 1
    with pytest.raises(RuntimeError, match='exhausted'):
        CallBudget(tmp_path, 2).reserve({})
    with pytest.raises(ValueError, match='different limit'):
        CallBudget(tmp_path, 3)
    with pytest.raises(RuntimeError, match='exhausted'):
        CallBudget(tmp_path/'zero', 0).reserve({})


def test_execution_key_ignores_history_and_rationale_but_consumes_sensors():
    def messages(history, sensor):
        return [{}, {'content': [{'text':'preconditions'}, {'text':json.dumps({
            'instruction':'pick', 'observation':sensor, 'previous_plan':history})}]}]
    plan = {'composition':'pick','execution':'restart','rationale':'a'}
    original = execution_key(plan, messages('old', {'position':1}))
    assert execution_key(dict(plan, rationale='b'), messages('new', {'position':1})) == original
    assert execution_key(plan, messages('old', {'position':2})) != original
    assert execution_key(dict(plan, execution='bind'), messages('old', {'position':1})) != original


def test_observed_events_require_command_error_and_measured_absence_of_motion():
    detector = ObservedEvents(window=3, motion_epsilon=.01, command_epsilon=.1, motion_guards=[])
    def obs(position=0):
        return {'instruction':'pick','vision':{'head':{'color':b'image'}},
                'state':{'right_arm_joint_state':[position]}}
    assert detector.observe(obs(), None, 1) is None
    assert detector.observe(obs(), {'right_arm_joint_state':[1]}, 2) is None
    assert detector.observe(obs(), {'right_arm_joint_state':[1]}, 3) is None
    assert detector.observe(obs(), {'right_arm_joint_state':[1]}, 4).kind == 'no_progress'
    moving = ObservedEvents(window=3, motion_epsilon=.01, command_epsilon=.1, motion_guards=[])
    for n in range(6):
        assert moving.observe(obs(n*.2), {'right_arm_joint_state':[1]}, n) is None
    assert moving.observe({'vision':{}}, None, 7).kind == 'observation_lost'


def setup_runtime(tmp_path, fault=None):
    executed, calls, evidence = [], [], []
    class Queue:
        def is_empty(self):
            return fault != 'controls'
    class Env:
        num_envs, success = 1, [False]
        robot_manager = SimpleNamespace(control_manager=SimpleNamespace(control_queue=[Queue()]))
        def get_obs(self):
            return {'instruction':'pick', 'state':{'right_arm_joint_state':[len(executed)]},
                    'vision':{'head':{'color':b'image'}}}
        def is_episode_end(self):
            return False
        def take_action(self, action):
            executed.append(action['x'])
    class Client:
        def call(self, *, func_name, obs):
            calls.append(func_name)
            if func_name == 'online_start':
                revision = PlanRevision('initial','',obs['episode_id'],0,obs['observation_seq'],
                    'catalog','binding',{'execution':'bind'},'initial-key')
                return asdict(revision)
            if func_name == 'online_validate':
                return {'validated':obs['revision']['revision_id']}
            if func_name == 'online_bind':
                assert evidence[-1]['type'] == 'plan_committed'
                return {'bound':obs['revision']['revision_id']}
            if func_name == 'online_actions':
                return {'revision_id':obs['revision_id'], 'actions':[
                    {'x':'old-1'}, {'x':'old-2'}] if obs['revision_id']=='initial' else [{'x':'new-1'}]}
            if func_name == 'online_stop':
                if fault == 'stop':
                    raise TimeoutError('worker stop unknown')
                return {k:obs[k] for k in ('episode_id','generation')} | {
                    'queued_actions':0,'in_flight_actions':0,'worker_reaped':True}
            if func_name == 'online_plan':
                return asdict(PlanRevision('replacement','initial',obs['episode_id'],0,
                    obs['observation_seq'],'catalog','binding',{'execution':'restart'},'new-key'))
            raise AssertionError(func_name)
    runtime = RoboDojoRuntime(Env(), Client(), root=tmp_path, deadline=time.monotonic()+30)
    def record(row):
        evidence.append(row)
    runtime.record = record
    state, initial = runtime.initialize()
    class Provider:
        def __getattr__(self, name):
            return getattr(runtime,name)
        def validate(self, revision, state):
            if revision != initial:
                runtime.validate(revision,state)
    coordinator = RecoveryCoordinator(runtime=Provider(), state=state, initial_revision=initial,
        budget=RecoveryBudget(runtime.deadline, 5, 30, 2, 2), plan=runtime.replan, record=record)
    return runtime, coordinator, executed, calls, evidence


def test_runner_reaps_old_skill_discards_tail_and_executes_new_revision(tmp_path):
    runtime, coordinator, executed, calls, records = setup_runtime(tmp_path)
    coordinator.actions()
    coordinator.observe(runtime.capture())
    assert len(runtime.queue) == 1
    assert coordinator.recover(ExecutionEvent('event',runtime.seq,'no_progress'))
    coordinator.actions()
    assert executed == ['old-1','new-1']
    assert calls.index('online_stop') < calls.index('online_plan') < len(calls)-1
    ack = next(row for row in records if row['type']=='stop_acknowledged')
    assert ack['discarded_runner_actions'] == 1
    kinds = [r['type'] for r in records]
    assert kinds.index('stop_acknowledged') < kinds.index('fresh_observation')
    assert kinds.index('fresh_observation') < kinds.index('plan_committed',1)


@pytest.mark.parametrize('fault',['stop','controls'])
def test_unknown_stop_or_native_controls_prevent_planning_and_actions(tmp_path, fault):
    runtime, coordinator, executed, calls, records = setup_runtime(tmp_path, fault)
    with pytest.raises((RuntimeError,TimeoutError)):
        coordinator.recover(ExecutionEvent('event',runtime.seq,'no_progress'))
    assert runtime.blocked and coordinator.status == 'failed'
    assert 'online_plan' not in calls
    with pytest.raises(RuntimeError, match='disabled'):
        coordinator.actions()
    assert executed == []


@pytest.mark.parametrize('stale', [False, True])
def test_native_binding_restarts_worker_without_resetting_scene(tmp_path, monkeypatch, stale):
    import sys
    from copy import deepcopy
    from types import ModuleType
    from deepseek_harness_embodied import online_model as module
    config = {'skills':{},'compositions':['pick']}
    skill_config = tmp_path/'skills.json'
    skill_config.write_text(json.dumps(config))
    settings = {'schema':'dsh-robodojo-online/v1','call_budget':{'root':str(tmp_path/'budget'),'limit':2},
        'evidence_root':str(tmp_path/'evidence'), 'episode_timeout_seconds':30,'rpc_timeout_seconds':20,
        'events':{'window':3,'motion_epsilon':.01,'command_epsilon':.1,'motion_guards':[{'path':['state','gripper'],'minimum':.9,'maximum':1.0}]},
        'recovery':{'stop_timeout_seconds':5,'observation_max_age_seconds':30,'max_revisions':1,'max_events':1},
        'restart_guards':{'pick':[
            {'path':['state','gripper'],'minimum':.9,'maximum':1.0}]}}
    settings_file = tmp_path/'online.json'
    settings_file.write_text(json.dumps(settings))
    monkeypatch.setenv('PHYSICALRSI_SKILL_CONFIG',str(skill_config))
    monkeypatch.setenv('DSH_ROBODOJO_ONLINE_CONFIG',str(settings_file))
    natives, seen = [], []
    class Native:
        def __init__(self, model_cfg):
            self.preloaded, self.operation_program = {}, None
            self.config, self.catalog = deepcopy(config), {'pick':{'steps':['memory.snapshot','code']}}
            self.memory = SimpleNamespace(read=lambda:{})
            self.planner = SimpleNamespace(close=lambda:None)
            self.root = tmp_path/f'native-{len(natives)}'
            self.action_queues, self.closed = {}, False
            natives.append(self)
        def update_obs_batch(self, rows):
            self.observed = rows
            self.chosen = self.planner.plan()
            self.sessions = {0:{'skill':SimpleNamespace(describe=lambda:{'name':'pick','revision':'frozen'})}}
        def get_action_batch(self, indices):
            return [[{'x':len(natives)}]]
        def close(self):
            self.closed = True
    class Bridge:
        def begin_episode(self, *args):
            return 0
        def plan(self, **kwargs):
            seen.append(kwargs)
            phase = 'bind' if len(seen)==1 else 'restart'
            return kwargs['validate']({'composition':'pick','rationale':phase,'execution':phase})
        def reset(self, *args):
            pass
    def transport(**kwargs):
        return SimpleNamespace(bridge=Bridge(), run_id='run',timeout=10,close=lambda:None)
    monkeypatch.setattr(module,'PhysicalRSITransport',transport)
    native_module = ModuleType('PhysicalRSI_baselines.robodojo.skill_model')
    native_module.Model = Native
    planner_module = ModuleType('PhysicalRSI_baselines.robodojo.agent_planner')
    def builder(**kwargs):
        return [{'role':'system','content':'Return JSON with exactly composition and rationale as nonempty strings.'}, {'role':'user','content':[{
            'type':'text','text':json.dumps({'instruction':kwargs['instruction'],
                'observation':kwargs['observation'],'previous_plan':kwargs['previous_plan']})}]}]
    planner_module.build_planner_messages = builder
    planner_module.validate_planner_result = lambda value, **kwargs:value
    monkeypatch.setitem(sys.modules,native_module.__name__,native_module)
    monkeypatch.setitem(sys.modules,planner_module.__name__,planner_module)
    model = module.OnlineModel({})
    def packet(**kwargs):
        return dict(episode_id='episode',generation=0,**kwargs)
    observation = {'instruction':'pick','vision':{'head':{'color':'rgb'}},'state':{'gripper':1}}
    first = model.online_start(packet(observation=observation,observation_seq=1,
        deadline=time.monotonic()+30,event=None,executed_actions=0))
    model.online_bind(packet(revision=first))
    assert model.online_actions(packet(revision_id=first['revision_id']))['actions']==[{'x':1}]
    assert model.online_stop(packet())['worker_reaped']
    assert natives[0].closed
    if stale:
        with pytest.raises(ValueError,match='newer than the trigger'):
            model.online_plan(packet(observation=observation,observation_seq=1,
                event={'kind':'no_progress','observation_seq':1},executed_actions=3))
        assert model.failed and model.stopped and len(seen)==1
        model.close()
        return
    second = model.online_plan(packet(observation=observation,observation_seq=2,
        event={'kind':'no_progress','observation_seq':1},executed_actions=3))
    model.online_bind(packet(revision=second))
    assert len(natives)==2 and natives[0].closed and not natives[1].closed
    assert natives[1].chosen == {'composition':'pick','rationale':'restart'}
    assert model.online_actions(packet(revision_id=second['revision_id']))['actions']==[{'x':2}]
    assert seen[0]['episode_session'] and seen[1]['episode_session']
    current = json.loads(seen[1]['messages'][1]['content'][0]['text'])['current_observation_status']
    assert current == {'observation_seq':2,'rgb_available':True,'rgb_cameras':['head']}
    assert second['parent_id']==first['revision_id'] and second['execution_key'] != first['execution_key']
    with pytest.raises(RuntimeError,match='stale revision'):
        model.online_actions(packet(revision_id=first['revision_id']))
    assert model.failed
    model.close()


@pytest.mark.parametrize('change', [
    lambda c:c.update(episode_timeout_seconds=-1),
    lambda c:c['events'].update(window=1),
    lambda c:c['call_budget'].update(limit=True),
    lambda c:c.update(recovery_enabled=1),
    lambda c:c['restart_guards']['instruction-grounded-pickup'][0].update(minimum=2),
])
def test_invalid_online_configuration_fails_before_model_start(tmp_path, change):
    from pathlib import Path
    from deepseek_harness_embodied.online_config import load_online_config
    example = Path(__file__).parents[1]/'configs/robodojo-online.example.json'
    config = json.loads(example.read_text())
    change(config)
    path = tmp_path/'config.json'
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_online_config(path)


@pytest.mark.parametrize('enabled', [False, True])
def test_recovery_ablation_retains_initial_binding_and_native_outcome(tmp_path, monkeypatch, enabled):
    from pathlib import Path
    from deepseek_harness_embodied import robodojo as module
    runtime, initial_coordinator, executed, calls, records = setup_runtime(tmp_path)
    runtime.env.is_episode_end = lambda:len(executed) == 2
    runtime.env.success = [True]
    lost = False
    def observation():
        nonlocal lost
        missing = len(executed) == 1 and not lost
        lost |= missing
        return {'instruction':'pick','state':{'right_arm_joint_state':[len(executed)]},
                'vision':{} if missing else {'head':{'color':b'image'}}}
    runtime.env.get_obs = observation
    runtime.initialize = lambda:(initial_coordinator.state, runtime.current)
    original = runtime.client.call
    def call(*, func_name, **kwargs):
        if func_name in {'online_observe','reset'}:
            calls.append(func_name)
            return None
        return original(func_name=func_name, **kwargs)
    runtime.client.call = call
    monkeypatch.setattr(module, 'RoboDojoRuntime', lambda *args, **kwargs:runtime)
    config = json.loads((Path(__file__).parents[1]/'configs/robodojo-online.example.json').read_text())
    config.update(recovery_enabled=enabled, evidence_root=str(tmp_path),
                  call_budget={'root':str(tmp_path/'budget'),'limit':3})
    path = tmp_path/'online.json'
    path.write_text(json.dumps(config))
    monkeypatch.setenv('DSH_ROBODOJO_ONLINE_CONFIG', str(path))
    module.eval_one_episode(runtime.env, runtime.client)
    assert executed == ['old-1', 'new-1' if enabled else 'old-2']
    assert ('online_stop' in calls) is enabled
    assert ('online_plan' in calls) is enabled
    assert any(row['type']=='observed_event' for row in records)
    assert records[-1]['type']=='native_outcome' and records[-1]['success']
    assert calls[-1] == 'reset' and runtime.blocked


def test_expected_closed_gripper_contact_is_not_arm_motion_failure():
    guards = [{'path':[source,'gripper'],'minimum':.9,'maximum':1.0} for source in ('state','command')]
    detector = ObservedEvents(window=3,motion_epsilon=.01,command_epsilon=.1,motion_guards=guards)
    def observe(seq, measured, commanded):
        return detector.observe({'instruction':'pick','vision':{'head':{'color':b'rgb'}},
            'state':{'right_arm_joint_state':[0.0],'gripper':[measured]}},
            {'right_arm_joint_state':[1.0],'gripper':[commanded]},seq)
    for seq in range(10):
        assert observe(seq,0.0,0.0) is None
    for seq in range(10,20):
        assert observe(seq,1.0,0.0) is None
    assert observe(20,1.0,1.0) is None
    assert observe(21,1.0,1.0) is None
    event = observe(22,1.0,1.0)
    assert event.kind == 'no_progress'
    assert event.facts['observed_motion']==0 and event.facts['command_error']==1
    assert event.facts['first_joints']==event.facts['current_joints']=={'right_arm_joint_state':[0.0]}


@pytest.mark.parametrize('operations',[False,True])
def test_enhanced_prompt_has_one_consistent_json_field_instruction(operations):
    from deepseek_harness_embodied.online_model import online_prompt
    native = ('Return JSON with exactly composition, operations, and rationale.' if operations else
              'Return JSON with exactly composition and rationale as nonempty strings.')
    source = native+' Use capability geometry; keep the implementation stable for the episode.'
    result = online_prompt(source,operations=operations)
    assert native not in result and result.count('Return JSON with exactly') == 1
    assert 'rationale, and execution' in result
    assert 'Use capability geometry' in result
    assert 'stable for the episode' not in result
    with pytest.raises(ValueError,match='Unrecognized native'):
        online_prompt('Different schema instruction',operations=operations)


def test_online_response_schema_uses_string_handles_and_phase_specific_decisions():
    from deepseek_harness_embodied.online_model import online_response_schema
    catalog = {'registered-composition':{}}
    program = {'operations':{'registered-operation':{}}}
    schema = online_response_schema(catalog,program,initial=True)
    assert schema['properties']['operations']['items']=={'type':'string','enum':['registered-operation']}
    assert schema['properties']['execution']['enum']==['bind','abort']
    assert set(schema['required'])=={'composition','operations','rationale','execution'}
    assert not schema['additionalProperties']
    assert online_response_schema(catalog,program,initial=False)['properties']['execution']['enum']==['restart','switch','abort']
    assert 'operations' not in online_response_schema(catalog,None,initial=True)['properties']


def test_current_rgb_status_does_not_use_historical_fault_facts():
    from deepseek_harness_embodied.online_model import observation_status
    supplied = {'vision':{'head':{'color':b'fresh'},'wrist':{'color':b'fresh'}}}
    assert observation_status(supplied,9)['rgb_available'] is True
    supplied['vision']['wrist']['color'] = None
    assert observation_status(supplied,10)['rgb_available'] is False
    assert observation_status({'vision':{}},11)['rgb_available'] is False
