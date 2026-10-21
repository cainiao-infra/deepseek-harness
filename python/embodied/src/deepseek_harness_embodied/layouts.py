"""Pin a native SeedManager layout without altering scene content or model inputs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from .bridge import write_record


def select_layout(manager, *, layout_id: int, expected_sha256: str, evidence: Path,
                  fresh_source: dict | None = None) -> dict:
    """Select an eligible native layout index only after verifying its frozen file.

    Call after the native init_eval has applied resume exclusions. Native layout
    indices refer to its numerically sorted files, not to the CLI directory seed.
    Failed identity checks leave the native selection unchanged.
    """
    if type(layout_id) is not int or layout_id < 0 or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ValueError('Nonnegative native layout index and lowercase SHA-256 required')
    if layout_id not in manager.seed_list:
        raise ValueError('Selected layout is missing or excluded by native resume state')
    path = Path(manager.seed_info[layout_id]['scene_layout'])
    data = path.read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    if checksum != expected_sha256:
        raise ValueError('Native layout file differs from the frozen case')
    snapshot = evidence.with_suffix('.snapshot.json')
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    with snapshot.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    record = {'schema':'dsh-native-layout-selection/v1','qualification':False,
              'native_layout_id':layout_id,'layout_path':str(path),'layout_sha256':checksum,
              'runtime_snapshot':str(snapshot)}
    if fresh_source is not None:
        record['fresh_source'] = fresh_source
    write_record(evidence, record)
    manager.seed_info = dict(manager.seed_info)
    manager.seed_info[layout_id] = dict(manager.seed_info[layout_id], scene_layout=str(snapshot))
    def load_snapshot(seed):
        if type(seed) is not int or seed != layout_id:
            raise ValueError('Native reset requested a layout outside the frozen case')
        retained = snapshot.read_bytes()
        if hashlib.sha256(retained).hexdigest() != checksum:
            raise ValueError('Runtime layout snapshot changed before reset')
        return json.loads(retained)
    manager.get_seed_scene_info = load_snapshot
    manager.seed_list = [layout_id]
    manager.st_idx, manager.ed_idx, manager.idx = 0, 1, 0
    manager._current_batch_seeds = None
    return record


def bind_fresh_case(seed_manager_class, *, manifest_path: Path, task: str, index: int,
                    expected_sha256: str, expected_manifest_sha256: str,
                    split: str, comparison_sha256: str | None):
    """Use PhysicalRSI's verified fresh cohort; never fall back to benchmark layouts.

    The returned class presents the one selected cohort row at native index zero.
    Metadata identifies its original task/index and manifest; callers retain it
    with select_layout's exact runtime snapshot, outside model-visible inputs.
    """
    if type(index) is not int or index < 0:
        raise ValueError('Nonnegative fresh cohort index required')
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if manifest_digest != expected_manifest_sha256:
        raise ValueError('Fresh cohort manifest differs from the frozen case')
    from .layout_cohort import verify_cohort
    from PhysicalRSI_baselines.robodojo.native_layouts import bind_layouts
    manifest = verify_cohort(manifest_path, split=split, comparison_sha256=comparison_sha256)
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != manifest_digest:
        raise ValueError('Fresh cohort manifest changed during verification')
    rows = [row for row in manifest['entries'] if row['task'] == task and row['index'] == index]
    if len(rows) != 1 or rows[0]['sha256'] != expected_sha256:
        raise ValueError('Fresh case identity differs from its verified cohort')
    row = rows[0]
    bound = bind_layouts(seed_manager_class, task=task, split=split, root=manifest_path.parent,
        rows=[{'task':task,'split':split,'file':row['file'],'layout_sha256':row['content_sha256']}])
    return bound, {'manifest_path':str(manifest_path),
                   'manifest_sha256':manifest_digest,
                   'task':task,'index':index,'split':split,'content_sha256':row['content_sha256']}
