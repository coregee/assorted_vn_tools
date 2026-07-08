"""
Extracts/patches UI strings held in the PE resource section (.rsrc).
"""
import struct

RT_NAMES = {1: 'CURSOR', 2: 'BITMAP', 3: 'ICON', 4: 'MENU', 5: 'DIALOG',
            6: 'STRING', 8: 'FONT', 9: 'ACCELERATOR', 10: 'RCDATA',
            11: 'MESSAGETABLE', 12: 'GROUP_CURSOR', 14: 'GROUP_ICON',
            16: 'VERSION', 24: 'MANIFEST'}
RT_IDS = {v: k for k, v in RT_NAMES.items()}

TRANSLATABLE = ('MENU', 'DIALOG')

MF_POPUP = 0x0010
MF_END   = 0x0080
DS_SETFONT = 0x0040


class PE:
    """Minimal PE accessor exposing the bits we rewrite."""
    def __init__(self, data):
        self.e = struct.unpack_from('<I', data, 0x3c)[0]
        if data[self.e:self.e + 4] != b'PE\0\0':
            raise ValueError('not a PE executable')
        self.opt = self.e + 24
        self.soh = struct.unpack_from('<H', data, self.e + 20)[0]
        self.nsec = struct.unpack_from('<H', data, self.e + 6)[0]
        self.file_align = struct.unpack_from('<I', data, self.opt + 0x24)[0]
        self.sect_align = struct.unpack_from('<I', data, self.opt + 0x20)[0]
        self.sectbl = self.e + 24 + self.soh
        self.sections = []
        for i in range(self.nsec):
            o = self.sectbl + i * 40
            name = data[o:o + 8].split(b'\0')[0].decode('latin1')
            vsize, vaddr, rsize, raw = struct.unpack_from('<IIII', data, o + 8)
            self.sections.append({'name': name, 'vsize': vsize, 'vaddr': vaddr,
                                  'rsize': rsize, 'raw': raw, 'hdr': o})

    def section(self, name):
        for s in self.sections:
            if s['name'] == name:
                return s
        return None


def _align(n, a):
    return (n + a - 1) // a * a


class RDir:
    __slots__ = ('entries',)
    def __init__(self):
        self.entries = []

class RData:
    __slots__ = ('data', 'codepage')
    def __init__(self, data, codepage):
        self.data = data
        self.codepage = codepage


def parse_tree(exe):
    """Parse .rsrc into a generic RDir/RData tree."""
    pe = PE(exe)
    sec = pe.section('.rsrc')
    rva, raw = sec['vaddr'], sec['raw']

    def rstr(rel):
        o = raw + rel
        ln = struct.unpack_from('<H', exe, o)[0]
        return exe[o + 2:o + 2 + ln * 2].decode('utf-16-le')

    def rdir(rel):
        base = raw + rel
        nnamed, nid = struct.unpack_from('<HH', exe, base + 12)
        node = RDir()
        eo = base + 16
        for _ in range(nnamed + nid):
            name, off = struct.unpack_from('<II', exe, eo)
            eo += 8
            key = rstr(name & 0x7fffffff) if name & 0x80000000 else name
            if off & 0x80000000:
                child = rdir(off & 0x7fffffff)
            else:
                de = raw + off
                data_rva, size, cp, _ = struct.unpack_from('<IIII', exe, de)
                doff = raw + (data_rva - rva)
                child = RData(exe[doff:doff + size], cp)
            node.entries.append((key, child))
        return node

    return rdir(0), pe, sec


def build_tree(root, section_rva):
    """Serialise an RDir tree into raw .rsrc bytes located at `section_rva`."""
    dirs, leaves = [], []

    def walk(node):
        dirs.append(node)
        for _, child in node.entries:
            if isinstance(child, RDir):
                walk(child)
        for _, child in node.entries:
            if isinstance(child, RData):
                leaves.append(child)
    walk(root)

    dir_off, cur = {}, 0
    for d in dirs:
        dir_off[id(d)] = cur
        cur += 16 + 8 * len(d.entries)

    de_off = {}
    for lf in leaves:
        de_off[id(lf)] = cur
        cur += 16

    str_off = {}
    for d in dirs:
        for key, _ in d.entries:
            if isinstance(key, str) and key not in str_off:
                str_off[key] = cur
                cur += 2 + 2 * len(key)
    cur = _align(cur, 4)

    data_off = {}
    for lf in leaves:
        cur = _align(cur, 4)
        data_off[id(lf)] = cur
        cur += len(lf.data)

    out = bytearray(cur)

    # directories
    for d in dirs:
        base = dir_off[id(d)]
        named = [e for e in d.entries if isinstance(e[0], str)]
        ids   = [e for e in d.entries if not isinstance(e[0], str)]
        struct.pack_into('<IIHHHH', out, base, 0, 0, 0, 0, len(named), len(ids))
        eo = base + 16
        for key, child in named + ids:
            name = (0x80000000 | str_off[key]) if isinstance(key, str) else key
            if isinstance(child, RDir):
                off = 0x80000000 | dir_off[id(child)]
            else:
                off = de_off[id(child)]
            struct.pack_into('<II', out, eo, name, off)
            eo += 8

    # data-entry descriptors
    for lf in leaves:
        struct.pack_into('<IIII', out, de_off[id(lf)],
                         section_rva + data_off[id(lf)], len(lf.data), lf.codepage, 0)

    # name strings
    for s, off in str_off.items():
        struct.pack_into('<H', out, off, len(s))
        out[off + 2:off + 2 + 2 * len(s)] = s.encode('utf-16-le')

    # resource data
    for lf in leaves:
        o = data_off[id(lf)]
        out[o:o + len(lf.data)] = lf.data

    return bytes(out)


