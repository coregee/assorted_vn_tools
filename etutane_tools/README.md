# 悦楽の胤 (Etsuraku no Tane) — VN Tools

Extract/repack toolkit for the (very 18+! warning!) visual novel 悦楽の胤.
May be applicable to other games developed by Cyberworks/WendyBell/etc., not tested.
I tried to get this working with the remaster too, but DRM :(.

## Requirements

Python 3.7+

## How to use

1. Run `python extract.py` within the game folder (or supply a path).
2. Edit the extracted assets:
   - dialogue/narration in `script\*.json` (fill each line's `translated` field),
   - speaker names in `script\_names.json` (map each 【name】 to its translation),
   - images in `image\*.png`, audio in `audio\*.ogg` (only if extracted; see below).
3. Run `python repack.py` to apply any translated lines to the original files.

By default, extract/repack only target the scripts. Pass `-i`/`-a` to also process
images/audio — these archives are large, so repacking them takes significantly longer.

## Flags

| Flag | Long form       | Applies to | Description                                                    |
| ---- | --------------- | ---------- | -------------------------------------------------------------- |
| `-p` | `--path`        | both       | Path to the game folder (default: the current folder).         |
| `-s` | `--scripts`     | both       | Process script/text data (the default when no flag is given).  |
| `-i` | `--image`       | both       | Process the image archive.                                     |
| `-a` | `--audio`       | both       | Process the audio archive (voice/SE/BGM).                      |
| `-f` | `--force`       | extract    | Re-export, overwriting existing files/translations.            |
| `-c` | `--cols NN`     | repack     | Set a custom line-wrap width in half-width cells (default 63). |
|      | `--patch-exe`   | repack     | Apply the .exe patches (registry gate, locale, half-width).    |
|      | `--restore-exe` | repack     | Revert the .exe patches from the `.orig` backup.               |
|      | `--show`        | repack     | Print the current .exe patch state.                            |

Pass a combination of `-s -i -a` to define a custom asset scope; e.g. `-i` is image
only, `-i -a` is image + audio, `-s -i` is scripts + image.

## Notes

Special thanks to [Garbro](https://github.com/morkt/garbro) for being a valuable source of information on how the image assets are stored.
