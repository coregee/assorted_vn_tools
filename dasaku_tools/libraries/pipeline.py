"""Orchestrate extract/repack over the game folder and the editable corpus.

  script/                 editable JSON text (scenario routes + UI config/charaname/namecol/names)
  images/<pack>/          editable graphics-pack blobs + manifest.json
  sound/<group>/          editable .ogg voices/movies + manifest.json
  <asset>.orig            pristine backup, snapshotted on first touch, used as source thereafter
  libraries/.working/     disposable scratch space
"""
import filecmp
import glob
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fontcfg  # noqa: E402
import gpk      # noqa: E402
import sound    # noqa: E402

ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB    = os.path.join(ROOT, 'libraries')
WORK   = os.path.join(LIB, '.working')
VNTP   = os.path.join(LIB, 'VNTextPatch', 'VNTextPatch.exe')
SCRIPT = os.path.join(ROOT, 'script')
IMAGE  = os.path.join(ROOT, 'images')
SOUND  = os.path.join(ROOT, 'sound')

EXE_NAME = 'dasaku_HD.exe'

# <game>/dwq packs: sys0=UI text, sc0/sm0=wallpapers/thumbs, ta0=sprites, bg0/ev0=bgs/CGs
ALL_PACKS = ('sys0', 'sc0', 'sm0', 'ta0', 'bg0', 'ev0')

# cdvaw = .vpk/.vtb voice packs, wgq = .wgq movies/audio.
ALL_SOUND = ('cdvaw', 'wgq')

# -s/--script sub-steps. extract has a names pass; repack folds names into text + ui.
EXTRACT_STEPS = ('text', 'ui', 'names')
REPACK_STEPS  = ('text', 'ui')

# Non-route files in script/; every other script/*.json is a scenario route.
UI_FILES  = ('config.json', 'charaname.json', 'namecol.json')
NAMES     = os.path.join(SCRIPT, 'names.json')
NON_ROUTE = UI_FILES + ('names.json',)


# --------------------------------------------------------------------------- helpers

def resolve_game(game_dir):
    """--path, else the repo root if the game is unpacked there, else <root>/game."""
    if game_dir:
        return os.path.abspath(game_dir)
    if any(os.path.exists(os.path.join(ROOT, m)) for m in (EXE_NAME, 'spt', 'dwq')):
        return ROOT
    return os.path.join(ROOT, 'game')


def run(*cmd):
    subprocess.run(cmd, check=True, cwd=ROOT)


def lib(script, *args):
    run(sys.executable, os.path.join(LIB, script), *args)


def fresh(path):
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def ensure_orig(path):
    """Snapshot <path> to <path>.orig on first touch (never overwritten); return the .orig."""
    orig = path + '.orig'
    if not os.path.exists(orig) and os.path.exists(path):
        shutil.copy2(path, orig)
    return orig


def warn_if_no_clean_baseline(game):
    """Warn if deploy artifacts (ddraw.dll / sjis_ext.bin) exist but no .orig does: an
    already-patched install where the first .orig taken would snapshot a PATCHED file."""
    artifacts = [f for f in ('ddraw.dll', 'sjis_ext.bin')
                 if os.path.exists(os.path.join(game, f))]
    if not artifacts:
        return
    for _, _, files in os.walk(game):
        if any(f.endswith('.orig') for f in files):
            return
    print('  !! %s present but NO .orig snapshots found -- this install looks already'
          % ', '.join(artifacts))
    print('     patched. Capturing .orig now would snapshot PATCHED files as the')
    print('     "originals". Restore a clean install before extracting/deploying.')


def orig_spt_folder(game):
    """Stage pristine .orig copies of <game>/spt/*.spt into .working/orig_spt/ for
    VNTextPatch. Returns that folder, or None if there are no scripts."""
    src = os.path.join(game, 'spt')
    if not os.path.isdir(src):
        return None
    staged = fresh(os.path.join(WORK, 'orig_spt'))
    n = 0
    for fn in sorted(os.listdir(src)):
        if fn.endswith('.spt'):
            shutil.copy2(ensure_orig(os.path.join(src, fn)), os.path.join(staged, fn))
            n += 1
    return staged if n else None


