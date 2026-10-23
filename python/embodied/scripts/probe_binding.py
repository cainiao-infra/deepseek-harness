"""Probe a pinned native skill with recorded observations; never dispatch actions."""
import argparse
import json
from pathlib import Path

from deepseek_harness_embodied.binding_probe import probe_binding
from deepseek_harness_embodied.capability import CapabilitySpec, read_evidence
from deepseek_harness_embodied.process_binding import ProcessBinding, resolve_worker


def main() -> None:
    """Resolve explicit deployment and frozen inputs before loading native policies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.config.parent
    config = json.loads(args.config.read_text())
    if config.get('schema') != 'dsh-binding-probe-config/v1':
        raise ValueError('Versioned binding probe configuration required')
    spec = CapabilitySpec.parse(json.loads(read_evidence(root, config['capability'])))
    snapshot = json.loads(read_evidence(root, config['input']))
    frozen = json.loads(read_evidence(root, config['skills']))
    settings = resolve_worker(config['worker'])
    # Recorded JSON arrays become numeric sensor arrays through the native wire codec.
    import numpy as np
    observation = snapshot['observation']
    for camera in observation.get('vision', {}).values():
        if camera.get('color') is not None:
            image = np.asarray(camera['color'])
            if (image.ndim != 3 or image.shape[-1] != 3 or image.dtype.kind not in 'iu'
                    or image.size == 0 or image.min() < 0 or image.max() > 255):
                raise ValueError('Recorded cameras must contain HWC uint8 RGB values')
            camera['color'] = image.astype(np.uint8)
    for key, value in observation.get('state', {}).items():
        state = np.asarray(value)
        if state.ndim != 1 or state.size == 0 or state.dtype.kind not in 'fiu' or not np.isfinite(state).all():
            raise ValueError('Recorded robot state must contain finite numeric vectors')
        observation['state'][key] = state.astype(np.float32)
    result = probe_binding(spec=spec, observation=observation, control=snapshot['control'],
        predicates=snapshot['predicates'], output=args.output,
        factory=lambda: ProcessBinding(config['deployment'], frozen, settings, args.output/'workers'),
        inspect=lambda binding: binding.diagnostics())
    print(json.dumps(result))
    if not result['passed']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
