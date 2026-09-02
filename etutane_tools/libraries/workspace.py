"""
Orchestrates file management within the game folder, and drives extract/repack.

The game ships its data as paired TOC + data .dat archives (see tinkerbell); this
module discovers which pair holds the scenario, images, and audio, then routes each
to the right codec. Edited assets live alongside the game:
    script/             editable JSON texts (+ _names.json speaker glossary)
    image/              editable images (.png)
    audio/              editable audio (.ogg)
    *.orig              backup copies of the untouched originals
    libraries/.working/ disposable scratchpad (scenario_orig/, patched/)
"""
import hashlib
import json
import os
import re
import shutil
import sys
from . import tinkerbell as tb, scenetext, assets, exepatch, cyberworks, tinkaudio

MANIFEST = "_manifest.json"                                 # per-dir sha1 index; lets repack skip unedited files
ORIG_SUFFIX = ".orig"
SCRIPT_DIR  = "script"
WORKING_DIR = os.path.join("libraries", ".working")
SCEN_ORIG   = os.path.join(WORKING_DIR, "scenario_orig")    # pristine extracted scenario blobs
SCEN_PATCHED = os.path.join(WORKING_DIR, "patched")         # rebuilt scenario blobs (repack input)

IMAGE_TYPES = {"b0", "n0", "o0", "c0"}                      # archive entry types that hold images
AUDIO_TYPES = {"j0", "k0", "u0"}                            # ... that hold audio (voice/SE/BGM)
CONTENT = ("scripts", "image", "audio")
_INSTALLER_RE = re.compile(r"unins|uninst|setup|install", re.I)   # ignore these when guessing the game .exe

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT    = os.path.dirname(PKG_DIR)

def _role_of(types):
    """Classify an archive by the entry types it holds: scenario / image / audio / None."""
    if "a0" in types:
        return "scenario"
    if types & IMAGE_TYPES:
        return "image"
    if types & AUDIO_TYPES:
        return "audio"
    return None

def discover_archives(root):
    """Pair the TOC and data .dat files in `root` and label each pair by role.

    Returns {role: (toc_path, data_path)} for whichever of scenario/image/audio are
    present. A TOC is detected structurally (tinkerbell.looks_like_toc); its matching
    data file is the smallest unclaimed .dat large enough to hold every entry."""
    dats = sorted(f for f in os.listdir(root) if f.lower().endswith(".dat"))
    tocs, data_sizes = [], {}
    for f in dats:
        p = _p(root, f)
        if tb.looks_like_toc(p):
            tocs.append(p)
        else:
            data_sizes[p] = os.path.getsize(p)
    found = {}
    for toc in tocs:
        entries = tb.read_toc(toc)
        role = _role_of({e.type for e in entries}) if entries else None
        if role is None or role in found:
            continue
        need = max(e.offset + e.packed for e in entries)
        fits = [(sz - need, p) for p, sz in data_sizes.items() if sz >= need]
        if not fits:
            continue
        data = min(fits)[1]
        del data_sizes[data]
        found[role] = (toc, data)
    return found

def find_exe(root):
    """Return the path of the game's main .exe (largest non-installer .exe), or None."""
    cands = [f for f in sorted(os.listdir(root))
             if f.lower().endswith(".exe") and not _INSTALLER_RE.search(f)]
    if not cands:
        return None
    cands.sort(key=lambda f: os.path.getsize(_p(root, f)), reverse=True)
    return _p(root, cands[0])

def resolve_dir(root):
    return os.path.abspath(root) if root else ROOT

def _p(*parts):
    return os.path.join(*parts)

