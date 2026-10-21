"""Directory seeds cannot substitute for explicit frozen physical-layout selection."""
import hashlib
import json
import sys
from types import ModuleType
from types import SimpleNamespace

import pytest

from deepseek_harness_embodied.layouts import bind_fresh_case, select_layout


def fixture(tmp_path):
    files=[]
    for index in range(3):
        path=tmp_path/f'layout_{index}.json'
        path.write_text('{"position":'+str(index)+'}')
        files.append(path)
    manager=SimpleNamespace(seed_list=[0,1,2],seed_info={i:{'scene_layout':str(p)} for i,p in enumerate(files)},
                            st_idx=0,ed_idx=3,idx=0,_current_batch_seeds=None)
    return manager, files


def test_selected_case_uses_requested_layout_instead_of_first_in_directory(tmp_path):
    manager,files=fixture(tmp_path)
    sha=hashlib.sha256(files[2].read_bytes()).hexdigest()
    result=select_layout(manager,layout_id=2,expected_sha256=sha,evidence=tmp_path/'selection.json')
    assert manager.seed_list==[2] and manager.ed_idx==1 and manager.idx==0
    assert result['layout_path']==str(files[2]) and result['layout_sha256']==sha
    assert files[0].read_text()=='{"position":0}'


def test_changed_layout_never_mutates_native_selection(tmp_path):
    manager,files=fixture(tmp_path)
    sha=hashlib.sha256(files[2].read_bytes()).hexdigest()
    files[2].write_text('{"position":99}')
    with pytest.raises(ValueError,match='frozen case'):
        select_layout(manager,layout_id=2,expected_sha256=sha,evidence=tmp_path/'selection.json')
    assert manager.seed_list==[0,1,2] and not (tmp_path/'selection.json').exists()


def test_native_reads_frozen_snapshot_even_if_source_changes_after_selection(tmp_path):
    manager,files=fixture(tmp_path)
    original=files[1].read_bytes()
    select_layout(manager,layout_id=1,expected_sha256=hashlib.sha256(original).hexdigest(),evidence=tmp_path/'selection.json')
    files[1].write_text('{"position":99}')
    from pathlib import Path
    assert Path(manager.seed_info[1]['scene_layout']).read_bytes()==original
    assert manager.get_seed_scene_info(1)==json.loads(original)
    with pytest.raises(ValueError,match='outside the frozen case'):
        manager.get_seed_scene_info(0)
    Path(manager.seed_info[1]['scene_layout']).write_text('{}')
    with pytest.raises(ValueError,match='changed before reset'):
        manager.get_seed_scene_info(1)


def test_resume_excluded_layout_cannot_be_reintroduced(tmp_path):
    manager,files=fixture(tmp_path)
    manager.seed_list=[0,1]
    with pytest.raises(ValueError,match='excluded'):
        select_layout(manager,layout_id=2,expected_sha256=hashlib.sha256(files[2].read_bytes()).hexdigest(),
                      evidence=tmp_path/'selection.json')
    assert manager.seed_list==[0,1]


def test_fresh_case_uses_verified_generator_manifest_without_eval_fallback(tmp_path,monkeypatch):
    manifest=tmp_path/'manifest.json'
    manifest.write_text('{}')
    sha=hashlib.sha256(manifest.read_bytes()).hexdigest()
    from deepseek_harness_embodied import layout_cohort
    monkeypatch.setattr(layout_cohort,'verify_cohort',lambda path,**kwargs:{'entries':[{
        'task':'general_pickup','index':7,'sha256':'file-sha','content_sha256':'content-sha','file':'pickup/7/layout.json'}]}
    )
    binder=ModuleType('PhysicalRSI_baselines.robodojo.native_layouts')
    calls=[]
    def bind(cls,**kwargs):
        calls.append(kwargs)
        return cls
    binder.bind_layouts=bind
    monkeypatch.setitem(sys.modules,binder.__name__,binder)
    cls,source=bind_fresh_case(SimpleNamespace,manifest_path=manifest,task='general_pickup',index=7,
        expected_sha256='file-sha',expected_manifest_sha256=sha,split='development',comparison_sha256=None)
    assert cls is SimpleNamespace and source['index']==7 and source['manifest_sha256']==sha
    assert calls[0]['rows']==[{'task':'general_pickup','split':'development','file':'pickup/7/layout.json','layout_sha256':'content-sha'}]
    with pytest.raises(ValueError,match='identity differs'):
        bind_fresh_case(SimpleNamespace,manifest_path=manifest,task='general_pickup',index=8,
            expected_sha256='file-sha',expected_manifest_sha256=sha,split='development',comparison_sha256=None)
    assert len(calls)==1
    manifest.write_text('{"changed":true}')
    with pytest.raises(ValueError,match='manifest differs'):
        bind_fresh_case(SimpleNamespace,manifest_path=manifest,task='general_pickup',index=7,
            expected_sha256='file-sha',expected_manifest_sha256=sha,split='development',comparison_sha256=None)
    assert len(calls)==1
