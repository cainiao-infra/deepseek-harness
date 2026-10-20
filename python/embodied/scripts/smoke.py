"""Two bounded live calls compare native planning input with the DSH bridge; no robot actions."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from deepseek_harness_embodied import ModelSpec, PlannerBridge, RequestIdentity
from deepseek_harness_embodied.bridge import write_record
from deepseek_harness_embodied.profile import install_source_profile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--physicalrsi', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--api-key-file', type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    spec = ModelSpec(**config['model_spec'])
    timeout = float(config['request_timeout_seconds'])
    if not 0 < timeout <= 120:
        raise ValueError('Live smoke requires a deadline no greater than 120 seconds')
    if args.api_key_file is not None:
        os.environ['PHYSICALRSI_AGENT_API_KEY'] = args.api_key_file.read_text().strip()
    key = os.environ['PHYSICALRSI_AGENT_API_KEY']
    sys.path.insert(0, str(args.physicalrsi.resolve()))
    from PhysicalRSI_baselines.robodojo.agent_planner import build_planner_messages, validate_planner_result
    import numpy as np
    root = Path(__file__).resolve().parents[3]
    home, evidence = Path(config['dsh_home']), Path(config['evidence_root'])
    install_source_profile(home, root/'packages/experimental/embodied-planner')
    compositions = {
        'pick': {'description':'Grasp and lift a visible object', 'steps':['memory.snapshot','pick']},
        'push': {'description':'Push a visible object along a surface without lifting', 'steps':['memory.snapshot','push']},
    }
    messages = build_planner_messages(instruction='Grasp and lift the blue object.',
        observation={'vision':{'camera0':{'color':np.full((8,8,3),[0,0,255],dtype=np.uint8)}}},
        memory={}, previous_plan=None, compositions=compositions)
    validate = lambda value:validate_planner_result(value, compositions=compositions)
    run = config['run_id']
    episode = uuid.uuid4().hex
    native_request = {'model':spec.model, 'temperature':spec.temperature, 'max_tokens':spec.max_tokens,
                      'response_format':{'type':'json_object'}, 'messages':messages}
    write_record(evidence/run/episode/'native-request.json', native_request)
    request = urllib.request.Request(spec.base_url.rstrip('/')+'/chat/completions',
        data=json.dumps(native_request).encode(), headers={
            'Authorization':'Bearer '+key, 'Content-Type':'application/json'})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    choice = result['choices'][0]
    if choice['finish_reason'] != 'stop':
        raise RuntimeError('Native response was incomplete')
    native = validate(json.loads(choice['message']['content']))
    write_record(evidence/run/episode/'native-response.json', result)
    print(json.dumps({'arm':'native','status':'completed','plan':native,
                      'elapsed_seconds':time.monotonic()-started}), flush=True)
    bridge = PlannerBridge(spec=spec, dsh_home=home, evidence_root=evidence,
        dsh_bin=config.get('dsh_bin'), runtime_cwd=config.get('runtime_cwd'))
    try:
        generation = bridge.begin_episode(0,episode)
        plan = bridge.plan(messages=messages, mode='composition', schema_version='physicalrsi-planner/v1',
            validate=validate, identity=RequestIdentity(run,episode,uuid.uuid4().hex,0,generation,
                                                       time.monotonic()+timeout))
        print(json.dumps({'arm':'dsh-compatibility','status':'completed','plan':plan}),flush=True)
    finally:
        bridge.close()
    print(json.dumps({'evidence':str(evidence/run/episode), 'qualification':'protocol_smoke_only',
                      'live_model_calls':2}),flush=True)


if __name__ == '__main__':
    main()
