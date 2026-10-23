"""Qualified scope, stale decisions and unchanged failures constrain recovery."""
from copy import deepcopy
import json
from types import SimpleNamespace
import time

import pytest

from deepseek_harness_embodied.admission import AdmissionPolicy, decision_schema, validate_decision
from deepseek_harness_embodied.bridge import digest
from deepseek_harness_embodied.online_model import OnlineModel
from deepseek_harness_embodied.skill_failure import SkillExecutionFault, SkillInferenceFailure, decode_result
from test_qualification import trial_manifest, observation, write


def policy_fixture(root):
    manifest = trial_manifest(root)
    sources = {'native.py':'a'*64}
    descriptor = {'name':'pick','configuration':{'checkpoint':'pinned','output':'original'}}
    spec = json.loads((root/'capability.json').read_text())
    spec['start_predicates'] = []
    spec['skill']['revision'] = digest({'descriptor':descriptor,'sources':sources})
    manifest['capability'] = write(root,'capability.json',spec)
    binding = json.loads((root/'trial/native_binding.json').read_text())
    binding['skill'] = spec['skill']
    manifest['trials'][0]['artifacts']['native_binding'] = write(root,'trial/native_binding.json',binding)
    entry = {'id':'lift','manifest':write(root,'manifest.json',manifest),
        'descriptor':write(root,'descriptor.json',descriptor),'sources':write(root,'sources.json',sources),
        'predicates':[{'id':'instruction','path':['instruction'],'equals':'lift'},
                      {'id':'position','path':['state','position'],'minimum':0,'maximum':2}]}
    document = {'schema':'dsh-admission-policy/v1','subgoal_id':'lift-object','required_effects':['task-complete'],
                'control':spec['control'],'max_transient_retries':1,'entries':[entry]}
    metadata = {'catalog':{'pick':{}},'config':{'skills':{'pick':deepcopy(descriptor)}},'execution_sources':sources}
    obs = observation();obs['state']['position']=0
    return document, metadata, obs


def resolve(policy, metadata, obs, **kwargs):
    return policy.resolve(metadata,obs,sequence=2,parent='old',replacement=True,
                          restart_check=lambda *_:None,**kwargs)


def decision(snapshot):
    return {'kind':'execute','candidate_id':'lift','candidate_set_revision':snapshot['revision'],
            'observation_seq':snapshot['observation_seq'],'parent_revision':snapshot['parent_revision'],'rationale':'execute'}


@pytest.mark.parametrize('change', ['none','output','weights','source','missing','predicate','unknown','effect'])
def test_only_qualified_current_implementations_and_observations_are_admitted(tmp_path,change):
    document, metadata, obs = policy_fixture(tmp_path)
    if change=='output': metadata['config']['skills']['pick']['configuration']['output']='new-private-evidence'
    if change=='weights': metadata['config']['skills']['pick']['configuration']['checkpoint']='different'
    if change=='source': metadata['execution_sources']={}
    if change=='missing': obs['vision']={}
    if change=='predicate': obs['instruction']='different scope'
    if change=='unknown': del obs['state']['position']
    if change=='effect': document['required_effects']=['unmeasured-effect']
    policy=AdmissionPolicy(tmp_path,document);snapshot=resolve(policy,metadata,obs)
    assert bool(snapshot['candidates']) == (change in ('none','output'))
    if change=='unknown': assert snapshot['excluded'][0]['status']=='undetermined'


def test_qualification_identity_cannot_be_relabelled(tmp_path):
    document,_,_=policy_fixture(tmp_path)
    document['entries'][0]['sources']=write(tmp_path,'changed.json',{'native.py':'b'*64})
    with pytest.raises(ValueError,match='Qualification identity'):
        AdmissionPolicy(tmp_path,document)


