"""System-NNN .gpk/.gtb graphics pack tool for Dasaku ~Nuiawase~.

Format (reversed 2026-06-11 from game\\dwq\\*, verified on all six packs).
Pack = a sequence of [64-byte header][image data] pairs, back-to-back, no padding:

  0x00  HEADER(0)        file header (also describes image 0)
  0x40  IMAGE(0)         image data (PNG, or BMP in a few sys0 entries)
        HEADER(1)        64-byte header describing image 1
        IMAGE(1) ...

Each 64-byte header (verified for PNG entries):
  0x00  "PNG" + 13*0x20        16 bytes magic ("BMP"/"IF PACKTYPE==0" variants precede BMP)
  0x10  16 bytes               per-image, preserve verbatim (content-independent)
  0x20  u32                    byte size of the image that follows
  0x24  u32                    image width
  0x28  u32                    image height
  0x2C  "    PACKTYPE=8A     "  20 bytes magic ("...=0" for BMP)

.gtb stores each IMAGE offset (not its header), relative to gpk 0x40:
  u32  count
  u32  name_off[count]   relative to name-table start
  u32  off32[count]      IMAGE offsets relative to gpk 0x40, ascending
  name table             NUL-terminated, tightly packed (order may differ from entries)
  zero pad to 8-byte alignment (relative to file start)
  u64  off64[count]      identical to off32
  u64  0
  "over2G!\\0"

The 64-byte header sits *after* each image's data (off32 points at the payload). Editors
drop bytes past a PNG's IEND on save, so this tool extracts CLEAN images and keeps the
headers in the manifest, re-synthesising size/dims at build time.

Usage:
  python libraries/gpk.py extract <pack.gpk|pack.gtb> <outdir> [--skip-existing]
  python libraries/gpk.py build   <srcdir> <out_basename> [--base <orig.gpk>]
  python libraries/gpk.py roundtrip <pack.gpk|pack.gtb>
"""
import hashlib
import json
import os
import struct
import sys
import tempfile

GPK_HEADER_SIZE = 0x40
FOOTER = b"over2G!\x00"
MAGIC0 = b"PNG" + b" " * 13
MAGIC2C = b"    PACKTYPE=8A     "
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _paths(pack_path):
    # accept a pristine ".orig" snapshot (e.g. sys0.gpk.orig) and keep the suffix
    orig = ".orig" if pack_path.endswith(".orig") else ""
    core = pack_path[:-len(orig)] if orig else pack_path
    base, _ = os.path.splitext(core)
    return base + ".gpk" + orig, base + ".gtb" + orig


def parse_gtb(gtb_bytes):
    cnt = struct.unpack_from("<I", gtb_bytes, 0)[0]
    name_offs = struct.unpack_from(f"<{cnt}I", gtb_bytes, 4)
    off32 = struct.unpack_from(f"<{cnt}I", gtb_bytes, 4 + 4 * cnt)
    names_base = 4 + 8 * cnt
    names = []
    table_end = names_base
    for off in name_offs:
        end = gtb_bytes.index(b"\x00", names_base + off)
        names.append(gtb_bytes[names_base + off:end].decode("ascii"))
        table_end = max(table_end, end + 1)
    name_table = gtb_bytes[names_base:table_end]

    if not gtb_bytes.endswith(FOOTER):
        raise ValueError("gtb footer mismatch")
    u64_base = len(gtb_bytes) - len(FOOTER) - 8 - 8 * cnt
    off64 = struct.unpack_from(f"<{cnt}Q", gtb_bytes, u64_base)
    if list(off64) != list(off32):
        raise ValueError("gtb u64/u32 offset tables disagree")
    pad = gtb_bytes[table_end:u64_base]
    if pad != bytes(len(pad)):
        raise ValueError("gtb pad region is not all zero")
    if not all(a < b for a, b in zip(off32, off32[1:])):
        raise ValueError("gtb offsets not ascending")
    return {
        "count": cnt,
        "names": names,
        "name_offs": list(name_offs),
        "name_table": name_table,
        "off32": list(off32),
    }


