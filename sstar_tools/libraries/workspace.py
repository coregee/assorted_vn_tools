"""
Orchestrates file management within the relative game folder.
    script/                 editable JSON texts
    image/ voice/           editable media archives
    sound/ music/           .bmp, .ogg
    *.orig                  backup copies of unmodified files
    libraries/.working/     disposable scratchpad directory
"""
import glob
import json
import os
import shutil
import sys
from . import rk1, scenetext, media, sysstrings, linespace, rsrc

SCRIPT_NAME = 'script.dat'
EXE_NAME     = 'Game.exe'
SCRIPT_DIR   = 'script'
WORKING_DIR  = os.path.join('libraries', '.working')
SCENES_SUB   = os.path.join(WORKING_DIR, 'scenes')
PATCHED_SUB  = os.path.join(WORKING_DIR, 'patched')
ORIG_SUFFIX  = '.orig'

MEDIA = {
    'image': ('image', '*.cdt'),   # 800x600 / 400x600 BMPs
    'voice': ('voice', '*.vdt'),   # Ogg voice
    'sound': ('sound', '*.pdt'),   # Ogg sound effects
    'music': ('music', '*.ovd'),   # Ogg BGM
}
CONTENT = ('scripts', 'system') + tuple(MEDIA)

PKG_DIR  = os.path.dirname(os.path.abspath(__file__))   # .../game/libraries
TOOL_DIR = os.path.dirname(PKG_DIR)                      # .../game

def resolve_dir(game_dir):
    return os.path.abspath(game_dir) if game_dir else TOOL_DIR

def _p(game_dir, *parts):
    return os.path.join(game_dir, *parts)

def _hide(path):
    """Hide dir in Windows."""
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)
        except Exception:
            pass

def ensure_working(game_dir):
    """Creates and returns path for hidden working dir."""
    work = _p(game_dir, WORKING_DIR)
    os.makedirs(work, exist_ok=True)
    _hide(work)
    return work

def parse_args(argv, value_flags=(), bool_flags=(), optint_flags=(), aliases=None):
    aliases = aliases or {}
    game_dir = None
    opts = {f: False for f in tuple(bool_flags) + tuple(optint_flags)}
    i = 0
    while i < len(argv):
        raw = argv[i]
        a = aliases.get(raw, raw)
        if a in ('-p', '--path'):
            if i + 1 >= len(argv):
                sys.exit('!! %s needs a value' % raw)
            game_dir = argv[i + 1]
            i += 2
        elif a in bool_flags:
            opts[a] = True
            i += 1
        elif a in optint_flags:
            if i + 1 < len(argv) and argv[i+1].lstrip('+-').isdigit():
                opts[a] = argv[i+1]
                i += 2
            else:
                opts[a] = True
                i += 1
        elif a in value_flags:
            if i + 1 >= len(argv):
                sys.exit('!! %s needs a value' % raw)
            opts[a] = argv[i + 1]
            i += 2
        elif a.startswith('-'):
            sys.exit('!! unknown option %s' % raw)
        elif game_dir is None:
            game_dir = a
            i += 1
        else:
            sys.exit('!! unexpected extra argument %r' % a)
    return game_dir, opts

def select_content(opts):
    sel = {t for t in CONTENT if opts.get('--' + t)}
    return sel or set(CONTENT)

def backup_once(path, game_dir):
    """Backup original file if .orig not already present."""
    bak = path + ORIG_SUFFIX
    if not os.path.exists(bak):
        shutil.copy(path, bak)
        print('  captured pristine backup -> %s' % os.path.relpath(bak, game_dir))
    return bak

