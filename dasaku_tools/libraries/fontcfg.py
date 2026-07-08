"""
Manages the VWF font injection.
Provide a config JSON at the root with a relative filename path (or leave blank for Noto Sans JP).
Optionally specify height and face (leave blank for auto).

Run: python libraries/fontcfg.py [--game DIR]
Generates:
  libraries/vwfhook/vwf_font.h
  libraries/VNTextPatch/VNTextPatch.exe.config
"""
import ctypes
import json
import os
import re
import shutil
from ctypes import wintypes

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_JSON  = os.path.join(ROOT, "font.json")
HEADER     = os.path.join(ROOT, "libraries", "vwfhook", "vwf_font.h")
CONFIG     = os.path.join(ROOT, "libraries", "VNTextPatch", "VNTextPatch.exe.config")

WIDTH_C1 = 98560   # name line width (1px = 100)
WIDTH_C2 = 66000   # follow-up line widths

NOTO_TMHEIGHT_OVER_CAP = 2.0
EN_MIN, EN_MAX = 70, 100

FW_NORMAL, FW_BOLD = 400, 700

class Spec:
    def __init__(self, face, bold, en, file_abs):
        self.face     = face
        self.bold     = bold
        self.weight   = FW_BOLD if bold else FW_NORMAL
        self.en       = en
        self.width1   = round(WIDTH_C1 / en)
        self.width2   = round(WIDTH_C2 / en)
        self.file_abs = file_abs
        self.file_base = os.path.basename(file_abs)


