#!/usr/bin/env python3
"""
Single-file standalone STEP modifier for PCB assemblies exported from KiCad.

Features
--------
- CLI mode and GUI mode in the same file.
- GUI: pick input/output STEP files, inspect a simple 2D preview, disable
  selected components, edit all parameters, then export.
- CLI: same processing engine, with optional disabled component list.
- Extends the full XY bounding footprint of each selected component toward the
  PCB, then exports either:
    * one fused body
    * a compound
    * or auto (try one fused body, then fall back to compound)

Notes
-----
- This script uses CadQuery.
- The GUI preview is a 2D footprint preview, not a real 3D viewer.
- Component names from the original KiCad assembly are usually not available
  after the import path used here, so components are identified by solid index.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Set, Tuple

try:
    import cadquery as cq
except Exception as _cad_exc:
    cq = None  # type: ignore[assignment]
    _CADQUERY_IMPORT_ERROR = _cad_exc
else:
    _CADQUERY_IMPORT_ERROR = None


@dataclass
class SolidInfo:
    idx: int
    shape: "cq.Shape"
    bb: "cq.BoundBox"

    @property
    def area_xy(self) -> float:
        return self.bb.xlen * self.bb.ylen


@dataclass
class ExtensionResult:
    solid_idx: int
    side: str
    extension_shape: "cq.Shape"
    x: float
    y: float
    z0: float
    z1: float
    sx: float
    sy: float
    gap: float


@dataclass
class ComponentInfo:
    solid: SolidInfo
    side: str
    gap: float
    overlaps_board_xy: bool
    can_extend: bool
    label: str


@dataclass
class ProcessConfig:
    pcb_thickness: float = 1.6
    penetration: float = 1.0
    component_overlap: float = 0.25
    xy_inset_abs: float = 0.0
    xy_inset_ratio: float = 0.0
    min_extension_size: float = 0.20
    pcb_thickness_tol: float = 0.8
    fuzzy_tol: float = 0.02
    glue: bool = False
    max_gap_to_board: float = 10.0
    export_mode: str = "auto"
    report_every: int = 25


class AnchorError(RuntimeError):
    pass


def ensure_cadquery() -> None:
    if cq is None:
        raise AnchorError(
            "CadQuery is not available. Install it with: pip install cadquery\n"
            f"Original import error: {_CADQUERY_IMPORT_ERROR}"
        )


def make_logger(log_cb: Optional[Callable[[str], None]]) -> Callable[[str], None]:
    if log_cb is not None:
        return log_cb

    def _default(msg: str) -> None:
        print(msg, flush=True)

    return _default


def _iter_solids_from_obj(obj: object) -> Iterable["cq.Shape"]:
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
    ensure_cadquery()

    try:
        wp = cq.importers.importStep(str(path))
    except Exception as exc:
        raise AnchorError(f"STEP import failed: {exc}") from exc

    solids: List["cq.Shape"] = []

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

    unique: List["cq.Shape"] = []
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


def detect_pcb(solids: Sequence[SolidInfo], pcb_thickness: float, tol: float, log: Callable[[str], None]) -> SolidInfo:
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
    log("WARNING: no solid matched PCB thickness tolerance; using largest XY-area solid as PCB candidate.")
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


def _nearest_board_face(sbb: "cq.BoundBox", pbb: "cq.BoundBox") -> Tuple[str, float, float]:
    dist_to_top = abs(sbb.zmin - pbb.zmax)
    dist_to_bottom = abs(sbb.zmax - pbb.zmin)

    if dist_to_top <= dist_to_bottom:
        return "top", pbb.zmax, sbb.zmin
    return "bottom", pbb.zmin, sbb.zmax


def describe_component(solid: SolidInfo, pcb: SolidInfo, max_gap_to_board: float) -> ComponentInfo:
    sbb = solid.bb
    pbb = pcb.bb
    ox = overlap_interval(sbb.xmin, sbb.xmax, pbb.xmin, pbb.xmax)
    oy = overlap_interval(sbb.ymin, sbb.ymax, pbb.ymin, pbb.ymax)
    overlaps_xy = ox is not None and oy is not None
    side, board_face_z, component_face_z = _nearest_board_face(sbb, pbb)
    gap = abs(component_face_z - board_face_z)
    can_extend = overlaps_xy and (gap <= max_gap_to_board)
    label = (
        f"#{solid.idx} | {side:<6} | gap={gap:.3f} mm | "
        f"size={sbb.xlen:.3f} x {sbb.ylen:.3f} x {sbb.zlen:.3f}"
    )
    return ComponentInfo(
        solid=solid,
        side=side,
        gap=gap,
        overlaps_board_xy=overlaps_xy,
        can_extend=can_extend,
        label=label,
    )


def analyze_step(path: Path, cfg: ProcessConfig, log_cb: Optional[Callable[[str], None]] = None) -> Tuple[List[SolidInfo], SolidInfo, List[ComponentInfo]]:
    log = make_logger(log_cb)
    solids = import_step_as_solids(path)
    log(f"Found {len(solids)} solid(s)")
    pcb = detect_pcb(solids, cfg.pcb_thickness, cfg.pcb_thickness_tol, log)
    pbb = pcb.bb
    log(
        f"Detected PCB candidate: solid #{pcb.idx} | bbox=({pbb.xlen:.3f} x {pbb.ylen:.3f} x {pbb.zlen:.3f}) mm"
    )
    components: List[ComponentInfo] = []
    for s in solids:
        if s.idx == pcb.idx:
            continue
        components.append(describe_component(s, pcb, cfg.max_gap_to_board))
    return solids, pcb, components


def build_bbox_extension(
    solid: SolidInfo,
    pcb: SolidInfo,
    cfg: ProcessConfig,
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

    sx = shrunken_size(ovx, cfg.xy_inset_abs, cfg.xy_inset_ratio, cfg.min_extension_size)
    sy = shrunken_size(ovy, cfg.xy_inset_abs, cfg.xy_inset_ratio, cfg.min_extension_size)
    if sx <= 0 or sy <= 0:
        return None

    cx = 0.5 * (ox0 + ox1)
    cy = 0.5 * (oy0 + oy1)

    side, board_face_z, component_face_z = _nearest_board_face(sbb, pbb)
    gap = abs(component_face_z - board_face_z)
    if gap > cfg.max_gap_to_board:
        return None

    if side == "top":
        z0 = board_face_z - cfg.penetration
        z1 = component_face_z + cfg.component_overlap
    else:
        z0 = component_face_z - cfg.component_overlap
        z1 = board_face_z + cfg.penetration

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
        gap=gap,
    )


def safe_fuse(base: "cq.Shape", other: "cq.Shape", fuzzy_tol: float, glue: bool) -> "cq.Shape":
    return base.fuse(other, tol=fuzzy_tol, glue=glue)


def count_solids(shape: "cq.Shape") -> int:
    solids_method = getattr(shape, "Solids", None)
    if callable(solids_method):
        try:
            return len(list(solids_method()))
        except Exception:
            pass
    return 1


def try_build_one_body(
    shapes: Sequence["cq.Shape"],
    cfg: ProcessConfig,
    log: Callable[[str], None],
) -> "cq.Shape":
    if not shapes:
        raise AnchorError("No shapes available for export.")

    result = shapes[0]
    for i, shp in enumerate(shapes[1:], start=1):
        result = safe_fuse(result, shp, cfg.fuzzy_tol, cfg.glue)
        if cfg.report_every > 0 and (i % cfg.report_every) == 0:
            log(f"  fused {i + 1}/{len(shapes)} solids")

    try:
        result = result.clean()
    except Exception:
        pass

    return result


def export_shape(shape: "cq.Shape", out: Path) -> None:
    shape.exportStep(str(out))


def run_process(
    input_path: Path,
    output_path: Path,
    cfg: ProcessConfig,
    disabled_indices: Optional[Set[int]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> int:
    ensure_cadquery()
    log = make_logger(log_cb)

    if disabled_indices is None:
        disabled_indices = set()

    if not input_path.exists():
        raise AnchorError(f"Input file does not exist: {input_path}")

    log(f"Loading STEP: {input_path}")
    solids, pcb, _ = analyze_step(input_path, cfg, log)

    modified_solids: List["cq.Shape"] = []
    extended = 0
    skipped = 0
    disabled = 0

    for s in solids:
        if s.idx == pcb.idx:
            modified_solids.append(s.shape)
            continue

        if s.idx in disabled_indices:
            disabled += 1
            continue

        ext_res = build_bbox_extension(s, pcb, cfg)
        if ext_res is None:
            modified_solids.append(s.shape)
            skipped += 1
            continue

        try:
            merged = safe_fuse(s.shape, ext_res.extension_shape, cfg.fuzzy_tol, cfg.glue)
        except Exception as exc:
            log(f"WARNING: component fuse failed for solid #{s.idx}: {exc}")
            modified_solids.append(s.shape)
            skipped += 1
            continue

        modified_solids.append(merged)
        extended += 1

    log(f"Extended solids: {extended}")
    log(f"Skipped solids:  {skipped}")
    log(f"Disabled solids: {disabled}")

    if cfg.export_mode == "compound":
        log("Export mode: compound")
        compound = cq.Compound.makeCompound(modified_solids)
        export_shape(compound, output_path)
        log(f"Wrote compound STEP: {output_path}")
        return 0

    try:
        log("Running final global fuse...")
        result = try_build_one_body(modified_solids, cfg, log)
        nsol = count_solids(result)
        if nsol != 1:
            msg = f"final fused result is not a single solid (contains {nsol} solids)"
            if cfg.export_mode == "onebody":
                raise AnchorError(msg)
            log(f"WARNING: {msg}; falling back to compound because export mode is auto.")
            compound = cq.Compound.makeCompound(modified_solids)
            export_shape(compound, output_path)
            log(f"Wrote compound STEP: {output_path}")
            return 0

        export_shape(result, output_path)
        log(f"Wrote fused one-body STEP: {output_path}")
        return 0
    except Exception as exc:
        if cfg.export_mode == "onebody":
            raise AnchorError(f"Final global fuse failed and onebody was required: {exc}") from exc

        log(f"WARNING: final global fuse failed: {exc}")
        log("Falling back to exporting a compound.")
        compound = cq.Compound.makeCompound(modified_solids)
        export_shape(compound, output_path)
        log(f"Wrote compound STEP: {output_path}")
        return 0


def parse_disable_list(text: str) -> Set[int]:
    out: Set[int] = set()
    text = text.strip()
    if not text:
        return out
    for part in text.replace(";", ",").split(","):
        p = part.strip()
        if not p:
            continue
        if "-" in p:
            a, b = p.split("-", 1)
            start = int(a.strip())
            end = int(b.strip())
            lo, hi = sorted((start, end))
            out.update(range(lo, hi + 1))
        else:
            out.add(int(p))
    return out


def format_disable_list(indices: Set[int]) -> str:
    if not indices:
        return ""
    vals = sorted(indices)
    return ",".join(str(v) for v in vals)


def parse_cli_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Attach STEP solids to a PCB by extending each selected component bounding footprint toward the board."
    )
    p.add_argument("--input", help="Input STEP file")
    p.add_argument("--output", help="Output STEP file")
    p.add_argument("--pcb-thickness", type=float, default=1.6)
    p.add_argument("--penetration", type=float, default=1.0)
    p.add_argument("--component-overlap", type=float, default=0.25)
    p.add_argument("--xy-inset-abs", type=float, default=0.0)
    p.add_argument("--xy-inset-ratio", type=float, default=0.0)
    p.add_argument("--min-extension-size", type=float, default=0.20)
    p.add_argument("--pcb-thickness-tol", type=float, default=0.8)
    p.add_argument("--fuzzy-tol", type=float, default=0.02)
    p.add_argument("--glue", action="store_true")
    p.add_argument("--max-gap-to-board", type=float, default=10.0)
    p.add_argument("--export-mode", choices=("auto", "onebody", "compound"), default="auto")
    p.add_argument("--report-every", type=int, default=25)
    p.add_argument(
        "--disable",
        default="",
        help="Comma-separated solid indices to disable, e.g. 1,2,5-9",
    )
    p.add_argument(
        "--list-components",
        action="store_true",
        help="Analyze the STEP file and print component indices/info without exporting",
    )
    p.add_argument("--gui", action="store_true", help="Launch the GUI")
    return p.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> ProcessConfig:
    return ProcessConfig(
        pcb_thickness=args.pcb_thickness,
        penetration=args.penetration,
        component_overlap=args.component_overlap,
        xy_inset_abs=args.xy_inset_abs,
        xy_inset_ratio=args.xy_inset_ratio,
        min_extension_size=args.min_extension_size,
        pcb_thickness_tol=args.pcb_thickness_tol,
        fuzzy_tol=args.fuzzy_tol,
        glue=args.glue,
        max_gap_to_board=args.max_gap_to_board,
        export_mode=args.export_mode,
        report_every=args.report_every,
    )


def cli_main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_cli_args(argv)

    if args.gui:
        launch_gui()
        return 0

    if not args.input:
        raise AnchorError("CLI mode requires --input, or launch without arguments to open the GUI.")

    cfg = config_from_args(args)
    input_path = Path(args.input)

    if args.list_components:
        solids, pcb, components = analyze_step(input_path, cfg)
        print(f"PCB solid: #{pcb.idx}")
        for comp in components:
            status = "OK" if comp.can_extend else "SKIP"
            print(f"{status:4} {comp.label}")
        return 0

    if not args.output:
        raise AnchorError("CLI mode requires --output.")

    disabled_indices = parse_disable_list(args.disable)
    return run_process(Path(args.input), Path(args.output), cfg, disabled_indices)


# ---------------- GUI ----------------

def launch_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class App(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title("KiAnchor Step Modifier")
            self.geometry("1400x860")
            self.minsize(1200, 760)

            self.solids: List[SolidInfo] = []
            self.pcb: Optional[SolidInfo] = None
            self.components: List[ComponentInfo] = []
            self.disabled_indices: Set[int] = set()
            self.selected_component_idx: Optional[int] = None
            self._busy = False
            self._worker_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

            self.input_var = tk.StringVar()
            self.output_var = tk.StringVar()
            self.pcb_thickness_var = tk.StringVar(value="1.6")
            self.penetration_var = tk.StringVar(value="1.0")
            self.component_overlap_var = tk.StringVar(value="0.25")
            self.xy_inset_abs_var = tk.StringVar(value="0.0")
            self.xy_inset_ratio_var = tk.StringVar(value="0.0")
            self.min_extension_size_var = tk.StringVar(value="0.20")
            self.pcb_thickness_tol_var = tk.StringVar(value="0.8")
            self.fuzzy_tol_var = tk.StringVar(value="0.02")
            self.glue_var = tk.BooleanVar(value=False)
            self.max_gap_to_board_var = tk.StringVar(value="10.0")
            self.export_mode_var = tk.StringVar(value="auto")
            self.report_every_var = tk.StringVar(value="25")
            self.show_labels_var = tk.BooleanVar(value=True)
            self.show_top_var = tk.BooleanVar(value=True)
            self.show_bottom_var = tk.BooleanVar(value=True)

            self._build_ui()
            self.after(100, self._process_worker_queue)

        def _build_ui(self) -> None:
            outer = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
            outer.pack(fill=tk.BOTH, expand=True)

            left = ttk.Frame(outer, padding=8)
            right = ttk.Frame(outer, padding=8)
            outer.add(left, weight=3)
            outer.add(right, weight=2)

            # Left side: file controls + preview + log
            file_frame = ttk.LabelFrame(left, text="Files", padding=8)
            file_frame.pack(fill=tk.X)

            style = ttk.Style(self)
            try:
                base_font = ("TkDefaultFont", 11, "bold")
                style.configure("Big.TButton", padding=(18, 12), font=base_font)
            except Exception:
                style.configure("Big.TButton", padding=(18, 12))

            ttk.Label(file_frame, text="Input STEP").grid(row=0, column=0, sticky="w")
            ttk.Entry(file_frame, textvariable=self.input_var).grid(row=0, column=1, sticky="ew", padx=4)
            self.input_browse_button = ttk.Button(file_frame, text="Browse...", command=self._browse_input)
            self.input_browse_button.grid(row=0, column=2, padx=4)
            self.load_button = ttk.Button(file_frame, text="Load", command=self._load_step)
            self.load_button.grid(row=0, column=3, padx=4)

            ttk.Label(file_frame, text="Output STEP").grid(row=1, column=0, sticky="w")
            ttk.Entry(file_frame, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", padx=4)
            self.output_browse_button = ttk.Button(file_frame, text="Browse...", command=self._browse_output)
            self.output_browse_button.grid(row=1, column=2, padx=4)
            self.export_button = ttk.Button(file_frame, text="EXPORT STEP", command=self._export_step, style="Big.TButton")
            self.export_button.grid(row=1, column=3, padx=4, pady=2, ipadx=8, ipady=4, sticky="nsew")

            file_frame.columnconfigure(1, weight=1)
            file_frame.columnconfigure(3, weight=0, minsize=170)

            preview_ctrl = ttk.Frame(left)
            preview_ctrl.pack(fill=tk.X, pady=(8, 0))
            ttk.Checkbutton(preview_ctrl, text="Show top", variable=self.show_top_var, command=self._redraw_preview).pack(side=tk.LEFT)
            ttk.Checkbutton(preview_ctrl, text="Show bottom", variable=self.show_bottom_var, command=self._redraw_preview).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Checkbutton(preview_ctrl, text="Show labels", variable=self.show_labels_var, command=self._redraw_preview).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Label(preview_ctrl, text="Preview: top-down XY board/components").pack(side=tk.RIGHT)

            preview_frame = ttk.LabelFrame(left, text="Board preview", padding=4)
            preview_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
            self.preview_canvas = tk.Canvas(preview_frame, bg="white", highlightthickness=0)
            self.preview_canvas.pack(fill=tk.BOTH, expand=True)
            self.preview_canvas.bind("<Configure>", lambda event: self._redraw_preview())

            log_frame = ttk.LabelFrame(left, text="Log", padding=4)
            log_frame.pack(fill=tk.BOTH, expand=False, pady=(8, 0))
            self.log_text = tk.Text(log_frame, height=10, wrap="word")
            self.log_text.pack(fill=tk.BOTH, expand=True)

            # Right side: params + components
            params_frame = ttk.LabelFrame(right, text="Parameters", padding=8)
            params_frame.pack(fill=tk.X)

            rows = [
                ("PCB thickness", self.pcb_thickness_var),
                ("Penetration", self.penetration_var),
                ("Component overlap", self.component_overlap_var),
                ("XY inset abs", self.xy_inset_abs_var),
                ("XY inset ratio", self.xy_inset_ratio_var),
                ("Min extension size", self.min_extension_size_var),
                ("PCB thickness tol", self.pcb_thickness_tol_var),
                ("Fuzzy tol", self.fuzzy_tol_var),
                ("Max gap to board", self.max_gap_to_board_var),
                ("Report every", self.report_every_var),
            ]
            for r, (label, var) in enumerate(rows):
                ttk.Label(params_frame, text=label).grid(row=r, column=0, sticky="w", pady=2)
                ttk.Entry(params_frame, textvariable=var, width=14).grid(row=r, column=1, sticky="ew", pady=2, padx=4)

            ttk.Label(params_frame, text="Export mode").grid(row=len(rows), column=0, sticky="w", pady=2)
            export_box = ttk.Combobox(
                params_frame,
                textvariable=self.export_mode_var,
                values=("auto", "onebody", "compound"),
                state="readonly",
                width=12,
            )
            export_box.grid(row=len(rows), column=1, sticky="ew", pady=2, padx=4)

            ttk.Checkbutton(params_frame, text="Glue fuse", variable=self.glue_var).grid(
                row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=4
            )

            params_frame.columnconfigure(1, weight=1)

            comp_frame = ttk.LabelFrame(right, text="Components", padding=8)
            comp_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

            toolbar = ttk.Frame(comp_frame)
            toolbar.pack(fill=tk.X, pady=(0, 6))
            self.disable_button = ttk.Button(toolbar, text="Disable selected", command=self._disable_selected)
            self.disable_button.pack(side=tk.LEFT)
            self.enable_button = ttk.Button(toolbar, text="Enable selected", command=self._enable_selected)
            self.enable_button.pack(side=tk.LEFT, padx=4)
            self.enable_all_button = ttk.Button(toolbar, text="Enable all", command=self._enable_all)
            self.enable_all_button.pack(side=tk.LEFT, padx=4)
            self.refresh_button = ttk.Button(toolbar, text="Refresh analysis", command=self._reload_analysis_only)
            self.refresh_button.pack(side=tk.LEFT, padx=4)

            columns = ("enabled", "id", "side", "gap", "size", "status")
            self.tree = ttk.Treeview(comp_frame, columns=columns, show="headings", selectmode="extended")
            for name, width in [
                ("enabled", 70),
                ("id", 60),
                ("side", 70),
                ("gap", 90),
                ("size", 180),
                ("status", 80),
            ]:
                self.tree.heading(name, text=name)
                self.tree.column(name, width=width, anchor="center")
            self.tree.pack(fill=tk.BOTH, expand=True)
            self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
            self.tree.bind("<Double-1>", self._on_tree_double_click)

        def _log(self, msg: str) -> None:
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.update_idletasks()

        def _set_busy(self, busy: bool, status_text: str = "") -> None:
            self._busy = busy
            state = "disabled" if busy else "normal"
            for widget_name in (
                "input_browse_button",
                "output_browse_button",
                "load_button",
                "export_button",
                "disable_button",
                "enable_button",
                "enable_all_button",
                "refresh_button",
            ):
                widget = getattr(self, widget_name, None)
                if widget is not None:
                    try:
                        widget.configure(state=state)
                    except Exception:
                        pass
            try:
                self.tree.configure(selectmode=("none" if busy else "extended"))
            except Exception:
                pass
            try:
                self.configure(cursor=("watch" if busy else ""))
            except Exception:
                pass
            if status_text:
                self._log(status_text)

        def _process_worker_queue(self) -> None:
            try:
                while True:
                    kind, payload = self._worker_queue.get_nowait()
                    if kind == "log":
                        self._log(str(payload))
                    elif kind == "done":
                        callback = payload
                        self._set_busy(False)
                        if callable(callback):
                            callback()
                    elif kind == "error":
                        message, details = payload  # type: ignore[misc]
                        self._set_busy(False)
                        self._log(f"ERROR: {message}")
                        if details:
                            self._log(details)
                        messagebox.showerror("Operation failed", str(message))
            except queue.Empty:
                pass
            self.after(100, self._process_worker_queue)

        def _run_worker(self, start_message: str, target: Callable[[Callable[[str], None]], Optional[Callable[[], None]]]) -> None:
            if self._busy:
                return
            self._set_busy(True, start_message)

            def worker() -> None:
                def qlog(msg: str) -> None:
                    self._worker_queue.put(("log", msg))

                try:
                    callback = target(qlog)
                except Exception as exc:
                    self._worker_queue.put(("error", (str(exc), traceback.format_exc())))
                    return
                self._worker_queue.put(("done", callback))

            threading.Thread(target=worker, daemon=True).start()

        def _browse_input(self) -> None:
            path = filedialog.askopenfilename(
                title="Select input STEP file",
                filetypes=[("STEP files", "*.step *.stp"), ("All files", "*.*")],
            )
            if path:
                self.input_var.set(path)
                p = Path(path)
                default_out = p.with_name(f"{p.stem}_anchored.step")
                self.output_var.set(str(default_out))

        def _browse_output(self) -> None:
            path = filedialog.asksaveasfilename(
                title="Select output STEP file",
                defaultextension=".step",
                filetypes=[("STEP files", "*.step *.stp"), ("All files", "*.*")],
            )
            if path:
                self.output_var.set(path)

        def _cfg_from_gui(self) -> ProcessConfig:
            try:
                return ProcessConfig(
                    pcb_thickness=float(self.pcb_thickness_var.get()),
                    penetration=float(self.penetration_var.get()),
                    component_overlap=float(self.component_overlap_var.get()),
                    xy_inset_abs=float(self.xy_inset_abs_var.get()),
                    xy_inset_ratio=float(self.xy_inset_ratio_var.get()),
                    min_extension_size=float(self.min_extension_size_var.get()),
                    pcb_thickness_tol=float(self.pcb_thickness_tol_var.get()),
                    fuzzy_tol=float(self.fuzzy_tol_var.get()),
                    glue=bool(self.glue_var.get()),
                    max_gap_to_board=float(self.max_gap_to_board_var.get()),
                    export_mode=self.export_mode_var.get(),
                    report_every=int(self.report_every_var.get()),
                )
            except Exception as exc:
                raise AnchorError(f"Invalid GUI parameter value: {exc}") from exc

        def _load_step(self) -> None:
            try:
                cfg = self._cfg_from_gui()
                input_path = Path(self.input_var.get().strip())
                if not input_path:
                    raise AnchorError("Pick an input STEP file first.")
            except Exception as exc:
                messagebox.showerror("Load failed", str(exc))
                self._log(f"ERROR: {exc}")
                return

            def task(log_cb: Callable[[str], None]) -> Callable[[], None]:
                solids, pcb, components = analyze_step(input_path, cfg, log_cb)

                def apply() -> None:
                    self.solids = solids
                    self.pcb = pcb
                    self.components = components
                    self.disabled_indices = set()
                    self.selected_component_idx = None
                    self._fill_tree()
                    self._redraw_preview()

                return apply

            self._run_worker(f"Loading: {input_path}", task)

        def _reload_analysis_only(self) -> None:
            if not self.input_var.get().strip():
                return
            prev_disabled = set(self.disabled_indices)
            prev_selected = self.selected_component_idx
            try:
                cfg = self._cfg_from_gui()
                input_path = Path(self.input_var.get().strip())
            except Exception as exc:
                messagebox.showerror("Refresh failed", str(exc))
                self._log(f"ERROR: {exc}")
                return

            def task(log_cb: Callable[[str], None]) -> Callable[[], None]:
                solids, pcb, components = analyze_step(input_path, cfg, log_cb)
                valid_indices = {c.solid.idx for c in components}
                new_disabled = {i for i in prev_disabled if i in valid_indices}
                new_selected = prev_selected if prev_selected in valid_indices else None

                def apply() -> None:
                    self.solids = solids
                    self.pcb = pcb
                    self.components = components
                    self.disabled_indices = new_disabled
                    self.selected_component_idx = new_selected
                    self._fill_tree()
                    self._redraw_preview()

                return apply

            self._run_worker("Refreshing analysis...", task)

        def _fill_tree(self) -> None:
            for item in self.tree.get_children():
                self.tree.delete(item)

            for comp in self.components:
                sbb = comp.solid.bb
                enabled = "yes" if comp.solid.idx not in self.disabled_indices else "no"
                status = "OK" if comp.can_extend else "skip"
                iid = str(comp.solid.idx)
                self.tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(
                        enabled,
                        comp.solid.idx,
                        comp.side,
                        f"{comp.gap:.3f}",
                        f"{sbb.xlen:.2f}x{sbb.ylen:.2f}x{sbb.zlen:.2f}",
                        status,
                    ),
                )
                if self.selected_component_idx == comp.solid.idx:
                    self.tree.selection_add(iid)

        def _selected_indices(self) -> List[int]:
            out: List[int] = []
            for item in self.tree.selection():
                try:
                    out.append(int(item))
                except Exception:
                    pass
            return out

        def _disable_selected(self) -> None:
            if self._busy:
                return
            for idx in self._selected_indices():
                self.disabled_indices.add(idx)
            self._fill_tree()
            self._redraw_preview()

        def _enable_selected(self) -> None:
            if self._busy:
                return
            for idx in self._selected_indices():
                self.disabled_indices.discard(idx)
            self._fill_tree()
            self._redraw_preview()

        def _enable_all(self) -> None:
            if self._busy:
                return
            self.disabled_indices.clear()
            self._fill_tree()
            self._redraw_preview()

        def _on_tree_select(self, event: object = None) -> None:
            if self._busy:
                return
            sels = self._selected_indices()
            self.selected_component_idx = sels[0] if sels else None
            self._redraw_preview()

        def _on_tree_double_click(self, event: object = None) -> None:
            if self._busy:
                return
            sels = self._selected_indices()
            for idx in sels:
                if idx in self.disabled_indices:
                    self.disabled_indices.discard(idx)
                else:
                    self.disabled_indices.add(idx)
            self._fill_tree()
            self._redraw_preview()

        def _component_color(self, comp: ComponentInfo) -> Tuple[str, str]:
            if comp.solid.idx in self.disabled_indices:
                return "#d9d9d9", "#888888"
            if comp.side == "top":
                return "#9ec5ff", "#2f5aa8"
            return "#ffd29b", "#a86216"

        def _redraw_preview(self) -> None:
            self.preview_canvas.delete("all")
            if self.pcb is None:
                self.preview_canvas.create_text(
                    20, 20, anchor="nw", text="Load a STEP file to see the preview.", fill="#666"
                )
                return

            pbb = self.pcb.bb
            canvas_w = max(100, self.preview_canvas.winfo_width())
            canvas_h = max(100, self.preview_canvas.winfo_height())
            margin = 30

            dx = max(pbb.xlen, 1e-6)
            dy = max(pbb.ylen, 1e-6)
            scale = min((canvas_w - 2 * margin) / dx, (canvas_h - 2 * margin) / dy)
            scale = max(scale, 1e-6)

            def map_xy(x: float, y: float) -> Tuple[float, float]:
                px = margin + (x - pbb.xmin) * scale
                py = canvas_h - margin - (y - pbb.ymin) * scale
                return px, py

            x0, y0 = map_xy(pbb.xmin, pbb.ymin)
            x1, y1 = map_xy(pbb.xmax, pbb.ymax)
            self.preview_canvas.create_rectangle(x0, y1, x1, y0, fill="#f6f6f6", outline="black", width=2)

            self.preview_canvas.create_text(
                10, 10, anchor="nw",
                text=f"PCB #{self.pcb.idx} | {pbb.xlen:.2f} x {pbb.ylen:.2f} x {pbb.zlen:.2f} mm",
                fill="#111"
            )

            show_top = bool(self.show_top_var.get())
            show_bottom = bool(self.show_bottom_var.get())
            show_labels = bool(self.show_labels_var.get())

            for comp in self.components:
                if comp.side == "top" and not show_top:
                    continue
                if comp.side == "bottom" and not show_bottom:
                    continue

                bb = comp.solid.bb
                rx0 = max(bb.xmin, pbb.xmin)
                rx1 = min(bb.xmax, pbb.xmax)
                ry0 = max(bb.ymin, pbb.ymin)
                ry1 = min(bb.ymax, pbb.ymax)
                if rx1 <= rx0 or ry1 <= ry0:
                    continue

                px0, py0 = map_xy(rx0, ry0)
                px1, py1 = map_xy(rx1, ry1)
                fill, outline = self._component_color(comp)
                width = 3 if self.selected_component_idx == comp.solid.idx else 1
                self.preview_canvas.create_rectangle(px0, py1, px1, py0, fill=fill, outline=outline, width=width)

                if show_labels and ((px1 - px0) >= 28) and ((py0 - py1) >= 16):
                    self.preview_canvas.create_text(
                        (px0 + px1) * 0.5,
                        (py0 + py1) * 0.5,
                        text=str(comp.solid.idx),
                        fill="black",
                    )

            legend_y = canvas_h - 20
            self.preview_canvas.create_rectangle(12, legend_y - 8, 26, legend_y + 6, fill="#9ec5ff", outline="#2f5aa8")
            self.preview_canvas.create_text(34, legend_y, anchor="w", text="top", fill="#111")
            self.preview_canvas.create_rectangle(90, legend_y - 8, 104, legend_y + 6, fill="#ffd29b", outline="#a86216")
            self.preview_canvas.create_text(112, legend_y, anchor="w", text="bottom", fill="#111")
            self.preview_canvas.create_rectangle(190, legend_y - 8, 204, legend_y + 6, fill="#d9d9d9", outline="#888888")
            self.preview_canvas.create_text(212, legend_y, anchor="w", text="disabled", fill="#111")

        def _export_step(self) -> None:
            try:
                cfg = self._cfg_from_gui()
                input_path = Path(self.input_var.get().strip())
                output_path = Path(self.output_var.get().strip())

                if not input_path:
                    raise AnchorError("Pick an input STEP file first.")
                if not output_path:
                    raise AnchorError("Pick an output STEP file first.")
            except Exception as exc:
                messagebox.showerror("Export failed", str(exc))
                self._log(f"ERROR: {exc}")
                return

            disabled_text = format_disable_list(self.disabled_indices) or "(none)"

            def task(log_cb: Callable[[str], None]) -> Callable[[], None]:
                log_cb(f"Disabled solids (excluded from output): {disabled_text}")
                rc = run_process(input_path, output_path, cfg, set(self.disabled_indices), log_cb)

                def apply() -> None:
                    if rc == 0:
                        messagebox.showinfo("Done", f"Exported:\n{output_path}")
                    else:
                        messagebox.showwarning("Done with issues", f"Process returned code {rc}")

                return apply

            self._run_worker("Exporting STEP...", task)

    ensure_cadquery()
    app = App()
    app.mainloop()


def main() -> int:
    if len(sys.argv) == 1:
        launch_gui()
        return 0
    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", flush=True)
        raise SystemExit(130)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
