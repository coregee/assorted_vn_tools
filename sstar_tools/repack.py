import sys
from libraries import workspace, scenetext, linespace

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '-h' in sys.argv or '--help' in sys.argv:
        print(__doc__)
        sys.exit(0)
    game_dir, opts = workspace.parse_args(
        sys.argv[1:],
        value_flags=('--cols',),
        optint_flags=('--vspace',),
        bool_flags=('--scripts', '--image', '--voice', '--sound', '--music',
                    '--exe-only', '--show', '--restore-exe', '--compress'),
        aliases={'-s': '--scripts', '-i': '--image', '-v': '--vspace', '-c': '--cols'})
    pv = opts.get('--vspace')
    pitch = None if pv is False else (linespace.DEFAULT_PITCH if pv is True else int(pv))
    cols = int(opts['--cols']) if opts.get('--cols') else scenetext.LINE_COLS

    if opts['--show']:
        workspace.do_linespace_show(game_dir)
    elif opts['--restore-exe']:
        workspace.do_linespace_restore(game_dir)
    elif opts['--exe-only']:
        if pitch is None:
            sys.exit('!! --exe-only needs -v/--vspace N')
        workspace.do_linespace(game_dir, pitch)
    else:
        workspace.do_repack(game_dir, sel=workspace.select_content(opts),
                            cols=cols, pitch=pitch, compress=opts['--compress'])