def _hide(path):
    """Hide a dir on Windows (no-op elsewhere)."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)
        except Exception:
            pass

def ensure_working(root):
    """Create and return the path of the hidden working dir."""
    work = _p(root, WORKING_DIR)
    os.makedirs(work, exist_ok=True)
    _hide(work)
    return work

def names_path(root):
    """Path to the speaker glossary JSON (script\\_names.json)."""
    return _p(root, SCRIPT_DIR, scenetext.NAMES_FILE)

def parse_args(argv, value_flags=(), bool_flags=(), aliases=None):
    aliases = aliases or {}
    root = None
    opts = {f: False for f in bool_flags}
    i = 0
    while i < len(argv):
        raw = argv[i]
        a = aliases.get(raw, raw)
        if a in ("-p", "--path"):
            if i + 1 >= len(argv):
                sys.exit("!! %s needs a value" % raw)
            root = argv[i + 1]
            i += 2
        elif a in bool_flags:
            opts[a] = True
            i += 1
        elif a in value_flags:
            if i + 1 >= len(argv):
                sys.exit("!! %s needs a value" % raw)
            opts[a] = argv[i + 1]
            i += 2
        elif a.startswith("-"):
            sys.exit("!! unknown option %s" % raw)
        elif root is None:
            root = a
            i += 1
        else:
            sys.exit("!! unexpected extra argument %r" % a)
    return root, opts

def select_content(opts):
    """With no content flag, process scripts only; each --scripts/--image/--audio adds
    exactly that content (e.g. -i is image only, -i -a is image + audio)."""
    sel = {t for t in CONTENT if opts.get("--" + t)}
    return sel or {"scripts"}

def backup_once(path, base):
    """Copy `path` to `path`.orig once, if it exists and no backup is present yet."""
    bak = path + ORIG_SUFFIX
    if os.path.exists(path) and not os.path.exists(bak):
        shutil.copy(path, bak)
        print("  captured unmodified backup -> %s" % os.path.relpath(bak, base))
    return bak

def _ensure_game(root):
    """Locate the game archives or exit with a hint; returns the discover_archives() map."""
    arcs = discover_archives(root)
    if not arcs:
        sys.exit("!! no game archives found in %s -- run inside the game root"
                 "folder, or pass its path as an arg / -p" % root)
    return arcs

def _aimage(src, e):
    """Read entry `e`'s blob from open file `src`, LZSS-unpacking it if it was stored packed."""
    src.seek(e.offset)
    blob = src.read(e.packed)
    return tb.lzss_unpack(blob, e.unpacked) if e.packed != e.unpacked else blob

def _load_manifest(in_dir):
    """Load `in_dir`'s sha1 manifest ({name: sha1}), or {} if none exists."""
    p = _p(in_dir, MANIFEST)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}

def _decode_images(toc_path, data_path, out_dir):
    """Decode every image entry to out_dir\\<name>.png, recording a sha1 manifest so
    repack can tell which PNGs were later edited."""
    os.makedirs(out_dir, exist_ok=True)
    ok = skip = 0
    manifest = {}
    with open(data_path, "rb") as src:
        for e in tb.read_toc(toc_path):
            if e.type not in IMAGE_TYPES:
                continue
            try:
                png = cyberworks.to_png(cyberworks.decode(_aimage(src, e)))
                open(_p(out_dir, e.name + ".png"), "wb").write(png)
                manifest[e.name] = hashlib.sha1(png).hexdigest()
                ok += 1
            except Exception:
                skip += 1
    json.dump(manifest, open(_p(out_dir, MANIFEST), "w"))
    print("  decoded %d images -> %s\\ (%d non-image skipped)" % (ok, out_dir, skip))

def _encode_images(toc_path, data_path, in_dir):
    """Re-encode only the PNGs in `in_dir` that differ from the originals (by manifest
    sha1, else by comparing against a freshly decoded baseline). Returns a substitution
    map {name: (packed_blob, unpacked_len)} for assets.pack_map."""
    entries = {e.name: e for e in tb.read_toc(toc_path)}
    manifest = _load_manifest(in_dir)
    sub = {}
    errors = []
    with open(data_path, "rb") as src:
        for fn in sorted(f for f in os.listdir(in_dir) if f.endswith(".png")):
            name = fn[:-4]
            e = entries.get(name)
            if e is None:
                continue
            edited = open(_p(in_dir, fn), "rb").read()
            if name in manifest and hashlib.sha1(edited).hexdigest() == manifest[name]:
                continue
            try:
                orig = cyberworks.decode(_aimage(src, e))
            except Exception:
                continue
            if name not in manifest:
                baseline = orig["png"] if orig["kind"] == "png" else cyberworks.to_png(orig)
                if edited == baseline:
                    continue
            try:
                aimage = cyberworks.encode_from_png(orig, edited)
                sub[name] = (tb.lzss_pack(aimage), len(aimage))
                print("  + %s edited -> re-encoded" % name)
            except Exception as ex:
                errors.append("%s: %s" % (name, ex))
    for er in errors[:20]:
        print("  !! %s" % er)
    return sub

