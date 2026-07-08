"""
Cyberworks "AImage" image codec: decode the engine's image blobs to pixels/PNG, and
re-encode edited PNGs back into the original blob's layout. The scheme constants follow
Garbro's reading of this format.

A blob's first byte is its type:
  'b' / 'c'   an embedded PNG, returned/stored verbatim,
  'a' / 'd'   a custom raster -- byte 1 is the scheme id (Value2), then an obfuscated
              header (see _get_int), then a pixel encoding chosen by the header flags
              (the _copy_v*/_unpack_v* variants).

Decoded raster pixels are kept in DIB byte order (BGR / BGRA, top row first); to_png swaps
to RGB(A). 'd'/v1/v6d are *differential* frames layered over a `baseline` image -- without
one they decode only the pixels they carry.
"""
from . import png as _png

# Obfuscation constants for the header integer codec, and the slot order the header
# fields are stored in (see read_header / _encode_header). Mirrors Garbro's scheme.
SCHEME = {
    "Value1": 0xE9, "Value2": 0xEF, "Value3": 0xFB, "Flipped": False,
    "HeaderOrder": [4, 14, 3, 2, 13, 1, 10, 1, 1, 6, 5, 0, 12, 9, 15, 16, 17, 7, 18, 1],
}

class _Reader:
    __slots__ = ("d", "p")
    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos
    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v
    def read(self, n):
        v = self.d[self.p:self.p + n]
        self.p += n
        return v


def _put_int(v, s):
    """Encode one header integer in the scheme's obfuscated base-Value1 form (Value2 terminates)."""
    V1 = s["Value1"]
    a = v % V1
    rem = v // V1
    c = rem % V1
    d = rem // V1
    out = bytearray([a])
    out += bytes([V1]) * d
    if c != 0:
        out.append(c)
    out.append(s["Value2"])
    return out


def _get_int(r, s):
    """Read one header integer written by _put_int (Value2 ends it, Value3 stands for zero)."""
    a = r.u8()
    if a == s["Value3"]:
        a = 0
    d = c = 0
    while True:
        a1 = r.u8()
        if a1 == s["Value2"]:
            break
        if a1 != s["Value1"]:
            c = 0 if a1 == s["Value3"] else a1
        else:
            d += 1
    return a + (c + d * s["Value1"]) * s["Value1"]


