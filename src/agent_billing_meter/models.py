"""Data models for agent-billing-meter."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class DebitResult:
    """Result of a credit debit operation."""

    success: bool
    app_user_id: str
    operation: str
    amount_debited: int
    timestamp: float = field(default_factory=time.time)
    balance_before: int | None = None
    balance_after: int | None = None
    error: str | None = None

    @property
    def failed(self) -> bool:
        return not self.success
