"""The fixed-input diagnostic retains truncation, invalid plans and unknown calls."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import runpy
import tarfile

import pytest

from deepseek_harness_embodied.bridge import write_record
from deepseek_harness_embodied.thinking_comparison import assess, clarify_online, execute, payload_for, prepare, summarize


def source():
    return {'model': 'deepseek-flash', 'max_tokens': 2048, 'temperature': 0,
            'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'},
            'stream': True, 'stream_options': {'include_usage': True},
            'messages': [{'role': 'system', 'content': 'Return JSON.'},
                {'role': 'user', 'content': [{'type': 'text', 'text': json.dumps({
                    'instruction': 'pick up scissors',
                    'available_compositions': {'memory-guided-program-library': {}},
                    'operation_program': {'operations': {'instruction-grounded-pickup': {}}}})},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,frozen-pixels'}}]}]}


def frozen(tmp_path):
    archive = tmp_path/'source.tar.gz'
    data = json.dumps(source()).encode()
    with tarfile.open(archive, 'w:gz') as bundle:
        member = tarfile.TarInfo('episode/provider/http-0000/sent.json')
        member.size = len(data)
        bundle.addfile(member, io.BytesIO(data))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    output = tmp_path/'comparison'
    protocol = prepare(archive, checksum, output, repeats=2, caps=[2048, 8192], seed=7, timeout=5, workers=2)
    return output, protocol


def test_only_thinking_and_declared_controls_change():
    original = source()
    before = deepcopy(original)
    off, on = [payload_for(original, thinking=v, cap=8192) for v in (False, True)]
    assert original == before
    assert off['messages'] == on['messages'] == original['messages']
    assert off['thinking'] == {'type': 'disabled'} and 'reasoning_effort' not in off
    assert on['thinking'] == {'type': 'enabled'} and on['reasoning_effort'] == 'high'
    assert {k: v for k, v in off.items() if k != 'thinking'} == {
        k: v for k, v in on.items() if k not in {'thinking', 'reasoning_effort'}}


def test_clarification_keeps_current_images_and_history_unchanged():
    request = source()
    domain = json.loads(request['messages'][1]['content'][0]['text'])
    domain['observation'] = {'vision':{'head':{'color':{'image_index':0}}}}
    domain['previous_plan'] = {'execution_event':{'facts':{'rgb_available':False}}}
    request['messages'][1]['content'][0]['text'] = json.dumps(domain)
    request['messages'][1]['content'].insert(0, {'type':'text','text':json.dumps({'phase':'recovery'})})
    before = deepcopy(request)
    clarified = clarify_online(request)
    assert request == before
    assert clarified['messages'][1]['content'][1:] == request['messages'][1]['content'][1:]
    status = json.loads(clarified['messages'][1]['content'][0]['text'])['current_observation_status']
    assert status == {'rgb_available':True,'rgb_cameras':['head']}


@pytest.mark.parametrize('content,finish', [
    ('{}', 'stop'), ('not-json', 'stop'), ('{"operations":[]}', 'length'),
    (json.dumps({'composition': 'memory-guided-program-library', 'rationale': 'selected',
                 'operations': [{'handle': 'instruction-grounded-pickup', 'parameters': {}}]}), 'stop'),
])
def test_malformed_or_truncated_output_never_matches(content, finish):
    oracle = {'composition': 'memory-guided-program-library', 'operations': ['instruction-grounded-pickup'], 'execution': None}
    assert not assess(content, finish, oracle)['decision_matches']


def test_frozen_protocol_keeps_missing_and_failed_requests_in_denominators(tmp_path):
    output, protocol = frozen(tmp_path)
    assert len(protocol['assignments']) == 8
    for pair in range(4):
        rows = [r for r in protocol['assignments'] if r['pair'] == pair]
        assert {r['thinking'] for r in rows} == {False, True}
        assert len({(r['input_id'], r['repeat'], r['cap']) for r in rows}) == 1
    report = summarize(output)
    assert report['physical_success_measured'] is False
    assert all(g['assigned'] == g['unknown'] == 2 and g['matched'] == 0 for g in report['groups'].values())


def test_http_failure_consumes_assignment_and_cannot_be_silently_retried(tmp_path):
    output, protocol = frozen(tmp_path)
    class Failing:
        def open(self, request, timeout):
            raise TimeoutError('controlled unknown outcome')
    assignment = protocol['assignments'][0]
    row = execute(output, assignment, protocol, 'private-test-key', opener=Failing())
    assert row['status'] == 'unknown' and row['error']['type'] == 'TimeoutError'
    assert 'private-test-key' not in (output/'calls'/assignment['id']/'sent.json').read_text()
    with pytest.raises(FileExistsError):
        execute(output, assignment, protocol, 'private-test-key', opener=Failing())


def test_valid_output_records_reasoning_usage_without_using_it_as_answer(tmp_path):
    output, protocol = frozen(tmp_path)
    plan = {'composition': 'memory-guided-program-library', 'operations': ['instruction-grounded-pickup'], 'rationale': 'supported'}
    class Opener:
        def open(self, request, timeout):
            response = io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(plan), 'reasoning_content': 'private reasoning'}}],
                'usage': {'total_tokens': 20}}).encode())
            response.status = 200
            return response
    assignment = protocol['assignments'][0]
    row = execute(output, assignment, protocol, 'test-key', opener=Opener())
    assert row['assessment']['decision_matches'] and row['reasoning_characters'] > 0
    assert row['usage']['total_tokens'] == 20
    row['cap'] += 1
    receipt = output/'calls'/assignment['id']/'receipt.json'
    receipt.unlink()
    write_record(receipt, row)
    with pytest.raises(ValueError, match='frozen assignment'):
        summarize(output)


def test_audit_recomputes_outcomes_and_rejects_changed_raw_responses(tmp_path):
    audit = runpy.run_path(str(Path(__file__).parents[1]/'scripts/thinking_comparison_report.py'))['audit']
    output, protocol = frozen(tmp_path)
    plan = {'composition':'memory-guided-program-library','operations':['instruction-grounded-pickup'],'rationale':'supported'}
    class Opener:
        def open(self, request, timeout):
            response = io.BytesIO(json.dumps({'model':'deepseek-flash','choices':[{
                'finish_reason':'stop','message':{'content':json.dumps(plan)}}],
                'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}}).encode())
            response.status = 200
            return response
    for assignment in protocol['assignments']:
        execute(output, assignment, protocol, 'test-key', opener=Opener())
    report = audit(output)
    assert report['assigned_calls']==8 and not report['physical_success_measured']
    assert all(group['categories']=={'matched':2} for group in report['groups'].values())
    raw = output/'calls'/protocol['assignments'][0]['id']/'response.bin'
    raw.write_bytes(b'{}')
    with pytest.raises(ValueError,match='frozen assignment'):
        audit(output)
