"""Cross-process, crash-conservative reservations for tool-free model turns."""
from __future__ import annotations

import json
from pathlib import Path

from .bridge import write_record


class CallBudget:
    """Share an immutable limit and never refund attempted or unknown inference.

    Each reserved turn permits one provider request: tools and provider retries
    must remain disabled. Exclusive file creation arbitrates concurrent owners.
    """
    def __init__(self, root: Path, limit: int) -> None:
        if type(limit) is not int or limit < 0:
            raise ValueError("Call limit must be a nonnegative integer")
        self.root, self.limit = root.resolve(), limit
        expected = {"schema": "dsh-model-call-budget/v1", "limit": limit}
        try:
            write_record(self.root / "limit.json", expected)
        except FileExistsError:
            if json.loads((self.root / "limit.json").read_text()) != expected:
                raise ValueError("Existing model call budget has a different limit")

    def reserve(self, request: dict) -> int:
        """Durably consume one slot before inference; exhaustion performs no request."""
        for number in range(self.limit):
            try:
                write_record(self.root / f"call-{number:04d}.json", request)
                return number
            except FileExistsError:
                continue
        raise RuntimeError("Model call budget exhausted; action dispatch remains disabled")