@pytest.mark.parametrize('retry',['never','after_predicate_change','bounded_transient'])
def test_failure_history_requires_relevant_change_or_remaining_transient_budget(tmp_path,retry):
    doc,metadata,obs=policy_fixture(tmp_path);policy=AdmissionPolicy(tmp_path,doc)
    candidate=resolve(policy,metadata,obs)['candidates'][0]
    handling={'reason':'transient' if retry=='bounded_transient' else 'incompatible',
              'retry':retry,'predicates':['position'] if retry=='after_predicate_change' else [],'effects':'none'}
    policy.record_failure(candidate,handling,obs)
    later=resolve(policy,metadata,obs)
    assert bool(later['candidates']) == (retry=='bounded_transient')
    obs['unrelated_frame_sequence']=100
    assert bool(resolve(policy,metadata,obs)['candidates']) == (retry=='bounded_transient')
    obs['state']['position']=1
    assert bool(resolve(policy,metadata,obs)['candidates']) == (retry!='never')
    if retry=='bounded_transient':
        policy.record_failure(candidate,handling,obs)
        assert not resolve(policy,metadata,obs)['candidates']
    policy.reset()
    assert resolve(policy,metadata,obs)['candidates']


@pytest.mark.parametrize('field,value',[('candidate_id','unavailable'),('candidate_set_revision','stale'),
    ('observation_seq',1),('observation_seq',True),('parent_revision','other'),('kind','restart'),('extra',0)])
def test_decisions_reject_stale_or_unavailable_fields(tmp_path,field,value):
    doc,meta,obs=policy_fixture(tmp_path);snapshot=resolve(AdmissionPolicy(tmp_path,doc),meta,obs)
    result=decision(snapshot);result[field]=value
    with pytest.raises(ValueError):validate_decision(result,snapshot)


def test_stop_decision_requires_no_invented_native_capability(tmp_path):
    doc,meta,obs=policy_fixture(tmp_path);snapshot=resolve(AdmissionPolicy(tmp_path,doc),meta,obs)
    result=decision(snapshot);result.pop('candidate_id');result.update(kind='stop',reason='no safe execution')
    assert validate_decision(result,snapshot)==result
    assert 'composition' not in json.dumps(decision_schema(snapshot))


def model_fixture(root):
    doc,meta,obs=policy_fixture(root);model=OnlineModel.__new__(OnlineModel)
    model.admission=AdmissionPolicy(root,doc);model.binding=SimpleNamespace(metadata=meta)
    model.settings={'recovery':{'observation_max_age_seconds':20}}
    model.root=root/'online';model.episode='episode';model.generation=0
    model.catalog=meta['catalog'];model.operation_program=None;model.memory={}
    model.catalog_revision='catalog';model.binding_revision='binding';model.deadline=time.monotonic()+30
    model.revision=model.proposal=None;model.initial_plan={'composition':'pick','operations':[],'rationale':'fixed'}
    model.failed=False;model.stopped=True;model.bound=False;model._check=lambda _:None
    model._native_plan=lambda x:x;model._guard=lambda *_:None
    model.transport=SimpleNamespace(run_id='run',timeout=10,bridge=SimpleNamespace(plan=lambda **_:pytest.fail('No model call')))
    packet={'episode_id':'episode','generation':0,'observation':obs,'observation_seq':2,
            'observed_at':time.monotonic(),'event':None,'executed_actions':0}
    return model,packet


def test_empty_candidates_skip_model_and_native_message_builder(tmp_path):
    model,packet=model_fixture(tmp_path);packet['observation']['instruction']='outside scope'
    # No native framework is needed to resolve an empty candidate set.
    import sys
    from types import ModuleType
    module=ModuleType('PhysicalRSI_baselines.robodojo.agent_planner')
    module.build_planner_messages=lambda **_:pytest.fail('No request should be built')
    from unittest.mock import patch
    with patch.dict(sys.modules, {module.__name__:module}):
        result=model.online_plan(packet)
    assert result=={'stopped':{'reason':'no_admissible_candidate','subgoal_id':'lift-object'}}
    assert model.proposal is None


def test_provider_fault_handling_survives_private_wire_without_message_parsing():
    handling={'reason':'dependency_unavailable','retry':'never','predicates':[],'effects':'unknown'}
    fault=SkillExecutionFault('private paths and details',handling)
    confirmed=SkillInferenceFailure('actions',type(fault).__name__,str(fault),fault.handling)
    decoded=decode_result({'status':'skill_failed','failure':confirmed.record()},'actions')
    assert decoded.handling==handling
    assert SkillInferenceFailure('actions','UnexpectedError','transient-looking text').handling['retry']=='never'
    with pytest.raises(ValueError):
        SkillExecutionFault('bad retry',dict(handling,retry='bounded_transient'))


