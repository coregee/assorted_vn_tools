"""
Repack edited scripts (and optionally assets) back into the game, and manage .exe patches.
  python repack.py                           build + deploy scripts
  python repack.py --image                   repack edited images
  python repack.py --audio                   repack replaced audio (voice/SE/BGM)
  python repack.py -c ##                     override the wrap width (default 63 half-width cells)
  python repack.py --patch-exe               apply the exe patches (registry gate, locale, half-width)
  python repack.py --restore-exe             revert the exe patches from the .orig backup
  python repack.py --show                    print the current exe patch state and exit
  python repack.py <game-folder>             run against a game folder elsewhere (or use -p PATH; default: this folder)

Combine -s -i -a to widen the scope. The --patch-exe/--restore-exe/--show flags act on the
.exe alone and ignore the content scope.
"""
import sys
from libraries import workspace, scenetext

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__); sys.exit(0)
    root, opts = workspace.parse_args(
        sys.argv[1:],
        value_flags=("--cols",),
        bool_flags=("--scripts", "--image", "--audio",
                    "--patch-exe", "--restore-exe", "--show"),
        aliases={"-s": "--scripts", "-i": "--image", "-a": "--audio", "-c": "--cols"})

    if opts["--show"]:
        workspace.do_show_exe(root)
    elif opts["--restore-exe"]:
        workspace.do_restore_exe(root)
    elif opts["--patch-exe"]:
        workspace.do_patch_exe(root)
    else:
        cols = int(opts["--cols"]) if opts.get("--cols") else scenetext.LINE_COLS
        workspace.do_repack(root, sel=workspace.select_content(opts), cols=cols)
