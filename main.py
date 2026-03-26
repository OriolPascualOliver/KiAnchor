#!/usr/bin/env python3
"""
Standalone STEP modifier for PCB assemblies exported from KiCad.

Goal
----
For every non-PCB solid in the STEP model, extend the solid toward the PCB
using the *full XY bounding footprint* of that solid (clipped to the PCB XY
bounds), then optionally export the result either as:

- one fused body,
- a compound of multiple touching bodies,
- or auto: try one fused body first, fall back to a compound.

This version intentionally avoids face-picking heuristics. The extension
(direction and length) is driven only by the component bounding box and the
nearest PCB face, because that is more robust for KiCad STEP exports.
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


@dataclass
class ExtensionResult:
    solid_idx: int
    side: str
    extension_shape: cq.Shape
    x: float
    y: float
    z0: float
    z1: float
    sx: float
    sy: float


class AnchorError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Attach STEP solids to a PCB by extending each component bounding footprint toward the board."
        )
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
        help="How far the extension should penetrate into the PCB in mm (default: 1.0)",
    )
    p.add_argument(
        "--component-overlap",
        type=float,
        default=0.25,
        help=(
            "How far the extension should overlap back into the component solid in mm to ensure fusion "
            "(default: 0.25)"
        ),
    )
    p.add_argument(
        "--xy-inset-abs",
        type=float,
        default=0.0,
        help=(
            "Absolute inset from the bounding-footprint rectangle, in mm. Keep at 0.0 to extend the full "
            "bounding footprint (default: 0.0)"
        ),
    )
    p.add_argument(
        "--xy-inset-ratio",
        type=float,
        default=0.0,
        help=(
            "Relative inset from the bounding-footprint rectangle. Keep at 0.0 to extend the full bounding "
            "footprint (default: 0.0)"
        ),
    )
    p.add_argument(
        "--min-extension-size",
        type=float,
        default=0.20,
        help="Minimum extension width/height in mm after any inset (default: 0.20)",
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
        "--max-gap-to-board",
        type=float,
        default=10.0,
        help="Skip solids whose nearest board-face gap exceeds this value in mm (default: 10)",
    )
    p.add_argument(
        "--export-mode",
        choices=("auto", "onebody", "compound"),
        default="auto",
        help=(
            "Export strategy: onebody = require a single fused solid, compound = export a multi-solid compound, "
            "auto = try onebody first and fall back to compound (default: auto)"
        ),
    )
    p.add_argument(
        "--report-every",
        type=int,
        default=25,
        help="Print progress every N fuse operations (default: 25)",
    )
    return p.parse_args()


def _iter_solids_from_obj(obj: object) -> Iterable[cq.Shape]:
    if obj is None:
        return

    solids_method = getattr(obj, "Solids", None)
    if callable(solids_method):
        for s in solids_method():
            yield s
        return

    if hasattr(obj, "BoundingBox") and hasattr(obj, "exportStep"):
        yield obj  # type: ignore[misc]
        return


def import_step_as_solids(path: Path) -> List[SolidInfo]:
    try:
        wp = cq.importers.importStep(str(path))
    except Exception as exc:
        raise AnchorError(f"STEP import failed: {exc}") from exc

    solids: List[cq.Shape] = []

    try:
        solids = list(wp.solids().vals())
    except Exception:
        solids = []

    if not solids:
        try:
            for obj in wp.vals():
                solids.extend(_iter_solids_from_obj(obj))
        except Exception:
            pass

    if not solids:
        try:
            solids.extend(_iter_solids_from_obj(wp.val()))
        except Exception:
            pass

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


def shrunken_size(raw_len: float, inset_abs: float, inset_ratio: float, min_size: float) -> float:
    if raw_len <= 0:
        return 0.0

    size_abs = raw_len - 2.0 * inset_abs
    size_ratio = raw_len * (1.0 - 2.0 * inset_ratio)
    size = min(size_abs, size_ratio)

    if size <= 0.0:
        size = raw_len * 0.6

    size = min(size, raw_len)
    size = max(size, min_size)
    size = min(size, raw_len)
    return size


def _nearest_board_face(sbb: cq.BoundBox, pbb: cq.BoundBox) -> Tuple[str, float, float]:
    """
    Return (side, board_face_z, component_face_z), where side is either:
      - 'top'    : extend from component zmin toward PCB top face zmax
      - 'bottom' : extend from component zmax toward PCB bottom face zmin

    This avoids relying on the solid center, which can be misleading for odd
    or asymmetric shapes.
    """
    dist_to_top = abs(sbb.zmin - pbb.zmax)
    dist_to_bottom = abs(sbb.zmax - pbb.zmin)

    if dist_to_top <= dist_to_bottom:
        return "top", pbb.zmax, sbb.zmin
    return "bottom", pbb.zmin, sbb.zmax


def build_bbox_extension(
    solid: SolidInfo,
    pcb: SolidInfo,
    penetration: float,
    component_overlap: float,
    xy_inset_abs: float,
    xy_inset_ratio: float,
    min_extension_size: float,
    max_gap_to_board: float,
) -> Optional[ExtensionResult]:
    sbb = solid.bb
    pbb = pcb.bb

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

    sx = shrunken_size(ovx, xy_inset_abs, xy_inset_ratio, min_extension_size)
    sy = shrunken_size(ovy, xy_inset_abs, xy_inset_ratio, min_extension_size)
    if sx <= 0 or sy <= 0:
        return None

    cx = 0.5 * (ox0 + ox1)
    cy = 0.5 * (oy0 + oy1)

    side, board_face_z, component_face_z = _nearest_board_face(sbb, pbb)
    gap = abs(component_face_z - board_face_z)
    if gap > max_gap_to_board:
        return None

    if side == "top":
        z0 = board_face_z - penetration
        z1 = component_face_z + component_overlap
    else:
        z0 = component_face_z - component_overlap
        z1 = board_face_z + penetration

    if z1 <= z0:
        return None

    height = z1 - z0
    cz = 0.5 * (z0 + z1)
    extension = cq.Workplane("XY").box(sx, sy, height).translate((cx, cy, cz)).val()

    return ExtensionResult(
        solid_idx=solid.idx,
        side=side,
        extension_shape=extension,
        x=cx,
        y=cy,
        z0=z0,
        z1=z1,
        sx=sx,
        sy=sy,
    )


def safe_fuse(base: cq.Shape, other: cq.Shape, fuzzy_tol: float, glue: bool) -> cq.Shape:
    return base.fuse(other, tol=fuzzy_tol, glue=glue)


def count_solids(shape: cq.Shape) -> int:
    solids_method = getattr(shape, "Solids", None)
    if callable(solids_method):
        try:
            return len(list(solids_method()))
        except Exception:
            pass
    return 1


def try_build_one_body(
    shapes: Sequence[cq.Shape],
    fuzzy_tol: float,
    glue: bool,
    report_every: int,
) -> cq.Shape:
    if not shapes:
        raise AnchorError("No shapes available for export.")

    result = shapes[0]
    for i, shp in enumerate(shapes[1:], start=1):
        result = safe_fuse(result, shp, fuzzy_tol, glue)
        if report_every > 0 and (i % report_every) == 0:
            log(f"  fused {i + 1}/{len(shapes)} solids")

    try:
        result = result.clean()
    except Exception:
        pass

    return result


def export_shape(shape: cq.Shape, out: Path) -> None:
    shape.exportStep(str(out))


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
    extended = 0
    skipped = 0

    for s in solids:
        if s.idx == pcb.idx:
            modified_solids.append(s.shape)
            continue

        ext_res = build_bbox_extension(
            solid=s,
            pcb=pcb,
            penetration=args.penetration,
            component_overlap=args.component_overlap,
            xy_inset_abs=args.xy_inset_abs,
            xy_inset_ratio=args.xy_inset_ratio,
            min_extension_size=args.min_extension_size,
            max_gap_to_board=args.max_gap_to_board,
        )

        if ext_res is None:
            modified_solids.append(s.shape)
            skipped += 1
            continue

        try:
            merged = safe_fuse(s.shape, ext_res.extension_shape, args.fuzzy_tol, args.glue)
        except Exception as exc:
            log(f"WARNING: component fuse failed for solid #{s.idx}: {exc}")
            modified_solids.append(s.shape)
            skipped += 1
            continue

        modified_solids.append(merged)
        extended += 1

    log(f"Extended solids: {extended}")
    log(f"Skipped solids:  {skipped}")

    if args.export_mode == "compound":
        log("Export mode: compound")
        compound = cq.Compound.makeCompound(modified_solids)
        export_shape(compound, out)
        log(f"Wrote compound STEP: {out}")
        return 0

    try:
        log("Running final global fuse...")
        result = try_build_one_body(
            modified_solids,
            fuzzy_tol=args.fuzzy_tol,
            glue=args.glue,
            report_every=args.report_every,
        )
        nsol = count_solids(result)
        if nsol != 1:
            msg = f"final fused result is not a single solid (contains {nsol} solids)"
            if args.export_mode == "onebody":
                log(f"ERROR: {msg}")
                return 3
            log(f"WARNING: {msg}; falling back to compound because export mode is auto.")
            compound = cq.Compound.makeCompound(modified_solids)
            export_shape(compound, out)
            log(f"Wrote compound STEP: {out}")
            return 0

        export_shape(result, out)
        log(f"Wrote fused one-body STEP: {out}")
        return 0
    except Exception as exc:
        if args.export_mode == "onebody":
            log(f"ERROR: final global fuse failed and onebody was required: {exc}")
            return 3

        log(f"WARNING: final global fuse failed: {exc}")
        log("Falling back to exporting a compound.")
        compound = cq.Compound.makeCompound(modified_solids)
        export_shape(compound, out)
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
