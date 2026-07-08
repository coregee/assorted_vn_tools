"""
Extract 悦楽の胤 scenario scripts (and optionally assets) into editable form.

  python extract.py                 extract scripts -> script\\ (+ _names.json)
  python extract.py -s              scripts only (same as the default)
  python extract.py --image         decode images -> image\\ (PNG, via cyberworks)
  python extract.py --audio         decrypt Tink audio (voice/SE/BGM) -> audio\\ (.ogg)
  python extract.py -f              re-export, overwriting existing translations in script\\
  python extract.py <game-folder>   run against a game folder elsewhere (or use -p PATH; default: this folder)

Combine -s -i -a to widen the scope. Media archives are large (~1.7 GB total), so they
are opt-in. Run repack.py when done editing.
"""
import sys
from libraries import workspace

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__)
    else:
        root, opts = workspace.parse_args(
            sys.argv[1:],
            bool_flags=("--scripts", "--image", "--audio", "--force"),
            aliases={"-s": "--scripts", "-i": "--image", "-a": "--audio", "-f": "--force"})
        workspace.do_extract(root, sel=workspace.select_content(opts), force=opts["--force"])
