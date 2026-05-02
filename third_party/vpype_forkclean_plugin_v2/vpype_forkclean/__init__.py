from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple, Set, Optional, Iterable

import click
import numpy as np
import vpype as vp
from vpype_cli.decorators import layer_processor
from vpype_cli.types import AngleType, LengthType

Point = Tuple[float, float]
NodeId = int
EdgeId = int


@dataclass
class Edge:
    a: NodeId
    b: NodeId
    removed: bool = False


@dataclass
class Branch:
    first_eid: EdgeId
    chain: List[EdgeId]
    length: float
    kind: str  # "short", "long_fork", "long_limit"
    vec: Point


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _inner_angle_deg(u: Point, v: Point) -> float:
    nu = math.hypot(u[0], u[1])
    nv = math.hypot(v[0], v[1])
    if nu <= 1e-12 or nv <= 1e-12:
        return 0.0
    dot = (u[0] * v[0] + u[1] * v[1]) / (nu * nv)
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot))


def _turn_angle_deg(u: Point, v: Point) -> float:
    # straight continuation => 0°, right angle => 90°, sharp corner => large
    return 180.0 - _inner_angle_deg(u, v)


class DSU:
    def __init__(self) -> None:
        self.parent: List[int] = []

    def add(self) -> int:
        idx = len(self.parent)
        self.parent.append(idx)
        return idx

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _line_to_points(line: np.ndarray) -> List[Point]:
    arr = np.asarray(line)
    return [(float(z.real), float(z.imag)) for z in arr]


def _cluster_points(points: List[Point], tolerance: float) -> Tuple[List[Point], List[int]]:
    """
    SVG/vpype has no topological shared-node object. A fork is inferred from
    coincident or near-coincident vertices. Set tolerance to 0px for exact
    coordinate matching, or keep a small value to absorb floating-point and
    tracing/simplification noise.
    """
    dsu = DSU()
    for _ in points:
        dsu.add()

    if tolerance <= 0:
        exact: Dict[Tuple[int, int], int] = {}
        for i, p in enumerate(points):
            key = (round(p[0] * 1e9), round(p[1] * 1e9))
            if key in exact:
                dsu.union(i, exact[key])
            else:
                exact[key] = i
    else:
        grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        inv = 1.0 / tolerance

        def key(p: Point) -> Tuple[int, int]:
            return (math.floor(p[0] * inv), math.floor(p[1] * inv))

        for i, p in enumerate(points):
            kx, ky = key(p)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in grid.get((kx + dx, ky + dy), []):
                        if _dist(p, points[j]) <= tolerance:
                            dsu.union(i, j)
            grid[(kx, ky)].append(i)

    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(points)):
        groups[dsu.find(i)].append(i)

    centers: List[Point] = []
    point_to_node = [0] * len(points)
    for node_id, members in enumerate(groups.values()):
        cx = sum(points[i][0] for i in members) / len(members)
        cy = sum(points[i][1] for i in members) / len(members)
        centers.append((cx, cy))
        for i in members:
            point_to_node[i] = node_id
    return centers, point_to_node


def _build_graph(lines: Iterable[np.ndarray], node_tolerance: float) -> Tuple[List[Point], List[Edge]]:
    raw_points: List[Point] = []
    polylines: List[List[Point]] = []

    for line in lines:
        pts = _line_to_points(line)
        if len(pts) < 2:
            continue
        cleaned = [pts[0]]
        for p in pts[1:]:
            if _dist(cleaned[-1], p) > 1e-9:
                cleaned.append(p)
        if len(cleaned) < 2:
            continue
        polylines.append(cleaned)
        raw_points.extend(cleaned)

    if not raw_points:
        return [], []

    nodes, point_to_node = _cluster_points(raw_points, node_tolerance)

    edges: List[Edge] = []
    cursor = 0
    for pts in polylines:
        node_seq: List[NodeId] = []
        for _ in pts:
            node_seq.append(point_to_node[cursor])
            cursor += 1
        for i in range(len(node_seq) - 1):
            a = node_seq[i]
            b = node_seq[i + 1]
            if a != b:
                edges.append(Edge(a=a, b=b))

    return nodes, edges


