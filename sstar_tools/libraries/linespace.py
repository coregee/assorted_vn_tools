"""
Patches Game.exe to use a custom vertical line-spacing value.
Default is 0x27/39, recommended is 30px.
"""
import struct

FONT_PX = 0x18
ORIG_PITCH = 0x27
DEFAULT_PITCH = 30

# Live typewriter
TW_MUL_SIG = bytes.fromhex('8d3489c1e603')     # lea esi,[ecx+ecx*4]; shl esi,3
TW_SUB_SIG = bytes.fromhex('2bf1')             # sub esi,ecx

# Page draw
LINE_ANCHOR, SPK_ANCHOR = 0x44, 0x38
REDRAW = [   # (file_off, anchor, idx)
    (0x00b8e4, LINE_ANCHOR, 1), (0x00b8f5, LINE_ANCHOR, 2),
    (0x00b94a, SPK_ANCHOR,  1), (0x00b95c, SPK_ANCHOR,  2),
    (0x00b99f, LINE_ANCHOR, 1), (0x00b9b4, LINE_ANCHOR, 2),
    (0x00ba04, LINE_ANCHOR, 1), (0x00ba15, LINE_ANCHOR, 2),
    (0x00ba6a, SPK_ANCHOR,  1), (0x00ba7c, SPK_ANCHOR,  2),
    (0x00babf, LINE_ANCHOR, 1), (0x00bad4, LINE_ANCHOR, 2),
]

# Text log
BL_BASE1, BL_BASE2 = 0x1bc, 0x1c8
BACKLOG = {
    'pitch1': (0x00df66, '<B', 0x27),
    'bound1': (0x00df6f, '<I', 0x258),
    'pitch2': (0x00dfa3, '<B', 0x27),
    'bound2': (0x00dfac, '<I', 0x264),
}

def locate_typewriter(data):
    """Return (mul_off, sub_off) for the live typewriter pitch."""
    i = data.find(TW_MUL_SIG)
    if i < 0 or data.find(TW_MUL_SIG, i + 1) >= 0:
        return None
    sub = data.find(TW_SUB_SIG, i + len(TW_MUL_SIG), i + 16)
    if sub < 0:
        return None
    return i, sub

def _redraw_ok(data):
    for off, anchor, idx in REDRAW:
        if data[off] != ((anchor + idx * 0x27) & 0xff):
            return False
    for _, (off, fmt, orig) in BACKLOG.items():
        if data[off:off+struct.calcsize(fmt)] != struct.pack(fmt, orig):
            return False
    return True

def read_pitch(data, tw_off):
    """Read the current dialogue pitch at the located typewriter offset."""
    if data[tw_off:tw_off+2] == b'\x6b\xf1':              # imul esi,ecx,imm8
        return data[tw_off+2]
    if data[tw_off:tw_off+len(TW_MUL_SIG)] == TW_MUL_SIG:
        return ORIG_PITCH
    return None

def apply_pitch(data, pitch):
    """
    Patches the Game.exe in-place with the specified pitch value.
    Returns array of note strings for console output.
    """
    loc = locate_typewriter(data)
    if loc is None:
        raise ValueError("Couldn't find typewriter from provided Game.exe. Is this the right game?")
    mul_off, sub_off = loc
    notes = []
    data[mul_off:mul_off+6] = bytes([0x6b, 0xf1, pitch, 0x90, 0x90, 0x90])
    data[sub_off:sub_off+2] = b'\x90\x90'
    if _redraw_ok(data):
        for off, anchor, idx in REDRAW:
            y = anchor + idx * pitch
            if idx == 1 and y > 0x7f:
                raise ValueError("Pitch value exceeds draw limits. Pick a smaller value.")
            data[off] = y & 0xff
        newbl = {'pitch1': pitch, 'bound1': BL_BASE1 + 4*pitch,
                 'pitch2': pitch, 'bound2': BL_BASE2 + 4*pitch}
        for name, (off, fmt, _) in BACKLOG.items():
            struct.pack_into(fmt, data, off, newbl[name])
    else:
        notes.append("Couldn't patch backlog for the provided Game.exe. Is this the right game?")
    return notes
