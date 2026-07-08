"""
Handles scene .bin scripts and translation JSON representations.

A 'scene' is a run of fixed 0x90 (144)-byte slots indexed by the VM PC:
[ u8    opcode          0x64 'd' dialogue / 0x6a 'j' speaker / 0x69 'i' title ]
[ var   text            NUL-terminated Shift-JIS, then zero padding           ]
[ u8    row_index       @ +0x82 (dialogue slots): 0-based row within the page ]

A 'page' is a run of consecutive 'd' slots (1..3 on-screen lines) preceded by one 'j' speaker.
slot (empty => narration); the dialogue inherits the preceding speaker.

Branch/choice menus use a different opcode and layout:
[ u8    opcode          0x6a 0xcd 'choice option' (a run of these = one menu, ended by 0xce) ]
[ u8    branch_tag      @ +0x01  the variable value this option sets ('x'/'y'/'z', 'a'/'b', ..)]
[ var   option_text     @ +0x41  NUL-terminated Shift-JIS (the player-facing choice label)     ]
[ u8    option_index    @ +0x82  0-based position of the option within the menu                ]
The choice label lives at +0x41, NOT the usual +0x01 text field, so it is parsed/written
specially (kind == 'choice'); a translated label must fit in [+0x41 .. +0x80).

The translation string is wrapped to fit at most 3 on-screen dialogue lines, using the following priorities:
  1. greedy word wrap tries to keep whole words
  2. otherwise hyphenate on vowel-consonant boundaries
  3. otherwise truncate remainder
"""
import glob
import json
import os
import unicodedata

SLOT             = 0x90
OP_D, OP_J, OP_I = 0x64, 0x6a, 0x69
OP_CHOICE        = 0xcd                         # branch/choice menu option
PARAM_OFF        = 0x80
ROW_OFF          = 0x82
CHOICE_TAG_OFF   = 0x01                         # branch-tag byte in a choice slot
CHOICE_TEXT_OFF  = 0x41                         # option label text in a choice slot
CHOICE_MAX_BYTES = PARAM_OFF - CHOICE_TEXT_OFF - 1   # label + NUL must fit in [0x41, 0x80)
MAX_DECODE_SLOTS = 0x54600 // SLOT              # engine decode cap (2400 slots)

LINE_COLS        = 54                           # n half-width ASCII chars per line
MAX_PAGE_LINES   = 3

NAMES_FILE = "_names.json"

def _is_lead(b):
    return 0x81 <= b <= 0x9f or 0xe0 <= b <= 0xfc

def slot_text_bytes(slot):
    nul = slot.find(0, 1)
    if nul < 0:
        nul = len(slot)
    return slot[1:nul]

def decode_sjis(raw):
    """Decode raw text bytes to str; escape non-SJIS controls"""
    out = []
    i = 0
    while i < len(raw):
        b = raw[i]
        if _is_lead(b) and i + 1 < len(raw):
            try:
                out.append(raw[i:i+2].decode('cp932'))
                i += 2
                continue
            except UnicodeDecodeError:
                pass
        if 0x20 <= b <= 0x7e:
            out.append(chr(b))
            i += 1
            continue
        out.append('\\x%02x' % b)
        i += 1
    return ''.join(out)

# unicode -> ascii punctuation map
_PUNCT = {
    '—': '--',
    '–': '-',
    '―': '--',
    '−': '-',
    '‘': "'",
    '’': "'",
    '‛': "'",
    '“': '"',
    '”': '"',
    '„': '"',
    '…': '...',
    ' ': ' ',
    ' ': ' ',
    ' ': ' ',
    '⟦': '[',
    '⟧': ']',
    '×': 'x',
}