def _restore_original(folder, stem, exts=('.gpk', '.gtb')):
    """Copy each pristine .orig back over a deployed asset that has no edits now.
    Returns True if anything was restored."""
    restored = False
    for ext in exts:
        orig = os.path.join(folder, stem + ext + '.orig')
        dst = os.path.join(folder, stem + ext)
        if os.path.exists(orig) and os.path.exists(dst) and \
                not filecmp.cmp(orig, dst, shallow=False):
            shutil.copy2(orig, dst)
            restored = True
    return restored


# ------------------------------------------------------------------------ arg parsing

def parse_args(argv, scope_flags=(), bool_flags=(), value_flags=(), aliases=None):
    """Hand-rolled arg parser. -p/--path takes the next token; each scope flag (-s/-i/-a)
    collects following non-option tokens as a sub-list ([] = all of that scope), absent =
    False; bool flags are on/off and value flags consume one argument. Returns
    (game_dir, opts)."""
    aliases = aliases or {}
    game_dir = None
    opts = {f: False for f in tuple(scope_flags) + tuple(bool_flags) + tuple(value_flags)}
    i = 0
    while i < len(argv):
        raw = argv[i]
        a = aliases.get(raw, raw)
        if a in ('-p', '--path'):
            if i + 1 >= len(argv):
                sys.exit('!! %s needs a value' % raw)
            game_dir = argv[i + 1]
            i += 2
        elif a in scope_flags:
            vals = []
            i += 1
            while i < len(argv) and not argv[i].startswith('-'):
                vals.append(argv[i])
                i += 1
            opts[a] = vals
        elif a in bool_flags:
            opts[a] = True
            i += 1
        elif a in value_flags:
            if i + 1 >= len(argv):
                sys.exit('!! %s needs a value' % raw)
            opts[a] = argv[i + 1]
            i += 2
        else:
            sys.exit('!! unknown option %s' % raw)
    return game_dir, opts


def _resolve(val, allowed, label):
    """Expand a scope sub-list to concrete items (False/[] => all) and validate it."""
    items = list(allowed) if not val else val
    bad = [x for x in items if x not in allowed]
    if bad:
        sys.exit('!! unknown %s: %s (choose from %s)'
                 % (label, ', '.join(bad), ', '.join(allowed)))
    return items


def select_content(opts, steps_all, with_exe=False):
    """Resolve parsed opts into a scope plan. No scope flag defaults to scripts only;
    any explicit scope flag (including -e) selects exactly what was named."""
    explicit = any(opts[k] is not False for k in ('--script', '--image', '--audio'))
    if with_exe:
        explicit = explicit or opts['--exe']
    sel = {}
    if opts['--script'] is not False or not explicit:
        sel['script'] = _resolve(opts['--script'], steps_all, 'script step')
    if opts['--image'] is not False:
        sel['image'] = _resolve(opts['--image'], ALL_PACKS, 'pack')
    if opts['--audio'] is not False:
        sel['audio'] = _resolve(opts['--audio'], ALL_SOUND, 'audio group')
    if with_exe and opts['--exe']:
        sel['exe'] = True
    return sel


# ------------------------------------------------------------------------------ extract

def extract_text(game, force):
    """<game>/spt/*.spt (via .orig) -> script/<route>.json, preserving translations."""
    src = orig_spt_folder(game)
    if not src:
        print('[text]    no .spt under %s -- nothing to extract' % os.path.join(game, 'spt'))
        return
    raw = fresh(os.path.join(WORK, 'extract_raw'))
    os.makedirs(SCRIPT, exist_ok=True)
    print('[text]    scene text -> script/%s' % ('  (--force)' if force else ''))
    run(VNTP, 'extractlocal', src, raw)

    for fn in sorted(os.listdir(raw)):
        if not fn.endswith('.json'):
            continue
        with open(os.path.join(raw, fn), encoding='utf-8') as fh:
            new = json.load(fh)
        target = os.path.join(SCRIPT, fn)

        if not os.path.exists(target):
            _write(target, [_skeleton(e) for e in new])
            print('  %-14s created %d entries' % (fn, len(new)))
            continue

        with open(target, encoding='utf-8') as fh:
            existing = json.load(fh)
        if len(existing) != len(new):
            print('  !! %-14s SKIPPED -- %d corpus entries vs %d in source (out of sync)'
                  % (fn, len(existing), len(new)))
            continue
        mismatch = sum(1 for a, b in zip(existing, new) if a['message'] != b['message'])
        if mismatch:
            print('  !! %-14s SKIPPED -- %d message(s) differ from source (out of sync)'
                  % (fn, mismatch))
            continue
        if force:
            _write(target, [_merge(e, r) for e, r in zip(existing, new)])
            print('  %-14s re-synced %d entries (translations kept)' % (fn, len(existing)))
        else:
            done = sum(1 for e in existing if e.get('translated') is not None)
            print('  %-14s in sync, %d/%d translated' % (fn, done, len(existing)))


