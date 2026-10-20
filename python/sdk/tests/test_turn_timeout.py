"""Turn deadlines include notifications after the prompt RPC has returned."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from deepseek_harness import DeepSeekHarness


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_deadline_does_not_launch_runtime(timeout: float) -> None:
    harness = DeepSeekHarness(dsh_bin="/missing/runtime")
    with pytest.raises(ValueError, match="positive and finite"):
        harness.run("hello", timeout_seconds=timeout)


@pytest.mark.parametrize("admit", [False, True])
def test_deadline_covers_prompt_admission_and_notification_wait(tmp_path: Path, admit: bool) -> None:
    script = tmp_path / "runtime.py"
    script.write_text('''
import json
import sys
for line in sys.stdin:
    msg = json.loads(line)
    method = msg['method']
    if method == 'initialize':
        result = {'serverInfo': {'name': 'deadline-test'}}
    elif method == 'session/prompt':
        if sys.argv[1] == 'False':
            continue
        result = {'messageId': 'message-1'}
        print(json.dumps({'jsonrpc': '2.0', 'method': 'session.event', 'params': {
            'sessionId': msg['params']['sessionId'], 'event': {
                'type': 'agent/inbox/spliced', 'data': {'inserted': [{'id': 'message-1'}]}
            }
        }}), flush=True)
    elif method == 'shutdown':
        result = {}
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)
    if method == 'shutdown':
        break
''')
    with DeepSeekHarness(_launch_args=(sys.executable, str(script), str(admit))) as harness:
        with pytest.raises(TimeoutError):
            harness.run("hello", timeout_seconds=0.1)