def normalize_text(s):
    """Map unicode to ASCII output; ignores escapes"""
    out = []
    for ch in s:
        if ch in _PUNCT:
            out.append(_PUNCT[ch])
            continue
        if ord(ch) <= 0x7f:
            out.append(ch)
            continue
        try:
            ch.encode('cp932')
            out.append(ch)
            continue
        except UnicodeEncodeError:
            dec = ''.join(c for c in unicodedata.normalize('NFKD', ch)
                          if not unicodedata.combining(c))
            out.append(dec if dec else ch)
    return ''.join(out)

def encode_for_slot(s, dropped=None):
    """Encode a translation string to slot bytes. Characters with no Shift-JIS representation
    (even after normalize_text) are dropped rather than failing the whole string; when `dropped`
    is a list, each removed character is appended to it for reporting."""
    s = normalize_text(s)
    out = bytearray()
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 3 <= len(s) and s[i+1] == 'x':
            out.append(int(s[i+2:i+4], 16))
            i += 4
            continue
        try:
            out += s[i].encode('cp932')
        except UnicodeEncodeError:
            if dropped is not None:
                dropped.append(s[i])
        i += 1
    return bytes(out)

def _swidth(s):
    w = 0
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 3 <= len(s) and s[i+1] == 'x':
            w += 1
            i += 4
            continue
        w += 1 if ord(s[i]) <= 0x7f else 2
        i += 1
    return w

def _take_cols(s, cols):
    w = 0
    n = 0
    for ch in s:
        cw = 1 if ord(ch) <= 0x7f else 2
        if w + cw > cols:
            break
        w += cw
        n += 1
    return max(1, n)

_VOWELS = set('aeiouy')
_DIGRAPHS = {'ch', 'sh', 'th', 'ph', 'wh', 'gh', 'ck', 'ng', 'qu', 'gu'}

def _is_cons(c):
    return c.isalpha() and c.lower() not in _VOWELS

def _best_break(w, head_cols):
    """
    Pick best split index for hyphenating a word from head_columns limit.
    Prefers last non-digraph consonant boundary that leaves >=2 chars on either end.
    """
    hi = min(_take_cols(w, head_cols), len(w) - 2)
    if hi < 2:
        return None
    good = None
    for k in range(2, hi + 1):
        a, b = w[k-1].lower(), w[k].lower()
        if _is_cons(a) and _is_cons(b) and (a + b) not in _DIGRAPHS:
            good = k
    if good is not None:
        return good
    if hi >= 3 and (w[hi-1].lower() + w[hi].lower()) in _DIGRAPHS:
        return hi - 1
    return hi

def wrap_line(text, cols, hyphenate=True):
    lines = []
    cur = ''
    for word in text.split():
        w = word
        while True:
            sep = 1 if cur else 0
            if _swidth(cur) + sep + _swidth(w) <= cols:
                cur = (cur + ' ' + w) if cur else w
                break
            if _swidth(w) <= cols and not hyphenate:
                if cur:
                    lines.append(cur)
                cur = ''
                continue
            avail = cols - (_swidth(cur) + sep)
            k = _best_break(w, avail - 1) if avail >= 3 else None
            if k is None:
                if cur:
                    lines.append(cur)
                    cur = ''
                    continue
                take = _take_cols(w, cols - 1)
                lines.append(w[:take] + '-')
                w = w[take:]
                continue
            piece = (cur + ' ' + w[:k]) if cur else w[:k]
            lines.append(piece + '-')
            cur = ''
            w = w[k:]
    if cur:
        lines.append(cur)
    return lines

def wrap_page(tr, cols, max_lines=MAX_PAGE_LINES):
    tr = normalize_text(tr).replace('(', '（').replace(')', '）')
    segs = tr.split('\n')
    def run(hyph):
        out = []
        for seg in segs:
            seg = seg.strip()
            if not seg:
                out.append('')
            else:
                out.extend(wrap_line(seg, cols, hyph))
        return out
    lines = run(False)
    if len(lines) > max_lines:
        lines = run(True)
    dropped = ' '.join(lines[max_lines:]).strip()
    return lines[:max_lines], dropped

