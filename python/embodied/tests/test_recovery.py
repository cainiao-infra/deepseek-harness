"""A replacement changes execution only after acknowledged stop and fresh observation."""
from dataclasses import replace

import pytest

from deepseek_harness_embodied.recovery import (
    ExecutionEvent, PlanRevision, RecoveryBudget, RecoveryCoordinator, RuntimeState, StopReceipt)


def case(fault=None):
    now = [10.0]
    initial = RuntimeState('episode', 0, 1, 10., 'catalog', 'binding', {}, False)
    revision = PlanRevision('plan-0', '', 'episode', 0, 1, 'catalog', 'binding', {'operation':'old'}, 'old-key')
    records, order = [], []
    class Runtime:
        def stop(self, *, deadline):
            order.append('stop')
            with pytest.raises(RuntimeError, match='dispatch disabled'):
                coordinator.actions()
            if fault == 'stop_timeout':
                raise TimeoutError('stop unknown')
            now[0] = 11.
            return StopReceipt('episode', 0, 11., 1, 1 if fault == 'queued' else 0, 0)
        def observe_after(self, receipt, *, deadline):
            order.append('observe')
            return replace(initial, observation_seq=1 if fault == 'old_observation' else 2,
                observed_at=11., native_success=fault == 'already_done')
        def validate(self, plan, state):
            order.append('validate')
            if fault == 'precondition' and plan.revision_id != 'plan-0':
                raise RuntimeError('grasp state unknown')
        def bind(self, plan):
            order.append('bind')
            assert records[-1]['type'] == 'plan_committed'
            if fault == 'bind':
                raise OSError('unknown binding')
        def actions(self, revision_id):
            order.append(revision_id)
            return revision_id
    def plan(state, previous, event, deadline):
        order.append('plan')
        if fault == 'late':
            now[0] = 101.
        return PlanRevision('plan-1', previous.revision_id, state.episode_id, state.generation,
            state.observation_seq, 'changed' if fault == 'catalog' else state.catalog_revision,
            state.binding_revision,
            {**previous.plan, 'rationale':'different advice'} if fault == 'advice' else {'operation':'new'},
            previous.execution_key if fault == 'advice' else 'new-key')
    coordinator = RecoveryCoordinator(runtime=Runtime(), state=initial, initial_revision=revision,
        budget=RecoveryBudget(100., 5., 30., 1, 3), plan=plan,
        record=records.append, clock=lambda:now[0])
    return coordinator, records, order


def test_replacement_drives_new_actions_and_deduplicates_trigger():
    coordinator, records, order = case()
    assert coordinator.actions() == 'plan-0'
    event = ExecutionEvent('failure-1', 1, 'grasp_failed')
    assert coordinator.recover(event)
    assert coordinator.actions() == 'plan-1'
    assert not coordinator.recover(event)
    assert order == ['validate','plan-0','stop','observe','plan','validate','bind','plan-1']
    assert [r['type'] for r in records] == ['stop_requested','plan_committed']
    with pytest.raises(RuntimeError, match='budget'):
        coordinator.recover(ExecutionEvent('failure-2', 2, 'grasp_failed'))
    assert coordinator.status == 'failed'


@pytest.mark.parametrize('fault', ['stop_timeout','queued','old_observation','precondition',
                                   'catalog','advice','late','bind'])
def test_failed_recovery_never_dispatches_replacement_actions(fault):
    coordinator, records, order = case(fault)
    with pytest.raises((RuntimeError, TimeoutError, OSError)):
        coordinator.recover(ExecutionEvent('event', 1, 'target_changed'))
    assert coordinator.status == 'failed'
    with pytest.raises(RuntimeError, match='dispatch disabled'):
        coordinator.actions()
    assert 'plan-1' not in order
    if fault != 'bind':
        assert 'bind' not in order


def test_only_native_outcome_can_complete_execution():
    coordinator, records, order = case()
    coordinator.observe(replace(coordinator.state, observation_seq=2, native_success=True))
    assert coordinator.status == 'completed'
    assert records[-1]['type'] == 'native_success'
    with pytest.raises(RuntimeError, match='dispatch disabled'):
        coordinator.actions()
