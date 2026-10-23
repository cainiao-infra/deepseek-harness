"""Capability coverage cannot be inferred from loaded weights or partial outcomes."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from deepseek_harness_embodied.binding_probe import probe_binding
from deepseek_harness_embodied.bridge import digest
from deepseek_harness_embodied.capability import CapabilitySpec, coverage, read_evidence
from deepseek_harness_embodied.qualification import audit_qualification, file_reference
from deepseek_harness_embodied.policy_diagnostics import encoded_record, inspect_policy


def descriptor():
    return {'schema': 'dsh-capability-spec/v1', 'capability_id': 'pickup', 'scope': 'one development pickup scene',
        'catalog_sha256': digest({'pick': {}}), 'skill': {'name': 'pick', 'revision': '1'*64},
        'selection': {'composition': 'pick', 'operations': []},
        'control': {'embodiment': 'arx_x5', 'action_type': 'joint', 'frame': 'robot-joints',
                    'units': 'radians; grippers normalized', 'semantics': 'absolute', 'cadence': 'one observation per action'},
        'observations': [['vision', 'head', 'color'], ['state', 'joints']], 'start_predicates': ['gripper-open'],
        'lifecycle': {'interrupt': 'unknown', 'restart': 'unknown'},
        'effects': [{'id': 'task-complete', 'implementation': 'available', 'completion': 'target lifted 10 cm',
                     'evidence': [{'path': 'source.py', 'sha256': '2'*64}]}]}


def observation():
    return {'vision': {'head': {'color': [[[1, 2, 3]]]}}, 'state': {'joints': [0, 0]}, 'instruction': 'lift'}


@pytest.mark.parametrize('change', [
    {'schema': 'unknown'}, {'catalog_sha256': 'x'}, {'skill': {'name': 'pick'}},
    {'selection': {'composition': 'pick', 'operations': [{'invented': 'parameter'}]}},
    {'control': {}}, {'observations': [[]]}, {'observations': []},
    {'start_predicates': None}, {'lifecycle': {'interrupt': True, 'restart': 'unknown'}},
    {'effects': []}, {'effects': [dict(descriptor()['effects'][0], evidence=[])]},
    {'effects': descriptor()['effects']*2},
])
def test_incomplete_or_ambiguous_capability_is_rejected(change):
    with pytest.raises(ValueError):
        CapabilitySpec.parse(dict(descriptor(), **change))


@pytest.mark.parametrize('field,actual', [('units', 'degrees'), ('frame', 'world'), ('semantics', 'delta'), ('cadence', None)])
def test_wrong_control_convention_stops_before_worker_creation(tmp_path, field, actual):
    spec = CapabilitySpec.parse(descriptor())
    control = dict(spec.document['control'], **{field: actual})
    def forbidden():
        pytest.fail('Input validation must precede policy construction')
    result = probe_binding(spec=spec, observation=observation(), control=control, predicates={'gripper-open': True},
                           factory=forbidden, output=tmp_path/'probe')
    assert not result['passed'] and not result['binding_succeeded']
    assert result['issues'][0]['field'] == field
    assert result['robot_actions_dispatched'] == 0 and result['cleanup_confirmed'] is None


def test_unknown_observation_and_start_condition_never_default_to_supported():
    spec = CapabilitySpec.parse(descriptor())
    assert spec.check_input(observation(), spec.document['control'], {'gripper-open': True}) == []
    issues = spec.check_input({'vision': {}, 'state': {'joints': []}}, spec.document['control'], {})
    assert [row['kind'] for row in issues] == ['missing_observation', 'missing_observation', 'predicate_unknown']
    assert spec.check_input(observation(), spec.document['control'], {'gripper-open': 1})[0]['kind'] == 'predicate_unknown'


def test_inference_success_does_not_verify_task_effect_and_cleanup_is_independent(tmp_path):
    spec = CapabilitySpec.parse(descriptor())
    calls = []
    def bind(plan, value):
        calls.append(('bind', plan, value))
        return {'skill': spec.document['skill']}
    def close():
        calls.append('close')
        raise TimeoutError('worker not reaped')
    binding = SimpleNamespace(metadata={'catalog': {'pick': {}}}, bind=bind,
                              actions=lambda: [{'joint': [0.1]}], close=close)
    result = probe_binding(spec=spec, observation=observation(), control=spec.document['control'],
                           predicates={'gripper-open': True}, factory=lambda: binding, output=tmp_path/'probe')
    assert result['inference_succeeded'] and not result['passed']
    assert result['cleanup_error']['type'] == 'TimeoutError' and calls[-1] == 'close'
    assert json.loads((tmp_path/'probe/actions.json').read_text()) == [{'joint': [0.1]}]
    assert not coverage(spec, ['task-complete'], [])['covered']


@pytest.mark.parametrize('fault', ['catalog', 'skill', 'inference'])
def test_failed_probe_closes_worker_and_retains_failure(tmp_path, fault):
    spec = CapabilitySpec.parse(descriptor())
    closed = []
    def actions():
        raise RuntimeError('failed inference')
    binding = SimpleNamespace(metadata={'catalog': {'other' if fault == 'catalog' else 'pick': {}}},
        bind=lambda *_: {'skill': dict(spec.document['skill'], revision='0'*64) if fault == 'skill' else spec.document['skill']},
        actions=actions, close=lambda: closed.append(True))
    result = probe_binding(spec=spec, observation=observation(), control=spec.document['control'],
        predicates={'gripper-open': True}, factory=lambda: binding, output=tmp_path/'probe')
    assert closed == [True] and result['error'] is not None
    assert not result['passed'] and result['cleanup_confirmed']
    assert not (tmp_path/'probe/actions.json').exists()


def test_toolbox_missing_implementations_remain_explicit():
    value = descriptor()
    value['effects'] = [dict(value['effects'][0], id='tape-measure', implementation='unknown'),
                        dict(value['effects'][0], id='hammer', implementation='unsupported')]
    result = coverage(CapabilitySpec.parse(value), ['tape-measure', 'hammer', 'pliers', 'wrench'], [])
    assert [row['status'] for row in result['effects']] == ['unverified', 'unsupported', 'unverified', 'unverified']
    assert not result['covered']


def test_encoded_policy_diagnostics_preserve_camera_order_and_state_without_observing(monkeypatch):
    import numpy as np
    from deepseek_harness_embodied import policy_diagnostics
    monkeypatch.setattr(policy_diagnostics.metadata, 'version', lambda _: 'fixture')
    window = {'state': np.array([[1., 2., 3.]], dtype=np.float32),
              'images': {'left': np.zeros((1, 3, 4, 4), dtype=np.uint8),
                         'right': np.ones((1, 3, 4, 4), dtype=np.uint8)}}
    implementation = SimpleNamespace(observation_window=window)
    skill = SimpleNamespace(implementation=implementation, describe=lambda: {'name': 'pi05'})
    model = SimpleNamespace(sessions={0: {'skill': skill}})
    record = inspect_policy(model)
    assert record['encoded_observation']['state']['values'] == [[1., 2., 3.]]
    cameras = record['encoded_observation']['images']
    assert cameras['left']['sha256'] != cameras['right']['sha256']
    assert cameras['left']['shape'] == [1, 3, 4, 4] and 'values' not in cameras['left']
    assert implementation.observation_window is window
    with pytest.raises(ValueError, match='Unsupported'):
        encoded_record(object())
    model.sessions = {}
    with pytest.raises(ValueError, match='one bound'):
        inspect_policy(model)


def write(root, name, value):
    path = root/name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value)+'\n')
    return file_reference(root, path)


def trial_manifest(root):
    source = write(root, 'source.py', 'inspected implementation')
    dependency = write(root, 'dependencies.json', {'schema': 'dsh-execution-environment/v1',
                       'status': 'complete', 'versions': {'runtime': 'test-fixture'}})
    spec = descriptor()
    spec['effects'][0]['evidence'] = [source]
    plan = dict(spec['selection'], rationale='explicit binding')
    native = {'eval_time': 1, 'success_rate': 1, 'score': 100, 'details': {'0': {'success': True, 'score': 1}}}
    timing = {'elapsed_seconds': 1.0, 'error': None, 'actions': 4}
    result = {'schema': 'dsh-paired-pilot-episode/v1', 'qualification': False, 'case': {'case_id': 'fixture'},
        'arm': 'N', 'client_exit': 0, 'process_timeout': False, 'error': None,
        'native_results': [{'path': 'simulation/_result.json', 'result': native}],
        'timings': [timing], 'provider_receipts': []}
    records = {'result': result, 'deployment': {'initial_plan': plan},
        'configured_plan': {'origin': 'deployment', 'plan': plan, 'model_calls': 0},
        'native_binding': {'plan': plan, 'composition_catalog_sha256': spec['catalog_sha256'], 'skill': spec['skill']},
        'native_result': native, 'timing': timing, 'initial_observation': observation(),
        'terminal': {'schema': 'dsh-native-terminal-timing/v1', 'native_terminal': {'success': True}},
        'planner_audit': {'schema': 'dsh-execution-planner-audit/v1', 'origin': 'retained-pilot-audit',
                          'attempts': 0, 'attempt_files': []}}
    artifacts = {key: write(root, 'trial/'+key+'.json', value) for key, value in records.items()}
    return {'schema': 'dsh-execution-qualification/v1', 'partition': 'development',
        'capability': write(root, 'capability.json', spec), 'required_effects': ['task-complete'],
        'assignments': write(root, 'assignments.json', {'schema': 'dsh-execution-assignments/v1',
            'scope': spec['scope'], 'timing': 'prospective', 'trial_ids': ['one']}),
        'sources': [source], 'dependencies': [dependency], 'trials': [{'id': 'one', 'scope': spec['scope'],
            'episode_timeout_seconds': 3, 'artifacts': artifacts,
            'effect_checks': {'task-complete': {'kind': 'native_task_success'}}}]}


def test_raw_execution_evidence_supports_only_the_measured_scope(tmp_path):
    manifest = trial_manifest(tmp_path)
    result = audit_qualification(tmp_path, manifest)
    assert result['covered'] and result['effects'][0]['status'] == 'verified'
    assert result['qualification'] is False and result['formal_heldout_runs'] == 0
    assert result['trials'][0]['planner_enforced'] is False


def test_failed_trial_remains_in_coverage_denominator(tmp_path):
    manifest = trial_manifest(tmp_path)
    failed = deepcopy(manifest['trials'][0])
    failed['id'] = 'two'
    data = json.loads((tmp_path/'trial/result.json').read_text())
    data['error'] = {'type': 'CleanupFailure', 'message': 'unknown exit'}
    failed['artifacts']['result'] = write(tmp_path, 'failed/result.json', data)
    manifest['trials'].append(failed)
    manifest['assignments'] = write(tmp_path, 'assignments.json', {'schema': 'dsh-execution-assignments/v1',
        'scope': failed['scope'], 'timing': 'prospective', 'trial_ids': ['one', 'two']})
    report = audit_qualification(tmp_path, manifest)
    assert not report['covered']
    assert report['effects'][0]['assigned_trials'] == 2 and report['effects'][0]['passed_trials'] == 1
    manifest['trials'].pop()
    with pytest.raises(ValueError, match='Every frozen assignment'):
        audit_qualification(tmp_path, manifest)


def test_startup_failure_without_binding_or_native_outcome_remains_assigned(tmp_path):
    manifest = trial_manifest(tmp_path)
    trial = manifest['trials'][0]
    result = json.loads((tmp_path/'trial/result.json').read_text())
    result.update(native_results=[], timings=[], client_exit=None,
                  error={'type': 'RuntimeError', 'message': 'worker exited before readiness'})
    trial['artifacts']['result'] = write(tmp_path, 'trial/result.json', result)
    for name in ('configured_plan', 'native_binding', 'native_result', 'timing', 'terminal', 'initial_observation'):
        trial['artifacts'][name] = None
    report = audit_qualification(tmp_path, manifest)
    assert report['effects'][0]['assigned_trials'] == 1 and not report['covered']
    assert report['trials'][0]['launcher_error']['type'] == 'RuntimeError'


def test_incomplete_dependency_inventory_cannot_certify_a_success(tmp_path):
    manifest = trial_manifest(tmp_path)
    manifest['dependencies'] = [write(tmp_path, 'dependencies.json',
        {'schema': 'dsh-execution-environment/v1', 'status': 'incomplete', 'versions': {}})]
    report = audit_qualification(tmp_path, manifest)
    assert not report['covered'] and not report['environment_complete']


@pytest.mark.parametrize('name', ['native_result', 'native_binding', 'terminal', 'initial_observation', 'timing'])
def test_tampered_or_missing_raw_evidence_cannot_qualify(tmp_path, name):
    manifest = trial_manifest(tmp_path)
    path = tmp_path/manifest['trials'][0]['artifacts'][name]['path']
    path.write_text('{}')
    with pytest.raises(ValueError, match='digest mismatch'):
        audit_qualification(tmp_path, manifest)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        audit_qualification(tmp_path, manifest)


def test_unreported_model_attempt_is_rejected(tmp_path):
    manifest = trial_manifest(tmp_path)
    write(tmp_path, 'trial/provider/attempts/call-0000.json', {'unknown': True})
    with pytest.raises(ValueError, match='retained attempt'):
        audit_qualification(tmp_path, manifest)


def test_catalog_drift_and_partial_task_success_cannot_verify_an_effect(tmp_path):
    manifest = trial_manifest(tmp_path)
    binding = json.loads((tmp_path/'trial/native_binding.json').read_text())
    binding['skill']['revision'] = '9'*64
    manifest['trials'][0]['artifacts']['native_binding'] = write(tmp_path, 'trial/native_binding.json', binding)
    with pytest.raises(ValueError, match='revision'):
        audit_qualification(tmp_path, manifest)
    manifest = trial_manifest(tmp_path)
    manifest['trials'][0]['effect_checks'] = {'tape-measure': {'kind': 'native_task_success'}}
    with pytest.raises(ValueError, match='declared effects'):
        audit_qualification(tmp_path, manifest)


def test_evidence_references_cannot_escape_through_paths_or_symlinks(tmp_path):
    root = tmp_path/'root'
    root.mkdir()
    outside = tmp_path/'outside'
    outside.write_text('private')
    for path in ('../outside', str(outside), '..\\outside'):
        with pytest.raises(ValueError, match='inside'):
            read_evidence(root, {'path': path, 'sha256': '0'*64})
    (root/'link').symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        read_evidence(root, {'path': 'link', 'sha256': '0'*64})


def test_qualification_cli_writes_uncovered_result_and_nonzero_status(tmp_path, monkeypatch, capsys):
    manifest = trial_manifest(tmp_path)
    manifest['required_effects'].append('unsupported-extra-effect')
    write(tmp_path, 'manifest.json', manifest)
    import sys
    monkeypatch.setattr(sys, 'argv', ['qualify_execution', '--manifest', str(tmp_path/'manifest.json'),
                                     '--output', str(tmp_path/'report.json')])
    script = Path(__file__).parents[1]/'scripts/qualify_execution.py'
    with pytest.raises(SystemExit) as failure:
        runpy.run_path(str(script), run_name='__main__')
    assert failure.value.code == 2
    output = json.loads(capsys.readouterr().out)
    assert not output['covered'] and output['effects'][1]['status'] == 'unverified'
    assert json.loads((tmp_path/'report.json').read_text())['qualification'] is False


@pytest.mark.parametrize('name', ['pi05-general-pickup', 'pi05-stack-blocks-by-language',
                                 'pi05-store-tools-in-toolbox', 'toolbox-code', 'sparse-memory'])
def test_retained_development_reports_recompute_from_raw_evidence(name):
    root = Path(__file__).parents[1]/'evaluations/2026-10-23-execution-qualification'
    report = audit_qualification(root, json.loads((root/(name+'.manifest.json')).read_text()))
    assert report == json.loads((root/(name+'.report.json')).read_text())
    assert not report['covered'] and report['formal_heldout_runs'] == 0


def test_restored_renderer_reports_retain_all_assignments_and_recompute():
    root = Path(__file__).parents[1]/'evaluations/2026-10-23-execution-qualification'
    summary = json.loads((root/'restored-summary.json').read_text())
    assignments = json.loads((root/'restored-assignments.json').read_text())['trials']
    restored = [row for row in summary['trials'] if row['batch'] == '12-restored']
    assert {row['id'] for row in restored} == {
        'restored-'+row['id'].replace('_', '-') for row in assignments}
    assert len(restored) == len(assignments) == 6
    assert summary['cleanup']['all_experiments_quiescent']
    for row in summary['trials']:
        manifest = json.loads((root/(row['id']+'.manifest.json')).read_text())
        actual = audit_qualification(root, manifest)
        assert actual == json.loads((root/row['report']).read_text())
        assert actual['covered'] == row['covered']
        assert actual['formal_heldout_runs'] == 0
        assert actual['trials'][0]['planner_attempts'] == 0


def test_transform_capture_uses_actual_call_once_and_preserves_result(tmp_path):
    import numpy as np
    from deepseek_harness_embodied.policy_diagnostics import capture_transform
    calls = []
    def transform(value):
        calls.append(value)
        value['state'][0] = 2
        return value
    wrapped = capture_transform(transform, tmp_path/'transform.json')
    value = {'state': np.array([1.0])}
    assert wrapped(value) is value and len(calls) == 1
    record = json.loads((tmp_path/'transform.json').read_text())
    assert record['input']['state']['values'] == [1.0]
    assert record['output']['state']['values'] == [2.0]
    assert wrapped(value) is value and len(calls) == 2
    assert json.loads((tmp_path/'transform.json').read_text()) == record


def test_native_diagnostics_do_not_duplicate_observations_or_inference(tmp_path, monkeypatch):
    from deepseek_harness_embodied import policy_diagnostics
    import numpy as np
    monkeypatch.setattr(policy_diagnostics, 'inspect_policy', lambda _: {'schema': 'fixture'})
    policy = SimpleNamespace(_input_transform=lambda x: x, _output_transform=lambda x: x)
    class Native:
        def __init__(self):
            self.sessions = {0: {'skill': SimpleNamespace(implementation=SimpleNamespace(policy=policy))}}
            self.observations = self.actions = 0
        def update_obs_batch(self, values):
            self.observations += 1
        def get_action_batch(self):
            self.actions += 1
            return [[{'joints': np.array([1.0])}]]
    model = policy_diagnostics.diagnostic_model_type(Native, tmp_path)()
    for _ in range(2):
        model.update_obs_batch([observation()])
        model.get_action_batch()
    assert model.observations == model.actions == 2
    assert json.loads((tmp_path/'native-actions.json').read_text()) == [[{'joints': [1.0]}]]
    assert json.loads((tmp_path/'raw-observation.json').read_text()) == [observation()]


def test_native_only_runner_freezes_assignments_and_keeps_failed_trials(tmp_path, monkeypatch):
    import importlib.util
    scripts = Path(__file__).parents[1]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    definition = importlib.util.spec_from_file_location('execution_trials_test', scripts/'execution_trials.py')
    module = importlib.util.module_from_spec(definition)
    definition.loader.exec_module(module)
    monkeypatch.setattr(module, 'validate_spec', lambda _: None)
    monkeypatch.setattr(module, 'resolve_inputs', lambda _: {'online': {}})
    assigned = [{'id': name, 'case_id': 'scene', 'plan': {'composition': name, 'operations': [], 'rationale': 'fixed'}}
                for name in ('pi05', 'sparse')]
    root = tmp_path/'output'
    document = {'schema': 'dsh-execution-trials/v1',
        'pilot': {'output': str(root), 'planner_mode': 'forbidden', 'cases': [{'case_id': 'scene'}]}, 'trials': assigned}
    calls = []
    def episode(spec, case, arm, index, key, inputs):
        assert json.loads((root/'assignments.json').read_text()) == document
        assert arm == 'N' and key is None and spec['capture_execution_diagnostics']
        assert inputs['online']['initial_plan'] == assigned[index]['plan']
        calls.append(case['case_id'])
        return {'native_results': [], 'error': {'type': 'InferenceFailure'}}
    monkeypatch.setattr(module, 'episode', episode)
    module.run(document)
    assert calls == ['pi05', 'sparse']
    document['pilot']['planner_mode'] = 'enabled'
    with pytest.raises(ValueError, match='forbidden planning'):
        module.run(document)


@pytest.mark.parametrize('corruption', [None, 'camera', 'state', 'tensor', 'native', 'rpc'])
def test_trace_audit_rejects_camera_state_and_command_mismatches(corruption):
    import numpy as np
    from deepseek_harness_embodied.policy_diagnostics import audit_trace, encoded_record
    keys = ['left_arm_joint_state', 'left_ee_joint_state', 'right_arm_joint_state', 'right_ee_joint_state']
    values = [[1.0]*6, [0.5], [2.0]*6, [0.8]]
    pixels = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
    observed = {'instruction': 'pick', 'state': dict(zip(keys, values)), 'vision': {
        camera: {'color': (pixels + index).tolist()}
        for index, camera in enumerate(['cam_head', 'cam_left_wrist', 'cam_right_wrist'])}}
    state = np.array(sum(values, []), dtype=np.float32)
    inputs = {'input': {'prompt': 'pick', 'state': encoded_record(state), 'images': {
        camera: encoded_record((pixels + index).transpose(2, 0, 1))
        for index, camera in enumerate(['cam_high', 'cam_left_wrist', 'cam_right_wrist'])}},
        'output': {'state': encoded_record(state)}}
    outputs = {'input': {'actions': encoded_record(state[None])},
               'output': {'actions': encoded_record(state[None])}}
    row = state.tolist()
    returned = [dict(zip(keys, [row[:6], row[6:7], row[7:13], row[13:14]]))]
    native = deepcopy([returned])
    if corruption == 'camera':
        inputs['input']['images']['cam_high'] = inputs['input']['images']['cam_left_wrist']
    elif corruption == 'state':
        inputs['input']['state'] = encoded_record(state[::-1])
    elif corruption == 'tensor':
        outputs['output']['actions']['sha256'] = '0'*64
    elif corruption == 'native':
        native[0][0]['left_ee_joint_state'][0] = 0
    elif corruption == 'rpc':
        returned[0]['right_ee_joint_state'][0] = 0
    if corruption:
        with pytest.raises(ValueError, match='differs|differ'):
            audit_trace([observed], inputs, outputs, native, returned)
    else:
        report = audit_trace([observed], inputs, outputs, native, returned)
        assert report['actions'] == 1 and not report['qualification']
