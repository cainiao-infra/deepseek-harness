"""Offline-qualified candidates and bounded failure history for stopped execution.

Qualification labels stay private. The model sees only executable candidate IDs,
public observation predicates, phase and correlated decision identities.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from .bridge import digest
from .capability import CapabilitySpec, read_evidence
from .qualification import audit_qualification


class AdmissionStopped(RuntimeError):
    """No admitted decision exists; dispatch remains stopped and no plan is invented."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__('Execution stopped: '+reason)


def execution_descriptor(value: dict) -> dict:
    """Remove evidence output and validated fixed-program launch identities from a descriptor."""
    result = deepcopy(value)
    result['configuration'].pop('output', None)
    if result.get('implementation')=='code-policy' and result['configuration'].get('operation_memories') is not None:
        environment = result['configuration'].get('environment', {})
        environment.pop('PHYSICALRSI_OPERATION_PLAN', None)
        environment.pop('PHYSICALRSI_OPERATION_PLAN_SHA256', None)
    return result


def predicate_values(rules: list, observation: dict) -> dict:
    """Read configured public sensor fields; absent/nonfinite values remain unknown."""
    values = {}
    for rule in rules:
        value = observation
        for part in rule['path']:
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if hasattr(value, 'tolist'):
            value = value.tolist()
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if (not isinstance(value, (str, bool, int, float)) or
                isinstance(value, float) and not math.isfinite(value)):
            value = None
        values[rule['id']] = value
    return values


