"""RoboDojo execution-thread checkpoint, observed triggers, and revision dispatch.

This provider is restricted to one synchronous simulator environment. It stops
between completed native actions, never during a physics step. No simulation
steps occur while the model plans; this is not a physical-robot stop provider.
"""
from __future__ import annotations

import math
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from pathlib import Path

from .online_config import load_online_config
from .bridge import write_record
from .recovery import (
    ExecutionEvent, PlanRevision, RecoveryBudget, RecoveryCoordinator, RuntimeState, StopReceipt)


class ObservedEvents:
    """Detect unavailable RGB, instruction changes and commanded-but-absent joint motion."""
    def __init__(self, *, window: int, motion_epsilon: float, command_epsilon: float,
                 motion_guards: list[dict]) -> None:
        if type(window) is not int or window < 2 or any(
                not math.isfinite(v) or v <= 0 for v in (motion_epsilon, command_epsilon)):
            raise ValueError('Explicit positive motion thresholds and window >= 2 required')
        self.window, self.motion_epsilon, self.command_epsilon = window, motion_epsilon, command_epsilon
        self.history = deque(maxlen=window)
        self.instruction = None
        self.motion_guards = motion_guards

    def _eligible(self, observation, action) -> bool:
        packet = {'state':observation.get('state', {}), 'command':action}
        for guard in self.motion_guards:
            try:
                value = packet
                for part in guard['path']:
                    value = value[part]
                if hasattr(value, 'tolist'):
                    value = value.tolist()
                if isinstance(value, (list, tuple)) and len(value) == 1:
                    value = value[0]
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or not guard['minimum'] <= value <= guard['maximum']):
                    return False
            except (KeyError, TypeError):
                # Unknown preconditions cannot establish this particular trigger.
                return False
        return True

    def observe(self, observation: dict, action: dict | None, seq: int) -> ExecutionEvent | None:
        """Return one sensor-derived event, excluding intervention and benchmark labels."""
        vision = observation.get('vision')
        if not vision or any(row.get('color') is None for row in vision.values()):
            return ExecutionEvent(f'vision-{seq}', seq, 'observation_lost', {'rgb_available':False})
        instruction = observation.get('instruction')
        previous, self.instruction = self.instruction, instruction
        if previous is not None and instruction != previous:
            return ExecutionEvent(f'instruction-{seq}', seq, 'target_changed', {
                'previous_instruction':previous, 'current_instruction':instruction})
        if not self._eligible(observation, action):
            self.history.clear()
            return None
        state = observation.get('state', {})
        joints = {}
        for name, value in state.items():
            if name.endswith('arm_joint_state'):
                vector = value.tolist() if hasattr(value, 'tolist') else value
                if isinstance(vector, (list, tuple)) and vector and all(
                        isinstance(v, (float, int)) and math.isfinite(v) for v in vector):
                    joints[name] = tuple(vector)
        self.history.append((joints, action))
        if len(self.history) < self.window or not joints:
            return None
        first = self.history[0][0]
        commanded = set(joints)
        for positions, command in self.history:
            if not command:
                return None
            commanded &= set(positions) & set(command)
        if not commanded:
            return None
        demanded = 0.0
        for positions, command in self.history:
            for name in commanded:
                values, target = positions[name], command[name]
                if len(target) != len(values) or len(first[name]) != len(joints[name]):
                    return None
                demanded = max(demanded, max(abs(float(a)-b) for a, b in zip(target, values)))
        moved = max(abs(a-b) for name in commanded for a, b in zip(first[name], joints[name]))
        if demanded > self.command_epsilon and moved < self.motion_epsilon:
            self.history.clear()
            return ExecutionEvent(f'motion-{seq}', seq, 'no_progress', {
                'window':self.window,'command_error':demanded,'observed_motion':moved,
                'first_joints':{name:list(first[name]) for name in commanded},
                'current_joints':{name:list(joints[name]) for name in commanded}})
        return None


