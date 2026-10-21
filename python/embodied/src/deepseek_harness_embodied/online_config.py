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
    events = config['events']
    if type(events['window']) is not int or events['window'] < 2:
        raise ValueError('Observed motion window must be at least two')
    for name in ('motion_epsilon','command_epsilon'):
        value = events[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('Positive finite observed motion thresholds required')
    guards = config['restart_guards']
    if not isinstance(guards, dict) or not guards:
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