class AImage:
    """Decoder for one 'a'/'d' raster blob. unpack() fills .output with BGR/BGRA bytes
    according to the header flags; `baseline` supplies the reference frame for the
    differential ('d') variants."""
    def __init__(self, data, scheme=SCHEME, type_char="a", baseline=None):
        self.r = _Reader(bytes(data))
        self.s = scheme
        self.type = type_char
        self.baseline = baseline
        self.header = None
        self.width = self.height = self.bpp = 0
        self.output = None

    def read_header(self):
        """Decode the obfuscated header into a slot list and cache width/height (slots 4/3)."""
        if self.header is None:
            order = self.s["HeaderOrder"]
            h = [0] * max(8, len(order))
            for i in range(len(order)):
                h[order[i]] = _get_int(self.r, self.s)
            self.header = h
            self.width, self.height = h[4], h[3]
        return self.header

    def unpack(self):
        """Decode the body into .output, dispatching to a pixel variant on the header flags
        (slot 0) and bit-plane size (slot 7). Returns the BGR/BGRA pixel buffer."""
        h = self.read_header()
        if not (0 < self.width < 0x8000 and 0 < self.height < 0x8000):
            raise ValueError("bad dimensions %dx%d" % (self.width, self.height))
        flags, unpacked_size, bits_size = h[0], h[5], h[7]
        if unpacked_size <= 0:
            if unpacked_size == 0 and h[6] == 0 and (flags & 1) == 1 and self.baseline is not None:
                self._v6_noalpha(bits_size)
                return self.output
            raise ValueError("bad unpacked_size %d (flags=%d)" % (unpacked_size, flags))
        data_offset = bits_size * 2
        if flags == 0:
            self._copy_v0(unpacked_size)
        elif (flags & 6) == 2:
            self._unpack_v2(bits_size, data_offset)
        elif (flags & 6) == 6:
            if bits_size == 0:
                self._copy_v6(unpacked_size, h[6])
            elif (flags & 1) == 1 and self.type == "d" and self.baseline is not None:
                self._unpack_v6d(bits_size, bits_size + h[6])
            else:
                self._unpack_v6(bits_size, data_offset, data_offset + h[6])
        elif bits_size == 0:
            self._copy_v0(unpacked_size)
        else:
            self._unpack_v1(bits_size, unpacked_size)
        return self.output

    def _copy_v0(self, data_size):
        """Uncompressed pixels: 8/24/32 bpp inferred from plane size, with a stride-padded 24bpp fallback."""
        plane = self.width * self.height
        if plane == data_size:
            self.bpp = 8
            self.output = bytearray(self.r.read(data_size))
        elif 3 * plane == data_size:
            self.bpp = 24
            self.output = bytearray(self.r.read(data_size))
        elif 4 * plane == data_size:
            self.bpp = 32
            self.output = bytearray(self.r.read(data_size))
        else:
            self.bpp = 24
            dst_stride = self.width * 3
            src_stride = (dst_stride + 3) & ~3
            if src_stride * self.height != data_size:
                raise ValueError("V0 stride mismatch")
            self.output = bytearray(dst_stride * self.height)
            gap = src_stride - dst_stride
            dst = 0
            for _ in range(self.height):
                self.output[dst:dst + dst_stride] = self.r.read(dst_stride)
                self.r.read(gap)
                dst += dst_stride

    def _unpack_v1(self, alpha_size, rgb_size):
        """1-bit presence mask + BGR triples for set pixels, over a 24bpp baseline or a transparent 32bpp plane."""
        alpha_map = self.r.read(alpha_size)
        plane = self.width * self.height
        if self.baseline is not None:
            self.bpp = 24
            self.output = bytearray(self.baseline)
        else:
            self.bpp = 32
            self.output = bytearray(plane * 4)
        psize = self.bpp // 8
        bit, bit_src, dst = 1, 0, 0
        for _ in range(plane):
            alpha = 0
            if alpha_map[bit_src] & bit:
                self.output[dst:dst + 3] = self.r.read(3)
                alpha = 0xFF
            if psize == 4:
                self.output[dst + 3] = alpha
            dst += psize
            if bit == 0x80:
                bit_src += 1
                bit = 1
            else:
                bit <<= 1

    def _unpack_v2(self, offset1, rgb_offset):
        """24bpp: paint a BGR triple only where the rgb mask is set and the alpha mask is clear."""
        self.bpp = 24
        rgb_map = self.r.read(offset1)
        alpha_map = self.r.read(rgb_offset - offset1)
        plane = self.width * self.height
        self.output = bytearray(plane * 3)
        bit, bit_src, dst = 1, 0, 0
        for _ in range(plane):
            if (alpha_map[bit_src] & bit) == 0 and (rgb_map[bit_src] & bit) != 0:
                self.output[dst:dst + 3] = self.r.read(3)
            dst += 3
            if bit == 0x80:
                bit_src += 1
                bit = 1
            else:
                bit <<= 1

    def _copy_v6(self, alpha_size, rgb_size):
        """32bpp BGRA built from two stride-padded planes: the alpha plane, then the BGR plane."""
        self.bpp = 32
        plane = self.width * self.height
        self.output = bytearray(plane * 4)
        stride = (self.width * 3 + 3) & ~3
        dst = 3
        for _ in range(self.height):
            line = self.r.read(stride)
            src = 0
            for _ in range(self.width):
                self.output[dst] = line[src]
                dst += 4
                src += 3
        dst = 0
        for _ in range(self.height):
            line = self.r.read(stride)
            src = 0
            for _ in range(self.width):
                self.output[dst] = line[src]
                self.output[dst + 1] = line[src + 1]
                self.output[dst + 2] = line[src + 2]
                src += 3
                dst += 4

    def _unpack_v6(self, offset1, alpha_offset, rgb_offset):
        """32bpp BGRA: rgb mask + alpha mask select which pixels carry a BGR triple / per-pixel alpha."""
        self.bpp = 32
        rgb_map = self.r.read(offset1)
        alpha_map = self.r.read(alpha_offset - offset1)
        alpha = self.r.read(rgb_offset - alpha_offset)
        plane = self.width * self.height
        self.output = bytearray(plane * 4)
        bit, bit_src, alpha_src, dst = 1, 0, 0, 0
        for _ in range(plane):
            has_alpha = (alpha_map[bit_src] & bit) != 0
            if has_alpha or (rgb_map[bit_src] & bit) != 0:
                self.output[dst:dst + 3] = self.r.read(3)
                if has_alpha and alpha_src < len(alpha):
                    self.output[dst + 3] = alpha[alpha_src]
                    alpha_src += 3
                else:
                    self.output[dst + 3] = 0xFF
            dst += 4
            if bit == 0x80:
                bit_src += 1
                bit = 1
            else:
                bit <<= 1

    def _unpack_v6d(self, bits_size, rgb_offset):
        """Differential 32bpp BGRA over the baseline: the rgb mask marks pixels that get new BGR + alpha."""
        self.bpp = 32
        rgb_map = self.r.read(bits_size)
        alpha = self.r.read(rgb_offset - bits_size)
        plane = min(len(self.baseline), bits_size * 8)
        self.output = bytearray(self.baseline)
        bit, bit_src, alpha_src, dst = 1, 0, 0, 0
        for _ in range(plane):
            if rgb_map[bit_src] & bit:
                self.output[dst:dst + 3] = self.r.read(3)
                self.output[dst + 3] = alpha[alpha_src]
                alpha_src += 1
            dst += 4
            bit <<= 1
            if bit == 0x100:
                bit_src += 1
                bit = 1

    def _v6_noalpha(self, bits_size):
        """Differential RGB-only over the baseline (rgb mask, no alpha plane)."""
        rgb_map = self.r.read(bits_size)
        plane = min(len(self.baseline), bits_size * 8)
        self.output = bytearray(self.baseline)
        self.bpp = 24 if self.width * self.height * 3 == len(self.output) else 32
        psize = self.bpp // 8
        bit, bit_src, dst = 1, 0, 0
        for _ in range(plane):
            if rgb_map[bit_src] & bit:
                self.output[dst:dst + 3] = self.r.read(3)
            dst += psize
            bit <<= 1
            if bit == 0x100:
                bit_src += 1
                bit = 1


