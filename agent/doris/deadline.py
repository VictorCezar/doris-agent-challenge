"""Prazo interno do turno (ADR-004: proxy corta em 100s, agente opera com 80s)."""
from __future__ import annotations

import time

MIN_TIMEOUT_S = 0.1


class Deadline:
    def __init__(self, seconds: float, *, clock=time.monotonic):
        self._clock = clock
        self.seconds = float(seconds)
        self._end = clock() + self.seconds

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    def is_expired(self) -> bool:
        return self.remaining() <= 0.0

    def cap_timeout(self, default: float) -> float:
        """Timeout para uma operação: nunca maior que o tempo restante do turno."""
        return max(MIN_TIMEOUT_S, min(float(default), self.remaining()))

    def __repr__(self) -> str:  # pragma: no cover
        return f"Deadline(remaining={self.remaining():.1f}s)"
