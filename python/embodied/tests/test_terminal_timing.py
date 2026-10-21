"""Native completion timing excludes teardown and ignores preterminal success flags."""
from deepseek_harness_embodied.terminal_timing import TerminalTiming


def test_only_first_native_terminal_check_ends_measurement():
    now = [10.0]
    meter = TerminalTiming(clock=lambda: now[0])
    meter.checked(False, True)
    assert meter.snapshot()['native_terminal'] is None
    now[0] = 15
    meter.dispatched()
    now[0] = 20
    meter.dispatched()
    meter.checked(True, True)
    now[0] = 100
    meter.checked(True, False)
    assert meter.snapshot()['native_terminal'] == {'elapsed_seconds': 10, 'success': True}
    assert meter.snapshot()['first_action_seconds'] == 5
    assert meter.snapshot()['includes_evaluator_teardown'] is False


def test_native_failure_is_not_confused_with_missing_outcome():
    meter = TerminalTiming(clock=lambda: 1.0)
    assert meter.snapshot()['native_terminal'] is None
    meter.checked(True, False)
    assert meter.snapshot()['native_terminal'] == {'elapsed_seconds': 0, 'success': False}
