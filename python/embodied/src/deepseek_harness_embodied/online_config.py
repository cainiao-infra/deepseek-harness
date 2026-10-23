"""Validate deployment-varying online recovery controls before any inference."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .recovery import RecoveryBudget


def load_online_config(path: Path) -> dict:
    """Read explicit bounded simulator controls; invalid configuration fails at startup."""
    config = json.loads(path.read_text())
    if config.get('schema') != 'dsh-robodojo-online/v1':
        raise ValueError('Versioned online configuration required')
    if 'admission_policy' in config:
        from .admission import load_admission
        load_admission(config['admission_policy'])
    config.setdefault('recovery_enabled', True)
    if type(config['recovery_enabled']) is not bool:
        raise ValueError('Recovery enablement must be a boolean')
    config.setdefault('recover_skill_failures', False)
    if type(config['recover_skill_failures']) is not bool:
        raise ValueError('Skill failure recovery enablement must be a boolean')
    if config['recover_skill_failures'] and not config['recovery_enabled']:
        raise ValueError('Skill failure recovery requires online recovery')
    if 'initial_plan' in config and not isinstance(config['initial_plan'], dict):
        raise ValueError('Configured initial plan must be an object')
    for value in (config['evidence_root'], config['call_budget']['root']):
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('Absolute online evidence and shared budget paths required')
    limit = config['call_budget']['limit']
    if type(limit) is not int or limit < 0:
        raise ValueError('Nonnegative integer model call limit required')
    duration = config['episode_timeout_seconds']
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('Positive finite episode timeout required')
    rpc = config['rpc_timeout_seconds']
    if isinstance(rpc, bool) or not isinstance(rpc, (int, float)) or not math.isfinite(rpc) or rpc <= 0:
        raise ValueError('Positive finite RPC timeout required')
    RecoveryBudget(deadline=duration, **config['recovery'])
    if 'native_worker' in config:
        from .process_binding import resolve_worker
        worker = config['native_worker'] = resolve_worker(config['native_worker'])
        # The audited native client's drain/join each has a five-second bound.
        stop_bound = sum(worker[name] for name in ('request_timeout_seconds',
            'exit_timeout_seconds','kill_timeout_seconds','client_close_timeout_seconds'))+10
        if config['recovery']['stop_timeout_seconds'] < stop_bound:
            raise ValueError('Recovery stop timeout must cover native worker and client cleanup')
        start_bound = worker['startup_timeout_seconds']+worker['connect_timeout_seconds']+2*worker['request_timeout_seconds']
        if rpc <= start_bound or rpc <= stop_bound:
            raise ValueError('RPC timeout must cover native worker startup, binding and cleanup')
    events = config['events']
    if type(events['window']) is not int or events['window'] < 2:
        raise ValueError('Observed motion window must be at least two')
    for name in ('motion_epsilon','command_epsilon'):
        value = events[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('Positive finite observed motion thresholds required')
    guards = config['restart_guards']
    if not isinstance(guards, dict) or (config['recovery_enabled'] and not guards):
        raise ValueError('Reviewed capability restart guards required')
    for handle, rows in [*guards.items(), ('observed motion', events['motion_guards'])]:
        if not isinstance(handle, str) or not isinstance(rows, list) or not rows:
            raise ValueError('Nonempty capability guard list required')
        for row in rows:
            if not isinstance(row['path'], list) or not row['path'] or any(
                    not isinstance(part, str) or not part for part in row['path']):
                raise ValueError('Nonempty observation field path required')
            if any(isinstance(row[name], bool) or not isinstance(row[name], (int, float))
                   or not math.isfinite(row[name]) for name in ('minimum','maximum')):
                raise ValueError('Finite restart guard bounds required')
            if row['minimum'] > row['maximum']:
                raise ValueError('Restart guard minimum exceeds maximum')
    return config
