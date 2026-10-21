"""Frozen-input DeepSeek comparisons; planner validity never implies robot success.

Every assigned request is retained, including HTTP errors and incomplete output.
The oracle checks the inspected single-capability pickup protocol only. It does
not score rationale quality, infer physical truth, or execute returned plans.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import tarfile
import time
import urllib.error
import urllib.request

from .bridge import digest, write_record


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a provider credential through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def pickup_oracle(messages: list) -> dict:
    """Freeze the one registered capability and phase without consulting responses."""
    contexts = []
    for block in messages[-1]['content']:
        if block.get('type') == 'text':
            try:
                contexts.append(json.loads(block['text']))
            except json.JSONDecodeError:
                pass  # Native instructions can include plain text before their JSON context.
    domain = next(c for c in contexts if 'available_compositions' in c)
    compositions = domain['available_compositions']
    operations = domain['operation_program']['operations']
    if (list(compositions) != ['memory-guided-program-library']
            or list(operations) != ['instruction-grounded-pickup']):
        raise ValueError('The diagnostic oracle admits only the frozen single-capability pickup catalog')
    phase = next((c['phase'] for c in contexts if 'phase' in c), None)
    if phase not in (None, 'initial', 'recovery'):
        raise ValueError('Unsupported planning phase')
    return {'composition': 'memory-guided-program-library',
            'operations': ['instruction-grounded-pickup'],
            'execution': None if phase is None else ('bind' if phase == 'initial' else 'restart')}


def assess(content: str, finish_reason: str | None, oracle: dict) -> dict:
    """Check native field types and supported phase choices, rejecting truncation."""
    result = {'complete': finish_reason == 'stop', 'valid_plan': False,
              'decision_matches': False, 'category': 'incomplete', 'plan': None}
    if not result['complete']:
        return result
    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        return dict(result, category='invalid_json')
    expected = {'composition', 'operations', 'rationale'}
    if oracle['execution'] is not None:
        expected.add('execution')
    if (not isinstance(plan, dict) or set(plan) != expected
            or not isinstance(plan.get('rationale'), str) or not plan['rationale'].strip()
            or plan.get('composition') != oracle['composition']
            or not isinstance(plan.get('operations'), list)
            or plan['operations'] != oracle['operations']
            or ('execution' in expected and (not isinstance(plan['execution'], str)
                or plan['execution'] not in ({'bind', 'abort'} if oracle['execution'] == 'bind'
                                            else {'restart', 'switch', 'abort'})))):
        return dict(result, category='invalid_plan', plan=plan)
    matches = oracle['execution'] is None or plan['execution'] == oracle['execution']
    return dict(result, valid_plan=True, decision_matches=matches,
                category='matched' if matches else 'different_execution', plan=plan)


def payload_for(source: dict, *, thinking: bool, cap: int) -> dict:
    """Keep all model-visible input unchanged and normalize both arms' transport."""
    if source.get('model') != 'deepseek-flash' or source.get('tools'):
        raise ValueError('Only tool-free deepseek-flash diagnostic inputs are admitted')
    payload = deepcopy(source)
    payload.pop('stream_options', None)
    payload.pop('reasoning_effort', None)
    payload.update(stream=False, max_tokens=cap,
                   thinking={'type': 'enabled' if thinking else 'disabled'})
    if thinking:
        payload['reasoning_effort'] = 'high'
    return payload


def clarify_online(source: dict) -> dict:
    """Mark fresh camera availability while retaining every frozen image and historical turn."""
    from .online_model import OBSERVATION_TIMING_GUIDANCE
    result = deepcopy(source)
    content = result['messages'][-1]['content']
    contexts = [json.loads(block['text']) for block in content if block['type'] == 'text']
    domain = next(c for c in contexts if 'observation' in c)
    control = next(c for c in contexts if 'phase' in c)
    vision = domain['observation'].get('vision', {})
    cameras = sorted(name for name, camera in vision.items() if camera.get('color') is not None)
    control['current_observation_status'] = {
        'rgb_available': bool(vision) and len(cameras) == len(vision), 'rgb_cameras': cameras}
    for block in content:
        if block['type'] == 'text' and 'phase' in json.loads(block['text']):
            block['text'] = json.dumps(control, allow_nan=False)
    result['messages'][0]['content'] += OBSERVATION_TIMING_GUIDANCE
    return result