class AdmissionPolicy:
    """Own qualified identities and per-episode failure counts, separate from the catalog."""
    def __init__(self, root: Path, document: dict):
        fields = {'schema', 'subgoal_id', 'required_effects', 'control', 'max_transient_retries', 'entries'}
        if (not isinstance(document, dict) or set(document) != fields
                or document['schema'] != 'dsh-admission-policy/v1'
                or not isinstance(document['subgoal_id'], str) or not document['subgoal_id']
                or type(document['max_transient_retries']) is not int or document['max_transient_retries'] < 0
                or not isinstance(document['required_effects'], list) or not document['required_effects']
                or any(not isinstance(v,str) or not v for v in document['required_effects'])
                or not isinstance(document['entries'], list) or not document['entries']):
            raise ValueError('Complete versioned admission policy required')
        self.document = deepcopy(document)
        self.entries, self.failures = [], []
        identifiers = set()
        for row in document['entries']:
            if (not isinstance(row, dict) or set(row) not in ({'id','manifest','descriptor','sources','predicates'},
                                                     {'id','manifest','descriptor','sources','predicates','operation_plan'})
                    or not isinstance(row['id'], str) or not row['id'] or row['id'] in identifiers):
                raise ValueError('Unique complete admission entry required')
            identifiers.add(row['id'])
            manifest = json.loads(read_evidence(root, row['manifest']))
            manifest_root = (root/row['manifest']['path']).parent
            report = audit_qualification(manifest_root, manifest)
            spec = CapabilitySpec.parse(json.loads(read_evidence(manifest_root, manifest['capability'])))
            descriptor = json.loads(read_evidence(root, row['descriptor']))
            sources = json.loads(read_evidence(root, row['sources']))
            if digest({'descriptor': descriptor, 'sources': sources}) != spec.document['skill']['revision']:
                raise ValueError('Qualification identity differs from the retained executable descriptor and sources')
            if descriptor.get('implementation')=='code-policy' and descriptor['configuration'].get('operation_memories') is not None:
                if 'operation_plan' not in row:
                    raise ValueError('Retained fixed operation plan required')
                plan = json.loads(read_evidence(root, row['operation_plan']))
                memories = descriptor['configuration']['operation_memories']
                expected = {revision:{name:program['steps'] for name,program in memory['programs'].items()}
                            for revision,memory in memories.items()}
                environment = descriptor['configuration']['environment']
                if (plan.get('schema')!='physicalrsi.episode-operation-plan/v1' or plan.get('programs')!=expected
                        or digest(plan)!=environment.get('PHYSICALRSI_OPERATION_PLAN_SHA256')):
                    raise ValueError('Fixed program differs from the qualified launch plan')
            rules = row['predicates']
            if not isinstance(rules, list) or not rules:
                raise ValueError('Observable scope predicates required')
            names = set()
            for rule in rules:
                if (not isinstance(rule, dict) or set(rule) not in ({'id','path','equals'}, {'id','path','minimum','maximum'})
                        or not isinstance(rule['id'], str) or not rule['id'] or rule['id'] in names
                        or not isinstance(rule['path'], list) or not rule['path']
                        or rule['path'][0] not in {'instruction', 'state', 'vision'}
                        or any(not isinstance(part, str) or not part for part in rule['path'])):
                    raise ValueError('Unique public observation predicate required')
                names.add(rule['id'])
                if 'equals' in rule:
                    if (not isinstance(rule['equals'], (str, bool, int, float))
                            or type(rule['equals']) is float and not math.isfinite(rule['equals'])):
                        raise ValueError('Scalar predicate comparison required')
                elif (any(type(rule[k]) not in (int,float) or not math.isfinite(rule[k]) for k in ('minimum','maximum'))
                      or rule['minimum'] > rule['maximum']):
                    raise ValueError('Finite ordered predicate bounds required')
            self.entries.append({'entry': deepcopy(row), 'spec':spec, 'report':report,
                                 'descriptor':descriptor, 'sources':sources,
                                 'version':digest({'descriptor':execution_descriptor(descriptor),'sources':sources})})

    def reset(self) -> None:
        """Clear failure history only when the owning physical episode is reset."""
        self.failures.clear()

    def record_failure(self, candidate: dict, handling: dict, observation: dict) -> None:
        """Retain a cleanup-confirmed provider failure and its relevant predicate values."""
        entry = next(row for row in self.entries if row['entry']['id'] == candidate['id'])
        facts = predicate_values(entry['entry']['predicates'], observation)
        if any(name not in facts for name in handling['predicates']):
            raise ValueError('Failure names an unregistered observable predicate')
        self.failures.append({'version':entry['version'], 'reason':handling['reason'],
            'retry':handling['retry'], 'effects':handling['effects'],
            'predicates':{key:facts[key] for key in handling['predicates']}})

    def resolve(self, metadata: dict, observation: dict, *, sequence: int, parent: str,
                replacement: bool, restart_check) -> dict:
        """Classify every entry before inference; unknown facts never grant execution."""
        candidates, excluded, observed_predicates = [], [], {}
        for row in self.entries:
            entry, spec = row['entry'], row['spec'].document
            reasons, unknown = [], []
            verified = {item['effect'] for item in row['report']['effects'] if item['status'] == 'verified'}
            if not set(self.document['required_effects']) <= verified:
                reasons.append('execution_coverage_missing')
            if digest(metadata['catalog']) != spec['catalog_sha256']:
                reasons.append('catalog_changed')
            if spec['selection']['operations']:
                operations = spec['selection']['operations']
                registered = metadata['config'].get('program_registry', {}).get(operations[0]) if len(operations)==1 else None
                current = None if registered is None else {
                    'name':registered.get('source_name', operations[0]), 'implementation':'code-policy',
                    'configuration':registered['configuration']}
            else:
                current = metadata['config']['skills'].get(spec['skill']['name'])
            if (current is None or execution_descriptor(current) != execution_descriptor(row['descriptor'])
                    or metadata.get('execution_sources') != row['sources']):
                reasons.append('implementation_changed')
            facts = predicate_values(entry['predicates'], observation)
            observed_predicates[entry['id']] = facts
            for rule in entry['predicates']:
                value = facts[rule['id']]
                if value is None:
                    unknown.append(rule['id'])
                elif 'equals' in rule:
                    if type(value) is not type(rule['equals']) or value != rule['equals']:
                        reasons.append('predicate_false:'+rule['id'])
                elif type(value) not in (int,float) or not rule['minimum'] <= value <= rule['maximum']:
                    reasons.append('predicate_false:'+rule['id'])
            for issue in row['spec'].check_input(observation, self.document['control'], facts):
                unknown.append(issue['kind'])
            failures = [f for f in self.failures if f['version'] == row['version']]
            for failure in failures:
                if failure['retry'] == 'after_predicate_change':
                    previous = failure['predicates']
                    if not previous or not any(facts[k] is not None and previous[k] is not None
                                              and facts[k] != previous[k] for k in previous):
                        reasons.append('failure_conditions_unchanged')
                elif failure['retry'] == 'bounded_transient':
                    if sum(f['retry']=='bounded_transient' for f in failures) > self.document['max_transient_retries']:
                        reasons.append('transient_retry_limit')
                else:
                    reasons.append('persistent_failure')
            plan = {**spec['selection'], 'rationale':'Admitted candidate.'}
            if replacement:
                try:
                    restart_check(plan, observation)
                except (KeyError, TypeError, ValueError):
                    unknown.append('restart_not_admitted')
            if reasons or unknown:
                excluded.append({'id':entry['id'], 'status':'rejected' if reasons else 'undetermined',
                                 'reasons':sorted(set(reasons)), 'unknown':sorted(set(unknown))})
            else:
                candidates.append({'id':entry['id'], 'version':row['version'], 'selection':spec['selection']})
        state = {'schema':'dsh-candidate-set/v1','subgoal_id':self.document['subgoal_id'],
                 'observation_seq':sequence,'parent_revision':parent,'phase':'recovery' if replacement else 'initial',
                 'candidates':candidates,'excluded':excluded,'predicates':observed_predicates,
                 'failure_revision':digest(self.failures)}
        return {**state, 'revision':digest(state)}


