"""Recompute pilot wire controls and usage from retained provider bytes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .evaluation import provider_request, response_usage


def audit_provider(directory: Path, spec: dict) -> list[dict]:
    """Reject inconsistent completed receipts; preserve incomplete attempts as unknown.

    Content digests identify the raw files independently of launcher summaries.
    A failed attempt may have only partial files and never becomes a completed
    request because its partial response happens to contain usage.
    """
    rows=[]
    for path in sorted(directory.glob('http-*')):
        files={name:(path/name).read_bytes() for name in
               ('received.json','sent.json','response.bin','receipt.json') if (path/name).is_file()}
        row={'directory':path.name,'file_sha256':{name:hashlib.sha256(data).hexdigest()
                                               for name,data in files.items()},'complete':False}
        if 'receipt.json' not in files:
            rows.append(row)
            continue
        receipt=json.loads(files['receipt.json'])
        completed=receipt['status']=='completed'
        if completed and len(files)!=4:
            raise ValueError('Completed provider receipt lacks its raw request or response')
        if 'received.json' in files and 'sent.json' in files:
            received,sent=(json.loads(files[name]) for name in ('received.json','sent.json'))
            expected=provider_request(received,model=spec['model'],max_tokens=spec['max_tokens'],
                                      reasoning=spec.get('deepseek_reasoning','off'))
            if sent != expected:
                raise ValueError('Sent provider payload differs from frozen controls or received inputs')
        if 'response.bin' in files:
            if row['file_sha256']['response.bin'] != receipt['response_sha256']:
                raise ValueError('Provider response bytes differ from their receipt digest')
        if completed:
            sent=json.loads(files['sent.json'])
            usage=response_usage(files['response.bin'],streaming=bool(sent.get('stream')))
            if receipt.get('http_status')!=200 or receipt.get('usage')!=usage:
                raise ValueError('Completed provider receipt disagrees with HTTP status or raw usage')
            row.update(complete=True,usage=usage)
        rows.append(row)
    return rows
