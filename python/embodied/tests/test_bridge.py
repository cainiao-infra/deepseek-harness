"""Receipts distinguish valid plans from stale, truncated and unknown inference."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from deepseek_harness import RunResult
from deepseek_harness_embodied import ModelSpec, PlannerBridge, PlanningFailure, RequestIdentity
from deepseek_harness_embodied import bridge as module


MESSAGES = [{"role": "system", "content": "Return a JSON plan"},
            {"role": "user", "content": [{"type": "text", "text": "Pick the cup"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,Bw=="}}]}]


def spec() -> ModelSpec:
    return ModelSpec(model="vision", base_url="https://provider.invalid/v1", context_window=8192,
        max_tokens=512, temperature=0, max_image_bytes=1024, max_image_pixels=4096,
        max_image_dimension=64, stream_idle_ms=1000, calls_per_runtime=2, max_output_bytes=4096)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    runtimes = []
    class Runtime:
        def __init__(self, config):
            self.config, self.closed, self.inputs = config, False, []
            self.finish, self.output = "completed", '{"subgoal":"pick cup"}'
            self.callback = None
            runtimes.append(self)
        def start(self):
            pass
        def run(self, content, **kwargs):
            self.inputs.append((content, kwargs))
            if self.callback:
                self.callback()
            return RunResult(kwargs['session_id'], self.output, self.finish, [], [])
        def close(self):
            self.closed = True
    monkeypatch.setattr(module, 'DeepSeekHarness', Runtime)
    bridge = PlannerBridge(spec=spec(), dsh_home=tmp_path/'home', evidence_root=tmp_path/'evidence')
    generation = bridge.begin_episode(0, 'episode')
    def identity(request='request'):
        return RequestIdentity('run', 'episode', request, 0, generation, time.monotonic()+10)
    def call(request='request'):
        return bridge.plan(messages=MESSAGES, identity=identity(request), mode='subgoal',
                           schema_version='v1', validate=lambda value: value)
    yield bridge, runtimes, call, tmp_path/'evidence'/'run'/'episode'
    bridge.close()


def test_fresh_sessions_reuse_runtime_and_receipt_precedes_return(setup):
    bridge, runtimes, call, folder = setup
    assert call() == {'subgoal':'pick cup'}
    assert call('second') == {'subgoal':'pick cup'}
    assert len(runtimes) == 1
    inputs = runtimes[0].inputs
    assert inputs[0][1]['session_id'] != inputs[1][1]['session_id']
    assert inputs[0][0] == [{'type':'text','text':'Pick the cup'},
                            {'type':'image','mimeType':'image/png','data':'Bw=='}]
    patch = json.loads(Path(runtimes[0].config.patches[0]).read_text())
    assert next(row['config']['personaPrefix'] for row in patch if row['id'] == 'system-prompt') == MESSAGES[0]['content']
    assert 'episode' not in json.dumps(inputs[0][0])
    receipt = json.loads((folder/'request'/'receipt.json').read_text())
    assert receipt['status'] == 'completed'
    assert receipt['session_id'] == inputs[0][1]['session_id']
    call('third')
    assert len(runtimes) == 2 and runtimes[0].closed


@pytest.mark.parametrize('finish,output', [('max-tokens','{"subgoal":"cut"}'),
    ('error','{}'), ('completed','invalid JSON')])
def test_truncated_or_malformed_output_never_returns_a_plan(setup, finish, output):
    bridge, runtimes, call, folder = setup
    runtime = bridge._open(MESSAGES[0]['content'])
    runtime.finish, runtime.output = finish, output
    with pytest.raises((PlanningFailure, ValueError)):
        call()
    assert runtimes[0].closed
    assert json.loads((folder/'request'/'receipt.json').read_text())['status'] == 'failed'


def test_reset_during_inference_rejects_the_late_plan(setup):
    bridge, runtimes, call, folder = setup
    runtime = bridge._open(MESSAGES[0]['content'])
    entered, release = threading.Event(), threading.Event()
    def wait():
        entered.set()
        assert release.wait(2)
    runtime.callback = wait
    errors = []
    def run():
        try:
            call()
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(2)
        bridge.reset(0)
        with pytest.raises(PlanningFailure, match='in-flight'):
            call('overlap')
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert len(errors) == 1 and errors[0].status == 'cancelled'
    assert json.loads((folder/'request'/'receipt.json').read_text())['status'] == 'cancelled'


def test_timeout_records_unknown_and_reaps_runtime(setup):
    bridge, runtimes, call, folder = setup
    runtime = bridge._open(MESSAGES[0]['content'])
    def fail():
        raise TimeoutError('provider did not acknowledge completion')
    runtime.callback = fail
    with pytest.raises(TimeoutError):
        call()
    assert runtime.closed
    assert json.loads((folder/'request'/'receipt.json').read_text())['status'] == 'unknown'


def test_duplicate_request_cannot_overwrite_success_evidence(setup):
    bridge, runtimes, call, folder = setup
    call()
    before = (folder/'request'/'receipt.json').read_bytes()
    with pytest.raises(FileExistsError):
        call()
    assert len(runtimes[0].inputs) == 1
    assert (folder/'request'/'receipt.json').read_bytes() == before


def test_episode_session_reuses_history_and_refuses_rotation(setup):
    bridge, runtimes, call, folder = setup
    generation = bridge._episodes[0][1]
    def plan(number):
        return bridge.plan(messages=MESSAGES,
            identity=RequestIdentity('run','episode',f'episode-{number}',0,generation,time.monotonic()+10),
            mode='online',schema_version='v1',validate=lambda value:value,episode_session=True)
    plan(0)
    plan(1)
    assert runtimes[0].inputs[0][1]['session_id'] == runtimes[0].inputs[1][1]['session_id']
    with pytest.raises(PlanningFailure,match='cannot rotate'):
        plan(2)
    assert len(runtimes[0].inputs) == 2


def test_exhausted_shared_budget_performs_no_inference(setup, tmp_path):
    from deepseek_harness_embodied.budget import CallBudget
    bridge, runtimes, call, folder = setup
    bridge._reserve_call = CallBudget(tmp_path/'budget',0).reserve
    with pytest.raises(RuntimeError,match='exhausted'):
        call()
    assert runtimes[0].inputs == [] and runtimes[0].closed
