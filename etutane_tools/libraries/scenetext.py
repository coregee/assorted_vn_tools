"""
Converts scenario ('a0') string records to editable JSON and back, wrapping the EN
translation to fit on screen.

Scene text is carried by the 'S' string records of an a0 blob (see tinkerbell). Within
a record the engine stores two bytes per glyph -- a full-width cp932 pair, or 0x00 + an
ASCII byte for half-width -- and uses 0xFE (TERM) as the line break / record terminator.

A record whose decoded text is bracketed 【...】 is a *name* record: it names the speaker
of the line that follows, and its translation is supplied through the _names.json glossary
rather than inline. Every other string record is one displayed line: 'dialogue' if a name
preceded it, otherwise 'narration'.

extract() emits, per a0 file, {file, lines:[{i, kind, speaker, jp, translated}, ...]};
build() re-encodes the `translated` fields (and glossary names) back into the records.

The translation is wrapped to the line budget for its kind (narration 4, dialogue 3) by
greedy word-wrap, then vowel/consonant-boundary hyphenation, then truncation.
"""
import glob
import json
import os
import re
import struct
import unicodedata
from . import tinkerbell as tb

TERM = 0xFE     # line break / record terminator
LINE_COLS = 63  # half-width cells per row
MAX_LINES = {"narration": 4, "dialogue": 3}
NAMES_FILE = "_names.json"
_PUNCT = {
    "—": "--",
    "–": "-",
    "―": "--",
    "−": "-",
    "‘": "'",
    "’": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "…": "...",
    "　": " ",
    " ": " ",
    " ": " ",
    "×": "x",
}

def normalize_text(s):
    """Map unicode punctuation/spaces to cp932-safe equivalents; strip combining marks
    from anything cp932 still can't hold (leaving the base letter)."""
    out = []
    for ch in s:
        if ch in _PUNCT:
            out.append(_PUNCT[ch])
            continue
        if ord(ch) <= 0x7f:
            out.append(ch)
            continue
        try:
            ch.encode("cp932")
            out.append(ch)
        except UnicodeEncodeError:
            dec = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
            out.append(dec if dec else ch)
    return "".join(out)

def _cp932_cols(s):
    """Display width of `s` in half-width cells (1 per half-width char, 2 per full-width)."""
    n = 0
    for c in s:
        try:
            n += 1 if len(c.encode("cp932")) == 1 else 2
        except Exception:
            n += 2
    return n

def _take_cols(s, cols):
    w = n = 0
    for ch in s:
        cw = _cp932_cols(ch)
        if w + cw > cols:
            break
        w += cw
        n += 1
    return max(1, n)

_VOWELS = set("aeiouy")
_DIGRAPHS = {"ch", "sh", "th", "ph", "wh", "gh", "ck", "ng", "qu", "gu", "ll", "ss", "tt", "ff"}

def _is_cons(c):
    return c.isalpha() and c.lower() not in _VOWELS

def _best_break(w, head_cols):
    """Pick a hyphenation index for word `w` within `head_cols`: prefer the last non-digraph
    consonant boundary leaving >= 2 chars each side; returns None if it can't split."""
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
    """Greedily wrap one paragraph to `cols`-cell lines, hyphenating over-long words when allowed."""
    lines = []
    cur = ""
    for word in text.split():
        w = word
        while True:
            sep = 1 if cur else 0
            if _cp932_cols(cur) + sep + _cp932_cols(w) <= cols:
                cur = (cur + " " + w) if cur else w
                break
            if _cp932_cols(w) <= cols and not hyphenate:
                if cur:
                    lines.append(cur)
                cur = ""
                continue
            avail = cols - (_cp932_cols(cur) + sep)
            k = _best_break(w, avail - 1) if avail >= 3 else None
            if k is None:
                if cur:
                    lines.append(cur)
                    cur = ""
                    continue
                take = _take_cols(w, cols - 1)
                lines.append(w[:take] + "-")
                w = w[take:]
                continue
            piece = (cur + " " + w[:k]) if cur else w[:k]
            lines.append(piece + "-")
            cur = ""
            w = w[k:]
    if cur:
        lines.append(cur)
    return lines

