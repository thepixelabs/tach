# Tach

A local dashboard for coding agents that run on local models. It reads the history your agents already write to disk and shows generation speed, prompt cache reuse, context growth, tool latency, and where your agent's time actually went.

![The Tach dashboard](docs/assets/shots/dashboard-dark.jpg)

Zero dependencies, no account, no network. Python standard library and vanilla JavaScript.

Tach is scoped to models you run yourself; agents that only talk to a hosted API are not read, on purpose. It reads history after the fact, so nothing sits in the path of your agent while it works.

## Why

A terminal gives you a spinner and a token count. It does not tell you:

* Whether a slow session was the model thinking or a tool call that hung
* How much of your context was served from cache instead of re-encoded
* Which tools fail, and which ones quietly eat hours
* How large your context actually grows, as distinct from how much input you are billed for
* Which model is genuinely faster for your work

Tach answers those from data your machine already has.

## Quick start

Python 3.9 or newer. Nothing to install.

```bash
git clone https://github.com/thepixelabs/tach
cd tach
./run
```

Then open <http://127.0.0.1:3344>.

```bash
./run 8080          # another port
./run --no-browser  # start it but leave the browser alone
python3 server.py   # skip the wrapper; takes a port too
```

## What it reads

Nothing to configure. On startup Tach looks for:

| Source | Where |
|---|---|
| **OpenCode** | `opencode.db` in XDG data dirs, `~/Library/Application Support/opencode/`, `~/.opencode/`, or `%APPDATA%` |
| **OpenClaw** | JSONL transcripts under every OpenClaw home: `$OPENCLAW_HOME`, `~/.openclaw`, XDG/Application Support, and project-local `.openclaw` dirs |
| **Hermes Agent** | `state.db` under `HERMES_HOME` (default `~/.hermes`), including named profiles |
| **Aider** | `.aider.chat.history.md` in the working tree, your home dir, and sibling project dirs |
| **Continue** | `~/.continue/sessions/` |
| **Cline** | VS Code global storage, `saoudrizwan.claude-dev/tasks` |
| **Roo Code** | VS Code global storage, `rooveterinaryinc.roo-cline/tasks` |
| **Zed** | `Zed/conversations` and `Zed/threads` |
| **Goose** | `goose/sessions/` in XDG data dirs |
| **LM Studio** | `~/.lmstudio/conversations/` |
| **Jan** | `~/jan/threads/` |

Seven more are found and listed but not read yet: Crush, Open WebUI, LibreChat, AnythingLLM, Msty, Chatbox and GPT4All. Your sidebar shows only what is actually on your machine, and Settings rescans on demand after you install something new.

Fifteen local inference servers are probed for reachability, loaded model and throughput: MLX LM (`:8080`), Ollama (`:11434`), llama.cpp (`:8077`), LM Studio (`:1234`), vLLM (`:8000`), Jan (`:1337`), KoboldCpp (`:5001`), Text-gen WebUI (`:5000`), LocalAI (`:8081`), SGLang (`:30000`), Cortex (`:39281`), TabbyAPI (`:5555`), Open WebUI (`:3000`), the Hermes gateway (`:8642`) and the OpenClaw gateway (`:18789`). Several of these share port 8080, so each probe carries the endpoint that identifies the server rather than trusting the port alone.

### Overrides

Paths are discovered automatically. Override them only if yours are unusual.

```bash
python3 server.py --opencode-db /path/to/opencode.db
python3 server.py --openclaw-db /path/to/openclaw.sqlite
python3 server.py --openclaw-home /path/to/.openclaw   # repeatable
```

`OPENCODE_DB`, `OPENCLAW_DB`, `OPENCLAW_HOME`, `HERMES_HOME` and `XDG_DATA_HOME` are read during discovery and do the same job. `API_SERVER_PORT` is read too, but it sets the port Tach probes for the Hermes gateway, not the port Tach itself serves on.

## What it shows

**Observatory.** A dashboard you can rearrange: decode speed percentiles, the split of agent wall clock between tool execution, reasoning and generation, cache savings, outcome and failure counts, context economics. Save any arrangement as a named dashboard, set a default, duplicate or delete it. Built-in dashboards can be customised and reset, but not deleted. Panels move and resize on a twelve column grid, and layouts live in your browser.

**Sessions.** Every session across every source, searchable and filterable, with a full transcript view including per turn tokens and tool calls.

**Engines.** Live metrics from whichever inference server is running: throughput, time to first token, prefix cache hit rate, request history.

**Tools.** Call counts, error rates and latency percentiles per tool, plus the individual calls that ate the most wall clock.

**Models.** Every model you have used, compared on speed, cache reuse, error rate and context handling.

**Projects.** Where effort landed, grouped by repository, and which files the agent kept re-editing.

## How the numbers are defined

Some of these differ from what other tools report, so they are worth stating plainly.

**Decode speed** is measured per assistant turn and reported as a median with p90, p99 and peak. A single average hides a wide distribution. Dividing total output by total elapsed time answers a different question, and that figure is shown separately as end to end throughput, which includes time spent in tools.

**Billed input** is the sum of prompt tokens across turns. Every turn resends the conversation, so this grows far faster than your context does. **Peak context** is the largest single turn, and is the number to read when you want to know how big your context got.

**Turn time** splits into tool execution, reasoning, and the remainder. That remainder is prefill, generation and queueing together. Most sources do not record time to first token, so prefill cannot be separated out and is not claimed to be.

**Live engine metrics** exist only while that engine is running. Nothing is recorded when it is offline, and panels say so rather than showing a stale or placeholder value.

## Export and API

**Export JSON** in the header, or `GET /api/export`, returns the filtered set with per turn token counts, durations, tool events and timestamps.

The rest of the API is directly useful too: `/api/stats`, `/api/sessions`, `/api/session/<id>`, `/api/turns`, `/api/tools`, `/api/models`, `/api/projects`, `/api/timeseries`, `/api/live`, `/api/catalog`.

## Privacy

Tach collects nothing, sends nothing and stores nothing about you. There is no account, no key, no analytics and no update check. The server binds to `127.0.0.1`, refuses requests that do not carry a local `Host` header, and its only outbound requests go to inference servers on your own loopback. Fonts and icons are served from this repo, so a normal run makes no external request at all.

Your data stays in the files your agents already wrote. Tach opens them read only and never writes to them.

Your home directory is masked to `~/...` everywhere it appears, including inside message bodies and tool output, so a screenshot or a pasted export does not give away your username or directory layout. Project names, session titles, branch names and model names are shown as they are.

## Contributing

Pull requests welcome. Two constraints: the backend stays standard library only, and paths stay masked.

Adding a source is a catalog entry plus a reader. Adding a server is a catalog entry with the endpoint that identifies it.

Tests are Node's built-in runner, no install required:

```bash
node --test test/*.test.js
```

## Security

See [SECURITY.md](SECURITY.md) for the threat model and how to report a problem.

## License

MIT. See [LICENSE](LICENSE). That includes the warranty disclaimer: this is provided as is.
