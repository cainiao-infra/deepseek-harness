"""Raw request/response tampering must not inherit a launcher's success receipt."""
import hashlib
import json

import pytest

from deepseek_harness_embodied.evaluation import provider_request
from deepseek_harness_embodied.provider_evidence import audit_provider


@pytest.fixture
def evidence(tmp_path):
    spec={'model':'deepseek-flash','max_tokens':8192,'deepseek_reasoning':'high'}
    request={'model':'deepseek-flash','max_tokens':8192,'temperature':0,
        'response_format':{'type':'json_object'},'messages':[{'role':'user','content':'pick'}]}
    path=tmp_path/'http-0000';path.mkdir()
    body=json.dumps({'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}}).encode()
    (path/'received.json').write_text(json.dumps(request))
    (path/'sent.json').write_text(json.dumps(provider_request(request,**{'model':spec['model'],
        'max_tokens':spec['max_tokens'],'reasoning':'high'})))
    (path/'response.bin').write_bytes(body)
    (path/'receipt.json').write_text(json.dumps({'status':'completed','http_status':200,
        'response_sha256':hashlib.sha256(body).hexdigest(),'usage':json.loads(body)['usage']}))
    return tmp_path,path,spec


@pytest.mark.parametrize('target,change',[
    ('sent.json',lambda value:value.update(thinking={'type':'disabled'})),
    ('sent.json',lambda value:value['messages'][0].update(content='changed image or instruction')),
    ('receipt.json',lambda value:value['usage'].update(total_tokens=13)),
    ('receipt.json',lambda value:value.update(http_status=500)),
])
def test_completed_request_is_recomputed_from_raw_evidence(evidence,target,change):
    root,path,spec=evidence
    assert audit_provider(root,spec)[0]['complete']
    value=json.loads((path/target).read_text());change(value);(path/target).write_text(json.dumps(value))
    with pytest.raises(ValueError):audit_provider(root,spec)


def test_response_digest_and_missing_bytes_are_independent_checks(evidence):
    root,path,spec=evidence
    (path/'response.bin').write_bytes(b'{}')
    with pytest.raises(ValueError,match='digest'):audit_provider(root,spec)
    (path/'response.bin').unlink()
    with pytest.raises(ValueError,match='lacks'):audit_provider(root,spec)


def test_interrupted_request_remains_unknown_without_erasing_its_assignment(evidence):
    root,path,spec=evidence
    (path/'receipt.json').unlink()
    rows=audit_provider(root,spec)
    assert len(rows)==1 and rows[0]['complete'] is False
