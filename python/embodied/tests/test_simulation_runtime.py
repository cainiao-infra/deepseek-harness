"""Simulator loader configuration rejects missing dependencies before launch."""
import os

import pytest

from deepseek_harness_embodied.simulation_runtime import simulation_environment


def test_library_directories_are_simulator_local_and_preserve_inherited_order(tmp_path):
    original = {'LD_LIBRARY_PATH': '/inherited', 'OTHER': 'preserved'}
    result = simulation_environment({'simulation_library_dirs': [str(tmp_path)]}, original)
    assert result == {'LD_LIBRARY_PATH': str(tmp_path)+os.pathsep+'/inherited', 'OTHER': 'preserved'}
    assert original == {'LD_LIBRARY_PATH': '/inherited', 'OTHER': 'preserved'}
    assert simulation_environment({}, original) == original
    assert simulation_environment({'simulation_library_dirs': [str(tmp_path)]}, {}) == {
        'LD_LIBRARY_PATH': str(tmp_path)}


@pytest.mark.parametrize('value', ['directory', [None], [''], ['relative'], ['/nonexistent-dsh-directory'], ['/tmp:/other']])
def test_invalid_library_directories_fail_before_launch(value):
    with pytest.raises(ValueError, match='existing absolute'):
        simulation_environment({'simulation_library_dirs': value}, {})