def ensure_workspace(game_dir, archive_name=SCRIPT_NAME):
    archive = _p(game_dir, archive_name)
    if not os.path.exists(archive):
        sys.exit('!! no %s in %s -- is this the game folder? (pass the game path as an arg)'
                 % (archive_name, game_dir))
    pristine = backup_once(archive, game_dir)
    exe = _p(game_dir, EXE_NAME)
    if os.path.exists(exe):
        backup_once(exe, game_dir)
    ensure_working(game_dir)
    scenes = _p(game_dir, SCENES_SUB)
    if not glob.glob(os.path.join(scenes, '*.BIN')):
        n = rk1.extract(pristine, scenes)
        print('  extracted %d entries -> %s' % (len(n), os.path.relpath(scenes, game_dir)))
    return pristine

def do_extract(game_dir, sel=None, archive_name=SCRIPT_NAME, force=False):
    game_dir = resolve_dir(game_dir)
    sel = sel or set(CONTENT)
    ensure_workspace(game_dir, archive_name)

    if 'scripts' in sel:
        print('[scripts] scene text -> %s%s' % (SCRIPT_DIR, '  (--force)' if force else ''))
        scenetext.extract(_p(game_dir, SCENES_SUB), _p(game_dir, SCRIPT_DIR), force=force)

    if 'system' in sel:
        exe_orig = _p(game_dir, EXE_NAME) + ORIG_SUFFIX
        if os.path.exists(exe_orig):
            out = _p(game_dir, SCRIPT_DIR, sysstrings.SYSTEM_FILE)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            n = sysstrings.extract(exe_orig, out, force=force)
            print('[system]  %d Game.exe UI strings -> %s/%s'
                  % (n, SCRIPT_DIR, sysstrings.SYSTEM_FILE))
        else:
            print('[system]  no %s -- skipped' % EXE_NAME)

    for key, (subdir, pattern) in MEDIA.items():
        if key not in sel:
            continue
        packs = media.find_packs(game_dir, pattern)
        if not packs:
            print('[%-6s] no RK1 %s packs found' % (key, pattern))
            continue
        print('[%-6s] %d pack(s); extracting -> %s/ (this can take a long time)'
              % (key, len(packs), subdir))
        for pk in packs:
            orig = backup_once(pk, game_dir)
            dest = media._pack_dir(_p(game_dir, subdir), pk)
            w, s = media.extract_pack(orig, dest, force=force)
            print('  %-14s %4d written, %4d kept (already present)'
                  % (os.path.basename(pk), w, s))

    print('\nedit %s\\ (text) and the media dirs, then run repack.py' % SCRIPT_DIR)

def _build_exe(game_dir, sel, pitch):
    """Rebuild Game.exe from Game.exe.orig with specified system TL and text pitch."""
    exe = _p(game_dir, EXE_NAME)
    orig = exe + ORIG_SUFFIX
    if not os.path.exists(orig):
        if not os.path.exists(exe):
            print('[exe]     no %s -- skipped' % EXE_NAME)
            return
        orig = backup_once(exe, game_dir)
    orig_bytes = open(orig, 'rb').read()
    data = bytearray(orig_bytes)
    loc = linespace.locate_typewriter(orig_bytes)

    nstr = nres = 0
    warns = []
    res_recs = []
    sysjson = _p(game_dir, SCRIPT_DIR, sysstrings.SYSTEM_FILE)
    if 'system' in sel and os.path.exists(sysjson):
        recs = json.load(open(sysjson, encoding='utf-8'))
        nstr, warns = sysstrings.apply(data, recs)
        res_recs = [r for r in recs if 'res' in r]

    target = pitch
    if target is None and loc is not None and os.path.exists(exe):
        target = linespace.read_pitch(open(exe, 'rb').read(), loc[0])
    notes = []
    if target is not None and target != linespace.ORIG_PITCH:
        notes = linespace.apply_pitch(data, target)

    data, nres, rwarns = rsrc.apply(data, res_recs)
    warns += rwarns

    cur = open(exe, 'rb').read() if os.path.exists(exe) else orig_bytes
    if bytes(data) == cur:
        print('[exe]     %s unchanged' % EXE_NAME)
        return
    open(exe, 'wb').write(data)
    counts = '%d system string(s)' % nstr
    if nres:
        counts += ', %d resource string(s)' % nres
    print('[exe]     patched %s: %s%s'
          % (EXE_NAME, counts, (', line pitch %d px' % target) if notes else ''))
    for nt in notes:
        print('            - ' + nt)
    if warns:
        print('          !! %d system string(s) skipped:' % len(warns))
        for off, why, s in warns[:25]:
            loc_s = ('0x%06x' % off) if isinstance(off, int) else str(off)
            print('             %-14s %-12s %.40s' % (loc_s, why, s))