def _load_json():
    if not os.path.isfile(FONT_JSON):
        return None
    try:
        with open(FONT_JSON, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        print("[font]    !! could not read font.json: %s" % e)
        return None


def resolve():
    cfg = _load_json() or {}
    rel = (cfg.get("file") or "").strip()
    if not rel:
        return None
    path = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
    return _spec_from_file(cfg, path)


def _spec_from_file(cfg, path):
    if not os.path.isfile(path):
        print("[font]    !! font file not found: %s -- using default font" % path)
        return None
    face = cfg.get("face") or family_name(path)
    if not face:
        print("[font]    !! could not read a face name from %s -- using default font" % path)
        return None
    bold = str(cfg.get("weight", "normal")).lower() in ("bold", "700")
    weight = FW_BOLD if bold else FW_NORMAL

    override = cfg.get("en_height_pct")
    if override:
        en = max(1, min(200, int(override)))
        print("[font]    %s  EN_HEIGHT_PCT=%d (pinned in font.json)" % (face, en))
    else:
        en = auto_en(path, face, weight)
    return Spec(face=face, bold=bold, en=en, file_abs=path)


def _u16(b, o):  return (b[o] << 8) | b[o + 1]
def _u32(b, o):  return (b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]


def family_name(path):
    """Windows English family name (nameID 1, fallback 16) from a TTF/OTF/TTC."""
    try:
        with open(path, "rb") as fh:
            b = fh.read()
    except OSError:
        return None
    base = 0
    if b[:4] == b"ttcf":
        base = _u32(b, 12)
    if len(b) < base + 12:
        return None
    num = _u16(b, base + 4)
    name_off = None
    for i in range(num):
        rec = base + 12 + i * 16
        if b[rec:rec + 4] == b"name":
            name_off = _u32(b, rec + 8)
            break
    if name_off is None or name_off + 6 > len(b):
        return None
    count = _u16(b, name_off + 2)
    strings = name_off + _u16(b, name_off + 4)
    best_name, best_score = None, -1
    for i in range(count):
        rp = name_off + 6 + i * 12
        if rp + 12 > len(b):
            break
        pid, eid, lid, nid = _u16(b, rp), _u16(b, rp + 2), _u16(b, rp + 4), _u16(b, rp + 6)
        length, soff = _u16(b, rp + 8), _u16(b, rp + 10)
        if nid not in (1, 16):
            continue
        raw = b[strings + soff: strings + soff + length]
        try:
            if pid in (0, 3):
                name = raw.decode("utf-16-be")
            elif pid == 1:
                name = raw.decode("mac-roman")
            else:
                continue
        except (UnicodeDecodeError, LookupError):
            continue
        if not name:
            continue
        score = (4 if pid == 3 else 0) + (2 if lid == 0x409 else 0) + (1 if nid == 16 else 0)
        if score > best_score:
            best_name, best_score = name, score
    return best_name


_HGDI = ctypes.c_void_p
SHIFTJIS_CHARSET, OUT_TT_PRECIS, ANTIALIASED_QUALITY = 128, 4, 4
FR_PRIVATE, GGO_METRICS, GDI_ERROR = 0x10, 0, 0xFFFFFFFF


class TEXTMETRICW(ctypes.Structure):
    _fields_ = [(n, wintypes.LONG) for n in (
        "tmHeight", "tmAscent", "tmDescent", "tmInternalLeading", "tmExternalLeading",
        "tmAveCharWidth", "tmMaxCharWidth", "tmWeight", "tmOverhang",
        "tmDigitizedAspectX", "tmDigitizedAspectY")] + [
        (n, wintypes.WCHAR) for n in ("tmFirstChar", "tmLastChar", "tmDefaultChar", "tmBreakChar")] + [
        (n, wintypes.BYTE) for n in ("tmItalic", "tmUnderlined", "tmStruckOut",
                                     "tmPitchAndFamily", "tmCharSet")]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class GLYPHMETRICS(ctypes.Structure):
    _fields_ = [("gmBlackBoxX", wintypes.UINT), ("gmBlackBoxY", wintypes.UINT),
                ("gmptGlyphOrigin", _POINT),
                ("gmCellIncX", ctypes.c_short), ("gmCellIncY", ctypes.c_short)]


class _FIXED(ctypes.Structure):
    _fields_ = [("fract", wintypes.WORD), ("value", ctypes.c_short)]


class MAT2(ctypes.Structure):
    _fields_ = [("eM11", _FIXED), ("eM12", _FIXED), ("eM21", _FIXED), ("eM22", _FIXED)]


def _measure(path, face, weight):
    """(tmHeight, capHeight, resolved_face) at em=1000, font privately loaded. capHeight =
    black-box height of 'H'. None off Windows / on any failure."""
    if os.name != "nt":
        return None
    try:
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        g.CreateFontW.restype = _HGDI
        g.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
        g.CreateCompatibleDC.restype = _HGDI
        g.CreateCompatibleDC.argtypes = [_HGDI]
        g.SelectObject.restype = _HGDI
        g.SelectObject.argtypes = [_HGDI, _HGDI]
        g.AddFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        g.RemoveFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        g.GetTextMetricsW.argtypes = [_HGDI, ctypes.POINTER(TEXTMETRICW)]
        g.GetTextFaceW.argtypes = [_HGDI, ctypes.c_int, wintypes.LPWSTR]
        g.GetGlyphOutlineW.restype = wintypes.DWORD
        g.GetGlyphOutlineW.argtypes = [_HGDI, wintypes.UINT, wintypes.UINT,
                                       ctypes.POINTER(GLYPHMETRICS), wintypes.DWORD,
                                       ctypes.c_void_p, ctypes.POINTER(MAT2)]
        # handles are pointer-sized: type these or ctypes' default 32-bit int truncates a
        # high 64-bit GDI handle ("int too long to convert").
        g.DeleteObject.argtypes = [_HGDI]
        g.DeleteDC.argtypes = [_HGDI]

        added = g.AddFontResourceExW(path, FR_PRIVATE, None)
        dc = g.CreateCompatibleDC(None)
        # negative height => em == 1000, so metrics are per-em (capHeight/tmHeight ratios).
        font = g.CreateFontW(-1000, 0, 0, 0, weight, 0, 0, 0,
                             SHIFTJIS_CHARSET, OUT_TT_PRECIS, 0, ANTIALIASED_QUALITY, 0, face)
        old = g.SelectObject(dc, font)
        tm = TEXTMETRICW()
        g.GetTextMetricsW(dc, ctypes.byref(tm))
        buf = ctypes.create_unicode_buffer(64)
        g.GetTextFaceW(dc, 64, buf)
        gm = GLYPHMETRICS()
        identity = MAT2(_FIXED(0, 1), _FIXED(0, 0), _FIXED(0, 0), _FIXED(0, 1))
        r = g.GetGlyphOutlineW(dc, ord("H"), GGO_METRICS, ctypes.byref(gm), 0, None,
                               ctypes.byref(identity))
        cap = gm.gmBlackBoxY if r != GDI_ERROR else 0
        g.SelectObject(dc, old)
        g.DeleteObject(font)
        g.DeleteDC(dc)
        if added:
            g.RemoveFontResourceExW(path, FR_PRIVATE, None)
        return tm.tmHeight, cap, buf.value
    except Exception as e:
        print("[font]    (could not measure font: %s)" % e)
        return None


def auto_en(path, face, weight):
    m = _measure(path, face, weight)
    if not m or not m[1]:
        print("[font]    %s  EN_HEIGHT_PCT=100 (auto; metrics unavailable)" % face)
        return 100
    tm_h, cap, actual = m
    if actual and actual.isascii() and actual.lower() != face.lower():
        print('[font]    !! GDI resolved "%s", not "%s" -- is the font installed/JP-capable?'
              % (actual, face))
    en = round(100 * (tm_h / cap) / NOTO_TMHEIGHT_OVER_CAP)
    en = max(EN_MIN, min(EN_MAX, en))
    print("[font]    %s  EN_HEIGHT_PCT=%d (auto: cap %d/%d em)" % (face, en, cap, tm_h))
    return en


def register(path):
    """Make the font resolvable by name to later-spawned processes (e.g. VNTextPatch), session-
    wide and temporarily -- no permanent install. No-op off Windows / no path."""
    if path and os.name == "nt":
        g = ctypes.WinDLL("gdi32")
        g.AddFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        g.AddFontResourceExW(path, 0, None)


def unregister(path):
    if path and os.name == "nt":
        g = ctypes.WinDLL("gdi32")
        g.RemoveFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        g.RemoveFontResourceExW(path, 0, None)


def _ensure_config_orig():
    orig = CONFIG + ".orig"
    if not os.path.exists(orig) and os.path.exists(CONFIG):
        shutil.copy2(CONFIG, orig)
    return orig


def _set_key(text, key, value):
    pat = r'(<add key="%s" value=")[^"]*(")' % re.escape(key)
    new, n = re.subn(pat, lambda m: m.group(1) + value + m.group(2), text)
    if not n:
        print('[font]    !! key %s not found in config -- left unchanged' % key)
    return new


def _c_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _write_header(spec):
    weight = "FW_BOLD" if spec.bold else "FW_NORMAL"
    face = _c_escape(spec.face)
    body = (
        "// Generated by libraries/fontcfg.py from font.json -- DO NOT EDIT.\n"
        "// Delete this file (or clear font.json) to revert to the built-in default.\n"
        '#define TL_FACE_A  "%s"\n'
        '#define TL_FACE_W  L"%s"\n'
        "#define TL_WEIGHT  %s\n"
        "#define EN_HEIGHT_PCT %d\n"
        '#define TL_FONT_FILE_W L"%s"\n'
        % (face, face, weight, spec.en, _c_escape(spec.file_base))
    )
    with open(HEADER, "w", encoding="utf-8") as fh:
        fh.write(body)


def _patch_config(spec):
    orig = _ensure_config_orig()
    src = orig if os.path.exists(orig) else CONFIG
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    text = _set_key(text, "ProportionalFontName", _c_escape(spec.face))
    text = _set_key(text, "ProportionalFontBold", "true" if spec.bold else "false")
    text = _set_key(text, "ProportionalLineWidth", str(spec.width1))
    text = _set_key(text, "SecondaryProportionalLineWidth", str(spec.width2))
    with open(CONFIG, "w", encoding="utf-8") as fh:
        fh.write(text)


def _restore_defaults():
    """No custom font: revert any prior patch so the built-in Noto default is used."""
    orig = CONFIG + ".orig"
    if os.path.exists(orig):
        shutil.copy2(orig, CONFIG)
    if os.path.exists(HEADER):
        os.remove(HEADER)


def apply(game=None):
    """Resolve font.json and write all outputs. Returns the Spec, or None (default).
    Idempotent; safe to call every repack."""
    spec = resolve()
    if spec is None:
        _restore_defaults()
        print("[font]    default font (Noto Sans JP, by name)")
        return None
    _write_header(spec)
    _patch_config(spec)
    if game and os.path.isdir(game):
        dst = os.path.join(game, spec.file_base)
        if not (os.path.exists(dst) and _same(spec.file_abs, dst)):
            shutil.copy2(spec.file_abs, dst)
    print("[font]    %s  wrap=%d/%d  -> hook + VNTextPatch%s"
          % (spec.face, spec.width1, spec.width2,
             "  (font copied into game/)" if game else ""))
    return spec


def _same(a, b):
    try:
        return os.path.getsize(a) == os.path.getsize(b) and \
            open(a, "rb").read() == open(b, "rb").read()
    except OSError:
        return False


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    game = None
    if "--game" in sys.argv:
        game = sys.argv[sys.argv.index("--game") + 1]
    apply(game)
