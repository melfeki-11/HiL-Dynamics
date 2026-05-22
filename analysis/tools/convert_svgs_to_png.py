#!/usr/bin/env python3
"""Convert generated SVG figures to PNG files.

This is optional: the main analysis generator writes SVGs only. Use this helper
when Markdown or release surfaces need raster PNG copies.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


DEFAULT_FIGURES_DIR = Path(__file__).resolve().parents[1] / "figures"


def find_converter(preferred: str | None = None) -> str:
    candidates = [preferred] if preferred else ["rsvg-convert", "inkscape", "convert"]
    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return candidate
    raise SystemExit(
        "No SVG converter found. Install one of: rsvg-convert, inkscape, or ImageMagick convert."
    )


def command_for(converter: str, svg_path: Path, png_path: Path) -> list[str]:
    if converter == "rsvg-convert":
        return [converter, str(svg_path), "-o", str(png_path)]
    if converter == "inkscape":
        return [converter, str(svg_path), "--export-type=png", f"--export-filename={png_path}"]
    if converter == "convert":
        return [converter, str(svg_path), str(png_path)]
    raise SystemExit(f"Unsupported converter: {converter}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert SVG figures to PNG.")
    parser.add_argument(
        "--figures-dir",
        type=Path,
        default=DEFAULT_FIGURES_DIR,
        help="Directory containing SVG figures and where PNGs should be written.",
    )
    parser.add_argument(
        "--converter",
        choices=["rsvg-convert", "inkscape", "convert"],
        help="Converter command to use. Defaults to the first available converter.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing PNG files.",
    )
    args = parser.parse_args()

    figures_dir = args.figures_dir.expanduser().resolve()
    converter = find_converter(args.converter)
    svg_paths = sorted(figures_dir.glob("*.svg"))
    if not svg_paths:
        raise SystemExit(f"No SVG files found under {figures_dir}")

    converted = 0
    skipped = 0
    for svg_path in svg_paths:
        png_path = svg_path.with_suffix(".png")
        if png_path.exists() and not args.overwrite:
            skipped += 1
            continue
        subprocess.run(command_for(converter, svg_path, png_path), check=True)
        converted += 1

    print(f"Converted {converted} SVG files with {converter}; skipped {skipped} existing PNG files.")


if __name__ == "__main__":
    main()
