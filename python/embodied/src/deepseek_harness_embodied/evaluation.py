"""Common request controls and provider accounting for development comparisons.

Pilot records never qualify statistical acceptance. Missing provider usage is
unknown, including a failed or interrupted response, rather than zero cost.
"""
from __future__ import annotations

import json


def provider_request(payload: dict, *, model: str, max_tokens: int) -> dict:
    """Freeze model/output controls and disable thinking identically in every arm.

    Native requests omit the DeepSeek thinking option; the pilot explicitly
    records its insertion. Other request fields, messages and images are retained.
    Conflicting controls or tool use reject the request before provider inference.
    """
    if (payload.get('model') != model or payload.get('max_tokens') != max_tokens
            or payload.get('temperature') != 0
            or payload.get('response_format') != {'type': 'json_object'}
            or payload.get('tools') or payload.get('tool_choice')
            or payload.get('reasoning_effort') is not None
            or payload.get('thinking', {'type': 'disabled'}) != {'type': 'disabled'}):
        raise ValueError('Pilot request conflicts with frozen provider controls')
    if not isinstance(payload.get('messages'), list) or not payload['messages']:
        raise ValueError('Nonempty provider messages required')
    return dict(payload, thinking={'type': 'disabled'})


def response_usage(body: bytes, *, streaming: bool) -> dict | None:
    """Read final JSON/SSE usage; malformed or incomplete accounting stays unknown."""
    try:
        if not streaming:
            packets = [json.loads(body)]
        else:
            packets = [json.loads(line[6:]) for line in body.decode().splitlines()
                       if line.startswith('data: ') and line != 'data: [DONE]']
        rows = [p['usage'] for p in packets if isinstance(p, dict) and p.get('usage') is not None]
        if not rows:
            return None
        usage = rows[-1]
        if (not isinstance(usage, dict)
                or any(type(usage.get(key)) is not int or usage[key] < 0
                       for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
                or usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']):
            return None
        return usage
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return None
