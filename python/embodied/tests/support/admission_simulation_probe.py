"""Controlled v2 decisions with real RoboDojo execution; no live model-quality claim.

The driver owns an isolated output root and an injected worker launcher. It uses
normal SDK sessions and the paired pilot's recorded gateway, replacing only the
outbound provider transport with a finite local response. Real robot actions,
observations, cleanup and native success evaluation are unchanged.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
import paired_pilot
from deepseek_harness_embodied.bridge import write_record


class RecordedResponse(io.BytesIO):
    """HTTP response consumed by the ordinary bounded, receipt-writing gateway."""
    status = 200
    headers = {'Content-Type':'text/event-stream'}


class ControlledGateway(paired_pilot.Gateway):
    """Never contact an external endpoint; retain one explicitly controlled decision."""
    def __init__(self, root, spec, key):
        super().__init__(root, spec, 'controlled-provider-no-credential')
        owner = self
        self.count = 0
        class Transport:
            def open(self, request, timeout):
                if owner.count:
                    raise RuntimeError('Unexpected second controlled planning request')
                owner.count += 1
                payload = json.loads(request.data)
                control = json.loads(payload['messages'][-1]['content'][0]['text'])
                candidates = control['candidate_set']
                result = {'kind':'execute','candidate_id':candidates['candidates'][0]['id'],
                    'candidate_set_revision':candidates['revision'],
                    'observation_seq':candidates['observation_seq'],'parent_revision':candidates['parent_revision'],
                    'rationale':'Use the currently admitted candidate after confirmed stopping.'}
                mode = spec['controlled_decision']
                if mode=='unavailable': result['candidate_id']='visual-history-pickup'
                elif mode=='stale': result['observation_seq']-=1
                elif mode=='stop':
                    result.pop('candidate_id');result.update(kind='stop',reason='Controlled stop decision.')
                elif mode!='execute': raise ValueError('Unknown controlled decision')
                write_record(root/'controlled-decision.json', {'mode':mode,'decision':result,'external_provider_contacted':False})
                frames = [{'choices':[{'index':0,'delta':{'role':'assistant','content':json.dumps(result)},'finish_reason':None}]},
                    {'choices':[{'index':0,'delta':{},'finish_reason':'stop'}],
                     'usage':{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}}]
                raw = ''.join('data: '+json.dumps(frame)+'\n\n' for frame in frames)+'data: [DONE]\n\n'
                return RecordedResponse(raw.encode())
        self.opener = Transport()


def main():
    """Freeze all cases before real simulator launches; preserve unsuccessful trials."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec',required=True,type=Path)
    args=parser.parse_args();document=json.loads(args.spec.read_text())
    root=Path(document['output']);root.mkdir(parents=True,exist_ok=False)
    write_record(root/'assignments.json',document)
    previous=paired_pilot.Gateway
    paired_pilot.Gateway=ControlledGateway
    try:
        for index,row in enumerate(document['trials']):
            spec=row['pilot'];paired_pilot.validate_spec(spec)
            inputs=paired_pilot.resolve_inputs(spec)
            result=paired_pilot.episode(spec,row['case'],'H1',index,None,inputs)
            if 'client_exit' not in result:
                raise RuntimeError('Deployment failed before simulator completion; dependent cases remain assigned')
            print(json.dumps({'id':row['case']['case_id'],'error':result.get('error'),
                'timings':result['timings'],'native_results':result['native_results']}),flush=True)
    finally:
        paired_pilot.Gateway=previous


if __name__=='__main__':main()
