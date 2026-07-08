"""System-NNN audio container tools for Dasaku ~Nuiawase~ (.vpk/.vtb + .wgq).

Both container types are plain Ogg behind a disguise header (reversed 2026-06-24 from
game\\cdvaw\\* and game\\wgq\\*, verified byte-identical round-trip):

.vpk + .vtb  (voice packs, PACKTYPE==2)   game\\cdvaw\\<pack>.vpk
  .vpk: 108-byte header, then complete Ogg files back-to-back.
        0x00  64 bytes  instructional magic ("IF PACKTYPE==2  CUT THIS 108BYTE
              THEN REMAKE OGG PACKTYPE=2      ")
        0x40  44 bytes  fake RIFF/WAVE header (disguise; size fields are NOT real - keep verbatim)
        0x6C  audio payload: each entry is a whole Ogg (starts "OggS").
  .vtb: 12-byte records, no header/footer: char[8] name (zero-padded) + u32 LE offset
        relative to 0x6C, ascending. Final record is a sentinel: 8 NUL + u32 = absolute
        .vpk size. blob i = vpk[0x6C+off[i] : 0x6C+off[i+1]), last ends at EOF.

.wgq  (movies / streamed audio, PACKTYPE=6)   game\\wgq\\<name>.wgq
  64-byte header (spaces + "OGG" + "PACKTYPE=6"), then one complete Ogg to EOF. No index.

Extraction writes .ogg + manifest.json (verbatim header + per-entry sha256); build replaces
only entries whose .ogg differs from that hash, the rest inheriting from base (mirrors gpk.py).

Usage:
  python libraries/sound.py extract-vpk <pack.vpk|.vtb> <outdir> [--skip-existing]
  python libraries/sound.py build-vpk   <srcdir> <out_basename> [--base <orig.vpk>]
  python libraries/sound.py extract-wgq <wgq_src_dir> <outdir> [--skip-existing]
  python libraries/sound.py build-wgq   <srcdir> <out_dir> [--base <orig_wgq_dir>]
  python libraries/sound.py roundtrip-vpk <pack.vpk|.vtb>
  python libraries/sound.py roundtrip-wgq <name.wgq>
"""
import glob
import hashlib
import json
import os
import struct
import sys

VPK_HEADER_SIZE = 0x6C   # 108
WGQ_HEADER_SIZE = 0x40   # 64


def _sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(srcdir):
    with open(os.path.join(srcdir, "manifest.json")) as fh:
        return json.load(fh)


def compute_modified(srcdir, manifest=None):
    """Entries in srcdir whose .ogg differs from the manifest sha256. Absent files inherit
    from base at build; a hash-less manifest marks every present file modified."""
    if manifest is None:
        manifest = load_manifest(srcdir)
    hashes = manifest.get("sha256")
    out = []
    for fname in manifest["entries"]:
        path = os.path.join(srcdir, fname)
        if not os.path.exists(path):
            continue
        if hashes is None or hashes.get(fname) != _sha_file(path):
            out.append(fname)
    return out


# --------------------------------------------------------------------------- vpk

def _vpk_vtb_paths(path):
    orig = ".orig" if path.endswith(".orig") else ""
    core = path[:-len(orig)] if orig else path
    base, _ = os.path.splitext(core)
    return base + ".vpk" + orig, base + ".vtb" + orig


def parse_vtb(vtb_bytes, vpk_size):
    """-> (names, bounds): named entry ids + absolute blob boundaries; len(bounds) =
    len(names)+1, blob i = vpk[bounds[i]:bounds[i+1])."""
    if len(vtb_bytes) % 12:
        raise ValueError("vtb length not a multiple of 12")
    n = len(vtb_bytes) // 12
    names, offs = [], []
    for i in range(n):
        rec = vtb_bytes[i * 12:i * 12 + 12]
        names.append(rec[:8].rstrip(b"\x00").decode("ascii"))
        offs.append(struct.unpack_from("<I", rec, 8)[0])
    if names[-1] != "":
        raise ValueError("vtb final record is not the empty-name sentinel")
    if offs[-1] != vpk_size:
        raise ValueError(f"vtb sentinel {offs[-1]} != vpk size {vpk_size}")
    named = names[:-1]
    bounds = [VPK_HEADER_SIZE + offs[i] for i in range(n - 1)] + [vpk_size]
    if not all(a < b for a, b in zip(bounds, bounds[1:])):
        raise ValueError("vtb offsets not strictly ascending")
    return named, bounds


