"""Native execution ownership and capability stability across stop/rebind."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from deepseek_harness_embodied.native_binding import CodeBinding, CommittedPlanner
from deepseek_harness_embodied.skill_failure import SkillInferenceFailure


def factory(models, change=None):
    def create(deployment):
        model = SimpleNamespace(config={'skills':{}}, catalog={'pick':{}},
            operation_program=None, memory=SimpleNamespace(read=lambda:{}),
            preloaded={}, action_queues={0:['old-tail']}, closed=False, root='native-root',
            planner=SimpleNamespace(close=lambda:None))
        def close():
            model.closed = True
        def observe(rows):
            assert not model.closed
            model.observed = rows
            model.chosen = model.planner.plan()
            model.sessions = {0:{'skill':SimpleNamespace(describe=lambda:{'name':'pick'})}}
        model.close, model.update_obs_batch = close, observe
        model.get_action_batch = lambda indices:[[{'joint':[1]}]]
        if change is not None and models:
            change(model)
        models.append(model)
        return model
    return create


def test_committed_plan_is_copied_and_cannot_call_a_model():
    plan = {'composition':'pick','operations':['one']}
    planner = CommittedPlanner(plan)
    plan['operations'].append('external-change')
    returned = planner.plan()
    returned['operations'].clear()
    assert planner.plan() == {'composition':'pick','operations':['one']}
    planner.close()


def test_binding_stops_native_tail_and_reopens_the_same_capabilities():
    models = []
    binding = CodeBinding(factory(models), {}, {'skills':{}})
    observation = {'instruction':'pick','state':{'joint':[0]}}
    original = deepcopy(observation)
    with pytest.raises(RuntimeError, match='stopped'):
        binding.actions()
    assert binding.bind({'composition':'pick'}, observation)['skill'] == {'name':'pick'}
    assert observation == original and models[0].observed[0]['env_idx']==0
    assert binding.actions() == [{'joint':[1]}]
    with pytest.raises(RuntimeError, match='Stopping'):
        binding.bind({'composition':'pick'}, observation)
    binding.close()
    assert models[0].closed and models[0].action_queues == {}
    with pytest.raises(RuntimeError, match='stopped'):
        binding.observe(observation)
    binding.bind({'composition':'pick'}, observation)
    assert len(models)==2 and models[0].closed and not models[1].closed
    binding.close()


@pytest.mark.parametrize('change', [
    lambda m:m.config.update(changed=True),
    lambda m:m.catalog.update(new={}),
    lambda m:setattr(m,'memory',SimpleNamespace(read=lambda:{'changed':True})),
])
def test_changed_capabilities_or_memory_close_the_replacement_before_actions(change):
    models = []
    binding = CodeBinding(factory(models, change), {}, {'skills':{}})
    binding.bind({'composition':'pick'}, {'instruction':'pick'})
    binding.close()
    with pytest.raises(RuntimeError, match='metadata changed'):
        binding.bind({'composition':'pick'}, {'instruction':'pick'})
    assert models[1].closed and not binding.bound
    with pytest.raises(RuntimeError, match='stopped'):
        binding.actions()


def test_learned_policy_configuration_is_rejected_before_in_process_creation():
    models = []
    with pytest.raises(ValueError, match='code-policy'):
        CodeBinding(factory(models), {}, {'skills':{'pi05':{'configuration':{}}}})
    assert models == []


def test_native_closed_flag_cannot_acknowledge_an_earlier_failed_cleanup():
    models = []
    binding = CodeBinding(factory(models), {}, {'skills':{}})
    binding.bind({'composition':'pick'}, {'instruction':'pick'})
    def close():
        if models[0].closed:
            return
        models[0].closed = True
        raise TimeoutError('worker exit unconfirmed')
    models[0].close = close
    with pytest.raises(TimeoutError, match='unconfirmed'):
        binding.close()
    with pytest.raises(RuntimeError, match='cleanup is unconfirmed'):
        binding.close()
    with pytest.raises(RuntimeError, match='cleanup is unconfirmed'):
        binding.bind({'composition':'pick'}, {'instruction':'pick'})
    assert len(models)==1 and not binding.bound and models[0].action_queues=={}


@pytest.mark.parametrize('method', ['observe','actions'])
def test_policy_failure_discards_tail_and_disables_native_delivery(method):
    models = []
    binding = CodeBinding(factory(models), {}, {'skills':{}})
    binding.bind({'composition':'pick'}, {'instruction':'pick'})
    def fail(*args):
        raise ValueError('policy failure')
    setattr(models[0], 'update_obs_batch' if method=='observe' else 'get_action_batch', fail)
    with pytest.raises(SkillInferenceFailure, match='policy failure'):
        binding.observe({'instruction':'pick'}) if method=='observe' else binding.actions()
    assert models[0].closed and models[0].action_queues=={} and not binding.bound
    with pytest.raises(RuntimeError, match='stopped'):
        binding.actions()


@pytest.mark.parametrize('invalid', [False, True])
def test_configured_plan_uses_native_validation_before_disposing_planner(monkeypatch, invalid):
    import sys
    from types import ModuleType
    from deepseek_harness_embodied.native_binding import install_initial_plan
    calls = []
    model = factory([])({})
    previous = model.planner
    previous.close = lambda:calls.append('closed')
    module = ModuleType('PhysicalRSI_baselines.robodojo.agent_planner')
    def validate(value, *, compositions, operation_program):
        assert compositions == model.catalog and operation_program is None
        calls.append('validated')
        if value['composition'] not in compositions:
            raise ValueError('Unregistered capability')
        value['rationale'] = 'normalized by native'
        return value
    module.validate_planner_result = validate
    monkeypatch.setitem(sys.modules, module.__name__, module)
    plan = {'composition':'missing' if invalid else 'pick','rationale':'deployment'}
    if invalid:
        with pytest.raises(ValueError, match='Unregistered capability'):
            install_initial_plan(model, plan)
        assert calls==['validated'] and model.planner is previous
    else:
        result = install_initial_plan(model, plan)
        assert calls==['validated','closed'] and plan['rationale']=='deployment'
        assert model.planner.plan()==result and result['rationale']=='normalized by native'
        result['composition']='changed'
        assert model.planner.plan()['composition']=='pick'
