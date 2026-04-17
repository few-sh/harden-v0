"""Extract all iteration 1 data from a Claude investigation log."""
import json
import sys

log_path = "/mnt/disk-rh/harbor/harden-kb/reports/batch_20260314_203057_subset/kernelbench-level1-007-matmul-with-small-k-dimension.log"

with open(log_path) as f:
    lines = [line.strip() for line in f if line.strip()]

# Collect all tool results that reference paths
for i, line in enumerate(lines):
    try:
        obj = json.loads(line)
    except Exception:
        continue

    if obj.get("type") == "user":
        msg = obj.get("message", {})
        content = msg.get("content", [])
        for c in content:
            if c.get("type") == "tool_result":
                tur = obj.get("tool_use_result", {})
                if isinstance(tur, dict):
                    file_info = tur.get("file", {})
                    fp = file_info.get("filePath", "")
                    if fp and "iter1" in fp:
                        fc = file_info.get("content", "")
                        print(f"FILE: {fp}")
                        print(fc[:5000])
                        print("---END---")
                    # Also check for ls results
                    text = tur.get("text", "") if isinstance(tur, dict) else ""
                    if not fp and "iter1" in str(text):
                        pass  # will handle below

                # Also check direct content text
                txt = c.get("content", "")
                if isinstance(txt, str) and "iter1" in txt:
                    print(f"TOOL_RESULT_LINE_{i}:")
                    print(txt[:5000])
                    print("---END---")
                elif isinstance(txt, list):
                    for item in txt:
                        if isinstance(item, dict):
                            t = item.get("text", "")
                            if "iter1" in t:
                                print(f"TOOL_RESULT_LINE_{i}:")
                                print(t[:5000])
                                print("---END---")

    # Also check for Bash tool results with ls output
    if obj.get("type") == "tool_result":
        content_items = obj.get("content", [])
        for ci in content_items:
            if isinstance(ci, dict):
                t = ci.get("text", "")
                if "iter1" in t:
                    print(f"TOOL_RESULT_DIRECT_LINE_{i}:")
                    print(t[:5000])
                    print("---END---")

# Also extract assistant messages referencing the Write tool for the final report
for i, line in enumerate(lines):
    try:
        obj = json.loads(line)
    except Exception:
        continue
    if obj.get("type") == "assistant":
        msg = obj.get("message", {})
        content = msg.get("content", [])
        for c in content:
            if c.get("type") == "tool_use" and c.get("name") == "Write":
                inp = c.get("input", {})
                fp = inp.get("file_path", "")
                ct = inp.get("content", "")
                if "iter" in ct.lower() or "report" in fp.lower() or ".md" in fp:
                    print(f"WRITE_TO: {fp}")
                    print(ct[:10000])
                    print("---END---")