def parse_scene(data, scene_name):
    """Return an ordered list of per-page/per-title records for one scene."""
    n = len(data) // SLOT
    units = []
    page_no = 0
    s = 0
    while s < n:
        op = data[s*SLOT]
        if op == OP_D:  # page
            start = s
            slots, lines = [], []
            while s < n and data[s*SLOT] == OP_D:
                raw = slot_text_bytes(data[s*SLOT:(s+1)*SLOT])
                slots.append(s)
                lines.append(decode_sjis(raw))
                s += 1
            sp_jp, sp_slot = None, None
            k = start - 1
            while k >= 0:
                kop = data[k*SLOT]
                if kop == OP_D:
                    break
                if kop == OP_J:
                    raw = slot_text_bytes(data[k*SLOT:(k+1)*SLOT])
                    if raw:
                        sp_jp, sp_slot = decode_sjis(raw), k
                    break
                k -= 1
            page_no += 1
            units.append({
                'scene': scene_name, 'kind': 'page', 'page': page_no,
                'slots': slots, 'speaker': sp_jp, 'speaker_slot': sp_slot,
                'jp': ''.join(lines), 'jp_lines': lines, 'tr': None,
            })
        elif op == OP_I:    # title
            raw = slot_text_bytes(data[s*SLOT:(s+1)*SLOT])
            if raw:
                t = decode_sjis(raw)
                units.append({'scene': scene_name, 'kind': 'title', 'slots': [s],
                              'jp': t, 'jp_lines': [t], 'tr': None})
            s += 1
        elif op == OP_CHOICE:   # branch/choice menu option (label @ +0x41)
            slot = data[s*SLOT:(s+1)*SLOT]
            nul = slot.find(0, CHOICE_TEXT_OFF)
            if nul < 0:
                nul = SLOT
            t = decode_sjis(slot[CHOICE_TEXT_OFF:nul])
            tag = chr(slot[CHOICE_TAG_OFF]) if 0x20 <= slot[CHOICE_TAG_OFF] <= 0x7e else ''
            units.append({'scene': scene_name, 'kind': 'choice', 'slots': [s],
                          'tag': tag, 'jp': t, 'jp_lines': [t], 'tr': None})
            s += 1
        else:
            s += 1
    return units

def load_glossary(path):
    return json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {}

def _merge_existing(units, prev_path):
    """Merges existing JSON tr fields with incoming to avoid clobber.."""
    if not os.path.exists(prev_path):
        return
    def merge_key(r):
        kind = r.get('kind')
        if kind in ('title', 'choice'):
            return (kind, r['slots'][0])
        return ('page', r.get('page'))
    prev = {}
    for r in json.load(open(prev_path, encoding='utf-8')):
        prev[merge_key(r)] = r
    for u in units:
        old = prev.get(merge_key(u))
        if old:
            if old.get('tr') is not None:
                u['tr'] = old['tr']
            if old.get('speaker_tr'):
                u['speaker_tr'] = old['speaker_tr']

def extract(scene_dir, json_dir, force=False):
    """Scenes -> per-page JSON (+ _names.json). Preserves existing tr unless force."""
    os.makedirs(json_dir, exist_ok=True)
    names_path = os.path.join(json_dir, NAMES_FILE)
    glossary = load_glossary(names_path)
    pages = titles = choices = 0
    for fn in sorted(glob.glob(os.path.join(scene_dir, '*.BIN'))):
        name = os.path.basename(fn)
        units = parse_scene(open(fn, 'rb').read(), name)
        if not units:
            continue
        out_path = os.path.join(json_dir, name.replace('.BIN', '.json'))
        if not force:
            _merge_existing(units, out_path)
        n_choice = 0
        for u in units:
            if u['kind'] == 'page':
                pages += 1
                if u['speaker'] and u['speaker'] not in glossary:
                    glossary[u['speaker']] = None
            elif u['kind'] == 'choice':
                choices += 1
                n_choice += 1
            else:
                titles += 1
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(units, f, ensure_ascii=False, indent=1)
        print('  %-14s %4d pages%s' % (
            name, sum(1 for u in units if u['kind'] == 'page'),
            '  %d choices' % n_choice if n_choice else ''))
    with open(names_path, 'w', encoding='utf-8') as f:
        json.dump(glossary, f, ensure_ascii=False, indent=1)
    print('  total %d pages, %d title cards, %d choice options, %d distinct speakers' % (
        pages, titles, choices, len(glossary)))

