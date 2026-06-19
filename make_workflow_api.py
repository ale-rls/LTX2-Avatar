#!/usr/bin/env python3
"""
make_workflow_api.py
--------------------
Convert a ComfyUI EDITOR-format workflow into the API/prompt format that
server.py consumes, using the comfyui-workflow-to-api-converter-endpoint that
runs inside ComfyUI (POST /workflow/convert). Writes workflow_api.json next to
server.py, then probes node IDs.

WHY AN ENDPOINT, NOT A LOCAL PARSER:
The editor JSON stores each node's widget values *positionally*, so turning them
into named API inputs requires ComfyUI's node registry. The converter runs
inside ComfyUI (all custom nodes loaded) and handles subgraphs, including nested
ones. That also means ComfyUI must be up with every custom node installed --
the same requirement as the built-in "Save (API Format)". If a node pack is
missing, the exported nodes lose their class_type (they serialize as UNKNOWN);
this script flags exactly that so you don't ship a dead workflow.

Usage:
  python3 make_workflow_api.py [EDITOR_WORKFLOW.json]
                               [--comfy http://127.0.0.1:8188]
                               [--out workflow_api.json]
                               [--no-probe]

If EDITOR_WORKFLOW.json is omitted, the newest editor-format *.json in
user-inputs/ is used.
"""

from __future__ import annotations
import argparse
import glob
import json
import os
import subprocess
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))


def is_editor_format(obj) -> bool:
    return isinstance(obj, dict) and "nodes" in obj and "links" in obj


def autodetect_editor() -> str | None:
    """Pick the newest editor-format JSON in user-inputs/."""
    cands = []
    for p in glob.glob(os.path.join(HERE, "user-inputs", "*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                if is_editor_format(json.load(f)):
                    cands.append(p)
        except Exception:
            continue
    return max(cands, key=os.path.getmtime) if cands else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("editor", nargs="?",
                    help="editor-format workflow JSON (default: newest in user-inputs/)")
    ap.add_argument("--comfy", default="http://127.0.0.1:8188",
                    help="ComfyUI base URL (default: %(default)s)")
    ap.add_argument("--out", default=os.path.join(HERE, "workflow_api.json"),
                    help="output API workflow path (default: ./workflow_api.json)")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the workflow_adapter.py --probe step")
    args = ap.parse_args()

    editor_path = args.editor or autodetect_editor()
    if not editor_path:
        print("ERROR: no editor workflow given and none found in user-inputs/.",
              file=sys.stderr)
        return 2
    if not os.path.exists(editor_path):
        print(f"ERROR: {editor_path} does not exist.", file=sys.stderr)
        return 2

    with open(editor_path, "r", encoding="utf-8") as f:
        workflow = json.load(f)
    if not is_editor_format(workflow):
        print(f"WARNING: {os.path.basename(editor_path)} does not look like the "
              f"editor format (no 'nodes'/'links'). Sending it anyway.",
              file=sys.stderr)
    print(f"[convert] source: {editor_path}")

    url = args.comfy.rstrip("/") + "/workflow/convert"
    try:
        r = requests.post(url, json=workflow, timeout=120)
    except requests.exceptions.ConnectionError:
        print(f"ERROR: could not reach ComfyUI at {args.comfy}. Is it running?",
              file=sys.stderr)
        return 1
    if r.status_code == 404:
        print("ERROR: /workflow/convert returned 404 — the converter custom node "
              "isn't loaded. Install comfyui-workflow-to-api-converter-endpoint "
              "into ComfyUI/custom_nodes (setup.sh clones it) and restart ComfyUI.",
              file=sys.stderr)
        return 1
    if r.status_code != 200:
        print(f"ERROR: /workflow/convert -> {r.status_code}: {r.text[:500]}",
              file=sys.stderr)
        return 1

    api = r.json()
    if not isinstance(api, dict) or not api:
        print(f"ERROR: converter returned an unexpected payload: {type(api)}",
              file=sys.stderr)
        return 1

    # Guard against the exact failure we hit before: nodes that lost their
    # class_type because a custom node pack wasn't loaded at convert time.
    missing = [nid for nid, n in api.items()
               if not (isinstance(n, dict) and n.get("class_type"))]
    if missing:
        print(f"ERROR: {len(missing)} node(s) came back without a class_type "
              f"(ids: {', '.join(sorted(missing)[:10])}"
              f"{'…' if len(missing) > 10 else ''}).\n"
              f"       That means those custom nodes aren't installed/loaded in "
              f"ComfyUI. Run setup.sh, restart ComfyUI, and convert again — do "
              f"NOT use this output.", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(api, f, indent=2)
    print(f"[convert] wrote {args.out}  ({len(api)} nodes)")

    if args.no_probe:
        return 0

    print("[probe] verifying node IDs against workflow_adapter.NODE_IDS …")
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "workflow_adapter.py"),
         "--probe", args.out],
        cwd=HERE,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
