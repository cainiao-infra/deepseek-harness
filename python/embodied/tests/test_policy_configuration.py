"""The pi05 port preserves training transforms across OpenPI default changes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


def load_config(source, monkeypatch):
    config = ModuleType('openpi.training.config')
    def lookup(name):
        if name == 'installed':
            return 'installed-config'
        raise ValueError('unknown training configuration')
    config.get_config = lookup
    config.TrainConfig = config.AssetsConfig = config.DataConfig = lambda **kw: SimpleNamespace(**kw)
    config.LeRobotAlohaDataConfig = lambda **kw: SimpleNamespace(**dict(
        {'adapt_to_pi': True, 'use_delta_joint_actions': False}, **kw))
    modules = {name: ModuleType(name) for name in ('openpi', 'openpi.training', 'openpi.models',
                'openpi.models.pi0_config', 'openpi.transforms', 'openpi.training.weight_loaders')}
    modules[config.__name__] = config
    modules['openpi.models.pi0_config'].Pi0Config = lambda **kw: kw
    modules['openpi.transforms'].Group = lambda **kw: kw
    modules['openpi.transforms'].RepackTransform = lambda value: value
    modules['openpi.training.weight_loaders'].CheckpointWeightLoader = lambda value: value
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    definition = importlib.util.spec_from_file_location('inspected_inference_config', source)
    module = importlib.util.module_from_spec(definition)
    definition.loader.exec_module(module)
    return module


def test_pinned_pi05_patch_removes_dependency_on_installed_transform_defaults(tmp_path, monkeypatch):
    root = Path(__file__).parents[1]
    relative = 'PhysicalRSI_baselines/robodojo/skills/inference_configs.py'
    source = tmp_path/relative
    source.parent.mkdir(parents=True)
    source.write_bytes((root/'tests/fixtures/inference_configs.before.py').read_bytes())
    manifest = json.loads((root/'integrations/physicalrsi-pi05-transforms.sources.json').read_text())['files'][relative]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == manifest['before_sha256']
    before = load_config(source, monkeypatch).get_config('pi05_base_aloha_full_sim_arx-x5_seed_0', tmp_path)
    assert before.data.adapt_to_pi is True
    assert before.data.use_delta_joint_actions is False
    patch = root/'integrations/physicalrsi-pi05-transforms.patch'
    subprocess.run(['git', 'apply', '--check', str(patch)], cwd=tmp_path, check=True)
    subprocess.run(['git', 'apply', str(patch)], cwd=tmp_path, check=True)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == manifest['after_sha256']
    module = load_config(source, monkeypatch)
    after = module.get_config('pi05_base_aloha_full_sim_arx-x5_seed_0', tmp_path)
    assert after.data.adapt_to_pi is False
    assert after.data.use_delta_joint_actions is True
    assert after.data.assets.assets_dir == str(tmp_path/'assets')
    assert after.model == {'pi05': True}
    assert module.get_config('installed', tmp_path) == 'installed-config'
    with pytest.raises(ValueError, match='unknown training configuration'):
        module.get_config('another-model', tmp_path)
