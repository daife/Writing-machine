# vpype-forkclean

`forkclean` cleans short terminal chains at high-turn multi-fork junctions in centerline SVGs.

## Important note about SVG forks

SVG does not contain a real topological “shared node” concept. It only stores path coordinates.  
`forkclean` builds a graph by treating coincident or near-coincident vertices as the same graph node.

That is why `--node-tolerance` remains useful:

- use `--node-tolerance 0px` for exact coordinate matching
- use `--node-tolerance 0.5px` to `1px` when centerline tracing, `linemerge`, or `linesimplify` leaves tiny coordinate gaps

## Rule

For each node with degree `>= 3`:

1. Walk along every incident direction.
2. Pass through every intermediate node with `degree == 2`.
3. Stop at:
   - endpoint `degree == 1`: classify as a **short chain**
   - fork `degree >= 3`: classify as a **long chain**
   - `--max-search-length`: classify as a **long chain**
4. For long chains, compute the direction from the current fork to:
   - the next fork, or
   - the exact point at `--max-search-length`
5. Compute pairwise turn angles among all long chains.
6. If the minimum pairwise turn angle is greater than `--angle-threshold`, delete every terminal short chain whose length is less than `--min-short-length`.

Turn angle is:

```text
turn = 180° - inner_angle
```

So a straight continuation has turn angle near `0°`, while a sharp turn has a large turn angle.

## Install

```bash
python -m pip install -e .
```

## Suggested pipeline

```bash
vpype read line_only.svg \
  linemerge -t 1px \
  linesimplify -t 0.3px \
  forkclean -m 24px -s 5px -a 110deg -n 1px \
  linemerge -t 1px \
  linesort --two-opt \
  write cleaned.svg
```

## Exact node matching

If `linemerge` already gives exact shared vertices:

```bash
vpype read line_only.svg \
  linemerge -t 1px \
  linesimplify -t 0.3px \
  forkclean -m 24px -s 5px -a 110deg -n 0px \
  write cleaned.svg
```

## Dry run

```bash
vpype read line_only.svg \
  linemerge -t 1px \
  linesimplify -t 0.3px \
  forkclean -m 24px -s 5px -a 110deg -n 1px --dry-run \
  write dryrun.svg
```

## Options

```bash
forkclean \
  --max-search-length 24px \
  --min-short-length 5px \
  --angle-threshold 110deg \
  --node-tolerance 1px \
  --passes 1
```

- `--max-search-length / -m`: branch search limit. Reaching this length means the branch is long.
- `--min-short-length / -s`: delete terminal short chains shorter than this.
- `--angle-threshold / -a`: delete only when the minimum pairwise turn angle among long chains is greater than this.
- `--node-tolerance / -n`: clusters nearby vertices into one graph node. Use `0px` for exact matching.
- `--passes`: repeat pruning.
- `--dry-run`: print how many graph edges would be removed without modifying geometry.
