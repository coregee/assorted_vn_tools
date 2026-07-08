"""Stage script/ scenario JSON into VNTextPatch-format JSON ({name?, message}).

Each entry uses "translated" if non-null else the JP "message" (untranslated lines
pass through). Speaker resolves: "name_translated" override, then names.json, then
JP "name". UI files (config/charaname/namecol) share the folder and are skipped.

Usage: python libraries/stage_json.py <script_dir> <staging_dir>
"""
import glob
import json
import os
import sys

from pipeline import NAMES, NON_ROUTE

src_dir, out_dir = sys.argv[1], sys.argv[2]
os.makedirs(out_dir, exist_ok=True)

name_map = {}
if os.path.exists(NAMES):
    name_map = {k: v for k, v in json.load(open(NAMES, encoding="utf-8")).items()
                if v is not None}

total = translated = 0
for path in sorted(glob.glob(os.path.join(src_dir, "*.json"))):
    if os.path.basename(path) in NON_ROUTE:
        continue
    with open(path, encoding="utf-8") as fh:
        entries = json.load(fh)
    staged = []
    n_done = 0
    for i, e in enumerate(entries):
        msg = e.get("translated")
        if msg is not None and not isinstance(msg, str):
            raise SystemExit(f"{path}[{i}]: 'translated' must be string or null")
        if msg is not None:
            n_done += 1
        out = {}
        jp_name = e.get("name")
        name = e.get("name_translated") or name_map.get(jp_name) or jp_name
        if name:
            out["name"] = name
        out["message"] = msg if msg is not None else e["message"]
        staged.append(out)
    with open(os.path.join(out_dir, os.path.basename(path)), "w",
              encoding="utf-8") as fh:
        json.dump(staged, fh, ensure_ascii=False, indent=1)
    total += len(entries)
    translated += n_done
    print(f"{os.path.basename(path)}: {n_done}/{len(entries)} translated")
print(f"staged {translated}/{total} lines translated")
