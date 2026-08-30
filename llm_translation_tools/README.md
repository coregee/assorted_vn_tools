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
python -m llm_translation_tools
```

Choose the game folder from the editor's welcome screen or the sidebar. The
button opens the operating system's native folder picker; typing a path remains
available as a fallback.

The folder may be either:

- a game/tool folder containing `script\`, or
- the extracted `script` folder itself.

Choose the actual game folder when using the in-app extract/repack actions. A
direct `script` folder is still supported for editing only.

The second form is useful for Dasaku when `extract.py -p ...` points at an
external game, because that tool keeps its editable corpus beside the tool.

The editor opens at `http://127.0.0.1:8765/`. Use `--no-browser` to suppress
automatic browser opening, `--port 0` to select a free port, or `--game` to
open a known folder immediately at launch.

## Workflow

1. Start LM Studio's local server and load a chat/instruct model.
2. Choose the target game folder in the editor.
3. If scripts have not been extracted yet, choose the matching **Game toolset**
   (or leave it on auto-detect) and use **Extract scripts**. The workbench runs
   the repository's existing tool against the selected game folder and opens
   the resulting script corpus when it succeeds.
4. Open **Settings**, load the model list, and select a model.
5. Describe the game, characters, tone, terminology, and naming rules in
   **Game context**. Customize the system prompt if needed.
6. Open a JSON file and choose **Translate untranslated**, select individual
   lines and choose **Translate selected**, or use **Re-translate file**. To
   process several files in order, select them in the sidebar and choose
   **Translate untranslated in selected files**.
7. Model translations are written directly to each file's native target fields.
   Use **Save changes** only for manual edits.
8. Inspect the translated JSON and use **Repack scripts** to run the matching
   toolset against the selected game folder. The workbench saves pending edits
   first and asks for confirmation before the repacker writes rebuilt game data.

The editor also supports direct manual editing, source/translation search,
speaker-name glossary files, protected non-translatable records, optimistic
save conflict detection, and keyboard shortcuts shown from the `?` button.

The in-app actions intentionally use each toolset's script-only default. Large
image/audio processing and executable/font patches remain explicit command-line
operations documented by the individual toolsets.

## Translation cycle

The model is not given isolated lines. Files are processed sequentially, and
every successful request turn is written to the native target fields before
the next request begins. The editor reloads those committed translations as
job progress advances. Within a file, translation runs in chronological
conversation turns using:

1. an editable system prompt, target language, and game-level context;
2. every earlier line in the file, in native order, including existing
   translations where available;
3. one or more stable, delimited target IDs in native script order;
4. the continuing user/assistant conversation from earlier lines in the same
   event file;
5. translated speaker-glossary context and original on-screen row boundaries;
6. a strict JSON response schema, exact ID/order validation, engine-token
   preservation, and up to three retries for malformed/incomplete output or
   transient LM Studio request failures.

Each request adds only the chronological lines not already present in the
conversation. Set **Batch by** to **Messages** to cap the number of target
messages per request, or **Source characters** to pack consecutive messages up
to a combined source-character limit. A single message is always included even
when it exceeds the character limit. Batches never cross file boundaries.

History resets at file boundaries because the extracted formats do not encode
a reliable cross-file story order. The configured **Response reserve** is kept
free within the model's **Context window**; when the input no longer fits, the
oldest complete conversation turns and then the oldest reference lines are
removed. Token budgeting uses a conservative UTF-8 byte estimate so it remains
model-independent.

Set **Context window** to the context length configured for the loaded LM Studio
model. For a good result, keep related event lines in their native file and
write specific game context before translating.

## Supported JSON

| Toolset | Source field | Translation field | Other supported files |
| --- | --- | --- | --- |
| Dasaku | `message` | `translated` | UI arrays and `names.json` |
| Etutane | `jp` | `translated` | `_names.json` |
| Shining Star | `jp` | `tr` | `_names.json` and `_system.json` |

The adapter changes only the native translation value. It does not add editor
metadata to game JSON or reorder records. Successful model output is saved
directly through the same native-field adapter used by manual edits.
Protected Dasaku engine-variable fields are read-only. Sstar `\xHH` and Etutane
`«HH»` engine tokens must be reproduced exactly or the model batch is rejected.

## Local safety and persistence

- The editor server only binds to a loopback address.
- LM Studio URLs are restricted to localhost/loopback unless **Allow a remote
  LM Studio host** is explicitly enabled.
- Writes are atomic and require the file revision token loaded by the editor;
  an externally changed file must be reloaded before saving.
- Per-project prompt/context settings are stored as
  `.llm_translation_tools.settings` in the opened root. Its non-JSON extension
  prevents the native repackers from mistaking it for a script.
- Jobs save after every successful request turn. Cancelling or failing a later
  request preserves all earlier committed translations, including partial
  progress within the current file.
- A request turn is not committed until its JSON contains every expected line
  in order. Timeouts, connection failures, rate limits, server errors, and
  invalid line counts are retried up to three times before the job stops.

Native wrapping, byte limits, and character encoding rules still apply. The
game-specific repacker remains the final validator.

## Tests

```powershell
python -m unittest discover -s llm_translation_tools\tests -v
```