def _make_text_slot(op, enc, row):
    s = bytearray(SLOT)
    s[0] = op
    s[1:1+len(enc)] = enc
    s[ROW_OFF] = row & 0xff
    return bytes(s)

def _build_text_run(rec, op, cols, name, overflow, enc_err, trunc):
    out = []
    lines, dropped = wrap_page(rec['tr'], cols)
    if dropped:
        trunc.append((name, rec.get('page'), dropped))
    for row, ln in enumerate(lines):
        bad = []
        enc = encode_for_slot(ln, bad)
        if bad:
            enc_err.append((name, 'page %s' % rec.get('page'), ''.join(bad)))
        if 1 + len(enc) + 1 > SLOT:
            overflow.append((name, rec.get('page'), len(enc), ln))
            return None
        out.append(_make_text_slot(op, enc, row))
    return out or None

def _build_choice_slot(orig_slot, tr, name, overflow, enc_err):
    """Rebuild one 0xcd choice slot: overwrite only the label at +0x41, preserving the
    opcode, branch tag (+0x01) and the param tail (+0x80..). Returns new bytes, or None to
    keep the original (label too long / unencodable)."""
    bad = []
    enc = encode_for_slot(tr, bad)
    if bad:
        enc_err.append((name, 'choice', ''.join(bad)))
    if len(enc) > CHOICE_MAX_BYTES:
        overflow.append((name, 'choice', len(enc), tr))
        return None
    s = bytearray(orig_slot)
    for i in range(CHOICE_TEXT_OFF, PARAM_OFF):     # clear old label, keep NUL terminator room
        s[i] = 0
    s[CHOICE_TEXT_OFF:CHOICE_TEXT_OFF + len(enc)] = enc
    return bytes(s)

def _apply_speaker(data, rec, name, glossary, enc_err):
    sp_slot = rec.get('speaker_slot')
    if sp_slot is None:
        return 0
    sp_tr = rec.get('speaker_tr') or glossary.get(rec.get('speaker'))
    if not sp_tr:
        return 0
    base = sp_slot * SLOT
    if data[base] != OP_J:
        raise AssertionError('%s slot %d: opcode 0x%02x != 0x6a (j)' % (name, sp_slot, data[base]))
    bad = []
    enc = encode_for_slot(sp_tr, bad)
    if bad:
        enc_err.append((name, 'name slot %d' % sp_slot, ''.join(bad)))
    if 1 + PARAM_OFF - 1 < 1 + len(enc) + 1:
        enc_err.append((name, 'name slot %d (too long)' % sp_slot, sp_tr))
        return 0
    tail = bytes(data[base+PARAM_OFF:base+SLOT])
    data[base:base+SLOT] = bytes([OP_J]) + enc + b'\0' * (PARAM_OFF - 1 - len(enc)) + tail
    return 1

