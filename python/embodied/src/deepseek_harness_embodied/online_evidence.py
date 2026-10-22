"""Independent successful-episode audit of committed plans, stops and dispatch.

This verifies retained records, not the simulator or statistical acceptance.
Failed and incomplete episodes remain in the pilot denominator and cannot pass.
Native success requires an applied binding or a fresh post-stop observation.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .bridge import digest
from .online_model import execution_key


def audit_online_episode(root: Path, model_root: Path, *, initial_plan: dict | None = None) -> dict:
    """Check one completed online chain, including explicitly configured zero-call starts.

    Caller supplies the frozen deployment plan and separately checks native
    outcome files, provider bytes, assignments and implementation identities.
    """
    files, sessions, revisions = {}, set(), set()
    current = ack = fresh = trigger = fault = None
    bound = waiting = terminal = success_observed = False
    actions = replacements = model_calls = configured_plans = skill_failures = 0
    requested = last_time = float('-inf')

    def require(value, message):
        if not value:
            raise ValueError(message)

    def identifier(value):
        require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value),
                'Invalid evidence correlation identifier')
        return value

    def read(path):
        data = path.read_bytes()
        files[str(path)] = hashlib.sha256(data).hexdigest()
        return json.loads(data)

    identifier(root.name)
    for number, path in enumerate(sorted(root.glob('[0-9]*.json')), start=1):
        require(path.name==f'{number:06d}.json', 'Missing or unordered execution record')
        row = read(path)
        require(not terminal, 'Records follow the native terminal outcome')
        require(row['episode_id']==root.name and row['time']>=last_time, 'Episode identity or record order changed')
        last_time = row['time']
        if current is not None:
            require(row['generation']==current['generation'], 'Execution generation changed')
        kind = row['type']
        if kind=='plan_committed':
            revision_id, request_id = identifier(row['revision_id']), identifier(row['request_id'])
            require(revision_id not in revisions, 'Duplicate committed revision')
            proposal = root/('revision-'+revision_id+'.json')
            revision = read(proposal) if proposal.exists() else read(root/('binding-'+revision_id+'.json'))['revision']
            require(all(revision[k]==row[k] for k in (
                'revision_id','request_id','parent_id','plan','execution_key','observation_seq')),
                'Native binding and committed revision disagree')
            require(revision['episode_id']==root.name, 'Native binding has another episode')
            candidates = list(model_root.glob('*/'+root.name+'/'+request_id+'/receipt.json'))
            configured = root/('configured-plan-'+request_id+'.json')
            if current is None and initial_plan is not None:
                require(not candidates and configured.is_file(), 'Configured initial plan has missing or conflicting evidence')
                receipt = read(configured)
                require(receipt['origin']=='deployment' and receipt['model_calls']==0
                        and receipt['plan']==dict(initial_plan, execution='bind')==row['plan'],
                        'Configured plan differs from frozen deployment')
                require(receipt['identity']['episode_id']==root.name
                        and receipt['identity']['request_id']==request_id
                        and receipt['identity']['reset_generation']==revision['generation']
                        and receipt['catalog_revision']==revision['catalog_revision']
                        and receipt['binding_revision']==revision['binding_revision'],
                        'Configured plan has different execution identities')
                messages = receipt['messages']
                configured_plans += 1
            else:
                require(not configured.exists() and len(candidates)==1, 'Missing or conflicting model receipt')
                receipt = read(candidates[0])
                request = read(candidates[0].parent/'request.json')
                require(all(receipt[k]==request[k]==v for k, v in {
                    'episode_id':root.name,'request_id':request_id,
                    'reset_generation':revision['generation']}.items()), 'Model request identity mismatch')
                require(receipt['status']=='completed' and receipt['plan']==row['plan'], 'Uncommitted model output')
                require(receipt['input_digest']==digest({'messages':request['messages'],
                    'mode':receipt['mode'],'schema':receipt['schema_version']}), 'Model input digest mismatch')
                messages = request['messages']
                sessions.add(receipt['session_id'])
                model_calls += 1
            require(execution_key(row['plan'], messages)==row['execution_key'], 'Executable input digest mismatch')
            if current is not None:
                require(waiting and ack and fresh, 'Replacement preceded stop and fresh observation')
                require(row['parent_id']==current['revision_id'] and row['execution_key']!=current['execution_key'],
                        'Replacement has another parent or unchanged executable inputs')
                require(row['observation_seq']==fresh['observation_seq'], 'Replacement consumed another observation')
                require(all(revision[k]==current[k]==fresh[k] for k in (
                    'generation','catalog_revision','binding_revision')), 'Pinned execution identities changed')
                replacements += 1
            else:
                require(not row['parent_id'], 'Initial revision has a parent')
            revisions.add(revision_id)
            current, bound = revision, False
            waiting, ack, fresh, trigger = False, None, None, None
        elif kind=='binding_applied':
            require(current and not bound and row['revision_id']==current['revision_id'], 'Unknown or duplicate native binding')
            require(read(root/('binding-'+identifier(row['revision_id'])+'.json'))['revision']==current,
                    'Applied native binding differs from committed revision')
            bound, waiting, ack, fresh, trigger = True, False, None, None, None
            fault = None
        elif kind=='skill_failure':
            require(current and not fault and row['revision_id']==current['revision_id']
                    and row['operation'] in {'bind','observe','actions'}
                    and (bound or row['operation']=='bind'), 'Skill failure has no active binding attempt')
            diagnostic = read(root/('skill-failure-'+identifier(row['diagnostic_id'])+'.json'))
            require(all(diagnostic[k]==row[k] for k in (
                'episode_id','generation','revision_id','diagnostic_id','kind','operation'))
                and diagnostic['kind']=='skill_inference_failed'
                and diagnostic['inference_cleanup_confirmed'] is True
                and diagnostic['diagnostic']['operation']==row['operation'],
                'Skill failure lacks correlated cleanup confirmation')
            fault, bound = row, False
            skill_failures += 1
        elif kind=='action_dispatched':
            require(not success_observed and bound and not waiting and current and row['revision_id']==current['revision_id'],
                    'Action bypassed stopped or current revision')
            require(row['action_index']==actions, 'Duplicate or missing action index')
            actions += 1
        elif kind=='observed_event':
            if fault:
                require(row['event_id']=='skill-'+fault['diagnostic_id'] and row['kind']=='skill_failed'
                        and row['facts']=={'kind':fault['kind'],'operation':fault['operation']},
                        'Skill recovery trigger differs from confirmed fault')
            trigger = row
        elif kind=='stop_requested':
            require((bound or fault) and current and row['revision_id']==current['revision_id'] and trigger
                    and row['event_id']==trigger['event_id'], 'Stopping has no current binding or observed trigger')
            bound, waiting, requested = False, True, row['time']
        elif kind=='stop_acknowledged':
            require(waiting and not bound and ack is None, 'Unsolicited or duplicate stop acknowledgement')
            require(row['generation']==current['generation'] and row['queued_actions']==row['in_flight_actions']==0,
                    'Stop has another generation or remaining actions')
            require(requested<=row['stopped_at']<=row['time'], 'Invalid stop timing')
            ack = row
            fault = None
        elif kind=='fresh_observation':
            require(ack is not None and fresh is None, 'Observation preceded stopping or duplicates recapture')
            fresh = row['state']
            require(fresh['episode_id']==root.name and fresh['observation_seq']>max(
                ack['observation_seq'],trigger['observation_seq']) and fresh['observed_at']>=ack['stopped_at'],
                'Stale post-stop observation')
        elif kind=='execution_failed':
            raise ValueError('Execution failed; no successful-chain evidence')
        elif kind=='native_success':
            require(current and row['revision_id']==current['revision_id'] and not success_observed,
                    'Native success has no current revision or repeats')
            require((bound and not waiting) or (waiting and ack and fresh),
                    'Native success bypassed binding or post-stop observation')
            success_observed = True
        elif kind=='native_outcome':
            require(success_observed and row['terminal'] and row['success'] and row['actions']==actions,
                    'Native outcome did not confirm completed successful actions')
            terminal = True
        else:
            raise ValueError('Unknown execution record type: '+str(kind))
    require(terminal and current is not None, 'Missing terminal outcome or committed plan')
    require(len(sessions)==int(model_calls>0), 'Episode history split across Sessions')
    return {'qualification':False,'confirmed_successful_chain':True,'actions':actions,
            'replacements':replacements,'model_calls':model_calls,'configured_plans':configured_plans,
            'skill_failures':skill_failures,
            'session_count':len(sessions),'files':files}
