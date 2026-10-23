"""Explicit Linux dynamic-library directories for the RoboDojo simulator only."""
from pathlib import Path
import os


def simulation_environment(spec: dict, environment: dict) -> dict:
    """Prepend validated configured directories without changing the parent environment.

    An omitted list preserves the inherited loader configuration. Empty entries,
    relative paths and missing directories fail before a simulator is launched.
    Policy processes do not consume this configuration.
    """
    directories = spec.get('simulation_library_dirs', [])
    if not isinstance(directories, list) or any(
            not isinstance(value, str) or not value or os.pathsep in value
            or not Path(value).is_absolute() or not Path(value).is_dir() for value in directories):
        raise ValueError('Simulation library directories must be existing absolute directories')
    result = dict(environment)
    if directories:
        inherited = environment.get('LD_LIBRARY_PATH', '')
        result['LD_LIBRARY_PATH'] = os.pathsep.join([*directories, *([inherited] if inherited else [])])
    return result