def _adjacency(edges: List[Edge]) -> Dict[NodeId, List[Tuple[EdgeId, NodeId]]]:
    adj: Dict[NodeId, List[Tuple[EdgeId, NodeId]]] = defaultdict(list)
    for eid, e in enumerate(edges):
        if e.removed:
            continue
        adj[e.a].append((eid, e.b))
        adj[e.b].append((eid, e.a))
    return adj


def _other(edges: List[Edge], eid: EdgeId, node: NodeId) -> NodeId:
    e = edges[eid]
    return e.b if node == e.a else e.a


def _edge_len(nodes: List[Point], edges: List[Edge], eid: EdgeId) -> float:
    e = edges[eid]
    return _dist(nodes[e.a], nodes[e.b])


def _interpolate(p0: Point, p1: Point, t: float) -> Point:
    return (p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t)


def _walk_branch(
    start: NodeId,
    first_eid: EdgeId,
    nodes: List[Point],
    edges: List[Edge],
    adj: Dict[NodeId, List[Tuple[EdgeId, NodeId]]],
    max_search_length: float,
) -> Branch:
    """
    Walk from a multi-fork node along one branch.

    Stop/classify by the requested rule:
      - degree == 1 endpoint => short chain
      - degree >= 3 fork    => long chain
      - reaches max length  => long chain

    For long-chain angle calculation, the vector goes from the current fork to:
      - the next fork node, or
      - the exact point at max_search_length.
    """
    chain: List[EdgeId] = []
    length = 0.0
    prev = start
    cur_eid = first_eid
    cur = _other(edges, cur_eid, start)
    seen_edges: Set[EdgeId] = set()

    while True:
        if cur_eid in seen_edges:
            # Loop/malformed branch: protect geometry by classifying as long.
            stop = nodes[cur]
            return Branch(first_eid, chain, length, "long_fork", (stop[0] - nodes[start][0], stop[1] - nodes[start][1]))

        seen_edges.add(cur_eid)
        chain.append(cur_eid)

        p0 = nodes[prev]
        p1 = nodes[cur]
        seg_len = _edge_len(nodes, edges, cur_eid)

        if length + seg_len >= max_search_length:
            if seg_len <= 1e-12:
                stop = p1
            else:
                t = (max_search_length - length) / seg_len
                t = max(0.0, min(1.0, t))
                stop = _interpolate(p0, p1, t)
            vec = (stop[0] - nodes[start][0], stop[1] - nodes[start][1])
            return Branch(first_eid, chain, max_search_length, "long_limit", vec)

        length += seg_len
        deg = len(adj.get(cur, []))

        if deg <= 1:
            stop = nodes[cur]
            vec = (stop[0] - nodes[start][0], stop[1] - nodes[start][1])
            return Branch(first_eid, chain, length, "short", vec)

        if deg >= 3:
            stop = nodes[cur]
            vec = (stop[0] - nodes[start][0], stop[1] - nodes[start][1])
            return Branch(first_eid, chain, length, "long_fork", vec)

        # degree == 2: pass through
        candidates = [(eid, nb) for eid, nb in adj[cur] if eid != cur_eid]
        if not candidates:
            stop = nodes[cur]
            vec = (stop[0] - nodes[start][0], stop[1] - nodes[start][1])
            return Branch(first_eid, chain, length, "short", vec)

        next_eid, next_node = candidates[0]
        prev, cur_eid, cur = cur, next_eid, next_node


def _trace_remaining_paths(nodes: List[Point], edges: List[Edge]) -> List[np.ndarray]:
    adj = _adjacency(edges)
    used: Set[EdgeId] = set()
    out: List[np.ndarray] = []

    def walk_from(start: NodeId, first_eid: EdgeId) -> None:
        pts: List[Point] = [nodes[start]]
        cur = start
        eid = first_eid

        while True:
            used.add(eid)
            nxt = _other(edges, eid, cur)
            pts.append(nodes[nxt])

            options = [(neid, nb) for neid, nb in adj.get(nxt, []) if neid not in used]
            if len(adj.get(nxt, [])) == 2 and len(options) == 1:
                cur, eid = nxt, options[0][0]
                continue
            break

        if len(pts) >= 2:
            out.append(np.array([complex(x, y) for x, y in pts]))

    for node in sorted(adj.keys(), key=lambda n: (nodes[n][0], nodes[n][1])):
        if len(adj[node]) != 2:
            for eid, _ in list(adj[node]):
                if eid not in used and not edges[eid].removed:
                    walk_from(node, eid)

    for eid, e in enumerate(edges):
        if e.removed or eid in used:
            continue
        walk_from(e.a, eid)

    return out


