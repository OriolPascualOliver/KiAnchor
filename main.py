#!/usr/bin/env python3
"""
Standalone STEP modifier for PCB assemblies exported from KiCad.

Goal
----
For every non-PCB solid in the STEP model, create a simple rectangular
"anchor" that bridges the solid to the PCB and penetrates into the board.
Then boolean-fuse everything so the final exported STEP is printable as
one connected body.

Design choices
--------------
- Standalone Python: uses CadQuery (pip-installable), not FreeCAD.
- Avoids CadQuery Assembly.importStep(), because KiCad STEP files often reuse
  the same part/subassembly names and that import path can fail on duplicate
  names. Instead this script uses cq.importers.importStep() and works on the
  flattened solid geometry.
- Heuristic PCB detection: finds the solid whose thickness is closest to the
  specified PCB thickness and has the largest XY area.
- Heuristic anchoring: uses an inset rectangular box based on the XY overlap
  between each solid and the PCB footprint.
- Top and bottom sides are both handled.

This is intentionally pragmatic, not pretty. It is meant to force reliable
intersection for 3D printing, not preserve exact electronic package geometry.
"""

from __future__ import annotations

import argparse
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import cadquery as cq


@dataclass
class SolidInfo:
    idx: int
    shape: cq.Shape
    bb: cq.BoundBox

    @property
    def area_xy(self) -> float:
        return self.bb.xlen * self.bb.ylen

    @property
    def volume_bbox(self) -> float:
        return self.bb.xlen * self.bb.ylen * self.bb.zlen


@dataclass
class AnchorResult:
    solid_idx: int
    side: str
    anchor_shape: cq.Shape
    x: float
    y: float
    z0: float
    z1: float


class AnchorError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Attach STEP solids to a PCB by adding rectangular anchors into the board so that components dont fall."
    )
    p.add_argument("--input", required=True, help="Input STEP file")
    p.add_argument("--output", required=True, help="Output STEP file")
    p.add_argument(
        "--pcb-thickness",
        type=float,
        default=1.6,
        help="Nominal PCB thickness in mm (default: 1.6)",
    )
    p.add_argument(
        "--penetration",
        type=float,
        default=1.0,
        help="How far the anchor should penetrate into the PCB in mm (default: 1.0)",
    )
    p.add_argument(
        "--component-overlap",
        type=float,
        default=0.25,
        help="How far the anchor should overlap into the component solid in mm (default: 0.25)",
    )
    p.add_argument(
        "--xy-inset-abs",
        type=float,
        default=0.15,
        help="Absolute inset from the component/PCB overlap rectangle, in mm (default: 0.15)",
    )
    p.add_argument(
        "--xy-inset-ratio",
        type=float,
        default=0.15,
        help="Fallback relative inset as a fraction of overlap width/height (default: 0.15)",
    )
    p.add_argument(
        "--min-anchor-size",
        type=float,
        default=0.20,
        help="Minimum anchor width/height in mm (default: 0.20)",
    )
    p.add_argument(
        "--pcb-thickness-tol",
        type=float,
        default=0.8,
        help="Allowed mismatch when detecting the PCB by thickness, in mm (default: 0.8)",
    )
    p.add_argument(
        "--fuzzy-tol",
        type=float,
        default=0.02,
        help="Fuzzy boolean tolerance in mm (default: 0.02)",
    )
    p.add_argument(
        "--glue",
        action="store_true",
        help="Enable CadQuery/OCCT glue mode for fuse operations",
    )
    p.add_argument(
        "--keep-compound-on-fuse-failure",
        action="store_true",
        help=(
            "If the final global fuse fails, export a compound of overlapping solids instead of aborting. "
            "Most slicers still handle this acceptably."
        ),
    )
    p.add_argument(
        "--max-gap-to-board",
        type=float,
        default=10.0,
        help="Skip solids whose nearest board-side gap exceeds this value in mm (default: 10)",
    )
    p.add_argument(
        "--report-every",
        type=int,
        default=25,
        help="Print progress every N fuse operations (default: 25)",
    )
    return p.parse_args()


