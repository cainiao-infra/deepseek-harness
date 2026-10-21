"""Built dsh profile through public stdio SDK and a recorded provider conversation."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from deepseek_harness_embodied import PlannerBridge, RequestIdentity
from deepseek_harness_embodied.profile import install_source_profile
from test_bridge import spec


@pytest.mark.skipif(os.environ.get('DSH_EMBODIED_INTEGRATION') != '1', reason='requires build:artifacts')
@pytest.mark.parametrize('episode_session', [False, True])
def test_recorded_compatibility_session_preserves_actual_wire_input(tmp_path, episode_session):
    import time
    root = Path(__file__).resolve().parents[3]
    fixture = json.loads((Path(__file__).parent/'fixtures/compatibility-session.json').read_text())
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            content = json.dumps(fixture['response'])
            for frame in [
                {'choices':[{'delta':{'role':'assistant','content':content},'index':0,'finish_reason':None}]},
                {'choices':[{'delta':{},'index':0,'finish_reason':'stop'}],
                 'usage':{'prompt_tokens':10,'completion_tokens':5}},
            ]:
                self.wfile.write(('data: '+json.dumps(frame)+'\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    home = tmp_path/'home'
    install_source_profile(home, root/'packages/experimental/embodied-planner')
    bridge = PlannerBridge(spec=replace(spec(), base_url=f'http://127.0.0.1:{server.server_port}/v1'),
        dsh_home=home, evidence_root=tmp_path/'evidence', dsh_bin=str(root/'apps/cli/lib/bin.js'),
        runtime_cwd=str(root))
    old_key = os.environ.get('PHYSICALRSI_AGENT_API_KEY')
    os.environ['PHYSICALRSI_AGENT_API_KEY'] = 'recorded-test-key'
    messages = [{'role':'system','content':fixture['system']}, {'role':'user','content':[
        {'type':'text','text':fixture['text']},
        {'type':'image_url','image_url':{'url':'data:image/png;base64,'+fixture['png']}}]}]
    try:
        generation = bridge.begin_episode(0, 'episode')
        for number in range(2):
            identity = RequestIdentity('run','episode',f'request-{number}',0,generation,time.monotonic()+20)
            assert bridge.plan(messages=messages, identity=identity, mode='subgoal',
                schema_version='v1', validate=lambda value:value, episode_session=episode_session) == fixture['response']
        assert len(requests) == 2
        for number, request in enumerate(requests):
            assert {name:request.get(name) for name in fixture['expected']} == fixture['expected']
            if episode_session and number == 1:
                assert request['messages'][:2] == messages
                assert request['messages'][-1] == messages[-1]
                assert json.loads(request['messages'][2]['content']) == fixture['response']
            else:
                assert request['messages'] == messages
            assert not request.get('tools')
        receipts = [json.loads(file.read_text()) for file in (tmp_path/'evidence').rglob('receipt.json')]
        assert len({receipt['session_id'] for receipt in receipts}) == (1 if episode_session else 2)
        bridge.close()
        logs = list((home/'sessions').rglob('*.jsonl'))
        assert len(logs) == (1 if episode_session else 2)
        for log in logs:
            events = [json.loads(line) for line in log.read_text().splitlines()]
            assert any(event.get('type') == 'request/header' for event in events)
            assert any(event.get('type') == 'assistant/message' for event in events)
            assert not any(event.get('type') == 'tool/call' for event in events)
    finally:
        bridge.close()
        server.shutdown()
        server.server_close()
        worker.join(2)
        if old_key is None:
            os.environ.pop('PHYSICALRSI_AGENT_API_KEY', None)
        else:
            os.environ['PHYSICALRSI_AGENT_API_KEY'] = old_key