def decision_schema(candidates: dict) -> dict:
    """Generate the execute/stop JSON definitions consumed by both model and validator."""
    common = {'candidate_set_revision':{'type':'string','const':candidates['revision']},
              'observation_seq':{'type':'integer','const':candidates['observation_seq']},
              'parent_revision':{'type':'string','const':candidates['parent_revision']},
              'rationale':{'type':'string','minLength':1}}
    def variant(kind, extra):
        properties = {'kind':{'type':'string','const':kind}, **common, **extra}
        return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
    return {'oneOf':[variant('execute', {'candidate_id':{'type':'string','enum':[
        row['id'] for row in candidates['candidates']]}}), variant('stop', {'reason':{'type':'string','minLength':1}})]}


def validate_decision(value: dict, candidates: dict) -> dict:
    """Reject stale identities and unavailable candidates without fabricating native fields."""
    if not isinstance(value, dict):
        raise ValueError('Versioned decision object required')
    definition = next((v for v in decision_schema(candidates)['oneOf']
                       if v['properties']['kind']['const'] == value.get('kind')), None)
    if definition is None or set(value) != set(definition['required']):
        raise ValueError('Exact execute or stop decision fields required')
    for key, rule in definition['properties'].items():
        item = value[key]
        if (rule['type'] == 'string' and (not isinstance(item, str)
                    or rule.get('minLength',0)>0 and not item.strip())
                or rule['type'] == 'integer' and type(item) is not int
                or 'const' in rule and item != rule['const'] or 'enum' in rule and item not in rule['enum']):
            raise ValueError('Stale or inadmissible decision field: '+key)
    return deepcopy(value)


def load_admission(reference: dict) -> AdmissionPolicy:
    """Load a digest-pinned deployment policy from an explicit absolute evidence root."""
    if (not isinstance(reference, dict) or set(reference) != {'root','path','sha256'}
            or not isinstance(reference['root'], str) or not Path(reference['root']).is_absolute()):
        raise ValueError('Explicit absolute admission root required')
    root = Path(reference['root'])
    raw = read_evidence(root, {key:reference[key] for key in ('path','sha256')})
    return AdmissionPolicy(root, json.loads(raw))