def build_vtb(names, sizes, vpk_size):
    out = bytearray()
    pos = 0
    for name, size in zip(names, sizes):
        nb = name.encode("ascii")
        if len(nb) > 8:
            raise ValueError(f"name too long for vtb: {name}")
        out += nb + b"\x00" * (8 - len(nb)) + struct.pack("<I", pos)
        pos += size
    out += b"\x00" * 8 + struct.pack("<I", vpk_size)  # sentinel = absolute EOF
    return bytes(out)


def extract_vpk(pack_path, outdir, skip_existing=False):
    vpk_path, vtb_path = _vpk_vtb_paths(pack_path)
    vpk_size = os.path.getsize(vpk_path)
    header = open(vpk_path, "rb").read(VPK_HEADER_SIZE)
    if b"PACKTYPE==2" not in header:
        raise ValueError(f"{vpk_path}: not a PACKTYPE==2 voice pack")
    names, bounds = parse_vtb(open(vtb_path, "rb").read(), vpk_size)
    os.makedirs(outdir, exist_ok=True)
    entries, hashes = [], {}
    written = kept = 0
    with open(vpk_path, "rb") as f:
        for i, name in enumerate(names):
            f.seek(bounds[i])
            blob = f.read(bounds[i + 1] - bounds[i])
            if blob[:4] != b"OggS":
                raise ValueError(f"{name}: blob does not start with OggS")
            fname = name + ".ogg"
            entries.append(fname)
            hashes[fname] = _sha_bytes(blob)
            path = os.path.join(outdir, fname)
            if skip_existing and os.path.exists(path):
                kept += 1
            else:
                with open(path, "wb") as o:
                    o.write(blob)
                written += 1
    source = os.path.basename(vpk_path)
    if source.endswith(".orig"):
        source = source[:-len(".orig")]
    manifest = {
        "type": "vpk",
        "source": source,
        "header_hex": header.hex(),
        "names": names,
        "entries": entries,
        "sha256": hashes,
    }
    with open(os.path.join(outdir, "manifest.json"), "w") as o:
        json.dump(manifest, o, indent=1)
    print(f"extracted {len(entries)} voices from {vpk_path} -> {outdir} "
          f"({written} written, {kept} kept)")
    return {"total": len(entries), "written": written, "kept": kept}


