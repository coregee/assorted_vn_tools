# LLM Translation Tools

A local browser editor for the extracted script JSON produced by the three VN
toolsets in this repository. The Python server binds to loopback, the interface
runs in your normal browser, and translation requests go to a configurable OpenAI-compatible endpoint. This is
not a hosted service and has no ChatGPT Sites integration.

## Requirements

- Python 3.9 or newer. There are no third-party Python dependencies.
- Lemonade or another server implementing OpenAI-compatible `/models` and
  `/chat/completions` endpoints, with a chat/instruct model available.

The default base URL is `http://localhost:8000/api/v1` for
[Lemonade](https://lemonade-server.ai/docs/api/openai/). Enter the API base URL
shown by your server, including its path prefix. For LM Studio, use
`http://127.0.0.1:1234/v1`; other servers commonly use `/v1`.
Enter an optional **API key** for servers requiring Bearer authentication.
The key stays out of project files; **Save as default** stores it in plaintext
in your local user defaults file. Existing saved URLs are preserved. Requests use Chat Completions, so the server
need not implement the Responses API or store conversation IDs.

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

1. Start your OpenAI-compatible server and make a chat/instruct model available.
2. Choose the target game folder in the editor.
3. If scripts have not been extracted yet, choose the matching **Game toolset**
   (or leave it on auto-detect) and use **Extract scripts**. The workbench runs
   the repository's existing tool against the selected game folder and opens
   the resulting script corpus when it succeeds.
4. Open **Settings**, enter the server base URL, load the model list, select a model, and choose whether
   that model should use thinking/reasoning for translation requests.
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
   After repacking, every translation reported as truncated or unable to fit is
   flagged in the editor with the repacker's reason. Edit and save the line to clear
   its flag, then repack again. These flags live in `.llm_translation_tools.review`,
   not in the native game JSON.

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
job progress advances. Each `/chat/completions` request sends the retained
system/user/assistant transcript. Conversation history is managed locally for
validation, recovery, and context trimming. The response
reserve is subtracted from the model context window first. When the next turn
would overflow the remaining prompt budget, the oldest complete turns are
cleared until the retained prompt reaches the configured target. The default
50% clear setting targets half of the usable prompt budget while always keeping
the system message and current turn; 0% keeps the former behavior of trimming
only enough to fit. Subsequent requests send that shorter
transcript and fills the context again. Within a file, translation runs in
chronological conversation turns using:

For Etutane, one message is an actual displayed dialogue/narration page. Its
physical Japanese rows are joined without line breaks before being sent to the
model, so visual wrapping does not distract from translating the page as one
coherent unit.

1. an editable system prompt, target language, and game-level context;
2. every earlier line in the file, in native order, including existing
   translations where available;
3. one or more stable, delimited target lines in native script order;
4. the continuing user/assistant conversation from earlier lines in the same
   event file;
5. translated speaker-glossary context, with Japanese source line breaks removed;
6. a simple JSON string-array response schema, exact count/order validation,
   per-entry engine-token review flags, and up to three retries for
   malformed/incomplete output or transient LLM server request failures.

The editor requests a JSON schema when supported, falling back to plain chat
if the server rejects the format. All output still passes strict application-level
JSON, count, and order validation. Engine-token
mismatches are accepted with per-entry review flags; structurally invalid responses
are repaired conversationally before anything is committed.

Each new turn adds only the chronological lines not already present in the
conversation; the retained transcript is resent with each request. Set **Batch by** to **Messages** to cap the number of target
messages per request, or **Source characters** to pack consecutive messages up
to a combined source-character limit. A single message is always included even
when it exceeds the character limit. Batches never cross file boundaries.

History resets at file boundaries because the extracted formats do not encode
a reliable cross-file story order. The configured **Response reserve** is kept
free within the model's **Context window**; when the input no longer fits, the
oldest complete conversation turns and then the oldest reference lines are
removed. If LLM server reports a context overflow despite that estimate, the
oldest complete turn is removed and the request is retried as a new shortened
conversation. Token budgeting uses a conservative UTF-8 byte estimate so it
remains model-independent.

Set **Context window** to the context length configured for the loaded LLM server
model. For a good result, keep related event lines in their native file and
write specific game context before translating.

The **Enable thinking** setting is stored with the selected model settings. It
sends LLM server `reasoning_effort: "medium"` when enabled and `"none"` when
disabled. Models whose chat template does not expose controllable reasoning may
ignore the setting. If the server explicitly rejects `reasoning_effort`, the
client retries without it and omits it for the rest of that job.

Use **Save as default** in the settings dialog to apply the current form to the
open project and use it as the starting configuration for future projects. User
defaults are stored outside game folders in the operating system's user-config
directory; an existing project's `.llm_translation_tools.settings` values still
override them.

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
`«HH»` engine tokens should be reproduced exactly. A translation with missing,
changed, or reordered tokens is still saved, but that individual entry is flagged
for review. Review flags persist in `.llm_translation_tools.review`, outside the
native game JSON consumed by the repackers.

## Local safety and persistence

- The editor server only binds to a loopback address.
- LLM server URLs are restricted to localhost/loopback unless **Allow a remote
  LLM server host** is explicitly enabled.
- Writes are atomic and require the file revision token loaded by the editor;
  an externally changed file must be reloaded before saving.
- Per-project prompt/context settings are stored as
  `.llm_translation_tools.settings` in the opened root. Its non-JSON extension
  prevents the native repackers from mistaking it for a script.
- Jobs save after every successful request turn. Cancelling or failing a later
  request preserves all earlier committed translations, including partial
  progress within the current file.
- A request turn is not committed until its JSON array contains one string for every
  expected line in order. Timeouts, connection failures, rate limits, server errors, and
  invalid line counts are retried up to three times before the job stops.

Native wrapping, byte limits, and character encoding rules still apply. The
game-specific repacker remains the final validator.

## Tests

```powershell
python -m unittest discover -s llm_translation_tools\tests -v
```
