"""
Converts scenario ('a0') dialogue pages to editable JSON and back, wrapping the EN
translation to fit on screen.

Scene text is carried by the 'S' string records of an a0 blob (see tinkerbell). Within
a record the engine stores two bytes per glyph -- a full-width cp932 pair, or 0x00 + an
ASCII byte for half-width -- and uses 0xFE (TERM) as the displayed-line terminator.
One or more consecutive string records immediately before the engine's page-advance
command form a single dialogue/narration page.

A record whose decoded text is bracketed 【...】 is a *name* record: it names the speaker
of the line that follows, and its translation is supplied through the _names.json glossary
rather than inline. A page is 'dialogue' if a name preceded it, otherwise 'narration'.

extract() emits, per a0 file, page records with `string_indices` and `jp_lines` preserving
the physical source-line layout; build() reflows each translated page back into the
required number of string records (and also rebuilds glossary names).

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
PAGE_ADVANCE = b"M#NF\x00\x00\x00"
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

def is_page_advance(payload):
    """True for the command that displays/advances the accumulated text page."""
    return payload == PAGE_ADVANCE

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
    if not os.path.exists(names_path):
        return {}
    with open(names_path, encoding="utf-8") as stream:
        return json.load(stream)

def _entry_indices(entry):
    """Return an extracted entry's physical string indices (old line JSON is supported)."""
    indices = entry.get("string_indices")
    if (isinstance(indices, list) and indices
            and all(isinstance(index, int) and not isinstance(index, bool) for index in indices)):
        return tuple(indices)
    index = entry.get("i")
    return (index,) if isinstance(index, int) and not isinstance(index, bool) else ()

def _merge_existing_translated(pages, prev_path):
    """Carry translations across re-extraction, including old one-record-per-line JSON."""
    if not os.path.exists(prev_path):
        return
    with open(prev_path, encoding="utf-8") as stream:
        document = json.load(stream)
    previous = document.get("lines", document.get("pages", []))
    exact = {_entry_indices(entry): entry for entry in previous if _entry_indices(entry)}
    by_index = {index: entry for entry in previous for index in _entry_indices(entry)}
    for page in pages:
        indices = _entry_indices(page)
        old = exact.get(indices)
        if old and old.get("translated") is not None:
            page["translated"] = old["translated"]
            continue
        parts = [by_index.get(index) for index in indices]
        if parts and all(part is not None and part.get("translated") is not None for part in parts):
            page["translated"] = "\n".join(part["translated"] for part in parts)

def _page_entry(items, speaker, page_number=None):
    indices = [item[0] for item in items]
    source_lines = [item[1] for item in items]
    entry = {
        "i": indices[0],
        "string_indices": indices,
        "kind": "dialogue" if speaker else "narration",
        "speaker": speaker,
        "jp": "\n".join(source_lines),
        "jp_lines": source_lines,
        "translated": None,
    }
    if page_number is not None:
        entry["page"] = page_number
    return entry

def extract_pages(blob, names):
    """Return page-level text entries and update `names` with encountered speakers.

    The text renderer accumulates a consecutive run of S records and displays it when
    `PAGE_ADVANCE` follows. Other strings (menus/config labels) remain independent so
    unrelated UI records are never merged merely because they share a script file.
    """
    pages = []
    run = []
    pending_speaker = None
    string_index = 0
    page_number = 0

    def flush(as_page=False):
        nonlocal run, pending_speaker, page_number
        if not run:
            return
        if as_page:
            page_number += 1
            pages.append(_page_entry(run, pending_speaker, page_number))
        else:
            for position, item in enumerate(run):
                pages.append(_page_entry([item], pending_speaker if position == 0 else None))
        run = []
        pending_speaker = None

    for _off, payload in tb.parse_records(blob):
        if tb.is_string_record(payload):
            body, _had_term = strip_term(tb.decrypt_string(payload))
            text = tb.body_to_text(body)
            if is_name(text):
                flush()
                names.setdefault(text, None)
                pending_speaker = text[1:-1]
            else:
                run.append((string_index, text))
            string_index += 1
            continue
        if is_page_advance(payload):
            flush(as_page=True)
        elif run:
            flush()
    flush()
    return pages

def extract(orig_dir, json_dir, names_path, force=False):
    """Decode every a0 blob in `orig_dir` to <name>.json under `json_dir`, collecting
    every 【name】 into the _names.json glossary. Existing translations are preserved
    unless `force`."""
    os.makedirs(json_dir, exist_ok=True)
    names = load_names(names_path)
    files = sorted(glob.glob(os.path.join(orig_dir, "*.a0")))
    if not files:
        raise SystemExit("!! no %s\\*.a0 -- run extract with flag first" % os.path.basename(orig_dir))
    total_pages = total_source_lines = 0
    for f in files:
        with open(f, "rb") as stream:
            blob = stream.read()
        pages = extract_pages(blob, names)
        out_path = os.path.join(json_dir, os.path.splitext(os.path.basename(f))[0] + ".json")
        if not force:
            _merge_existing_translated(pages, out_path)
        total_pages += len(pages)
        total_source_lines += sum(len(page["string_indices"]) for page in pages)
        with open(out_path, "w", encoding="utf-8") as stream:
            json.dump({"file": os.path.basename(f), "lines": pages}, stream,
                      ensure_ascii=False, indent=1)
    with open(names_path, "w", encoding="utf-8") as stream:
        json.dump(names, stream, ensure_ascii=False, indent=1)
    print("  %d scripts, %d dialogue pages from %d source lines, %d distinct speakers"
          % (len(files), total_pages, total_source_lines, len(names)))

