# Analysis Tools

This folder contains the scripts used to derive the analysis CSVs and SVG figures for the HIL-Bench model-harness writeup.

The code is self-contained in the sense that it does not import from an external `autonomy_calibration` checkout. The trajectory classifier helper is vendored as `trust_horizon_core.py`. The raw run artifacts are not included here, so reproducing the figures still requires local copies of the evaluated runs.

## Files

- `make_release_assets.py`: ingests run artifacts, writes `data/*.csv`, and generates lightweight SVG figures.
- `make_trajectory_phenotype_panels.py`: regenerates the `06_action_phenotypes_*` panels from cached CSVs.
- `convert_svgs_to_png.py`: optional helper for converting generated SVGs to PNGs.
- `trust_horizon_core.py`: vendored deterministic trajectory classifier used by the release asset script.

## Requirements

Python 3.10+ and the standard library. No plotting package is required; figures are written directly as SVG. PNG conversion is optional and requires one of `rsvg-convert`, `inkscape`, or ImageMagick's `convert`.

## Expected Inputs

`make_release_assets.py` accepts these roots:

- `--native-runs-root`: Trust Horizon native harness runs, including `*_swe_skill3`, custom-tool, FullInfo, and explicitly listed custom-skill/example9 directories. May be repeated to combine runs from multiple checkouts.
- `--swe-agent-raw-root`: SWE-agent raw per-model trajectory directories.
- `--swe-agent-analysis-root`: derived SWE-agent CSV analysis directory. If omitted, this defaults to `../analysis/figure10_model_families` relative to `--swe-agent-raw-root`.
- `--harbor-root`: HIL-Bench Harbor SWE task root containing blocker registries. May be repeated.

## Example

From the repository root:

```bash
python analysis/tools/make_release_assets.py \
  --native-runs-root /path/to/trust_horizon/runs \
  --native-runs-root /path/to/trust_horizon_extra/runs \
  --swe-agent-raw-root /path/to/swe_prompteng2_trajectory_analysis/raw \
  --swe-agent-analysis-root /path/to/swe_prompteng2_trajectory_analysis/analysis/figure10_model_families \
  --harbor-root /path/to/hil-bench-public/harbor_swe \
  --scrub-local-paths
```

By default, outputs are written to `analysis/data`, `analysis/figures`, and `analysis/release_asset_notes.md`. Use `--out-dir /tmp/somewhere` for smoke tests or one-off regeneration checks.

Then regenerate only the action phenotype panels from cached CSVs:

```bash
python analysis/tools/make_trajectory_phenotype_panels.py
```

Optionally convert generated SVGs to PNGs:

```bash
python analysis/tools/convert_svgs_to_png.py --overwrite
```

## Notes

- The scripts generate SVGs. Use `convert_svgs_to_png.py` when PNG copies are needed.
- Custom-skill/example9 native shards are filtered to UIDs with all three passes cleanly evaluated; excluded pass rows are written to `data/native_scoreable_filter_audit.csv`.
- `data/bad_first_ask_recovery.csv` recomputes the Result 3 denominator from trace-level ask sequences and ignores Codex MCP permission prompts for `ask_human`.
- `figures/13_swe_agent_model_family_strategy.svg` is copied and text-normalized by `make_release_assets.py` from the SWE-agent `figure10_model_families` analysis artifact.
- Use `--scrub-local-paths` before publishing generated CSVs or `path_verification.json`; without it, fields such as `pass_dir` and `trajectory_path` preserve absolute paths for debugging.
- The tools do not read `.env` files, inspect shell environment variables, call LiteLLM/model APIs, or make network requests. `convert_svgs_to_png.py` only shells out to a local SVG converter selected from the allowlist in its help text.
- Generated audit CSVs can contain trajectory-derived command or thought snippets, such as `last_verification_command`, `example_action`, and `example_thought`. The writer redacts common secret-looking tokens and assignments, but release artifacts should still be reviewed before publishing when generated from new raw runs.
