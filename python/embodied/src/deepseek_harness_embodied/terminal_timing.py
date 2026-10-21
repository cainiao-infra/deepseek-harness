"""Measure the first native terminal check without adding environment queries."""
from __future__ import annotations

import time
from typing import Callable


class TerminalTiming:
    """Observe existing checks; cold start stays in completion time, teardown does not.

    The runner passes the native terminal return and success flag after the
    original method returns. Success flags before terminal are never outcomes.
    This measurement alone does not qualify a warmed-up performance comparison.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.started = clock()
        self.first_action = None
        self.terminal = None

    def dispatched(self) -> None:
        """Capture the first dispatch time without claiming the action succeeded."""
        if self.first_action is None:
            self.first_action = self.clock() - self.started

    def checked(self, terminal: bool, success: bool) -> None:
        """Retain only the first observed native terminal result."""
        if terminal and self.terminal is None:
            self.terminal = {'elapsed_seconds': self.clock() - self.started, 'success': success}

    def snapshot(self) -> dict:
        """Return unknown completion for failures without a native terminal check."""
        return {'schema': 'dsh-native-terminal-timing/v1', 'qualification': False,
                'first_action_seconds': self.first_action, 'native_terminal': self.terminal,
                'includes_skill_cold_start': True, 'includes_evaluator_teardown': False}
