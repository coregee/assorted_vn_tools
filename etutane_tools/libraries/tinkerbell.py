"""
Handles the Tinkerbell/Cyberworks archive format and its LZSS codec.

An archive is a *pair* of sibling .dat files (matched up by workspace.discover_archives):
a TOC file describing the entries, and a data file holding their bytes.

TOC file:
[ 8B   decimal   unpacked size of the TOC body ]
[ 8B   decimal   packed size of the TOC body   ]
[ var            LZSS-packed TOC body          ]
  'decimal' is the obfuscated base-10 codec in decode_decimal/encode_decimal: an
  8-byte number, most-significant first, each digit byte stored as (digit ^ 0x7F),
  with 0xFF marking an unused leading position.

The TOC body is a run of variable-length entry records:
[ u32   entry_size   number of bytes that follow in this record ]
[ u32   id           numeric file id                            ]
[ u32   unpacked      uncompressed size                          ]
[ u32   packed        size stored in the data file               ]
[ u32   offset        byte address within the data file          ]
[ 2B    type          ASCII type tag -- 'a0' scenario, 'b0'.. art, etc. ]
[ 4B    reserved      present when entry_size >= 0x17             ]
[ u8    arc_idx       data-file index, present when entry_size >= 0x17 ]
  An entry's logical name is "%06d.%s" % (id, type), e.g. "000123.a0".

Data file: each entry's bytes sit at `offset`; a blob is LZSS-compressed when
packed != unpacked, otherwise stored verbatim.

Scenario ('a0') blobs are themselves a record stream (parse_records):
[ u32   length ][ length bytes  payload ]
  A payload is a string record when it starts with 'S' and is >= 5 bytes:
  [ 'S' ][ u32 slen ][ slen bytes  XOR body, key = slen & 0xFF ]
  The decrypted body is Shift-JIS text; see body_to_text for the «HH» escaping of
  non-text bytes.
"""
import os
import struct
import re

def decode_decimal(buf, off, num_length=8):
    """Read an obfuscated `num_length`-byte base-10 number at buf[off:] (digit ^ 0x7F, 0xFF = unused)."""
    v = 0
    rank = 1
    for i in range(num_length - 1, -1, -1):
        b = buf[off + i]
        if b != 0xFF:
            v += (b ^ 0x7F) * rank
        rank *= 10
    return v

def encode_decimal(value, num_length=8):
    """Inverse of decode_decimal: pack `value` into `num_length` obfuscated digit bytes."""
    out = bytearray([0xFF]) * num_length
    if value == 0:
        out[num_length - 1] = 0 ^ 0x7F
        return bytes(out)
    j = 0
    v = value
    while v and j < num_length:
        out[num_length - 1 - j] = (v % 10) ^ 0x7F
        v //= 10
        j += 1
    return bytes(out)

def lzss_unpack(src, out_size):
    """Decompress `src` to exactly `out_size` bytes via 4K-ring LZSS."""
    frame = bytearray(0x1000)                          # 4K sliding window
    fp = 0xFEE                                         # ring write cursor (engine's reset position)
    out = bytearray()
    sp = 0
    n = len(src)
    while sp < n and len(out) < out_size:
        ctrl = src[sp]
        sp += 1
        for _ in range(8):
            if len(out) >= out_size or sp >= n:
                break
            if ctrl & 1:
                b = src[sp]
                sp += 1
                out.append(b)
                frame[fp] = b
                fp = (fp + 1) & 0xFFF
            else:
                if sp + 1 >= n:
                    break
                lo = src[sp]
                hi = src[sp + 1]
                sp += 2
                offset = lo | ((hi & 0xF0) << 4)
                count = (hi & 0x0F) + 3
                for _ in range(count):
                    b = frame[offset & 0xFFF]
                    offset += 1
                    out.append(b)
                    frame[fp] = b
                    fp = (fp + 1) & 0xFFF
            ctrl >>= 1
    return bytes(out)

