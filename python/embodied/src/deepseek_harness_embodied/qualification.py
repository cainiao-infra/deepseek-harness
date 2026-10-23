"""Audit explicit native execution trials before claiming capability coverage.

The manifest pins source, dependencies, descriptors and every trial artifact.
Only native N trials are consumed: comparisons with DSH are a later increment.
The evaluator's facts remain offline and never enter a planner request.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .capability import CapabilitySpec, coverage, read_evidence
from .pilot_report import episode_summary


def file_reference(root: Path, path: Path) -> dict:
    """Create a digest reference for an existing regular file inside an evidence root."""
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Evidence must be a regular file inside its root')
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    return {'path': path.relative_to(root).as_posix(), 'sha256': hasher.hexdigest()}


def _json(root: Path, reference: dict):
    return json.loads(read_evidence(root, reference))


def _trial(root: Path, value: dict, spec: CapabilitySpec) -> dict:
    """Audit raw native binding/outcome evidence, retaining unsuccessful assignments."""
    fields = {'id', 'scope', 'episode_timeout_seconds', 'artifacts', 'effect_checks'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Complete execution trial required')
    if not isinstance(value['id'], str) or not value['id'] or value['scope'] != spec.document['scope']:
        raise ValueError('Trial identity and matching capability scope required')
    duration = value['episode_timeout_seconds']
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('Positive finite episode deadline required')
    artifacts = value['artifacts']
    required = {'result', 'deployment', 'configured_plan', 'native_binding', 'native_result',
                'timing', 'terminal', 'initial_observation', 'planner_audit'}
    if not isinstance(artifacts, dict) or set(artifacts) != required:
        raise ValueError('Execution trial requires raw binding, observation, outcome and planner evidence')
    if any(artifacts[key] is None for key in ('result', 'deployment', 'planner_audit')):
        raise ValueError('Assignment, deployment and planner audit cannot be absent')
    data = {key: None if ref is None else (_json(root, ref) if key != 'timing' else [json.loads(line)
            for line in read_evidence(root, ref).decode().splitlines()]) for key, ref in artifacts.items()}
    result, binding = data['result'], data['native_binding']
    if spec.document['skill']['revision'] is None:
        raise ValueError('Execution trial requires a resolved native skill revision')
    if result.get('schema') != 'dsh-paired-pilot-episode/v1' or result.get('arm') != 'N':
        raise ValueError('Explicit native pilot execution required')
    expected = spec.document['selection']
    plan = data['deployment'].get('initial_plan')
    if (not isinstance(plan, dict) or {key: plan.get(key) for key in expected} != expected
            or (data['configured_plan'] is not None
                and data['configured_plan'] != {'origin': 'deployment', 'plan': plan, 'model_calls': 0})
            or (binding is not None and binding.get('plan') != plan)):
        raise ValueError('Execution must bind the same explicitly configured capability')
    if binding is not None and (binding.get('composition_catalog_sha256') != spec.document['catalog_sha256']
            or any(binding.get('skill', {}).get(key) != expected_value
                   for key, expected_value in spec.document['skill'].items())):
        raise ValueError('Executed catalog or skill revision differs from the capability descriptor')
    if result['timings'] != (data['timing'] if data['timing'] is not None else []):
        raise ValueError('Execution summary differs from raw timing')
    if ((data['native_result'] is None and result['native_results'] != [])
            or (data['native_result'] is not None and (len(result['native_results']) != 1
                or result['native_results'][0]['result'] != data['native_result']))):
        raise ValueError('Execution summary differs from raw native result')
    planner = data['planner_audit']
    if (planner.get('schema') != 'dsh-execution-planner-audit/v1'
            or planner.get('origin') not in ('forbidden-gateway', 'retained-pilot-audit')
            or type(planner.get('attempts')) is not int or planner['attempts'] < 0):
        raise ValueError('Explicit planner audit required')
    # Raw receipt names are digest-pinned, including rejected or unknown attempts.
    if not isinstance(planner.get('attempt_files'), list) or len(planner['attempt_files']) != planner['attempts']:
        raise ValueError('Planner attempt inventory disagrees with its count')
    episode_directory = (root/artifacts['result']['path']).parent
    attempts = sorted([*episode_directory.glob('provider/attempts/call-*.json'),
                       *episode_directory.glob('provider/forbidden-*.json')])
    actual_attempts = [file_reference(root, path) for path in attempts]
    if actual_attempts != planner['attempt_files']:
        raise ValueError('Planner audit differs from retained attempt files')
    receipts = [json.loads(path.read_text()) for path in sorted(episode_directory.glob('provider/http-*/receipt.json'))]
    if result['provider_receipts'] != receipts:
        raise ValueError('Planner audit differs from retained provider receipts')
    if planner['origin'] == 'forbidden-gateway':
        policy = _json(root, planner['policy'])
        if policy != {'schema': 'dsh-planner-policy/v1', 'planner_mode': 'forbidden'}:
            raise ValueError('Forbidden planning requires its retained gateway policy')
    for reference in planner['attempt_files']:
        read_evidence(root, reference)
    summary = episode_summary(result, episode_timeout=value['episode_timeout_seconds'],
                              reserved_attempts=planner['attempts'])
    terminal = data['terminal']
    if terminal is not None and terminal.get('schema') != 'dsh-native-terminal-timing/v1':
        raise ValueError('Native terminal evidence required')
    observed_terminal = terminal.get('native_terminal') if terminal is not None else None
    terminal_success = observed_terminal is not None and observed_terminal.get('success') is True
    eligible = (summary['success'] and terminal_success and planner['attempts'] == 0
                and result['provider_receipts'] == [] and all(item is not None for item in data.values())
                and type(summary['actions']) is int and summary['actions'] > 0)
    checks = value['effect_checks']
    if not isinstance(checks, dict) or set(checks) - {e['id'] for e in spec.document['effects']}:
        raise ValueError('Effect checks must refer to declared effects')
    effects = {}
    for identifier, check in checks.items():
        if check == {'kind': 'native_task_success'}:
            # A whole-task evaluator cannot certify each intermediate effect separately.
            if identifier != 'task-complete':
                raise ValueError('Native task success only supports the task-complete effect')
            effects[identifier] = terminal_success and summary['native_success']
        else:
            raise ValueError('Unsupported independent effect evaluator')
    return {'id': value['id'], 'eligible': eligible, 'effects': effects,
            'native_success': summary['native_success'], 'planner_attempts': planner['attempts'],
            'planner_enforced': planner['origin'] == 'forbidden-gateway',
            'actions': summary['actions'], 'episode_error': summary['episode_error'],
            'launcher_error': summary['launcher_error'], 'artifact_digests': artifacts}


def audit_qualification(root: Path, manifest: dict) -> dict:
    """Recompute scoped coverage from pinned raw evidence; never qualify held-out gains.

