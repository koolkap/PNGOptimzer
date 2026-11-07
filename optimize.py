"""Recursively optimize PNG images with Oxipng.

The optimizer is intentionally lossless.  Source files are read from
``unoptimized/`` and are never modified.  Each validated Oxipng result is
written to the matching path below ``optimized/`` using an atomic replace.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image


# Simple configuration for users who prefer editing the script instead of
# passing command-line options.
INPUT_DIR = "unoptimized"
OUTPUT_DIR = "optimized"
COMPRESSION_LEVEL = 4
LOSSLESS = True
WORKERS = 1
OXIPNG_EXECUTABLE = "oxipng"


class PngValidationError(ValueError):
    """Raised when a file is not a valid PNG or does not preserve metadata."""


@dataclass(frozen=True)
class PngMetadata:
    """Properties that must remain unchanged after optimization."""

    width: int
    height: int
    has_transparency: bool
    transparency_signature: tuple[str, object] | None


@dataclass(frozen=True)
class OptimizationResult:
    """Outcome of optimizing one source file."""

    source: Path
    destination: Path
    success: bool
    original_size: int = 0
    optimized_size: int = 0
    error: str | None = None


@dataclass
class ProcessingSummary:
    """Aggregate statistics for one optimizer run."""

    total_found: int = 0
    successful: list[OptimizationResult] = field(default_factory=list)
    failed: list[OptimizationResult] = field(default_factory=list)
    total_original_size: int = 0
    duration_seconds: float = 0.0

    @property
    def total_optimized_size(self) -> int:
        """Return the size of validated output files."""

        return sum(result.optimized_size for result in self.successful)

    @property
    def successful_original_size(self) -> int:
        """Return source size for files that were successfully optimized."""

        return sum(result.original_size for result in self.successful)

    @property
    def total_space_saved(self) -> int:
        """Return actual source-minus-output savings for successful files."""

        return self.successful_original_size - self.total_optimized_size


def get_file_size(file_path: Path) -> int:
    """Return a file's size in bytes."""

    return file_path.stat().st_size


def format_size(size_in_bytes: int) -> str:
    """Format a byte count using binary units with two decimal places."""

    sign = "-" if size_in_bytes < 0 else ""
    value = float(abs(size_in_bytes))
    units = ("B", "KB", "MB", "GB", "TB")

    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{sign}{int(value)} {unit}"
            return f"{sign}{value:.2f} {unit}"
        value /= 1024

    # The loop always returns, but this keeps static analyzers satisfied.
    return f"{sign}{value:.2f} TB"


def _is_relative_to(path: Path, parent: Path) -> bool:
    """Return whether ``path`` is inside ``parent`` or equal to it."""

    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def find_png_files(
    input_dir: Path,
    excluded_dirs: Iterable[Path] | None = None,
) -> list[Path]:
    """Recursively discover PNG files below ``input_dir``.

    Extension matching is case-insensitive.  Directory traversal does not
    follow directory symlinks, and any paths in ``excluded_dirs`` are pruned.
    The optional exclusion prevents an output directory from being scanned if
    a user configures it below the input directory.
    """

    root = input_dir.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Input directory does not exist: {input_dir}")

    excluded = {Path(directory).resolve() for directory in (excluded_dirs or ())}
    if any(root == directory for directory in excluded):
        return []

    png_files: list[Path] = []
    for current_directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current_directory)
        directory_names[:] = [
            directory_name
            for directory_name in directory_names
            if not any(
                _is_relative_to(current_path / directory_name, excluded_dir)
                for excluded_dir in excluded
            )
        ]

        for file_name in file_names:
            candidate = current_path / file_name
            if candidate.suffix.casefold() == ".png" and candidate.is_file():
                png_files.append(candidate)

    return sorted(png_files, key=lambda path: path.relative_to(root).as_posix().casefold())


