# HiL-SWE Public Split

This repo uses the public HiL-SWE tasks in two roles:

- `train80`: development and skill-iteration split. UIDs sourced from `data/hil_swe_80_remaining_public_uids.txt`.
- `test20`: held-out public test split. UIDs defined inline in `configs/slices/test20.yaml`.

Do not tune prompts, skills, caps, or harness behavior on `test20`. The `test20`
slice is marked `held_out: true`, and `bin/hilbench run` requires
`--allow-test-set` before it will launch that slice.

The paper-private task set remains the preferred final confirmation set if it
becomes available.
