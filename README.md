# PNG Optimizer

A lossless, recursive PNG optimizer that uses [Oxipng](https://github.com/shssoichiro/oxipng). It scans `unoptimized/`, preserves the complete directory hierarchy, and writes validated results to `optimized/`.

## Features

- Recursively discovers `.png`, `.PNG`, and other case variants.
- Preserves the original filename and relative directory structure.
- Uses Oxipng through a subprocess at a configurable optimization level.
- Never modifies or deletes anything in `unoptimized/`.
- Validates PNG integrity, dimensions, and transparency before publishing output.
- Processes failures independently and prints progress plus a final report.
- Supports optional parallel processing with `--workers`.
- Replaces only the matching generated output after successful validation; unrelated files in `optimized/` are retained.
- Lossy processing is not implemented, so the tool cannot silently reduce image quality.

## Requirements

- Python 3.10 or newer.
- Oxipng available on `PATH`, or supplied with `--oxipng`.
- Python dependency from `requirements.txt` (Pillow).

Create a virtual environment and install the Python dependency:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Install Oxipng separately. For example, with Rust and Cargo installed:

```powershell
cargo install oxipng
oxipng --version
```

Alternatively, download an Oxipng release and pass its executable path:

```powershell
python optimize.py --oxipng "C:\tools\oxipng.exe"
```

## Folder layout

Put source images under `unoptimized/`:

```text
unoptimized/
├── image1.png
└── products/
    └── icons/
        └── home.PNG
```

The output is written to the matching paths:

```text
optimized/
├── image1.png
└── products/
    └── icons/
        └── home.PNG
```

Non-PNG files are ignored. The output directory must be outside the input directory; this prevents generated files from ever being written into the source tree.

## Usage

With the default folders and Oxipng optimization level 4:

```powershell
python optimize.py
```

Useful options:

```powershell
python optimize.py --input-dir unoptimized --output-dir optimized
python optimize.py --compression-level 6 --workers 4
python optimize.py --oxipng "C:\tools\oxipng.exe"
```

Oxipng levels are from 0 through 6. Higher levels can take longer. The default is one worker; increase `--workers` when the machine has enough CPU and disk throughput.

## Safety and validation

Each source image is opened before optimization to record its dimensions and transparency. Oxipng operates on a temporary copy, never on the source. The candidate output must be a readable PNG with the same width, height, and transparency metadata before it is atomically moved into `optimized/`. If validation or optimization fails, an existing output at that path is left unchanged and the failure is included in the report.

The report's original-size total includes every discovered source PNG. Optimized-size and savings totals include only files that passed validation, so failed files are never reported as successfully optimized. The process exits with code `0` when all images succeed and `1` when one or more images fail.

## Configuration

The defaults are also available as constants near the top of `optimize.py`:

```python
INPUT_DIR = "unoptimized"
OUTPUT_DIR = "optimized"
COMPRESSION_LEVEL = 4
LOSSLESS = True
```