def _skeleton(raw):
    out = {}
    if raw.get('name'):
        out['name'] = raw['name']
    out['message'] = raw['message']
    out['translated'] = None
    return out


def _merge(existing, raw):
    out = {}
    if raw.get('name'):
        out['name'] = raw['name']
    if existing.get('name_translated') is not None:
        out['name_translated'] = existing['name_translated']
    out['message'] = raw['message']
    out['translated'] = existing.get('translated')
    return out


def _write(path, data):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def extract_ui(game, force):
    print('[ui]      Game UI text -> script/{config,charaname,namecol}.json')
    lib('uitext.py', 'extract', '--game', game)


def extract_names(game, force):
    print('[names]   speaker glossary -> script/names.json')
    lib('names.py')


def extract_images(game, force, packs):
    """<game>/dwq/<pack>.gpk (via .orig) -> images/<pack>/ (full set + manifest.json).
    Redraw in place; repack only replaces files whose hash differs. images/ is gitignored."""
    dwq = os.path.join(game, 'dwq')
    if not os.path.isdir(dwq):
        print('[image]   %s not present -- nothing to extract' % dwq)
        return
    print('[image]   %d pack(s) -> images/ (this can take a while)' % len(packs))
    for pack in packs:
        gpk_path = os.path.join(dwq, pack + '.gpk')
        if not os.path.isfile(gpk_path):
            print('  !! %-6s %s not found -- skipped' % (pack, gpk_path))
            continue
        base = ensure_orig(gpk_path)
        ensure_orig(os.path.join(dwq, pack + '.gtb'))
        dest = os.path.join(IMAGE, pack)
        if force:
            fresh(dest)  # restore pristine originals (discards any edits in this pack)
        os.makedirs(dest, exist_ok=True)
        gpk.extract(base, dest, skip_existing=not force)
        print('  %-6s extracted' % pack)
    print('  redraw in place; only files you change get repacked (manifest sha256)')


def extract_sound(game, force, groups):
    """<game>/cdvaw/*.vpk + <game>/wgq/*.wgq (via .orig) -> sound/<group>/ as .ogg +
    manifest.json. Re-extract keeps edits unless -f/--force restores pristine."""
    for group in groups:
        if group == 'cdvaw':
            srcfolder = os.path.join(game, 'cdvaw')
            vpks = sorted(glob.glob(os.path.join(srcfolder, '*.vpk')))
            if not vpks:
                print('[cdvaw]   no .vpk under %s -- skipped' % srcfolder)
                continue
            print('[cdvaw]   %d voice pack(s) -> sound/cdvaw/' % len(vpks))
            for vpk in vpks:
                pack = os.path.splitext(os.path.basename(vpk))[0]
                base = ensure_orig(vpk)
                ensure_orig(os.path.join(srcfolder, pack + '.vtb'))
                dest = os.path.join(SOUND, 'cdvaw', pack)
                if force:
                    fresh(dest)
                os.makedirs(dest, exist_ok=True)
                sound.extract_vpk(base, dest, skip_existing=not force)
        elif group == 'wgq':
            srcfolder = os.path.join(game, 'wgq')
            wgqs = sorted(glob.glob(os.path.join(srcfolder, '*.wgq')))
            if not wgqs:
                print('[wgq]     no .wgq under %s -- skipped' % srcfolder)
                continue
            print('[wgq]     %d movie/audio file(s) -> sound/wgq/' % len(wgqs))
            origs = [ensure_orig(w) for w in wgqs]
            dest = os.path.join(SOUND, 'wgq')
            if force:
                fresh(dest)
            os.makedirs(dest, exist_ok=True)
            sound.extract_wgq(origs, dest, skip_existing=not force)
    print('  playable .ogg under sound/<group>/; only edits get repacked (manifest sha256)')