def parse_menu(blob):
    """Return ordered list of {opt, id, text} for every menu item."""
    items, pos = [], [4]

    def rec():
        while pos[0] + 2 <= len(blob):
            opt = struct.unpack_from('<H', blob, pos[0])[0]
            pos[0] += 2
            popup, end = opt & MF_POPUP, opt & MF_END
            mid = None
            if not popup:
                mid = struct.unpack_from('<H', blob, pos[0])[0]
                pos[0] += 2
            start = pos[0]
            while struct.unpack_from('<H', blob, pos[0])[0] != 0:
                pos[0] += 2
            text = blob[start:pos[0]].decode('utf-16-le')
            pos[0] += 2
            items.append({'opt': opt, 'id': mid, 'text': text})
            if popup:
                rec()
            if end:
                return
    rec()
    return items


def build_menu(items, repl):
    out = bytearray(b'\x00\x00\x00\x00')
    for i, it in enumerate(items):
        out += struct.pack('<H', it['opt'])
        if it['id'] is not None:
            out += struct.pack('<H', it['id'])
        out += repl.get(i, it['text']).encode('utf-16-le') + b'\x00\x00'
    return bytes(out)


def _read_sz(blob, o):
    w = struct.unpack_from('<H', blob, o)[0]
    if w == 0x0000:
        return ('empty',), o + 2
    if w == 0xffff:
        return ('ord', struct.unpack_from('<H', blob, o + 2)[0]), o + 4
    start = o
    while struct.unpack_from('<H', blob, o)[0] != 0:
        o += 2
    return ('str', blob[start:o].decode('utf-16-le')), o + 2


def _emit_sz(v):
    if v[0] == 'empty':
        return struct.pack('<H', 0)
    if v[0] == 'ord':
        return struct.pack('<HH', 0xffff, v[1])
    return v[1].encode('utf-16-le') + b'\x00\x00'


def parse_dialog(blob):
    """Return a structured DLGTEMPLATE.  Raises ValueError on the EX format."""
    if struct.unpack_from('<H', blob, 0)[0] == 1 and struct.unpack_from('<H', blob, 2)[0] == 0xffff:
        raise ValueError('DLGTEMPLATEEX not supported')
    style, ex, cdit = struct.unpack_from('<IIH', blob, 0)
    rect = struct.unpack_from('<hhhh', blob, 10)
    o = 18
    menu, o = _read_sz(blob, o)
    cls, o = _read_sz(blob, o)
    title, o = _read_sz(blob, o)
    font = None
    if style & DS_SETFONT:
        ps = struct.unpack_from('<H', blob, o)[0]
        o += 2
        face, o = _read_sz(blob, o)
        font = (ps, face)
    controls = []
    for _ in range(cdit):
        o = _align(o, 4)
        cstyle, cex = struct.unpack_from('<II', blob, o)
        crect = struct.unpack_from('<hhhh', blob, o + 8)
        cid = struct.unpack_from('<H', blob, o + 16)[0]
        o += 18
        cc, o = _read_sz(blob, o)
        ct, o = _read_sz(blob, o)
        extra = struct.unpack_from('<H', blob, o)[0]
        o += 2
        eb = blob[o:o + extra]
        o += extra
        controls.append({'style': cstyle, 'ex': cex, 'rect': crect, 'id': cid,
                         'class': cc, 'title': ct, 'extra': eb})
    return {'style': style, 'ex': ex, 'cdit': cdit, 'rect': rect, 'menu': menu,
            'class': cls, 'title': title, 'font': font, 'controls': controls}


