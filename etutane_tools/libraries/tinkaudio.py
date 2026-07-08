"""
Tink/Song header (de/en)cryption for the engine's Ogg audio.

The leading 'OggS' page marker is replaced with a 4-byte signature ('Tink' or 'Song'),
and the first HEADER_LEN bytes are XORed with a fixed key -- the long copyright-GUID
string registered for that signature. Some entries store the signature behind a short
prefix at offset 0x0C, which decrypt() splices out. Bytes past the header are untouched.
"""
HEADER_LEN = 0xE1F          # number of leading bytes that are XOR-encrypted
TINK_SIG = b"Tink"
SONG_SIG = b"Song"

KEYS = {
    # 'Tink' = "DBB3206F-F171-4885-A131-EC7FBA6FF491 Copyright 2004 Cyberworks \"TinkerBell\"., all rights reserved.\0"
    TINK_SIG: bytes.fromhex(
        "44424233323036462d463137312d343838352d413133312d45433746424136464634393120"
        "436f707972696768742032303034204379626572776f726b73202254696e6b657242656c6c"
        "222e2c20616c6c207269676874732072657365727665642e00"),
    SONG_SIG: bytes.fromhex(
        "343932333345443439313145343863363845424631444441434533413737353241384235324"
        "433443133433334653530394642452d45334546444533463244363100"),
}

def _xor(buf, key):
    """XOR buf[4:] with the cycling key. Note the cursor wraps back to index 1, not 0,
    after each full pass -- this matches the engine's keystream."""
    k = 0
    for i in range(4, len(buf)):
        buf[i] ^= key[k]
        k += 1
        if k >= len(key):
            k = 1

def is_tink(blob):
    """True if `blob` is Tink/Song-encrypted audio (signature at offset 0 or 0x0C)."""
    return blob[:4] in KEYS or (len(blob) >= 0x10 and blob[0xC:0x10] in KEYS)

def decrypt(blob):
    """Decrypt a Tink/Song blob back to a plain Ogg stream (restores the 'OggS' marker)."""
    n = min(HEADER_LEN, len(blob))
    if blob[:4] in KEYS:
        key = KEYS[bytes(blob[:4])]
        header = bytearray(blob[:n])
        tail = blob[n:]
    elif len(blob) >= 0x10 and blob[0xC:0x10] in KEYS:
        key = KEYS[bytes(blob[0xC:0x10])]
        header = bytearray(n)
        header[0:4] = blob[0:4]
        header[4:n] = blob[0x10:0x10 + (n - 4)]
        tail = blob[0x10 + (n - 4):]
    else:
        raise ValueError("not Tink-encrypted audio")
    header[0:4] = b"OggS"
    _xor(header, key)
    return bytes(header) + bytes(tail)

def encrypt(ogg, sig=TINK_SIG):
    """Re-encrypt a plain Ogg stream under `sig` (the inverse of decrypt for the
    signature-at-offset-0 layout)."""
    if ogg[:4] != b"OggS":
        raise ValueError("not an Ogg stream")
    key = KEYS[sig]
    n = min(HEADER_LEN, len(ogg))
    out = bytearray(ogg[:n])
    _xor(out, key)
    out[0:4] = sig
    return bytes(out) + bytes(ogg[n:])