def _decode_audio(toc_path, data_path, out_dir, types):
    """Decrypt every audio entry of the given types to out_dir\\<name>.ogg, recording a
    sha1 manifest so repack can tell which files were later replaced."""
    os.makedirs(out_dir, exist_ok=True)
    ok = skip = 0
    manifest = _load_manifest(out_dir)
    with open(data_path, "rb") as src:
        for e in tb.read_toc(toc_path):
            if e.type not in types:
                continue
            src.seek(e.offset)
            blob = src.read(e.packed)
            try:
                ogg = tinkaudio.decrypt(blob)
                open(_p(out_dir, e.name + ".ogg"), "wb").write(ogg)
                manifest[e.name] = hashlib.sha1(ogg).hexdigest()
                ok += 1
            except Exception:
                skip += 1
    json.dump(manifest, open(_p(out_dir, MANIFEST), "w"))
    print("  decoded %d Ogg stream(s) -> %s\\ (%d skipped)" % (ok, out_dir, skip))

def _encode_audio(toc_path, data_path, in_dir):
    """Re-encrypt only the .ogg files in `in_dir` that were actually replaced (by manifest
    sha1, else by comparing against the decrypted original). Returns a substitution map
    {name: blob} for assets.pack_map."""
    entries = {e.name: e for e in tb.read_toc(toc_path)}
    manifest = _load_manifest(in_dir)
    sub = {}
    errors = []
    with open(data_path, "rb") as src:
        for fn in sorted(f for f in os.listdir(in_dir) if f.endswith(".ogg")):
            name = fn[:-4]
            e = entries.get(name)
            if e is None:
                continue
            edited = open(_p(in_dir, fn), "rb").read()
            if name in manifest and hashlib.sha1(edited).hexdigest() == manifest[name]:
                continue
            if name not in manifest:
                try:
                    src.seek(e.offset)
                    if edited == tinkaudio.decrypt(src.read(e.packed)):
                        continue
                except Exception:
                    continue
            try:
                sub[name] = tinkaudio.encrypt(edited)
                print("  + %s replaced -> re-encrypted" % name)
            except Exception as ex:
                errors.append("%s: %s" % (name, ex))
    for er in errors[:20]:
        print("  !! %s" % er)
    return sub

def do_extract(root, sel=None, force=False):
    """Extract the selected content (default: scripts) into editable form under `root`."""
    root = resolve_dir(root)
    arcs = _ensure_game(root)
    sel = sel or {"scripts"}
    ensure_working(root)

    if "scripts" in sel:
        toc_src, data_src = (backup_once(p, root) for p in arcs["scenario"])
        orig = _p(root, SCEN_ORIG)
        os.makedirs(orig, exist_ok=True)
        n = 0
        for e, blob in tb.extract_archive(toc_src, data_src):
            open(_p(orig, e.name), "wb").write(blob)
            n += 1
        print("[scripts] extracted %d unmodified entries -> %s\\" % (n, SCEN_ORIG))
        scenetext.extract(orig, _p(root, SCRIPT_DIR), names_path(root), force=force)

    for cat in (c for c in CONTENT if c in ("image", "audio") and c in sel):
        pair = arcs.get(cat)
        if not pair:
            print("[%-6s] no %s archive found -- skipped" % (cat, cat))
            continue
        toc, data = pair
        if cat == "image":
            print("[image ] decoding %s -> %s\\ as PNG" % (os.path.basename(data), cat))
            _decode_images(toc, data, _p(root, cat))
        else:
            print("[audio ] decrypting %s -> %s\\ as Ogg" % (os.path.basename(data), cat))
            _decode_audio(toc, data, _p(root, cat), AUDIO_TYPES)

    print("\nedit %s\\ (text) + %s, then run repack.py" % (SCRIPT_DIR, scenetext.NAMES_FILE))

