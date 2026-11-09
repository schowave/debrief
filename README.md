<div align="center">

# Debrief

**Every meeting recording, filed in the right place in your Obsidian vault – with summary, decisions and todos – after one quick review.**
**Plus a live assistant that suggests questions and models the process while you are still in the call.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)
[![Powered by Claude Code](https://img.shields.io/badge/powered%20by-Claude%20Code-D97757.svg)](https://docs.claude.com/en/docs/claude-code)

**English** · [Deutsch](README.de.md)

</div>

![Debrief walkthrough: from a MacWhisper recording to a filed note](docs/demo.gif)

*Review page – a recording becomes a note in `Clients/Acme/platform-migration/Erika Mustermann/` (invented data).*

![Live assistant: transcript, suggested questions and the event storming board filling up during a call](docs/live-demo.gif)

*Live assistant – questions, assumptions and an event storming board from the running transcript (invented data).*

## Contents

- [Why Debrief](#why-debrief)
- [Features](#features)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Usage](#usage)
- [Configuration](#configuration)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Privacy](#privacy)
- [Contributing](#contributing) · [License](#license)

## Why Debrief

Recording meetings is easy. Making them useful is the work: transcripts pile up in an inbox, nobody reads
them again, todos stay buried in the text, and filing them by hand is the first thing that gets skipped.

| Without Debrief | With Debrief |
|---|---|
| Transcripts sit unread in one folder | Each one is filed by client, project and person, where you would look for it |
| Todos are somewhere in 40 minutes of text | Your todos are `- [ ]` checkboxes for the Obsidian *Tasks* plugin, others' todos list their owner and due date |
| "What did we decide?" means searching the transcript | Summary and decisions are at the top of every note |
| Speakers are "Speaker 1" and "Speaker 2" | The model suggests who is who, based on how people are addressed and on the calendar invitees |
| The follow-up email is written from memory | A draft is generated from the summary, decisions and todos |
| The right question occurs to you after the call | The live assistant suggests it while you can still ask it |
| Handing everything to an AI means trusting it blindly | Nothing is filed before you confirmed it on a local review page; your corrections teach the model |
| Sensitive conversations leave the building | Optionally runs on a self-hosted LiteLLM-compatible gateway, so transcripts stay in-house |

The result is plain Markdown in your own vault: no lock-in, no new app to live in.

## Features

**After the meeting – review page**
- **Automatic sorting** into categories, projects and people, with a short reasoning and a confidence value
- **Summary, decisions, tags and todos**, written in the language of the conversation
- **Speaker naming** with AI suggestions; the names replace the placeholders everywhere in the note
- **Calendar context:** the matching macOS calendar event (title, organiser, invitees) is passed to the model
- **Follow-up email draft** (formal/informal, German/English) to copy or open in your mail client
- **Todos per project** in a `Todos.md` next to the notes; templates for *Tasks*, *Task Board* and *Bases*
- **Learns from corrections**, detects duplicates, imports older transcripts (folders, drag & drop, Teams chat)

**During the meeting – [live assistant](docs/live-assistant.md)**
- Reads MacWhisper's live transcript and suggests **questions**, **assumptions** to confirm and **open points**
- Models the discussed **process with event storming**, editable by hand or by plain-language instruction
- **Reviews** the process like an experienced modeller, **researches** in your repositories, draws it on **Miro**
- Hands everything over to the note once the transcript arrives

**Under the hood**
- **Event driven**, no polling: FSEvents for the inbox, Server-Sent Events for the page
- **Private by default:** the page only listens on `127.0.0.1`; configuration and learned data stay local
- German and English UI, switchable in the header

## Quick start

You need macOS, [mise](https://mise.jdx.dev) (`brew install mise`), [Claude Code](https://docs.claude.com/en/docs/claude-code)
and Obsidian with the **Local REST API** plugin.

```sh
git clone https://github.com/schowave/debrief.git && cd debrief
mise trust && mise run install   # first run creates config.yaml and categories.yaml, then stops
$EDITOR config.yaml categories.yaml   # vault path, your name, your clients and projects
mise run install                 # installs the service and starts it
mise run open                    # http://debrief.localhost:8765
```

Then point MacWhisper at the vault: Settings → Integrations → **Obsidian** with the IP/port/token of the Local REST
API plugin and the folder from `inbox` in `config.yaml` (default `/transkripts`). The next recording shows up as a
"Review transcript" notification.

<details>
<summary><b>All requirements and optional plugins</b></summary>

| What | Used for | Install |
|---|---|---|
| [mise](https://mise.jdx.dev) (**required**) | installs `uv`, `terminal-notifier`, `jq`, `node` and provides the tasks (see `mise.toml`) | `brew install mise` |
| [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`, **required**) | runs the analysis | `curl -fsSL https://claude.ai/install.sh \| bash` |
| [MacWhisper](https://goodsnooze.gumroad.com/l/macwhisper) (**required**) | records and transcribes the meetings | – |
| Obsidian + plugin **Local REST API** (**required**) | MacWhisper writes into the vault through it | Obsidian → Community plugins |
| Xcode Command Line Tools (`swiftc`, optional) | builds the calendar helper | `xcode-select --install` |
| Obsidian plugin **Tasks** (optional) | todo overview across all notes – template `templates/Todos.md`; copy `templates/debrief-tasks.css` to `.obsidian/snippets/` and enable it | Obsidian → Community plugins |
| Obsidian plugin **Task Board** (optional) | kanban board over all todos – template `templates/task-board-todos.json` (insert as `data.boardConfigs[0]` in `.obsidian/plugins/task-board/data.json` while the plugin is off, then enable it and "Scan Vault") | Obsidian → Community plugins |
| Obsidian **Bases** (built in, 1.9+) | conversation overview – copy `templates/Conversations.base` into the vault | – |

</details>

`config.yaml` and `categories.yaml` are git-ignored, so your personal data never ends up in the repository.
`install.sh` creates `~/Library/LaunchAgents/de.<user>.debrief.plist`; the service runs at login and restarts after a
crash. macOS shows "Background item added" once; keep **debrief** allowed under System Settings → General → Login Items.

Tip: naming speakers in MacWhisper pays off – the todo assignees will be correct.

## How it works

```
MacWhisper ──(Obsidian integration)──▶ Vault/transkripts/
                                              │  server.py (macOS file system events)
                                              ▼
                                    LLM analysis (claude -p, optionally via your own gateway)
                                              │  suggestion in pending/
                                              ▼
                       Notification ──click──▶ review page http://debrief.localhost:8765
                                              │  "File to Obsidian"
                                              ▼
                       Vault/<category folder>[/<project>[/<person>]]/<date> <title>.md
```

During a call the live page (`/live`) reads MacWhisper's database read-only, analyses the new lines every ~75 s and
links its session to the transcript once it arrives in the inbox.

## Usage

### Review page

New recording → after 20–60 s a "Review transcript" notification appears → click opens the review page. If the page
is already open it updates itself.

- **Title** – edit it directly in the heading
- **Category / project / person** – pick via chips; "AI" marks the model's suggestion. "＋ New …" creates entries
  on the spot, with the switch you remember them permanently. The target path is shown live
- **Name speakers** – "Speaker 1" etc. with a sample sentence; the model suggests a name with reasoning
- **Todos** – "Me"/"Others", assignee, due date, tick to file as already done (`- [x] … ✅ <date>`), flag as
  urgent (`⏫`, also suggested by the model), assign a todo to another project of the same client
- **Summary** (click to edit), **decisions**, **tags**, transcript
- **Follow-up email** – choose formal/informal and the language, create a draft, copy it or open it in the mail client
- **File to Obsidian** (⌘↵) writes the note and jumps to the next open transcript
- **Re-analyse** (also with another model), **Skip** (stays in the inbox), **Discard** (to the macOS trash, undoable)

| Key | Action |
|---|---|
| <kbd>J</kbd> / <kbd>K</kbd> | next / previous transcript |
| <kbd>1</kbd>–<kbd>9</kbd> | pick category |
| <kbd>⌘</kbd><kbd>↵</kbd> | file to Obsidian |
| <kbd>?</kbd> | all shortcuts |

More details (direct mode, models and cost, calendar, import, duplicates, settings) are in [README.de.md](README.de.md).

### Live assistant

Open `http://debrief.localhost:8765/live` in its own window next to the call; when MacWhisper starts recording, a
notification offers to open it. Transcript on the left, suggested questions and assumptions in the middle, the event
storming board on the right. Rate assumptions, mark questions as asked, change the process by instruction, review it,
save it to the vault or draw it on Miro.

→ **[Full guide: docs/live-assistant.md](docs/live-assistant.md)** (sessions, recordings, process board, review,
Miro setup, code research, settings)

## Configuration

**`config.yaml`** (template: [`config.example.yaml`](config.example.yaml)) – also editable on the review page under
*Settings → Configuration*.

| Key | Meaning |
|---|---|
| `vault` | path to the Obsidian vault |
| `inbox` | folder in the vault MacWhisper writes to |
| `ich` | your full speaker name in the transcripts (for "my todos"; a first name alone also matches namesakes) |
| `rolle` | optional, your role (e.g. "consultant at Acme") – helps the model |
| `model` | model for `claude -p`, e.g. `sonnet` or a gateway model |
| `sprache` | `de` or `en`: language of notifications (the page remembers its own choice) |
| `gateway.base_url` | optional LiteLLM-compatible gateway. Without `gateway` your normal Claude subscription is used (data then goes to Anthropic) |
| `gateway.token_cmd` / `token_env` / `token` | API key: a command printing it (e.g. `security find-generic-password -s my-gateway -w`), an environment variable, or the key itself |
| `min_konfidenz`, `unklar_ordner` | direct mode only |
| `review.host`, `review.port` | address of the review page. `*.localhost` works without setup; ports below 1024 need admin rights |
| `review.auto_open` | `false`: notification, click opens the page. `true`: opens a browser tab directly |
| `review.mitteilung` | `false` turns notifications off |
| `min_woerter` | shorter recordings are not analysed but offered for discarding |
| `kalender`, `kalender_fenster` | use the calendar event at recording time as context |
| `links` | wikilinks `[[client]]`, `[[project]]`, `[[person]]`, `[[participants]]` at the top of each note (backlinks) |
| `duplikate` | thresholds for duplicate detection |
| `import.ordner` | folders with older transcripts for the import dialog |
| `live`, `miro` | live assistant and Miro, see [docs/live-assistant.md](docs/live-assistant.md) |

**`categories.yaml`** – also editable on the review page under *Settings → Categories*.

```yaml
acme:                           # value of the `kunde` property
  ordner: Kunden/Acme           # target folder in the vault
  hinweise: client project …    # helps the model to assign
  projekte:
    plattform-migration:        # -> Kunden/Acme/plattform-migration/
      hinweise: move to the cloud
      personen: [Erika Mustermann]   # -> Kunden/Acme/plattform-migration/Erika Mustermann/
```

Good `hinweise` (project names, contacts, typical terms) improve the assignment the most.

> The keys in the YAML files and the note format (`kunde:`, `## Transkript`, `## Meine Todos` …) are German on purpose:
> the Obsidian templates in `templates/` and existing notes rely on them.

## Development

All tasks run through mise (`mise tasks` lists them):

| Task | Purpose |
|---|---|
| `mise run install` / `uninstall` | set up or reload / remove the service (files and suggestions stay) |
| `mise run restart` | restart the service and wait until the page answers |
| `mise run status` | is the service running? which suggestions are open? |
| `mise run open` / `logs` | open the review page / follow the logs |
| `mise run dev` | run the server in the terminal; the service pauses meanwhile |
| `mise run calendar-access` | request/check calendar access of the helper |
| `mise run dry-run` | analyse the inbox, only print |
| `mise run sort` | direct mode: file without review |
| `mise run check` | `lint` (ESLint, ruff) and `test` (pytest) – runs before every commit after `mise run hooks` |
| `mise run smoke` | click through the live page in headless Chrome against a scratch server (fixture data, model stub) |
| `mise run demo` | record `docs/live-demo.gif` the same way (needs Google Chrome and ffmpeg) |

**When changes take effect**

| Changed | Takes effect |
|---|---|
| `static/*.html`, `static/i18n.js`, `categories.yaml` | immediately (reload the page) |
| `server.py`, `debrief.py`, `live.py`, `macwhisper.py`, `miro.py`, `transcripts.py` | after `mise run restart` |
| `config.yaml` | via *Settings → Configuration* immediately, otherwise `mise run restart` |
| `mise.toml`, `launchd.plist.template` | `mise run install` |

<details>
<summary><b>Project layout</b></summary>

| File | Content |
|---|---|
| `server.py` | service: watch the inbox, analysis, review page + API |
| `debrief.py` | analysis (prompt, schema), render/file notes; also usable as a CLI |
| `importer.py` | import from folders, drag & drop and the clipboard (Teams chat headers) |
| `live.py`, `macwhisper.py` | live assistant (sessions, merge rules, prompts, process library) and read-only access to MacWhisper's database |
| `transcripts.py` | existing transcripts (MacWhisper export, chat, VTT/SRT, text) as a recording for the live assistant |
| `miro.py` | draws the event storming board on a Miro board (REST API v2) |
| `static/index.html`, `static/live.html`, `static/i18n.js` | review page, live assistant and their English texts |
| `tests/` | unit tests (`mise run test`) and the browser smoke test (`mise run smoke`) |
| `config.example.yaml`, `categories.example.yaml` | templates for your local `config.yaml` / `categories.yaml` |
| `calendar/` | source and build script of the calendar helper (`bin/Debrief Calendar.app`, not in git) |
| `templates/` | Obsidian templates (Bases, Tasks, Task Board) |
| `docs/` | live assistant guide, demo GIFs and the script recording the live demo |
| `install.sh`, `launchd.plist.template`, `bin/debrief` | service setup |

</details>

## Troubleshooting

| Symptom | What to do |
|---|---|
| Page does not load | `mise run status`, `mise run logs` |
| Analysis fails | The error is shown on the page and in `debrief.log`. Common causes: API key not readable (test `token_cmd` in a terminal) or gateway not reachable (VPN?) |
| No notification | System Settings → Notifications → allow "terminal-notifier" |
| Transcript not picked up | Files are analysed 20 s after their last change; skipped ones are listed as "Skipped" |

## Privacy

Transcripts contain client and personnel internals. With a `gateway` they can be processed on models you host
yourself; without it they go to Anthropic through your Claude account. The review page only listens on `127.0.0.1`
and only accepts JSON requests from its own page. MacWhisper's database is only ever opened read-only.
`config.yaml`, `categories.yaml` and everything learned (`learned.json`, `filed.json`, …) stay local and are git-ignored.

## Contributing

Issues and pull requests are welcome. Before committing, run `mise run check` (or enable it as a hook with
`mise run hooks`); after changing the live page, also `mise run smoke`. For UI changes please add the English text to
`static/i18n.js` (the German text is the key).

## License

[MIT](LICENSE)
