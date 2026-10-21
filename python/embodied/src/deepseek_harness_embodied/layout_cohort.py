"""Fresh PhysicalRSI catalog layouts; generation does not qualify physics or scores.

The trusted sampler reads public task configuration and object assets only. Each
case runs in a separate process with PhysicalRSI's input guard. Validation splits
require a frozen comparison identity and never reuse a development seed stream.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .bridge import digest, write_record


def sampler():
    """Load the installed trusted sampler and fingerprint its Python sources."""
    from PhysicalRSI_baselines.robodojo.layout_sampling import catalog, extended
    from PhysicalRSI_baselines.robodojo import layouts
    folder = Path(catalog.__file__).parent
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.glob('*.py'))}
    sources['native-input-guard.py'] = hashlib.sha256(Path(layouts.__file__).read_bytes()).hexdigest()
    return catalog, extended, sources


def worker(request_path: Path) -> None:
    """Generate one isolated layout with no benchmark layouts or model inference."""
    import numpy as np
    from PhysicalRSI_baselines.robodojo.layouts import _input_guard
    request = json.loads(request_path.read_text())
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=request['implementation_sha256']:
        raise ValueError('Cohort generator changed after assignment')
    catalog, extended, sources = sampler()
    if sources != request['sampler_sources']:
        raise ValueError('Layout sampler changed after assignment')
    generate = extended.generate_train_layout if request['task'] in extended.SUPPORTED_TASKS else catalog.generate_train_layout
    np.random.default_rng(0).random()
    source, assets, output = (Path(request[k]) for k in ('source', 'assets', 'output'))
    accessed = _input_guard([source/'task/RoboDojo/config',source/'env_cfg/scene',assets/'Object',Path(catalog.__file__).parent],output)
    row = {'task':request['task'],'index':request['index'],'state':'generation_failed'}
    try:
        layout = generate(source_root=source,assets_root=assets,task=request['task'],
                          generation_seed=request['stream_seed'],layout_id=request['index'])
        metadata = layout.pop('_train_layout')
        write_record(output/'layout.json',layout)
        row.update(state='generated',sha256=hashlib.sha256((output/'layout.json').read_bytes()).hexdigest(),
                   content_sha256=digest(layout),sampling_metadata=metadata)
    except Exception as error:
        row['error']={'type':type(error).__name__,'message':str(error)}
    row['input_files']={name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in sorted(accessed)}
    write_record(output/'receipt.json',row)


def generate_cohort(*, source: Path, assets: Path, output: Path, tasks: list[str], count: int,
                     seed: int, split: str, comparison_sha256: str | None,
                     workers: int, timeout: float) -> dict:
    """Freeze assignments first; failed or duplicate layouts leave the cohort incomplete."""
    catalog, extended, sources = sampler()
    supported = set(catalog.SUPPORTED_TASKS) | set(extended.SUPPORTED_TASKS)
    if (not tasks or len(set(tasks))!=len(tasks) or not set(tasks)<=supported
            or type(count) is not int or count<1 or type(seed) is not int or seed<0
            or type(workers) is not int or not 1<=workers<=10 or not 0<timeout<=3600):
        raise ValueError('Supported unique tasks, positive count, nonnegative seed and bounded workers/time required')
    if split not in {'development','validation'} or (split=='development' and comparison_sha256 is not None):
        raise ValueError('Development cohorts cannot consume validation comparison state')
    if split=='validation' and (not isinstance(comparison_sha256,str) or not re.fullmatch('[0-9a-f]{64}',comparison_sha256)):
        raise ValueError('Validation cohort requires a frozen comparison SHA-256')
    source,assets,output=(p.resolve() for p in (source,assets,output))
    if any({'Eval_Layout','Train_Layout','Traj','eval_result'} & set(p.parts) for p in (source,assets,output)):
        raise ValueError('Fresh generation cannot consume benchmark layouts or trajectories')
    output.mkdir(parents=True,exist_ok=False)
    protocol={'schema':'dsh-layout-cohort/v1','split':split,'qualification':False,'physics_status':'not_validated',
              'tasks':tasks,'count_per_task':count,'seed':seed,'comparison_sha256':comparison_sha256,
              'sampler_sources':sources,'timeout_seconds':timeout,'workers':workers,
              'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    write_record(output/'protocol.json',protocol)
    requests=[]
    for task in tasks:
        for index in range(count):
            folder=output/task/str(index)
            request={'task':task,'index':index,'source':str(source),'assets':str(assets),'output':str(folder),
                'stream_seed':int(digest({'seed':seed,'split':split,'comparison':comparison_sha256})[:16],16),
                'sampler_sources':sources,'implementation_sha256':protocol['implementation_sha256']}
            path=folder/'request.json';write_record(path,request);requests.append(path)
    def run(path):
        with (path.parent/'process.log').open('x') as log:
            try:
                result=subprocess.run([sys.executable,'-m',__name__,'--worker',str(path)],
                    env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('KEY','SECRET','TOKEN','PASSWORD'))},
                    stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,timeout=timeout,check=False)
                code=result.returncode
            except subprocess.TimeoutExpired:
                code=None
        receipt=path.parent/'receipt.json'
        assigned=json.loads(path.read_text())
        row=json.loads(receipt.read_text()) if code==0 and receipt.exists() else {
            'task':assigned['task'],'index':assigned['index'],'state':'generation_failed','process_exit':code}
        row['file']=str((path.parent/'layout.json').relative_to(output))
        return row
    with ThreadPoolExecutor(max_workers=workers) as executor:
        rows=list(executor.map(run,requests))
    identities=[(r['task'],r.get('content_sha256')) for r in rows]
    complete=all(r['state']=='generated' for r in rows) and len(set(identities))==len(identities)
    manifest={**protocol,'state':'generated' if complete else 'incomplete','entries':rows}
    write_record(output/'manifest.json',manifest)
    return manifest


def verify_cohort(path: Path, *, split: str, comparison_sha256: str | None) -> dict:
    """Reject changed, duplicate, incomplete or wrong-split scene inputs before reset."""
    manifest=json.loads(path.read_text())
    if (manifest.get('schema')!='dsh-layout-cohort/v1' or manifest.get('state')!='generated'
            or manifest['split']!=split or manifest['comparison_sha256']!=comparison_sha256):
        raise ValueError('Incomplete or wrong-split fresh cohort')
    protocol=json.loads((path.parent/'protocol.json').read_text())
    if any(manifest.get(k)!=v for k,v in protocol.items()):
        raise ValueError('Fresh cohort differs from its frozen assignments')
    if (protocol.get('qualification') is not False or protocol.get('physics_status')!='not_validated'
            or type(protocol.get('count_per_task')) is not int or protocol['count_per_task']<1
            or not isinstance(protocol.get('tasks'),list) or not protocol['tasks']
            or any(not isinstance(task,str) or not re.fullmatch('[A-Za-z0-9_]+',task) for task in protocol['tasks'])
            or len(set(protocol['tasks']))!=len(protocol['tasks'])):
        raise ValueError('Invalid fresh cohort task/count or qualification fields')
    expected={(task,index) for task in protocol['tasks'] for index in range(protocol['count_per_task'])}
    actual=[(r['task'],r['index']) for r in manifest['entries']]
    if set(actual)!=expected or len(actual)!=len(expected):
        raise ValueError('Fresh cohort assignment coverage differs')
    seen=set()
    root=path.parent.resolve()
    for row in manifest['entries']:
        file=(root/row['file']).resolve(strict=True)
        if not file.is_relative_to(root) or row['state']!='generated':
            raise ValueError('Fresh layout is outside its complete cohort')
        data=file.read_bytes();content=digest(json.loads(data));identity=(row['task'],content)
        if hashlib.sha256(data).hexdigest()!=row['sha256'] or content!=row['content_sha256'] or identity in seen:
            raise ValueError('Changed or duplicate fresh layout')
        retained=json.loads((file.parent/'receipt.json').read_text())
        if retained!={k:v for k,v in row.items() if k!='file'}:
            raise ValueError('Fresh cohort disagrees with its generation receipt')
        seen.add(identity)
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',type=Path,required=True)
    worker(parser.parse_args().worker)