def prepare(archive: Path, expected_sha256: str, output: Path, *, repeats: int,
            caps: list[int], seed: int, timeout: float, workers: int,
            online_variants: bool = False) -> dict:
    """Create a new immutable protocol and input copies before any provider request."""
    if (type(repeats) is not int or repeats < 1 or not caps or len(caps) != len(set(caps))
            or any(type(cap) is not int or not 1 <= cap <= 393216 for cap in caps)
            or not 0 < timeout <= 3600 or type(workers) is not int or not 1 <= workers <= 8):
        raise ValueError('Positive repetitions, unique token caps, bounded timeout/workers required')
    checksum = hashlib.sha256()
    with archive.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    actual = checksum.hexdigest()
    if actual != expected_sha256:
        raise ValueError('Source archive SHA-256 mismatch')
    sources = []
    with tarfile.open(archive) as bundle:
        for member in sorted(bundle.getmembers(), key=lambda m: m.name):
            if '/provider/http-' not in member.name or not member.name.endswith('/sent.json'):
                continue
            if not member.isfile() or member.size > 32 * 1024 * 1024:
                raise ValueError('Source request must be a bounded regular archive member')
            source = json.load(bundle.extractfile(member))
            payload_for(source, thinking=False, cap=caps[0])
            oracle = pickup_oracle(source['messages'])
            if online_variants and oracle['execution'] is None:
                continue
            for variant in (['original', 'clarified'] if online_variants else ['original']):
                request = clarify_online(source) if variant == 'clarified' else source
                sources.append({'id': f'input-{len(sources):03d}', 'member': member.name,
                                'variant': variant, 'source_input_digest': digest(source['messages']),
                                'input_digest': digest(request['messages']), 'request': request, 'oracle': oracle})
    if not sources:
        raise ValueError('Source archive contains no frozen requests')
    output.mkdir(parents=True, exist_ok=False)
    for source in sources:
        write_record(output/'inputs'/(source['id']+'.json'), source)
    rng = random.Random(seed)
    pairs = [(source['id'], cap, repeat) for source in sources for cap in caps for repeat in range(repeats)]
    rng.shuffle(pairs)
    assignments = []
    for pair_index, (input_id, cap, repeat) in enumerate(pairs):
        arms = [False, True]
        rng.shuffle(arms)
        for thinking in arms:
            assignments.append({'id': f'call-{len(assignments):04d}', 'pair': pair_index,
                                'input_id': input_id, 'cap': cap, 'repeat': repeat, 'thinking': thinking})
    protocol = {'schema': 'dsh-thinking-comparison/v1', 'qualification': False,
                'scope': 'frozen-input planner diagnostic; no robot execution',
                'source_archive_sha256': actual, 'seed': seed, 'repeats': repeats, 'caps': caps,
                'online_variants': online_variants,
                'workers': workers, 'timeout_seconds': timeout, 'max_response_bytes': 16 * 1024 * 1024,
                'temperature_note': 'Both retain temperature=0; provider ignores temperature when thinking.',
                'transport': 'Direct official Chat Completions, nonstreaming in both arms, no retries',
                'oracle': 'single-capability pickup protocol; bind initially, restart at supplied recovery',
                'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'inputs': [{k: v for k, v in source.items() if k != 'request'} for source in sources],
                'assignments': assignments}
    write_record(output/'protocol.json', protocol)
    return protocol


