from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, TypeAlias

Aggregation: TypeAlias = Literal["last", "min", "max", "mean", "explicit"]
JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class Histogram:
    """Binned data: one more strictly increasing edge than nonnegative counts."""

    bin_edges: list[float]
    counts: list[int]

    def __post_init__(self) -> None:
        if not 1 <= len(self.counts) <= 256 or len(self.bin_edges) != len(self.counts) + 1:
            raise ValueError("A histogram needs 1–256 counts and one extra edge")
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in self.bin_edges
        ):
            raise ValueError("Histogram edges must be finite numbers")
        if any(a >= b for a, b in zip(self.bin_edges, self.bin_edges[1:])):
            raise ValueError("Histogram edges must strictly increase")
        if any(
            isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 2**53 - 1
            for v in self.counts
        ):
            raise ValueError("Histogram counts must be nonnegative safe integers")
