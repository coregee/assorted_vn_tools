"""Refresh names.json, the global speaker-name glossary { "<JP>": "<EN>"|null }.

Collects names from charaname.json + namecol.json (cast order first), then route speaker
fields (first appearance), preserving existing translations. null = keep JP.

Usage: python libraries/names.py
"""
import glob
import json
import os
import sys

from pipeline import NAMES as NAMES_PATH
from pipeline import NON_ROUTE

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "script")


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_names():
    return load(NAMES_PATH) if os.path.exists(NAMES_PATH) else {}


def collect():
    old = load_names()
    ordered = []  # (name, sources)

    def add(name, src):
        for n, s in ordered:
            if n == name:
                s.add(src)
                return
        ordered.append((name, {src}))

    for ui, src in (("charaname.json", "charaname"), ("namecol.json", "namecol")):
        path = os.path.join(SCRIPT, ui)
        if os.path.exists(path):
            for e in load(path):
                add(e["original"], src)
    for path in sorted(glob.glob(os.path.join(SCRIPT, "*.json"))):
        if os.path.basename(path) in NON_ROUTE:
            continue
        route = os.path.splitext(os.path.basename(path))[0]
        for e in load(path):
            if e.get("name"):
                add(e["name"], route)

    out = {n: old.get(n) for n, _ in ordered}
    stale = [n for n in old if n not in out]
    with open(NAMES_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    done = sum(1 for v in out.values() if v is not None)
    print(f"names.json: {len(out)} distinct names, {done} translated")
    for n, s in ordered:
        mark = "*" if out[n] is not None else " "
        print(f" {mark} {n}  ({', '.join(sorted(s))})")
    if stale:
        print("dropped (no longer found):", stale)


if __name__ == "__main__":
    collect()