def build_vpk(srcdir, out_base, base_pack=None, modified=None):
    """Rebuild <out_base>.vpk/.vtb, replacing only modified .ogg entries; unmodified/
    absent ones inherit byte-identical from the base pack. Returns replaced filenames."""
    manifest = load_manifest(srcdir)
    if manifest.get("type") != "vpk":
        raise ValueError("manifest is not a vpk manifest")
    header = bytes.fromhex(manifest["header_hex"])
    names = manifest["names"]
    entries = manifest["entries"]
    if [n + ".ogg" for n in names] != entries:
        raise ValueError("manifest names/entries out of sync")

    if modified is None:
        modified = compute_modified(srcdir, manifest)
    modset = set(modified)
    present = {f for f in entries if os.path.exists(os.path.join(srcdir, f))}

    if base_pack is None:
        base_pack = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "game", "cdvaw", manifest["source"])
    base_vpk_path, base_vtb_path = _vpk_vtb_paths(base_pack)
    base_avail = os.path.exists(base_vpk_path) and os.path.exists(base_vtb_path)
    base_bounds = base_index = None
    if base_avail:
        bnames, base_bounds = parse_vtb(open(base_vtb_path, "rb").read(),
                                        os.path.getsize(base_vpk_path))
        base_index = {n: i for i, n in enumerate(bnames)}

    def from_src(fname):
        return fname in present and (fname in modset or not base_avail)

    inherited = [f for f in entries if not from_src(f)]
    if inherited and not base_avail:
        raise ValueError(f"{inherited[0]} not in srcdir and no base pack at {base_vpk_path}")
    for f, name in zip(entries, names):
        if not from_src(f) and name not in base_index:
            raise ValueError(f"{f} not in srcdir and not in base pack")

    def blob_size(fname, name):
        if from_src(fname):
            return os.path.getsize(os.path.join(srcdir, fname))
        i = base_index[name]
        return base_bounds[i + 1] - base_bounds[i]

    sizes = [blob_size(f, n) for f, n in zip(entries, names)]
    vpk_size = VPK_HEADER_SIZE + sum(sizes)

    vpk_out, vtb_out = out_base + ".vpk", out_base + ".vtb"
    if inherited and os.path.abspath(vpk_out) == os.path.abspath(base_vpk_path):
        raise ValueError("output would overwrite the base pack it inherits from")
    base_f = open(base_vpk_path, "rb") if inherited else None
    try:
        with open(vpk_out, "wb") as o:
            o.write(header)
            for fname, name in zip(entries, names):
                if from_src(fname):
                    with open(os.path.join(srcdir, fname), "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            o.write(chunk)
                else:
                    i = base_index[name]
                    base_f.seek(base_bounds[i])
                    remaining = base_bounds[i + 1] - base_bounds[i]
                    while remaining:
                        chunk = base_f.read(min(1 << 20, remaining))
                        if not chunk:
                            raise IOError("unexpected EOF in base pack")
                        o.write(chunk)
                        remaining -= len(chunk)
    finally:
        if base_f:
            base_f.close()
    with open(vtb_out, "wb") as o:
        o.write(build_vtb(names, sizes, vpk_size))
    replaced = [f for f in entries if from_src(f) and f in modset]
    print(f"built {vpk_out} ({vpk_size} bytes) and {vtb_out}: "
          f"{len(replaced)} replaced, {len(inherited)} inherited from base")
    return replaced


# --------------------------------------------------------------------------- wgq

def extract_wgq(src_paths, outdir, skip_existing=False):
    """src_paths: iterable of .wgq files (may be .orig). One Ogg each -> <stem>.ogg
    in outdir, with a shared manifest (per-file 64-byte header + sha256)."""
    os.makedirs(outdir, exist_ok=True)
    entries, hashes, headers = [], {}, {}
    written = kept = 0
    for path in sorted(src_paths):
        data = open(path, "rb").read()
        header = data[:WGQ_HEADER_SIZE]
        if b"PACKTYPE=6" not in header:
            raise ValueError(f"{path}: not a PACKTYPE=6 wgq")
        ogg = data[WGQ_HEADER_SIZE:]
        if ogg[:4] != b"OggS":
            raise ValueError(f"{path}: payload does not start with OggS")
        stem = os.path.basename(path)
        for suf in (".orig", ".wgq"):
            if stem.endswith(suf):
                stem = stem[:-len(suf)]
        fname = stem + ".ogg"
        entries.append(fname)
        hashes[fname] = _sha_bytes(ogg)
        headers[fname] = header.hex()
        out = os.path.join(outdir, fname)
        if skip_existing and os.path.exists(out):
            kept += 1
        else:
            with open(out, "wb") as o:
                o.write(ogg)
            written += 1
    manifest = {
        "type": "wgq",
        "entries": entries,
        "sha256": hashes,
        "headers_hex": headers,
    }
    with open(os.path.join(outdir, "manifest.json"), "w") as o:
        json.dump(manifest, o, indent=1)
    print(f"extracted {len(entries)} wgq movies -> {outdir} "
          f"({written} written, {kept} kept)")
    return {"total": len(entries), "written": written, "kept": kept}


def build_wgq(srcdir, out_dir, base_dir=None, modified=None):
    """Rebuild <out_dir>/<stem>.wgq per entry: modified .ogg re-wrapped with its 64-byte
    header, unmodified ones copied verbatim from base_dir's .wgq(.orig). Returns replaced."""
    manifest = load_manifest(srcdir)
    if manifest.get("type") != "wgq":
        raise ValueError("manifest is not a wgq manifest")
    entries = manifest["entries"]
    headers = manifest["headers_hex"]
    if modified is None:
        modified = compute_modified(srcdir, manifest)
    modset = set(modified)
    os.makedirs(out_dir, exist_ok=True)
    if base_dir is None:
        base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "game", "wgq")
    replaced = []
    for fname in entries:
        stem = fname[:-len(".ogg")]
        out = os.path.join(out_dir, stem + ".wgq")
        src = os.path.join(srcdir, fname)
        present = os.path.exists(src)
        if present and fname in modset:
            with open(out, "wb") as o:
                o.write(bytes.fromhex(headers[fname]))
                with open(src, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        o.write(chunk)
            replaced.append(fname)
        else:
            base = os.path.join(base_dir, stem + ".wgq.orig")
            if not os.path.exists(base):
                base = os.path.join(base_dir, stem + ".wgq")
            if not os.path.exists(base):
                raise ValueError(f"{fname} not modified in {srcdir} and no base wgq")
            with open(base, "rb") as fh, open(out, "wb") as o:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    o.write(chunk)
    print(f"built {len(entries)} wgq -> {out_dir}: {len(replaced)} replaced")
    return replaced


# ---------------------------------------------------------------------- roundtrip

def _roundtrip_vpk(pack_path):
    import tempfile
    vpk_path, vtb_path = _vpk_vtb_paths(pack_path)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "x")
        extract_vpk(pack_path, out)
        rebuilt = os.path.join(tmp, "r")
        build_vpk(out, rebuilt, modified=load_manifest(out)["entries"])
        ok_vpk = open(rebuilt + ".vpk", "rb").read() == open(vpk_path, "rb").read()
        ok_vtb = open(rebuilt + ".vtb", "rb").read() == open(vtb_path, "rb").read()
    print(f"{os.path.basename(vpk_path)}: vpk identical={ok_vpk} vtb identical={ok_vtb}")
    if not (ok_vpk and ok_vtb):
        sys.exit(1)


