# Token Telemetry

A lightweight, zero-dependency local observatory for your coding agents and LLM inference servers. Token Telemetry hooks into your local model harnesses and engines to track generation speed, prompt caching efficiency, token costs, context growth, and tool execution patterns in real time.

```
┌────────────────────────────────────────────────────────────────────────┐
│  TOKEN TELEMETRY OBSERVATORY                                           │
│  Harnesses: OpenCode • OpenClaw • Aider • Continue                     │
│  Engines:   MLX LM • Ollama                                            │
└────────────────────────────────────────────────────────────────────────┘
```

## Why Token Telemetry?

When running local models for agentic coding, standard terminals only show brief progress indicators or raw token counts. Critical performance characteristics often remain hidden:

* Why did one turn decode at 32 tokens per second while another spiked to 118,000?
* How much time was spent on prompt prefill versus token generation?
* Did Automatic Prefix Caching (APC) hit, or did the model recompute the entire repository context?
* Which tool calls consume the most tokens and context window space?

Token Telemetry answers these questions through a clean, customizable dashboard that runs entirely on your local machine without external telemetry services or cloud dependencies.

## Key Capabilities

### Multi-Harness Auto-Discovery
You do not need to configure paths manually. On startup, the server automatically inspects standard operating system locations for:

1. **OpenCode**: Reads `opencode.db` from standard XDG data directories, `~/Library/Application Support/opencode/`, and local application data folders.
2. **OpenClaw**: Scans every OpenClaw home on the machine (`$OPENCLAW_HOME`, `~/.openclaw`, XDG/Application Support locations, and project-local `.openclaw` directories) for agent transcripts under `agents/<agent>/sessions/*.jsonl`, and reads `state/openclaw.sqlite` for live ACP sessions.
3. **Aider**: Scans git repositories and project roots for `.aider.chat.history.md` records.
4. **Continue.dev**: Pulls session metadata and prompt histories from `~/.continue/sessions/`.
5. **Local Servers**: Queries live endpoints on MLX LM (`localhost:8080`), Ollama (`localhost:11434`), and the OpenClaw gateway (port read from `openclaw.json`, default `18789`) for reachability, active model status, context windows, and continuous batching metrics. These are engines, not session archives, and are listed separately from data sources in the sidebar.

### Speculative Decoding & MTP Telemetry
If you run modern speculative architectures such as Qwen MTP or Medusa on Apple Silicon, you will occasionally notice instantaneous decode rates exceeding 100,000 tokens per second in your logs. Token Telemetry captures and explains these multi-token verification bursts alongside standard autoregressive generation curves.

### Privacy and Anonymization
All paths displayed in the dashboard and exported datasets are automatically sanitized. User home directories are masked to `~/...` so you can record demos or share telemetry screenshots without leaking sensitive filesystem structures or usernames.

### Customizable Dashboard
Every chart and telemetry card can be resized between compact, normal, wide, and full span. You can remove cards you do not need, add panels from the gallery, or reset to default layouts at any time. Preferences persist locally in your browser.

## Quick Start

### Requirements
Token Telemetry requires Python 3.9 or newer. There are no third-party pip packages to install; everything relies on Python's built-in standard library (`sqlite3`, `http.server`, `urllib`, `json`).

### Running the Server

Clone the repository and run:

```bash
chmod +x run.sh
./run.sh
```

Or invoke the Python server directly:

```bash
python3 server.py
```

By default, the server listens on `http://127.0.0.1:3344` and opens your default browser.

### Custom Flags and Overrides

If your database or session directory lives in a custom location, pass it via command-line arguments or environment variables:

```bash
# Specify a custom port
python3 server.py 8080

# Specify custom database paths
python3 server.py --opencode-db /path/to/opencode.db --openclaw-db /path/to/openclaw.sqlite

# Add an OpenClaw home to scan for transcripts (repeatable)
python3 server.py --openclaw-home /path/to/.openclaw

# Run headless without opening a browser
python3 server.py --no-browser
```

Supported environment variables include `OPENCODE_DB`, `OPENCLAW_DB`, `AIDER_DIR`, `MLX_HOST`, and `OLLAMA_HOST`.

## Data Export

Click the **Export JSON** button in the header or make an HTTP request to `/api/export`. The endpoint yields filtered turn-by-turn records containing prompt token counts, completion token counts, duration metrics, tool call events, and timestamps for your own scientific benchmarks.

## Contributing

Contributions are welcome. Please ensure pull requests keep the zero-dependency philosophy intact (standard library only for backend services) and maintain path sanitization for privacy.
