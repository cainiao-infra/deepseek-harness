"""Native execution service used after a DSH plan has been admitted.

The provider owns policy creation, observation delivery, queued actions and
worker cleanup. The online planner owns plan admission and episode history.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import sys
from typing import Protocol

from .skill_failure import SkillInferenceFailure, SkillExecutionFault


class NativeBinding(Protocol):
    """Execution metadata and lifecycle required by the online planning consumer."""
    metadata: dict
    identity: dict

    def bind(self, plan: dict, observation: dict) -> dict:
        """Apply a plan while stopped and return its root and skill description."""
        ...

    def observe(self, observation: dict) -> None:
        """Deliver a current observation; reject calls before binding or after stopping."""
        ...

    def actions(self) -> list:
        """Return a bound policy's chunk; reject calls while stopped."""
        ...

    def close(self) -> None:
        """Discard queued inference and return only after owned workers are reaped."""
        ...


class CommittedPlanner:
    """Supply an admitted native plan without making another model request."""
    def __init__(self, plan: dict) -> None:
        self.plan_value = deepcopy(plan)

    def plan(self, **_kwargs) -> dict:
        """Return the pinned plan to PhysicalRSI's unchanged binding implementation."""
        return deepcopy(self.plan_value)

    def close(self) -> None:
        """Release no resources; this planner owns only the copied plan."""


def metadata(model) -> dict:
    """Capture native capabilities and memory, excluding the per-instance output root."""
    result = deepcopy({'config':model.config, 'catalog':model.catalog,
                       'operation_program':model.operation_program, 'memory':model.memory.read()})
    owner = next((base for base in type(model).__mro__
                  if base.__module__.startswith('PhysicalRSI_baselines.')), None)
    module = None if owner is None else sys.modules.get(owner.__module__)
    if module is not None:
        directory = Path(module.__file__).parent/'skills'
        result['execution_sources'] = {source.name: hashlib.sha256(source.read_bytes()).hexdigest()
                                       for source in sorted(directory.glob('*.py'))}
    return result


def install_initial_plan(model, plan: dict) -> dict:
    """Install one explicit deployment plan through native validation, without inference.

    The caller owns model cleanup if validation or planner disposal fails.
    PhysicalRSI still binds and executes the selected capability normally.
    """
    from PhysicalRSI_baselines.robodojo.agent_planner import validate_planner_result
    validated = validate_planner_result(deepcopy(plan), compositions=model.catalog,
                                        operation_program=model.operation_program)
    model.planner.close()
    model.planner = CommittedPlanner(validated)
    return validated


class OwnedModelBinding:
    """Own native binding inside a process whose caller also owns GPU teardown.

    Learned policies require an isolated worker process. The in-process online
    consumer uses CodeBinding to reject them before construction.
    """
    def __init__(self, factory, deployment: dict) -> None:
        self.factory, self.deployment = factory, deepcopy(deployment)
        self.bound = False
        self.cleanup_error: BaseException | None = None
        self.model = factory(self.deployment)
        try:
            self.metadata = metadata(self.model)
        except BaseException:
            self.model.close()
            raise

    def bind(self, plan: dict, observation: dict) -> dict:
        """Bind through the native planner consumer without resetting the physical scene."""
        if self.cleanup_error is not None:
            raise RuntimeError('Native worker cleanup is unconfirmed; replace the owning service') from self.cleanup_error

        if self.bound:
            raise RuntimeError('Stopping is required before native rebinding')
        try:
            if self.model.closed:
                self.model = self.factory(self.deployment)
                if metadata(self.model) != self.metadata:
                    raise RuntimeError('Native capability or memory metadata changed after stopping')
            self.model.planner.close()
            self.model.planner = CommittedPlanner(plan)
            self._infer('bind', self.model.update_obs_batch, [dict(observation, env_idx=0)])
            applied = {'native_root':str(self.model.root),
                       'skill':self.model.sessions[0]['skill'].describe()}
            self.bound = True
            return applied
        except SkillInferenceFailure:
            raise
        except BaseException:
            self.close()
            raise

    def observe(self, observation: dict) -> None:
        """Preserve the observation and add the native single-environment index."""
        if not self.bound:
            raise RuntimeError('Native observation delivery is disabled while stopped')
        self._infer('observe', self.model.update_obs_batch, [dict(observation, env_idx=0)])

    def actions(self) -> list:
        """Return the single environment's chunk without dispatching robot actions."""
        if not self.bound:
            raise RuntimeError('Native action delivery is disabled while stopped')
        try:
            return self._infer('actions', self.model.get_action_batch, [0])[0]
        except SkillInferenceFailure:
            raise
        except BaseException:
            self.close()
            raise

    def _infer(self, operation, call, value):
        try:
            return call(value)
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise SkillInferenceFailure(operation, type(error).__name__, str(error),
                    error.handling if isinstance(error, SkillExecutionFault) else None) from error
            raise

    def close(self) -> None:
        """Close native workers; an earlier cleanup failure permanently forbids reuse."""
        self.bound = False
        self.model.action_queues.clear()
        try:
            self.model.close()
        except BaseException as error:
            self.cleanup_error = error
            raise
        if self.cleanup_error is not None:
            raise RuntimeError('Native worker cleanup is unconfirmed; replace the owning service') from self.cleanup_error


class CodeBinding(OwnedModelBinding):
    """Reject learned policies before in-process creation; require stable metadata on reopen."""
    def __init__(self, factory, deployment: dict, config: dict) -> None:
        if any(entry.get('implementation', name) != 'code-policy'
               for name, entry in config['skills'].items()):
            raise ValueError('Online v1 supports synchronous code-policy workers only')
        def code_factory(deployment):
            model = factory(deployment)
            if model.preloaded:
                model.close()
                raise ValueError('Online v1 supports synchronous code-policy workers only')
            return model
        super().__init__(code_factory, deployment)
        self.identity = {'provider':'in-process-code'}
