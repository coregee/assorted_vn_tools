"""
Patches NULL-terminated SJIS strings in-place.

Two record kinds share _system.json:
  - data records   {off, va, max_bytes, jp, tr}      cp932 in .data/.rdata,
                                                      patched in place here.
  - resource records {res, lang, idx, jp, tr}        UTF-16 menu/dialog text
                                                      in .rsrc, rebuilt by rsrc.py.
"""
import json
import os
import struct

from . import rsrc

SYSTEM_FILE = '_system.json'

def pe_sections(data):
    """Return [(name, vaddr, vsize, raw, rsize), ...] for the PE in `data`."""
    e_lfanew = struct.unpack_from('<I', data, 0x3c)[0]
    if data[e_lfanew:e_lfanew+4] != b'PE\0\0':
        raise ValueError('not a PE executable')
    nsec = struct.unpack_from('<H', data, e_lfanew + 6)[0]
    opt = struct.unpack_from('<H', data, e_lfanew + 20)[0]
    sectbl = e_lfanew + 24 + opt
    secs = []
    for i in range(nsec):
        o = sectbl + i * 40
        name = data[o:o+8].split(b'\0')[0].decode('latin1')
        vsize, vaddr, rsize, raw = struct.unpack_from('<IIII', data, o + 8)
        secs.append((name, vaddr, vsize, raw, rsize))
    return secs

def _is_lead(b):
    return 0x81 <= b <= 0x9f or 0xe0 <= b <= 0xfc

def _decode_run(buf, i):
    """
    Decode a maximal printable cp932 run starting at i; return (text, end) or
    (None, end) if a non-printable/undecodable byte is hit before a NULL.
    """
    chars = []
    j = i
    while j < len(buf):
        c = buf[j]
        if c == 0:
            break
        if _is_lead(c) and j + 1 < len(buf):
            try:
                chars.append(buf[j:j+2].decode('cp932'))
                j += 2
                continue
            except UnicodeDecodeError:
                return None, j
        if 0x20 <= c <= 0x7e:
            chars.append(chr(c))
            j += 1
            continue
        return None, j
    return ''.join(chars), j

def _looks_japanese(s):
    kana = sum(1 for ch in s if 0x3040 <= ord(ch) <= 0x30ff)
    kanji = sum(1 for ch in s if 0x4e00 <= ord(ch) <= 0x9fff)
    return kana >= 1 or kanji >= 2

def scan(exe_data, sections=('.data', '.rdata')):
    """
    Find candidate NULL-terminated Japanese UI strings.
    Returns ordered list of {off, va, max_bytes, jp}.
    """
    out = []
    for name, vaddr, vsize, raw, rsize in pe_sections(exe_data):
        if name not in sections:
            continue
        seg = exe_data[raw:raw + rsize]
        i = 0
        while i < len(seg):
            if seg[i] == 0:
                i += 1
                continue
            s, j = _decode_run(seg, i)
            if s is not None and j < len(seg) and seg[j] == 0 and len(s) >= 2 and _looks_japanese(s):
                off = raw + i
                out.append({'off': off, 'va': '0x%06x' % (vaddr + i),
                            'max_bytes': j - i, 'jp': s})
                i = j + 1
            else:
                i += 1
    return out

def extract(exe_path, out_json, force=False):
    """
    Scan Game.exe into script/_system.json (list of records, tr=null): cp932
    strings from .data/.rdata plus UTF-16 menu/dialog text from .rsrc.
    Preserves any existing tr unless force. Returns the record count.
    """
    data = open(exe_path, 'rb').read()
    found = scan(data)
    res_found = rsrc.extract(data)
    prev_data, prev_res = {}, {}
    if os.path.exists(out_json) and not force:
        for r in json.load(open(out_json, encoding='utf-8')):
            if 'off' in r:
                prev_data[r['off']] = r.get('tr')
            elif 'res' in r:
                prev_res[(r['res'], r['lang'], r['idx'])] = r.get('tr')
    recs = []
    for f in found:
        recs.append({'off': f['off'], 'va': f['va'], 'max_bytes': f['max_bytes'],
                     'jp': f['jp'], 'tr': prev_data.get(f['off'])})
    for r in res_found:
        r['tr'] = prev_res.get((r['res'], r['lang'], r['idx']))
        recs.append(r)
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(recs, f, ensure_ascii=False, indent=1)
    return len(recs)

def apply(data, recs):
    """
    Patch a Game.exe bytearray in place with each record's tr (cp932-encoded, NULL-padded to the original byte length).
    Returns (n_patched, warnings).
    """
    from . import scenetext
    npatched = 0
    warnings = []
    for r in recs:
        tr = r.get('tr')
        if not tr or 'off' not in r:
            continue
        off = r['off']
        cap = r['max_bytes']
        try:
            enc = scenetext.encode_for_slot(tr)
        except UnicodeEncodeError:
            warnings.append((off, 'non-cp932', tr))
            continue
        if len(enc) > cap:
            warnings.append((off, '%d>%d bytes' % (len(enc), cap), tr))
            continue
        data[off:off + cap + 1] = enc + b'\0' * (cap + 1 - len(enc))
        npatched += 1
    return npatched, warnings
