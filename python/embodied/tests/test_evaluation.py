"""Frozen controls and conservative usage accounting for paired pilots."""
import json
import io
import http.client
from pathlib import Path
import runpy

import pytest

from deepseek_harness_embodied.evaluation import provider_request, response_usage


def request():
    return {'model': 'deepseek-flash', 'max_tokens': 2048, 'temperature': 0,
            'response_format': {'type': 'json_object'},
            'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'move'}]}]}


def test_native_and_dsh_provider_controls_agree():
    native = request()
    dsh = dict(native, thinking={'type': 'disabled'})
    assert provider_request(native, model='deepseek-flash', max_tokens=2048) == dsh
    assert provider_request(dsh, model='deepseek-flash', max_tokens=2048) == dsh
    assert 'thinking' not in native


@pytest.mark.parametrize('reasoning', ['low', 'high', 'max'])
def test_thinking_controls_are_equal_for_native_and_dsh(reasoning):
    native = request()
    dsh = dict(native, thinking={'type': 'enabled'}, reasoning_effort=reasoning)
    assert provider_request(native, model='deepseek-flash', max_tokens=2048, reasoning=reasoning) == dsh
    assert provider_request(dsh, model='deepseek-flash', max_tokens=2048, reasoning=reasoning) == dsh
    with pytest.raises(ValueError, match='frozen'):
        provider_request(dict(dsh, reasoning_effort='off'), model='deepseek-flash', max_tokens=2048, reasoning=reasoning)


@pytest.mark.parametrize('change', [
    {'model': 'different'}, {'max_tokens': 4096}, {'temperature': 1},
    {'thinking': {'type': 'enabled'}}, {'reasoning_effort': 'high'},
    {'tools': [{'type': 'function'}]}, {'tool_choice': 'auto'},
    {'response_format': None}, {'messages': []},
])
def test_conflicting_controls_reject_before_inference(change):
    with pytest.raises(ValueError):
        provider_request(dict(request(), **change), model='deepseek-flash', max_tokens=2048)


def test_json_and_sse_retain_cache_accounting():
    usage = {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12,
             'prompt_cache_hit_tokens': 6, 'prompt_cache_miss_tokens': 4}
    packet = json.dumps({'choices': [], 'usage': usage}).encode()
    assert response_usage(packet, streaming=False) == usage
    assert response_usage(b'data: '+packet+b'\n\ndata: [DONE]\n\n', streaming=True) == usage


@pytest.mark.parametrize('body,streaming', [
    (b'{}', False), (b'{', False), (b'data: [DONE]\n\n', True),
    (b'data: {\n', True),
    (b'{"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":4}}', False),
    (b'{"usage":{"prompt_tokens":true,"completion_tokens":2,"total_tokens":3}}', False),
])
def test_missing_or_broken_usage_is_unknown(body, streaming):
    assert response_usage(body, streaming=streaming) is None


def test_gateway_records_attempts_usage_and_reaps_listener(tmp_path):
    gateway_class = runpy.run_path(str(Path(__file__).parents[1]/'scripts/paired_pilot.py'))['Gateway']
    spec = {'model': 'deepseek-flash', 'max_tokens': 2048, 'calls_per_episode': 1,
            'provider_timeout_seconds': 5, 'max_request_bytes': 4096, 'max_response_bytes': 4096}
    gateway = gateway_class(tmp_path, spec, 'private-test-provider-key')
    received = []
    class Opener:
        def open(self, value, timeout):
            received.append(value)
            response = io.BytesIO(b'{"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}')
            response.status = 200
            response.headers = {'Content-Type': 'application/json'}
            return response
    gateway.opener = Opener()
    with gateway:
        connection = http.client.HTTPConnection('127.0.0.1', gateway.server.server_port, timeout=10)
        try:
            for expected in (200, 502):
                connection.request('POST', '/v1/chat/completions', json.dumps(request()),
                    {'Authorization': 'Bearer '+gateway.token, 'Content-Type': 'application/json'})
                response = connection.getresponse()
                assert response.status == expected
                response.read()
        finally:
            connection.close()
    assert not gateway.thread.is_alive()
    assert gateway.server.socket.fileno() == -1
    assert len(received) == 1
    assert received[0].get_header('Authorization') == 'Bearer private-test-provider-key'
    sent = json.loads((tmp_path/'http-0000/sent.json').read_text())
    assert sent['thinking'] == {'type': 'disabled'}
    assert 'private-test-provider-key' not in json.dumps(sent)
    receipt = json.loads((tmp_path/'http-0000/receipt.json').read_text())
    assert receipt['status'] == 'completed' and receipt['usage']['total_tokens'] == 12


@pytest.mark.parametrize('change', [
    {'partition': 'test'}, {'provider_timeout_seconds': float('inf')},
    {'calls_per_episode': True}, {'output': 'relative-output'},
    {'cases': []},
    {'cases': [{'case_id': '../escape', 'order': ['N', 'H0', 'H1']}]},
    {'cases': [{'case_id': 'valid', 'order': ['N', 'N', 'H1']}]},
    {'deepseek_reasoning':'unsupported'},
    {'cases': [{'case_id':'valid','order':['N','H0','H1'],'layout_id':1}]},
    {'cases': [{'case_id':'valid','order':['N','H0','H1'],'layout_id':True,'layout_sha256':'0'*64}]},
    {'cases': [{'case_id':'valid','order':['N','H0','H1'],'layout_id':0,'layout_sha256':'0'*64,
                'fresh_layout':{'manifest':'/frozen/manifest.json','manifest_sha256':'0'*64,'index':0,'split':'validation'}}]},
])
def test_invalid_pilot_spec_fails_before_launch(change):
    root = Path(__file__).parents[1]
    validate = runpy.run_path(str(root/'scripts/paired_pilot.py'))['validate_spec']
    spec = json.loads((root/'configs/paired-pilot.example.json').read_text())
    validate(spec)
    with pytest.raises(ValueError):
        validate(dict(spec, **change))
