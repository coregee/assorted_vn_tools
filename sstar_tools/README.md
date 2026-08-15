# Shining Star: VN Extract/Repack Tools

This repo provides a basic toolkit for extracting/repacking scripts/assets from visual
novels developed by Shining Star.

## Requirements

Python 3.7+

## How to use

1. Run the tools from the repo, passing the target game folder with
   `python extract.py -p "path\to\game"`. If the repo is in the game folder,
   you can omit `-p`.
2. By default, extraction only processes the scene scripts in `script.dat`.
3. Modify the extracted scripts as needed:
   - scene text in `script\*.json` (fill each page's `tr` field),
   - speaker names in `script\_names.json`.
   UI strings in `script\_system.json` are available when `--system` is used.
4. Run `python repack.py -p "path\to\game"` to repack `script.dat`.

Images, audio, and executable UI strings are only processed when their
selection flags are supplied explicitly. Multiple selection flags may be used
together.

## Additional Parameters

| Flag | Long form | Applies to | Description |
| ---- | ---- | ---- | ---- |
| `-p` | `--path` | both | Use a specific folder as the working directory. |
| `-s` | `--scripts` | both | Target scene `script.dat` only. |
| | `--system` | both | Target UI strings in `Game.exe` only. |
| `-i` | `--image` | both | Target image packs (`.cdt`) only. |
| | `--voice` | both | Target voice packs (`.vdt`) only. |
| | `--sound` | both | Target sound-effect packs (`.pdt`) only. |
| | `--music` | both | Target music packs (`.ovd`) only. |
| `-v ##` | `--vspace ##` | repack | Set the vertical line-spacing to `##` pixels (no value = default 30; 39 = stock). |
| `-c NN` | `--cols NN` | repack | Set a custom characters per line wrap width (default 54). |
| `-f` | `--force` | extract | Overwrite existing extracted files. |
