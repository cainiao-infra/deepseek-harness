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
from .admission import load_admission, decision_schema, validate_decision
from .bridge import RequestIdentity, digest, write_record
from .budget import CallBudget
from .physicalrsi import PhysicalRSITransport
from .recovery import PlanRevision
from .native_binding import CodeBinding, NativeBinding
from .skill_failure import SkillInferenceFailure


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
        'restart preconditions. Operations must be an array of operation ID strings, never '
        'objects or parameter dictionaries. Runtime parameters are not accepted; the original '
        'instruction is passed unchanged to the selected skill. Follow response_schema and return JSON only.'
        + OBSERVATION_TIMING_GUIDANCE)


OBSERVATION_TIMING_GUIDANCE = (
    ' The previous_plan.execution_event describes the trigger BEFORE stopping and recapture; '
    'its facts are historical, not the status of the current observation. '
    'current_observation_status describes the newly supplied observation and images. '
    'If its rgb_available is true, an earlier observation_lost event does not mean RGB is '
    'currently missing. Evaluate restart using the current images and restart preconditions; '
    'do not abort solely because the historical trigger had rgb_available=false.')


def observation_status(observation: dict, sequence: int) -> dict:
    """Describe only the current supplied camera availability, without benchmark state."""
    vision = observation.get('vision', {})
    cameras = sorted(name for name, camera in vision.items() if camera.get('color') is not None)
    return {'observation_seq': sequence, 'rgb_available': bool(vision) and len(cameras) == len(vision),
            'rgb_cameras': cameras}


def online_response_schema(compositions: dict, operation_program: dict | None, *, initial: bool) -> dict:
    """Describe the exact native field types and admitted handles for this planning phase."""
    properties = {
        'composition':{'type':'string','enum':sorted(compositions)},
        'rationale':{'type':'string','minLength':1},
        'execution':{'type':'string','enum':['bind','abort'] if initial else ['restart','switch','abort']},
    }
    if operation_program is not None:
        properties['operations'] = {'type':'array','maxItems':64,'uniqueItems':True,
            'items':{'type':'string','enum':sorted(operation_program['operations'])}}
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}