def _rebuild_scenario(root, scen):
    """Rebuild the scenario TOC+data from the patched/ blobs (falling back to the pristine
    scenario_orig/ for untouched entries), then sanity-check that every a0 record reparses."""
    toc, data = scen
    toc_src = backup_once(toc, root)
    backup_once(data, root)
    entries = tb.read_toc(toc_src)
    patched, orig = _p(root, SCEN_PATCHED), _p(root, SCEN_ORIG)
    blobs = []
    for e in entries:
        src = _p(patched, e.name) if os.path.exists(_p(patched, e.name)) else _p(orig, e.name)
        if not os.path.exists(src):
            sys.exit("!! missing blob for %s (run extract)" % e.name)
        blobs.append((e, open(src, "rb").read()))
    new_data, new_toc = tb.build_archive(blobs)
    open(data, "wb").write(new_data)
    open(toc, "wb").write(new_toc)
    re_entries = tb.read_toc(toc)
    bad = 0
    for e in re_entries:
        if e.offset + e.packed > len(new_data):
            bad += 1
            continue
        if e.type == "a0":
            try:
                list(tb.parse_records(new_data[e.offset:e.offset + e.packed]))
            except Exception:
                bad += 1
    print("[scripts] rebuilt %s + %s: %d entries, %d problems (%s)"
          % (os.path.basename(data), os.path.basename(toc), len(re_entries), bad,
             "OK" if bad == 0 else "FAIL"))

def do_repack(root, sel=None, cols=scenetext.LINE_COLS, review_report=None):
    """Rebuild the selected content (default: scripts) back into the game archives.
    Image/audio archives are only rewritten if an edited/replaced file is detected."""
    root = resolve_dir(root)
    arcs = _ensure_game(root)
    sel = sel or {"scripts"}
    ensure_working(root)

    if "scripts" in sel:
        patched = _p(root, SCEN_PATCHED)
        os.makedirs(patched, exist_ok=True)
        for f in os.listdir(patched):
            os.remove(_p(patched, f))
        print("[scripts] building from %s\\ ..." % SCRIPT_DIR)
        scenetext.build(_p(root, SCRIPT_DIR), _p(root, SCEN_ORIG), patched,
                        names_path(root), cols=cols, review_report=review_report)
        _rebuild_scenario(root, arcs["scenario"])

    if "image" in sel and arcs.get("image"):
        d = _p(root, "image")
        if os.path.isdir(d) and any(f.endswith(".png") for f in os.listdir(d)):
            toc, data = arcs["image"]
            toc_src, data_src = backup_once(toc, root), backup_once(data, root)
            print("[image ] checking image\\ for edits ...")
            sub = _encode_images(toc_src, data_src, d)
            if sub:
                assets.pack_map(toc_src, data_src, sub, data, toc)
            else:
                print("  no edited images detected -- %s unchanged" % os.path.basename(data))

    if "audio" in sel and arcs.get("audio"):
        d = _p(root, "audio")
        if os.path.isdir(d) and any(f.endswith(".ogg") for f in os.listdir(d)):
            toc, data = arcs["audio"]
            toc_src, data_src = backup_once(toc, root), backup_once(data, root)
            print("[audio ] checking audio\\ for replaced files ...")
            sub = _encode_audio(toc_src, data_src, d)
            if sub:
                assets.pack_map(toc_src, data_src, sub, data, toc)
            else:
                print("  no replaced audio detected -- %s unchanged" % os.path.basename(data))

    print("\ndone -- launch %s to test." % os.path.basename(find_exe(root) or "the game .exe"))

def _exe(root):
    """Resolve the game .exe path or exit with a hint."""
    root = resolve_dir(root)
    exe = find_exe(root)
    if not exe:
        sys.exit("!! no game .exe found in %s" % root)
    return exe

def do_patch_exe(root, groups=exepatch.ORDER):
    """Apply the .exe byte patches (default: registry gate, locale, half-width)."""
    exepatch.apply(_exe(root), groups)

def do_restore_exe(root):
    """Restore the .exe from its .orig backup, reverting all patches."""
    exepatch.restore(_exe(root))

def do_show_exe(root):
    """Print the per-group patch state of the game .exe."""
    exe = _exe(root)
    print("%s patch status:" % os.path.basename(exe))
    exepatch.show(exe)