_EXTRACT = {'text': extract_text, 'ui': extract_ui, 'names': extract_names}


def do_extract(game_dir, sel, force=False):
    game = resolve_game(game_dir)
    os.makedirs(WORK, exist_ok=True)
    print('game folder: %s' % game)
    warn_if_no_clean_baseline(game)
    for name in sel.get('script', ()):
        _EXTRACT[name](game, force)
    if 'image' in sel:
        extract_images(game, force, sel['image'])
    if 'audio' in sel:
        extract_sound(game, force, sel['audio'])
    print('\nedit script\\ (+ the images/sound dirs), then run repack.py')


# ------------------------------------------------------------------------------- repack

def repack_text(game):
    """script/ -> staging -> insertlocal (vs .orig) -> verify -> deploy to <game>/spt.
    Returns the count that failed verification (and so were not deployed)."""
    game_spt = os.path.join(game, 'spt')
    src = orig_spt_folder(game)
    if not src:
        print('[text]    no .spt under %s -- cannot repack (is the game present?)' % game_spt)
        return 1
    staging = fresh(os.path.join(WORK, 'staging'))
    patched = fresh(os.path.join(WORK, 'patched'))
    lib('stage_json.py', SCRIPT, staging)
    run(VNTP, 'insertlocal', src, staging, patched)

    bad = 0
    print('[text]    verify + deploy -> %s' % os.path.relpath(game_spt, game))
    for fn in sorted(os.listdir(patched)):
        if not fn.endswith('.spt'):
            continue
        orig = os.path.join(src, fn)
        out = os.path.join(patched, fn)
        r = subprocess.run([sys.executable, os.path.join(LIB, 'verify_spt.py'), orig, out],
                           cwd=ROOT)
        if r.returncode != 0:
            print('  !! %-14s verify FAILED -- not deployed' % fn)
            bad += 1
        else:
            shutil.copy2(out, os.path.join(game_spt, fn))
            print('  %s' % fn)

    # tunnel map for non-SJIS chars, read by the VWF hook next to the exe
    ext = os.path.join(patched, 'sjis_ext.bin')
    dst = os.path.join(game, 'sjis_ext.bin')
    if os.path.exists(ext):
        shutil.copy2(ext, dst)
        print('  sjis_ext.bin (em-dash, accents, curly quotes, ...)')
    elif os.path.exists(dst):
        os.remove(dst)  # no non-SJIS chars this pass; drop the stale map
    return bad


def repack_ui(game):
    """script/{config,charaname,namecol}.json -> <game>/{init2,spt}/... (vs .orig)."""
    if not os.path.isdir(game):
        print('[ui]      %s not present -- skipped' % game)
        return 1
    print('[ui]      applying config/charaname/namecol + names')
    lib('uitext.py', 'build', '--game', game)
    return 0


def repack_exe(game, custom_font=False):
    """Build + deploy libraries/vwfhook's ddraw.dll, falling back to the prebuilt one if
    the VS build tools are absent. Returns the number of problems.

    custom_font: face/EN_HEIGHT_PCT are baked into the hook, so the Noto-default prebuilt
    fallback would mismatch a configured font (and its wrap) -- counted as a problem."""
    if not os.path.isdir(game):
        print('[exe]     %s not present -- skipped' % game)
        return 1
    build = os.path.join(LIB, 'vwfhook', 'build.ps1')
    print('[exe]     build + deploy the VWF hook -> %s' % os.path.join(game, 'ddraw.dll'))
    r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-File', build, '-Game', game], cwd=ROOT)
    if r.returncode == 0:
        return 0  # build.ps1 prints its own "Built and deployed -> ..." line
    prebuilt = os.path.join(LIB, 'vwfhook', 'ddraw.dll')
    if os.path.isfile(prebuilt):
        shutil.copy2(prebuilt, os.path.join(game, 'ddraw.dll'))
        if custom_font:
            print('  !! build FAILED and a custom font is set -- the prebuilt ddraw.dll has the')
            print('     DEFAULT font baked in, so your custom font will NOT render in-game (text')
            print('     would also be mis-wrapped). Install the VS C++ build tools and re-run.')
            return 1
        print('  !! build failed -- deployed prebuilt ddraw.dll instead')
        return 0
    print('  !! VWF hook build failed and no prebuilt ddraw.dll to fall back on')
    return 1


