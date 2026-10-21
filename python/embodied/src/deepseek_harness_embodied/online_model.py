"""Single-environment RoboDojo service using native frozen PhysicalRSI bindings.

The evaluation thread owns robot stopping. This service owns synchronous skill
inference, model Sessions, and replacement admission; it cannot stop a robot.
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from .online_config import load_online_config
from .bridge import RequestIdentity, digest, write_record
from .budget import CallBudget
from .physicalrsi import PhysicalRSITransport
from .recovery import PlanRevision


class CommittedPlanner:
    """Supply an admitted native plan without making another model request."""
    def __init__(self, plan: dict) -> None:
        self.plan_value = plan

    def plan(self, **_kwargs):
        """Return the pinned plan to PhysicalRSI's unchanged binding implementation."""
        return deepcopy(self.plan_value)


def execution_key(plan: dict, messages: list) -> str:
    """Hash only consumed execution choices and fresh sensor inputs, excluding history."""
    blocks = messages[1]['content']
    context = json.loads(blocks[1]['text'])
    return digest({'execution': {k: v for k, v in plan.items() if k != 'rationale'},
                   'instruction': context['instruction'], 'observation': context['observation'],
                   'images': blocks[2:]})


def online_prompt(native: str, *, operations: bool) -> str:
    """Retain native capability guidance with one consistent enhanced JSON field set."""
    before = ('Return JSON with exactly composition, operations, and rationale.' if operations else
              'Return JSON with exactly composition and rationale as nonempty strings.')
    after = ('Return JSON with exactly composition, operations, rationale, and execution.' if operations else
             'Return JSON with exactly composition, rationale, and execution as nonempty strings.')
    if native.count(before) != 1:
        raise ValueError('Unrecognized native planner schema instruction; review the source variant')
    return native.replace(before, after).replace(
        'keep the implementation stable for the episode.',
        'keep the implementation stable until an admitted recovery checkpoint.') + (
        ' Online execution permits a replacement only after acknowledged stopping and fresh observation. '
        'Field execution must be bind for the first plan; restart to discard the old skill '
        'state and recompute the same capability from current RGB/proprioception; switch to bind '
        'a different registered capability; abort when safe recovery is unsupported. '
        'Rationale alone never changes actions. Use current observations as physical truth; '
        'history may contain obsolete images. A restart is allowed only with the supplied '
        'restart preconditions. Return JSON only.')


