#!/bin/bash
set -e
# Start the drop-params proxy
REAL_LITELLM_URL="${LITELLM_BASE_URL}" \
  LITELLM_API_KEY="${LITELLM_API_KEY}" \
  node /opt/hil_dynamics/src/hil_swe/litellm_drop_params_proxy.mjs >/tmp/pp.txt 2>/dev/null &
PROXY_PID=$!
sleep 2
PORT=$(grep PROXY_PORT /tmp/pp.txt | cut -d= -f2 | tr -d "\n")
echo "PORT=$PORT"

# Build config
python3 - <<'PYEOF' > /tmp/cfg.json
import json
cfg = {
    "provider": {
        "litellm": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "LiteLLM",
            "options": {"apiKey": os.environ.get("LITELLM_API_KEY", ""), "baseURL": "http://127.0.0.1:PORT_PLACEHOLDER/v1"},
            "models": {"fireworks_ai/glm-5p1": {"name": "fireworks_ai/glm-5p1", "tool_call": True, "reasoning": False}}
        }
    },
    "model": "litellm/fireworks_ai/glm-5p1",
    "small_model": "litellm/fireworks_ai/glm-5p1",
    "enabled_providers": ["litellm"],
    "autoupdate": False,
    "agent": {"build": {"model": "litellm/fireworks_ai/glm-5p1", "mode": "primary", "steps": 2, "prompt": "You are a helpful assistant.", "permission": {"bash": "ask"}}}
}
print(json.dumps(cfg))
PYEOF
sed -i "s|PORT_PLACEHOLDER|${PORT}|g" /tmp/cfg.json
echo "Config built"

# Test 1: /app WITH .git (should hang)
echo "=== Test 1: /app with .git ==="
START=$SECONDS
echo "hi" | OPENCODE_CONFIG_CONTENT="$(cat /tmp/cfg.json)" HOME=/tmp OPENCODE_NO_UPDATE=1 \
  timeout 20 opencode run --format json --dangerously-skip-permissions --dir /app --agent build >/tmp/r1.txt 2>/dev/null || true
echo "time=$((SECONDS-START))s  events=$(grep -c '"type"' /tmp/r1.txt 2>/dev/null || echo 0)"

# Test 2: /app WITHOUT .git (should work)
echo ""
echo "=== Test 2: /app without .git ==="
mv /app/.git /tmp/app_git_bk
START=$SECONDS
echo "hi" | OPENCODE_CONFIG_CONTENT="$(cat /tmp/cfg.json)" HOME=/tmp2 OPENCODE_NO_UPDATE=1 \
  timeout 20 opencode run --format json --dangerously-skip-permissions --dir /app --agent build >/tmp/r2.txt 2>/dev/null || true
mv /tmp/app_git_bk /app/.git
echo "time=$((SECONDS-START))s  events=$(grep -c '"type"' /tmp/r2.txt 2>/dev/null || echo 0)"
head -2 /tmp/r2.txt

echo ""
echo "DONE"
kill $PROXY_PID 2>/dev/null
