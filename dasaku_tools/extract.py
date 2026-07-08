"""Extract game assets. Default scope is -s (scripts).

Usage:
  python extract.py                         Extract the text/script content
  python extract.py -p "path/to/game"       Specify the game directory
  python extract.py -s [text|ui|names ...]  Scripts only (with optional subsets)
  python extract.py -i [PACK ...]           Images only (with optional target files)
  python extract.py -a [GROUP ...]          Audio only (with optional target group)
  python extract.py -f                      Force-overwrite existing extracted files
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'libraries'))
import pipeline  # noqa: E402

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')

    if '-h' in sys.argv or '--help' in sys.argv:
        print(__doc__)
        sys.exit(0)

    game_dir, opts = pipeline.parse_args(
        sys.argv[1:],
        scope_flags=('--script', '--image', '--audio'),
        bool_flags=('--force',),
        aliases={
            '-s': '--script',
            '-i': '--image',
            '-a': '--audio',
            '-f': '--force',
        })

    pipeline.do_extract(game_dir,
                        sel=pipeline.select_content(opts, pipeline.EXTRACT_STEPS),
                        force=opts['--force'])
