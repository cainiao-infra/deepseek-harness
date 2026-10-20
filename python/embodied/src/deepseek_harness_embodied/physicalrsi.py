"""Explicit backend selected by PhysicalRSI's shared domain planner."""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from pathlib import Path

from .bridge import ModelSpec, PlannerBridge, RequestIdentity


class PhysicalRSITransport:
    """Resolve deployment configuration once; reuse the native prompt and validator."""
    def __init__(self, *, endpoint: str | None = None, model: str | None = None) -> None:
        config = json.loads(Path(os.environ["PHYSICALRSI_DSH_CONFIG"]).read_text())
        if config.get("schema") != "dsh-physicalrsi-deployment/v1":
            raise ValueError("Versioned DSH deployment configuration required")
        spec = ModelSpec(**config["model_spec"])
        if model is not None and model != spec.model:
            raise ValueError("Native and DSH model settings disagree")
        if endpoint is not None and endpoint.rstrip('/') != spec.base_url.rstrip('/') + '/chat/completions':
            raise ValueError("Native and DSH endpoints disagree")
        self.timeout = config["request_timeout_seconds"]
        if not isinstance(self.timeout, (float, int)) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Positive finite request timeout required")
        self.bridge = PlannerBridge(spec=spec, dsh_home=Path(config["dsh_home"]),
            evidence_root=Path(config["evidence_root"]), dsh_bin=config.get("dsh_bin"),
            runtime_cwd=config.get("runtime_cwd"))
        self.run_id = config["run_id"]
        self._episodes: dict[int, tuple[str, int]] = {}

    def plan(self, *, messages, context, validate, mode):
        """Return a committed plan; execution context never becomes model-visible input."""
        if not isinstance(context, dict) or not isinstance(context.get("episode_id"), str):
            raise ValueError("PhysicalRSI must preallocate an episode before DSH planning")
        index, episode = context["env_idx"], context["episode_id"]
        current = self._episodes.get(index)
        if current is None or current[0] != episode:
            generation = self.bridge.begin_episode(index, episode)
            self._episodes[index] = (episode, generation)
        else:
            generation = current[1]
        return self.bridge.plan(messages=messages, validate=validate, mode=mode,
            schema_version="physicalrsi-planner/v1", identity=RequestIdentity(
                run_id=self.run_id, episode_id=episode, request_id=uuid.uuid4().hex,
                env_idx=index, reset_generation=generation,
                deadline=time.monotonic() + self.timeout))

    def reset(self) -> None:
        """Invalidate every environment before a subsequent physical episode."""
        for index in self._episodes:
            self.bridge.reset(index)
        self._episodes.clear()

    def close(self) -> None:
        """Reap only the owned inference runtime; this is not a physical stop."""
        self.bridge.close()
