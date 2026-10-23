"""Offline capability qualification tied to native catalog and skill revisions.

Descriptions never grant execution coverage. Unsupported effects stay distinct
from implemented effects that have not passed a zero-planner execution trial.
These records are evaluation inputs, not additional planner context.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .bridge import digest


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Nonempty {name} required')
    return value


def _sha(value) -> str:
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('SHA-256 required')
    return value


def read_evidence(root: Path, reference: dict) -> bytes:
    """Read a relative, digest-pinned file confined to the supplied evidence root."""
    if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
        raise ValueError('Evidence requires a path and SHA-256')
    name = _text(reference['path'], 'evidence path')
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError('Evidence path must remain inside its root')
    resolved = (root/path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('Evidence symlink escapes its root')
    raw = resolved.read_bytes()
    if hashlib.sha256(raw).hexdigest() != _sha(reference['sha256']):
        raise ValueError(f'Evidence digest mismatch: {name}')
    return raw


@dataclass(frozen=True)
class CapabilitySpec:
    """Versioned deployment scope for one existing native capability selection.

The copied document includes control conventions, required observation paths,
observable predicates, lifecycle support and per-effect implementation evidence.
It neither registers a skill nor changes the native capability catalog.
    """
    document: dict

    @classmethod
    def parse(cls, document: dict) -> CapabilitySpec:
        """Validate a descriptor loaded from JSON; reject ambiguous scope or effects."""
        fields = {'schema', 'capability_id', 'scope', 'catalog_sha256', 'skill', 'selection',
                  'control', 'observations', 'start_predicates', 'lifecycle', 'effects'}
        if not isinstance(document, dict) or set(document) != fields:
            raise ValueError('Complete capability descriptor fields required')
        if document['schema'] != 'dsh-capability-spec/v1':
            raise ValueError('Unsupported capability schema')
        for name in ('capability_id', 'scope'):
            _text(document[name], name)
        if document['catalog_sha256'] is not None:
            _sha(document['catalog_sha256'])
        skill = document['skill']
        if not isinstance(skill, dict) or set(skill) != {'name', 'revision'}:
            raise ValueError('Native skill name and revision required')
        _text(skill['name'], 'skill name')
        if skill['revision'] is not None:
            _sha(skill['revision'])
        selection = document['selection']
        if not isinstance(selection, dict) or set(selection) != {'composition', 'operations'}:
            raise ValueError('Explicit native capability selection required')
        _text(selection['composition'], 'composition')
        if not isinstance(selection['operations'], list):
            raise ValueError('Registered operation IDs required')
        for operation in selection['operations']:
            _text(operation, 'operation ID')
        control = document['control']
        if not isinstance(control, dict) or set(control) != {
                'embodiment', 'action_type', 'frame', 'units', 'semantics', 'cadence'}:
            raise ValueError('Explicit control conventions required')
        for name, value in control.items():
            _text(value, name)
        observations = document['observations']
        if not isinstance(observations, list) or not observations:
            raise ValueError('Required observation paths must be declared')
        for path in observations:
            if not isinstance(path, list) or not path:
                raise ValueError('Nonempty observation path required')
            for part in path:
                _text(part, 'observation path component')
        predicates = document['start_predicates']
        if not isinstance(predicates, list):
            raise ValueError('Start predicates must be explicit, including an empty list')
        for predicate in predicates:
            _text(predicate, 'start predicate')
        lifecycle = document['lifecycle']
        if not isinstance(lifecycle, dict) or set(lifecycle) != {'interrupt', 'restart'}:
            raise ValueError('Explicit lifecycle support required')
        if any(value not in ('supported', 'unsupported', 'unknown') for value in lifecycle.values()):
            raise ValueError('Unsupported lifecycle state')
        effects = document['effects']
        if not isinstance(effects, list) or not effects:
            raise ValueError('Nonempty effect declarations required')
        ids = set()
        for effect in effects:
            if not isinstance(effect, dict) or set(effect) != {'id', 'implementation', 'completion', 'evidence'}:
                raise ValueError('Complete effect declaration required')
            identifier = _text(effect['id'], 'effect ID')
            if identifier in ids:
                raise ValueError('Duplicate effect ID')
            ids.add(identifier)
            if effect['implementation'] not in ('available', 'unsupported', 'unknown'):
                raise ValueError('Unsupported implementation state')
            _text(effect['completion'], 'observable completion predicate')
            if not isinstance(effect['evidence'], list):
                raise ValueError('Effect evidence references required')
            if effect['implementation'] != 'unknown' and not effect['evidence']:
                raise ValueError('Known implementation support requires inspected source evidence')
        return cls(deepcopy(document))

    @property
    def revision(self) -> str:
        """Return the descriptor digest, separate from the native skill revision."""
        return digest(self.document)

    def check_input(self, observation: dict, control: dict, predicates: dict) -> list[dict]:
        """Report unsupported or unknown requirements before a qualification binding.

Control metadata is supplied by the reviewed deployment provider. Predicate
values come from allowed observations; absent facts never become true.
        """
        issues = []
        if self.document['catalog_sha256'] is None:
            issues.append({'kind': 'catalog_revision_unknown'})
        if self.document['skill']['revision'] is None:
            issues.append({'kind': 'skill_revision_unknown'})
        for name, expected in self.document['control'].items():
            actual = control.get(name)
            if actual != expected:
                issues.append({'kind': 'control_mismatch' if actual is not None else 'control_unknown',
                               'field': name, 'expected': expected, 'actual': actual})
        for path in self.document['observations']:
            value = observation
            for part in path:
                if not isinstance(value, dict) or part not in value:
                    value = None
                    break
                value = value[part]
            if value is None or (hasattr(value, '__len__') and len(value) == 0):
                issues.append({'kind': 'missing_observation', 'path': path})
        for predicate in self.document['start_predicates']:
            value = predicates.get(predicate)
            if value is not True:
                issues.append({'kind': 'predicate_false' if value is False else 'predicate_unknown',
                               'predicate': predicate})
        return issues


def coverage(spec: CapabilitySpec, required: list[str], trials: list[dict]) -> dict:
    """Reduce audited trials without discarding failures or inferring partial success.

A verified effect requires every assigned trial to finish cleanly with that
independently evaluated effect true. This is scoped execution evidence, never
held-out statistical qualification. Unmeasured effects remain unverified.
    """
    if not required or len(set(required)) != len(required) or any(not isinstance(x, str) or not x for x in required):
        raise ValueError('Unique nonempty required effects required')
    declared = {effect['id']: effect for effect in spec.document['effects']}
    rows = []
    for identifier in required:
        effect = declared.get(identifier)
        state = 'unverified'
        reason = 'effect_not_declared' if effect is None else 'execution_not_verified'
        if effect is not None and effect['implementation'] == 'unsupported':
            state, reason = 'unsupported', 'implementation_unavailable'
        elif effect is not None and effect['implementation'] == 'available' and trials:
            if all(row['eligible'] and row['effects'].get(identifier) is True for row in trials):
                state, reason = 'verified', 'all_assigned_execution_trials_passed'
        rows.append({'effect': identifier, 'status': state, 'reason': reason,
                     'passed_trials': sum(row['eligible'] and row['effects'].get(identifier) is True for row in trials),
                     'assigned_trials': len(trials)})
    return {'capability_id': spec.document['capability_id'], 'scope': spec.document['scope'],
            'spec_revision': spec.revision, 'effects': rows,
            'covered': all(row['status'] == 'verified' for row in rows)}