@pytest.mark.skipif(__import__('os').environ.get('DSH_EMBODIED_INTEGRATION')!='1', reason='requires build:artifacts')
@pytest.mark.parametrize('assembled',[False,True])
def test_v2_decisions_use_the_shipped_profile_and_one_durable_session(tmp_path,monkeypatch,recorded_provider,assembled):
    import base64
    import sys
    from pathlib import Path
    from dataclasses import replace
    from types import ModuleType
    from deepseek_harness_embodied import PlannerBridge
    from deepseek_harness_embodied.profile import install_source_profile
    from deepseek_harness_embodied.recovery import PlanRevision
    from test_bridge import spec
    model,packet=model_fixture(tmp_path)
    model.initial_plan=None;model.deadline=time.monotonic()+120
    model.settings['recovery']['observation_max_age_seconds']=120
    monkeypatch.setenv('PHYSICALRSI_AGENT_API_KEY','recorded-only')
    png=json.loads((Path(__file__).parent/'fixtures/compatibility-session.json').read_text())['png']
    def response(request):
        context=json.loads(request['messages'][-1]['content'][0]['text'])
        return decision(context['candidate_set'])
    url,requests=recorded_provider([response,response])
    repository=Path(__file__).resolve().parents[3];home=tmp_path/'home'
    install_source_profile(home,repository/'packages/experimental/embodied-planner')
    bridge=PlannerBridge(spec=replace(spec(),base_url=url,calls_per_runtime=3),dsh_home=home,
        evidence_root=tmp_path/'model-evidence',dsh_bin=str(repository/'apps/cli/lib/bin.js'),
        runtime_cwd=str(repository))
    model.transport=SimpleNamespace(bridge=bridge,run_id='admission-loop',timeout=60)
    model.generation=bridge.begin_episode(0,'episode')
    module=ModuleType('PhysicalRSI_baselines.robodojo.agent_planner')
    def messages(**kwargs):
        context={'instruction':kwargs['instruction'],'observation':{'state':kwargs['observation']['state']},
                 'previous_plan':kwargs['previous_plan']}
        return [{'role':'system','content':'native'}, {'role':'user','content':[
            {'type':'text','text':json.dumps(context)},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,'+png}}]}]
    module.build_planner_messages=messages
    monkeypatch.setitem(sys.modules,module.__name__,module)
    revisions=[]
    try:
        if assembled:
            from deepseek_harness_embodied import robodojo
            from deepseek_harness_embodied.online_evidence import audit_online_episode
            settings=json.loads((Path(__file__).parents[1]/'configs/robodojo-online.example.json').read_text())
            settings.update(evidence_root=str(model.root),episode_timeout_seconds=120,
                rpc_timeout_seconds=90,recover_skill_failures=True)
            settings['recovery']['observation_max_age_seconds']=120
            settings_path=tmp_path/'online.json';settings_path.write_text(json.dumps(settings))
            monkeypatch.setenv('DSH_ROBODOJO_ONLINE_CONFIG',str(settings_path))
            model.settings=settings;model.episode=None
            faulted=False;executed=[]
            def actions():
                nonlocal faulted
                if not faulted:
                    faulted=True
                    raise SkillInferenceFailure('actions','SkillExecutionFault','private diagnostic',{
                        'reason':'transient','retry':'bounded_transient','predicates':[],'effects':'none'})
                return [{'joint':[0.25]}]
            model.binding.bind=lambda *_:{'skill':{'name':'pick'}}
            model.binding.close=lambda:None
            model.binding.observe=lambda _:None
            model.binding.actions=actions
            class World:
                num_envs=1
                success=[False]
                robot_manager=SimpleNamespace(control_manager=SimpleNamespace(
                    control_queue=[SimpleNamespace(is_empty=lambda:True)]))
                def get_obs(self):return packet['observation']
                def take_action(self,action):
                    executed.append(action);self.success=[len(executed)==2]
                def is_episode_end(self):return len(executed)==2
            class Client:
                def call(self,*,func_name,obs=None):
                    return getattr(model,func_name)() if func_name=='reset' else getattr(model,func_name)(obs)
            robodojo.eval_one_episode(World(),Client())
            episode=next(p for p in model.root.iterdir() if p.is_dir())
            rows=[json.loads(p.read_text()) for p in sorted(episode.glob('[0-9]*.json'))]
            revisions=[r for r in rows if r['type']=='plan_committed']
            audit=audit_online_episode(episode,tmp_path/'model-evidence')
            assert (audit['actions'],audit['skill_failures'],audit['replacements'])==(2,1,1)
            for path in [next(episode.glob('candidates-*.json')),
                         next(p for p in episode.glob('[0-9]*.json') if json.loads(p.read_text())['type']=='observed_event')]:
                original=path.read_bytes();changed=json.loads(original)
                if 'revision' in changed:changed['predicates']={}
                else:changed['facts']['handling']['retry']='never'
                try:
                    path.write_text(json.dumps(changed))
                    with pytest.raises(ValueError):audit_online_episode(episode,tmp_path/'model-evidence')
                finally:path.write_bytes(original)
        else:
            first=model.online_plan(packet);revisions.append(first)
            model.revision=PlanRevision(**first);model.proposal=None
            later={**packet,'observation_seq':3,'observed_at':time.monotonic(),
                   'event':{'kind':'observation_lost','observation_seq':2},'executed_actions':0}
            revisions.append(model.online_plan(later))
    finally:
        bridge.close()
    controls=[json.loads(r['messages'][-1]['content'][0]['text'])['candidate_set'] for r in requests]
    receipts=[json.loads(p.read_text()) for p in (tmp_path/'model-evidence').rglob('receipt.json')]
    events=[json.loads(line) for p in (home/'sessions').rglob('*.jsonl') for line in p.read_text().splitlines()]
    actual={'requests':len(requests),'one_session':len({r['session_id'] for r in receipts})==1,
        'phases':[c['phase'] for c in controls],'observation_sequences':[c['observation_seq'] for c in controls],
        'candidate_ids':[[v['id'] for v in c['candidates']] for c in controls],
        'executions':[r['plan']['execution'] for r in revisions],
        'history_roles':[m['role'] for m in requests[-1]['messages']],
        'tool_calls':sum(e.get('type')=='tool/call' for e in events)}
    fixture=json.loads((Path(__file__).parent/'fixtures/admission-loop-session.json').read_text())
    expected=fixture['assembled_expected' if assembled else 'expected']
    if assembled:
        context=json.loads(requests[-1]['messages'][-1]['content'][1]['text'])
        actual['failure_handling']=context['previous_plan']['execution_event']['facts']['handling']
        assert 'private diagnostic' not in json.dumps(requests)
    assert actual==expected
    assert all(r['schema_version']=='dsh-robodojo-plan/v2' for r in receipts)
    assert all(r['status']=='completed' for r in receipts)
    assert 'excluded' not in json.dumps(controls)


@pytest.mark.parametrize('mutation',['valid','sequence','age','failure','source'])
def test_commit_rechecks_current_candidates_and_snapshot_age(tmp_path,monkeypatch,mutation):
    import sys
    from types import ModuleType
    model,packet=model_fixture(tmp_path)
    native=ModuleType('PhysicalRSI_baselines.robodojo.agent_planner')
    native.build_planner_messages=lambda **kwargs:[{'role':'system','content':'native'},
        {'role':'user','content':[{'type':'text','text':json.dumps({
            'instruction':kwargs['instruction'],'observation':kwargs['observation']})}]}]
    monkeypatch.setitem(sys.modules,native.__name__,native)
    revision=model.online_plan(packet)
    calls=[]
    model.binding.bind=lambda *args:calls.append(args) or {'skill':{'name':'pick'}}
    request={'revision':revision,'observation_seq':packet['observation_seq']}
    if mutation=='sequence':request['observation_seq']+=1
    elif mutation=='age':model.admission_context['created']-=21
    elif mutation=='failure':model.admission.record_failure(model.admission_context['candidate'],{
        'reason':'incompatible','retry':'never','predicates':[],'effects':'none'},packet['observation'])
    elif mutation=='source':model.binding.metadata['execution_sources']={}
    if mutation=='valid':
        assert model.online_bind(request)=={'bound':revision['revision_id']}
        assert len(calls)==1
    else:
        with pytest.raises(ValueError):model.online_bind(request)
        assert calls==[] and model.failed


@pytest.mark.parametrize('mutation',['path-only','program','launch-digest','missing-plan'])
def test_fixed_program_normalization_requires_qualified_program_evidence(tmp_path,mutation):
    doc,metadata,obs=policy_fixture(tmp_path)
    descriptor=json.loads((tmp_path/'descriptor.json').read_text())
    memory={'programs':{'act':{'steps':[{'operation':'move','revision':'frozen'}]}}}
    plan={'schema':'physicalrsi.episode-operation-plan/v1','episode':'recorded',
          'rationale':'registered fixed program','programs':{'memory':{'act':memory['programs']['act']['steps']}}}
    descriptor['implementation']='code-policy'
    descriptor['configuration'].update(operation_memories={'memory':memory},environment={
        'PROGRAM_ROOT':'fixed','PHYSICALRSI_OPERATION_PLAN':'/old/plan.json',
        'PHYSICALRSI_OPERATION_PLAN_SHA256':digest(plan)})
    source=json.loads((tmp_path/'sources.json').read_text())
    spec=json.loads((tmp_path/'capability.json').read_text());spec['skill']['revision']=digest({
        'descriptor':descriptor,'sources':source})
    manifest=json.loads((tmp_path/'manifest.json').read_text());manifest['capability']=write(tmp_path,'capability.json',spec)
    binding=json.loads((tmp_path/'trial/native_binding.json').read_text());binding['skill']=spec['skill']
    manifest['trials'][0]['artifacts']['native_binding']=write(tmp_path,'trial/native_binding.json',binding)
    doc['entries'][0]['manifest']=write(tmp_path,'manifest.json',manifest)
    doc['entries'][0]['descriptor']=write(tmp_path,'descriptor.json',descriptor)
    doc['entries'][0]['operation_plan']=write(tmp_path,'plan.json',plan)
    metadata['config']['skills']['pick']=deepcopy(descriptor)
    if mutation=='missing-plan':doc['entries'][0].pop('operation_plan')
    elif mutation=='launch-digest':
        plan['programs']['memory']['act']=[]
        doc['entries'][0]['operation_plan']=write(tmp_path,'plan.json',plan)
    else:
        metadata['config']['skills']['pick']['configuration']['environment']['PHYSICALRSI_OPERATION_PLAN']='/new/plan.json'
        if mutation=='program':metadata['config']['skills']['pick']['configuration']['operation_memories']={}
    if mutation in ('missing-plan','launch-digest'):
        with pytest.raises(ValueError,match='operation plan|Fixed program'):
            AdmissionPolicy(tmp_path,doc)
    else:
        snapshot=resolve(AdmissionPolicy(tmp_path,doc),metadata,obs)
        assert bool(snapshot['candidates'])==(mutation=='path-only')


def test_candidate_revision_tracks_observed_predicates_even_without_a_sequence_change(tmp_path):
    document,metadata,obs=policy_fixture(tmp_path);policy=AdmissionPolicy(tmp_path,document)
    first=resolve(policy,metadata,obs)
    obs['state']['position']=1
    second=resolve(policy,metadata,obs)
    assert first['candidates']==second['candidates']
    assert first['revision']!=second['revision']
    with pytest.raises(ValueError):validate_decision(decision(first),second)


def test_recovery_requires_reviewed_restart_support_even_with_qualified_effects(tmp_path):
    document,metadata,obs=policy_fixture(tmp_path)
    def unavailable(*_):raise ValueError('No reviewed restart preconditions')
    snapshot=AdmissionPolicy(tmp_path,document).resolve(metadata,obs,sequence=2,parent='old',
        replacement=True,restart_check=unavailable)
    assert snapshot['candidates']==[]
    assert snapshot['excluded'][0]['status']=='undetermined'
    assert snapshot['excluded'][0]['unknown']==['restart_not_admitted']
