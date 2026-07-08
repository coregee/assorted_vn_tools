"""
Extract/repack media archives to `/type/source/filename.ext`.

Types:
- script                scene-specific JSON output
- image                 .bmp
- voice/sound/music     .ogg
"""
import os
import glob
import struct
from . import rk1

def find_packs(game_dir, pattern):
    """Return absolute paths of every file matching `pattern` in game_dir that is an RK1 archive."""
    out = []
    for fn in sorted(glob.glob(os.path.join(game_dir, pattern))):
        with open(fn, 'rb') as f:
            f.seek(-12, os.SEEK_END)
            if struct.unpack('<I', f.read(4))[0] == rk1.MAGIC_NUMBER:
                out.append(fn)
    return out

def _pack_dir(root, pack_path):
    return os.path.join(root, os.path.splitext(os.path.basename(pack_path))[0])

def extract_pack(pack_path, dest_dir, force=False):
    """
    Writes a pack's entries to the dest_dir.
    Skips existing files unless force flag.
    """
    data = open(pack_path, 'rb').read()
    entries = rk1.read_dir(data)
    os.makedirs(dest_dir, exist_ok=True)
    written = skipped = 0
    for e in entries:
        out = os.path.join(dest_dir, e.name)
        if os.path.exists(out) and not force:
            skipped += 1
            continue
        with open(out, 'wb') as f:
            f.write(rk1.get_bytes(data, e))
        written += 1
    with open(os.path.join(dest_dir, '_rk1_dir.txt'), 'w') as f:
        f.write('\n'.join(e.name for e in entries))
    return written, skipped

def rebuild_pack(orig_pack, repl_dir, outpath):
    """
    Rebuilds a pack from its original file, only replacing entries whose file differs from the original decompressed content. Unchanged entries are preserved.
    Returns (n_changed, n_total).
    """
    data = open(orig_pack, 'rb').read()
    entries = rk1.read_dir(data)
    out = bytearray()
    dir_recs = []
    nchg = 0
    for e in entries:
        fp = os.path.join(repl_dir, e.name)
        new = open(fp, 'rb').read() if os.path.exists(fp) else None
        if new is not None and new != rk1.get_bytes(data, e):
            stored = new
            flag = 0
            unpacked = len(new)
            nchg += 1
        else:
            stored = data[e.offset:e.offset + e.packed_size]
            flag = e.lzss_flag
            unpacked = e.unpacked_size
        offset = len(out)
        out += stored
        nb = e.name.encode('latin1')[:16].ljust(16, b'\0')
        dir_recs.append(nb + struct.pack('<IIII', len(stored), unpacked, flag, offset))
    diroff = len(out)
    for rec in dir_recs:
        out += rec
    out += struct.pack('<III', rk1.MAGIC_NUMBER, len(entries), diroff)
    open(outpath, 'wb').write(out)
    return nchg, len(entries)
