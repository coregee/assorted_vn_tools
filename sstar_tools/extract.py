import sys
from libraries import workspace

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '-h' in sys.argv or '--help' in sys.argv:
        print(__doc__)
    else:
        game_dir, opts = workspace.parse_args(
            sys.argv[1:],
            bool_flags=('--force', '--scripts', '--image', '--voice', '--sound', '--music'),
            aliases={'-s': '--scripts', '-i': '--image', '-f': '--force'})
        workspace.do_extract(game_dir,
                             sel=workspace.select_content(opts),
                             force=opts['--force'])
