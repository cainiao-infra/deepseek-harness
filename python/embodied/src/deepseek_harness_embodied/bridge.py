"""Tool-free planning through the public SDK with durable request receipts."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import threading
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, TypeAlias

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig, JsonObject, Notification

PlanValidator: TypeAlias = Callable[[object], JsonObject]


def digest(value: object) -> str:
    """Hash canonical UTF-8 JSON, rejecting nonfinite values."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def write_record(path: Path, value: object) -> None:
    """Commit a JSON record with exclusive creation; existing evidence is immutable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".record-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class ModelSpec:
    """Non-secret frozen model configuration; native comparisons use identical controls."""
    model: str
    base_url: str
    context_window: int
    max_tokens: int
    temperature: float
    max_image_bytes: int
    max_image_pixels: int
    max_image_dimension: int
    stream_idle_ms: int
    calls_per_runtime: int
    max_output_bytes: int

    def __post_init__(self) -> None:
        if not self.model or not self.base_url:
            raise ValueError("Explicit model and OpenAI-compatible base URL required")
        for name in ("context_window", "max_tokens", "max_image_bytes", "stream_idle_ms",
                     "calls_per_runtime", "max_output_bytes", "max_image_pixels", "max_image_dimension"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be finite and between zero and two")


@dataclass(frozen=True)
class RequestIdentity:
    """Execution correlation, excluded from model input."""
    run_id: str
    episode_id: str
    request_id: str
    env_idx: int
    reset_generation: int
    deadline: float

    def __post_init__(self) -> None:
        for value in (self.run_id, self.episode_id, self.request_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                raise ValueError("Correlation IDs must be nonempty filesystem-safe identifiers")
        if type(self.env_idx) is not int or self.env_idx < 0:
            raise ValueError("env_idx must be a nonnegative integer")
        if type(self.reset_generation) is not int or self.reset_generation < 0:
            raise ValueError("reset_generation must be a nonnegative integer")
        if not math.isfinite(self.deadline):
            raise ValueError("deadline must be a finite monotonic timestamp")


class PlanningFailure(RuntimeError):
    """No executable plan was committed; status distinguishes known failure from unknown inference."""
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def sdk_input(messages: list[JsonObject]) -> tuple[str, list[JsonObject]]:
    """Convert PhysicalRSI's two-message request, preserving text and camera order."""
    if len(messages) != 2 or messages[0].get("role") != "system" or messages[1].get("role") != "user":
        raise ValueError("Compatibility planning requires one system and one user message")
    system = messages[0].get("content")
    content = messages[1].get("content")
    if not isinstance(system, str) or not system or not isinstance(content, list):
        raise ValueError("Nonempty system text and ordered user blocks required")
    blocks: list[JsonObject] = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("User blocks must be objects")
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            blocks.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image_url" and isinstance(block.get("image_url"), dict):
            url = block["image_url"].get("url")
            if not isinstance(url, str) or not url.startswith("data:image/png;base64,"):
                raise ValueError("Planner cameras must be inline PNG data URLs")
            data = url.split(",", 1)[1]
            base64.b64decode(data, validate=True)
            blocks.append({"type": "image", "mimeType": "image/png", "data": data})
        else:
            raise ValueError("Unsupported planner input block")
    return system, blocks