@click.command("forkclean")
@click.option("-m", "--max-search-length", type=LengthType(), default="24px", show_default=True,
              help="Branch search limit. Reaching this distance classifies the branch as long.")
@click.option("-s", "--min-short-length", type=LengthType(), default="5px", show_default=True,
              help="Delete terminal short chains whose length is below this value after the angle condition is met.")
@click.option("-a", "--angle-threshold", type=AngleType(), default="110deg", show_default=True,
              help="Delete short chains only if the minimum pairwise turn angle among long chains is above this threshold.")
@click.option("-n", "--node-tolerance", type=LengthType(), default="1px", show_default=True,
              help="Cluster vertices closer than this distance into one graph node. Use 0px for exact matching.")
@click.option("--passes", type=int, default=1, show_default=True,
              help="Repeat cleanup passes. Useful when deleting one short chain exposes another.")
@click.option("--dry-run", is_flag=True,
              help="Analyze but do not delete. Prints per-pass statistics.")
@layer_processor
def forkclean(
    lines: vp.LineCollection,
    max_search_length: float,
    min_short_length: float,
    angle_threshold: float,
    node_tolerance: float,
    passes: int,
    dry_run: bool,
) -> vp.LineCollection:
    """
    Clean short terminal chains at high-turn multi-fork junctions.

    Workflow:
      1. Find graph nodes with degree >= 3.
      2. From each incident direction, walk through all degree == 2 nodes.
      3. Stop at endpoint degree == 1 (short), fork degree >= 3 (long), or max-search-length (long).
      4. For long chains, use the vector from the fork to the next fork or exact max-search point.
      5. Compute pairwise turn angles among long chains.
      6. If the minimum turn angle is above angle-threshold, delete all terminal short chains below min-short-length.
    """
    nodes, edges = _build_graph(list(lines), node_tolerance)
    if not edges:
        return lines

    angle_threshold_deg = math.degrees(angle_threshold)

    for pass_idx in range(max(1, passes)):
        adj = _adjacency(edges)
        forks = [n for n, items in adj.items() if len(items) >= 3]
        forks.sort(key=lambda n: (nodes[n][0], nodes[n][1]))

        removed_this_pass: Set[EdgeId] = set()

        for fork in forks:
            adj = _adjacency(edges)
            if len(adj.get(fork, [])) < 3:
                continue

            branches = [
                _walk_branch(
                    fork, eid, nodes, edges, adj,
                    max_search_length=max_search_length,
                )
                for eid, _ in adj[fork]
            ]

            long_branches = [b for b in branches if b.kind.startswith("long")]
            short_branches = [b for b in branches if b.kind == "short"]

            if len(long_branches) < 2:
                continue

            turns: List[float] = []
            for i in range(len(long_branches)):
                for j in range(i + 1, len(long_branches)):
                    turns.append(_turn_angle_deg(long_branches[i].vec, long_branches[j].vec))

            if not turns:
                continue

            if min(turns) > angle_threshold_deg:
                for b in short_branches:
                    if b.length < min_short_length:
                        for eid in b.chain:
                            if not edges[eid].removed:
                                removed_this_pass.add(eid)

        if dry_run:
            click.echo(f"forkclean pass {pass_idx + 1}: would remove {len(removed_this_pass)} edge(s)", err=True)
            break

        if not removed_this_pass:
            break

        for eid in removed_this_pass:
            edges[eid].removed = True

    if dry_run:
        return lines

    out = vp.LineCollection()
    for arr in _trace_remaining_paths(nodes, edges):
        out.append(arr)
    return out
