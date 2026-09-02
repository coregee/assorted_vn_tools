"""Repack game assets. Default scope is -s (scripts).

Usage:
  python repack.py                          Repack scripts (default)
  python repack.py -p "path/to/game"        Specify the game directory
  python repack.py -s [text|ui ...]         Scripts only (with optional subsets)
  python repack.py -e                       Build and deploy the VWF hook only
  python repack.py -i [PACK ...]            Images only (with optional target files)
  python repack.py -a [GROUP ...]           Audio only (with optional target group)
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
        bool_flags=('--exe',),
        value_flags=('--review-report',),
        aliases={
            '-s': '--script',
            '-i': '--image',
            '-a': '--audio',
            '-e': '--exe',
        })

    sel = pipeline.select_content(opts, pipeline.REPACK_STEPS, with_exe=True)
    sys.exit(1 if pipeline.do_repack(
        game_dir, sel, review_report=opts.get('--review-report')) else 0)