def wrap_page(text, cols=LINE_COLS, max_lines=MAX_LINES["narration"]):
    """Wrap `text` to at most `max_lines` of `cols` cells (no-hyphen first, hyphenated if
    that overflows). Returns (lines, dropped_tail) where dropped_tail is the truncated rest."""
    segs = normalize_text(text).split("\n")
    def run(hyph):
        out = []
        for seg in segs:
            seg = seg.strip()
            out.append("") if not seg else out.extend(wrap_line(seg, cols, hyph))
        return out
    lines = run(False)
    if len(lines) > max_lines:
        lines = run(True)
    dropped = " ".join(lines[max_lines:]).strip()
    return lines[:max_lines], dropped

_ESC_HH = re.compile(r"«([0-9A-Fa-f]{2})»")

def _to_engine_body(text):
    """Encode display text to engine bytes: «HH» escapes pass through as that raw byte,
    '\\n' becomes the 0xFE 0x00 break, full-width chars keep their cp932 pair, and each
    half-width char is stored as 0x00 + its ASCII byte (the engine's 2-bytes-per-glyph form)."""
    out = bytearray()
    for part in re.split(r"(«[0-9A-Fa-f]{2}»)", text):
        if not part:
            continue
        m = _ESC_HH.fullmatch(part)
        if m:
            out.append(int(m.group(1), 16))
            continue
        for ch in part:
            if ch == "\n":
                out += b"\xfe\x00"
                continue
            b = ch.encode("cp932")
            out += (b"\x00" + b) if len(b) == 1 else b
    return bytes(out)

def _encode_line(en, had_term):
    """Encode one (possibly multi-line) string and re-append the 0xFE terminator the
    original record carried, if any."""
    body = _to_engine_body(en)
    if had_term and not body.endswith(bytes([TERM])):
        body += bytes([TERM])
    return body

def a0_strings(blob):
    """Yield (string_index, payload, decrypted_body) for each string record in an a0 blob.
    The index counts only string records, matching the `i` field written to JSON."""
    si = 0
    for off, payload in tb.parse_records(blob):
        if tb.is_string_record(payload):
            yield si, payload, tb.decrypt_string(payload)
            si += 1

def strip_term(body):
    """Split a trailing 0xFE terminator off `body`; returns (body_without_term, had_term)."""
    if body.endswith(bytes([TERM])):
        return body[:-1], True
    return body, False

def is_name(text):
    """True for a 【speaker】 name record."""
    return text.startswith("【") and text.endswith("】")

def load_names(names_path):
    """Load the _names.json glossary {jp_name: translation_or_None}, or {} if absent."""
    return json.load(open(names_path, encoding="utf-8")) if os.path.exists(names_path) else {}

def _merge_existing_translated(lines, prev_path):
    """Carry over previously-saved `translated` fields from prev_path, keyed by line index."""
    if not os.path.exists(prev_path):
        return
    prev = {ln["i"]: ln for ln in json.load(open(prev_path, encoding="utf-8")).get("lines", [])}
    for ln in lines:
        old = prev.get(ln["i"])
        if old and old.get("translated") is not None:
            ln["translated"] = old["translated"]

