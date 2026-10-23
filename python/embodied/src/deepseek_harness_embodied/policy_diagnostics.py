"""Read-only learned-policy diagnostics inside the actual native worker.

This opt-in probe retains pre-OpenPI encoded observations and loaded source
identities. It does not execute normalization again, advance memory, infer an
action, or establish checkpoint training/task compatibility.
"""
from __future__ import annotations

import hashlib
from importlib import metadata
from pathlib import Path
import sys


def encoded_record(value):
    """Retain numeric state and image tensor identities without copying image pixels."""
    if isinstance(value, dict):
        return {key: encoded_record(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encoded_record(item) for item in value]
    if hasattr(value, 'dtype') and hasattr(value, 'tobytes'):
        result = {'shape': list(value.shape), 'dtype': str(value.dtype),
                  'sha256': hashlib.sha256(value.tobytes()).hexdigest()}
        if value.ndim <= 2:
            result['values'] = value.tolist()
        return result
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError('Unsupported encoded observation value')


def inspect_policy(model) -> dict:
    """Capture the selected native implementation after binding, without another observe."""
    if set(model.sessions) != {0}:
        raise ValueError('Policy diagnostics require one bound environment')
    skill = model.sessions[0]['skill']
    implementation = skill.implementation
    sources = {}
    for name, module in sorted(tuple(sys.modules.items())):
        if not name.startswith(('PhysicalRSI_baselines.', 'PhysicalRSI_core.', 'XPolicyLab.', 'openpi.')):
            continue
        filename = getattr(module, '__file__', None)
        if filename is not None and Path(filename).suffix == '.py':
            path = Path(filename)
            sources[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    versions = {}
    for name in ('numpy', 'jax', 'jaxlib', 'torch', 'flax', 'orbax-checkpoint', 'lerobot', 'msgpack-numpy'):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {'schema': 'dsh-policy-diagnostics/v1', 'skill': skill.describe(),
            'implementation_module': type(implementation).__module__, 'python': sys.version,
            'versions': versions, 'loaded_sources': sources,
            'encoded_observation': encoded_record(getattr(implementation, 'observation_window', None)),
            'scope': 'Encoded inputs before OpenPI transforms; no normalization replay or task qualification.'}


def diagnostic_model_type(base, output: Path):
    """Instrument one native episode without extra observations or inference calls.

The opt-in runner retains raw RGB/state, the actual first input/output transforms
and converted commands. Recording errors stop execution. Reset cannot overwrite
an earlier trace; this wrapper supports exactly one episode per process.
    """
    from .bridge import write_record
    from .robodojo import action_record

    class DiagnosticModel(base):
        def __init__(self, *args, **kwargs):
            self._diagnostic_bound = False
            self._diagnostic_actions = False
            super().__init__(*args, **kwargs)

        def update_obs_batch(self, observations):
            result = super().update_obs_batch(observations)
            if not self._diagnostic_bound:
                write_record(output/'raw-observation.json', action_record(observations))
                write_record(output/'policy.json', inspect_policy(self))
                implementation = self.sessions[0]['skill'].implementation
                policy = getattr(implementation, 'policy', None)
                if policy is not None:
                    for name in ('_input_transform', '_output_transform'):
                        original = getattr(policy, name)
                        setattr(policy, name, capture_transform(original, output/(name+'.json')))
                self._diagnostic_bound = True
            return result

        def get_action_batch(self, *args, **kwargs):
            result = super().get_action_batch(*args, **kwargs)
            if not self._diagnostic_actions:
                write_record(output/'native-actions.json', action_record(result))
                self._diagnostic_actions = True
            return result

    return DiagnosticModel


def capture_transform(transform, output: Path):
    """Record the first actual transform input/output and return its original value."""
    from .bridge import write_record
    captured = False

    def apply(value):
        nonlocal captured
        before = None if captured else encoded_record(value)
        result = transform(value)
        if not captured:
            write_record(output, {'input': before, 'output': encoded_record(result)})
            captured = True
        return result

    return apply


def audit_trace(observations: list, inputs: dict, outputs: dict,
                native_actions: list, returned_actions: list) -> dict:
    """Check recorded ARX-X5 camera/state ordering and command conversion.

This offline check compares actual transform inputs and outputs with their
adjacent records. It does not validate training statistics, control cadence,
physical execution or task effects. Altered tensors and commands are rejected.
    """
    import numpy as np

    if len(observations) != 1:
        raise ValueError('One observed environment required')
    observation = observations[0]
    actual = inputs['input']
    for source, target in (('cam_head', 'cam_high'), ('cam_left_wrist', 'cam_left_wrist'),
                           ('cam_right_wrist', 'cam_right_wrist')):
        pixels = np.asarray(observation['vision'][source]['color'])
        if (pixels.ndim != 3 or pixels.shape[-1] != 3 or pixels.size == 0
                or pixels.dtype.kind not in 'iu' or pixels.min() < 0 or pixels.max() > 255):
            raise ValueError('Recorded RGB must contain HWC uint8 values')
        expected = encoded_record(pixels.astype(np.uint8).transpose(2, 0, 1))
        if actual['images'][target] != expected:
            raise ValueError(f'Camera encoding differs: {source}')
    keys = ('left_arm_joint_state', 'left_ee_joint_state',
            'right_arm_joint_state', 'right_ee_joint_state')
    state = observation['state']
    if [len(state[key]) for key in keys] != [6, 1, 6, 1]:
        raise ValueError('ARX-X5 requires six joints and one gripper per arm')
    expected_state = np.asarray([value for key in keys for value in state[key]], dtype=np.float32)
    if (not np.isfinite(expected_state).all() or actual['state'] != encoded_record(expected_state)
            or actual['prompt'] != observation['instruction']):
        raise ValueError('State ordering or instruction differs')
    for record in (inputs['output']['state'], outputs['input']['actions'], outputs['output']['actions']):
        tensor = np.asarray(record['values'], dtype=record['dtype'])
        if not np.isfinite(tensor).all() or encoded_record(tensor) != record:
            raise ValueError('Transformed tensor digest, dimensions or values differ')
    actions = outputs['output']['actions']['values']
    if not actions or any(len(row) != 14 for row in actions):
        raise ValueError('Nonempty ARX-X5 action chunk required')
    expected_actions = [dict(zip(keys, (row[:6], row[6:7], row[7:13], row[13:14]))) for row in actions]
    if native_actions != [expected_actions] or returned_actions != expected_actions:
        raise ValueError('Native or RPC action conversion differs')
    return {'schema': 'dsh-policy-trace-audit/v1', 'camera_encoding': True,
            'state_ordering': True, 'tensor_integrity': True, 'action_conversion': True,
            'actions': len(actions), 'qualification': False}