def _transparency_signature(image: Image.Image) -> tuple[str, object] | None:
    """Return a comparable description of an image's transparency."""

    if "A" in image.getbands():
        return ("alpha-channel", None)

    if "transparency" not in image.info:
        return None

    value = image.info["transparency"]
    if isinstance(value, bytes):
        return ("transparency-bytes", value)
    if isinstance(value, tuple):
        return ("transparency-tuple", tuple(value))
    return ("transparency-value", value)


def _read_png_metadata(file_path: Path) -> PngMetadata:
    """Read and integrity-check source PNG metadata."""

    try:
        with Image.open(file_path) as image:
            if image.format != "PNG":
                raise PngValidationError(f"Not a PNG file: {file_path}")
            width, height = image.size
            signature = _transparency_signature(image)
            image.verify()

        # verify() checks the file structure, while load() also exercises the
        # image decoder for every pixel.  Open a second time because verify()
        # invalidates the first image object.
        with Image.open(file_path) as image:
            image.load()
            if image.format != "PNG":
                raise PngValidationError(f"Not a PNG file: {file_path}")
            actual_signature = _transparency_signature(image)

        if signature != actual_signature:
            raise PngValidationError(f"Could not read transparency metadata: {file_path}")

        return PngMetadata(
            width=width,
            height=height,
            has_transparency=signature is not None,
            transparency_signature=signature,
        )
    except PngValidationError:
        raise
    except Exception as exc:
        raise PngValidationError(f"Unable to open source PNG {file_path}: {exc}") from exc


def validate_png(
    output_path: Path,
    expected_width: int,
    expected_height: int,
    expected_transparency: bool,
    expected_transparency_signature: tuple[str, object] | None = None,
) -> None:
    """Validate PNG integrity, dimensions, and transparency.

    ``PngValidationError`` is raised instead of returning ``False`` so callers
    cannot accidentally treat a failed validation as a successful result.
    """

    if not output_path.is_file():
        raise PngValidationError(f"Output file was not created: {output_path}")

    try:
        with Image.open(output_path) as image:
            if image.format != "PNG":
                raise PngValidationError(f"Output is not a PNG: {output_path}")
            image.verify()

        with Image.open(output_path) as image:
            image.load()
            actual_width, actual_height = image.size
            actual_signature = _transparency_signature(image)

        if (actual_width, actual_height) != (expected_width, expected_height):
            raise PngValidationError(
                "Dimensions changed: "
                f"expected {expected_width}x{expected_height}, "
                f"got {actual_width}x{actual_height}"
            )

        actual_transparency = actual_signature is not None
        if actual_transparency != expected_transparency:
            raise PngValidationError(
                "Transparency changed: "
                f"expected {expected_transparency}, got {actual_transparency}"
            )

        if (
            expected_transparency_signature is not None
            and actual_signature != expected_transparency_signature
        ):
            raise PngValidationError("Transparency metadata changed")
    except PngValidationError:
        raise
    except Exception as exc:
        raise PngValidationError(f"Unable to validate output PNG {output_path}: {exc}") from exc


def _short_process_error(completed_process: subprocess.CompletedProcess[str]) -> str:
    """Build a concise, useful error from an Oxipng process result."""

    details = (completed_process.stderr or completed_process.stdout or "").strip()
    if len(details) > 1000:
        details = details[-1000:]
    return (
        f"Oxipng exited with code {completed_process.returncode}"
        + (f": {details}" if details else "")
    )


