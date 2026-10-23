"""Explicit native binding probe; generated actions are retained and never dispatched."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .bridge import digest, write_record
from .capability import CapabilitySpec
from .native_binding import NativeBinding
from .robodojo import action_record


def probe_binding(*, spec: CapabilitySpec, observation: dict, control: dict,
                  predicates: dict, factory: Callable[[], NativeBinding], output: Path,
                  inspect: Callable[[NativeBinding], dict] | None = None) -> dict:
    """Check input before creating a worker; retain inference and cleanup independently.

The factory must return the existing explicit-plan NativeBinding provider.
The caller supplies reviewed control metadata and observable start predicates.
No generated action leaves this probe. Passing it cannot verify a task effect.
An inspection callback retains provider diagnostics after binding; omission
records no provider preprocessing or dependency evidence.
    """
    output.mkdir(parents=True, exist_ok=False)
    write_record(output/'capability.json', spec.document)
    write_record(output/'input.json', {'observation': action_record(observation),
                 'control': control, 'predicates': predicates})
    issues = spec.check_input(observation, control, predicates)
    result = {'schema': 'dsh-binding-probe/v1', 'spec_revision': spec.revision,
              'qualification': False, 'robot_actions_dispatched': 0,
              'binding_succeeded': False, 'inference_succeeded': False,
              'cleanup_confirmed': None, 'issues': issues, 'error': None, 'cleanup_error': None}
    binding = None
    try:
        if not issues:
            binding = factory()
            write_record(output/'metadata.json', binding.metadata)
            if digest(binding.metadata['catalog']) != spec.document['catalog_sha256']:
                raise ValueError('Native catalog differs from capability descriptor')
            plan = {**spec.document['selection'], 'rationale': 'Explicit execution qualification binding.'}
            write_record(output/'plan.json', plan)
            applied = binding.bind(plan, observation)
            write_record(output/'binding.json', applied)
            if any(applied['skill'].get(key) != value for key, value in spec.document['skill'].items()):
                raise ValueError('Native skill revision differs from capability descriptor')
            result['binding_succeeded'] = True
            if inspect is not None:
                write_record(output/'policy-diagnostics.json', inspect(binding))
            actions = binding.actions()
            if not isinstance(actions, list) or not actions:
                raise ValueError('Native binding returned no action chunk')
            write_record(output/'actions.json', [action_record(action) for action in actions])
            result['inference_succeeded'] = True
            result['generated_actions'] = len(actions)
    except Exception as error:
        result['error'] = {'type': type(error).__name__, 'message': str(error)}
    finally:
        if binding is not None:
            try:
                binding.close()
                result['cleanup_confirmed'] = True
            except Exception as error:
                result['cleanup_confirmed'] = False
                result['cleanup_error'] = {'type': type(error).__name__, 'message': str(error)}
        result['passed'] = (result['inference_succeeded'] and result['cleanup_confirmed'] is True)
        write_record(output/'result.json', result)
    return result
