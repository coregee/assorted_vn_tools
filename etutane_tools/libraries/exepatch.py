"""
Reversible byte patches for the game's main .exe, organised into named groups:
  registry    bypass the installer/registry install-gate so the game runs unconfigured,
  locale      force the Japanese locale (LangID 0x411) so the Shift-JIS text renders,
  halfwidth   hook the text pen through a code cave to draw ASCII at half-width.

Each patch site records both its original and patched bytes, so apply() is idempotent
and restore() is exact; a site whose bytes match neither aborts the run before any write.
Addresses in the tables are virtual addresses, mapped to file offsets by _off (subtract
DELTA, the image base plus the .text RVA->raw gap).
"""
import os
import shutil

DELTA = 0x400C00            # VA -> file-offset delta (image base + section RVA/raw gap)
ORIG_SUFFIX = ".orig"

def _off(va):
    """Map a virtual address to a file offset within the .exe."""
    return va - DELTA

_HW_HOOK = ("e98f6d0500" "909090909090")        # 0x46A8B1: jmp 0x4C1645 ; nop*6   (11 bytes)
_HW_HOOK_ORIG = ("8b4c2478" "64890d00000000")   # mov ecx,[esp+0x78] ; mov fs:[0],ecx
_HW_CAVE = ("8a4500" "3286f90c0000" "7514"      # mov al,[ebp]; xor al,[esi+0xCF9]; jne L
            "8b86c8000000" "0386d4040000" "d1f8" "2986c4010000"  # eax=(0xC8+0x4D4)>>1; [0x1C4]-=eax
            "8b4c2478" "64890d00000000" "e94892faff")  # displaced head ; jmp 0x46A8BC
_HW_CAVE_ORIG = "00" * (len(_HW_CAVE) // 2)


# (file_offset, original_hex, patched_hex, note)
PATCHES = {
    "registry": [
        (_off(0x436E9D), "0f85a3000000", "e9a400000090", "install-gate JNZ -> JMP 0x436F46"),
    ],
    "locale": [
        (_off(0x46FE8E), "ff1544214c00", "b81104000090", "GetSystemDefaultLangID -> EAX=0x411"),
        (_off(0x46FE94), "8b1d40214c00", "bb11040000" "90", "MOV EBX,0x411 (was UILang ptr)"),
        (_off(0x46FED3), "ffd3", "8bc3", "CALL EBX -> MOV EAX,EBX"),
        (_off(0x46FFDC), "ffd3", "8bc3", "CALL EBX -> MOV EAX,EBX"),
        (_off(0x4701ED), "ffd3", "8bc3", "CALL EBX -> MOV EAX,EBX"),
        (_off(0x47078E), "ff1540214c00", "b81104000090", "GetUserDefaultUILanguage -> EAX=0x411"),
    ],
    "halfwidth": [
        (_off(0x46A8B1), _HW_HOOK_ORIG, _HW_HOOK, "hook FUN_0046a450 epilogue -> cave"),
        (_off(0x4C1645), _HW_CAVE_ORIG, _HW_CAVE, "ASCII half-width pen cave"),
    ],
}
ORDER = ("registry", "locale", "halfwidth")


def _site_state(blob, off, orig_hex, patched_hex):
    """Classify one site: 'patched', 'orig', or 'mismatch' (bytes match neither)."""
    cur = bytes(blob[off:off + len(patched_hex) // 2])
    if cur == bytes.fromhex(patched_hex):
        return "patched"
    if cur == bytes.fromhex(orig_hex):
        return "orig"
    return "mismatch"

def status(blob):
    """Per-group state: 'patched' / 'orig' / 'partial' (sites disagree) / 'mismatch' (unexpected bytes)."""
    out = {}
    for grp, sites in PATCHES.items():
        states = {_site_state(blob, o, a, b) for o, a, b, _ in sites}
        if states == {"patched"}:
            out[grp] = "patched"
        elif states == {"orig"}:
            out[grp] = "orig"
        elif "mismatch" in states:
            out[grp] = "mismatch"
        else:
            out[grp] = "partial"
    return out

def show(exe_path):
    """Print each patch group's current state for `exe_path`."""
    if not os.path.exists(exe_path):
        print("  no %s" % os.path.basename(exe_path))
        return
    st = status(bytearray(open(exe_path, "rb").read()))
    for grp in ORDER:
        print("  %-10s %s" % (grp, st[grp]))

def apply(exe_path, groups=ORDER):
    """Apply the given patch groups (default: all) to `exe_path`, backing it up to .orig
    first. Already-patched sites are skipped; a site with unexpected bytes aborts before
    any write, so the call is safe to repeat."""
    if not os.path.exists(exe_path):
        print("[exe]     no %s -- skipped" % os.path.basename(exe_path))
        return
    orig = exe_path + ORIG_SUFFIX
    if not os.path.exists(orig):
        shutil.copy(exe_path, orig)
        print("  captured unmodified backup -> %s" % os.path.basename(orig))
    blob = bytearray(open(exe_path, "rb").read())
    applied = []
    for grp in groups:
        done = skipped = 0
        for off, orig_hex, patched_hex, note in PATCHES[grp]:
            state = _site_state(blob, off, orig_hex, patched_hex)
            if state == "mismatch":
                raise SystemExit("!! [%s] unexpected bytes at file 0x%06x -- aborting (no write)"
                                 % (grp, off))
            if state == "patched":
                skipped += 1
                continue
            blob[off:off + len(patched_hex) // 2] = bytes.fromhex(patched_hex)
            done += 1
        applied.append("%s (%d applied%s)" % (grp, done, ", %d already" % skipped if skipped else ""))
    open(exe_path, "wb").write(blob)
    print("[exe]     patched %s: %s" % (os.path.basename(exe_path), "; ".join(applied)))

def restore(exe_path):
    """Restore the .exe from its .orig backup, reverting every patch (raises if no backup)."""
    orig = exe_path + ORIG_SUFFIX
    if not os.path.exists(orig):
        raise SystemExit("!! no %s backup to restore from" % os.path.basename(orig))
    shutil.copy(orig, exe_path)
    print("  restored %s from unmodified backup" % os.path.basename(exe_path))
