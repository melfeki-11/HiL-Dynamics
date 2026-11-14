#!/usr/bin/env bash
# Pre-pull the Docker images referenced by built tasks, so the first training step / eval does not
# spend its environment-start budget downloading them.
#   bash rlvr/pull_images.sh rlvr/tasks            # all splits
#   bash rlvr/pull_images.sh rlvr/tasks/train 4    # one split, 4 parallel pulls
set -euo pipefail
ROOT="${1:?task dir or directory of task dirs}"; PAR="${2:-4}"
find "$ROOT" -path '*/environment/docker-compose.yaml' -print0 |
  xargs -0 grep -h -E '^\s*image:' | awk '{print $2}' | sort -u > /tmp/rlvr_images.$$
echo "$(wc -l < /tmp/rlvr_images.$$) images"
xargs -P "$PAR" -I{} sh -c 'docker image inspect {} >/dev/null 2>&1 || docker pull -q {} >/dev/null && echo "ok {}"' < /tmp/rlvr_images.$$
rm -f /tmp/rlvr_images.$$
