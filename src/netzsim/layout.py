"""2-D coordinates for drawing a grid as a single-line diagram.

The source workbooks carry **no geographic data**, so coordinates are derived
from the topology. Two layouts are produced and both shipped in ``/network`` so
the UI can toggle without a round-trip:

* **geographic** — a length-aware radial layout: feeders fan out from the
  substation and every edge's geometric length is proportional to its real cable
  length (``net.line.length_km``), with a small deterministic angular jitter. This
  reproduces the sprawling, real-network look of the archive's plots (whose true
  coordinates exist only in the PNG pixels) while staying robust for every grid.
* **tree** — a tidy left-to-right radial tree rooted at the slack (``x`` = depth,
  ``y`` = leaf order); best for tracing feeders.

Both are normalised to ``[0, 1]``.
"""
from __future__ import annotations

import math
from collections import deque
from random import Random

import networkx as nx


def _graph(net) -> nx.Graph:
    g = nx.Graph()
    g.add_nodes_from(int(b) for b in net.bus.index)
    for _, r in net.line.iterrows():
        g.add_edge(int(r["from_bus"]), int(r["to_bus"]))
    for _, r in net.trafo.iterrows():
        g.add_edge(int(r["hv_bus"]), int(r["lv_bus"]))
    return g


def _normalize(pos: dict[int, tuple[float, float]]) -> dict[int, list[float]]:
    if not pos:
        return {}
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    dx, dy = (maxx - minx) or 1.0, (maxy - miny) or 1.0
    return {
        n: [round((x - minx) / dx, 5), round((y - miny) / dy, 5)]
        for n, (x, y) in pos.items()
    }


def _rooted_tree(g: nx.Graph, root: int, visited: set[int]):
    """BFS spanning tree from ``root``; returns (order, children, leaf-weight)."""
    parent = {root: None}
    children: dict[int, list[int]] = {root: []}
    order = [root]
    visited.add(root)
    dq = deque([root])
    while dq:
        u = dq.popleft()
        for v in sorted(g.neighbors(u)):
            if v not in visited:
                visited.add(v)
                parent[v] = u
                children[u].append(v)
                children.setdefault(v, [])
                order.append(v)
                dq.append(v)
    weight: dict[int, int] = {}
    for u in reversed(order):
        kids = children.get(u, [])
        weight[u] = 1 if not kids else sum(weight[c] for c in kids)
    return order, children, weight


# --------------------------------------------------------------------------- #
# length-aware geographic layout
# --------------------------------------------------------------------------- #
def _edge_lengths(net) -> dict[frozenset, float]:
    lengths: dict[frozenset, float] = {}
    for _, r in net.line.iterrows():
        lengths[frozenset((int(r["from_bus"]), int(r["to_bus"])))] = max(
            float(r["length_km"]), 1.0e-4)
    for _, r in net.trafo.iterrows():
        # transformer = substation: HV slack and LV busbar are essentially co-located
        lengths[frozenset((int(r["hv_bus"]), int(r["lv_bus"])))] = 1.0e-3
    return lengths


def _geographic_positions(net) -> dict[int, tuple[float, float]]:
    g = _graph(net)
    if g.number_of_nodes() == 0:
        return {}
    lengths = _edge_lengths(net)
    pos: dict[int, tuple[float, float]] = {}
    visited: set[int] = set()
    component_origin = [0.0, 0.0]

    def grow(root: int) -> None:
        _, children, weight = _rooted_tree(g, root, visited)
        pos[root] = (component_origin[0], component_origin[1])
        # iterative DFS placing each child at its edge length along an angle within
        # the angular sector allotted to its subtree (sectors ∝ subtree leaf count).
        stack = [(root, math.pi / 2.0, 2.0 * math.pi)]
        while stack:
            node, facing, span = stack.pop()
            kids = children.get(node, [])
            if not kids:
                continue
            total = sum(weight[c] for c in kids) or 1
            a = facing - span / 2.0
            for c in sorted(kids):
                cspan = span * weight[c] / total
                jitter = (Random(c).random() - 0.5) * min(cspan, 0.5)
                angle = a + cspan / 2.0 + jitter
                length = lengths.get(frozenset((node, c)), 1.0e-3)
                px, py = pos[node]
                pos[c] = (px + length * math.cos(angle), py + length * math.sin(angle))
                # child continues outward; its subtree fans within a (narrowed) cone
                stack.append((c, angle, min(max(cspan, 0.35) * 0.85, math.pi)))
                a += cspan

    for r in (int(b) for b in net.ext_grid["bus"].tolist()):
        if r not in visited:
            grow(r)
    for n in (int(b) for b in net.bus.index):  # any leftover components
        if n not in visited:
            component_origin[1] -= 1.0
            grow(n)
    return pos


# --------------------------------------------------------------------------- #
# tidy radial tree (rooted at the slack)
# --------------------------------------------------------------------------- #
FEEDER_GAP = 2.0   # extra rows between the main feeders (children of the busbar)
BRANCH_GAP = 0.75  # extra rows between adjacent branch bundles deeper in a feeder


def _tree_positions(net) -> dict[int, tuple[float, float]]:
    g = _graph(net)
    pos: dict[int, tuple[float, float]] = {}
    visited: set[int] = set()
    y_cursor = [0.0]

    def grow(root: int) -> None:
        order, children, _ = _rooted_tree(g, root, visited)
        depth = {root: 0}
        for u in order:
            for c in children.get(u, []):
                depth[c] = depth[u] + 1
        ypos: dict[int, float] = {}

        # depth-first, left to right: every subtree occupies a CONTIGUOUS band of
        # rows (the old reversed-BFS leaf order interleaved leaves of different
        # feeders, which made branches cross and nodes overlap). Between sibling
        # subtrees extra spacing is inserted — a lot between the main feeders
        # (parent at depth <= 1: the slack or the LV busbar), a little between
        # adjacent branch bundles further down.
        def assign(u: int) -> None:
            kids = children.get(u, [])
            if not kids:
                ypos[u] = y_cursor[0]
                y_cursor[0] += 1.0
                return
            for i, c in enumerate(kids):
                if i:
                    if depth[u] <= 1:
                        y_cursor[0] += FEEDER_GAP
                    elif children.get(kids[i - 1]) and children.get(c):
                        y_cursor[0] += BRANCH_GAP
                assign(c)
            ypos[u] = (ypos[kids[0]] + ypos[kids[-1]]) / 2.0

        import sys
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(limit, len(order) * 2 + 100))
        try:
            assign(root)
        finally:
            sys.setrecursionlimit(limit)
        for u in order:
            pos[u] = (float(depth[u]), ypos[u])

    for root in (int(b) for b in net.ext_grid["bus"].tolist()):
        if root not in visited:
            grow(root)
    for node in (int(b) for b in net.bus.index):
        if node not in visited:
            grow(node)
    return pos


def compute_layouts(net) -> tuple[dict[int, list[float]], dict[int, list[float]]]:
    """Return ``(geographic, tree)`` coordinate maps, each ``bus_index -> [x, y]``."""
    geographic = _normalize(_geographic_positions(net))
    tree = _normalize(_tree_positions(net))
    return geographic, tree