class RoboDojoRuntime:
    """Own the runner tail, RPC skill generation and all simulator action dispatch.

    Every public operation must run on its creating execution thread. A failed
    checkpoint or RPC leaves dispatch blocked; reset is never a stop receipt.
    """
    def __init__(self, env, client, *, root: Path, deadline: float) -> None:
        if env.num_envs != 1:
            raise ValueError('Online RoboDojo v1 requires exactly one environment')
        self.env, self.client, self.root, self.deadline = env, client, root, deadline
        self.owner = threading.get_ident()
        self.episode, self.generation = uuid.uuid4().hex, 0
        self.seq, self.executed, self.record_seq = 0, 0, 0
        self.queue = deque()
        self.in_flight, self.blocked = False, True
        self.observation = None
        self.current = None

    def _thread(self) -> None:
        if threading.get_ident() != self.owner:
            raise RuntimeError('RoboDojo runtime must stay on its execution thread')
        if time.monotonic() >= self.deadline:
            self.blocked = True
            raise TimeoutError('Episode deadline exhausted')

    def record(self, row: dict) -> None:
        """Commit immutable ordered episode evidence before dependent execution."""
        self.record_seq += 1
        write_record(self.root / self.episode / f'{self.record_seq:06d}.json', {
            'episode_id': self.episode, 'generation': self.generation,
            'time': time.monotonic(), **row})

    def packet(self, **kwargs) -> dict:
        """Correlate each RPC with the current episode and reset generation."""
        return dict(episode_id=self.episode, generation=self.generation, **kwargs)

    def capture(self) -> RuntimeState:
        """Acquire RGB and proprioception without advancing simulation or guessing state."""
        self._thread()
        self.observation = self.env.get_obs()
        self.seq += 1
        # Native success is meaningful only after native terminal detection.
        success = bool(self.env.is_episode_end() and self.env.success[0])
        state = RuntimeState(self.episode, self.generation, self.seq, time.monotonic(),
            self.current.catalog_revision if self.current else '',
            self.current.binding_revision if self.current else '',
            {'executed_actions': self.executed}, success)
        return state

    def initialize(self) -> tuple[RuntimeState, PlanRevision]:
        """Plan and commit the initial native binding before delivering any action."""
        state = self.capture()
        row = self.client.call(func_name='online_start', obs=self.packet(
            observation=self.observation, observation_seq=self.seq, deadline=self.deadline,
            event=None, executed_actions=0))
        revision = PlanRevision(**row)
        if revision.episode_id != self.episode or revision.observation_seq != self.seq or revision.parent_id:
            raise RuntimeError('Initial plan has an invalid episode or observation')
        self.generation = revision.generation
        self.current = revision
        state = RuntimeState(self.episode, self.generation, self.seq, state.observed_at,
            revision.catalog_revision, revision.binding_revision, state.facts, state.native_success)
        self.validate(revision, state)
        self.record({'type': 'plan_committed', **asdict(revision)})
        self.bind(revision)
        return state, revision

    def stop(self, *, deadline: float) -> StopReceipt:
        """Discard runner tails, reap skill inference, and verify native controls are drained."""
        self.blocked = True
        self._thread()
        discarded = len(self.queue)
        self.queue.clear()
        if self.in_flight:
            raise RuntimeError('Cannot acknowledge stopping inside an action')
        manager = self.env.robot_manager.control_manager
        if not manager.control_queue[0].is_empty():
            raise RuntimeError('Native control queue is not drained')
        reply = self.client.call(func_name='online_stop', obs=self.packet())
        if reply != self.packet(queued_actions=0, in_flight_actions=0, worker_reaped=True):
            raise RuntimeError('Skill inference stop is unknown')
        if time.monotonic() > deadline or not manager.control_queue[0].is_empty():
            raise TimeoutError('Stop checkpoint not confirmed within deadline')
        receipt = StopReceipt(self.episode, self.generation, time.monotonic(), self.seq, 0, 0)
        self.record({'type': 'stop_acknowledged', **asdict(receipt), 'discarded_runner_actions': discarded})
        return receipt

    def observe_after(self, receipt: StopReceipt, *, deadline: float) -> RuntimeState:
        """Capture a new frame after the synchronous stop checkpoint."""
        if not self.blocked or receipt.episode_id != self.episode or time.monotonic() > deadline:
            raise RuntimeError('Fresh observation requires a current stop checkpoint')
        state = self.capture()
        self.record({'type': 'fresh_observation', 'state': asdict(state)})
        return state

    def validate(self, revision: PlanRevision, state: RuntimeState) -> None:
        """Check the server's pending proposal against runner-owned observation identity."""
        self._thread()
        if revision.episode_id != state.episode_id or revision.generation != state.generation:
            raise RuntimeError('Plan belongs to a different physical execution')
        result = self.client.call(func_name='online_validate', obs=self.packet(revision=asdict(revision)))
        if result != {'validated': revision.revision_id}:
            raise RuntimeError('Native plan validation was not acknowledged')

    def bind(self, revision: PlanRevision) -> None:
        """Bind only while dispatch is blocked and after a durable runner commit."""
        self._thread()
        if not self.blocked or self.queue or self.in_flight:
            raise RuntimeError('Binding requires stopped dispatch with no old tail')
        result = self.client.call(func_name='online_bind', obs=self.packet(revision=asdict(revision)))
        self._thread()
        if result != {'bound': revision.revision_id}:
            raise RuntimeError('Native plan binding outcome is unknown')
        self.current, self.blocked = revision, False
        self.record({'type': 'binding_applied', 'revision_id': revision.revision_id})

    def actions(self, revision_id: str):
        """Execute at most one native action, retaining only revision-tagged cached tails."""
        self._thread()
        if self.blocked or revision_id != self.current.revision_id:
            raise RuntimeError('Action dispatch disabled or stale revision')
        try:
            if not self.queue:
                reply = self.client.call(func_name='online_actions', obs=self.packet(revision_id=revision_id))
                if (reply.get('revision_id') != revision_id or not isinstance(reply.get('actions'), list)
                        or not reply['actions'] or any(not isinstance(a, dict) for a in reply['actions'])):
                    raise ValueError('Malformed or stale native action chunk')
                self.queue.extend(reply['actions'])
            self._thread()
            action = self.queue.popleft()
            self.record({'type': 'action_dispatched', 'revision_id': revision_id,
                         'action_index': self.executed, 'action': action})
            self.in_flight = True
            self.env.take_action(action)
            self.in_flight = False
            if not self.env.robot_manager.control_manager.control_queue[0].is_empty():
                raise RuntimeError('Native action returned with pending controls')
            self.executed += 1
            return action
        except BaseException:
            self.blocked = True
            raise

    def replan(self, state, previous, event, deadline) -> PlanRevision:
        """Generate a replacement from fresh sensors in the same durable DSH Session."""
        self._thread()
        row = self.client.call(func_name='online_plan', obs=self.packet(
            observation=self.observation, observation_seq=state.observation_seq, deadline=deadline,
            executed_actions=self.executed, event=asdict(event)))
        return PlanRevision(**row)


