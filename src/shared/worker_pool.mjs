export async function runBounded(items, concurrency, worker) {
  const results = new Array(items.length);
  let nextIndex = 0;
  const workerCount = Math.min(concurrency, items.length);

  async function runWorker(workerId) {
    while (nextIndex < items.length) {
      const itemIndex = nextIndex;
      nextIndex += 1;
      try {
        results[itemIndex] = await worker(items[itemIndex], itemIndex, workerId);
      } catch (error) {
        results[itemIndex] = { error: String(error?.stack || error), item: items[itemIndex] };
      }
    }
  }

  await Promise.all(Array.from({ length: workerCount }, (_, workerId) => runWorker(workerId + 1)));
  return results;
}
