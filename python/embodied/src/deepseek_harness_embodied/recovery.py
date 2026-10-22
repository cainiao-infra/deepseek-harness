"""Stop-observe-plan-commit coordination; the execution provider owns physical facts."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

from deepseek_harness import JsonObject


@dataclass(frozen=True)
class RuntimeState:
    """Provider-owned observation and pinned execution identities; unknown facts stay absent."""
    episode_id: str
    generation: int
    observation_seq: int
    observed_at: float
    catalog_revision: str
    binding_revision: str
    facts: JsonObject
    native_success: bool


@dataclass(frozen=True)
class ExecutionEvent:
    """Deduplicated observed trigger; benchmark intervention labels are not accepted."""
    event_id: str
    observation_seq: int
    kind: Literal["grasp_failed", "target_changed", "observation_lost", "no_progress", "skill_failed"]
    facts: JsonObject = field(default_factory=dict)
    """Observed trigger measurements, excluding benchmark intervention labels."""


@dataclass(frozen=True)
class StopReceipt:
    """Provider acknowledgement that all old actions are quiescent for this episode."""
    episode_id: str
    generation: int
    stopped_at: float
    observation_seq: int
    queued_actions: int
    in_flight_actions: int


@dataclass(frozen=True)
class PlanRevision:
    """A replacement over one fresh state and the episode's frozen capability catalog."""
    revision_id: str
    parent_id: str
    episode_id: str
    generation: int
    observation_seq: int
    catalog_revision: str
    binding_revision: str
    plan: JsonObject
    execution_key: str
    """Provider-validated digest of capability and executable parameters, excluding rationale."""
    request_id: str = ""
    """Planner receipt identity when the provider persists model requests."""


@dataclass(frozen=True)
class RecoveryBudget:
    """Episode-wide bounds; a stop timeout never authorizes replacement execution."""
    deadline: float
    stop_timeout_seconds: float
    observation_max_age_seconds: float
    max_revisions: int
    max_events: int

    def __post_init__(self) -> None:
        import math
        if any(not math.isfinite(v) or v <= 0 for v in (
            self.deadline, self.stop_timeout_seconds, self.observation_max_age_seconds)):
            raise ValueError("Positive finite recovery deadlines and ages required")
        if any(type(v) is not int or v <= 0 for v in (self.max_revisions, self.max_events)):
            raise ValueError("Positive revision and event bounds required")


class PhysicalRuntime(Protocol):
    """Execution provider responsibilities; reset and close do not implement stop."""
    def stop(self, *, deadline: float) -> StopReceipt:
        """Block until old queued/in-flight actions stop, or raise with an unknown outcome."""
        ...

    def observe_after(self, receipt: StopReceipt, *, deadline: float) -> RuntimeState:
        """Return a new physical observation acquired after confirmed stopping."""
        ...

    def validate(self, revision: PlanRevision, state: RuntimeState) -> None:
        """Verify execution_key and reject unavailable capabilities, unknown preconditions or invalid bindings."""
        ...

    def bind(self, revision: PlanRevision) -> None:
        """Install an already committed replacement; raises on an unknown binding outcome."""
        ...

    def actions(self, revision_id: str) -> object:
        """Deliver one action chunk for the current revision only."""
        ...


