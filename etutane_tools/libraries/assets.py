"""
Generic helpers over a TOC+data archive (see tinkerbell): bulk-dump entry blobs, and
rebuild an archive while substituting selected entries.
"""
import os
from collections import Counter
from . import tinkerbell as tb

def dump_types(toc_path, data_path, out_dir, types=None):
    """Write every entry blob (optionally filtered to `types`) to out_dir as <name>.
    A debugging aid; prints a per-type count. Returns the number of blobs written."""
    os.makedirs(out_dir, exist_ok=True)
    counts = Counter()
    n = 0
    for e, blob in tb.extract_archive(toc_path, data_path):
        if types and e.type not in types:
            continue
        open(os.path.join(out_dir, e.name), "wb").write(blob)
        counts[e.type] += 1
        n += 1
    print("  dumped %d blobs -> %s\\  %s" % (n, os.path.basename(out_dir), dict(counts)))
    return n

def pack_map(toc_path, data_path, sub_map, out_data_path, out_toc_path):
    """Rebuild (out_data_path, out_toc_path) from the original archive, replacing each entry
    named in `sub_map`. A substitution may be a (packed_blob, unpacked_len) tuple, a raw
    bytes blob, or a file path; untouched entries are copied verbatim. Returns
    (n_substituted, n_total)."""
    entries = tb.read_toc(toc_path)
    new_entries = []
    changed = 0
    with open(data_path, "rb") as src, open(out_data_path, "wb") as dst:
        for e in entries:
            sub = sub_map.get(e.name)
            if sub is not None:
                if isinstance(sub, tuple):
                    blob, unpacked = bytes(sub[0]), sub[1]
                else:
                    blob = bytes(sub) if isinstance(sub, (bytes, bytearray)) else open(sub, "rb").read()
                    unpacked = len(blob)
                changed += 1
            else:
                src.seek(e.offset)
                blob = src.read(e.packed)
                unpacked = e.unpacked
            off = dst.tell()
            new_entries.append(tb.Entry(e.id, unpacked, len(blob), off, e.type, e.arc_idx,
                                        max(e.entry_size, 0x17), e.reserved))
            dst.write(blob)
    open(out_toc_path, "wb").write(tb.build_toc(new_entries))
    print("  rebuilt %s (%d/%d entries substituted)"
          % (os.path.basename(out_data_path), changed, len(entries)))
    return changed, len(entries)