def extract(orig_dir, json_dir, names_path, force=False):
    """Decode every a0 blob in `orig_dir` to <name>.json under `json_dir`, collecting
    every 【name】 into the _names.json glossary. Existing translations are preserved
    unless `force`."""
    os.makedirs(json_dir, exist_ok=True)
    names = load_names(names_path)
    files = sorted(glob.glob(os.path.join(orig_dir, "*.a0")))
    if not files:
        raise SystemExit("!! no %s\\*.a0 -- run extract with flag first" % os.path.basename(orig_dir))
    total_lines = total_names = 0
    for f in files:
        blob = open(f, "rb").read()
        lines = []
        pending_speaker = None
        for si, payload, body in a0_strings(blob):
            jp = tb.body_to_text(strip_term(body)[0])
            if is_name(jp):
                names.setdefault(jp, None)
                pending_speaker = jp[1:-1]
                total_names += 1
                continue
            speaker = pending_speaker
            pending_speaker = None
            kind = "dialogue" if speaker else "narration"
            lines.append({"i": si, "kind": kind, "speaker": speaker, "jp": jp, "translated": None})
        out_path = os.path.join(json_dir, os.path.splitext(os.path.basename(f))[0] + ".json")
        if not force:
            _merge_existing_translated(lines, out_path)
        total_lines += len(lines)
        json.dump({"file": os.path.basename(f), "lines": lines},
                  open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(names, open(names_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("  %d scripts, %d text lines (%d 【name】 records -> glossary), %d distinct speakers"
          % (len(files), total_lines, total_names, len(names)))

def build(json_dir, orig_dir, out_dir, names_path, cols=LINE_COLS):
    """Re-encode the `translated` lines (and glossary names) from `json_dir` back into the
    a0 blobs from `orig_dir`, writing patched copies to `out_dir`. Each line is wrapped to
    `cols`; overflow and non-SJIS encoding problems are reported, not fatal."""
    os.makedirs(out_dir, exist_ok=True)
    names = load_names(names_path)
    names_active = any(v for v in names.values())
    jfiles = sorted(f for f in glob.glob(os.path.join(json_dir, "*.json"))
                    if not os.path.basename(f).startswith("_"))   # skip _names.json
    if not jfiles:
        raise SystemExit("!! no %s\\*.json -- run extract first" % os.path.basename(json_dir))
    changed = 0
    errors, warns = [], []
    for jf in jfiles:
        spec = json.load(open(jf, encoding="utf-8"))
        a0name = spec["file"]
        orig = open(os.path.join(orig_dir, a0name), "rb").read()
        tmap = {ln["i"]: (ln["translated"], ln.get("kind", "narration"))
                for ln in spec["lines"] if ln.get("translated")}
        if not tmap and not names_active:
            open(os.path.join(out_dir, a0name), "wb").write(orig)
            continue
        out = bytearray()
        si = 0
        orig_end = 0
        file_changed = False
        for off, payload in tb.parse_records(orig):
            orig_end = off + 4 + len(payload)
            if tb.is_string_record(payload):
                body = tb.decrypt_string(payload)
                _t, had_term = strip_term(body)
                jp = tb.body_to_text(_t)
                if is_name(jp):
                    new_en = names.get(jp)
                    if new_en:
                        try:
                            payload = tb.encrypt_string(_encode_line(new_en, had_term))
                            file_changed = True
                        except UnicodeEncodeError as ex:
                            errors.append("%s#%d: non-SJIS char in name: %s" % (a0name, si, ex))
                else:
                    entry = tmap.get(si)
                    if entry:
                        en_text, kind = entry
                        budget = MAX_LINES.get(kind, MAX_LINES["narration"])
                        lines, dropped = wrap_page(en_text, cols, budget)
                        if dropped:
                            warns.append("%s#%d (%s): >%d lines -> truncated; dropped: %r"
                                         % (a0name, si, kind, budget, dropped))
                        try:
                            payload = tb.encrypt_string(_encode_line("\n".join(lines), had_term))
                            file_changed = True
                        except UnicodeEncodeError as ex:
                            errors.append("%s#%d: non-SJIS char in translated: %s" % (a0name, si, ex))
                si += 1
            out += struct.pack("<I", len(payload)) + payload
        out += orig[orig_end:]
        open(os.path.join(out_dir, a0name), "wb").write(bytes(out))
        if file_changed:
            changed += 1
    print("  built %d scripts (%d with translations)" % (len(jfiles), changed))
    if errors:
        print("  !! %d encoding error(s) (skipped):" % len(errors))
        for e in errors[:20]:
            print("     " + e)
    if warns:
        print("  !! %d line(s) overflowed and were truncated:" % len(warns))
        for w in warns[:40]:
            print("     " + w)
    return changed
