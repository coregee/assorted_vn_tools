"""
Minimal dependency-free PNG read/write: 8-bit grayscale/RGB/RGBA, no interlacing.
Paletted (color type 3) images are expanded to RGB/RGBA on read; write picks the color
type from the channel count. Just enough to bridge cyberworks rasters <-> editable PNGs.
"""
import struct
import zlib

MAGIC = b"\x89PNG\r\n\x1a\n"
_CT_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}        # color type -> channels (gray, RGB, gray+alpha, RGBA)
_CHANNELS_CT = {1: 0, 3: 2, 4: 6}              # channels -> color type

def _chunk(typ, data):
    """Frame one PNG chunk: length + (type+data) + CRC32 over (type+data)."""
    body = typ + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

def write(width, height, channels, pixels):
    """Encode raw `pixels` (width*height*channels, top row first, filter None) as PNG bytes."""
    ct = _CHANNELS_CT[channels]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, ct, 0, 0, 0)
    stride = width * channels
    raw = bytearray()
    for y in range(height):
        raw.append(0)                          # filter: None
        raw += pixels[y * stride:(y + 1) * stride]
    idat = zlib.compress(bytes(raw), 6)
    return MAGIC + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")

def _unfilter(f, line, prev, bpp):
    """Reverse PNG scanline filter `f` (0=None..4=Paeth) in place, given the prior row `prev`."""
    n = len(line)
    if f == 0:
        return
    for i in range(n):
        a = line[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        x = line[i]
        if f == 1:
            line[i] = (x + a) & 0xFF
        elif f == 2:
            line[i] = (x + b) & 0xFF
        elif f == 3:
            line[i] = (x + ((a + b) >> 1)) & 0xFF
        elif f == 4:
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = (x + pr) & 0xFF
        else:
            raise ValueError("bad PNG filter %d" % f)

def read(data):
    """Decode a PNG to (width, height, channels, pixels), expanding paletted images to
    RGB/RGBA. Raises ValueError on interlaced or non-8-bit input."""
    if data[:8] != MAGIC:
        raise ValueError("not a PNG")
    pos = 8
    width = height = bitdepth = ct = 0
    idat = bytearray()
    plte = trns = None
    while pos < len(data):
        ln = struct.unpack_from(">I", data, pos)[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            width, height, bitdepth, ct = struct.unpack_from(">IIBB", chunk, 0)
            if chunk[12] != 0:
                raise ValueError("interlaced PNG not supported")
        elif typ == b"PLTE":
            plte = chunk
        elif typ == b"tRNS":
            trns = chunk
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
    if bitdepth != 8:
        raise ValueError("only 8-bit PNG supported (got %d)" % bitdepth)
    if ct == 3:
        channels = 1
    else:
        channels = _CT_CHANNELS[ct]
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(height * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        _unfilter(f, line, prev, channels)
        out[y * stride:(y + 1) * stride] = line
        prev = line
    if ct == 3:
        has_a = trns is not None
        ch = 4 if has_a else 3
        exp = bytearray(width * height * ch)
        for i, idx in enumerate(out):
            exp[i * ch + 0] = plte[idx * 3 + 0]
            exp[i * ch + 1] = plte[idx * 3 + 1]
            exp[i * ch + 2] = plte[idx * 3 + 2]
            if has_a:
                exp[i * ch + 3] = trns[idx] if idx < len(trns) else 0xFF
        return width, height, ch, bytes(exp)
    return width, height, channels, bytes(out)