class PlannerBridge:
    """Own one inference at a time; reset invalidates pending plans before commit.

    A runtime is reused only with an identical prompt and model specification.
    Every compatibility request uses a fresh Session. The caller owns physical
    stopping; closing this bridge only reaps its SDK process.
    """
    def __init__(self, *, spec: ModelSpec, dsh_home: Path, evidence_root: Path,
                 dsh_bin: str | None = None, runtime_cwd: str | None = None,
                 reserve_call: Callable[[dict], int] | None = None) -> None:
        self.spec = spec
        self.dsh_home = dsh_home.resolve()
        self.evidence_root = evidence_root.resolve()
        self.dsh_bin = dsh_bin
        self.runtime_cwd = runtime_cwd
        self._inference = threading.Lock()
        self._state = threading.Lock()
        self._episodes: dict[int, tuple[str, int]] = {}
        self._runtime: DeepSeekHarness | None = None
        self._prompt: str | None = None
        self._calls = 0
        self._closed = False
        self._reserve_call = reserve_call
        self._sessions: dict[tuple[int, str, int], str] = {}
        self._session_runtimes: dict[tuple[int, str, int], DeepSeekHarness] = {}

    def begin_episode(self, env_idx: int, episode_id: str) -> int:
        """Allocate a generation before planning; late requests cannot cross resets."""
        with self._state:
            if self._closed:
                raise RuntimeError("Planner bridge is closed")
            for key in [key for key in self._sessions if key[0] == env_idx]:
                del self._sessions[key]
                del self._session_runtimes[key]
            generation = self._episodes.get(env_idx, ("", -1))[1] + 1
            self._episodes[env_idx] = (episode_id, generation)
            return generation

    def reset(self, env_idx: int) -> None:
        """Invalidate pending inference without claiming to stop an execution skill."""
        with self._state:
            episode, generation = self._episodes.get(env_idx, ("", -1))
            self._episodes[env_idx] = (episode, generation + 1)

    def _is_current(self, identity: RequestIdentity) -> bool:
        return not self._closed and self._episodes.get(identity.env_idx) == (
            identity.episode_id, identity.reset_generation)

    def _open(self, system: str, startup_timeout: float = 30.0) -> DeepSeekHarness:
        if self._runtime is not None and (self._prompt != system or self._calls >= self.spec.calls_per_runtime):
            self._runtime.close()
            self._runtime = None
        if self._runtime is None:
            patch = self.evidence_root / ".runtime-config" / (uuid.uuid4().hex + ".json")
            write_record(patch, [
                {"id": "embodied-llm", "config": {"providers": {"embodied": {
                    "apiKeyEnv": "PHYSICALRSI_AGENT_API_KEY", "api": "openai-completions",
                    "baseURL": self.spec.base_url, "responseFormat": "json_object",
                    "imagePresentation": "inline", "maxRequestImageBytes": self.spec.max_image_bytes,
                    "streamIdleTimeoutMs": self.spec.stream_idle_ms,
                    "retryPolicy": {"mode": "normal", "maxRetries": 0},
                    "compat": {"maxTokensField": "max_tokens", "supportsStore": False,
                               "supportsDeveloperRole": False},
                    "models": [{"id": self.spec.model, "input": ["text", "image"],
                                "contextWindow": self.spec.context_window,
                                "maxTokens": self.spec.max_tokens, "reasoningEfforts": False}],
                }}}},
                {"id": "system-prompt", "config": {"includeHarnessIdentity": False,
                    "includeRuntimeContext": False, "personaPrefix": system}},
                {"id": "attachment-local", "config": {
                    "normalizedImageMaxBytes": self.spec.max_image_bytes,
                    "normalizedImageMaxPixels": self.spec.max_image_pixels,
                    "normalizedImageMaxDimension": self.spec.max_image_dimension}},
                {"id": "embodied-planner", "config": {"temperature": self.spec.temperature}},
            ])
            self._runtime = DeepSeekHarness(DeepSeekHarnessConfig(
                provider="embodied", model=self.spec.model, max_tokens=self.spec.max_tokens,
                dsh_bin=self.dsh_bin, profile="embodied-planner", dsh_home=str(self.dsh_home),
                runtime_cwd=self.runtime_cwd,
                initialize_timeout_seconds=startup_timeout,
                patches=(str(patch),),
            ))
            self._prompt = system
            self._calls = 0
        return self._runtime

    def plan(self, *, messages: list[JsonObject], identity: RequestIdentity,
             mode: str, schema_version: str, validate: PlanValidator,
             episode_session: bool = False) -> JsonObject:
        """Commit only complete, validated plans with a current execution identity.

        Evidence is persisted before a successful return. No inference retry or
        action replay occurs here. Timeout or transport loss closes the process.
        """
        if not self._inference.acquire(blocking=False):
            raise PlanningFailure("failed", "One planner runtime supports one in-flight request")
        record: JsonObject = {"schema": "dsh-physicalrsi-request/v1", **asdict(identity),
                              "attempt_id": uuid.uuid4().hex, "mode": mode,
                              "schema_version": schema_version, "model_config": asdict(self.spec)}
        folder = self.evidence_root / identity.run_id / identity.episode_id / identity.request_id
        partial_events: list[JsonObject] = []

        def collect(notification: Notification) -> None:
            if notification.method == "session.event" and notification.payload.get("sessionId") == record.get("session_id"):
                event = notification.payload.get("event")
                if isinstance(event, dict):
                    partial_events.append(event)

        try:
            system, blocks = sdk_input(messages)
            record["input_digest"] = digest({"messages": messages, "mode": mode, "schema": schema_version})
            record["model_config_digest"] = digest(asdict(self.spec))
            write_record(folder / "request.json", {**record, "messages": messages})
            with self._state:
                if not self._is_current(identity):
                    raise PlanningFailure("cancelled", "Episode generation is stale")
            remaining = identity.deadline - time.monotonic()
            if remaining <= 0:
                raise PlanningFailure("cancelled", "Deadline expired before inference")
            key = (identity.env_idx, identity.episode_id, identity.reset_generation)
            if episode_session and key in self._sessions and (
                    self._runtime is not self._session_runtimes[key] or self._prompt != system
                    or self._calls >= self.spec.calls_per_runtime):
                raise PlanningFailure("failed", "Episode Session cannot rotate; start a fresh episode")
            runtime = self._open(system, remaining)
            runtime.start()
            session_id = self._sessions.setdefault(key, "embodied-" + uuid.uuid4().hex) if episode_session else (
                "embodied-" + uuid.uuid4().hex)
            if episode_session:
                self._session_runtimes[key] = runtime
            record["session_id"] = session_id
            self._calls += 1
            remaining = identity.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Deadline expired during runtime startup")
            if self._reserve_call is not None:
                record["call_slot"] = self._reserve_call({
                    "run_id": identity.run_id, "episode_id": identity.episode_id,
                    "request_id": identity.request_id, "session_id": session_id})
            result = runtime.run(blocks, session_id=session_id, timeout_seconds=remaining, on_notification=collect)
            write_record(folder / "events.json", result.events)
            if result.finish_reason != "completed":
                raise PlanningFailure("failed", f"Inference did not complete: {result.finish_reason}")
            if len(result.final_response.encode()) > self.spec.max_output_bytes:
                raise PlanningFailure("failed", "Planner output exceeds configured byte limit")
            plan = validate(json.loads(result.final_response))
            with self._state:
                if not self._is_current(identity):
                    raise PlanningFailure("cancelled", "Episode reset invalidated inference")
                if time.monotonic() >= identity.deadline:
                    raise TimeoutError("Inference returned after the execution deadline")
                write_record(folder / "receipt.json", {**record, "status": "completed", "plan": plan})
            return plan
        except BaseException as error:
            status = error.status if isinstance(error, PlanningFailure) else (
                "unknown" if isinstance(error, (TimeoutError, OSError, EOFError)) else "failed")
            try:
                if self._runtime is not None:
                    self._runtime.close()
                    self._runtime = None
            finally:
                if folder.exists() and not (folder / "receipt.json").exists():
                    if not (folder / "events.json").exists():
                        write_record(folder / "events.json", partial_events)
                    write_record(folder / "receipt.json", {**record, "status": status, "error_type": type(error).__name__})
            raise
        finally:
            self._inference.release()

    def close(self) -> None:
        """Invalidate all episodes and reap the owned inference process after its current call settles."""
        with self._state:
            self._closed = True
        with self._inference:
            if self._runtime is not None:
                self._runtime.close()
                self._runtime = None
