# KiAnchor

Standalone tool to modify KiCad-exported PCB STEP assemblies for 3D printing.

KiAnchor extends each selected component body **toward the PCB** so the printed model has stronger attachment between the board and the parts. It supports both:

- **GUI mode** for interactive inspection and export
- **CLI mode** for batch use or scripting
- **Windows `.exe` releases** for users who do not want to install Python manually

## What it does

Given a STEP assembly:

- detects the PCB automatically
- analyzes every other solid as a component candidate
- extends the **full XY bounding footprint** of each enabled component toward the PCB
- supports both **top-side** and **bottom-side** components
- optionally exports as:
  - **one fused body**
  - **compound**
  - **auto**: try one body, fall back to compound
- allows selected components to be **disabled**, which means they are **excluded from the exported output file**

## Current behavior

- Works on **both PCB sides**
- GUI preview is a **2D top-down board/components view**, not a 3D viewer
- Components are identified by **solid index** because names from the original KiCad STEP assembly are often lost in the import path
- GUI load/export runs in a **background thread** so the window stays responsive
- GUI includes a built-in **log panel** showing load, analysis, warnings, progress, and export messages

## Requirements

### Python version

- Python **3.10+** recommended

### Python dependencies

- `cadquery`
- `tkinter` for the GUI

`tkinter` is included with most standard Python installers on Windows.

## Installation

### Option 1: Run from Python

Install dependencies:

```bash
pip install cadquery
```

Then use the single script:

- `KiAnchor.py`

You can rename it to `KiAnchor.py` if you want.

### Option 2: Use the Windows `.exe`

If you download a release build:

- no Python installation is required
- no manual `pip install` is required
- you can run the GUI directly by opening the `.exe`
- you can also run it from Command Prompt with CLI arguments

Depending on how the release is packaged, you may get either:

- a single `KiAnchor.exe`
- or a `KiAnchor/` folder containing `KiAnchor.exe` and bundled runtime files

If the release is a `.zip`, extract it first before running.

## Launching

### GUI mode (Python)

If you run the script with no arguments, it opens the GUI:

```bash
python KiAnchor.py
```

You can also force GUI mode:

```bash
python KiAnchor.py --gui
```

### GUI mode (`.exe`)

Double-click the `.exe`, or run:

```bat
KiAnchor.exe
```

### CLI mode (Python)

```bash
python KiAnchor.py --input input.step --output output.step
```

### CLI mode (`.exe`)

```bat
KiAnchor.exe --input input.step --output output.step
```

## GUI workflow

1. Select an **input STEP** file
2. Select an **output STEP** file
3. Click **Load**
4. Inspect the 2D preview and component table
5. Disable any components you do not want to export
6. Adjust parameters
7. Click **EXPORT STEP**
8. Watch the **log panel** for warnings, progress, and export status

## GUI controls

- **Browse...**: select input/output STEP files
- **Load**: import and analyze the STEP file
- **EXPORT STEP**: generate the modified STEP output
- **Disable selected**: exclude selected components from output
- **Enable selected**: re-enable selected components
- **Enable all**: include all components again
- **Refresh analysis**: re-run analysis after parameter changes

## Log panel

The GUI includes a built-in **Log** area.

It reports things such as:

- STEP loading messages
- number of solids found
- PCB detection results
- skipped or disabled solids
- per-stage processing messages
- warnings when fuse operations fail
- fallback messages when `auto` switches from `onebody` to `compound`
- final output path and export mode used
- GUI-side errors when analysis or export fails

The CLI prints similar messages to standard output.

## Preview legend

- **Blue** = top-side components
- **Orange** = bottom-side components
- **Gray** = disabled components

## Component table columns

- `enabled` — whether the component will be included in export
- `id` — solid index
- `side` — top or bottom
- `gap` — estimated distance to nearest PCB face
- `size` — bounding box dimensions
- `status` — whether extension is possible under current rules

## Disabled components

Disabled components are **not rendered into the output STEP file**.

That means:

- they are not extended
- they are not kept as original solids
- they are omitted completely from the exported result

The PCB itself is still exported.

## CLI usage

### Basic export

```bash
python KiAnchor.py \
  --input SmartKnob.step \
  --output SmartKnob_anchored.step
```

### Require a single fused body

```bash
python KiAnchor.py \
  --input SmartKnob.step \
  --output SmartKnob_anchored.step \
  --export-mode onebody
```

### Force compound export

```bash
python KiAnchor.py \
  --input SmartKnob.step \
  --output SmartKnob_anchored.step \
  --export-mode compound
```

### List detected components without exporting

```bash
python KiAnchor.py --input SmartKnob.step --list-components
```

### Disable components by solid index

```bash
python KiAnchor.py \
  --input SmartKnob.step \
  --output SmartKnob_anchored.step \
  --disable 3,7,12-18
```

### Same examples with the `.exe`

```bat
KiAnchor.exe --input SmartKnob.step --output SmartKnob_anchored.step
```

```bat
KiAnchor.exe --input SmartKnob.step --output SmartKnob_anchored.step --export-mode onebody
```

```bat
KiAnchor.exe --input SmartKnob.step --list-components
```

```bat
KiAnchor.exe --input SmartKnob.step --output SmartKnob_anchored.step --disable 3,7,12-18
```

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
- logs a warning and the fallback decision

### `onebody`

- requires the final result to be one fused solid
- fails if that cannot be achieved

### `compound`

- exports a multi-solid compound directly
- useful when global fusing is too fragile

## Windows release notes

The `.exe` build is intended to expose the **same functionality** as the Python script:

- GUI mode
- CLI mode
- component disabling
- parameter editing
- log output
- export modes

What the `.exe` does **not** change:

- PCB detection is still heuristic
- preview is still 2D
- geometry quality still depends on the STEP model and CadQuery/OpenCascade behavior

## Known limitations

This tool is intentionally blunt. It is not doing semantic ECAD analysis.

- It works from **STEP geometry only**
- It does **not** know the actual footprint name or reference designator reliably
- The extension is based on **bounding boxes / XY overlap**, not exact package faces
- Large odd-shaped components, connectors, or very messy imported STEP geometry may produce ugly but printable results
- The preview is **2D only**
- PCB detection is heuristic; if the wrong solid is chosen as the board, results will be wrong
- A one-body export may still fail on difficult geometry

## Troubleshooting

### `CadQuery is not available`

Install it:

```bash
pip install cadquery
```

### GUI does not open

Make sure your Python installation includes `tkinter`.

### The GUI freezes or does not respond

Load/export is threaded in the current GUI, so a completely frozen window usually indicates:

- a CAD operation hung inside the geometry kernel
- a very large STEP file
- an issue in the local Python/CadQuery environment

Check the **log panel** first.

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

### The `.exe` does not start

Typical causes:

- missing files from a `onedir` release were not kept together
- antivirus blocked or quarantined the executable
- the release was not extracted from the `.zip` before launch

If using a `onedir` release, do not move only the `.exe` by itself.

## Suggested repository structure

```text
KiAnchor/
├─ KiAnchor.py
├─ README.md
├─ LICENSE
└─ examples/
   ├─ input.step
   └─ output.step
```

## Summary

BoardBond is a practical geometry tool for preparing PCB STEP assemblies for 3D printing when you need components to be more strongly attached to the board.

It is not elegant CAD reconstruction. It is a controlled geometry hack designed to get a printable result.