def do_repack(game_dir, sel=None, archive_name=SCRIPT_NAME, cols=scenetext.LINE_COLS,
              pitch=None, compress=False):
    game_dir = resolve_dir(game_dir)
    sel = sel or set(CONTENT)
    pristine = ensure_workspace(game_dir, archive_name)

    if 'scripts' in sel:
        patched = _p(game_dir, PATCHED_SUB)
        os.makedirs(patched, exist_ok=True)
        for f in os.listdir(patched):
            os.remove(os.path.join(patched, f))
        print('[scripts] building from %s\\ ...%s'
              % (SCRIPT_DIR, '' if compress else '  (storing uncompressed -- fast)'))
        scenetext.build(_p(game_dir, SCRIPT_DIR), _p(game_dir, SCENES_SUB), patched, cols=cols)
        nrepl, total = rk1.rebuild(pristine, patched, _p(game_dir, archive_name), compress=compress)
        print('  rebuilt %s: %d/%d entries replaced' % (archive_name, nrepl, total))

    for key, (subdir, pattern) in MEDIA.items():
        if key not in sel:
            continue
        for pk in media.find_packs(game_dir, pattern):
            stem = media._pack_dir(_p(game_dir, subdir), pk)
            if not os.path.isdir(stem):
                continue
            orig = backup_once(pk, game_dir)
            nchg, total = media.rebuild_pack(orig, stem, pk)
            print('[%-6s] %-14s %d/%d entries changed'
                  % (key, os.path.basename(pk), nchg, total))

    if 'system' in sel or pitch is not None:
        _build_exe(game_dir, sel, pitch)

    print('\ndone -- launch %s to test.' % EXE_NAME)

def do_linespace(game_dir, pitch):
    game_dir = resolve_dir(game_dir)
    _build_exe(game_dir, sel=set(), pitch=pitch)

def do_linespace_show(game_dir):
    game_dir = resolve_dir(game_dir)
    exe = _p(game_dir, EXE_NAME)
    bak = exe + ORIG_SUFFIX
    src = exe if os.path.exists(exe) else bak
    if not os.path.exists(src):
        print('no %s found' % EXE_NAME)
        return
    ref = open(bak, 'rb').read() if os.path.exists(bak) else open(src, 'rb').read()
    loc = linespace.locate_typewriter(ref)
    if loc is None:
        print('%s: live-typewriter site not recognised (is this the right game?)' % EXE_NAME)
        return
    p = linespace.read_pitch(open(src, 'rb').read(), loc[0])
    if p is None:
        print('%s: live pitch site altered in an unrecognised way' % EXE_NAME)
    else:
        print('%s live dialogue line pitch: %d px  (gap %d px, font %d px)'
              % (EXE_NAME, p, p - linespace.FONT_PX, linespace.FONT_PX))

def do_linespace_restore(game_dir):
    game_dir = resolve_dir(game_dir)
    exe = _p(game_dir, EXE_NAME)
    bak = exe + ORIG_SUFFIX
    if not os.path.exists(bak):
        sys.exit('no unmodified %s backup to restore from' % EXE_NAME)
    shutil.copy(bak, exe)
    print('restored %s from unmodified backup (line pitch restored to %d px)'
          % (EXE_NAME, linespace.ORIG_PITCH))
