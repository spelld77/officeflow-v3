"""Literal text comparison with a hard cooperative CPU/size budget, no unit inference."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True, slots=True)
class TextComparison:
    left: tuple[tuple[int, int], ...] = ()
    right: tuple[tuple[int, int], ...] = ()
    fallback: bool = False


def compare_text(left: str, right: str, *, budget: float = 0.2) -> TextComparison:
    if len(left) + len(right) > 100_000 or left.count("\n") + right.count("\n") > 10_000:
        return TextComparison(fallback=True)
    deadline = monotonic() + budget
    before, after = left.splitlines(keepends=True), right.splitlines(keepends=True)
    offsets: list[list[int]] = []
    for lines in (before, after):
        positions, position = [], 0
        for line in lines:
            positions.append(position)
            position += len(line)
        offsets.append(positions)
    changes: list[list[tuple[int, int]]] = [[], []]
    i = j = 0
    while i < len(before) or j < len(after):
        if monotonic() >= deadline:
            return TextComparison(fallback=True)
        if i < len(before) and j < len(after) and before[i] == after[j]:
            i += 1
            j += 1
            continue
        # A bounded local line alignment. Whole rewrites do not become an O(n²) search.
        match: tuple[int, int] | None = None
        right_lines = {line: k for k, line in reversed(list(enumerate(after[j : j + 32], j)))}
        for k in range(i, min(i + 32, len(before))):
            if before[k] in right_lines:
                pair = k, right_lines[before[k]]
                if match is None or sum(pair) < sum(match):
                    match = pair
        end_i, end_j = (
            match if match is not None else (min(i + 1, len(before)), min(j + 1, len(after)))
        )
        length = max(end_i - i, end_j - j)
        for step in range(length):
            a = before[i + step] if i + step < end_i else ""
            b = after[j + step] if j + step < end_j else ""
            start = 0
            while start < min(len(a), len(b)) and a[start] == b[start]:
                start += 1
            suffix = 0
            while suffix < min(len(a), len(b)) - start and a[-suffix - 1] == b[-suffix - 1]:
                suffix += 1
            for side, line, index in ((0, a, i + step), (1, b, j + step)):
                end = len(line) - suffix
                if start < end:
                    changes[side].append((offsets[side][index] + start, offsets[side][index] + end))
        i, j = end_i, end_j
    return TextComparison(tuple(changes[0]), tuple(changes[1]))
