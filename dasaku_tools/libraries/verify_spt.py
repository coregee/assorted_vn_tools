#!/usr/bin/env python3
"""Independent validator for System-NNN .spt scripts.

VNTextPatch repacks correctly, but its own `extractlocal` re-read crashes on length-changed
files; the game instead uses the header offset tables, so this reads a .spt the engine's way
(message/string tables) to confirm a repack is sound and diff it against the original.

Usage:
    python verify_spt.py <original.spt> [patched.spt]
  - One arg : dump stats for a single .spt.
  - Two args: validate <patched>, report changed messages/strings; exit 1 on count
              mismatch or parse failure (real corruption).
"""
import struct
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # clean Japanese output on Windows consoles
except Exception:
    pass

XOR_KEY = 0xFF
# Header word indices (see SystemNnnReleaseScript.cs SptHeaderField)
F_MSG_COUNT, F_MSG_TABLE, F_STR_COUNT, F_STR_TABLE = 4, 5, 6, 7
MAGIC_OFF, MAGIC = 0x30, b"SPTHEADER0"


def load(path):
    return bytes(b ^ XOR_KEY for b in open(path, "rb").read())


def word(d, i):
    return struct.unpack_from("<i", d, 4 * i)[0]


def read_str(d, byte_off):
    """Read a NUL-terminated Shift-JIS string at a byte offset (engine semantics)."""
    if byte_off < 0 or byte_off >= len(d):
        raise ValueError(f"offset 0x{byte_off:x} out of range (len 0x{len(d):x})")
    end = d.find(b"\x00", byte_off)
    if end == -1:
        raise ValueError(f"no NUL terminator from offset 0x{byte_off:x}")
    return d[byte_off:end].decode("cp932", errors="replace")


def parse(path):
    d = load(path)
    if d[MAGIC_OFF:MAGIC_OFF + len(MAGIC)] != MAGIC:
        raise ValueError(f"{path}: bad header magic (not a System-NNN .spt?)")
    mc, mt = word(d, F_MSG_COUNT), word(d, F_MSG_TABLE)
    sc, st = word(d, F_STR_COUNT), word(d, F_STR_TABLE)
    msgs = [read_str(d, 4 * word(d, mt + i)) for i in range(mc)]
    strs = [read_str(d, 4 * word(d, st + i)) for i in range(sc)]
    return {"data": d, "msgs": msgs, "strs": strs}


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__)
        return 2

    orig = parse(argv[1])
    print(f"[orig] {argv[1]}")
    print(f"  size={len(orig['data'])}  messages={len(orig['msgs'])}  strings={len(orig['strs'])}")

    if len(argv) == 2:
        return 0

    try:
        patched = parse(argv[2])
    except ValueError as e:
        print(f"FAIL: patched file did not parse: {e}")
        return 1

    print(f"[patched] {argv[2]}")
    print(f"  size={len(patched['data'])}  messages={len(patched['msgs'])}  strings={len(patched['strs'])}")

    ok = True
    if len(orig["msgs"]) != len(patched["msgs"]):
        print(f"FAIL: message count changed {len(orig['msgs'])} -> {len(patched['msgs'])}")
        ok = False
    if len(orig["strs"]) != len(patched["strs"]):
        print(f"FAIL: string count changed {len(orig['strs'])} -> {len(patched['strs'])}")
        ok = False

    if ok:
        mdiff = [i for i in range(len(orig["msgs"])) if orig["msgs"][i] != patched["msgs"][i]]
        sdiff = [i for i in range(len(orig["strs"])) if orig["strs"][i] != patched["strs"][i]]
        print(f"  changed messages: {len(mdiff)}   changed strings: {len(sdiff)}")
        for i in mdiff[:20]:
            print(f"    msg[{i}]: {patched['msgs'][i][:70]!r}")
        if len(mdiff) > 20:
            print(f"    ... and {len(mdiff) - 20} more")
        for i in sdiff[:20]:
            print(f"    str[{i}]: {patched['strs'][i][:70]!r}")

    print("RESULT:", "OK - structurally sound" if ok else "FAIL - corruption detected")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
