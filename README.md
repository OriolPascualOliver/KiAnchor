# BoardBond

Standalone Python tool to modify KiCad-exported PCB STEP assemblies for 3D printing.

BoardBond extends each selected component body **toward the PCB** so the printed model has stronger attachment between the board and the parts. It supports both:

- **GUI mode** for interactive inspection and export
- **CLI mode** for batch use or scripting

## What it does

Given a STEP assembly:

- detects the PCB automatically
- analyzes every other solid as a component candidate
- extends the **full XY bounding footprint** of each enabled component toward the PCB
- optionally exports as:
  - **one fused body**
  - **compound**
  - **auto**: try one body, fall back to compound
- allows selected components to be **disabled**, which means they are **excluded from the exported output file**

## Current behavior

- Works on **both PCB sides**
- GUI preview is a **2D top-down board/components view**, not a 3D viewer
- Components are identified by **solid index** because names from the original KiCad STEP assembly are often lost in the import path
- GUI load/export runs in a background thread so the window stays responsive

## Requirements

- Python **3.10+** recommended
- `cadquery`
- `tkinter` for the GUI

## Installation

### 1. Install Python
Use a normal Python installation.

### 2. Install CadQuery

```bash
pip install cadquery
```

### 3. Download the script
Use the single file:

- `boardbond_v2.py`

You can rename it to `boardbond.py` if you want.

## Launching

### GUI mode
If you run the script with no arguments, it opens the GUI:

```bash
python boardbond_v2.py
```

You can also force GUI mode:

```bash
python boardbond_v2.py --gui
```

### CLI mode

```bash
python boardbond_v2.py --input input.step --output output.step
```

## GUI workflow

1. Open the script
2. Select an **input STEP** file
3. Select an **output STEP** file
4. Click **Load**
5. Inspect the 2D preview and component table
6. Disable any components you do not want to export
7. Adjust parameters
8. Click **EXPORT STEP**

### GUI controls

- **Browse...**: select input/output STEP files
- **Load**: import and analyze the STEP file
- **EXPORT STEP**: generate the modified STEP output
- **Disable selected**: exclude selected components from output
- **Enable selected**: re-enable selected components
- **Enable all**: include all components again
- **Refresh analysis**: re-run analysis after parameter changes

### Preview legend

- **Blue** = top-side components
- **Orange** = bottom-side components
- **Gray** = disabled components

### Component table columns

- `enabled` — whether the component will be included in export
- `id` — solid index
- `side` — top or bottom
- `gap` — estimated distance to nearest PCB face
- `size` — bounding box dimensions
- `status` — whether extension is possible under current rules

## CLI usage

### Basic export

```bash
python boardbond_v2.py \
  --input SmartKnob.step \
  --output SmartKnob_anchored.step
```

### Require a single fused body

```bash
python boardbond_v2.py \
  --input SmartKnob.step \
  --output SmartKnob_anchored.step \
  --export-mode onebody
```

### Force compound export

```bash
python boardbond_v2.py \
  --input SmartKnob.step \
  --output SmartKnob_anchored.step \
  --export-mode compound
```

### List detected components without exporting

```bash
python boardbond_v2.py --input SmartKnob.step --list-components
```

### Disable components by solid index

```bash
python boardbond_v2.py \
  --input SmartKnob.step \
  --output SmartKnob_anchored.step \
  --disable 3,7,12-18
```

Disabled solids are **not rendered into the output file**.

## CLI arguments

| Argument | Default | Meaning |
|---|---:|---|
| `--input` | - | Input STEP file |
| `--output` | - | Output STEP file |
| `--pcb-thickness` | `1.6` | Nominal PCB thickness in mm |
| `--penetration` | `1.0` | How far the extension penetrates into the PCB |
| `--component-overlap` | `0.25` | How far the extension overlaps back into the component |
| `--xy-inset-abs` | `0.0` | Absolute inset from the component XY overlap |
| `--xy-inset-ratio` | `0.0` | Relative inset from the component XY overlap |
| `--min-extension-size` | `0.20` | Minimum extension width/height |
| `--pcb-thickness-tol` | `0.8` | Tolerance used for PCB detection |
| `--fuzzy-tol` | `0.02` | Boolean fuse tolerance |
| `--glue` | off | Enable glue mode during boolean fuse |
| `--max-gap-to-board` | `10.0` | Skip parts too far away from the PCB |
| `--export-mode` | `auto` | `auto`, `onebody`, or `compound` |
| `--report-every` | `25` | Log progress every N fuse operations |
| `--disable` | empty | Comma-separated solid indices or ranges |
| `--list-components` | off | Analyze only, do not export |
| `--gui` | off | Launch GUI |

## Export modes

### `auto`
- tries to fuse everything into one body
- if final fuse fails, exports a compound instead

### `onebody`
- requires the final result to be one fused solid
- fails if that cannot be achieved

### `compound`
- exports a multi-solid compound directly
- useful when global fusing is too fragile

## Known limitations

This tool is intentionally blunt. It is not doing semantic ECAD analysis.

- It works from **STEP geometry only**
- It does **not** know the actual footprint name or reference designator reliably
- The extension is based on **bounding boxes / XY overlap**, not exact package faces
- Large odd-shaped components, connectors, or very messy imported STEP geometry may produce ugly but printable results
- The preview is **2D only**
- PCB detection is heuristic; if the wrong solid is chosen as the board, results will be wrong

## Troubleshooting

### `CadQuery is not available`
Install it:

```bash
pip install cadquery
```

### GUI does not open
Make sure your Python installation includes `tkinter`.

### Some components extend in the wrong direction
That usually means the wrong solid was detected as the PCB, or the geometry is unusual enough that the nearest-face heuristic fails.

### Final one-body export fails
Try one of these:

```bash
--export-mode auto
```

or

```bash
--export-mode compound
```

You can also try tuning:

```bash
--fuzzy-tol 0.01
```

or

```bash
--component-overlap 0.35
```

### A component should be hidden entirely
Disable it in the GUI or use:

```bash
--disable 5,9,11-14
```

Disabled components are excluded from the final output STEP.

## Suggested repository structure

```text
BoardBond/
├─ boardbond_v2.py
├─ README.md
└─ examples/
   ├─ input.step
   └─ output.step
```

## Summary

BoardBond is a practical geometry tool for preparing PCB STEP assemblies for 3D printing when you need components to be more strongly attached to the board.

It is not elegant CAD reconstruction. It is a controlled geometry hack designed to get a printable result.