def repack_images(game, packs):
    """images/<pack>/ -> rebuild vs <game>/dwq/<pack>.gpk.orig -> deploy changed packs.
    Only manifest-flagged edits replace their blobs; the rest inherit from .orig; no-edit
    packs are left alone or restored from a stale patch."""
    game_dwq = os.path.join(game, 'dwq')
    if not os.path.isdir(game_dwq):
        print('[image]   %s not present -- skipped' % game_dwq)
        return
    if not os.path.isdir(IMAGE):
        print('[image]   no images/ -- run `extract.py -i` first')
        return
    print('[image]   repack %s -> %s' % (', '.join(packs), os.path.relpath(game_dwq, game)))
    built = 0
    for pack in packs:
        srcdir = os.path.join(IMAGE, pack)
        if not os.path.isfile(os.path.join(srcdir, 'manifest.json')):
            continue  # pack not extracted into images/
        gpk_path = os.path.join(game_dwq, pack + '.gpk')
        if not os.path.isfile(gpk_path):
            print('  !! %-6s SKIPPED -- no %s' % (pack, gpk_path))
            continue
        base = ensure_orig(gpk_path)
        ensure_orig(os.path.join(game_dwq, pack + '.gtb'))

        modified = gpk.compute_modified(srcdir)
        if not modified:
            if _restore_original(game_dwq, pack):
                print('  %-6s no edits -- restored pristine pack' % pack)
                built += 1
            else:
                print('  %-6s no edits -- unchanged' % pack)
            continue

        out_base = os.path.join(fresh(os.path.join(WORK, 'gpk_build')), pack)
        gpk.build(srcdir, out_base, base_pack=base, modified=modified)
        for ext in ('.gpk', '.gtb'):
            built_file = out_base + ext
            dst = os.path.join(game_dwq, pack + ext)
            if os.path.exists(dst) and filecmp.cmp(built_file, dst, shallow=False):
                print('  %s%s unchanged -- not deployed' % (pack, ext))
                continue
            shutil.copy2(built_file, dst)
            print('  %s%s (%d image(s) replaced)' % (pack, ext, len(modified)))
            built += 1
    if not built:
        print('  no changes to deploy')


def repack_sound(game, groups):
    """sound/<group>/ edited .ogg -> rebuild vs .orig -> deploy changed (mirrors
    repack_images: voices into the .vpk, movies into the .wgq)."""
    built = 0
    for group in groups:
        if group == 'cdvaw':
            built += _repack_vpks(game)
        elif group == 'wgq':
            built += _repack_wgq(game)
    if not built:
        print('  no changes to deploy')


def _repack_vpks(game):
    game_cd = os.path.join(game, 'cdvaw')
    sound_cd = os.path.join(SOUND, 'cdvaw')
    if not os.path.isdir(game_cd) or not os.path.isdir(sound_cd):
        return 0
    print('[cdvaw]   repack voice packs -> %s' % os.path.relpath(game_cd, game))
    built = 0
    for pack in sorted(os.listdir(sound_cd)):
        srcdir = os.path.join(sound_cd, pack)
        if not os.path.isfile(os.path.join(srcdir, 'manifest.json')):
            continue
        vpk = os.path.join(game_cd, pack + '.vpk')
        if not os.path.isfile(vpk):
            print('  !! %-10s SKIPPED -- no %s' % (pack, vpk))
            continue
        base = ensure_orig(vpk)
        ensure_orig(os.path.join(game_cd, pack + '.vtb'))
        modified = sound.compute_modified(srcdir)
        if not modified:
            if _restore_original(game_cd, pack, ('.vpk', '.vtb')):
                print('  %-10s no edits -- restored pristine pack' % pack)
                built += 1
            else:
                print('  %-10s no edits -- unchanged' % pack)
            continue
        out_base = os.path.join(fresh(os.path.join(WORK, 'sound_build')), pack)
        sound.build_vpk(srcdir, out_base, base_pack=base, modified=modified)
        for ext in ('.vpk', '.vtb'):
            built_file = out_base + ext
            dst = os.path.join(game_cd, pack + ext)
            if os.path.exists(dst) and filecmp.cmp(built_file, dst, shallow=False):
                print('  %s%s unchanged -- not deployed' % (pack, ext))
                continue
            shutil.copy2(built_file, dst)
            print('  %s%s (%d voice(s) replaced)' % (pack, ext, len(modified)))
            built += 1
    return built