def _roundtrip_wgq(wgq_path):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "x")
        extract_wgq([wgq_path], out)
        rebuilt = os.path.join(tmp, "r")
        build_wgq(out, rebuilt, base_dir=os.path.dirname(wgq_path),
                  modified=load_manifest(out)["entries"])
        stem = os.path.splitext(os.path.basename(wgq_path))[0]
        ok = open(os.path.join(rebuilt, stem + ".wgq"), "rb").read() == \
            open(wgq_path, "rb").read()
    print(f"{os.path.basename(wgq_path)}: wgq identical={ok}")
    if not ok:
        sys.exit(1)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    base = None
    if "--base" in sys.argv:
        base = sys.argv[sys.argv.index("--base") + 1]
    skip = "--skip-existing" in sys.argv
    if cmd == "extract-vpk":
        extract_vpk(sys.argv[2], sys.argv[3], skip_existing=skip)
    elif cmd == "build-vpk":
        build_vpk(sys.argv[2], sys.argv[3], base)
    elif cmd == "extract-wgq":
        extract_wgq(glob.glob(os.path.join(sys.argv[2], "*.wgq")), sys.argv[3],
                    skip_existing=skip)
    elif cmd == "build-wgq":
        build_wgq(sys.argv[2], sys.argv[3], base)
    elif cmd == "roundtrip-vpk":
        _roundtrip_vpk(sys.argv[2])
    elif cmd == "roundtrip-wgq":
        _roundtrip_wgq(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