def _iter_solids_from_obj(obj: object) -> Iterable[cq.Shape]:
    """Best-effort extraction of all solids from a CadQuery-imported object."""
    if obj is None:
        return

    # CadQuery Shape/Compound objects usually expose .Solids()
    solids_method = getattr(obj, "Solids", None)
    if callable(solids_method):
        for s in solids_method():
            yield s
        return

    # A single solid may still have a bounding box and export methods.
    if hasattr(obj, "BoundingBox") and hasattr(obj, "exportStep"):
        yield obj  # type: ignore[misc]
        return


def import_step_as_solids(path: Path) -> List[SolidInfo]:
    """
    Import a STEP file as flattened geometry, avoiding CadQuery Assembly.importStep().
    """
    try:
        wp = cq.importers.importStep(str(path))
    except Exception as exc:
        raise AnchorError(f"STEP import failed: {exc}") from exc

    solids: List[cq.Shape] = []

    # Preferred path: let Workplane selection flatten compounds into solids.
    try:
        solids = list(wp.solids().vals())
    except Exception:
        solids = []

    # Fallback path: inspect raw values returned by the import.
    if not solids:
        try:
            for obj in wp.vals():
                solids.extend(_iter_solids_from_obj(obj))
        except Exception:
            pass

    # Last fallback: inspect the first value directly.
    if not solids:
        try:
            solids.extend(_iter_solids_from_obj(wp.val()))
        except Exception:
            pass

    # Deduplicate by wrapped shape hash when possible.
    unique: List[cq.Shape] = []
    seen = set()
    for s in solids:
        key = None
        try:
            key = s.hashCode()
        except TypeError:
            try:
                key = s.hashCode(1000003)
            except Exception:
                key = id(s)
        except Exception:
            key = id(s)
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)

    if not unique:
        raise AnchorError("No solids found in STEP file after import.")

    out: List[SolidInfo] = []
    for i, s in enumerate(unique):
        out.append(SolidInfo(i, s, s.BoundingBox()))
    return out


def detect_pcb(solids: Sequence[SolidInfo], pcb_thickness: float, tol: float) -> SolidInfo:
    candidates: List[Tuple[float, float, SolidInfo]] = []
    fallback: List[Tuple[float, SolidInfo]] = []

    for s in solids:
        err = abs(s.bb.zlen - pcb_thickness)
        fallback.append((s.area_xy, s))
        if err <= tol:
            # Prefer large XY area strongly, thickness error weakly.
            candidates.append((s.area_xy, -err, s))

    if candidates:
        candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return candidates[0][2]

    fallback.sort(key=lambda t: t[0], reverse=True)
    board = fallback[0][1]
    log(
        "WARNING: no solid matched PCB thickness tolerance; using the largest XY-area solid as PCB candidate."
    )
    return board


def overlap_interval(a0: float, a1: float, b0: float, b1: float) -> Optional[Tuple[float, float]]:
    lo = max(a0, b0)
    hi = min(a1, b1)
    if hi <= lo:
        return None
    return lo, hi


def shrunken_size(raw_len: float, inset_abs: float, inset_ratio: float, min_anchor: float) -> float:
    if raw_len <= 0:
        return 0.0

    size_abs = raw_len - 2.0 * inset_abs
    size_ratio = raw_len * (1.0 - 2.0 * inset_ratio)

    # Use the smaller of the two reductions, but keep something nonzero.
    size = min(size_abs, size_ratio)
    if size <= 0.0:
        size = raw_len * 0.6
    size = min(size, raw_len)
    size = max(size, min_anchor)
    size = min(size, raw_len)
    return size


