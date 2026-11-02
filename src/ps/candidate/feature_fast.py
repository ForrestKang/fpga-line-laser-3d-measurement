"""Exact data structures shared by observed-feature end analysis.

The historical ``features_v4``/``reference_candidate.features`` helpers use two
patterns that dominate the end-of-run analysis on complex clouds:

* one ``np.flatnonzero(bins == level)`` scan per Z level (O(points * levels)),
* a Python ``dict``/``set`` connected-component walk with tuple keys, repeated
  for every slice and every plane hypothesis.

This module keeps the exact historical grouping semantics -- lexicographic start
cell, LIFO DFS with dx-major/dy-minor neighbour order, original point order inside
a cell, and ``np.unique`` level order -- while replacing the scans with one stable
sort per call and the tuple/set walk with integer cell keys and a search-sorted
neighbour table.  No distance, threshold, gate or accepted-feature rule changes.
"""
from __future__ import annotations

import numpy as np


def bin_groups(values):
    """Return ``[(level, point_indices), ...]`` in ``np.unique`` order.

    ``point_indices`` are ascending, exactly like the replaced
    ``np.flatnonzero(bins == level)`` calls.  One stable sort replaces one full
    array scan per level.
    """
    values = np.asarray(values)
    if values.ndim != 1:
        raise ValueError("one-dimensional bin values required")
    if not len(values):
        return []
    order = np.argsort(values, kind="stable")
    ordered = values[order]
    cuts = np.flatnonzero(ordered[1:] != ordered[:-1]) + 1
    starts = np.r_[0, cuts]
    ends = np.r_[cuts, len(values)]
    return [(ordered[start], order[start:end])
            for start, end in zip(starts, ends)]


def _cell_neighbours(cells, width):
    """Neighbour table in the historical dx-major, dy-minor order."""
    count = len(cells)
    xs = cells // width
    ys = cells % width
    table = np.full((count, 9), -1, dtype=np.int64)
    slot = 0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                table[:, slot] = np.arange(count, dtype=np.int64)
                slot += 1
                continue
            nx = xs + dx
            ny = ys + dy
            neighbour = nx * width + ny
            position = np.searchsorted(cells, neighbour)
            valid = (nx >= 0) & (ny >= 0) & (ny < width) & (position < count)
            if valid.any():
                found_at = position[valid]
                found = cells[found_at] == neighbour[valid]
                rows = np.flatnonzero(valid)[found]
                table[rows, slot] = found_at[found]
            slot += 1
    return table


def connected_cells(points, cell):
    """Connected occupied 2-D cells, never interpolating an empty cell.

    Drop-in replacement for the historical ``_components``.  The returned group
    order (smallest occupied cell first), the DFS traversal order and the point
    order inside each occupied cell are identical to the original implementation.
    """
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2:
        raise ValueError("Nx2 occupied-cell points required")
    if len(points) == 0:
        return []
    if not np.isfinite(cell) or cell <= 0:
        raise ValueError("positive finite cell size required")

    grid = np.floor(points / cell).astype(np.int64)
    grid = grid - grid.min(axis=0)
    width = int(grid[:, 1].max()) + 1
    keys = grid[:, 0].astype(np.int64) * width + grid[:, 1]
    cells, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)

    if len(cells) == 1:
        # The historical DFS has a single start cell, marks it before the walk
        # and appends ``order[0:len(points)]``.  With one unique cell that stable
        # order is exactly 0..n-1, so skip the neighbour table and traversal.
        return [np.arange(len(points), dtype=np.intp)]

    # Stable argsort keeps the original point order within each occupied cell.
    order = np.argsort(inverse, kind="stable")
    starts = np.r_[0, np.cumsum(counts)[:-1]]
    neighbour = _cell_neighbours(cells, width)

    visited = np.zeros(len(cells), dtype=bool)
    groups = []
    for start in range(len(cells)):
        if visited[start]:
            continue
        visited[start] = True
        pending = [start]
        parts = []
        while pending:
            current = pending.pop()
            begin = int(starts[current])
            parts.append(order[begin:begin + int(counts[current])])
            row = neighbour[current]
            for slot in range(9):
                nxt = int(row[slot])
                if nxt >= 0 and nxt != current and not visited[nxt]:
                    visited[nxt] = True
                    pending.append(nxt)
        groups.append(np.concatenate(parts) if parts else np.empty(0, dtype=np.int64))
    return groups