Missing, altered or conflicting evidence fails the audit. Valid failed execution
remains in the denominator and leaves the affected capability unverified.
    """
    fields = {'schema', 'partition', 'capability', 'required_effects', 'sources', 'dependencies', 'assignments', 'trials'}
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest['schema'] != 'dsh-execution-qualification/v1'
            or manifest['partition'] != 'development'):
        raise ValueError('Versioned development qualification manifest required')
    spec = CapabilitySpec.parse(_json(root, manifest['capability']))
    environment_complete = True
    for category in ('sources', 'dependencies'):
        if not isinstance(manifest[category], list) or not manifest[category]:
            raise ValueError('Frozen source and dependency evidence required')
        for reference in manifest[category]:
            raw = read_evidence(root, reference)
            if category == 'dependencies':
                environment = json.loads(raw)
                if (environment.get('schema') != 'dsh-execution-environment/v1'
                        or environment.get('status') not in ('complete', 'incomplete')
                        or not isinstance(environment.get('versions'), dict)):
                    raise ValueError('Versioned execution environment inventory required')
                if environment['status'] == 'complete' and not environment['versions']:
                    raise ValueError('Complete environment requires dependency versions')
                environment_complete &= environment['status'] == 'complete'
    for effect in spec.document['effects']:
        for reference in effect['evidence']:
            read_evidence(root, reference)
    if not isinstance(manifest['trials'], list):
        raise ValueError('Explicit assigned trial list required')
    trials = [_trial(root, value, spec) for value in manifest['trials']]
    if len({trial['id'] for trial in trials}) != len(trials):
        raise ValueError('Duplicate execution trial')
    assignments = _json(root, manifest['assignments'])
    if (assignments.get('schema') != 'dsh-execution-assignments/v1'
            or assignments.get('scope') != spec.document['scope']
            or assignments.get('timing') not in ('prospective', 'retrospective')
            or assignments.get('trial_ids') != [trial['id'] for trial in trials]):
        raise ValueError('Every frozen assignment must be retained in its declared order')
    if not environment_complete:
        for trial in trials:
            trial['eligible'] = False
    return {'schema': 'dsh-execution-qualification-report/v1', 'partition': 'development',
            'qualification': False, 'formal_heldout_runs': 0,
            'environment_complete': environment_complete,
            'assignment_timing': assignments['timing'],
            **coverage(spec, manifest['required_effects'], trials), 'trials': trials,
            'sources': manifest['sources'], 'dependencies': manifest['dependencies']}
