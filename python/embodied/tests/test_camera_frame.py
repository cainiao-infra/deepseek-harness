"""Incomplete renderer frames cannot reach the camera's image-copy kernel."""
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('annotator,channels', [('rgb', 4), ('depth', 1), ('segmentation', 1)])
@pytest.mark.parametrize('size_delta', [-24, -1, 0, 1])
def test_camera_patch_rejects_incomplete_frames_before_allocation_or_launch(tmp_path, annotator, channels, size_delta):
    root = Path(__file__).parents[1]
    source = tmp_path/'env/camera_manager/capture/camera_view.py'
    source.parent.mkdir(parents=True)
    source.write_bytes((root/'tests/fixtures/camera_get_data.before.py').read_bytes())
    subprocess.run(['git', 'apply', '--check', str(root/'integrations/robodojo-camera-frame.patch')],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(['git', 'apply', str(root/'integrations/robodojo-camera-frame.patch')],
                   cwd=tmp_path, check=True, capture_output=True)
    calls = []
    size = max(0, 6*channels + size_delta)
    buffer = SimpleNamespace(size=size, device='cpu', flatten=lambda: 'pixels')
    warp = SimpleNamespace(
        zeros=lambda *a, **kw: calls.append('allocate') or 'output',
        launch=lambda **kw: calls.append(kw))
    values = {'wp': warp, 'ANNOTATOR_SPEC': {annotator: {'name': annotator, 'channels': channels, 'dtype': 'pixel'}},
              'reshape_tiled_image': 'kernel'}
    exec(compile(source.read_text(), str(source), 'exec'), values)
    camera = values['CameraView']()
    camera.tiled_resolution = camera.camera_resolution = (3, 2)
    camera.prims = ['camera']
    data = {'data': buffer, 'info': {'label': 1}} if annotator == 'segmentation' else buffer
    camera._annotators = {annotator: SimpleNamespace(get_data=lambda **kw: data)}
    if size_delta:
        with pytest.raises(RuntimeError, match='frame unavailable'):
            camera.get_data(annotator)
        assert calls == []
    else:
        output, info = camera.get_data(annotator)
        assert output == 'output' and info == ({'label': 1} if annotator == 'segmentation' else {})
        assert calls[0] == 'allocate' and calls[1]['dim'] == (1, 2, 3)
        assert calls[1]['inputs'][0] == 'pixels'