def build_scene(orig, recs, glossary, cols, name, overflow, enc_err, trunc):
    """Return (rebuilt_scene_bytes, slot_edits)."""
    data = bytearray(orig)
    edits = 0
    for rec in recs:
        edits += _apply_speaker(data, rec, name, glossary, enc_err)
    n = len(data) // SLOT
    starts = {rec['slots'][0]: rec for rec in recs if rec.get('tr')}
    out = bytearray()
    i = 0
    while i < n:
        rec = starts.get(i)
        if rec is None:
            out += data[i*SLOT:(i+1)*SLOT]
            i += 1
            continue
        if rec.get('kind') == 'choice':
            k = rec['slots'][0]
            if data[k*SLOT] != OP_CHOICE:
                raise AssertionError('%s slot %d: opcode 0x%02x != 0x%02x (choice)' % (
                    name, k, data[k*SLOT], OP_CHOICE))
            new = _build_choice_slot(data[k*SLOT:(k+1)*SLOT], rec['tr'], name, overflow, enc_err)
            if new is None:
                out += data[k*SLOT:(k+1)*SLOT]
            else:
                out += new
                edits += 1
            i = k + 1
            continue
        op = OP_I if rec.get('kind') == 'title' else OP_D
        orig_slots = rec['slots']
        for k in orig_slots:
            if data[k*SLOT] != op:
                raise AssertionError('%s slot %d: opcode 0x%02x != 0x%02x' % (
                    name, k, data[k*SLOT], op))
        run = _build_text_run(rec, op, cols, name, overflow, enc_err, trunc)
        if run is None:
            for k in orig_slots:
                out += data[k*SLOT:(k+1)*SLOT]
        else:
            out += b''.join(run)
            edits += len(run)
        i = orig_slots[-1] + 1
    return bytes(out), edits

def build(json_dir, scene_dir, out_dir, cols=LINE_COLS):
    import shutil
    os.makedirs(out_dir, exist_ok=True)
    order = os.path.join(scene_dir, '_rk1_dir.txt')
    if os.path.exists(order):
        shutil.copy(order, out_dir)
    glossary = load_glossary(os.path.join(json_dir, NAMES_FILE))
    by_scene = {}
    for jf in glob.glob(os.path.join(json_dir, '*.json')):
        if os.path.basename(jf).startswith('_'):
            continue
        for rec in json.load(open(jf, encoding='utf-8')):
            by_scene.setdefault(rec['scene'], []).append(rec)
    overflow, enc_err, trunc, toobig = [], [], [], []
    edits_total = scenes_written = 0
    for fn in sorted(glob.glob(os.path.join(scene_dir, '*.BIN'))):
        name = os.path.basename(fn)
        orig = open(fn, 'rb').read()
        out, edits = build_scene(orig, by_scene.get(name, []), glossary, cols,
                                 name, overflow, enc_err, trunc)
        if out == orig:
            continue
        nslots = len(out) // SLOT
        if nslots > MAX_DECODE_SLOTS:
            toobig.append((name, nslots))
            continue
        with open(os.path.join(out_dir, name), 'wb') as f:
            f.write(out)
        scenes_written += 1
        edits_total += edits
    print('  built %d slot-edits across %d changed scenes' % (edits_total, scenes_written))
    if trunc:
        print('  !! %d pages overflowed %d lines; tail TRUNCATED (shorten the translation):' % (
            len(trunc), MAX_PAGE_LINES))
        for nm, pg, tail in trunc[:25]:
            print('     %-12s page %-5s  dropped: %.40s' % (nm, pg, tail))
    if toobig:
        print('  !! %d scenes would exceed the %d-slot decode cap and were SKIPPED:' % (
            len(toobig), MAX_DECODE_SLOTS))
        for nm, nslots in toobig:
            print('     %-12s %d slots' % (nm, nslots))
    if overflow:
        print('  !! %d lines exceed a single 144-byte slot (kept Japanese):' % len(overflow))
        for nm, pg, nbytes, ln in overflow[:25]:
            print('     %-12s page %-5s  %d bytes  %.40s' % (nm, pg, nbytes, ln))
    if enc_err:
        print('  !! %d strings had non-Shift-JIS chars REMOVED (rest encoded):' % len(enc_err))
        for nm, where, s in enc_err[:25]:
            print('     %-12s %-12s dropped: %s' % (nm, where, s))
    return scenes_written
