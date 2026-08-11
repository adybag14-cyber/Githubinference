from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class Deadline:
    runtime_minutes: int
    reserve_minutes: int = 10
    _started: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.runtime_minutes <= 0:
            raise ValueError("runtime_minutes must be positive")
        if self.reserve_minutes < 0 or self.reserve_minutes >= self.runtime_minutes:
            raise ValueError("reserve_minutes must fit inside runtime")

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.runtime_minutes * 60 - self.elapsed_seconds)

    @property
    def work_seconds(self) -> float:
        return max(0.0, self.remaining_seconds - self.reserve_minutes * 60)

    def should_checkpoint(self, *, estimated_next_turn_seconds: int = 180) -> bool:
        return self.work_seconds <= max(0, estimated_next_turn_seconds)