def optimize_png(
    source_path: Path,
    destination_path: Path,
    compression_level: int = COMPRESSION_LEVEL,
    lossless: bool = LOSSLESS,
    oxipng_executable: str = OXIPNG_EXECUTABLE,
) -> OptimizationResult:
    """Optimize one PNG into ``destination_path`` and validate the result.

    Oxipng operates only on a temporary copy.  The destination is replaced
    atomically after validation, so a failed run cannot leave a corrupt output
    file or alter the input file.
    """

    original_size = 0
    try:
        if not lossless:
            raise ValueError(
                "Lossy optimization is disabled. This tool only supports lossless PNG output."
            )
        if not 0 <= compression_level <= 6:
            raise ValueError("compression_level must be between 0 and 6")
        if not source_path.is_file():
            raise FileNotFoundError(f"Source file does not exist: {source_path}")

        source_resolved = source_path.resolve()
        destination_resolved = destination_path.resolve()
        if source_resolved == destination_resolved:
            raise ValueError("Source and destination must be different files")

        original_size = get_file_size(source_path)
        source_metadata = _read_png_metadata(source_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        # Use separate input/output staging directories because Oxipng's
        # --dir mode never writes to the source copy in place.
        with tempfile.TemporaryDirectory(
            prefix=".png-optimizer-",
            dir=str(destination_path.parent),
        ) as temporary_directory:
            staging_root = Path(temporary_directory)
            staging_input = staging_root / source_path.name
            staging_output_dir = staging_root / "output"
            staging_output_dir.mkdir()
            shutil.copyfile(source_path, staging_input)

            command = [
                oxipng_executable,
                "--opt",
                str(compression_level),
                "--dir",
                str(staging_output_dir),
                str(staging_input),
            ]
            try:
                completed_process = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except OSError as exc:
                raise RuntimeError(
                    f"Could not start Oxipng ({oxipng_executable!r}). "
                    "Install Oxipng or pass its path with --oxipng."
                ) from exc

            if completed_process.returncode != 0:
                raise RuntimeError(_short_process_error(completed_process))

            candidate_path = staging_output_dir / staging_input.name
            validate_png(
                candidate_path,
                expected_width=source_metadata.width,
                expected_height=source_metadata.height,
                expected_transparency=source_metadata.has_transparency,
                expected_transparency_signature=source_metadata.transparency_signature,
            )

            # The candidate is validated before this atomic operation. Any
            # existing file at the matching output path is replaced only after
            # a successful run; unrelated files in optimized/ are untouched.
            os.replace(candidate_path, destination_path)

        optimized_size = get_file_size(destination_path)
        return OptimizationResult(
            source=source_path,
            destination=destination_path,
            success=True,
            original_size=original_size,
            optimized_size=optimized_size,
        )
    except Exception as exc:
        return OptimizationResult(
            source=source_path,
            destination=destination_path,
            success=False,
            original_size=original_size,
            error=str(exc),
        )


def generate_report(summary: ProcessingSummary, output_dir: Path) -> None:
    """Print the final optimization report."""

    successful_original_size = summary.successful_original_size
    if successful_original_size:
        compression_percentage = (
            summary.total_space_saved / successful_original_size * 100
        )
    else:
        compression_percentage = 0.0

    print()
    print("=" * 40)
    print("       PNG OPTIMIZATION REPORT")
    print("=" * 40)
    print()
    print(f"Total Images Found:       {summary.total_found}")
    print(f"Successfully Optimized:   {len(summary.successful)}")
    print(f"Failed:                   {len(summary.failed)}")
    print()
    print(f"Original Size:            {format_size(summary.total_original_size)}")
    print(f"Optimized Size:           {format_size(summary.total_optimized_size)}")
    print(f"Space Saved:              {format_size(summary.total_space_saved)}")
    print(f"Compression:              {compression_percentage:.2f}%")
    print()
    print(f"Processing Time:          {summary.duration_seconds:.2f} seconds")
    print(f"Output Directory:         {output_dir}")
    if summary.failed:
        print()
        print("Failed files:")
        for result in summary.failed:
            print(f"  - {result.source}: {result.error}")
    print()
    print("=" * 40)
    print("Optimization Completed!")
    print("=" * 40)


def _positive_integer(value: str) -> int:
    """Argparse converter for worker counts."""

    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _compression_level(value: str) -> int:
    """Argparse converter for Oxipng optimization levels."""

    parsed = int(value)
    if not 0 <= parsed <= 6:
        raise argparse.ArgumentTypeError("must be between 0 and 6")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""

    parser = argparse.ArgumentParser(
        description="Recursively optimize PNG files without modifying the inputs."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(INPUT_DIR),
        help=f"Input directory (default: {INPUT_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(OUTPUT_DIR),
        help=f"Output directory (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--compression-level",
        type=_compression_level,
        default=COMPRESSION_LEVEL,
        help="Oxipng optimization level from 0 to 6 (default: 4)",
    )
    parser.add_argument(
        "--workers",
        type=_positive_integer,
        default=WORKERS,
        help="Number of PNGs to process concurrently (default: 1)",
    )
    parser.add_argument(
        "--oxipng",
        default=OXIPNG_EXECUTABLE,
        help=f"Oxipng executable or path (default: {OXIPNG_EXECUTABLE})",
    )
    return parser


def _process_files(
    png_files: Sequence[Path],
    input_dir: Path,
    output_dir: Path,
    compression_level: int,
    workers: int,
    oxipng_executable: str,
) -> ProcessingSummary:
    """Process files and report progress as each worker completes."""

    summary = ProcessingSummary(total_found=len(png_files))
    for path in png_files:
        try:
            summary.total_original_size += get_file_size(path)
        except OSError:
            # The worker will report a disappearing or inaccessible file as an
            # individual failure instead of allowing one race to abort a run.
            continue
    started_at = time.perf_counter()

    def submit_one(source_path: Path) -> OptimizationResult:
        relative_path = source_path.relative_to(input_dir)
        destination_path = output_dir / relative_path
        return optimize_png(
            source_path=source_path,
            destination_path=destination_path,
            compression_level=compression_level,
            lossless=LOSSLESS,
            oxipng_executable=oxipng_executable,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures: dict[Future[OptimizationResult], Path] = {
            executor.submit(submit_one, source_path): source_path
            for source_path in png_files
        }
        for completed_number, future in enumerate(as_completed(futures), start=1):
            source_path = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # Defensive boundary for worker failures.
                result = OptimizationResult(
                    source=source_path,
                    destination=output_dir / source_path.relative_to(input_dir),
                    success=False,
                    error=str(exc),
                )

            relative_path = source_path.relative_to(input_dir)
            if result.success:
                summary.successful.append(result)
                print(
                    f"[{completed_number}/{summary.total_found}] OK   "
                    f"{relative_path} ({format_size(result.original_size)} -> "
                    f"{format_size(result.optimized_size)})"
                )
            else:
                summary.failed.append(result)
                print(f"[{completed_number}/{summary.total_found}] FAIL {relative_path}: {result.error}")

    summary.duration_seconds = time.perf_counter() - started_at
    summary.successful.sort(key=lambda result: result.source.as_posix().casefold())
    summary.failed.sort(key=lambda result: result.source.as_posix().casefold())
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Run the PNG optimizer and return a process exit code."""

    parser = _build_parser()
    args = parser.parse_args(argv)

    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    if input_dir == output_dir:
        parser.error("--input-dir and --output-dir must be different directories")
    if _is_relative_to(output_dir, input_dir):
        parser.error("--output-dir must be outside --input-dir so source files stay untouched")
    if not input_dir.is_dir():
        print(f"Error: input directory does not exist: {input_dir}", file=sys.stderr)
        return 2

    try:
        png_files = find_png_files(input_dir, excluded_dirs=(output_dir,))
    except OSError as exc:
        print(f"Error while scanning input directory: {exc}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    summary = _process_files(
        png_files=png_files,
        input_dir=input_dir,
        output_dir=output_dir,
        compression_level=args.compression_level,
        workers=args.workers,
        oxipng_executable=args.oxipng,
    )
    generate_report(summary, output_dir)
    return 0 if not summary.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