def lzss_pack_stored(data):
    """Wrap `data` as an all-literal LZSS stream (valid, no compression). Used for the
    rebuilt TOC, which the engine LZSS-decodes but which needn't actually shrink."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        chunk = data[i:i + 8]
        out.append((1 << len(chunk)) - 1)             # control byte: all 8 (or fewer) slots are literals
        out += chunk
        i += 8
    return bytes(out)

def lzss_pack(data):
    """Compress `data` with greedy 4K-ring LZSS matching (offset = lo|((hi&0xF0)<<4),
    count = (hi&0x0F)+3). Round-trips through lzss_unpack."""
    n = len(data)
    out = bytearray()
    table = {}
    MAXCAND, MAXLEN, MAXDIST = 96, 18, 0xFFF
    src = 0
    while src < n:
        ctrl_pos = len(out)
        out.append(0)
        flags = 0
        for bit in range(8):
            if src >= n:
                break
            best_len, best_j = 0, -1
            if src + 3 <= n:
                cands = table.get(data[src:src + 3])
                if cands:
                    low = src - MAXDIST
                    maxlen = MAXLEN if n - src >= MAXLEN else n - src
                    for j in cands[-MAXCAND:]:
                        if j < low:
                            continue
                        cap = src - j
                        if cap > maxlen:
                            cap = maxlen
                        ln = 0
                        while ln < cap and data[j + ln] == data[src + ln]:
                            ln += 1
                        if ln > best_len:
                            best_len, best_j = ln, j
                            if ln >= maxlen:
                                break
            if best_len >= 3:
                o = (0xFEE + best_j) & 0xFFF
                out.append(o & 0xFF)
                out.append((((o >> 8) & 0xF) << 4) | (best_len - 3))
                end = src + best_len
                while src < end:
                    if src + 3 <= n:
                        table.setdefault(data[src:src + 3], []).append(src)
                    src += 1
            else:
                flags |= 1 << bit
                out.append(data[src])
                if src + 3 <= n:
                    table.setdefault(data[src:src + 3], []).append(src)
                src += 1
        out[ctrl_pos] = flags
    return bytes(out)

class Entry:
    """One TOC record. `name` is the logical "%06d.%s" id.type used on disk and in manifests."""
    __slots__ = ("id", "unpacked", "packed", "offset", "type", "arc_idx", "entry_size", "reserved")
    def __init__(self, id, unpacked, packed, offset, type, arc_idx, entry_size,
                 reserved=b"\xff\xff\xff\xff"):
        self.id, self.unpacked, self.packed, self.offset = id, unpacked, packed, offset
        self.type, self.arc_idx, self.entry_size = type, arc_idx, entry_size
        self.reserved = reserved
    @property
    def name(self):
        return "%06d.%s" % (self.id, self.type)

def looks_like_toc(path, num_length=8):
    """Sniff whether `path` is a TOC: its declared packed size must equal the file size
    minus the 16-byte header. Cheap structural test; never raises."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(num_length * 2)
        if len(head) < num_length * 2:
            return False
        return decode_decimal(head, num_length, num_length) == size - num_length * 2
    except Exception:
        return False

def read_toc(toc_path, num_length=8):
    """Parse a TOC file into a list of Entry records (LZSS-unpacks the body first)."""
    buf = open(toc_path, "rb").read()
    unpacked = decode_decimal(buf, 0, num_length)         # first decimal = unpacked TOC-body size
    toc = lzss_unpack(buf[num_length * 2:], unpacked)      # body starts after the two 8-byte numbers
    entries = []
    i = 0
    n = len(toc)
    while i + 4 <= n:
        es = struct.unpack_from("<I", toc, i)[0]
        if es < 0x11 or i + 4 + es > n:                   # 0x11 = smallest record (no reserved/arc_idx)
            break
        b = i + 4
        eid, unp, pk, off = struct.unpack_from("<IIII", toc, b)
        typ = chr(toc[b + 16]) + chr(toc[b + 17])
        reserved = bytes(toc[b + 18:b + 22]) if es >= 0x17 else b"\xff\xff\xff\xff"
        arc_idx = toc[b + 22] if es >= 0x17 else 0
        entries.append(Entry(eid, unp, pk, off, typ, arc_idx, es, reserved))
        i += 4 + es
    return entries