def eval_one_episode(TASK_ENV, model_client) -> None:
    """Run bounded online recovery; native RoboDojo alone supplies terminal success."""
    settings = load_online_config(Path(os.environ['DSH_ROBODOJO_ONLINE_CONFIG']))
    runtime = RoboDojoRuntime(TASK_ENV, model_client, root=Path(settings['evidence_root']),
        deadline=time.monotonic() + settings['episode_timeout_seconds'])
    events = ObservedEvents(**settings['events'])
    coordinator = None
    try:
        state, initial = runtime.initialize()
        # initialize has already validated and consumed the initial proposal.
        class Provider:
            def __getattr__(self, name):
                return getattr(runtime, name)
            def validate(self, revision, current):
                if revision != initial:
                    runtime.validate(revision, current)
        coordinator = RecoveryCoordinator(runtime=Provider(), state=state, initial_revision=initial,
            budget=RecoveryBudget(deadline=runtime.deadline, **settings['recovery']),
            plan=runtime.replan, record=runtime.record)
        coordinator.observe(runtime.capture())
        model_client.call(func_name='online_observe', obs=runtime.packet(observation=runtime.observation))
        events.observe(runtime.observation, None, state.observation_seq)
        while coordinator.status == 'running':
            action = coordinator.actions()
            state = runtime.capture()
            coordinator.observe(state)
            if coordinator.status == 'completed' or TASK_ENV.is_episode_end():
                break
            event = events.observe(runtime.observation, action, state.observation_seq)
            if event is not None:
                runtime.record({'type': 'observed_event', **asdict(event)})
                coordinator.recover(event)
                events = ObservedEvents(**settings['events'])
                events.observe(runtime.observation, None, runtime.seq)
            else:
                model_client.call(func_name='online_observe', obs=runtime.packet(observation=runtime.observation))
        runtime.blocked = True
        runtime.queue.clear()
        runtime.record({'type': 'native_outcome', 'terminal': bool(TASK_ENV.is_episode_end()),
                        'success': bool(TASK_ENV.success[0]), 'actions': runtime.executed,
                        'status': coordinator.status})
    except BaseException as error:
        runtime.blocked = True
        runtime.queue.clear()
        runtime.record({'type': 'execution_failed', 'error_type': type(error).__name__,
                        'detail': str(error), 'actions': runtime.executed})
        raise
    finally:
        runtime.blocked = True
        # Reset releases inference processes only; unknown robot effects stay failed.
        model_client.call(func_name='reset')