def build_anchor(
    solid: SolidInfo,
    pcb: SolidInfo,
    penetration: float,
    component_overlap: float,
    xy_inset_abs: float,
    xy_inset_ratio: float,
    min_anchor_size: float,
    max_gap_to_board: float,
) -> Optional[AnchorResult]:
    sbb = solid.bb
    pbb = pcb.bb
    board_mid_z = 0.5 * (pbb.zmin + pbb.zmax)
    top_side = sbb.center.z >= board_mid_z

    ox = overlap_interval(sbb.xmin, sbb.xmax, pbb.xmin, pbb.xmax)
    oy = overlap_interval(sbb.ymin, sbb.ymax, pbb.ymin, pbb.ymax)
    if ox is None or oy is None:
        return None

    ox0, ox1 = ox
    oy0, oy1 = oy
    ovx = ox1 - ox0
    ovy = oy1 - oy0
    if ovx <= 0 or ovy <= 0:
        return None

    ax = shrunken_size(ovx, xy_inset_abs, xy_inset_ratio, min_anchor_size)
    ay = shrunken_size(ovy, xy_inset_abs, xy_inset_ratio, min_anchor_size)
    if ax <= 0 or ay <= 0:
        return None

    cx = 0.5 * (ox0 + ox1)
    cy = 0.5 * (oy0 + oy1)

    if top_side:
        gap = max(0.0, sbb.zmin - pbb.zmax)
        if gap > max_gap_to_board:
            return None
        z0 = pbb.zmax - penetration
        z1 = sbb.zmin + component_overlap
        side = "top"
    else:
        gap = max(0.0, pbb.zmin - sbb.zmax)
        if gap > max_gap_to_board:
            return None
        z0 = sbb.zmax - component_overlap
        z1 = pbb.zmin + penetration
        side = "bottom"

    if z1 <= z0:
        return None

    height = z1 - z0
    cz = 0.5 * (z0 + z1)

    anchor = cq.Workplane("XY").box(ax, ay, height).translate((cx, cy, cz)).val()
    return AnchorResult(solid.idx, side, anchor, cx, cy, z0, z1)


def safe_fuse(base: cq.Shape, other: cq.Shape, fuzzy_tol: float, glue: bool) -> cq.Shape:
    return base.fuse(other, tol=fuzzy_tol, glue=glue)


def main() -> int:
    args = parse_args()
    inp = Path(args.input)
    out = Path(args.output)

    if not inp.exists():
        log(f"ERROR: input file does not exist: {inp}")
        return 2

    log(f"Loading STEP: {inp}")
    solids = import_step_as_solids(inp)
    log(f"Found {len(solids)} solid(s)")

    pcb = detect_pcb(solids, args.pcb_thickness, args.pcb_thickness_tol)
    pbb = pcb.bb
    log(
        f"Detected PCB candidate: solid #{pcb.idx} | bbox=({pbb.xlen:.3f} x {pbb.ylen:.3f} x {pbb.zlen:.3f}) mm"
    )

    modified_solids: List[cq.Shape] = []
    anchored = 0
    skipped = 0

    for s in solids:
        if s.idx == pcb.idx:
            modified_solids.append(s.shape)
            continue

        anchor_res = build_anchor(
            solid=s,
            pcb=pcb,
            penetration=args.penetration,
            component_overlap=args.component_overlap,
            xy_inset_abs=args.xy_inset_abs,
            xy_inset_ratio=args.xy_inset_ratio,
            min_anchor_size=args.min_anchor_size,
            max_gap_to_board=args.max_gap_to_board,
        )

        if anchor_res is None:
            modified_solids.append(s.shape)
            skipped += 1
            continue

        try:
            merged = safe_fuse(s.shape, anchor_res.anchor_shape, args.fuzzy_tol, args.glue)
        except Exception as exc:
            log(f"WARNING: component fuse failed for solid #{s.idx}: {exc}")
            modified_solids.append(s.shape)
            skipped += 1
            continue

        modified_solids.append(merged)
        anchored += 1

    log(f"Anchored solids: {anchored}")
    log(f"Skipped solids:  {skipped}")

    try:
        log("Running final global fuse...")
        result = modified_solids[0]
        for i, shp in enumerate(modified_solids[1:], start=1):
            result = safe_fuse(result, shp, args.fuzzy_tol, args.glue)
            if args.report_every > 0 and (i % args.report_every) == 0:
                log(f"  fused {i + 1}/{len(modified_solids)} solids")
        try:
            result = result.clean()
        except Exception:
            pass
        result.exportStep(str(out))
        log(f"Wrote fused STEP: {out}")
        return 0
    except Exception as exc:
        log(f"WARNING: final global fuse failed: {exc}")
        if not args.keep_compound_on_fuse_failure:
            log("Aborting because --keep-compound-on-fuse-failure was not enabled.")
            return 3

        log("Falling back to exporting an overlapping compound.")
        compound = cq.Compound.makeCompound(modified_solids)
        compound.exportStep(str(out))
        log(f"Wrote compound STEP: {out}")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Interrupted")
        raise SystemExit(130)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