class RecoveryCoordinator:
    """Gate every action chunk and actively replace plans only after physical stop.

    Callers serialize these methods on the execution thread. Every exception
    leaves action dispatch disabled; only a fresh episode may resume it.
    """
    def __init__(self, *, runtime: PhysicalRuntime, state: RuntimeState,
                 initial_revision: PlanRevision, budget: RecoveryBudget,
                 plan: Callable[[RuntimeState, PlanRevision, ExecutionEvent, float], PlanRevision],
                 record: Callable[[JsonObject], None], clock: Callable[[], float] = time.monotonic) -> None:
        self.runtime, self.state, self.revision = runtime, state, initial_revision
        self.budget, self.plan, self.record, self.clock = budget, plan, record, clock
        self.status: Literal["running", "stopping", "planning", "failed", "completed"] = "failed"
        self._events: set[str] = set()
        self._revisions = {initial_revision.revision_id}
        self._check_revision(initial_revision, state, initial_revision.parent_id)
        runtime.validate(initial_revision, state)
        self.status = "running"

    def _check_state(self, state: RuntimeState) -> None:
        if (state.episode_id, state.generation, state.catalog_revision) != (
            self.state.episode_id, self.state.generation, self.state.catalog_revision):
            raise RuntimeError("Stale episode, generation or capability catalog")
        age = self.clock() - state.observed_at
        if age < 0 or age > self.budget.observation_max_age_seconds:
            raise RuntimeError("Physical observation is stale or has an invalid clock")
        if self.clock() >= self.budget.deadline:
            raise TimeoutError("Episode budget exhausted")

    def _check_revision(self, revision: PlanRevision, state: RuntimeState, parent: str) -> None:
        self._check_state(state)
        if (revision.episode_id, revision.generation, revision.observation_seq,
            revision.catalog_revision, revision.binding_revision, revision.parent_id) != (
                state.episode_id, state.generation, state.observation_seq,
                state.catalog_revision, state.binding_revision, parent):
            raise RuntimeError("Plan revision does not match the current physical state")

    def actions(self) -> object:
        """Deliver actions only while a validated current revision is running."""
        if self.status != "running":
            raise RuntimeError("Action dispatch disabled: " + self.status)
        try:
            self._check_state(self.state)
            return self.runtime.actions(self.revision.revision_id)
        except BaseException:
            self.status = "failed"
            raise

    def observe(self, state: RuntimeState) -> None:
        """Refresh authoritative state; only native success terminates the episode."""
        if self.status != "running":
            raise RuntimeError("Cannot observe into an inactive execution")
        try:
            self._check_state(state)
            if state.observation_seq <= self.state.observation_seq:
                raise RuntimeError("Observation sequence must advance")
            if state.binding_revision != self.state.binding_revision:
                raise RuntimeError("Execution binding changed outside plan commit")
            self.state = state
            if state.native_success:
                self.status = "completed"
                self.record({"type": "native_success", "revision_id": self.revision.revision_id})
        except BaseException:
            self.status = "failed"
            raise

    def recover(self, event: ExecutionEvent) -> bool:
        """Stop, acquire fresh state, validate and commit one action-changing replacement.

        Duplicate events return false. Advice-only revisions are refused. A
        provider without a physical stop implementation cannot enable recovery.
        """
        if self.status != "running":
            raise RuntimeError("Cannot recover an inactive execution")
        if event.event_id in self._events:
            return False
        self.status = "stopping"
        try:
            self._check_state(self.state)
            if event.observation_seq != self.state.observation_seq:
                raise RuntimeError("Trigger does not match the current observation")
            if len(self._events) >= self.budget.max_events or len(self._revisions) > self.budget.max_revisions:
                raise RuntimeError("Recovery budget exhausted")
            if event.kind not in {"grasp_failed", "target_changed", "observation_lost", "no_progress", "skill_failed"}:
                raise ValueError("Unknown observed trigger")
            self._events.add(event.event_id)
            requested_at = self.clock()
            stop_deadline = min(self.budget.deadline, requested_at + self.budget.stop_timeout_seconds)
            self.record({"type": "stop_requested", "event_id": event.event_id,
                         "revision_id": self.revision.revision_id})
            stopped = self.runtime.stop(deadline=stop_deadline)
            if (stopped.episode_id, stopped.generation) != (self.state.episode_id, self.state.generation):
                raise RuntimeError("Stop acknowledgement belongs to another execution")
            if (stopped.queued_actions != 0 or stopped.in_flight_actions != 0
                    or not requested_at <= stopped.stopped_at <= self.clock()
                    or self.clock() > stop_deadline):
                raise RuntimeError("Physical stop was not confirmed within its deadline")
            fresh = self.runtime.observe_after(stopped, deadline=self.budget.deadline)
            self._check_state(fresh)
            if (fresh.observation_seq <= max(stopped.observation_seq, self.state.observation_seq)
                    or fresh.observed_at < stopped.stopped_at):
                raise RuntimeError("Replacement requires an observation after stopping")
            if fresh.native_success:
                self.state = fresh
                self.record({"type": "native_success", "revision_id": self.revision.revision_id})
                self.status = "completed"
                return False
            self.status = "planning"
            replacement = self.plan(fresh, self.revision, event, self.budget.deadline)
            self._check_revision(replacement, fresh, self.revision.revision_id)
            if (replacement.revision_id in self._revisions
                    or replacement.execution_key == self.revision.execution_key):
                raise RuntimeError("Replacement must be new and change the executable plan")
            self.runtime.validate(replacement, fresh)
            self._check_state(fresh)
            self.record({"type": "plan_committed", "revision_id": replacement.revision_id,
                         "parent_id": replacement.parent_id, "observation_seq": fresh.observation_seq,
                         "execution_key": replacement.execution_key,
                         "request_id": replacement.request_id,
                         "plan": replacement.plan})
            self._revisions.add(replacement.revision_id)
            self.runtime.bind(replacement)
            self.revision, self.state = replacement, fresh
            self.status = "running"
            return True
        except BaseException:
            self.status = "failed"
            raise
