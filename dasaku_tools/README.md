# 駄作　～ヌイアワセ～ tools

Toolkit for extracting/repacking assets for the (very 18+!) visual novel **駄作 \~ヌイアワセ\~** by CYCLET.
It includes a variable-width font hook/override with `ddraw.dll`.

## Requirements

- **.NET Framework 4.8** -- for `libraries\VNTextPatch\VNTextPatch.exe`.
- **Python 3.7+** -- for `extract.py` / `repack.py` (the image tools also want `Pillow`).
- **A JP-capable font** -- **Noto Sans JP** by default

## How to use

1. Put this repo at the game root (or pass the game folder with `-p`).
2. Run `python extract.py` to pull the text into `script\*.json`.
3. Edit the assets you want:
   - scenario text in `script\<route>.json`
   - speaker names in `script\names.json`, UI strings in `script\{config,charaname,namecol}.json`,
   - images in `images\<pack>\` (after `extract.py -i`), audio in `sound\<group>\` (after `extract.py -a`).
4. Run `python repack.py` to repack the text and build the VWF hook.

## Additional Parameters

| Flag | Long form  | Applies to | Description                                                              |
| ---- | ---------- | ---------- | ------------------------------------------------------------------------ |
| `-p` | `--path`   | both       | Use a specific game folder as the working directory.                     |
| `-s` | `--script` | both       | Process scenario + UI text. Name sub-steps (`text ui [names]`) or all.   |
| `-i` | `--image`  | both       | Process image packs (`.gpk`). Name packs or default all.                 |
| `-a` | `--audio`  | both       | Process audio packs (voice `.vpk`, movie `.wgq`). Name groups or all.    |
| `-e` | `--exe`    | repack     | Build + deploy the VWF hook (`ddraw.dll`).                               |
| `-f` | `--force`  | extract    | Overwrite existing extracted files.                                      |

Scopes can be combined. The default flow is `-s -e` (scripts + VWF hook).

## Custom font

Set a custom font in the `font.json` config file (defaults to Noto Sans JP if available).

```jsonc
{ "file": "fonts/MyFont.otf",
  "weight": "normal", 
  "en_height_pct": null, 
  "face": null
}
```

## Credits

`libraries\VNTextPatch\` is vendored from **VNTranslationTools** by **arcusmaximus**
([repo](https://github.com/arcusmaximus/VNTranslationTools), v0.0.41), used under the
**MIT License** -- see [libraries/VNTextPatch/UPSTREAM.md](libraries/VNTextPatch/UPSTREAM.md).
Only its `.exe.config` (our word-wrap calibration) is modified.