class OnlineModel:
    """Serialized WebSocket methods for one code-policy environment.

    Only reviewed code-policy configurations with explicit restart guards are
    admitted. Any service failure disables actions until reset. Rebinding closes
    and reaps the old inference worker; it does not reset the physical scene.
    """
    def __init__(self, model_cfg: dict) -> None:
        from PhysicalRSI_baselines.robodojo.skill_model import Model
        self.factory, self.model_cfg = Model, model_cfg
        self.settings = load_online_config(Path(os.environ['DSH_ROBODOJO_ONLINE_CONFIG']))
        if self.settings.get('schema') != 'dsh-robodojo-online/v1':
            raise ValueError('Versioned online configuration required')
        budget = self.settings['call_budget']
        self.call_budget = CallBudget(Path(budget['root']), budget['limit'])
        self.transport = PhysicalRSITransport(reserve_call=self.call_budget.reserve)
        if self.settings['rpc_timeout_seconds'] <= self.transport.timeout:
            self.transport.close()
            raise ValueError('RPC timeout must exceed the model turn timeout')
        self.root = Path(self.settings['evidence_root'])
        frozen = json.loads(Path(os.environ['PHYSICALRSI_SKILL_CONFIG']).read_text())
        if any(entry.get('implementation', name) != 'code-policy'
               for name, entry in frozen['skills'].items()):
            self.transport.close()
            raise ValueError('Online v1 supports synchronous code-policy workers only')
        self.native = self.factory(model_cfg)
        if self.native.preloaded:
            self.native.close()
            self.transport.close()
            raise ValueError('Online v1 supports synchronous code-policy workers only')
        self.catalog = deepcopy(self.native.catalog)
        self.operation_program = deepcopy(self.native.operation_program)
        self.config = deepcopy(self.native.config)
        self.memory = self.native.memory.read()
        self.catalog_revision = digest({'config': self.config, 'catalog': self.catalog, 'memory': self.memory})
        self.binding_revision = digest({'deployment': model_cfg, 'catalog': self.catalog_revision})
        self.episode = None
        self.revision = None
        self.proposal = None
        self.stopped = True
        self.failed = False
        self.bound = False

    def _check(self, packet: dict) -> None:
        if self.failed or packet['episode_id'] != self.episode:
            raise RuntimeError('Inactive or stale online episode')
        if packet['generation'] != self.generation:
            raise RuntimeError('Stale online generation')
        if self.catalog_revision != digest({'config': self.config, 'catalog': self.catalog, 'memory': self.memory}):
            raise RuntimeError('Frozen capability snapshot changed')
        source = Path(os.environ['PHYSICALRSI_SKILL_CONFIG'])
        if json.loads(source.read_text()) != self.config:
            raise RuntimeError('Skill configuration changed during the episode')
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Online episode deadline exhausted')

    def _native_plan(self, plan: dict) -> dict:
        from PhysicalRSI_baselines.robodojo.agent_planner import validate_planner_result
        return validate_planner_result(plan, compositions=self.catalog, operation_program=self.operation_program)

    def _guard(self, plan: dict, observation: dict) -> None:
        handle = plan.get('operations', [None])[0] if plan.get('operations') else plan['composition']
        guards = self.settings['restart_guards'].get(handle)
        if not isinstance(guards, list) or not guards:
            raise ValueError('Capability has no reviewed restart preconditions')
        for guard in guards:
            value = observation
            for part in guard['path']:
                value = value[part]
            if hasattr(value, 'tolist'):
                value = value.tolist()
            if isinstance(value, (list, tuple)):
                if len(value) != 1:
                    raise ValueError('Restart guard requires a scalar observation')
                value = value[0]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('Restart precondition is unknown')
            if not guard['minimum'] <= value <= guard['maximum']:
                raise ValueError('Observed state does not permit restarting this capability')
        vision = observation.get('vision', {})
        if not vision or any('color' not in camera or camera['color'] is None for camera in vision.values()):
            raise ValueError('Fresh RGB observation required before binding')

    def online_start(self, packet: dict) -> dict:
        """Allocate the episode and generate an unbound initial plan."""
        if self.episode is not None:
            raise RuntimeError('Reset required before a new online episode')
        self.episode = packet['episode_id']
        self.generation = self.transport.bridge.begin_episode(0, self.episode)
        self.deadline = packet['deadline']
        return self.online_plan(dict(packet, generation=self.generation))

    def online_plan(self, packet: dict) -> dict:
        """Propose bind, restart, switch or abort using current RGB and durable history."""
        from PhysicalRSI_baselines.robodojo.agent_planner import build_planner_messages
        try:
            self._check(packet)
            if not self.stopped or self.proposal is not None:
                raise RuntimeError('Planning requires a stopped execution with no pending proposal')
            observation = packet['observation']
            previous = None if self.revision is None else {
                'plan': self.revision.plan, 'execution_event': packet['event'],
                'executed_actions': packet['executed_actions'],
                'current_state_authoritative': True}
            messages = build_planner_messages(instruction=observation['instruction'], observation=observation,
                memory=self.memory, previous_plan=previous, compositions=self.catalog,
                operation_program=self.operation_program)
            messages[0]['content'] = online_prompt(messages[0]['content'], operations=self.operation_program is not None)
            messages[1]['content'].insert(0, {'type': 'text', 'text': json.dumps({
                'phase': 'initial' if self.revision is None else 'recovery',
                'restart_preconditions': self.settings['restart_guards']}, allow_nan=False)})
            def validate(value):
                if not isinstance(value, dict) or 'execution' not in value:
                    raise ValueError('Executable online decision required')
                native = self._native_plan({k: v for k, v in value.items() if k != 'execution'})
                execution = value['execution']
                if execution == 'abort':
                    raise RuntimeError('Planner declined recovery; actions remain disabled')
                if (self.revision is None and execution != 'bind') or (
                        self.revision is not None and execution not in {'restart', 'switch'}):
                    raise ValueError('Invalid execution decision for current phase')
                if self.revision is not None:
                    before = {k: v for k, v in self.revision.plan.items() if k not in {'rationale', 'execution'}}
                    same = {k: v for k, v in native.items() if k != 'rationale'} == before
                    if same != (execution == 'restart'):
                        raise ValueError('Restart must retain the capability; switch must change it')
                self._guard(native, observation)
                return value
            identity = RequestIdentity(self.transport.run_id, self.episode, uuid.uuid4().hex, 0,
                self.generation, min(self.deadline, time.monotonic() + self.transport.timeout))
            plan = self.transport.bridge.plan(messages=messages, identity=identity, mode='online',
                schema_version='dsh-robodojo-plan/v1', validate=validate, episode_session=True)
            revision = PlanRevision(uuid.uuid4().hex, '' if self.revision is None else self.revision.revision_id,
                self.episode, self.generation, packet['observation_seq'], self.catalog_revision,
                self.binding_revision, plan, execution_key(plan, messages), identity.request_id)
            self.proposal = (revision, observation)
            return asdict(revision)
        except BaseException:
            self.failed = True
            raise

    def online_validate(self, packet: dict) -> dict:
        """Recheck the one pending proposal and its frozen executable inputs."""
        self._check(packet)
        if self.proposal is None or packet['revision'] != asdict(self.proposal[0]):
            raise RuntimeError('Unknown or modified plan proposal')
        revision, observation = self.proposal
        self._native_plan({k: v for k, v in revision.plan.items() if k != 'execution'})
        self._guard(revision.plan, observation)
        return {'validated': revision.revision_id}

    def online_bind(self, packet: dict) -> dict:
        """Apply a committed proposal through PhysicalRSI's native binding path."""
        try:
            self.online_validate(packet)
            if not self.stopped:
                raise RuntimeError('Stop acknowledgement required before binding')
            revision, observation = self.proposal
            if self.bound:
                raise RuntimeError('Previous skill worker has not been reaped')
            if self.native.closed:
                self.native = self.factory(self.model_cfg)
            if self.native.config != self.config or self.native.catalog != self.catalog:
                raise RuntimeError('Native binding catalog changed')
            self.native.planner.close()
            self.native.planner = CommittedPlanner({k: v for k, v in revision.plan.items() if k != 'execution'})
            self.native.update_obs_batch([dict(observation, env_idx=0)])
            self._check(packet)
            write_record(self.root / self.episode / ('binding-' + revision.revision_id + '.json'), {
                'revision': asdict(revision), 'native_root': str(self.native.root),
                'skill': self.native.sessions[0]['skill'].describe()})
            self.revision, self.proposal = revision, None
            self.stopped, self.bound = False, True
            return {'bound': revision.revision_id}
        except BaseException:
            self.failed = True
            raise

    def online_stop(self, packet: dict) -> dict:
        """Reap inference workers and discard policy tails; runner must stop robot dispatch."""
        self.stopped = True
        try:
            self._check(packet)
            self.native.action_queues.clear()
            self.native.close()
            self.bound = False
            return {'episode_id': self.episode, 'generation': self.generation,
                    'queued_actions': 0, 'in_flight_actions': 0, 'worker_reaped': True}
        except BaseException:
            self.failed = True
            raise

    def online_observe(self, packet: dict) -> None:
        """Feed each observation to the currently bound native skill."""
        self._check(packet)
        if self.stopped:
            raise RuntimeError('Cannot update an inactive skill')
        self.native.update_obs_batch([dict(packet['observation'], env_idx=0)])

    def online_actions(self, packet: dict) -> dict:
        """Return one revision-tagged native chunk; stale revisions never produce actions."""
        try:
            self._check(packet)
            if self.stopped or packet['revision_id'] != self.revision.revision_id:
                raise RuntimeError('Action dispatch disabled or stale revision')
            chunks = self.native.get_action_batch([0])
            self._check(packet)
            return {'revision_id': self.revision.revision_id, 'actions': chunks[0]}
        except BaseException:
            self.failed = True
            raise

    def reset(self) -> None:
        """Invalidate old inference and release owned workers; no physical stop is asserted."""
        self.stopped = True
        self.native.close()
        if self.episode is not None:
            self.transport.bridge.reset(0)
        self.episode, self.revision, self.proposal = None, None, None
        self.failed, self.bound = False, False

    def close(self) -> None:
        """Close owned skill and DSH processes after dispatch has stopped."""
        try:
            self.native.close()
        finally:
            self.transport.close()
