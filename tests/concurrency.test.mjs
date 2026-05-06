import test from "node:test";
import assert from "node:assert/strict";
import { defaultGenerateConcurrency } from "../src/shared/config.mjs";
import { runBounded } from "../src/shared/worker_pool.mjs";

test("bounded worker pool returns results in input order", async () => {
  const results = await runBounded([1, 2, 3, 4], 2, async (value) => value * 2);
  assert.deepEqual(results, [2, 4, 6, 8]);
});

test("default concurrency is capped by total jobs", () => {
  assert.equal(defaultGenerateConcurrency(5) <= 5, true);
  assert.equal(defaultGenerateConcurrency(0), 0);
});