def _repack_wgq(game):
    game_wgq = os.path.join(game, 'wgq')
    sound_wgq = os.path.join(SOUND, 'wgq')
    if not os.path.isdir(game_wgq) or \
            not os.path.isfile(os.path.join(sound_wgq, 'manifest.json')):
        return 0
    print('[wgq]     repack movie/audio -> %s' % os.path.relpath(game_wgq, game))
    manifest = sound.load_manifest(sound_wgq)
    for fname in manifest['entries']:  # snapshot pristine .wgq before any deploy
        wgq = os.path.join(game_wgq, fname[:-len('.ogg')] + '.wgq')
        if os.path.isfile(wgq):
            ensure_orig(wgq)
    modified = sound.compute_modified(sound_wgq, manifest)
    if not modified:
        restored = sum(_restore_original(game_wgq, f[:-len('.ogg')], ('.wgq',))
                       for f in manifest['entries'])
        print('  no edits -- %s'
              % ('restored %d pristine' % restored if restored else 'unchanged'))
        return 1 if restored else 0
    out_dir = fresh(os.path.join(WORK, 'sound_build_wgq'))
    sound.build_wgq(sound_wgq, out_dir, base_dir=game_wgq, modified=modified)
    built = 0
    for fname in modified:
        stem = fname[:-len('.ogg')]
        built_file = os.path.join(out_dir, stem + '.wgq')
        dst = os.path.join(game_wgq, stem + '.wgq')
        if os.path.exists(dst) and filecmp.cmp(built_file, dst, shallow=False):
            print('  %s.wgq unchanged -- not deployed' % stem)
            continue
        shutil.copy2(built_file, dst)
        print('  %s.wgq' % stem)
        built += 1
    return built


_REPACK = {'text': repack_text, 'ui': repack_ui}


def do_repack(game_dir, sel, review_report=None):
    """Run the selected repack surfaces; returns the total number of problems."""
    game = resolve_game(game_dir)
    os.makedirs(WORK, exist_ok=True)
    print('game folder: %s' % game)
    warn_if_no_clean_baseline(game)
    problems = 0

    # Resolve the optional custom font once (writes vwf_font.h + VNTextPatch wrap widths,
    # bundles the font). Register session-wide so the VNTextPatch child resolves it by name.
    font_spec = None
    if 'script' in sel or 'exe' in sel:
        font_spec = fontcfg.apply(game)
        if font_spec:
            fontcfg.register(font_spec.file_abs)
    try:
        for name in sel.get('script', ()):
            problems += _REPACK[name](game)
        if 'image' in sel:
            repack_images(game, sel['image'])
        if 'audio' in sel:
            repack_sound(game, sel['audio'])
        if 'exe' in sel:
            problems += repack_exe(game, custom_font=bool(font_spec))
    finally:
        if font_spec:
            fontcfg.unregister(font_spec.file_abs)
    if problems:
        print('\ndone with %d problem(s) -- see the !! lines above.' % problems)
    else:
        print('\ndone -- launch %s to test.' % EXE_NAME)
    if review_report:
        # Dasaku's proportional wrapper inserts as many line breaks as needed and its
        # current script repacker has no truncating/does-not-fit line diagnostic. Keep
        # the shared report contract so LLM Tools can parse every repacker uniformly.
        with open(review_report, 'w', encoding='utf-8') as fh:
            json.dump({'version': 1, 'issues': []}, fh, ensure_ascii=False, indent=1)
    return problems
