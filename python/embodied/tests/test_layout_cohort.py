"""Cohort coverage, split isolation and exact scene receipts precede simulation."""
from copy import deepcopy
import hashlib
import json

import pytest

from deepseek_harness_embodied.bridge import digest
from deepseek_harness_embodied.layout_cohort import verify_cohort


def cohort(tmp_path):
    protocol={'schema':'dsh-layout-cohort/v1','split':'development','comparison_sha256':None,
              'qualification':False,'physics_status':'not_validated','tasks':['general_pickup'],'count_per_task':2}
    rows=[]
    for index in range(2):
        file=tmp_path/f'general_pickup/{index}/layout.json'
        file.parent.mkdir(parents=True)
        data={'position':[index,0,1]}
        file.write_text(json.dumps(data))
        row={'task':'general_pickup','index':index,'state':'generated',
             'file':str(file.relative_to(tmp_path)),'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
             'content_sha256':digest(data)}
        (file.parent/'receipt.json').write_text(json.dumps({k:v for k,v in row.items() if k!='file'}))
        rows.append(row)
    (tmp_path/'protocol.json').write_text(json.dumps(protocol))
    manifest={**protocol,'state':'generated','entries':rows}
    path=tmp_path/'manifest.json'
    path.write_text(json.dumps(manifest))
    return path,manifest


def test_valid_generated_cohort_does_not_claim_physics_acceptance(tmp_path):
    path,_=cohort(tmp_path)
    result=verify_cohort(path,split='development',comparison_sha256=None)
    assert result['qualification'] is False and result['physics_status']=='not_validated'
    with pytest.raises(ValueError,match='wrong-split'):
        verify_cohort(path,split='validation',comparison_sha256='0'*64)


@pytest.mark.parametrize('change',[
    lambda m:m.update(state='incomplete'),
    lambda m:m['entries'].pop(),
    lambda m:m['entries'].append(deepcopy(m['entries'][0])),
    lambda m:m['entries'][1].update(file=m['entries'][0]['file']),
    lambda m:m.update(qualification=True),
])
def test_invalid_cohort_cannot_reach_native_binding(tmp_path,change):
    path,manifest=cohort(tmp_path)
    change(manifest);path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        verify_cohort(path,split='development',comparison_sha256=None)


def test_modified_scene_and_receipt_are_rejected(tmp_path):
    path,manifest=cohort(tmp_path)
    file=tmp_path/manifest['entries'][0]['file']
    original=file.read_bytes()
    file.write_text('{}')
    with pytest.raises(ValueError,match='Changed or duplicate'):
        verify_cohort(path,split='development',comparison_sha256=None)
    file.write_bytes(original)
    (file.parent/'receipt.json').write_text('{}')
    with pytest.raises(ValueError,match='generation receipt'):
        verify_cohort(path,split='development',comparison_sha256=None)