def build_gtb(name_offs, name_table, off32):
    cnt = len(name_offs)
    out = bytearray()
    out += struct.pack("<I", cnt)
    out += struct.pack(f"<{cnt}I", *name_offs)
    out += struct.pack(f"<{cnt}I", *off32)
    out += name_table
    out += bytes((-len(out)) % 8)
    out += struct.pack(f"<{cnt}Q", *off32)
    out += struct.pack("<Q", 0)
    out += FOOTER
    return bytes(out)


def _ext_for(blob):
    if blob.startswith(PNG_SIG):
        return ".png"
    if blob.startswith(b"BM"):
        return ".bmp"
    return ".bin"


def _is_inter_header(b):
    """True if the 64 bytes look like a pack inter-image header (any variant)."""
    return len(b) == GPK_HEADER_SIZE and (
        (b[:3] in (b"PNG", b"BMP") and b"PACKTYPE" in b) or b.startswith(b"IF PACKTYPE"))


def _clean_image(blob):
    """Normalise a blob to just the image payload, dropping any trailing inter-image header.
    PNGs truncate at IEND; other types strip a recognised 64-byte trailer."""
    if blob.startswith(PNG_SIG):
        end = blob.rfind(b"IEND")
        return blob[:end + 8] if end != -1 else blob
    if _is_inter_header(blob[-GPK_HEADER_SIZE:]):
        return blob[:-GPK_HEADER_SIZE]
    return blob


def _patch_header(hdr, img):
    """64-byte header `hdr` with size/dims refreshed from image `img`. Only PNG headers
    have a known layout, so non-PNG headers pass through verbatim (keep original dims)."""
    if not img.startswith(PNG_SIG):
        return hdr
    out = bytearray(hdr)
    w, h = struct.unpack(">II", img[16:24])  # PNG IHDR width, height
    struct.pack_into("<III", out, 0x20, len(img), w, h)
    return bytes(out)


def extract(pack_path, outdir, skip_existing=False):
    """Unpack a pack into outdir as clean images + manifest.json (per-entry sha256 for edit
    detection, per-entry 64-byte headers for reconstruction). skip_existing keeps present
    blobs but always rewrites the manifest. Returns counts dict."""
    gpk_path, gtb_path = _paths(pack_path)
    gtb = parse_gtb(open(gtb_path, "rb").read())
    os.makedirs(outdir, exist_ok=True)
    entries, hashes, headers_hex = [], {}, []
    written = kept = 0
    with open(gpk_path, "rb") as f:
        file_header = f.read(GPK_HEADER_SIZE)
        gpk_size = os.path.getsize(gpk_path)
        bounds = gtb["off32"] + [gpk_size - GPK_HEADER_SIZE]
        n = len(gtb["names"])
        prev_header = file_header  # header(0) is the file header
        for i, name in enumerate(gtb["names"]):
            f.seek(GPK_HEADER_SIZE + bounds[i])
            blob = f.read(bounds[i + 1] - bounds[i])
            # every blob but the last carries the next image's 64-byte header as its tail
            if i < n - 1:
                img, next_header = blob[:-GPK_HEADER_SIZE], blob[-GPK_HEADER_SIZE:]
            else:
                img, next_header = blob, None
            headers_hex.append(prev_header.hex())
            prev_header = next_header
            fname = name + _ext_for(img)
            entries.append(fname)
            hashes[fname] = hashlib.sha256(img).hexdigest()
            path = os.path.join(outdir, fname)
            if skip_existing and os.path.exists(path):
                kept += 1
            else:
                with open(path, "wb") as o:
                    o.write(img)
                written += 1
    source = os.path.basename(gpk_path)
    if source.endswith(".orig"):
        source = source[:-len(".orig")]
    manifest = {
        "source": source,
        "entries": entries,
        "name_offs": gtb["name_offs"],
        "name_table_hex": gtb["name_table"].hex(),
        "gpk_header_hex": file_header.hex(),
        "headers_hex": headers_hex,
        "sha256": hashes,
    }
    with open(os.path.join(outdir, "manifest.json"), "w") as o:
        json.dump(manifest, o, indent=1)
    print(f"extracted {len(entries)} entries from {gpk_path} -> {outdir} "
          f"({written} written, {kept} kept)")
    return {"total": len(entries), "written": written, "kept": kept}


