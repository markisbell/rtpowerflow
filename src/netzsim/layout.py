"""2-D coordinates for drawing a grid as a single-line diagram.

The source workbooks carry **no geographic data**, so coordinates are derived
from the topology. Two layouts are produced and both shipped in ``/network`` so
the UI can toggle without a round-trip:

* **force** — a Fruchterman-Reingold (spring) embedding, like the organic look of
  pandapower's generic coordinates / the archive's thumbnail PNGs. Seeded with the
  tree layout for speed and stability, deterministic via a fixed seed.
* **tree** — a tidy left-to-right radial tree rooted at the slack (``x`` = depth,
  ``y`` = leaf order); best for tracing feeders.

Both are normalised to ``[0, 1]``.
"""
from __future__ import annotations

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


# --------------------------------------------------------------------------- #
# tidy radial tree (rooted at the slack)
# --------------------------------------------------------------------------- #
def _tree_positions(net) -> dict[int, tuple[float, float]]:
    g = _graph(net)
    pos: dict[int, tuple[float, float]] = {}
    visited: set[int] = set()
    y_cursor = 0.0

    def grow(root: int) -> None:
        nonlocal y_cursor
        depth = {root: 0}
        children: dict[int, list[int]] = {root: []}
        preorder: list[int] = []
        stack = [root]
        visited.add(root)
        while stack:
            u = stack.pop()
            preorder.append(u)
            for v in sorted(g.neighbors(u), reverse=True):
                if v not in visited:
                    visited.add(v)
                    depth[v] = depth[u] + 1
                    children[u].append(v)
                    children.setdefault(v, [])
                    stack.append(v)
        ypos: dict[int, float] = {}
        for u in reversed(preorder):
            kids = children.get(u, [])
            if kids:
                ypos[u] = sum(ypos[c] for c in kids) / len(kids)
            else:
                ypos[u] = y_cursor
                y_cursor += 1.0
        for u in preorder:
            pos[u] = (float(depth[u]), ypos[u])

    for root in (int(b) for b in net.ext_grid["bus"].tolist()):
        if root not in visited:
            grow(root)
    for node in (int(b) for b in net.bus.index):
        if node not in visited:
            grow(node)
    return pos


# --------------------------------------------------------------------------- #
# force-directed (spring) embedding
# --------------------------------------------------------------------------- #
def _force_positions(net, init: dict[int, tuple[float, float]]) -> dict[int, tuple[float, float]]:
    g = _graph(net)
    n = g.number_of_nodes()
    if n <= 2:
        return init
    init_arr = {node: [init[node][0], init[node][1]] for node in g.nodes if node in init}
    # spring_layout (dense FR) is O(n^2) per iteration; with the tree as a warm
    # start, fewer iterations still relax it into an organic shape. Hold the
    # n^2 * iterations budget roughly constant so even ~1700-bus grids stay ~2 s.
    iters = int(min(50, max(8, 4.0e7 / (n * n))))
    try:
        pos = nx.spring_layout(
            g, pos=init_arr or None, seed=42, iterations=iters,
            k=1.0 / max(n ** 0.5, 1.0),
        )
        return {int(node): (float(p[0]), float(p[1])) for node, p in pos.items()}
    except Exception:
        return init  # fall back to the tree positions


def compute_layouts(net) -> tuple[dict[int, list[float]], dict[int, list[float]]]:
    """Return ``(force, tree)`` coordinate maps, each ``bus_index -> [x, y]``."""
    tree_raw = _tree_positions(net)
    tree = _normalize(tree_raw)
    force = _normalize(_force_positions(net, tree_raw))
    return force, tree
