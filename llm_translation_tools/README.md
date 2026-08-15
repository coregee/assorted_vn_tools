# LLM Translation Tools

A local browser editor for the extracted script JSON produced by the three VN
toolsets in this repository. The Python server binds to loopback, the interface
runs in your normal browser, and translation requests go to LM Studio. This is
not a hosted service and has no ChatGPT Sites integration.

## Requirements

- Python 3.9 or newer. There are no third-party Python dependencies.
- [LM Studio](https://lmstudio.ai/docs/developer/core/server) with a chat model
  loaded and its local server running.

LM Studio's default OpenAI-compatible base URL is
`http://127.0.0.1:1234/v1`. The editor uses its `/models` and
`/chat/completions` endpoints.

## Start the editor

From this repository's root:

```powershell
python -m llm_translation_tools --game "C:\Games\My Visual Novel"
```

The folder may be either:

- a game/tool folder containing `script\`, or
- the extracted `script` folder itself.

The second form is useful for Dasaku when `extract.py -p ...` points at an
external game, because that tool keeps its editable corpus beside the tool.

The editor opens at `http://127.0.0.1:8765/`. Use `--no-browser` to suppress
automatic browser opening, `--port 0` to select a free port, or omit `--game`
and enter a folder path in the interface.

## Workflow

1. Start LM Studio's local server and load a chat/instruct model.
2. Open the extracted script folder in the editor.
3. Open **Settings**, load the model list, and select a model.
4. Describe the game, characters, tone, terminology, and naming rules in
   **Game context**. Customize the system prompt if needed.
5. Open a JSON file and choose **Translate untranslated**, select individual
   lines and choose **Translate selected**, or use **Re-translate file**.
6. Review the proposed translations individually or accept them together.
   Suggestions remain unsaved edits until **Save changes** is pressed.
7. Run the relevant toolset's `repack.py` after reviewing the JSON.

The editor also supports direct manual editing, source/translation search,
speaker-name glossary files, protected non-translatable records, optimistic
save conflict detection, and keyboard shortcuts shown from the `?` button.

## Translation cycle

The model is not given isolated lines. Each file is translated in sequential
batches using:

1. an editable system prompt, target language, and game-level context;
2. surrounding lines before and after the requested targets, including existing
   translations where available;
3. stable, delimited target IDs in native script order;
4. a bounded history of prior user/assistant batch messages within the same
   event file;
5. translated speaker-glossary context and original on-screen row boundaries;
6. a strict JSON response schema, exact ID/order validation, engine-token
   preservation, and one corrective retry for malformed output.

History resets at file boundaries because the extracted formats do not encode a
reliable cross-file story order. For a good result, keep related event lines in
their native file and write specific game context before translating.

## Supported JSON

| Toolset | Source field | Translation field | Other supported files |
| --- | --- | --- | --- |
| Dasaku | `message` | `translated` | UI arrays and `names.json` |
| Etutane | `jp` | `translated` | `_names.json` |
| Shining Star | `jp` | `tr` | `_names.json` and `_system.json` |

The adapter changes only the native translation value. It does not add editor
metadata to game JSON, reorder records, or silently save model responses.
Protected Dasaku engine-variable fields are read-only. Sstar `\xHH` and Etutane
`«HH»` engine tokens must be reproduced exactly or a suggestion is rejected.

## Local safety and persistence

- The editor server only binds to a loopback address.
- LM Studio URLs are restricted to localhost/loopback unless **Allow a remote
  LM Studio host** is explicitly enabled.
- Writes are atomic and require the file revision token loaded by the editor;
  an externally changed file must be reloaded before saving.
- Per-project prompt/context settings are stored as
  `.llm_translation_tools.settings` in the opened root. Its non-JSON extension
  prevents the native repackers from mistaking it for a script.
- A model suggestion is only a proposal. Accept it, inspect it, then save it.

Native wrapping, byte limits, and character encoding rules still apply. The
game-specific repacker remains the final validator.

## Tests

```powershell
python -m unittest discover -s llm_translation_tools\tests -v
```