def load_manifest(srcdir):
    with open(os.path.join(srcdir, "manifest.json")) as fh:
        return json.load(fh)


def compute_modified(srcdir, manifest=None):
    """Filenames in srcdir whose content differs from the pristine manifest hash. Absent
    files are not modified (they inherit from base at build); a hash-less manifest marks
    every present file modified."""
    if manifest is None:
        manifest = load_manifest(srcdir)
    hashes = manifest.get("sha256")
    out = []
    for fname in manifest["entries"]:
        path = os.path.join(srcdir, fname)
        if not os.path.exists(path):
            continue
        if hashes is None or hashes.get(fname) != _file_sha256(path):
            out.append(fname)
    return out


def build(srcdir, out_base, base_pack=None, modified=None):
    """Rebuild <out_base>.gpk/.gtb from srcdir, replacing only the modified images. Edited
    images come from srcdir (cleaned, header size/dims recomputed); unedited/absent ones
    inherit byte-identical from the base pack. Headers come from the manifest (legacy
    manifests recover them from the base pack). Returns the replaced filenames."""
    manifest = load_manifest(srcdir)
    entries = manifest["entries"]
    name_table = bytes.fromhex(manifest["name_table_hex"])
    names_base_offsets = manifest["name_offs"]
    headers_hex = manifest.get("headers_hex")
    n = len(entries)

    # sanity: names embedded in the table must match the entry filenames
    for fname, noff in zip(entries, names_base_offsets):
        end = name_table.index(b"\x00", noff)
        if name_table[noff:end].decode("ascii") != os.path.splitext(fname)[0]:
            raise ValueError(f"manifest name mismatch for {fname}")

    if modified is None:
        modified = compute_modified(srcdir, manifest)
    modset = set(modified)
    present = {f for f in entries if os.path.exists(os.path.join(srcdir, f))}

    # base pack: unmodified/absent images and legacy-manifest headers inherit from it
    if base_pack is None:
        base_pack = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "game", "dwq", manifest["source"])
    base_gpk_path, base_gtb_path = _paths(base_pack)
    base_available = os.path.exists(base_gpk_path) and os.path.exists(base_gtb_path)
    base_gtb = base_bounds = base_index = base_f = None
    if base_available:
        base_gtb = parse_gtb(open(base_gtb_path, "rb").read())
        base_size = os.path.getsize(base_gpk_path)
        base_bounds = base_gtb["off32"] + [base_size - GPK_HEADER_SIZE]
        base_index = {nm: i for i, nm in enumerate(base_gtb["names"])}

    def use_src(fname):
        # edited images come from srcdir; so does everything if there's no base
        return fname in present and (fname in modset or not base_available)

    inherited = [f for f in entries if not use_src(f)]
    if inherited and not base_available:
        raise ValueError(f"{inherited[0]} not in srcdir and no base pack at {base_gpk_path}")
    for f in inherited:
        if os.path.splitext(f)[0] not in base_index:
            raise ValueError(f"{f} not in srcdir and not in base pack {base_gpk_path}")

    gpk_out = out_base + ".gpk"
    gtb_out = out_base + ".gtb"
    if inherited and os.path.abspath(gpk_out) == os.path.abspath(base_gpk_path):
        raise ValueError("output would overwrite the base pack it inherits from; "
                         "build to another path, then copy over")

    try:
        if base_available:
            base_f = open(base_gpk_path, "rb")

        def base_image_bounds(fname):
            bi = base_index[os.path.splitext(fname)[0]]
            start = GPK_HEADER_SIZE + base_bounds[bi]
            size = base_bounds[bi + 1] - base_bounds[bi]
            if bi < len(base_gtb["names"]) - 1:  # drop the trailing next-image header
                size -= GPK_HEADER_SIZE
            return start, size

        def header_for(i, fname):
            if headers_hex is not None:
                return bytes.fromhex(headers_hex[i])
            if i == 0:  # legacy manifest: header(0) is the file header
                return bytes.fromhex(manifest["gpk_header_hex"])
            if not base_available:
                raise ValueError("legacy manifest lacks headers_hex and no base pack "
                                 f"at {base_gpk_path} to recover them from")
            bi = base_index.get(os.path.splitext(fname)[0])
            if bi is None:
                raise ValueError(f"cannot recover header for {fname}: not in base pack")
            base_f.seek(base_bounds[bi])  # the 64 bytes preceding image(i) in the base
            return base_f.read(GPK_HEADER_SIZE)

        # resolve each entry's image (edited loaded now, rest streamed at write) and header
        plan, sizes = [], []
        replaced = []
        for i, fname in enumerate(entries):
            hdr = header_for(i, fname)
            if use_src(fname):
                img = _clean_image(open(os.path.join(srcdir, fname), "rb").read())
                hdr = _patch_header(hdr, img)
                if not img.startswith(PNG_SIG):
                    base_size = base_image_bounds(fname)[1] if base_available else len(img)
                    if len(img) != base_size:
                        print(f"  !! {fname}: non-PNG resize not supported (header dims "
                              f"left unchanged) -- redraw at original size")
                plan.append(("src", fname, img))
                sizes.append(len(img))
                if fname in modset:
                    replaced.append(fname)
            else:
                start, size = base_image_bounds(fname)
                plan.append(("base", start, size))
                sizes.append(size)
            if i > 0 and not _is_inter_header(hdr):
                # never emit a corrupt header -- it is what hangs the engine
                raise ValueError(f"header for {fname} is not a valid inter-image header")
            plan[i] = plan[i] + (hdr,)

        # off32 = image(i) offset relative to 0x40 = sum(prior image sizes) + 64*i
        off32, pos = [], 0
        for i, sz in enumerate(sizes):
            if i:
                pos += GPK_HEADER_SIZE  # header(i) precedes image(i)
            off32.append(pos)
            pos += sz

        with open(gpk_out, "wb") as o:
            for entry in plan:
                hdr = entry[-1]
                o.write(hdr)                  # header(i) precedes image(i); header(0) is the file header
                if entry[0] == "src":
                    o.write(entry[2])
                else:
                    _, start, size, _ = entry
                    base_f.seek(start)
                    remaining = size
                    while remaining:
                        chunk = base_f.read(min(1 << 20, remaining))
                        if not chunk:
                            raise IOError("unexpected EOF in base pack")
                        o.write(chunk)
                        remaining -= len(chunk)
    finally:
        if base_f:
            base_f.close()

    with open(gtb_out, "wb") as o:
        o.write(build_gtb(names_base_offsets, name_table, off32))
    inherited_n = sum(1 for e in plan if e[0] == "base")
    print(f"built {gpk_out} ({pos + GPK_HEADER_SIZE} bytes) and {gtb_out}: "
          f"{len(replaced)} replaced, {inherited_n} inherited from base")
    return replaced


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def roundtrip(pack_path):
    gpk_path, gtb_path = _paths(pack_path)
    base = os.path.splitext(os.path.basename(gpk_path))[0]
    with tempfile.TemporaryDirectory() as tmp:
        outdir = os.path.join(tmp, "x")
        extract(pack_path, outdir)
        rebuilt = os.path.join(tmp, base)
        # force every image from the extracted files (true reconstruction, not inherit-all)
        build(outdir, rebuilt, modified=load_manifest(outdir)["entries"])
        ok_gpk = _file_sha256(rebuilt + ".gpk") == _file_sha256(gpk_path)
        ok_gtb = open(rebuilt + ".gtb", "rb").read() == open(gtb_path, "rb").read()
    print(f"{base}: gpk identical={ok_gpk} gtb identical={ok_gtb}")
    if not (ok_gpk and ok_gtb):
        sys.exit(1)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "extract":
        extract(sys.argv[2], sys.argv[3], skip_existing="--skip-existing" in sys.argv)
    elif cmd == "build":
        base = None
        if "--base" in sys.argv:
            base = sys.argv[sys.argv.index("--base") + 1]
        build(sys.argv[2], sys.argv[3], base)
    elif cmd == "roundtrip":
        roundtrip(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