def build_dialog(d, repl):
    n = [0]
    def slot(v):
        if v[0] == 'str':
            i = n[0]
            n[0] += 1
            return ('str', repl.get(i, v[1]))
        return v
    out = bytearray()
    out += struct.pack('<IIHhhhh', d['style'], d['ex'], d['cdit'], *d['rect'])
    out += _emit_sz(d['menu'])
    out += _emit_sz(d['class'])
    out += _emit_sz(slot(d['title']))
    if d['font'] is not None:
        out += struct.pack('<H', d['font'][0])
        out += _emit_sz(d['font'][1])
    for c in d['controls']:
        while len(out) & 3:
            out += b'\x00'
        out += struct.pack('<IIhhhhH', c['style'], c['ex'], *c['rect'], c['id'])
        out += _emit_sz(c['class'])
        out += _emit_sz(slot(c['title']))
        out += struct.pack('<H', len(c['extra'])) + c['extra']
    return bytes(out)


def _menu_strings(blob):
    """Yield (idx, text) for non-empty menu items."""
    for i, it in enumerate(parse_menu(blob)):
        if it['text']:
            yield i, it['text']


def _dialog_strings(blob):
    """Yield (idx, text) for the caption and every control caption."""
    d = parse_dialog(blob)
    idx = 0
    for v in [d['title']] + [c['title'] for c in d['controls']]:
        if v[0] == 'str':
            if v[1]:
                yield idx, v[1]
            idx += 1


def _strings_of(type_name, blob):
    if type_name == 'MENU':
        return _menu_strings(blob)
    if type_name == 'DIALOG':
        return _dialog_strings(blob)
    return iter(())


def _iter_resources(root):
    """Yield (type_name, res_name, lang, RData) for translatable resources."""
    for type_key, type_dir in root.entries:
        type_name = RT_NAMES.get(type_key) if isinstance(type_key, int) else type_key
        if type_name not in TRANSLATABLE or not isinstance(type_dir, RDir):
            continue
        for res_key, res_dir in type_dir.entries:
            if not isinstance(res_dir, RDir):
                continue
            for lang_key, leaf in res_dir.entries:
                if isinstance(leaf, RData):
                    yield type_name, res_key, lang_key, leaf


def extract(exe_bytes):
    """Return ordered resource records [{res, lang, idx, jp, tr=None}, ...]."""
    root, _, _ = parse_tree(exe_bytes)
    recs = []
    for type_name, res_name, lang, leaf in _iter_resources(root):
        res = '%s/%s' % (type_name, res_name)
        try:
            strings = list(_strings_of(type_name, leaf.data))
        except (ValueError, struct.error):
            continue
        for idx, jp in strings:
            recs.append({'res': res, 'lang': lang, 'idx': idx, 'jp': jp, 'tr': None})
    return recs


def apply(exe, recs):
    """
    Rebuild `exe` (bytearray) with the resource records' translations applied.
    Returns (new_bytearray, n_patched, warnings).  Only resources that have at
    least one non-empty `tr` are rebuilt; everything else is copied verbatim.
    """
    wanted = {}
    for r in recs:
        tr = r.get('tr')
        if tr:
            wanted.setdefault((r['res'], r['lang']), {})[r['idx']] = tr
    if not wanted:
        return exe, 0, []

    root, pe, sec = parse_tree(bytes(exe))
    npatched, warnings = 0, []

    for type_name, res_name, lang, leaf in _iter_resources(root):
        repl = wanted.get(('%s/%s' % (type_name, res_name), lang))
        if not repl:
            continue
        try:
            if type_name == 'MENU':
                items = parse_menu(leaf.data)
                rebuilt = build_menu(items, repl)
            else:
                d = parse_dialog(leaf.data)
                rebuilt = build_dialog(d, repl)
        except (ValueError, struct.error) as exc:
            warnings.append(('%s/%s' % (type_name, res_name), str(exc), ''))
            continue
        leaf.data = rebuilt
        npatched += len(repl)

    new_rsrc = build_tree(root, sec['vaddr'])
    new_exe = _splice_rsrc(exe, pe, sec, new_rsrc)
    return new_exe, npatched, warnings


def _splice_rsrc(exe, pe, sec, new_rsrc):
    """Replace the (final) .rsrc section and fix the affected PE header fields."""
    out = bytearray(exe[:sec['raw']])
    out += new_rsrc
    raw_size = _align(len(new_rsrc), pe.file_align)
    out += b'\x00' * (raw_size - len(new_rsrc))

    vsize = len(new_rsrc)
    struct.pack_into('<I', out, sec['hdr'] + 8, vsize)        # VirtualSize
    struct.pack_into('<I', out, sec['hdr'] + 16, raw_size)    # SizeOfRawData
    size_of_image = sec['vaddr'] + _align(vsize, pe.sect_align)
    struct.pack_into('<I', out, pe.opt + 0x38, size_of_image)  # SizeOfImage
    struct.pack_into('<II', out, pe.opt + 0x60 + 2 * 8,        # DataDir[RESOURCE]
                     sec['vaddr'], vsize)
    return out