def build_toc(entries, num_length=8):
    """Serialize Entry records back into TOC-file bytes (header + stored-LZSS body).
    Records are normalised to the >= 0x17 layout so reserved/arc_idx round-trip."""
    body = bytearray()
    for e in entries:
        es = e.entry_size if e.entry_size >= 0x17 else 0x17
        rec = bytearray(es)
        struct.pack_into("<IIII", rec, 0, e.id, e.unpacked, e.packed, e.offset)
        rec[16] = ord(e.type[0])
        rec[17] = ord(e.type[1])
        if es >= 0x17:
            rec[18:22] = e.reserved
            rec[22] = e.arc_idx
        body += struct.pack("<I", es) + rec
    packed = lzss_pack_stored(bytes(body))
    return encode_decimal(len(body), num_length) + encode_decimal(len(packed), num_length) + packed

def extract_archive(toc_path, data_path):
    """Yield (entry, stored_blob) for every entry; the blob is still packed if packed != unpacked."""
    entries = read_toc(toc_path)
    data = open(data_path, "rb").read()
    for e in entries:
        yield e, data[e.offset:e.offset + e.packed]

def build_archive(blobs):
    """Concatenate (entry, blob) pairs into a fresh (data_bytes, toc_bytes). A blob whose
    length differs from its entry's `packed` is treated as stored (packed == unpacked)."""
    data = bytearray()
    out_entries = []
    for e, blob in blobs:
        off = len(data)
        packed = len(blob)
        unpacked = e.unpacked if packed == e.packed else packed
        ne = Entry(e.id, unpacked, packed, off, e.type, e.arc_idx, max(e.entry_size, 0x17), e.reserved)
        out_entries.append(ne)
        data += blob
    return bytes(data), build_toc(out_entries)

def parse_records(data):
    """Walk a scenario ('a0') blob, yielding (offset, payload) for each [u32 len][payload]."""
    i = 0
    n = len(data)
    while i + 4 <= n:
        ln = struct.unpack_from("<I", data, i)[0]
        if ln == 0 or i + 4 + ln > n:
            break
        yield i, data[i + 4:i + 4 + ln]
        i += 4 + ln

def is_string_record(payload):
    """True if `payload` is a dialogue/name string record ('S' tag, >= 5 bytes)."""
    return len(payload) >= 5 and payload[0:1] == b"S"

def string_slen(payload):
    """Declared body length of a string record (the u32 after the 'S' tag)."""
    return struct.unpack_from("<I", payload, 1)[0]

def decrypt_string(payload):
    """Return a string record's plaintext body (XOR with key = slen & 0xFF)."""
    slen = string_slen(payload)
    body = payload[5:5 + slen]
    k = slen & 0xFF
    return bytes(b ^ k for b in body)

def encrypt_string(raw_body):
    """Inverse of decrypt_string: wrap a plaintext body as an 'S' record (key = len & 0xFF)."""
    slen = len(raw_body)
    k = slen & 0xFF
    enc = bytes(b ^ k for b in raw_body)
    return b"S" + struct.pack("<I", slen) + enc

_ESC = re.compile(r"«([0-9A-Fa-f]{2})»")               # round-trippable escape for non-text bytes

def body_to_text(body):
    """Decode a record body to Shift-JIS text, escaping non-text bytes as «HH» (and the
    literal guillemets themselves) so the result is reversible by text_to_body."""
    out = []
    i = 0
    n = len(body)
    while i < n:
        b = body[i]
        if (0x81 <= b <= 0x9F or 0xE0 <= b <= 0xFC) and i + 1 < n:
            pair = body[i:i + 2]
            try:
                out.append(pair.decode("cp932"))
                i += 2
                continue
            except Exception:
                pass
        if 0x20 <= b <= 0x7E:
            ch = chr(b)
            out.append("«%02X»" % b if ch in ("«", "»") else ch)
            i += 1
            continue
        out.append("«%02X»" % b)
        i += 1
    return "".join(out)

def text_to_body(text):
    """Inverse of body_to_text: re-encode text (with «HH» escapes) to raw cp932 bytes."""
    out = bytearray()
    for part in re.split(r"(«[0-9A-Fa-f]{2}»)", text):
        if not part:
            continue
        m = _ESC.fullmatch(part)
        if m:
            out.append(int(m.group(1), 16))
        else:
            out += part.encode("cp932")
    return bytes(out)