def execute(output: Path, assignment: dict, protocol: dict, key: str, *, opener=None) -> dict:
    """Send one preassigned request once; persist partial/unknown outcomes without refund."""
    source = json.loads((output/'inputs'/(assignment['input_id']+'.json')).read_text())
    if digest(source['request']['messages']) != source['input_digest']:
        raise ValueError('Frozen input changed before inference')
    payload = payload_for(source['request'], thinking=assignment['thinking'], cap=assignment['cap'])
    root = output/'calls'/assignment['id']
    write_record(root/'assignment.json', assignment)
    write_record(root/'sent.json', payload)
    started, captured = time.monotonic(), bytearray()
    row = dict(assignment, status='unknown', usage=None, assessment=None,
               input_digest=source['input_digest'], http_status=None)
    request = urllib.request.Request('https://api.deepseek.com/chat/completions',
        data=json.dumps(payload, allow_nan=False).encode(),
        headers={'Authorization': 'Bearer '+key, 'Content-Type': 'application/json'})
    client = opener or urllib.request.build_opener(NoRedirect())
    try:
        with client.open(request, timeout=protocol['timeout_seconds']) as response:
            row['http_status'] = response.status
            while True:
                if time.monotonic()-started > protocol['timeout_seconds']:
                    raise TimeoutError('Whole provider deadline exceeded')
                chunk = response.read1(65536)
                if not chunk:
                    break
                captured.extend(chunk)
                if len(captured) > protocol['max_response_bytes']:
                    raise ValueError('Response exceeds diagnostic byte limit')
        packet = json.loads(captured)
        choice = packet['choices'][0]
        row.update(status='completed', usage=packet.get('usage'), model=packet.get('model'),
                   system_fingerprint=packet.get('system_fingerprint'),
                   reasoning_characters=len(choice['message'].get('reasoning_content') or ''),
                   assessment=assess(choice['message'].get('content') or '', choice.get('finish_reason'), source['oracle']))
    except Exception as error:
        row['error'] = {'type': type(error).__name__}
        if isinstance(error, urllib.error.HTTPError):
            row['http_status'] = error.code
    finally:
        (root/'response.bin').write_bytes(captured)
        row.update(elapsed_seconds=time.monotonic()-started,
                   response_sha256=hashlib.sha256(captured).hexdigest())
        write_record(root/'receipt.json', row)
    return row


def summarize(output: Path) -> dict:
    """Keep missing assignments in denominators and report only diagnostic counts."""
    protocol = json.loads((output/'protocol.json').read_text())
    inputs = {source['id']: source for source in protocol['inputs']}
    groups = {}
    for assignment in protocol['assignments']:
        key = f"{assignment['cap']}-{'on' if assignment['thinking'] else 'off'}"
        if protocol.get('online_variants'):
            key += '-'+inputs[assignment['input_id']]['variant']
        group = groups.setdefault(key, {'assigned': 0, 'completed': 0, 'valid': 0, 'matched': 0,
                                       'unknown': 0, 'incomplete': 0, 'tokens': 0, 'unknown_usage': 0,
                                       'seconds': [], 'reasoning_characters': 0})
        group['assigned'] += 1
        receipt = output/'calls'/assignment['id']/'receipt.json'
        if not receipt.exists():
            group['unknown'] += 1
            group['unknown_usage'] += 1
            continue
        row = json.loads(receipt.read_text())
        if any(row.get(k) != v for k, v in assignment.items()):
            raise ValueError('Receipt does not match its frozen assignment')
        group['completed'] += row['status'] == 'completed'
        group['unknown'] += row['status'] != 'completed'
        assessment = row.get('assessment') or {}
        group['valid'] += bool(assessment.get('valid_plan'))
        group['matched'] += bool(assessment.get('decision_matches'))
        group['incomplete'] += assessment.get('category') == 'incomplete'
        group['reasoning_characters'] += row.get('reasoning_characters', 0)
        if isinstance(row.get('usage', {}).get('total_tokens') if row.get('usage') else None, int):
            group['tokens'] += row['usage']['total_tokens']
        else:
            group['unknown_usage'] += 1
        group['seconds'].append(row['elapsed_seconds'])
    return {'qualification': False, 'physical_success_measured': False,
            'frozen_message_inputs': len({i['input_digest'] for i in protocol['inputs']}),
            'source_snapshots': len({i.get('source_input_digest', i['input_digest']) for i in protocol['inputs']}),
            'groups': groups}