def build(json_dir, orig_dir, out_dir, names_path, cols=LINE_COLS, review_report=None):
    """Re-encode translated pages (and glossary names) from `json_dir` back into the
    a0 blobs from `orig_dir`, writing patched copies to `out_dir`. Each page is wrapped
    to `cols`; overflow and non-SJIS encoding problems are reported, not fatal."""
    os.makedirs(out_dir, exist_ok=True)
    names = load_names(names_path)
    names_active = any(v for v in names.values())
    jfiles = sorted(f for f in glob.glob(os.path.join(json_dir, "*.json"))
                    if not os.path.basename(f).startswith("_"))   # skip _names.json
    if not jfiles:
        raise SystemExit("!! no %s\\*.json -- run extract first" % os.path.basename(json_dir))
    changed = 0
    errors, warns, review_issues = [], [], []
    for jf in jfiles:
        with open(jf, encoding="utf-8") as stream:
            spec = json.load(stream)
        a0name = spec["file"]
        with open(os.path.join(orig_dir, a0name), "rb") as stream:
            orig = stream.read()
        entry_key = "lines" if isinstance(spec.get("lines"), list) else "pages"
        entries = spec.get(entry_key, [])
        translated_entries = [(entry_index, entry) for entry_index, entry in enumerate(entries)
                              if entry.get("translated") and _entry_indices(entry)]
        if not translated_entries and not names_active:
            with open(os.path.join(out_dir, a0name), "wb") as stream:
                stream.write(orig)
            continue
        original_terms = {}
        for si, _payload, body in a0_strings(orig):
            _text, original_terms[si] = strip_term(body)
        replacements = {}
        extras_after = {}
        claimed = set()
        for entry_index, entry in translated_entries:
            indices = _entry_indices(entry)
            overlap = claimed.intersection(indices)
            if overlap:
                errors.append("%s: overlapping string indices %s" %
                              (a0name, ", ".join(str(index) for index in sorted(overlap))))
                continue
            if any(index not in original_terms for index in indices):
                errors.append("%s: unknown string index in %r" % (a0name, indices))
                continue
            claimed.update(indices)
            budget = MAX_LINES.get(entry.get("kind", "narration"), MAX_LINES["narration"])
            wrapped, dropped = wrap_page(entry["translated"], cols, budget)
            if not wrapped:
                wrapped = [""]
            if dropped:
                warns.append("%s#%d (%s): >%d lines -> truncated; dropped: %r"
                             % (a0name, indices[0], entry.get("kind", "narration"),
                                budget, dropped))
                review_issues.append({
                    "path": "script/" + os.path.basename(jf),
                    "pointer": "/%s/%d" % (entry_key, entry_index),
                    "reason": ("Repack truncated this translation after %d on-screen lines. "
                               "Dropped text: %s" % (budget, dropped)),
                    "details": {
                        "kind": "line_overflow",
                        "max_lines": budget,
                        "dropped_text": dropped,
                    },
                })
            try:
                for position, index in enumerate(indices):
                    replacements[index] = (tb.encrypt_string(
                        _encode_line(wrapped[position], original_terms[index]))
                                           if position < len(wrapped) else None)
                if len(wrapped) > len(indices):
                    extras_after[indices[-1]] = [tb.encrypt_string(
                        _encode_line(line, original_terms[indices[-1]]))
                                                 for line in wrapped[len(indices):]]
            except UnicodeEncodeError as ex:
                for index in indices:
                    replacements.pop(index, None)
                extras_after.pop(indices[-1], None)
                errors.append("%s#%d: non-SJIS char in translated page: %s" %
                              (a0name, indices[0], ex))
        out = bytearray()
        si = 0
        orig_end = 0
        file_changed = False
        for off, payload in tb.parse_records(orig):
            orig_end = off + 4 + len(payload)
            processed_string_index = None
            if tb.is_string_record(payload):
                processed_string_index = si
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
                    if si in replacements:
                        payload = replacements[si]
                        file_changed = True
                si += 1
            if payload is not None:
                out += struct.pack("<I", len(payload)) + payload
            for extra in extras_after.get(processed_string_index, ()):
                out += struct.pack("<I", len(extra)) + extra
        out += orig[orig_end:]
        with open(os.path.join(out_dir, a0name), "wb") as stream:
            stream.write(bytes(out))
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
    if review_report:
        with open(review_report, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "issues": review_issues}, stream,
                      ensure_ascii=False, indent=1)
    return changed
