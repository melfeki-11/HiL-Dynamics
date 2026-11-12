#!/usr/bin/env python3
# ask_human "<question>": terminal client for the task's ask-human MCP sidecar.
# The SQL task images ship this command (/usr/local/bin/ask_human + /usr/local/bin/mcp); the SWE repo images
# do not, so build_tasks.py mounts this file at /usr/local/bin/ask_human in SWE ask_human tasks. Same protocol
# and server as the SQL images' client (FastMCP streamable-http, tool "ask_human"). Stdlib only, Python >= 3.5.
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("ASK_HUMAN_MCP_URL", "http://ask-human:8000/mcp")
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def post(payload, session=None, timeout=60.0):
    h = dict(HEADERS)
    if session:
        h["Mcp-Session-Id"] = session
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers.get("Mcp-Session-Id"), r.read().decode("utf-8", "replace")


def texts(raw):
    out = []

    def collect(d):
        res = d.get("result") if isinstance(d, dict) else None
        for c in (res or {}).get("content") or []:
            if c.get("type") == "text":
                out.append(c.get("text", ""))
        if isinstance(d, dict) and d.get("error"):
            out.append("Error: " + json.dumps(d["error"]))

    for line in raw.splitlines():
        if line.startswith("data: "):
            try:
                collect(json.loads(line[6:]))
            except ValueError:
                out.append(line[6:])
    if not out:
        try:
            collect(json.loads(raw))
        except ValueError:
            return raw
    return "\n".join(out)


def main():
    question = " ".join(sys.argv[1:]).strip()
    if not question:
        print('usage: ask_human "<one specific question>"', file=sys.stderr)
        return 2
    try:
        sid, _ = post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "ask-human-cli", "version": "1.0"}}}, timeout=30.0)
        _, raw = post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "ask_human", "arguments": {"question": question}}}, session=sid)
    except urllib.error.HTTPError as e:
        print("ask_human failed ({}): {}".format(e.code, e.read().decode("utf-8", "replace")), file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001  - connection errors are reported to the agent, not raised
        print("ask_human failed: {}".format(e), file=sys.stderr)
        return 1
    print(texts(raw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
