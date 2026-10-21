"""Audit frozen requests and raw provider responses before reporting diagnostic counts."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics

from deepseek_harness_embodied.bridge import digest
from deepseek_harness_embodied.thinking_comparison import assess, payload_for


def audit(root: Path) -> dict:
    """Reject modified evidence; repeated frozen requests do not measure robot success."""
    protocol = json.loads((root/'protocol.json').read_text())
    sources = {}
    for source in protocol['inputs']:
        retained = json.loads((root/'inputs'/(source['id']+'.json')).read_text())
        if (any(retained.get(k) != v for k, v in source.items())
                or digest(retained['request']['messages']) != source['input_digest']):
            raise ValueError('Frozen source input changed')
        sources[source['id']] = retained
    groups, rows = defaultdict(list), {}
    expected = {a['id'] for a in protocol['assignments']}
    actual = {p.name for p in (root/'calls').iterdir() if p.is_dir()}
    if actual != expected:
        raise ValueError('Every assigned call must have exactly one retained directory')
    hashes = {}
    for assignment in protocol['assignments']:
        source = sources[assignment['input_id']]
        directory = root/'calls'/assignment['id']
        row = json.loads((directory/'receipt.json').read_text())
        raw = (directory/'response.bin').read_bytes()
        sent = json.loads((directory/'sent.json').read_text())
        if (json.loads((directory/'assignment.json').read_text()) != assignment
                or any(row.get(k) != v for k, v in assignment.items())
                or row['input_digest'] != source['input_digest']
                or row['response_sha256'] != hashlib.sha256(raw).hexdigest()
                or sent != payload_for(source['request'], thinking=assignment['thinking'], cap=assignment['cap'])):
            raise ValueError('Request or response differs from its frozen assignment')
        if row['status'] == 'completed':
            packet = json.loads(raw)
            choice = packet['choices'][0]
            expected_assessment = assess(choice['message'].get('content') or '', choice.get('finish_reason'), source['oracle'])
            if (row['assessment'] != expected_assessment or row['usage'] != packet.get('usage')
                    or row['http_status'] != 200 or packet.get('model') != 'deepseek-flash'
                    or row['reasoning_characters'] != len(choice['message'].get('reasoning_content') or '')):
                raise ValueError('Receipt disagrees with retained provider response')
        elif row['status'] != 'unknown' or row.get('assessment') is not None:
            raise ValueError('Unrecognized provider outcome')
        variant = source.get('variant', 'original')
        prompt = 'online' if source['oracle']['execution'] else 'native'
        group_key = f"{assignment['cap']}-{'on' if assignment['thinking'] else 'off'}-{variant}"
        groups[group_key].append((row, prompt))
        rows[assignment['id']] = row
        for p in directory.iterdir():
            if p.is_file():
                hashes[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    summaries = {}
    for key, group in groups.items():
        records = [r for r, _ in group]
        times = sorted(r['elapsed_seconds'] for r in records)
        usage_complete = all(r.get('usage') and type(r['usage'].get('total_tokens')) is int
            and r['usage'].get('prompt_tokens', -1) + r['usage'].get('completion_tokens', -1) == r['usage']['total_tokens']
            for r in records)
        summaries[key] = {
            'assigned': len(records),
            'categories': dict(Counter((r.get('assessment') or {}).get('category', 'unknown') for r in records)),
            'median_seconds': statistics.median(times), 'p95_seconds': times[math.ceil(len(times)*.95)-1],
            'usage_complete': usage_complete,
            'prompt_tokens': sum(r['usage']['prompt_tokens'] for r in records) if usage_complete else None,
            'completion_tokens': sum(r['usage']['completion_tokens'] for r in records) if usage_complete else None,
            'reasoning_tokens': sum(r['usage'].get('completion_tokens_details', {}).get('reasoning_tokens', 0)
                                    for r in records) if usage_complete else None,
            'reasoning_observed_requests': sum(r.get('reasoning_characters', 0) > 0 for r in records),
            'prompts': {prompt: {'assigned': sum(p == prompt for _, p in group),
                'matched': sum(bool((r.get('assessment') or {}).get('decision_matches')) for r, p in group if p == prompt)}
                       for prompt in {p for _, p in group}},
        }
    paired = defaultdict(Counter)
    for index in range(0, len(protocol['assignments']), 2):
        assignments = protocol['assignments'][index:index+2]
        if (len(assignments) != 2 or len({a['pair'] for a in assignments}) != 1
                or len({(a['input_id'], a['cap'], a['repeat']) for a in assignments}) != 1
                or {a['thinking'] for a in assignments} != {True, False}):
            raise ValueError('Incomplete or mismatched Thinking pair')
        arm = {a['thinking']: bool((rows[a['id']].get('assessment') or {}).get('decision_matches')) for a in assignments}
        variant = sources[assignments[0]['input_id']].get('variant', 'original')
        paired[f"{assignments[0]['cap']}-{variant}"][f'on={arm[True]},off={arm[False]}'] += 1
    return {'qualification': False, 'physical_success_measured': False,
            'source_snapshots': len({s.get('source_input_digest', s['input_digest']) for s in sources.values()}),
            'assigned_calls': len(rows), 'groups': summaries, 'paired_outcomes': dict(paired),
            'models': dict(Counter(r.get('model') for r in rows.values())),
            'system_fingerprints': dict(Counter(r.get('system_fingerprint') for r in rows.values())),
            'evidence_sha256': hashes}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.root), ensure_ascii=False, indent=2))
