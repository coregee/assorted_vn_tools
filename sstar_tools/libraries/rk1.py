"""
Handles archive decompress/extract/repack operations for the engine's RK1 archives.
Used by all .dat / .cdt / .vdt / .ovd / .pdt files.

Uses a 12-byte footer to define the archive bounds.
[ u32   magicNumber 'RK1' ]
[ u32   nFiles            ]
[ u32   dirOffset         ]

Offset points to directory of 32-byte file records:
[ 16B   name            ASCII null-padded           ]
[ u32   packed_size     nBytes in archive           ]
[ u32   unpacked_size   nBytes uncompressed         ]
[ u32   lzss_flag       bool for if LZSS-compressed ]
[ u32   offset          byte address                ]

"""
import os
import struct

MAGIC_NUMBER = 0x314b52
RING = 0x1000
RESET_POS = 0xFEE

def lzss_decompress(src, out_size):
    """
    Hand-rolled LZSS decompress function.
        src         source bytearray
        out_size    nBytes uncompressed size
    """
    ring = bytearray(RING)
    r = RESET_POS
    out = bytearray()
    sp = flags = 0
    n = len(src)
    while len(out) < out_size:
        flags >>= 1
        if (flags & 0x100) == 0:
            if sp >= n:
                break
            flags = src[sp] | 0xFF00
            sp += 1
        if flags & 1:
            if sp >= n:
                break
            b = src[sp]
            sp += 1
            out.append(b)
            ring[r] = b
            r = (r + 1) & 0xFFF
        else:
            if sp + 1 >= n:
                break
            lo = src[sp]
            hi = src[sp + 1]
            sp += 2
            offset = lo | ((hi & 0xF0) << 4)
            count = (hi & 0x0F) + 3
            for k in range(count):
                b = ring[(offset + k) & 0xFFF]
                out.append(b)
                ring[r] = b
                r = (r + 1) & 0xFFF
                if len(out) >= out_size:
                    break
    return bytes(out[:out_size])

def _match_len(ring, r, off, data, i, max_len):
    """
    Helper function for hand-rolled LZSS compression.
    Determines how many upcoming bytes of data would be correctly
    compressed using the current ring/write_pos, while considering
    mid-copy overlaps.
        ring     current ring bytearray
        r        write position of next byte
        off      source offset within ring to test
        data     uncompressed source data
        i        current input position
        max_len  maximum match length to consider
    """
    over = {}
    ln = 0
    while ln < max_len:
        sp = (off + ln) & 0xFFF
        b = over.get(sp, ring[sp])
        if b != data[i + ln]:
            break
        over[(r + ln) & 0xFFF] = b
        ln += 1
    return ln

def lzss_compress(data):
    """
    Slow LZSS compressor for byte-exact recreation of script data.
    """
    ring = bytearray(RING)
    r = RESET_POS
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        flag_pos = len(out)
        out.append(0)
        flag = 0
        for bit in range(8):
            if i >= n:
                break
            best_len = best_off = 0
            max_len = min(18, n - i)
            if max_len >= 3:
                for off in range(RING):
                    if ring[off] != data[i]:
                        continue
                    ln = _match_len(ring, r, off, data, i, max_len)
                    if ln > best_len:
                        best_len = ln
                        best_off = off
                        if ln == max_len:
                            break
            if best_len >= 3:
                hi = ((best_off >> 4) & 0xF0) | ((best_len - 3) & 0x0F)
                out.append(best_off & 0xFF)
                out.append(hi)
                for k in range(best_len):
                    ring[r] = data[i + k]
                    r = (r + 1) & 0xFFF
                i += best_len
            else:
                flag |= (1 << bit)
                b = data[i]
                out.append(b)
                ring[r] = b
                r = (r + 1) & 0xFFF
                i += 1
        out[flag_pos] = flag
    return bytes(out)


class Entry:
    __slots__ = ('name', 'packed_size', 'unpacked_size', 'lzss_flag', 'offset')
    def __init__(self, name, packed_size, unpacked_size, lzss_flag, offset):
        self.name, self.packed_size, self.unpacked_size, self.lzss_flag, self.offset = \
            name, packed_size, unpacked_size, lzss_flag, offset

def is_rk1(data):
    return len(data) >= 12 and struct.unpack_from('<I', data, len(data) - 12)[0] == MAGIC_NUMBER

def read_dir(data):
    magic, n_files, dir_offset = struct.unpack('<III', data[-12:])
    if magic != MAGIC_NUMBER:
        raise ValueError('not an RK1 archive (magic=0x%x)' % magic)
    entries = []
    for i in range(n_files):
        record = data[dir_offset + i * 32: dir_offset + i * 32 + 32]
        name = record[:16].split(b'\0')[0].decode('latin1')
        packed_size, unpacked_size, lzss_flag, offset = struct.unpack('<IIII', record[16:32])
        entries.append(Entry(name, packed_size, unpacked_size, lzss_flag, offset))
    return entries

def get_bytes(data, entry):
    raw = data[entry.offset: entry.offset + entry.packed_size]
    return raw[:entry.unpacked_size] if entry.lzss_flag == 0 else lzss_decompress(raw, entry.unpacked_size)

def extract(archive, outdir):
    """
    Unpack all entries from archive into outdir, decompressing as needed.
    Records the entries to _rk1_dir.txt and returns the name list.
    """
    data = open(archive, 'rb').read()
    entries = read_dir(data)
    os.makedirs(outdir, exist_ok=True)
    for entry in entries:
        b = get_bytes(data, entry)
        assert len(b) == entry.unpacked_size, '%s: %d != %d' % (entry.name, len(b), entry.unpacked_size)
        with open(os.path.join(outdir, entry.name), 'wb') as f:
            f.write(b)
    with open(os.path.join(outdir, '_rk1_dir.txt'), 'w') as f:
        f.write('\n'.join(entry.name for entry in entries))
    return [entry.name for entry in entries]

def rebuild(orig_archive, repl_dir, outpath, compress=True):
    """
    Rebuild an archive, leveraging the original to only replace entries
    present in the repl_dir, building to outpath.
    Only compresses when compress=True and the original file is compressed.
    Returns (n_replaced, n_total).
        orig_archive    original archive file path
        repl_dir        path to dir with replacement files
        outpath         path to write rebuilt archive to
        compress        enable compression if source file used it
                        (should disable for large archives)
    """
    data = open(orig_archive, 'rb').read()
    entries = read_dir(data)
    out = bytearray()
    dir_records = []
    n_replaced = 0
    for entry in entries:
        repl_path = os.path.join(repl_dir, entry.name)
        if os.path.exists(repl_path):
            raw = open(repl_path, 'rb').read()
            unpacked_size = len(raw)
            if compress and entry.lzss_flag != 0:
                stored = lzss_compress(raw)
                lzss_flag = 1
            else:
                stored = raw
                lzss_flag = 0
            n_replaced += 1
        else:
            stored = data[entry.offset:entry.offset + entry.packed_size]
            unpacked_size = entry.unpacked_size
            lzss_flag = entry.lzss_flag
        offset = len(out)
        out += stored
        name_bytes = entry.name.encode('latin1')[:16].ljust(16, b'\0')
        dir_records.append(name_bytes + struct.pack('<IIII', len(stored), unpacked_size, lzss_flag, offset))
    dir_offset = len(out)
    for record in dir_records:
        out += record
    out += struct.pack('<III', MAGIC_NUMBER, len(entries), dir_offset)
    open(outpath, 'wb').write(out)
    return n_replaced, len(entries)
