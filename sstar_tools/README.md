# Shining Star: VN Extract/Repack Tools

This repo provides a basic toolkit for extracting/repacking scripts/assets from visual
novels developed by Shining Star.

## Requirements

Python 3.7+

## How to use

1. Extract the repo to the root of the target game.
2. Run `python extract.py` to extract assets.
3. Modify assets as you need to:
   - scene text in `script\*.json` (fill each page's `tr` field),
   - speaker names in `script\_names.json`, UI strings in `script\_system.json`,
   - images in `image\<pack>\*.BMP`
   - audio in `voice\`/`sound\`/`music\`.
4. Run `python repack.py` to repack assets into the game files.

## Additional Parameters

| Flag | Long form | Applies to | Description |
| ---- | ---- | ---- | ---- |
| `-p` | `--path` | both | Use a specific folder as the working directory. |
| `-s` | `--scripts` | both | Target scene `script.dat` only. |
| `-i` | `--image` | both | Target image packs (`.cdt`) only. |
| | `--voice` | both | Target voice packs (`.vdt`) only. |
| | `--sound` | both | Target sound-effect packs (`.pdt`) only. |
| | `--music` | both | Target music packs (`.ovd`) only. |
| `-v ##` | `--vspace ##` | repack | Set the vertical line-spacing to `##` pixels (no value = default 30; 39 = stock). |
| `-c NN` | `--cols NN` | repack | Set a custom characters per line wrap width (default 54). |
| `-f` | `--force` | extract | Overwrite existing extracted files. |