def _encode_header(header, scheme):
    """Serialize the header slot list back into obfuscated bytes in HeaderOrder."""
    out = bytearray()
    for idx in scheme["HeaderOrder"]:
        out += _put_int(header[idx], scheme)
    return out


def encode(decoded, new_pixels=None, scheme=SCHEME):
    """Re-emit a blob from a decode() result, optionally swapping in `new_pixels`.

    PNG-kind blobs are repacked as-is. Raster blobs are only re-encoded for the layouts
    we can round-trip (the uncompressed CopyV0 and CopyV6 paths); other variants are not
    supported here, so edits to them should be avoided."""
    if decoded["kind"] == "png":
        png = decoded["png"] if new_pixels is None else bytes(new_pixels)
        return decoded.get("type", "c").encode("ascii") + len(png).to_bytes(4, "big") + png
    if decoded["kind"] != "raw":
        raise ValueError("encode expects a decoded 'raw' or 'png' image")
    pixels = decoded["pixels"] if new_pixels is None else bytes(new_pixels)
    header = list(decoded["header"])
    flags, bits_size = header[0], header[7]
    width, height, bpp = decoded["width"], decoded["height"], decoded["bpp"]
    plane = width * height

    def _emit(hdr, body):
        out = bytearray(b"a")
        out.append(scheme["Value2"])
        out += _encode_header(hdr, scheme)
        out += body
        return bytes(out)

    if flags == 0 or ((flags & 6) not in (2, 6) and bits_size == 0):
        if len(pixels) != plane * (bpp // 8):
            raise ValueError("CopyV0 pixel size mismatch")
        if bpp == 24:
            dst_stride = width * 3
            src_stride = (dst_stride + 3) & ~3
            if src_stride != dst_stride:
                pad = bytes(src_stride - dst_stride)
                body = bytearray()
                for y in range(height):
                    body += pixels[y * dst_stride:(y + 1) * dst_stride] + pad
                header[5] = src_stride * height
                return _emit(header, bytes(body))
        header[5] = len(pixels)
        return _emit(header, pixels)

    if (flags & 6) == 6 and bits_size == 0:
        if bpp != 32 or len(pixels) != plane * 4:
            raise ValueError("CopyV6 expects 32bpp BGRA")
        stride = (width * 3 + 3) & ~3
        pad = bytes(stride - width * 3)
        body = bytearray()
        for y in range(height):
            row = bytearray()
            for x in range(width):
                a = pixels[(y * width + x) * 4 + 3]
                row += bytes((a, a, a))
            body += row + pad
        for y in range(height):
            for x in range(width):
                i = (y * width + x) * 4
                body += pixels[i:i + 3]
            body += pad
        header[5] = height * stride
        header[6] = height * stride
        return _emit(header, body)
    

def _swap_br(pixels, ch):
    """Swap the B and R channels in place (BGR<->RGB); no-op for <3 channels."""
    if ch < 3:
        return bytes(pixels)
    out = bytearray(pixels)
    out[0::ch], out[2::ch] = out[2::ch], out[0::ch]
    return bytes(out)

def _adapt_channels(pixels, src, dst):
    """Convert pixels between 1/3/4 channels (gray<->RGB<->RGBA), preserving alpha where it exists."""
    if src == dst:
        return bytes(pixels)
    n = len(pixels) // src
    out = bytearray(n * dst)
    for i in range(n):
        s = pixels[i * src:i * src + src]
        if src == 1:
            r = g = b = s[0]
            a = 0xFF
        else:
            r, g, b = s[0], s[1], s[2]
            a = s[3] if src == 4 else 0xFF
        if dst == 1:
            out[i] = (r * 19 + g * 38 + b * 7) >> 6
        else:
            out[i * dst:i * dst + 3] = bytes((r, g, b))
            if dst == 4:
                out[i * dst + 3] = a
    return bytes(out)

def to_png(decoded):
    """Render a decode() result to PNG bytes (raster pixels are swapped from BGR to RGB)."""
    if decoded["kind"] == "png":
        return decoded["png"]
    ch = decoded["bpp"] // 8
    rgb = _swap_br(decoded["pixels"], ch)
    return _png.write(decoded["width"], decoded["height"], ch, rgb)

def encode_from_png(orig_decoded, png_bytes, scheme=SCHEME):
    """Encode an edited PNG back into the original blob's layout. Dimensions must match the
    original; channels are adapted and the data is swapped back to BGR before encode()."""
    if orig_decoded["kind"] == "png":
        return encode(orig_decoded, png_bytes, scheme)
    w, h, ch, rgb = _png.read(png_bytes)
    if (w, h) != (orig_decoded["width"], orig_decoded["height"]):
        raise ValueError("edited image is %dx%d, original is %dx%d (dimensions must match)"
                         % (w, h, orig_decoded["width"], orig_decoded["height"]))
    dst_ch = orig_decoded["bpp"] // 8
    rgb = _adapt_channels(rgb, ch, dst_ch)
    bgr = _swap_br(rgb, dst_ch)
    return encode(orig_decoded, bgr, scheme)


def decode(aimage_bytes, scheme=SCHEME, baseline=None):
    """Decode an AImage blob to a dict: {kind:'png', png, type} for embedded PNGs, or
    {kind:'raw', width, height, bpp, pixels(BGR/BGRA), type, header} for rasters. `baseline`
    feeds the differential ('d') variants. Raises ValueError on an unknown type byte."""
    type_char = chr(aimage_bytes[0])
    if type_char in ("b", "c"):
        start = aimage_bytes.find(b"\x89PNG\r\n\x1a\n")
        iend = aimage_bytes.rfind(b"IEND")
        if start >= 0 and iend > start:
            png = bytes(aimage_bytes[start:iend + 8])
        else:
            img_size = int.from_bytes(aimage_bytes[1:5], "big")
            png = bytes(aimage_bytes[len(aimage_bytes) - img_size:])
        return {"kind": "png", "png": png, "type": type_char}
    if type_char in ("a", "d"):
        if aimage_bytes[1] != scheme["Value2"]:
            raise ValueError("id byte 0x%02x != Value2 0x%02x" % (aimage_bytes[1], scheme["Value2"]))
        img = AImage(aimage_bytes[2:], scheme, type_char, baseline)
        img.unpack()
        return {"kind": "raw", "width": img.width, "height": img.height,
                "bpp": img.bpp, "pixels": bytes(img.output), "type": type_char,
                "header": img.header}
    raise ValueError("unknown AImage type 0x%02x" % aimage_bytes[0])