class OnlineModel:
    """Serialized WebSocket methods for one native-policy environment.

    Initial bindings retain native validation. Replacements require
    explicit reviewed restart guards. Unconfirmed service failures disable actions
    until reset. Opt-in confirmed skill faults require the runner stop checkpoint
    before recovery. Rebinding never resets the physical scene.
    """
    def __init__(self, model_cfg: dict) -> None:
        from PhysicalRSI_baselines.robodojo.skill_model import Model
        self.settings = load_online_config(Path(os.environ['DSH_ROBODOJO_ONLINE_CONFIG']))
        if self.settings.get('schema') != 'dsh-robodojo-online/v1':
            raise ValueError('Versioned online configuration required')
        self.admission = None
        if 'admission_policy' in self.settings:
            self.admission = load_admission(self.settings['admission_policy'])
        self.admission_context = None
        self.current_candidate = None
        self.latest_observation = None
        budget = self.settings['call_budget']
        self.call_budget = CallBudget(Path(budget['root']), budget['limit'])
        frozen = json.loads(Path(os.environ['PHYSICALRSI_SKILL_CONFIG']).read_text())
        self.transport = PhysicalRSITransport(reserve_call=self.call_budget.reserve)
        if self.settings['rpc_timeout_seconds'] <= self.transport.timeout:
            self.transport.close()
            raise ValueError('RPC timeout must exceed the model turn timeout')
        self.root = Path(self.settings['evidence_root'])
        try:
            self.binding: NativeBinding
            if 'native_worker' in self.settings:
                from .process_binding import ProcessBinding
                self.binding = ProcessBinding(model_cfg, frozen, self.settings['native_worker'],
                                              self.root/'native-workers')
            else:
                self.binding = CodeBinding(Model, model_cfg, frozen)
        except BaseException:
            self.transport.close()
            raise
        self.catalog = deepcopy(self.binding.metadata['catalog'])
        self.operation_program = deepcopy(self.binding.metadata['operation_program'])
        self.config = deepcopy(self.binding.metadata['config'])
        self.memory = deepcopy(self.binding.metadata['memory'])
        try:
            self.initial_plan = (self._native_plan(deepcopy(self.settings['initial_plan']))
                                 if 'initial_plan' in self.settings else None)
        except BaseException:
            try:
                self.binding.close()
            finally:
                self.transport.close()
            raise
        self.catalog_revision = digest({'config': self.config, 'catalog': self.catalog, 'memory': self.memory})
        self.binding_revision = digest({'deployment': model_cfg, 'catalog': self.catalog_revision,
                                        'provider': self.binding.identity})
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
            if self.admission is not None:
                return self._admissible_plan(packet)
            observation = packet['observation']
            if self.revision is not None and packet['observation_seq'] <= packet['event']['observation_seq']:
                raise ValueError('Recovery planning requires an observation newer than the trigger')
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
                'current_observation_status': observation_status(observation, packet['observation_seq']),
                'response_schema': online_response_schema(self.catalog, self.operation_program,
                                                         initial=self.revision is None),
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
                if self.revision is not None:
                    self._guard(native, observation)
                return value
            identity = RequestIdentity(self.transport.run_id, self.episode, uuid.uuid4().hex, 0,
                self.generation, min(self.deadline, time.monotonic() + self.transport.timeout))
            if self.revision is None and self.initial_plan is not None:
                plan = validate(dict(deepcopy(self.initial_plan), execution='bind'))
                write_record(self.root/self.episode/('configured-plan-'+identity.request_id+'.json'), {
                    'origin':'deployment','identity':asdict(identity),'plan':plan,
                    'messages':messages,'catalog_revision':self.catalog_revision,
                    'binding_revision':self.binding_revision,'model_calls':0})
            else:
                plan = self.transport.bridge.plan(messages=messages, identity=identity, mode='online',
                    schema_version='dsh-robodojo-plan/v1', validate=validate, episode_session=True)
            revision = PlanRevision(uuid.uuid4().hex, '' if self.revision is None else self.revision.revision_id,
                self.episode, self.generation, packet['observation_seq'], self.catalog_revision,
                self.binding_revision, plan, execution_key(plan, messages), identity.request_id)
            write_record(self.root/self.episode/('revision-'+revision.revision_id+'.json'), asdict(revision))
            self.proposal = (revision, observation)
            return asdict(revision)
        except BaseException:
            self.failed = True
            raise

    def _admissible_plan(self, packet: dict) -> dict:
        """Resolve qualified candidates before any model turn and retain a v2 decision."""
        from PhysicalRSI_baselines.robodojo.agent_planner import build_planner_messages
        observation = packet['observation']
        parent = '' if self.revision is None else self.revision.revision_id
        observed_at = packet['observed_at']
        if (type(observed_at) not in (int,float) or not math.isfinite(observed_at)
                or not 0 <= time.monotonic()-observed_at <= self.settings['recovery']['observation_max_age_seconds']):
            raise ValueError('Stale admission observation')
        if parent and packet['observation_seq'] <= packet['event']['observation_seq']:
            raise ValueError('Recovery requires a newer observation')
        candidates = self.admission.resolve(self.binding.metadata, observation,
            sequence=packet['observation_seq'], parent=parent, replacement=bool(parent), restart_check=self._guard)
        write_record(self.root/self.episode/('candidates-'+uuid.uuid4().hex+'.json'), candidates)
        def stopped(reason):
            result = {'stopped':{'reason':reason, 'subgoal_id':self.admission.document['subgoal_id']}}
            write_record(self.root/self.episode/('stopped-'+uuid.uuid4().hex+'.json'), result)
            return result
        if not candidates['candidates']:
            return stopped('no_admissible_candidate')
        selected_names = {row['selection']['composition'] for row in candidates['candidates']}
        operations = {value for row in candidates['candidates'] for value in row['selection']['operations']}
        program = None if self.operation_program is None else {
            **self.operation_program, 'operations':{key:value for key,value in self.operation_program['operations'].items()
                                                  if key in operations}}
        messages = build_planner_messages(instruction=observation['instruction'], observation=observation,
            memory=self.memory, previous_plan=None if self.revision is None else {
                'plan':self.revision.plan,'execution_event':packet['event'],
                'executed_actions':packet['executed_actions']},
            compositions={key:value for key,value in self.catalog.items() if key in selected_names},
            operation_program=program)
        messages[0]['content'] = ('Choose only an admitted candidate_id from the current candidate set, '
            'or return kind=stop with a reason. Follow response_schema exactly. '
            'Do not invent composition, operations or parameters. Current observations are authoritative. '
            + OBSERVATION_TIMING_GUIDANCE)
        projection = {key:value for key,value in candidates.items() if key != 'excluded'}
        messages[1]['content'].insert(0, {'type':'text','text':json.dumps({
            'candidate_set':projection,'response_schema':decision_schema(candidates),
            'current_observation_status':observation_status(observation, packet['observation_seq'])}, allow_nan=False)})
        identity = RequestIdentity(self.transport.run_id, self.episode, uuid.uuid4().hex, 0,
            self.generation, min(self.deadline, time.monotonic()+self.transport.timeout))
        if not parent and self.initial_plan is not None:
            selection = {key:self.initial_plan.get(key, [] if key=='operations' else None)
                         for key in ('composition','operations')}
            candidate = next((row for row in candidates['candidates'] if row['selection']==selection), None)
            if candidate is None:
                return stopped('configured_candidate_not_admitted')
            decision = validate_decision({'kind':'execute','candidate_id':candidate['id'],
                'candidate_set_revision':candidates['revision'],'observation_seq':packet['observation_seq'],
                'parent_revision':parent,'rationale':self.initial_plan['rationale']}, candidates)
            write_record(self.root/self.episode/('configured-decision-'+identity.request_id+'.json'), {
                'decision':decision,'messages':messages,'model_calls':0})
        else:
            decision = self.transport.bridge.plan(messages=messages, identity=identity, mode='online',
                schema_version='dsh-robodojo-plan/v2', validate=lambda value:validate_decision(value,candidates),
                episode_session=True)
        if decision['kind']=='stop':
            return stopped(decision['reason'])
        candidate = next(row for row in candidates['candidates'] if row['id']==decision['candidate_id'])
        native = self._native_plan({**candidate['selection'],'rationale':decision['rationale']})
        same = parent and {key:self.revision.plan[key] for key in candidate['selection']}==candidate['selection']
        plan = {**native,'execution':('restart' if same else 'switch') if parent else 'bind'}
        revision = PlanRevision(uuid.uuid4().hex, parent, self.episode, self.generation,
            packet['observation_seq'],self.catalog_revision,self.binding_revision,
            plan,execution_key(plan,messages),identity.request_id)
        write_record(self.root/self.episode/('revision-'+revision.revision_id+'.json'), asdict(revision))
        if not parent and self.initial_plan is not None:
            write_record(self.root/self.episode/('configured-plan-'+identity.request_id+'.json'), {
                'origin':'deployment','identity':asdict(identity),'plan':plan,'messages':messages,
                'catalog_revision':self.catalog_revision,'binding_revision':self.binding_revision,'model_calls':0})
        write_record(self.root/self.episode/('decision-'+revision.revision_id+'.json'), {
            'decision':decision,'candidate_set_revision':candidates['revision'],'revision':asdict(revision)})
        self.admission_context = {'decision':decision,'candidate':candidate,'created':observed_at}
        self.proposal = (revision, observation)
        return asdict(revision)

    def online_validate(self, packet: dict) -> dict:
        """Recheck the one pending proposal and its frozen executable inputs."""
        self._check(packet)
        if self.proposal is None or packet['revision'] != asdict(self.proposal[0]):
            raise RuntimeError('Unknown or modified plan proposal')
        revision, observation = self.proposal
        if self.admission is not None:
            context = self.admission_context
            if (packet.get('observation_seq') != revision.observation_seq
                    or time.monotonic()-context['created'] > self.settings['recovery']['observation_max_age_seconds']):
                raise ValueError('Stale admission observation')
            current = self.admission.resolve(self.binding.metadata, observation, sequence=revision.observation_seq,
                parent=revision.parent_id, replacement=bool(revision.parent_id), restart_check=self._guard)
            validate_decision(context['decision'], current)
        self._native_plan({k: v for k, v in revision.plan.items() if k != 'execution'})
        if revision.parent_id:
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
            try:
                applied = self.binding.bind(
                    {k:v for k,v in revision.plan.items() if k != 'execution'}, observation)
            except SkillInferenceFailure as error:
                return self._skill_failure(error, revision)
            self._check(packet)
            write_record(self.root / self.episode / ('binding-' + revision.revision_id + '.json'), {
                'revision': asdict(revision), **applied})
            self.revision, self.proposal = revision, None
            if self.admission is not None:
                self.current_candidate = self.admission_context['candidate']
                self.latest_observation = observation
            self.stopped, self.bound = False, True
            return {'bound': revision.revision_id}
        except BaseException:
            self.failed = True
            raise

    def _skill_failure(self, error: SkillInferenceFailure, revision: PlanRevision) -> dict:
        if not self.settings['recover_skill_failures']:
            raise error
        if self.admission is not None:
            candidate = (self.admission_context['candidate'] if self.proposal is not None else self.current_candidate)
            observed = self.proposal[1] if self.proposal is not None else self.latest_observation
            self.admission.record_failure(candidate, error.handling, observed)
        diagnostic_id = uuid.uuid4().hex
        failure = {'kind':error.kind,'operation':error.operation,'diagnostic_id':diagnostic_id,
                   'episode_id':self.episode,'generation':self.generation,'revision_id':revision.revision_id}
        if self.admission is not None:
            failure['handling'] = error.handling
        self.stopped, self.bound = True, False
        self.revision, self.proposal = revision, None
        write_record(self.root/self.episode/('skill-failure-'+diagnostic_id+'.json'), {
            **failure,'diagnostic':error.record(),'inference_cleanup_confirmed':True})
        return {'skill_failure':failure}

    def online_stop(self, packet: dict) -> dict:
        """Reap inference workers and discard policy tails; runner must stop robot dispatch."""
        self.stopped = True
        try:
            self._check(packet)
            self.binding.close()
            self.bound = False
            return {'episode_id': self.episode, 'generation': self.generation,
                    'queued_actions': 0, 'in_flight_actions': 0, 'worker_reaped': True}
        except BaseException:
            self.failed = True
            raise

    def online_observe(self, packet: dict) -> dict | None:
        """Feed each observation to the currently bound native skill."""
        try:
            self._check(packet)
            if self.stopped:
                raise RuntimeError('Cannot update an inactive skill')
            try:
                self.latest_observation = packet['observation']
                self.binding.observe(packet['observation'])
            except SkillInferenceFailure as error:
                return self._skill_failure(error, self.revision)
        except BaseException:
            self.failed = True
            raise

    def online_actions(self, packet: dict) -> dict:
        """Return one revision-tagged native chunk; stale revisions never produce actions."""
        try:
            self._check(packet)
            if self.stopped or packet['revision_id'] != self.revision.revision_id:
                raise RuntimeError('Action dispatch disabled or stale revision')
            try:
                actions = self.binding.actions()
            except SkillInferenceFailure as error:
                return self._skill_failure(error, self.revision)
            self._check(packet)
            return {'revision_id': self.revision.revision_id, 'actions': actions}
        except BaseException:
            self.failed = True
            raise

    def reset(self) -> None:
        """Invalidate old inference and release owned workers; no physical stop is asserted."""
        if self.episode is None and not self.failed and not self.bound:
            return
        self.stopped = True
        self.binding.close()
        if self.episode is not None:
            self.transport.bridge.reset(0)
        self.episode, self.revision, self.proposal = None, None, None
        self.failed, self.bound = False, False
        if self.admission is not None:
            self.admission.reset()
        self.admission_context = self.current_candidate = self.latest_observation = None

    def close(self) -> None:
        """Close owned skill and DSH processes after dispatch has stopped."""
        try:
            self.binding.close()
        finally:
            self.transport.close()
