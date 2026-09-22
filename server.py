"""
Tach Server
Serves real-time and historic performance data from local LLM harnesses:
OpenCode, OpenClaw, Hermes, Aider, Continue, Cline, Roo Code, Zed,
Goose, LM Studio and Jan, plus whichever local inference servers are running.
"""

import os
import sys
import json
import sqlite3
import time
from datetime import datetime
import urllib.request
import urllib.error
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
import argparse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

PORT = 3344
STATIC_DIR = Path(__file__).parent / "static"


def sanitize_path(p):
    """Replaces home directory path with ~ for privacy in open-source use."""
    if not p:
        return ""
    try:
        home_str = str(Path.home())
        if p.startswith(home_str):
            return "~" + p[len(home_str):]
    except Exception:
        pass
    return str(p)


def scrub_home(value):
    """
    Masks the home directory anywhere inside a string, not only at the start.

    sanitize_path only ever handled values that were paths in their own right,
    which left the home directory visible inside message bodies, tool output and
    error text. Those are the parts of a transcript people paste into an issue.
    """
    if not value:
        return value
    try:
        home = str(Path.home())
    except Exception:
        return value
    if not home or home == "/":
        return value
    out = value.replace(home, "~")
    # A file URL carries the same path in a different dress.
    out = out.replace("file://" + home, "file://~")
    return out


def scrub_payload(obj, _depth=0):
    """Walks a response and masks the home directory in every string in it."""
    if _depth > 12:
        return obj
    if isinstance(obj, str):
        return scrub_home(obj)
    if isinstance(obj, list):
        return [scrub_payload(v, _depth + 1) for v in obj]
    if isinstance(obj, dict):
        return {k: scrub_payload(v, _depth + 1) for k, v in obj.items()}
    return obj


def resolve_opencode_db(cli_path=None):
    """
    Automatically resolves the OpenCode database location.
    Checks CLI arguments, OPENCODE_DB environment variable,
    standard XDG data directories, macOS Application Support, and Windows AppData.
    """
    if cli_path and os.path.exists(cli_path):
        return Path(cli_path)

    env_path = os.environ.get("OPENCODE_DB")
    if env_path and os.path.exists(env_path):
        return Path(env_path)

    home = Path.home()
    candidates = []

    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        candidates.append(Path(xdg_data) / "opencode" / "opencode.db")

    candidates.extend([
        home / ".local" / "share" / "opencode" / "opencode.db",
        home / "Library" / "Application Support" / "opencode" / "opencode.db",
        home / ".opencode" / "opencode.db",
    ])

    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if appdata:
        candidates.append(Path(appdata) / "opencode" / "opencode.db")

    for p in candidates:
        if p.exists():
            return p

    return home / ".local" / "share" / "opencode" / "opencode.db"


def resolve_openclaw_homes(cli_paths=None):
    """
    Discovers every OpenClaw home directory on this machine.

    OpenClaw keeps its chat transcripts as JSONL under
    <home>/agents/<agent>/sessions/, not in the gateway state database, and a
    machine can hold several homes at once: the default ~/.openclaw, an
    OPENCLAW_HOME override, and project-local ones next to a checkout.
    """
    homes = []

    def add(candidate):
        try:
            path = Path(candidate).expanduser()
        except Exception:
            return
        if path.is_dir() and path not in homes:
            homes.append(path)

    for cli_path in (cli_paths or []):
        add(cli_path)

    env_home = os.environ.get("OPENCLAW_HOME")
    if env_home:
        add(env_home)

    home = Path.home()
    add(home / ".openclaw")

    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        add(Path(xdg_data) / "openclaw")

    add(home / ".local" / "share" / "openclaw")
    add(home / "Library" / "Application Support" / "openclaw")

    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if appdata:
        add(Path(appdata) / "openclaw")

    # Project-local homes: the working directory and its siblings, the same
    # sweep scan_aider_history() uses to find per-repo chat histories.
    cwd = Path.cwd()
    add(cwd / ".openclaw")
    try:
        for sub in list(cwd.parent.iterdir())[:40]:
            if sub.is_dir():
                add(sub / ".openclaw")
    except Exception:
        pass

    return homes


def resolve_openclaw_db(cli_path=None, homes=None):
    """Resolves the OpenClaw gateway state database if present on the system."""
    if cli_path and os.path.exists(cli_path):
        return Path(cli_path)
    env_path = os.environ.get("OPENCLAW_DB")
    if env_path and os.path.exists(env_path):
        return Path(env_path)
    for home in (homes if homes is not None else resolve_openclaw_homes()):
        for candidate in (home / "state" / "openclaw.sqlite", home / "openclaw.sqlite"):
            if candidate.exists():
                return candidate
    return None


def resolve_openclaw_gateway():
    """
    Reads the gateway bind port out of the OpenClaw config so the health probe
    targets the port this install actually listens on rather than the default.
    """
    port = 18789
    for home in CONFIG.get("openclaw_homes", []):
        cfg_file = home / "openclaw.json"
        if not cfg_file.exists():
            continue
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        gw = cfg.get("gateway") or {}
        if isinstance(gw.get("port"), int):
            port = gw["port"]
            break
    return port


def scan_aider_history():
    """Discovers Aider chat history files in cwd and common project repositories."""
    found = []
    cwd = Path.cwd()
    candidates = [cwd / ".aider.chat.history.md", Path.home() / ".aider.chat.history.md"]
    
    # Also check immediate sibling directories under common code locations
    try:
        parent = cwd.parent
        if parent.exists():
            for sub in list(parent.iterdir())[:20]:
                if sub.is_dir() and (sub / ".aider.chat.history.md").exists():
                    candidates.append(sub / ".aider.chat.history.md")
    except Exception:
        pass

    for p in candidates:
        if p.exists() and p not in found:
            found.append(p)
    return found


def scan_continue_sessions():
    """Discovers Continue.dev saved session directories if present."""
    cont_dir = Path.home() / ".continue" / "sessions"
    if cont_dir.exists() and cont_dir.is_dir():
        return list(cont_dir.glob("*.json"))
    return []


# Global active database path
_OPENCLAW_HOMES = resolve_openclaw_homes()
CONFIG = {
    "opencode_db": resolve_opencode_db(),
    "openclaw_homes": _OPENCLAW_HOMES,
    "openclaw_db": resolve_openclaw_db(homes=_OPENCLAW_HOMES),
}


def get_db_connection():
    db_path = CONFIG["opencode_db"]
    if not db_path or not os.path.exists(db_path):
        return None
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


# ============================================================
# ENGINE LIVE DETAIL
# Every engine gets a live card built from what it actually publishes. Each
# reader returns the same shape so the page renders them generically:
#   pulse  {value, unit, sub}        the headline on the live card
#   facts  [{label, value}]          the "Live details" panel
#   note   str                       what to switch on for more, if anything
#   served / installed / loaded      the model inventory
# Endpoints and field names are from each project's docs or source; see the
# comment on each reader.
# ============================================================

def _http_get(port, path, timeout=0.8, text=False):
    """(status, body). body is parsed JSON, or raw text when text=True."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"User-Agent": "Telemetry"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2_000_000).decode("utf-8", "replace")
            if text:
                return resp.status, raw
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def _prom(text):
    """
    Prometheus text format to {metric: value}, summed across label sets (one
    model, one engine is the local case). Histograms keep their _sum and
    _count series, which is enough for an average.
    """
    out = {}
    for line in (text or "").splitlines():
        if not line or line.startswith("#"):
            continue
        name_part, _, rest = line.partition(" ") if "{" not in line.split(" ")[0] else line.partition("} ")
        name = name_part.split("{")[0].strip()
        try:
            val = float(rest.strip().split(" ")[0])
        except Exception:
            continue
        if name.endswith("_bucket"):
            continue
        out[name] = out.get(name, 0.0) + val
    return out


def _avg(m, base):
    n = m.get(base + "_count")
    return (m.get(base + "_sum", 0.0) / n) if n else None


def _fact(label, value):
    return {"label": label, "value": value}


def _openai_served(port):
    st, body = _http_get(port, "/v1/models")
    if st == 200 and isinstance(body, dict) and isinstance(body.get("data"), list):
        return [m for m in body["data"] if isinstance(m, dict) and m.get("id")]
    return None


def _live_lmstudio(port):
    # lmstudio.ai/docs/developer/rest/list: /api/v1/models (0.4+) marks loaded
    # models with loaded_instances[]; /api/v0/models has state "loaded".
    st, body = _http_get(port, "/api/v1/models")
    models = []
    if st == 200 and isinstance(body, dict):
        for m in body.get("models") or body.get("data") or []:
            inst = m.get("loaded_instances") or []
            q = m.get("quantization") or {}
            models.append({
                "name": m.get("key") or m.get("display_name"),
                "size_gb": round((m.get("size_bytes") or 0) / 1024**3, 2),
                "parameters": m.get("params_string"),
                "quantization": q.get("name") if isinstance(q, dict) else q,
                "context": ((inst[0].get("config") or {}).get("context_length") if inst else None) or m.get("max_context_length") or 0,
                "loaded": bool(inst),
            })
    else:
        st, body = _http_get(port, "/api/v0/models")
        if st == 200 and isinstance(body, dict):
            for m in body.get("data") or []:
                models.append({"name": m.get("id"), "quantization": m.get("quantization"),
                               "context": m.get("max_context_length") or 0,
                               "loaded": m.get("state") == "loaded", "size_gb": 0})
    if st != 200:
        return None
    loaded = [m for m in models if m["loaded"]]
    return {
        "installed": models, "loaded": [{"name": m["name"], "context": m["context"]} for m in loaded],
        "pulse": {"value": escape_count(len(loaded), "model") + " loaded", "sub": loaded[0]["name"] if loaded else "Nothing loaded"},
        "facts": [_fact("Loaded", ", ".join(m["name"] for m in loaded) or "none"),
                  _fact("Context", f"{loaded[0]['context']:,}" if loaded and loaded[0]["context"] else "–"),
                  _fact("Downloaded models", len(models))],
        "note": "LM Studio reports speed per completion only, so there is no running tokens/s figure.",
    }


def _live_vllm(port):
    # vllm/v1/metrics/loggers.py; V0 used gpu_cache_usage_perc, V1 renamed it
    # kv_cache_usage_perc. /metrics, /version stay open even with --api-key.
    served = _openai_served(port)
    _, text = _http_get(port, "/metrics", text=True)
    m = _prom(text)
    if served is None and not m:
        return None
    kv = m.get("vllm:kv_cache_usage_perc", m.get("vllm:gpu_cache_usage_perc"))
    running = int(m.get("vllm:num_requests_running", 0))
    waiting = int(m.get("vllm:num_requests_waiting", 0))
    hits, queries = m.get("vllm:prefix_cache_hits_total"), m.get("vllm:prefix_cache_queries_total")
    ttft = _avg(m, "vllm:time_to_first_token_seconds")
    itl = _avg(m, "vllm:inter_token_latency_seconds") or _avg(m, "vllm:time_per_output_token_seconds")
    _, ver = _http_get(port, "/version")
    first = (served or [{}])[0]
    return {
        "served": [s["id"] for s in served or []],
        "pulse": {"value": f"{running}", "unit": "running", "sub": f"{waiting} waiting · KV {round(kv * 100)}%" if kv is not None else f"{waiting} waiting"},
        "facts": [f for f in [
            _fact("Model", first.get("id")),
            _fact("Max context", f"{first['max_model_len']:,}") if first.get("max_model_len") else None,
            _fact("Requests running / waiting", f"{running} / {waiting}"),
            _fact("KV cache used", f"{round(kv * 100, 1)}%") if kv is not None else None,
            _fact("Prefix cache hit rate", f"{round(100 * hits / queries, 1)}%") if queries else None,
            _fact("Avg time to first token", f"{ttft:.2f}s") if ttft else None,
            _fact("Avg decode speed", f"{1 / itl:.1f} tok/s") if itl else None,
            _fact("Tokens in / out (total)", f"{int(m.get('vllm:prompt_tokens_total', 0)):,} / {int(m.get('vllm:generation_tokens_total', 0)):,}") if m else None,
            _fact("Version", (ver or {}).get("version")) if isinstance(ver, dict) else None,
        ] if f and f["value"] not in (None, "")],
        "note": None if m else "/metrics did not answer, so only the model list is shown.",
    }


def _live_sglang(port):
    # sglang docs/references/production_metrics.mdx: /metrics needs
    # --enable-metrics. /model_info replaced /get_model_info.
    served = _openai_served(port)
    _, text = _http_get(port, "/metrics", text=True)
    m = _prom(text)
    st, info = _http_get(port, "/model_info")
    if st != 200:
        st, info = _http_get(port, "/get_model_info")
    if served is None and not m and not isinstance(info, dict):
        return None
    info = info if isinstance(info, dict) else {}
    running = int(m.get("sglang:num_running_reqs", 0))
    tput = m.get("sglang:gen_throughput")
    usage = m.get("sglang:token_usage")
    first = (served or [{}])[0]
    return {
        "served": [s["id"] for s in served or []],
        "pulse": ({"value": f"{tput:.1f}", "unit": "tok/s", "sub": f"{running} running · {int(m.get('sglang:num_queue_reqs', 0))} queued"}
                  if m else {"value": "Running", "sub": info.get("served_model_name") or first.get("id") or ""}),
        "facts": [f for f in [
            _fact("Model", info.get("served_model_name") or info.get("model_path") or first.get("id")),
            _fact("Max context", f"{first['max_model_len']:,}") if first.get("max_model_len") else None,
            _fact("Generation throughput", f"{tput:.1f} tok/s") if tput is not None else None,
            _fact("Requests running / queued", f"{running} / {int(m.get('sglang:num_queue_reqs', 0))}") if m else None,
            _fact("KV cache used", f"{round(usage * 100, 1)}%") if usage is not None else None,
            _fact("Prefix cache hit rate", f"{round(m['sglang:cache_hit_rate'] * 100, 1)}%") if "sglang:cache_hit_rate" in m else None,
            _fact("Avg time to first token", f"{_avg(m, 'sglang:time_to_first_token_seconds'):.2f}s") if _avg(m, "sglang:time_to_first_token_seconds") else None,
        ] if f and f["value"] not in (None, "")],
        "note": None if m else "Start SGLang with --enable-metrics for throughput, queue and KV cache figures.",
    }


def _live_llamacpp(port):
    # tools/server/README.md: /props always; /slots on unless --no-slots;
    # /metrics only with --metrics.
    st, props = _http_get(port, "/props")
    if st != 200 or not isinstance(props, dict):
        return None
    gen = props.get("default_generation_settings") or {}
    model_path = props.get("model_path") or gen.get("model") or ""
    model = str(model_path).split("/")[-1] or None
    _, slots = _http_get(port, "/slots")
    slots = slots if isinstance(slots, list) else None
    busy = sum(1 for s in slots or [] if isinstance(s, dict) and s.get("is_processing"))
    _, text = _http_get(port, "/metrics", text=True)
    m = _prom(text)
    decode = m.get("llamacpp:predicted_tokens_seconds")
    prefill = m.get("llamacpp:prompt_tokens_seconds")
    total = props.get("total_slots") or len(slots or []) or gen.get("n_parallel") or 0
    notes = []
    if slots is None:
        notes.append("/slots is off (--no-slots)")
    if not m:
        notes.append("start llama-server with --metrics for speed and queue figures")
    return {
        "context": gen.get("n_ctx", 0), "model": model, "slots": total, "chat_format": props.get("chat_format"),
        "pulse": ({"value": f"{decode:.1f}", "unit": "tok/s", "sub": f"{busy}/{total} slots busy · {model or ''}"}
                  if decode else {"value": f"{busy}/{total}", "unit": "slots busy", "sub": model or ""}),
        "facts": [f for f in [
            _fact("Model", model),
            _fact("Context per slot", f"{gen.get('n_ctx'):,}") if gen.get("n_ctx") else None,
            _fact("Slots busy", f"{busy} of {total}") if slots is not None else None,
            _fact("Avg decode speed", f"{decode:.1f} tok/s") if decode else None,
            _fact("Avg prefill speed", f"{prefill:.1f} tok/s") if prefill else None,
            _fact("Requests processing / deferred", f"{int(m.get('llamacpp:requests_processing', 0))} / {int(m.get('llamacpp:requests_deferred', 0))}") if m else None,
            _fact("Tokens in / out (total)", f"{int(m.get('llamacpp:prompt_tokens_total', 0)):,} / {int(m.get('llamacpp:tokens_predicted_total', 0)):,}") if m else None,
            _fact("Build", (props.get("build_info") or "")[:40] or None),
        ] if f and f["value"] not in (None, "")],
        "note": ("Tip: " + "; ".join(notes) + ".") if notes else None,
    }


def _live_kobold(port):
    # koboldcpp.py: /api/extra/perf, /api/extra/version, /api/v1/model and
    # /api/extra/true_max_context_length need no password.
    st, perf = _http_get(port, "/api/extra/perf")
    if st != 200 or not isinstance(perf, dict):
        return None
    _, model = _http_get(port, "/api/v1/model")
    _, ver = _http_get(port, "/api/extra/version")
    _, ctx = _http_get(port, "/api/extra/true_max_context_length")
    name = (model or {}).get("result") if isinstance(model, dict) else None
    speed = perf.get("last_eval_speed")
    return {
        "served": [name] if name else [],
        "pulse": {"value": f"{speed:.1f}" if speed else ("Idle" if perf.get("idle") else "Busy"),
                  "unit": "tok/s last" if speed else "", "sub": f"queue {perf.get('queue', 0)} · {perf.get('total_gens', 0)} generations"},
        "facts": [f for f in [
            _fact("Model", name),
            _fact("Max context", f"{ctx['value']:,}") if isinstance(ctx, dict) and ctx.get("value") else None,
            _fact("State", "idle" if perf.get("idle") else "generating"),
            _fact("Queue", perf.get("queue")),
            _fact("Last decode speed", f"{speed:.1f} tok/s") if speed else None,
            _fact("Last prefill speed", f"{perf.get('last_process_speed'):.1f} tok/s") if perf.get("last_process_speed") else None,
            _fact("Last request tokens in / out", f"{perf.get('last_input_count', 0)} / {perf.get('last_token_count', 0)}"),
            _fact("Uptime", f"{round((perf.get('uptime') or 0) / 60)} min") if perf.get("uptime") else None,
            _fact("Version", (ver or {}).get("version")) if isinstance(ver, dict) else None,
        ] if f and f["value"] not in (None, "")],
        "note": None,
    }


def _live_textgen(port):
    # modules/api/models.py: /v1/internal/model/info -> model_name, lora_names, loader.
    st, info = _http_get(port, "/v1/internal/model/info")
    if st != 200 or not isinstance(info, dict) or "model_name" not in info:
        return None
    name = info.get("model_name")
    return {
        "served": [name] if name and name != "None" else [],
        "pulse": {"value": "Loaded" if name and name != "None" else "No model", "sub": name or ""},
        "facts": [_fact("Model", name), _fact("Loader", info.get("loader") or "–"),
                  _fact("LoRAs", ", ".join(info.get("lora_names") or []) or "none")],
        "note": "text-generation-webui publishes no speed or queue figures.",
    }


def _live_localai(port):
    # core/schema/localai.go SystemInformationResponse: loaded_models[] with
    # process pid, rss_bytes, memory_percent, cpu_percent.
    # /system is LocalAI's own; /v1/models alone would also match an MLX
    # server on the same default port.
    st, sysinfo = _http_get(port, "/system")
    if st != 200 or not isinstance(sysinfo, dict) or not ({"backends", "loaded_models"} & set(sysinfo)):
        return None
    served = _openai_served(port)
    loaded = (sysinfo or {}).get("loaded_models") or [] if isinstance(sysinfo, dict) else []
    _, ver = _http_get(port, "/version")
    rss = sum(((m.get("process") or {}).get("rss_bytes") or 0) for m in loaded if isinstance(m, dict))
    return {
        "served": [s["id"] for s in served or []],
        "loaded": [{"name": m.get("id"), "context": 0} for m in loaded if isinstance(m, dict)],
        "pulse": {"value": escape_count(len(loaded), "model") + " loaded", "sub": f"{rss / 1024**3:.1f} GB resident" if rss else ""},
        "facts": [f for f in [
            _fact("Loaded", ", ".join(f"{m.get('id')} ({m.get('backend')})" for m in loaded if isinstance(m, dict)) or "none"),
            _fact("Memory in use", f"{rss / 1024**3:.2f} GB") if rss else None,
            _fact("CPU", ", ".join(f"{round((m.get('process') or {}).get('cpu_percent') or 0)}%" for m in loaded if isinstance(m, dict))) if loaded else None,
            _fact("Backends installed", len((sysinfo or {}).get("backends") or [])) if isinstance(sysinfo, dict) else None,
            _fact("Version", (ver or {}).get("version")) if isinstance(ver, dict) else None,
        ] if f and f["value"] not in (None, "")],
        "note": "LocalAI publishes no token speed; memory and CPU are per model process.",
    }


def _live_jan(port):
    # src-tauri/src/core/server/proxy.rs: the API key is on by default and
    # only /openapi.json and a few static paths skip it.
    served = _openai_served(port)
    st, spec = _http_get(port, "/openapi.json")
    if served is None and st != 200:
        return None
    if served is None:
        return {"pulse": {"value": "Running", "sub": "API key required"},
                "facts": [_fact("Models", "hidden behind the API key")],
                "note": "Jan's local API server has a key on by default. Clear the key in Jan's settings to let Tach list models."}
    return {"served": [s["id"] for s in served],
            "pulse": {"value": escape_count(len(served), "model"), "sub": ", ".join(s["id"] for s in served[:2])},
            "facts": [_fact("Models", ", ".join(s["id"] for s in served) or "none"),
                      _fact("Backends", ", ".join(sorted({s.get("owned_by") for s in served if s.get("owned_by")})) or "–")],
            "note": "Jan publishes no speed or queue figures."}


def _live_cortex(port):
    # janhq/cortex.cpp (archived 2025): /v1/models, /v1/models/status/{id},
    # /v1/hardware.
    served = _openai_served(port)
    if served is None:
        return None
    running = []
    for s in served[:12]:
        st, _ = _http_get(port, f"/v1/models/status/{quote(s['id'], safe='')}", timeout=0.4)
        if st == 200:
            running.append(s["id"])
    _, hw = _http_get(port, "/v1/hardware")
    ram = (hw or {}).get("ram") if isinstance(hw, dict) else None
    return {"served": [s["id"] for s in served],
            "loaded": [{"name": n, "context": 0} for n in running],
            "pulse": {"value": escape_count(len(running), "model") + " running", "sub": ", ".join(running[:2])},
            "facts": [f for f in [
                _fact("Running", ", ".join(running) or "none"),
                _fact("Installed", len(served)),
                _fact("RAM available", f"{(ram.get('available') or 0) / 1024:.1f} GB") if isinstance(ram, dict) and ram.get("available") else None,
            ] if f],
            "note": "Cortex is archived upstream and publishes no speed figures."}


def _live_tabby(port):
    # tabbyAPI endpoints/core/router.py: /health is open; /v1/model needs a key
    # (auth is on by default).
    st, health = _http_get(port, "/health")
    if not isinstance(health, dict) or health.get("status") not in ("healthy", "unhealthy"):
        return None
    st, card = _http_get(port, "/v1/model")
    if st == 200 and isinstance(card, dict):
        p = card.get("parameters") or {}
        return {"served": [card.get("id")],
                "pulse": {"value": "Loaded", "sub": card.get("id") or ""},
                "facts": [f for f in [
                    _fact("Model", card.get("id")),
                    _fact("Max sequence", f"{p['max_seq_len']:,}") if p.get("max_seq_len") else None,
                    _fact("Cache", f"{p.get('cache_size') or ''} {p.get('cache_mode') or ''}".strip() or None),
                    _fact("Draft model", ((p.get("draft") or {}).get("draft_model_name")) if isinstance(p.get("draft"), dict) else None),
                ] if f and f["value"]],
                "note": None}
    issues = health.get("issues") or []
    return {"pulse": {"value": health["status"].capitalize(), "sub": "API key required for model details"},
            "facts": [_fact("Health", health["status"]), _fact("Issues", len(issues))],
            "note": "TabbyAPI needs an API key for model details; auth is on by default."}


def _live_openwebui(port):
    # backend/open_webui/main.py: /api/version and /api/config are public.
    st, ver = _http_get(port, "/api/version")
    if st != 200 or not isinstance(ver, dict):
        return None
    _, cfg = _http_get(port, "/api/config")
    feats = (cfg or {}).get("features") or {} if isinstance(cfg, dict) else {}
    return {"pulse": {"value": "Running", "sub": f"v{ver.get('version')}"},
            "facts": [f for f in [_fact("Version", ver.get("version")),
                                  _fact("Name", (cfg or {}).get("name")) if isinstance(cfg, dict) else None,
                                  _fact("Sign-up open", "yes" if feats.get("enable_signup") else "no") if feats else None] if f and f["value"] is not None],
            "note": "Open WebUI is a front end with no inference telemetry of its own. Its chats are read from webui.db under Data sources."}


def escape_count(n, noun):
    return f"{n} {noun}{'' if n == 1 else 's'}"


_ENGINE_READERS = {
    "lmstudio": (_live_lmstudio, 1234),
    "vllm": (_live_vllm, 8000),
    "sglang": (_live_sglang, 30000),
    "koboldcpp": (_live_kobold, 5001),
    "textgenwebui": (_live_textgen, 5000),
    "localai": (_live_localai, 8080),
    "jan_server": (_live_jan, 1337),
    "cortex": (_live_cortex, 39281),
    "tabbyapi": (_live_tabby, 5000),
    "openwebui_srv": (_live_openwebui, 3000),
}


def _confirmed_ports(eid):
    """
    Ports the catalog scan has already fingerprinted as this engine. The live
    poll runs every few seconds, so it only reads confirmed ports; probing a
    default port that some unrelated dev server holds would stall every poll.
    """
    for srv in get_catalog()["servers"]:
        if srv["id"] == eid:
            return [i["port"] for i in srv.get("instances") or [] if i.get("online") and i.get("port")]
    return []


def read_engine_live(eid):
    reader, _default = _ENGINE_READERS[eid]
    for port in _confirmed_ports(eid):
        try:
            det = reader(port)
        except Exception:
            det = None
        if det:
            return {"online": True, "port": port, "details": det}
    return None


def _read_mlx(port):
    """One MLX server's /metrics, shaped for the live panels; None if it is not one."""
    out = {"online": True, "port": port, "details": None, "requests": [], "apc": None, "summary": None, "last_at": 0.0}
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/metrics", headers={"User-Agent": "Telemetry"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode())
            if not _looks_like("mlx", data):
                return None
            recent_list = data.get("recent") or []
            last_req = data.get("latest") or {}
            summary = data.get("summary") or {}
            server_rt = data.get("server") or {}
            apc = server_rt.get("apc") or {}

            out["details"] = {
                "model": server_rt.get("loaded_model") or server_rt.get("language_model") or last_req.get("model") or "Loaded",
                "decode_tok_s": round(float(last_req.get("decode_tok_s") or summary.get("avg_decode_tok_s") or 0.0), 1),
                "prefill_tok_s": round(float(last_req.get("prefill_tok_s") or summary.get("avg_request_tok_s") or 0.0), 1),
                "ttft_s": round(float(last_req.get("ttft_s") or 0.0), 2),
                "peak_memory_gb": round(float(last_req.get("peak_memory_gb") or 0.0), 1),
                "in_flight": summary.get("in_flight", 0),
                "uptime_s": round(summary.get("uptime_s", 0)),
                "prompt_tokens_total": summary.get("prompt_tokens_total", 0),
                "completion_tokens_total": summary.get("completion_tokens_total", 0),
                "sliding_first_32": round(float(last_req.get("sliding_decode_tok_s_first_32") or 0.0), 1),
                "sliding_last_32": round(float(last_req.get("sliding_decode_tok_s_last_32") or 0.0), 1),
            }
            out["apc"] = {
                "enabled": apc.get("enabled", False),
                "hit_rate": round(float(apc.get("token_hit_rate", 0.0)) * 100, 1),
                "matched_tokens": apc.get("matched_tokens", 0),
                "exact_hits": apc.get("exact_hits", 0),
                "lookups_hit": apc.get("lookups_hit", 0),
                "lookups_miss": apc.get("lookups_miss", 0),
            }
            out["summary"] = {
                "avg_decode_tok_s": round(float(summary.get("avg_decode_tok_s") or 0.0), 1),
                "avg_request_tok_s": round(float(summary.get("avg_request_tok_s") or 0.0), 1),
                "requests_completed": summary.get("requests_completed", 0),
                "requests_failed": summary.get("requests_failed", 0),
                "last_error": summary.get("last_error"),
            }
            cleaned_reqs = []
            if isinstance(recent_list, list):
                for r in reversed(recent_list[-20:]):
                    cleaned_reqs.append({
                        "timestamp": r.get("timestamp_unix", 0),
                        "model": r.get("model", "").split("/")[-1],
                        "prompt_tokens": r.get("prompt_tokens", 0),
                        "completion_tokens": r.get("completion_tokens", 0),
                        "prefill_tok_s": round(float(r.get("prefill_tok_s") or 0.0), 1),
                        "decode_tok_s": round(float(r.get("decode_tok_s") or 0.0), 1),
                        "ttft_s": round(float(r.get("ttft_s") or 0.0), 2),
                        "peak_memory_gb": round(float(r.get("peak_memory_gb") or 0.0), 1),
                        "sliding_first_32": round(float(r.get("sliding_decode_tok_s_first_32") or 0.0), 1),
                        "sliding_last_32": round(float(r.get("sliding_decode_tok_s_last_32") or 0.0), 1),
                        "finish_reason": r.get("finish_reason", "stop"),
                        "tool_calls": r.get("tool_calls", False),
                    })
            out["requests"] = cleaned_reqs
        out["last_at"] = float(last_req.get("timestamp_unix") or 0.0)
    except Exception:
        return None
    return out


def check_live_status():
    status = {
        "mlx": {"online": False, "details": None, "requests": [], "apc": None, "summary": None},
        "ollama": {"online": False, "details": None},
        "llamacpp": {"online": False, "details": None},
        "openclaw": {"online": False, "details": None},
    }

    # MLX: every port it was found on. With several instances up, the panels
    # follow the one that served a request most recently.
    mlx_found = [m for m in (_read_mlx(p) for p in engine_ports("mlx", 8080)) if m]
    if mlx_found:
        primary = max(mlx_found, key=lambda m: m["last_at"])
        status["mlx"] = {k: v for k, v in primary.items() if k != "last_at"}
        status["mlx"]["instances"] = [
            {"port": m["port"], "model": (m["details"] or {}).get("model"),
             "in_flight": (m["details"] or {}).get("in_flight", 0)}
            for m in mlx_found
        ]

    # Check Ollama (:11434 unless found elsewhere)
    ollama_port = next(iter(engine_ports("ollama", 11434)), 11434)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{ollama_port}/api/ps", headers={"User-Agent": "Telemetry"})
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            data = json.loads(resp.read().decode())
            status["ollama"]["online"] = True
            models = data.get("models", [])
            active_model = models[0] if models else None
            status["ollama"]["details"] = {
                "active_model": active_model.get("name") if active_model else "None running",
                "size_vram_gb": round(active_model.get("size_vram", 0) / (1024**3), 2) if active_model else 0,
                "context_length": active_model.get("context_length") if active_model else 0,
                "count": len(models),
                "loaded": [
                    {
                        "name": m.get("name"),
                        "vram_gb": round(m.get("size_vram", 0) / (1024**3), 2),
                        "context": m.get("context_length") or 0,
                        "expires_at": m.get("expires_at"),
                    }
                    for m in models
                ],
            }
    except Exception:
        pass

    # Ollama again, for what is installed rather than what is resident. This is
    # the inventory the Engines page shows: family, parameter size, quantisation
    # and the context each model was built with.
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{ollama_port}/api/tags", headers={"User-Agent": "Telemetry"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode())
            installed = []
            for m in (data.get("models") or []):
                det = m.get("details") or {}
                installed.append({
                    "name": m.get("name"),
                    "size_gb": round((m.get("size") or 0) / (1024**3), 2),
                    "family": det.get("family"),
                    "parameters": det.get("parameter_size"),
                    "quantization": det.get("quantization_level"),
                    "context": det.get("context_length") or 0,
                    "modified": m.get("modified_at"),
                })
            installed.sort(key=lambda x: -(x["size_gb"] or 0))
            status["ollama"]["online"] = True
            det = status["ollama"].get("details") or {}
            det["installed"] = installed
            det["installed_count"] = len(installed)
            status["ollama"]["details"] = det
    except Exception:
        pass

    # Every other engine, each from the endpoints it documents.
    for p in _confirmed_ports("llamacpp"):
        llama = _live_llamacpp(p)
        if llama:
            status["llamacpp"] = {"online": True, "port": p, "details": llama}
            break
    with ThreadPoolExecutor(max_workers=6) as pool:
        for eid, got in zip(_ENGINE_READERS, pool.map(read_engine_live, _ENGINE_READERS)):
            if got:
                status[eid] = got

    # Check the OpenClaw gateway (port from openclaw.json, default :18789).
    # The gateway requires a bearer token, so a 401/403 still proves it is up;
    # a raw TCP connect is the fallback when no HTTP route answers.
    port = resolve_openclaw_gateway()
    gw_online = False
    gw_detail = None
    for route in ("/health", "/healthz", "/status"):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{route}", headers={"User-Agent": "Telemetry"}
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                gw_online = True
                try:
                    gw_detail = json.loads(resp.read().decode())
                except Exception:
                    gw_detail = None
                break
        except urllib.error.HTTPError:
            # Answered, but refused us - the gateway is listening.
            gw_online = True
            break
        except Exception:
            continue

    if not gw_online:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.8):
                gw_online = True
        except Exception:
            pass

    status["openclaw"]["online"] = gw_online
    detail = {
        "port": port,
        "homes": [sanitize_path(str(h)) for h in CONFIG.get("openclaw_homes", [])],
        "sessions": len(scan_openclaw_session_files()),
    }
    if isinstance(gw_detail, dict):
        detail["version"] = gw_detail.get("version") or gw_detail.get("gatewayVersion")
        detail["uptime_s"] = gw_detail.get("uptime") or gw_detail.get("uptimeSeconds")
    status["openclaw"]["details"] = detail

    hermes = read_hermes_live()
    if hermes:
        status["hermes_gw"] = hermes

    return status


def count_opencode_sessions(start_ms=None, end_ms=None):
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        c = conn.cursor()
        if start_ms or end_ms:
            c.execute("SELECT COUNT(*) FROM session WHERE time_created >= ? AND time_created <= ?",
                      (start_ms or 0, end_ms or 1 << 62))
        else:
            c.execute("SELECT COUNT(*) FROM session")
        return c.fetchone()[0] or 0
    except Exception:
        return 0
    finally:
        conn.close()


def get_harness_inventory(query_params=None):
    """
    Per-source session counts for the sidebar, computed across every harness
    regardless of the harness filter (picking one source must not zero the
    others) but within the time window, so the counts match what the page
    shows. Detection does not depend on the window; the sidebar hides only
    sources with no artifacts on this machine at all.
    """
    start_ms, end_ms = resolve_window(query_params)

    def n(rows):
        if not (start_ms or end_ms):
            return len(rows)
        return sum(1 for r in rows
                   if (not start_ms or (r.get("time_created") or 0) >= start_ms)
                   and (not end_ms or (r.get("time_created") or 0) <= end_ms))

    def files(paths):
        if not (start_ms or end_ms):
            return paths
        out = []
        for p in paths:
            try:
                m = int(p.stat().st_mtime * 1000)
            except OSError:
                continue
            if (not start_ms or m >= start_ms) and (not end_ms or m <= end_ms):
                out.append(p)
        return out

    opencode_n = count_opencode_sessions(start_ms, end_ms)
    openclaw_n = n(get_openclaw_sessions())
    hermes_n = n(get_hermes_sessions())
    cline_n = n(get_cline_sessions())
    roo_n = n(get_roo_sessions())
    zed_n = n(get_zed_sessions())
    goose_n = n(get_goose_sessions())
    lmstudio_n = n(get_lmstudio_sessions())
    jan_n = n(get_jan_sessions())
    crush_n = n(get_crush_sessions())
    allm_n = n(get_anythingllm_sessions())
    owui_n = n(get_openwebui_sessions())
    aider_files = files(scan_aider_history())
    continue_files = files(scan_continue_sessions())

    catalog = {c["id"]: c for c in get_catalog()["sources"]}

    rows = [
        {"id": "all", "name": "All Sources", "icon": "globe", "detected": True,
         "count": (opencode_n + openclaw_n + hermes_n + cline_n + roo_n + zed_n
                   + goose_n + lmstudio_n + jan_n + crush_n + allm_n + owui_n + len(aider_files) + len(continue_files))},
        {"id": "opencode", "name": "OpenCode", "icon": "opencode",
         "detected": catalog.get("opencode", {}).get("detected", False),
         "path": sanitize_path(str(CONFIG["opencode_db"])), "count": opencode_n},
        {"id": "hermes", "name": "Hermes Agent", "icon": "hermes",
         "detected": len(hermes_state_dbs()) > 0,
         "path": sanitize_path(str(hermes_state_dbs()[0])) if hermes_state_dbs() else None,
         "count": hermes_n},
        {"id": "openclaw", "name": "OpenClaw", "icon": "paw",
         "detected": len(CONFIG["openclaw_homes"]) > 0,
         "path": sanitize_path(str(CONFIG["openclaw_homes"][0])) if CONFIG["openclaw_homes"] else None,
         "count": openclaw_n},
        {"id": "cline", "name": "Cline", "icon": "cline",
         "detected": cline_n > 0 or catalog.get("cline", {}).get("detected", False),
         "path": catalog.get("cline", {}).get("path"), "count": cline_n},
        {"id": "roo", "name": "Roo Code", "icon": "rabbit",
         "detected": roo_n > 0 or catalog.get("roo", {}).get("detected", False),
         "path": catalog.get("roo", {}).get("path"), "count": roo_n},
        {"id": "zed", "name": "Zed", "icon": "zed",
         "detected": zed_n > 0 or catalog.get("zed", {}).get("detected", False),
         "path": catalog.get("zed", {}).get("path"), "count": zed_n},
        {"id": "goose", "name": "Goose", "icon": "bird",
         "detected": goose_n > 0 or catalog.get("goose", {}).get("detected", False),
         "path": catalog.get("goose", {}).get("path"), "count": goose_n},
        {"id": "lmstudio", "name": "LM Studio", "icon": "lmstudio",
         "detected": lmstudio_n > 0 or catalog.get("lmstudio_chat", {}).get("detected", False),
         "path": catalog.get("lmstudio_chat", {}).get("path"), "count": lmstudio_n},
        {"id": "jan", "name": "Jan", "icon": "atom",
         "detected": jan_n > 0 or catalog.get("jan", {}).get("detected", False),
         "path": catalog.get("jan", {}).get("path"), "count": jan_n},
        {"id": "crush", "name": "Crush", "icon": "shapes",
         "detected": crush_n > 0 or bool(crush_dbs()),
         "path": sanitize_path(str(crush_dbs()[0])) if crush_dbs() else None, "count": crush_n},
        {"id": "anythingllm", "name": "AnythingLLM", "icon": "libraryBig",
         "detected": allm_n > 0 or bool(anythingllm_dbs()),
         "path": sanitize_path(str(anythingllm_dbs()[0])) if anythingllm_dbs() else None, "count": allm_n},
        {"id": "openwebui", "name": "Open WebUI", "icon": "globe",
         "detected": owui_n > 0 or bool(openwebui_dbs()),
         "path": sanitize_path(str(openwebui_dbs()[0])) if openwebui_dbs() else None, "count": owui_n},
        {"id": "aider", "name": "Aider", "icon": "squareTerminal",
         "detected": bool(scan_aider_history()), "count": len(aider_files)},
        {"id": "continue", "name": "Continue", "icon": "arrows",
         "detected": bool(scan_continue_sessions()), "count": len(continue_files)},
    ]

    # Anything else the catalog found on disk is surfaced as detected but not
    # yet readable, rather than silently omitted.
    for c in get_catalog()["sources"]:
        if c["id"] in {r["id"] for r in rows} or not c["detected"] or c["readable"]:
            continue
        rows.append({
            "id": c["id"], "name": c["name"], "icon": c["icon"],
            "detected": True, "readable": False, "path": c["path"], "count": 0, "reason": c.get("reason"),
        })

    return rows


WINDOW_MS = {
    "10m": 10 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "6h": 6 * 60 * 60 * 1000,
    "12h": 12 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
    "3d": 3 * 24 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
    "14d": 14 * 24 * 60 * 60 * 1000,
    "30d": 30 * 24 * 60 * 60 * 1000,
    "90d": 90 * 24 * 60 * 60 * 1000,
}


def resolve_window(query_params):
    """Shared time-window resolution: a named window, or an explicit range."""
    if not query_params:
        return None, None

    now_ms = int(time.time() * 1000)
    window = query_params.get("window", [""])[0]
    if window:
        delta = WINDOW_MS.get(window)
        return (now_ms - delta, now_ms) if delta else (None, None)

    # The custom picker is a datetime-local input (YYYY-MM-DDTHH:MM); a bare
    # date is also accepted and then covers the whole day.
    def parse(value, end_of_day):
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if len(value) <= 10 and end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        return int(dt.timestamp() * 1000)

    start_ms = parse(query_params.get("from", [""])[0], False)
    end_ms = parse(query_params.get("to", [""])[0], True)
    # Callers test "start and end" or "start only"; filling the open side of a
    # one-sided range keeps "until X" from being silently ignored.
    if start_ms or end_ms:
        start_ms = start_ms or 1
        end_ms = end_ms or now_ms + 365 * 24 * 60 * 60 * 1000
    return start_ms, end_ms


def get_all_stats(query_params=None):
    harness_filter = query_params.get("harness", [""])[0].lower() if query_params else ""

    start_ms, end_ms = resolve_window(query_params)

    # If a specific external harness is selected (e.g. Aider, OpenClaw, Continue)
    if harness_filter and harness_filter not in ["opencode", "all"]:
        harness_sessions = []
        if harness_filter == "openclaw":
            harness_sessions = get_openclaw_sessions()
        elif harness_filter == "hermes":
            harness_sessions = get_hermes_sessions()
        elif harness_filter == "cline":
            harness_sessions = get_cline_sessions()
        elif harness_filter == "roo":
            harness_sessions = get_roo_sessions()
        elif harness_filter == "zed":
            harness_sessions = get_zed_sessions()
        elif harness_filter == "crush":
            harness_sessions = get_crush_sessions()
        elif harness_filter == "anythingllm":
            harness_sessions = get_anythingllm_sessions()
        elif harness_filter == "openwebui":
            harness_sessions = get_openwebui_sessions()
        elif harness_filter == "goose":
            harness_sessions = get_goose_sessions()
        elif harness_filter == "lmstudio":
            harness_sessions = get_lmstudio_sessions()
        elif harness_filter == "jan":
            harness_sessions = get_jan_sessions()
        elif harness_filter == "aider":
            harness_sessions = get_aider_sessions()
        elif harness_filter == "continue":
            harness_sessions = get_continue_sessions()

        if start_ms or end_ms:
            filtered = []
            for s in harness_sessions:
                tc = s.get("time_created", 0)
                if start_ms and tc < start_ms:
                    continue
                if end_ms and tc > end_ms:
                    continue
                filtered.append(s)
            harness_sessions = filtered

        tot_s = len(harness_sessions)
        tot_in = sum(s.get("tokens_input", 0) for s in harness_sessions)
        tot_out = sum(s.get("tokens_output", 0) for s in harness_sessions)
        tot_reas = sum(s.get("tokens_reasoning", 0) for s in harness_sessions)
        tot_msg = sum(s.get("message_count", 0) for s in harness_sessions)
        tps_list = [s.get("tps", 0) for s in harness_sessions if s.get("tps", 0) > 0]
        avg_tps = round(sum(tps_list) / len(tps_list), 1) if tps_list else 0.0
        peak_tps = max(tps_list) if tps_list else 0.0

        m_counts = {}
        for s in harness_sessions:
            m = s.get("model", "unknown")
            m_counts[m] = m_counts.get(m, 0) + 1
        model_shares = [{
            "name": k,
            "session_count": v,
            "tokens_input": 0,
            "tokens_output": 0,
            "tokens_reasoning": 0,
            "total_tokens": 0
        } for k, v in m_counts.items()]

        dir_counts = {}
        for s in harness_sessions:
            f = s.get("folder", "root")
            dir_counts[f] = dir_counts.get(f, 0) + 1
        directories = [{
            "path": k,
            "folder": k,
            "count": v,
            "tokens_output": 0
        } for k, v in dir_counts.items()]

        return {
            "total_sessions": tot_s,
            "total_messages": tot_msg,
            "tokens_input": tot_in,
            "tokens_output": tot_out,
            "tokens_reasoning": tot_reas,
            "tokens_total": tot_in + tot_out + tot_reas,
            "avg_decode_tps": avg_tps,
            "peak_decode_tps": peak_tps,
            "total_generation_seconds": 0.0,
            "models": m_counts,
            "model_shares": model_shares,
            "tool_stats": [],
            "tps_buckets": {"< 15": 0, "15 - 30": 0, "30 - 45": 0, "45 - 60": 0, "60+": 0},
            "duration_buckets": {"< 1 min": 0, "1 - 5 mins": 0, "5 - 15 mins": 0, "15 - 30 mins": 0, "> 30 mins": 0},
            "directories": directories,
            "harnesses": get_harness_inventory(query_params),
            "live": check_live_status(),
            "system_info": {
                "opencode_db": sanitize_path(str(CONFIG["opencode_db"])),
                "openclaw_db": sanitize_path(str(CONFIG["openclaw_db"])) if CONFIG["openclaw_db"] else None,
                "openclaw_homes": [sanitize_path(str(h)) for h in CONFIG["openclaw_homes"]],
            }
        }

    # OpenCode SQLite DB stats (unified or opencode selected)
    conn = get_db_connection()
    if not conn:
        return {"error": "Database not found", "db_path": sanitize_path(str(CONFIG["opencode_db"]))}

    c = conn.cursor()

    where_sess = ""
    sess_params = []
    if start_ms and end_ms:
        where_sess = "WHERE time_created >= ? AND time_created <= ?"
        sess_params = [start_ms, end_ms]
    elif start_ms:
        where_sess = "WHERE time_created >= ?"
        sess_params = [start_ms]

    c.execute(f"SELECT COUNT(*) FROM session {where_sess};", sess_params)
    total_sessions = c.fetchone()[0]

    # 19 of 53 sessions are subagent children; counting them as sessions
    # inflates every per-session figure.
    joiner = "AND" if where_sess else "WHERE"
    c.execute(f"SELECT COUNT(*) FROM session {where_sess} {joiner} parent_id IS NULL;", sess_params)
    sessions_root = c.fetchone()[0]
    sessions_subagent = total_sessions - sessions_root

    # A trend line over 10 active days spread across 88 calendar days should
    # say so rather than draw a confident curve.
    c.execute(f"SELECT COUNT(DISTINCT date(time_created/1000, 'unixepoch')) FROM session {where_sess};", sess_params)
    active_days = c.fetchone()[0]

    # Filter messages strictly by the sessions within the time window
    if start_ms and end_ms:
        c.execute("""
            SELECT COUNT(*) FROM message 
            WHERE session_id IN (SELECT id FROM session WHERE time_created >= ? AND time_created <= ?);
        """, (start_ms, end_ms))
    elif start_ms:
        c.execute("""
            SELECT COUNT(*) FROM message 
            WHERE session_id IN (SELECT id FROM session WHERE time_created >= ?);
        """, (start_ms,))
    else:
        c.execute("SELECT COUNT(*) FROM message;")
    total_messages = c.fetchone()[0]

    c.execute(f"""
        SELECT 
            COALESCE(SUM(tokens_input), 0),
            COALESCE(SUM(tokens_output), 0),
            COALESCE(SUM(tokens_reasoning), 0)
        FROM session {where_sess};
    """, sess_params)
    sum_in, sum_out, sum_reas = c.fetchone()

    # Filter assistant messages strictly within the time window
    if start_ms and end_ms:
        c.execute("""
            SELECT data FROM message 
            WHERE session_id IN (SELECT id FROM session WHERE time_created >= ? AND time_created <= ?)
              AND data LIKE '%assistant%';
        """, (start_ms, end_ms))
    elif start_ms:
        c.execute("""
            SELECT data FROM message 
            WHERE session_id IN (SELECT id FROM session WHERE time_created >= ?)
              AND data LIKE '%assistant%';
        """, (start_ms,))
    else:
        c.execute("SELECT data FROM message WHERE data LIKE '%assistant%';")
    messages_data = c.fetchall()

    tps_samples = []
    total_assistant_gen_s = 0.0
    total_assistant_out_tokens = 0
    tps_buckets = {"< 15": 0, "15 - 30": 0, "30 - 45": 0, "45 - 60": 0, "60+": 0}

    for (raw_data,) in messages_data:
        try:
            d = json.loads(raw_data)
            if d.get("role") != "assistant":
                continue
            t = d.get("time", {})
            created = t.get("created")
            completed = t.get("completed")
            if not created or not completed or completed <= created:
                continue
            dur = (completed - created) / 1000.0
            toks = d.get("tokens", {})
            out_tok = toks.get("output", 0) + toks.get("reasoning", 0)
            if out_tok > 0 and dur > 0.05:
                tps = out_tok / dur
                if tps < 250:
                    tps_samples.append(tps)
                    total_assistant_gen_s += dur
                    total_assistant_out_tokens += out_tok

                    if tps < 15:
                        tps_buckets["< 15"] += 1
                    elif tps < 30:
                        tps_buckets["15 - 30"] += 1
                    elif tps < 45:
                        tps_buckets["30 - 45"] += 1
                    elif tps < 60:
                        tps_buckets["45 - 60"] += 1
                    else:
                        tps_buckets["60+"] += 1
        except Exception:
            continue

    avg_tps = (total_assistant_out_tokens / total_assistant_gen_s) if total_assistant_gen_s > 0 else 0.0
    peak_tps = max(tps_samples) if tps_samples else 0.0

    where_model_clause = "WHERE model IS NOT NULL"
    model_params = []
    if start_ms and end_ms:
        where_model_clause += " AND time_created >= ? AND time_created <= ?"
        model_params = [start_ms, end_ms]
    elif start_ms:
        where_model_clause += " AND time_created >= ?"
        model_params = [start_ms]

    c.execute(f"""
        SELECT 
            COALESCE(json_extract(model, '$.providerID'), 'local') || '/' || COALESCE(json_extract(model, '$.id'), 'model') as full_name,
            COALESCE(SUM(tokens_input), 0), 
            COALESCE(SUM(tokens_output), 0), 
            COALESCE(SUM(tokens_reasoning), 0),
            COUNT(*)
        FROM session 
        {where_model_clause}
        GROUP BY full_name
        ORDER BY SUM(tokens_output) DESC;
    """, model_params)
    model_shares = []
    for m_name, m_in, m_out, m_reas, m_count in c.fetchall():
        model_shares.append({
            "name": m_name,
            "tokens_input": m_in,
            "tokens_output": m_out,
            "tokens_reasoning": m_reas,
            "total_tokens": m_in + m_out + m_reas,
            "session_count": m_count
        })

    # Tool calling stats strictly within window
    if start_ms and end_ms:
        c.execute("""
            SELECT json_extract(data, '$.tool') as tool_name, COUNT(*) 
            FROM part 
            WHERE json_extract(data, '$.type') = 'tool' 
              AND message_id IN (
                  SELECT id FROM message WHERE session_id IN (
                      SELECT id FROM session WHERE time_created >= ? AND time_created <= ?
                  )
              )
            GROUP BY tool_name 
            ORDER BY COUNT(*) DESC 
            LIMIT 12;
        """, (start_ms, end_ms))
    elif start_ms:
        c.execute("""
            SELECT json_extract(data, '$.tool') as tool_name, COUNT(*) 
            FROM part 
            WHERE json_extract(data, '$.type') = 'tool' 
              AND message_id IN (
                  SELECT id FROM message WHERE session_id IN (
                      SELECT id FROM session WHERE time_created >= ?
                  )
              )
            GROUP BY tool_name 
            ORDER BY COUNT(*) DESC 
            LIMIT 12;
        """, (start_ms,))
    else:
        c.execute("""
            SELECT json_extract(data, '$.tool') as tool_name, COUNT(*) 
            FROM part 
            WHERE json_extract(data, '$.type') = 'tool' 
            GROUP BY tool_name 
            ORDER BY COUNT(*) DESC 
            LIMIT 12;
        """)
    tools = [{"name": r[0] or "tool", "count": r[1]} for r in c.fetchall()]

    where_dir_clause = "WHERE directory IS NOT NULL"
    dir_params = []
    if start_ms and end_ms:
        where_dir_clause += " AND time_created >= ? AND time_created <= ?"
        dir_params = [start_ms, end_ms]
    elif start_ms:
        where_dir_clause += " AND time_created >= ?"
        dir_params = [start_ms]

    c.execute(f"SELECT directory, COUNT(*), COALESCE(SUM(tokens_output), 0) FROM session {where_dir_clause} GROUP BY directory ORDER BY COUNT(*) DESC;", dir_params)
    dir_rows = c.fetchall()
    directories = [{
        "path": sanitize_path(r[0]),
        "folder": Path(r[0]).name or "root" if r[0] else "root",
        "count": r[1],
        "tokens_output": r[2]
    } for r in dir_rows]

    duration_buckets = {"< 1 min": 0, "1 - 5 mins": 0, "5 - 15 mins": 0, "15 - 30 mins": 0, "> 30 mins": 0}
    where_dur_clause = "WHERE time_created IS NOT NULL AND time_updated IS NOT NULL"
    dur_params = []
    if start_ms and end_ms:
        where_dur_clause += " AND time_created >= ? AND time_created <= ?"
        dur_params = [start_ms, end_ms]
    elif start_ms:
        where_dur_clause += " AND time_created >= ?"
        dur_params = [start_ms]

    c.execute(f"SELECT time_created, time_updated FROM session {where_dur_clause};", dur_params)
    for cr, up in c.fetchall():
        if up > cr:
            mins = (up - cr) / (1000.0 * 60)
            if mins < 1:
                duration_buckets["< 1 min"] += 1
            elif mins < 5:
                duration_buckets["1 - 5 mins"] += 1
            elif mins < 15:
                duration_buckets["5 - 15 mins"] += 1
            elif mins < 30:
                duration_buckets["15 - 30 mins"] += 1
            else:
                duration_buckets["> 30 mins"] += 1

    conn.close()

    # Fold the non-OpenCode harnesses into the unified totals. The session list
    # already merges them, so the headline numbers have to agree with it.
    if not harness_filter or harness_filter == "all":
        external = get_openclaw_sessions() + get_aider_sessions() + get_continue_sessions()
        if start_ms:
            external = [s for s in external if (s.get("time_created") or 0) >= start_ms]
        if end_ms:
            external = [s for s in external if (s.get("time_created") or 0) <= end_ms]

        for s in external:
            total_sessions += 1
            total_messages += s.get("message_count") or 0
            sum_in += s.get("tokens_input") or 0
            sum_out += s.get("tokens_output") or 0
            sum_reas += s.get("tokens_reasoning") or 0

            # OpenCode names models "<provider>/<id>"; match that so the same
            # model from two harnesses aggregates into one share.
            model_name = s.get("model") or "unknown"
            provider = s.get("provider")
            if provider and "/" not in model_name:
                model_name = f"{provider}/{model_name}"
            existing = next((m for m in model_shares if m["name"] == model_name), None)
            if existing:
                existing["session_count"] += 1
                existing["tokens_input"] += s.get("tokens_input") or 0
                existing["tokens_output"] += s.get("tokens_output") or 0
                existing["tokens_reasoning"] += s.get("tokens_reasoning") or 0
                existing["total_tokens"] += s.get("tokens_total") or 0
            else:
                model_shares.append({
                    "name": model_name,
                    "session_count": 1,
                    "tokens_input": s.get("tokens_input") or 0,
                    "tokens_output": s.get("tokens_output") or 0,
                    "tokens_reasoning": s.get("tokens_reasoning") or 0,
                    "total_tokens": s.get("tokens_total") or 0,
                })

            folder = s.get("folder") or "root"
            d_existing = next((d for d in directories if d["folder"] == folder), None)
            if d_existing:
                d_existing["count"] += 1
                d_existing["tokens_output"] += s.get("tokens_output") or 0
            else:
                directories.append({
                    "path": s.get("directory") or folder,
                    "folder": folder,
                    "count": 1,
                    "tokens_output": s.get("tokens_output") or 0,
                })

        model_shares.sort(key=lambda m: m["session_count"], reverse=True)
        directories.sort(key=lambda d: d["count"], reverse=True)

    # Corrected metrics from the turn grain. avg_decode_tps divides output by
    # end-to-end turn latency including tool time, so it answers a different
    # question from "how fast does this model decode"; both are published, each
    # under its own name.
    turn_summary = {}
    try:
        tconn = get_db_connection()
        if tconn:
            try:
                _turns = build_turns(tconn, start_ms, end_ms)
                turn_summary = summarize_turns(_turns)
                turn_summary["sessions_root"] = sessions_root
                turn_summary["sessions_subagent"] = sessions_subagent
                turn_summary["active_days"] = active_days
            finally:
                tconn.close()
    except Exception:
        turn_summary = {}

    # Discovered Harnesses
    harnesses = get_harness_inventory(query_params)

    return {
        "turn_metrics": turn_summary,
        "total_sessions": total_sessions,
        "total_messages": total_messages,
        "tokens_input": sum_in,
        "tokens_output": sum_out,
        "tokens_reasoning": sum_reas,
        "tokens_total": sum_in + sum_out + sum_reas,
        "avg_decode_tps": round(avg_tps, 1),
        "peak_decode_tps": round(peak_tps, 1),
        "total_generation_seconds": round(total_assistant_gen_s, 1),
        "models": {m["name"]: m["session_count"] for m in model_shares},
        "model_shares": model_shares,
        "tool_stats": tools,
        "tps_buckets": tps_buckets,
        "duration_buckets": duration_buckets,
        "directories": directories,
        "harnesses": harnesses,
        "live": check_live_status(),
        "system_info": {
            "opencode_db": sanitize_path(str(CONFIG["opencode_db"])),
            "openclaw_db": sanitize_path(str(CONFIG["openclaw_db"])) if CONFIG["openclaw_db"] else None,
            "openclaw_homes": [sanitize_path(str(h)) for h in CONFIG["openclaw_homes"]],
        }
    }


def scan_openclaw_session_files():
    """
    Every OpenClaw transcript on disk, across all discovered homes and agents.

    Transcripts live at <home>/agents/<agent>/sessions/<uuid>.jsonl with a
    sessions.json index alongside them holding per-scope metadata. The
    <uuid>.trajectory.jsonl sidecars mirror the same turns and are skipped so
    sessions are not counted twice.
    """
    found = []
    seen = set()

    for home in CONFIG.get("openclaw_homes", []):
        agents_dir = home / "agents"
        if not agents_dir.is_dir():
            continue
        try:
            agent_dirs = sorted(d for d in agents_dir.iterdir() if d.is_dir())
        except Exception:
            continue

        for agent_dir in agent_dirs:
            sess_dir = agent_dir / "sessions"
            if not sess_dir.is_dir():
                continue

            index = {}
            idx_file = sess_dir / "sessions.json"
            if idx_file.exists():
                try:
                    raw = json.loads(idx_file.read_text(encoding="utf-8", errors="ignore"))
                    if isinstance(raw, dict):
                        for scope_key, entry in raw.items():
                            if isinstance(entry, dict) and entry.get("sessionId"):
                                index[entry["sessionId"]] = dict(entry, scope_key=scope_key)
                except Exception:
                    pass

            try:
                files = sorted(sess_dir.glob("*.jsonl"))
            except Exception:
                continue

            for f in files:
                if f.name.endswith(".trajectory.jsonl"):
                    continue
                try:
                    key = f.resolve()
                except Exception:
                    key = f
                if key in seen:
                    continue
                seen.add(key)
                found.append({
                    "path": f,
                    "home": home,
                    "agent": agent_dir.name,
                    "meta": index.get(f.stem, {}),
                })

    return found


def _openclaw_text(content):
    """Flattens an OpenClaw message content field (string or part list) to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks = []
    for part in content:
        if isinstance(part, dict) and part.get("type") in ("text", "reasoning"):
            chunks.append(str(part.get("text") or ""))
    return "\n".join(c for c in chunks if c)


def parse_openclaw_transcript(entry, want_messages=False):
    """
    Reads one OpenClaw JSONL transcript into the shape the dashboard uses.

    Token accounting mirrors how the provider bills: `output` is additive per
    turn, while `input` is the whole context resent each turn, so summing it
    gives billed input rather than a single context size.
    """
    path = entry["path"]
    meta = entry.get("meta") or {}

    try:
        raw_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return None

    session_id = path.stem
    cwd = None
    provider = None
    model_id = None
    title = None

    tokens_input = 0
    tokens_output = 0
    tokens_reasoning = 0
    cost_total = 0.0
    assistant_turns = 0
    message_count = 0
    has_tool_calls = False

    first_ts = None
    last_ts = None
    messages = []

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue

        ev_type = ev.get("type")

        if ev_type == "session":
            session_id = ev.get("id") or session_id
            cwd = ev.get("cwd") or cwd
            continue

        if ev_type == "model_change":
            provider = ev.get("provider") or provider
            model_id = ev.get("modelId") or model_id
            continue

        if ev_type == "custom" and ev.get("customType") == "model-snapshot":
            data = ev.get("data") or {}
            provider = data.get("provider") or provider
            model_id = data.get("modelId") or model_id
            continue

        if ev_type != "message":
            continue

        msg = ev.get("message") or {}
        role = msg.get("role")
        ts = msg.get("timestamp")
        if not isinstance(ts, (int, float)):
            ts = None
        if ts:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)

        if role in ("user", "assistant"):
            message_count += 1

        if role == "user" and title is None:
            text = _openclaw_text(msg.get("content")).strip()
            if text:
                title = text.splitlines()[0][:120]

        parts = []
        if role == "assistant":
            assistant_turns += 1
            provider = msg.get("provider") or provider
            model_id = msg.get("model") or model_id

            usage = msg.get("usage") or {}
            tokens_input += int(usage.get("input") or 0)
            tokens_output += int(usage.get("output") or 0)
            tokens_reasoning += int(usage.get("reasoning") or 0)
            cost = usage.get("cost")
            if isinstance(cost, dict):
                cost_total += float(cost.get("total") or 0.0)
            elif isinstance(cost, (int, float)):
                cost_total += float(cost)

            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "toolCall":
                        has_tool_calls = True
                        if want_messages:
                            parts.append({
                                "type": "tool",
                                "tool": part.get("name"),
                                "call_id": part.get("id"),
                                "status": "pending",
                                "input": part.get("arguments") or {},
                                "output": "",
                            })
                    elif ptype == "reasoning" and want_messages:
                        parts.append({"type": "reasoning", "content": str(part.get("text") or "")})
                    elif ptype == "text" and want_messages:
                        parts.append({"type": "text", "content": str(part.get("text") or "")})

        elif want_messages and role == "user":
            parts.append({"type": "text", "content": _openclaw_text(msg.get("content"))})

        if want_messages and role == "toolResult":
            # Fold the result back into the tool part that issued the call. The
            # join key is the tool call id; an empty result is still a result,
            # so matching is on pending status rather than on empty output.
            call_id = msg.get("toolCallId")
            out_text = _openclaw_text(msg.get("content"))[:2000]
            status = (msg.get("details") or {}).get("status") or "completed"

            target = None
            fallback = None
            for prev in reversed(messages):
                for prev_part in prev.get("parts", []):
                    if prev_part.get("type") != "tool":
                        continue
                    if call_id and prev_part.get("call_id") == call_id:
                        target = prev_part
                        break
                    if fallback is None and prev_part.get("status") == "pending":
                        fallback = prev_part
                if target:
                    break

            hit = target or fallback
            if hit is not None:
                hit["output"] = out_text
                hit["status"] = status
            continue

        if want_messages and role in ("user", "assistant"):
            usage = msg.get("usage") or {}
            turn_in = int(usage.get("input") or 0)
            turn_out = int(usage.get("output") or 0)
            turn_reas = int(usage.get("reasoning") or 0)
            messages.append({
                "id": ev.get("id") or f"{session_id}:{len(messages)}",
                "role": role,
                "time_created": int(ts) if ts else 0,
                "date_str": datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S") if ts else "",
                "duration_s": 0.0,
                "tokens_input": turn_in,
                "tokens_output": turn_out,
                "tokens_reasoning": turn_reas,
                "total_tokens": turn_in + turn_out + turn_reas,
                "tps": 0.0,
                "parts": parts,
            })

    if first_ts is None:
        first_ts = meta.get("sessionStartedAt") or meta.get("updatedAt")
    if last_ts is None:
        last_ts = meta.get("lastInteractionAt") or meta.get("updatedAt") or first_ts
    if not first_ts:
        try:
            first_ts = int(path.stat().st_mtime * 1000)
        except Exception:
            first_ts = 0
        last_ts = first_ts

    duration_s = max(0.0, (last_ts - first_ts) / 1000.0) if (first_ts and last_ts) else 0.0
    generated = tokens_output + tokens_reasoning
    tps = round(generated / duration_s, 1) if (duration_s > 0.5 and generated) else 0.0

    directory = cwd or meta.get("cwd") or str(entry["home"])
    agent = entry.get("agent") or "main"
    channel = ((meta.get("origin") or {}).get("provider")
               or meta.get("lastChannel")
               or (meta.get("route") or {}).get("channel"))

    if not title:
        title = f"OpenClaw {agent} session"

    model_name = model_id or "unknown"
    provider_name = provider or "openclaw"

    record = {
        "id": session_id,
        "harness": "openclaw",
        "title": title,
        "directory": sanitize_path(directory),
        "folder": Path(directory).name if directory else agent,
        "model": model_name,
        "provider": provider_name,
        "agent": agent,
        "channel": channel or "local",
        "source_path": sanitize_path(str(path)),
        "date_str": datetime.fromtimestamp(first_ts / 1000).strftime("%Y-%m-%d %H:%M") if first_ts else "Recent",
        "time_created": int(first_ts or 0),
        "duration_s": round(duration_s, 1),
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "tokens_reasoning": tokens_reasoning,
        "tokens_total": tokens_input + tokens_output + tokens_reasoning,
        "cost": round(cost_total, 6),
        "message_count": message_count,
        "tps": tps,
        "peak_tps": tps,
        "has_tool_calls": has_tool_calls,
    }

    if want_messages:
        record["messages"] = messages
        record["avg_tps"] = tps
        record["date_str"] = (
            datetime.fromtimestamp(first_ts / 1000).strftime("%Y-%m-%d %H:%M:%S") if first_ts else ""
        )

    return record


def get_openclaw_acp_sessions():
    """Live gateway/ACP sessions from the OpenClaw state database, if any."""
    db_path = CONFIG.get("openclaw_db")
    if not db_path or not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c = conn.cursor()
        c.execute("SELECT session_key, session_id, agent, mode, cwd, updated_at FROM acp_sessions")
        rows = c.fetchall()
        conn.close()
    except Exception:
        return []

    res = []
    for r in rows:
        agent_name = r[2] or "default"
        t_updated = (r[5] or 0) / 1000
        created_dt = datetime.fromtimestamp(t_updated) if t_updated else None
        res.append({
            "id": str(r[0] or r[1] or "oc_session"),
            "harness": "openclaw",
            "title": f"OpenClaw ACP ({agent_name})",
            "directory": sanitize_path(r[4] or "~"),
            "folder": Path(r[4]).name if r[4] else "openclaw",
            "model": f"openclaw/{agent_name}",
            "provider": "openclaw",
            "agent": agent_name,
            "channel": r[3] or "acp",
            "date_str": created_dt.strftime("%Y-%m-%d %H:%M") if created_dt else "Recent",
            "time_created": int(t_updated * 1000),
            "duration_s": 0.0,
            "tokens_input": 0,
            "tokens_output": 0,
            "tokens_reasoning": 0,
            "tokens_total": 0,
            "cost": 0.0,
            "message_count": 1,
            "tps": 0.0,
            "peak_tps": 0.0,
            "has_tool_calls": False,
        })
    return res


def get_openclaw_sessions():
    """
    All OpenClaw sessions: JSONL transcripts from every home on disk, plus any
    live ACP sessions the gateway database knows about.
    """
    sessions = []
    seen_ids = set()

    for entry in scan_openclaw_session_files():
        try:
            rec = parse_openclaw_transcript(entry)
        except Exception:
            rec = None
        if rec and rec["id"] not in seen_ids:
            seen_ids.add(rec["id"])
            sessions.append(rec)

    for rec in get_openclaw_acp_sessions():
        if rec["id"] not in seen_ids:
            seen_ids.add(rec["id"])
            sessions.append(rec)

    sessions.sort(key=lambda s: s.get("time_created") or 0, reverse=True)
    return sessions


def get_openclaw_session_detail(session_id):
    """Full transcript for one OpenClaw session, for the session inspector."""
    for entry in scan_openclaw_session_files():
        if entry["path"].stem != session_id:
            continue
        try:
            rec = parse_openclaw_transcript(entry, want_messages=True)
        except Exception:
            rec = None
        if rec:
            return rec
    return None


def get_aider_sessions():
    histories = scan_aider_history()
    res = []
    for h in histories:
        try:
            path = Path(h["path"])
            text = path.read_text(encoding="utf-8", errors="ignore")
            prompts = text.count("\n#### ") or 1
            tokens_approx = len(text) // 4
            stat = path.stat()
            created_dt = datetime.fromtimestamp(stat.st_mtime)
            res.append({
                "id": f"aider_{path.name}_{int(stat.st_mtime)}",
                "harness": "aider",
                "title": f"Aider Session in {sanitize_path(str(path.parent))}",
                "directory": sanitize_path(str(path.parent)),
                "folder": path.parent.name or "aider",
                "model": "aider/local",
                "provider": "aider",
                "date_str": created_dt.strftime("%Y-%m-%d %H:%M"),
                "time_created": int(stat.st_mtime * 1000),
                "duration_s": 120.0,
                "tokens_input": int(tokens_approx * 0.7),
                "tokens_output": int(tokens_approx * 0.3),
                "tokens_reasoning": 0,
                "tokens_total": tokens_approx,
                "cost": 0.0,
                "message_count": prompts,
                "tps": 0.0,
                "peak_tps": 0.0,
                "has_tool_calls": False,
            })
        except Exception:
            continue
    return res


def get_continue_sessions():
    sessions = scan_continue_sessions()
    res = []
    for s in sessions:
        try:
            path = Path(s["path"])
            data = json.loads(path.read_text(encoding="utf-8"))
            msgs = data.get("history", [])
            title = data.get("title") or (msgs[0].get("message", {}).get("content", "Continue session")[:60] if msgs else "Continue Session")
            stat = path.stat()
            created_dt = datetime.fromtimestamp(stat.st_mtime)
            res.append({
                "id": f"continue_{path.stem}",
                "harness": "continue",
                "title": title,
                "directory": sanitize_path(s.get("workspaceDirectory") or "~"),
                "folder": Path(s.get("workspaceDirectory") or "~").name,
                "model": "continue/session",
                "provider": "continue",
                "date_str": created_dt.strftime("%Y-%m-%d %H:%M"),
                "time_created": int(stat.st_mtime * 1000),
                "duration_s": 90.0,
                "tokens_input": 0,
                "tokens_output": 0,
                "tokens_reasoning": 0,
                "tokens_total": 0,
                "cost": 0.0,
                "message_count": len(msgs),
                "tps": 0.0,
                "peak_tps": 0.0,
                "has_tool_calls": False,
            })
        except Exception:
            continue
    return res


# The Sessions Explorer sends named speed tiers ("turbo"); saved dashboards may
# still carry the older bucket strings ("60+"). Both resolve here, so neither
# side can silently emit a value the other ignores.
SPEED_TIERS = {
    "turbo":    (45.0, None),
    "fast":     (25.0, 45.0),
    "standard": (15.0, 25.0),
    "deep":     (0.0,  15.0),
    # Legacy bucket labels, kept so built-in dashboards keep working.
    "60+":      (60.0, None),
    "45-60":    (45.0, 60.0),
    "30-45":    (30.0, 45.0),
    "15-30":    (15.0, 30.0),
    "<15":      (0.0,  15.0),
}

SESSION_SORTS = {
    "latest":        (lambda x: x.get("time_created", 0), True),
    "oldest":        (lambda x: x.get("time_created", 0), False),
    "duration":      (lambda x: x.get("duration_s", 0), True),
    "tokens":        (lambda x: x.get("tokens_output", 0), True),
    "speed":         (lambda x: x.get("tps", 0), True),
    # Legacy names.
    "date_desc":     (lambda x: x.get("time_created", 0), True),
    "date_asc":      (lambda x: x.get("time_created", 0), False),
    "tps_desc":      (lambda x: x.get("tps", 0), True),
    "tokens_desc":   (lambda x: x.get("tokens_output", 0), True),
    "duration_desc": (lambda x: x.get("duration_s", 0), True),
}


def _matches_speed_tier(tps, tier):
    bounds = SPEED_TIERS.get(tier)
    if not bounds:
        return True  # Unknown tier filters nothing rather than everything.
    low, high = bounds
    if not tps:
        # A session with no measured speed belongs to no tier.
        return False
    return tps >= low and (high is None or tps < high)


def _sort_sessions(rows, sort_by):
    key, reverse = SESSION_SORTS.get(sort_by, SESSION_SORTS["latest"])
    rows.sort(key=key, reverse=reverse)


def get_sessions(query_params):
    search = query_params.get("q", [""])[0].lower()
    harness_filter = query_params.get("harness", [""])[0].lower()
    folder_filter = query_params.get("folder", [""])[0]
    provider_filter = query_params.get("provider", [""])[0]
    model_filter = query_params.get("model", [""])[0]
    speed_tier = query_params.get("speed_tier", [""])[0]
    status_filter = query_params.get("status", [""])[0]
    date_from = query_params.get("from", [""])[0]
    date_to = query_params.get("to", [""])[0]
    window = query_params.get("window", [""])[0]  # e.g. 10m, 1h, 6h, 1d, 3d, 7d, 30d
    sort_by = query_params.get("sort", ["latest"])[0]

    start_ms, end_ms = resolve_window(query_params)

    all_raw_sessions = []

    # 1. Fetch OpenCode sessions if selected
    if not harness_filter or harness_filter in ["opencode", "all"]:
        conn = get_db_connection()
        if conn:
            c = conn.cursor()
            sql = """
                SELECT 
                    s.id,
                    s.title,
                    s.directory,
                    s.model,
                    s.time_created,
                    s.time_updated,
                    s.tokens_input,
                    s.tokens_output,
                    s.tokens_reasoning,
                    s.cost,
                    s.summary_files,
                    s.summary_additions,
                    s.summary_deletions,
                    COUNT(m.id) as message_count
                FROM session s
                LEFT JOIN message m ON s.id = m.session_id
                GROUP BY s.id
                ORDER BY s.time_created DESC
            """
            c.execute(sql)
            rows = c.fetchall()
            for r in rows:
                (
                    sid,
                    title,
                    directory,
                    model_raw,
                    t_created,
                    t_updated,
                    t_in,
                    t_out,
                    t_reas,
                    cost,
                    s_files,
                    s_add,
                    s_del,
                    msg_count,
                ) = r

                folder = Path(directory).name if directory else "root"
                sanitized_dir = sanitize_path(directory)

                model_name = "unknown"
                provider_name = "unknown"
                if model_raw:
                    try:
                        m_obj = json.loads(model_raw)
                        model_name = m_obj.get("id", "unknown")
                        provider_name = m_obj.get("providerID", "unknown")
                    except Exception:
                        model_name = model_raw

                # Calculate duration
                duration_s = (t_updated - t_created) / 1000 if (t_updated and t_created and t_updated > t_created) else 0

                # Calculate assistant turn speeds from message table
                c.execute("""
                    SELECT id, data
                    FROM message
                    WHERE session_id = ? AND data LIKE '%assistant%'
                """, (sid,))
                asst_rows = c.fetchall()

                total_turn_tps = 0.0
                peak_turn_tps = 0.0
                turns_with_tps = 0
                has_tool_calls = False

                for (m_id, raw_mdata) in asst_rows:
                    try:
                        d = json.loads(raw_mdata)
                        if d.get("role") != "assistant":
                            continue
                        fin = d.get("finish", "")
                        if fin == "tool-calls" or "tool" in str(d):
                            has_tool_calls = True
                        t = d.get("time", {})
                        a_start = t.get("created")
                        a_end = t.get("completed")
                        toks = d.get("tokens", {})
                        a_out = toks.get("output", 0) + toks.get("reasoning", 0)
                        if a_start and a_end and a_end > a_start and a_out > 0:
                            turn_dur = (a_end - a_start) / 1000.0
                            if turn_dur > 0.05:
                                turn_tps = a_out / turn_dur
                                if turn_tps < 250:
                                    total_turn_tps += turn_tps
                                    turns_with_tps += 1
                                    if turn_tps > peak_turn_tps:
                                        peak_turn_tps = turn_tps
                    except Exception:
                        continue

                session_tps = round(total_turn_tps / turns_with_tps, 1) if turns_with_tps > 0 else 0.0
                created_dt = datetime.fromtimestamp(t_created / 1000) if t_created else None

                all_raw_sessions.append({
                    "id": sid,
                    "harness": "opencode",
                    "title": title or "Untitled Session",
                    "directory": sanitized_dir,
                    "folder": folder,
                    "model": model_name,
                    "provider": provider_name,
                    "date_str": created_dt.strftime("%Y-%m-%d %H:%M") if created_dt else "",
                    "time_created": t_created or 0,
                    "duration_s": round(duration_s, 1),
                    "tokens_input": t_in or 0,
                    "tokens_output": t_out or 0,
                    "tokens_reasoning": t_reas or 0,
                    "tokens_total": (t_in or 0) + (t_out or 0) + (t_reas or 0),
                    "cost": cost or 0.0,
                    "message_count": msg_count,
                    "tps": session_tps,
                    "peak_tps": round(peak_turn_tps, 1),
                    "has_tool_calls": has_tool_calls,
                })
            conn.close()

    # 2. Fetch OpenClaw sessions if selected
    if not harness_filter or harness_filter in ["openclaw", "all"]:
        all_raw_sessions.extend(get_openclaw_sessions())

    if not harness_filter or harness_filter in ["hermes", "all"]:
        all_raw_sessions.extend(get_hermes_sessions())

    for _hid, _fetch in (("cline", get_cline_sessions), ("roo", get_roo_sessions),
                         ("zed", get_zed_sessions), ("goose", get_goose_sessions),
                         ("lmstudio", get_lmstudio_sessions), ("jan", get_jan_sessions),
                         ("crush", get_crush_sessions), ("anythingllm", get_anythingllm_sessions),
                         ("openwebui", get_openwebui_sessions)):
        if not harness_filter or harness_filter in [_hid, "all"]:
            all_raw_sessions.extend(_fetch())

    # 3. Fetch Aider sessions if selected
    if not harness_filter or harness_filter in ["aider", "all"]:
        all_raw_sessions.extend(get_aider_sessions())

    # 4. Fetch Continue sessions if selected
    if not harness_filter or harness_filter in ["continue", "all"]:
        all_raw_sessions.extend(get_continue_sessions())

    results = []
    for s in all_raw_sessions:
        # Search filter
        if search:
            match_title = search in s["title"].lower()
            match_folder = search in s["folder"].lower()
            match_model = search in s["model"].lower()
            match_provider = search in s["provider"].lower()
            if not (match_title or match_folder or match_model or match_provider):
                continue

        # Folder filter
        if folder_filter and s["folder"] != folder_filter:
            continue

        # Provider filter
        if provider_filter and s["provider"] != provider_filter:
            continue

        # Model filter
        if model_filter and model_filter not in s["model"]:
            continue

        # Date / Time window filter
        if s.get("time_created"):
            t_created_ms = s["time_created"]
            if start_ms and t_created_ms < start_ms:
                continue
            if end_ms and t_created_ms > end_ms:
                continue

        # Speed tier filter.
        session_tps = s.get("tps", 0.0)
        if speed_tier and not _matches_speed_tier(session_tps, speed_tier):
            continue

        # Filter by status / finish reason
        if status_filter == "tool_calls" and not s.get("has_tool_calls"):
            continue
        elif status_filter == "stop" and s.get("has_tool_calls"):
            continue

        results.append(s)

    _sort_sessions(results, sort_by)
    return results


# ============================================================
# TURN-LEVEL AGGREGATION
# The missing grain. Session rows cannot answer "where did the time go" or
# "how fast is decode really", because a session's duration is wall clock
# including overnight idle, and its tps divides output by end-to-end turn
# latency - which contains tool execution. One pass over message + part
# produces the turn table every corrected metric derives from.
# ============================================================

def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo))


def build_turns(conn, start_ms=None, end_ms=None):
    """
    One row per assistant turn, with its time split into tool / reasoning /
    residual. Residual is prefill + answer decode + queue; OpenCode records no
    TTFT, so prefill cannot be separated out and the field is named honestly.
    """
    c = conn.cursor()

    where, params = "", []
    if start_ms and end_ms:
        where = "WHERE m.time_created >= ? AND m.time_created <= ?"
        params = [start_ms, end_ms]
    elif start_ms:
        where = "WHERE m.time_created >= ?"
        params = [start_ms]

    # Tool and reasoning spans, grouped by message in one pass rather than a
    # query per turn.
    c.execute("""
        SELECT message_id,
               json_extract(data, '$.type'),
               json_extract(data, '$.tool'),
               json_extract(data, '$.state.status'),
               json_extract(data, '$.state.time.start'),
               json_extract(data, '$.state.time.end'),
               json_extract(data, '$.time.start'),
               json_extract(data, '$.time.end'),
               json_extract(data, '$.state.input.filePath')
        FROM part
        WHERE json_extract(data, '$.type') IN ('tool', 'reasoning')
    """)

    tool_by_msg = {}
    reason_by_msg = {}
    for mid, ptype, tool, status, ts, te, rs, re_, fpath in c.fetchall():
        if ptype == "tool":
            dur = ((te - ts) / 1000.0) if (ts and te and te > ts) else 0.0
            tool_by_msg.setdefault(mid, []).append({
                "tool": tool or "unknown",
                "status": status or "unknown",
                "duration_s": round(dur, 3),
                "file": fpath,
            })
        else:
            dur = ((re_ - rs) / 1000.0) if (rs and re_ and re_ > rs) else 0.0
            reason_by_msg[mid] = reason_by_msg.get(mid, 0.0) + dur

    c.execute(f"""
        SELECT m.id, m.session_id, m.time_created, m.data, s.directory, s.project_id, s.parent_id
        FROM message m
        JOIN session s ON s.id = m.session_id
        {where}
        ORDER BY m.time_created ASC
    """, params)

    turns = []
    for mid, sid, t_created, raw, directory, project_id, parent_id in c.fetchall():
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("role") != "assistant":
            continue

        t = d.get("time") or {}
        created, completed = t.get("created"), t.get("completed")
        duration_s = ((completed - created) / 1000.0) if (created and completed and completed > created) else 0.0

        toks = d.get("tokens") or {}
        cache = toks.get("cache") or {}
        tok_in = int(toks.get("input") or 0)
        tok_out = int(toks.get("output") or 0)
        tok_reas = int(toks.get("reasoning") or 0)
        tok_cache_read = int(cache.get("read") or 0)
        tok_cache_write = int(cache.get("write") or 0)

        tools = tool_by_msg.get(mid, [])
        tool_time = sum(x["duration_s"] for x in tools)
        reason_time = round(reason_by_msg.get(mid, 0.0), 3)
        residual = max(0.0, duration_s - tool_time - reason_time)

        generated = tok_out + tok_reas
        decode_tps = round(generated / duration_s, 2) if (duration_s > 0.05 and generated) else 0.0

        err = d.get("error") or {}
        turns.append({
            "turn_id": mid,
            "session_id": sid,
            "project_id": project_id,
            "directory": sanitize_path(directory) if directory else None,
            "folder": Path(directory).name if directory else "root",
            "is_subagent": bool(parent_id),
            "agent": d.get("agent"),
            "mode": d.get("mode"),
            "model": d.get("modelID"),
            "provider": d.get("providerID"),
            "time_created": created or t_created,
            "duration_s": round(duration_s, 3),
            "tool_time_s": round(tool_time, 3),
            "reasoning_time_s": reason_time,
            "residual_s": round(residual, 3),
            "tokens_input": tok_in,
            "tokens_output": tok_out,
            "tokens_reasoning": tok_reas,
            "tokens_cache_read": tok_cache_read,
            "tokens_cache_write": tok_cache_write,
            "context_size": tok_in + tok_cache_read,
            "decode_tps": decode_tps,
            "finish": d.get("finish"),
            "error_name": err.get("name") if isinstance(err, dict) else None,
            "cost": float(d.get("cost") or 0.0),
            "tools": tools,
        })

    return turns


def get_turns(query_params=None):
    start_ms, end_ms = resolve_window(query_params)
    conn = get_db_connection()
    if not conn:
        return []
    try:
        return build_turns(conn, start_ms, end_ms)
    finally:
        conn.close()


def summarize_turns(turns):
    """Corrected speed and time metrics. Published alongside their definitions."""
    rates = sorted(t["decode_tps"] for t in turns if t["decode_tps"] > 0)
    wall = sum(t["duration_s"] for t in turns)
    tool_s = sum(t["tool_time_s"] for t in turns)
    reason_s = sum(t["reasoning_time_s"] for t in turns)
    generated = sum(t["tokens_output"] + t["tokens_reasoning"] for t in turns)
    billed_in = sum(t["tokens_input"] for t in turns)
    cache_read = sum(t["tokens_cache_read"] for t in turns)

    outcomes, errors = {}, {}
    for t in turns:
        outcomes[t["finish"] or "incomplete"] = outcomes.get(t["finish"] or "incomplete", 0) + 1
        if t["error_name"]:
            errors[t["error_name"]] = errors.get(t["error_name"], 0) + 1

    return {
        # Median per-turn generation rate. The headline speed number.
        "decode_tps_p50": round(_percentile(rates, 50), 1),
        "decode_tps_p90": round(_percentile(rates, 90), 1),
        "decode_tps_p99": round(_percentile(rates, 99), 1),
        "decode_tps_peak": round(rates[-1], 1) if rates else 0.0,
        # Tokens per second of elapsed agent time, tools included. Not the same
        # question as the median above, so it carries its own name.
        "throughput_end_to_end": round(generated / wall, 2) if wall else 0.0,
        "agent_wall_clock_s": round(wall, 1),
        "tool_time_s": round(tool_s, 1),
        "reasoning_time_s": round(reason_s, 1),
        "residual_time_s": round(max(0.0, wall - tool_s - reason_s), 1),
        "tokens_generated": generated,
        "tokens_billed_input": billed_in,
        "tokens_cache_read": cache_read,
        "cache_hit_ratio": round(cache_read / (billed_in + cache_read) * 100, 1) if (billed_in + cache_read) else 0.0,
        "context_peak": max((t["context_size"] for t in turns), default=0),
        "turns_total": len(turns),
        "turns_with_speed": len(rates),
        "outcome_counts": outcomes,
        "error_counts": errors,
        "abort_rate": round(errors.get("MessageAbortedError", 0) / len(turns) * 100, 1) if turns else 0.0,
    }


def get_tool_stats(turns):
    """Per-tool reliability and latency. Tools are the largest controllable cost."""
    by_tool = {}
    for t in turns:
        for call in t["tools"]:
            e = by_tool.setdefault(call["tool"], {"name": call["tool"], "calls": 0, "errors": 0, "durations": [], "total_s": 0.0})
            e["calls"] += 1
            if call["status"] == "error":
                e["errors"] += 1
            e["durations"].append(call["duration_s"])
            e["total_s"] += call["duration_s"]

    grand_total = sum(e["total_s"] for e in by_tool.values()) or 1.0
    out = []
    for e in by_tool.values():
        ds = sorted(e["durations"])
        out.append({
            "name": e["name"],
            "calls": e["calls"],
            "errors": e["errors"],
            "error_rate": round(e["errors"] / e["calls"] * 100, 1) if e["calls"] else 0.0,
            "p50_s": round(_percentile(ds, 50), 2),
            "p95_s": round(_percentile(ds, 95), 2),
            "max_s": round(ds[-1], 2) if ds else 0.0,
            "total_s": round(e["total_s"], 1),
            "pct_of_tool_time": round(e["total_s"] / grand_total * 100, 1),
            "is_mcp": "_" in e["name"] and not e["name"].islower() or e["name"].count("_") >= 2,
        })
    out.sort(key=lambda x: x["total_s"], reverse=True)
    return out


def get_long_poles(turns, limit=8):
    """Individual calls that ate the most wall clock, with their outcome."""
    calls = []
    for t in turns:
        for call in t["tools"]:
            calls.append({
                "tool": call["tool"],
                "status": call["status"],
                "duration_s": call["duration_s"],
                "session_id": t["session_id"],
                "folder": t["folder"],
                "time_created": t["time_created"],
            })
    calls.sort(key=lambda x: x["duration_s"], reverse=True)
    return calls[:limit]


def get_model_matrix(turns):
    by_model = {}
    for t in turns:
        name = f"{t['provider']}/{t['model']}" if t.get("provider") and t.get("model") else (t.get("model") or "unknown")
        e = by_model.setdefault(name, {
            "name": name, "turns": 0, "sessions": set(), "rates": [],
            "tok_out": 0, "tok_in": 0, "cache_read": 0, "errors": 0, "contexts": [],
        })
        e["turns"] += 1
        e["sessions"].add(t["session_id"])
        if t["decode_tps"] > 0:
            e["rates"].append(t["decode_tps"])
        e["tok_out"] += t["tokens_output"] + t["tokens_reasoning"]
        e["tok_in"] += t["tokens_input"]
        e["cache_read"] += t["tokens_cache_read"]
        if t["error_name"]:
            e["errors"] += 1
        e["contexts"].append(t["context_size"])

    out = []
    for e in by_model.values():
        rates = sorted(e["rates"])
        ctx = sorted(e["contexts"])
        total_ctx = e["tok_in"] + e["cache_read"]
        out.append({
            "name": e["name"],
            "sessions": len(e["sessions"]),
            "turns": e["turns"],
            "tokens_output": e["tok_out"],
            "tokens_billed_input": e["tok_in"],
            "tokens_cache_read": e["cache_read"],
            "cache_ratio": round(e["cache_read"] / total_ctx * 100, 1) if total_ctx else 0.0,
            "p50_tps": round(_percentile(rates, 50), 1),
            "p95_tps": round(_percentile(rates, 95), 1),
            "error_rate": round(e["errors"] / e["turns"] * 100, 1) if e["turns"] else 0.0,
            "context_median": int(_percentile(ctx, 50)),
            "context_peak": ctx[-1] if ctx else 0,
        })
    out.sort(key=lambda x: x["turns"], reverse=True)
    return out


def get_project_stats(turns):
    """
    Grouped by the project record rather than the raw directory string, which
    splits one project into several rows.
    """
    conn = get_db_connection()
    projects = {}
    if conn:
        try:
            c = conn.cursor()
            c.execute("SELECT id, worktree, vcs FROM project")
            for pid, worktree, vcs in c.fetchall():
                projects[pid] = {"worktree": worktree, "vcs": vcs}
        except Exception:
            pass
        finally:
            conn.close()

    by_proj = {}
    for t in turns:
        pid = t.get("project_id") or t.get("folder") or "unknown"
        meta = projects.get(pid, {})
        worktree = meta.get("worktree") or t.get("directory") or pid

        # OpenCode files sessions run outside a git repo under a "global"
        # project whose worktree is "/". Lumping them together hides real work,
        # so they are split by the directory the session actually ran in.
        if worktree in ("/", "", None):
            worktree = t.get("directory") or "unknown"
            pid = "dir:" + str(worktree)
            meta = {}
        e = by_proj.setdefault(pid, {
            "project_id": pid,
            "worktree": sanitize_path(worktree),
            "name": (Path(worktree).name or str(worktree)) if worktree else pid,
            "vcs": meta.get("vcs"),
            "turns": 0, "sessions": set(), "tokens_output": 0,
            "active_s": 0.0, "files": {}, "models": {}, "errors": 0,
        })
        e["turns"] += 1
        e["sessions"].add(t["session_id"])
        e["tokens_output"] += t["tokens_output"] + t["tokens_reasoning"]
        e["active_s"] += t["duration_s"]
        if t["error_name"]:
            e["errors"] += 1
        if t.get("model"):
            e["models"][t["model"]] = e["models"].get(t["model"], 0) + 1
        for call in t["tools"]:
            if call.get("file"):
                f = call["file"]
                e["files"][f] = e["files"].get(f, 0) + 1

    out = []
    for e in by_proj.values():
        files = sorted(e["files"].items(), key=lambda kv: kv[1], reverse=True)
        out.append({
            "project_id": e["project_id"],
            "worktree": e["worktree"],
            "name": e["name"],
            "vcs": e["vcs"],
            "sessions": len(e["sessions"]),
            "turns": e["turns"],
            "tokens_output": e["tokens_output"],
            "active_s": round(e["active_s"], 1),
            "error_rate": round(e["errors"] / e["turns"] * 100, 1) if e["turns"] else 0.0,
            "files_touched": len(files),
            "top_files": [{"path": sanitize_path(f), "name": Path(f).name, "edits": n} for f, n in files[:8]],
            "top_models": sorted(e["models"].items(), key=lambda kv: kv[1], reverse=True)[:3],
        })
    out.sort(key=lambda x: x["turns"], reverse=True)
    return out


def get_file_churn(turns, limit=12):
    counts = {}
    for t in turns:
        for call in t["tools"]:
            f = call.get("file")
            if f:
                counts[f] = counts.get(f, 0) + 1
    rows = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"path": sanitize_path(f), "name": Path(f).name, "edits": n} for f, n in rows]


# ============================================================
# HERMES AGENT  (NousResearch/hermes-agent)
# Sessions live in a SQLite state.db under HERMES_HOME (default ~/.hermes),
# with named profiles at <root>/profiles/<name>/state.db. The sessions table
# already carries per-session token and cost totals, so no message walk is
# needed for the list view.
#
# Columns are read by introspection rather than a fixed SELECT: the project is
# under heavy development and its schema gains columns often, so asking for one
# that does not exist yet would break the whole source.
# ============================================================

def resolve_hermes_homes():
    homes = []

    def add(candidate):
        try:
            path = Path(candidate).expanduser()
        except Exception:
            return
        if path.is_dir() and path not in homes:
            homes.append(path)

    h = Path.home()
    roots = []

    # An explicit HERMES_HOME may itself be a root holding named profiles.
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        add(env_home)
        roots.append(Path(env_home).expanduser())

    roots.append(h / ".hermes")

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        roots.append(Path(local_appdata) / "hermes")

    for root in roots:
        add(root)
        # Named profiles each keep their own transcript database.
        profiles = root / "profiles"
        if profiles.is_dir():
            try:
                for prof in sorted(profiles.iterdir()):
                    if prof.is_dir():
                        add(prof)
            except Exception:
                pass

    # A home is only useful if it actually holds a state database.
    return [hh for hh in homes if (hh / "state.db").exists()]


def hermes_state_dbs():
    return [hh / "state.db" for hh in resolve_hermes_homes()]


def _table_columns(conn, table):
    try:
        c = conn.cursor()
        c.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in c.fetchall()}
    except Exception:
        return set()


_HERMES_CACHE = {"at": 0.0, "rows": None}


def get_hermes_sessions():
    now = time.time()
    if _HERMES_CACHE["rows"] is not None and (now - _HERMES_CACHE["at"]) < 45:
        return _HERMES_CACHE["rows"]

    rows = []
    for db_path in hermes_state_dbs():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except Exception:
            continue
        try:
            cols = _table_columns(conn, "sessions")
            if not cols or "id" not in cols:
                continue

            wanted = [
                "id", "title", "display_name", "model", "source", "cwd", "git_branch",
                "started_at", "ended_at", "last_activity_at", "message_count",
                "tool_call_count", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "reasoning_tokens", "actual_cost_usd",
                "estimated_cost_usd", "parent_session_id", "profile_name", "archived",
                "billing_provider",
            ]
            select = [c for c in wanted if c in cols]
            order = "last_activity_at" if "last_activity_at" in cols else "started_at"

            c = conn.cursor()
            c.execute(f"SELECT {', '.join(select)} FROM sessions ORDER BY {order} DESC LIMIT 500")
            for record in c.fetchall():
                r = dict(zip(select, record))
                if r.get("archived"):
                    continue

                # Hermes stores seconds as REAL; everything here is milliseconds.
                started = float(r.get("started_at") or 0) * 1000
                ended = float(r.get("ended_at") or r.get("last_activity_at") or 0) * 1000
                duration_s = max(0.0, (ended - started) / 1000.0) if (started and ended) else 0.0

                tok_out = int(r.get("output_tokens") or 0)
                tok_reason = int(r.get("reasoning_tokens") or 0)
                generated = tok_out + tok_reason
                tps = round(generated / duration_s, 1) if (duration_s > 0.5 and generated) else 0.0

                directory = r.get("cwd") or str(db_path.parent)
                cost = r.get("actual_cost_usd")
                if cost in (None, 0):
                    cost = r.get("estimated_cost_usd") or 0.0

                rows.append({
                    "id": str(r.get("id")),
                    "harness": "hermes",
                    "title": r.get("title") or r.get("display_name") or "Hermes session",
                    "directory": sanitize_path(directory),
                    "folder": Path(directory).name if directory else "hermes",
                    "model": r.get("model") or "unknown",
                    "provider": r.get("billing_provider") or "hermes",
                    "branch": r.get("git_branch"),
                    "profile": r.get("profile_name"),
                    "channel": r.get("source") or "cli",
                    "is_subagent": bool(r.get("parent_session_id")),
                    "date_str": datetime.fromtimestamp(started / 1000).strftime("%Y-%m-%d %H:%M") if started else "Recent",
                    "time_created": int(started),
                    "duration_s": round(duration_s, 1),
                    "tokens_input": int(r.get("input_tokens") or 0),
                    "tokens_output": tok_out,
                    "tokens_reasoning": tok_reason,
                    "tokens_cache_read": int(r.get("cache_read_tokens") or 0),
                    "tokens_cache_write": int(r.get("cache_write_tokens") or 0),
                    "tokens_total": int(r.get("input_tokens") or 0) + generated,
                    "cost": round(float(cost or 0.0), 6),
                    "message_count": int(r.get("message_count") or 0),
                    "tps": tps,
                    "peak_tps": tps,
                    "has_tool_calls": bool(r.get("tool_call_count") or 0),
                    "source_path": sanitize_path(str(db_path)),
                })
        except Exception:
            continue
        finally:
            try:
                conn.close()
            except Exception:
                pass

    rows.sort(key=lambda x: x.get("time_created") or 0, reverse=True)
    _HERMES_CACHE["at"] = now
    _HERMES_CACHE["rows"] = rows
    return rows


def get_hermes_session_detail(session_id):
    for db_path in hermes_state_dbs():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except Exception:
            continue
        try:
            mcols = _table_columns(conn, "messages")
            if not mcols:
                continue
            c = conn.cursor()
            c.execute("SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,))
            if not c.fetchone():
                continue

            base = next((r for r in get_hermes_sessions() if r["id"] == session_id), None)
            if not base:
                continue

            sel = [x for x in ("role", "content", "timestamp", "token_count", "tool_name",
                               "finish_reason", "reasoning") if x in mcols]
            where = "WHERE session_id = ?"
            if "active" in mcols:
                where += " AND active = 1"
            c.execute(f"SELECT {', '.join(sel)} FROM messages {where} ORDER BY timestamp ASC", (session_id,))

            messages = []
            for rec in c.fetchall():
                m = dict(zip(sel, rec))
                ts = float(m.get("timestamp") or 0) * 1000
                parts = []
                if m.get("reasoning"):
                    parts.append({"type": "reasoning", "content": str(m["reasoning"])[:4000]})
                if m.get("tool_name"):
                    parts.append({"type": "tool", "tool": m["tool_name"], "status": "completed",
                                  "input": {}, "output": str(m.get("content") or "")[:2000]})
                elif m.get("content"):
                    parts.append({"type": "text", "content": str(m["content"])})

                messages.append({
                    "id": f"{session_id}:{len(messages)}",
                    "role": m.get("role") or "assistant",
                    "time_created": int(ts),
                    "date_str": datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S") if ts else "",
                    "duration_s": 0.0,
                    "tokens_input": 0,
                    "tokens_output": int(m.get("token_count") or 0),
                    "tokens_reasoning": 0,
                    "total_tokens": int(m.get("token_count") or 0),
                    "tps": 0.0,
                    "parts": parts,
                })

            detail = dict(base)
            detail["messages"] = messages
            detail["avg_tps"] = base.get("tps", 0.0)
            return detail
        except Exception:
            continue
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return None


# ============================================================
# ADDITIONAL LOCAL AGENTS AND CHAT APPS
#
# Six more tools that keep their history on disk. Each reader is deliberately
# defensive: these formats belong to other projects and change without notice,
# so a reader that cannot make sense of a file returns None and the source is
# simply reported as empty rather than taking the sidebar down with it.
# ============================================================

def _session_record(**kw):
    """Every source returns the same shape, so the rest of the app does not
    care which tool a row came from."""
    created = int(kw.get("time_created") or 0)
    out_tok = int(kw.get("tokens_output") or 0)
    reasoning = int(kw.get("tokens_reasoning") or 0)
    duration = float(kw.get("duration_s") or 0.0)
    generated = out_tok + reasoning
    tps = round(generated / duration, 1) if duration > 0.5 and generated else 0.0
    directory = kw.get("directory") or ""
    return {
        "id": kw["id"],
        "harness": kw["harness"],
        "title": (kw.get("title") or kw["harness"] + " session")[:120],
        "directory": sanitize_path(directory),
        "folder": Path(directory).name if directory else kw["harness"],
        "model": kw.get("model") or "unknown",
        "provider": kw.get("provider") or kw["harness"],
        "branch": kw.get("branch"),
        "version": kw.get("version"),
        "date_str": datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d %H:%M") if created else "Recent",
        "time_created": created,
        "duration_s": round(duration, 1),
        "tokens_input": int(kw.get("tokens_input") or 0),
        "tokens_output": out_tok,
        "tokens_reasoning": reasoning,
        "tokens_cache_read": int(kw.get("tokens_cache_read") or 0),
        "tokens_cache_write": int(kw.get("tokens_cache_write") or 0),
        "tokens_total": int(kw.get("tokens_input") or 0) + generated,
        "cost": 0.0,
        "message_count": int(kw.get("message_count") or 0),
        "tps": tps,
        "peak_tps": tps,
        "has_tool_calls": bool(kw.get("has_tool_calls")),
        "source_path": sanitize_path(str(kw.get("source_path") or "")),
    }


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _read_jsonl(path, limit=20000):
    rows = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[:limit]:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return rows


def _ms(value):
    """These tools variously store seconds, milliseconds or ISO strings."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            return int(v)          # already milliseconds
        if v > 1e9:
            return int(v * 1000)   # seconds
        return int(v)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return 0
    return 0


# ------------------------------------------------------------
# Cline and Roo Code
# Both are VS Code extensions sharing a task layout:
#   tasks/<taskId>/api_conversation_history.json  - the model exchange
#   tasks/<taskId>/ui_messages.json               - what the panel showed,
#                                                   including token counts
# ------------------------------------------------------------

def _cline_like_roots(kind):
    h = _home()
    app = h / "Library" / "Application Support"
    ext = "saoudrizwan.claude-dev" if kind == "cline" else "rooveterinaryinc.roo-cline"
    roots = [
        app / "Code" / "User" / "globalStorage" / ext / "tasks",
        app / "Code - Insiders" / "User" / "globalStorage" / ext / "tasks",
        app / "Cursor" / "User" / "globalStorage" / ext / "tasks",
        app / "VSCodium" / "User" / "globalStorage" / ext / "tasks",
        h / ".vscode-server" / "data" / "User" / "globalStorage" / ext / "tasks",
    ]
    local = os.environ.get("APPDATA")
    if local:
        roots.append(Path(local) / "Code" / "User" / "globalStorage" / ext / "tasks")
    xdg_cfg = h / ".config"
    roots.append(xdg_cfg / "Code" / "User" / "globalStorage" / ext / "tasks")
    return [r for r in roots if r.is_dir()]


def parse_cline_task(task_dir, harness):
    api = _read_json(task_dir / "api_conversation_history.json")
    ui = _read_json(task_dir / "ui_messages.json")
    if not isinstance(api, list) and not isinstance(ui, list):
        return None

    tok_in = tok_out = tok_cache_read = tok_cache_write = 0
    first_ts = last_ts = None
    title = None
    model = None
    has_tools = False
    messages = 0

    for ev in (ui if isinstance(ui, list) else []):
        if not isinstance(ev, dict):
            continue
        ts = _ms(ev.get("ts"))
        if ts:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        say = ev.get("say")
        if say == "api_req_started":
            # The panel stores this payload as a JSON string.
            info = ev.get("text")
            if isinstance(info, str):
                try:
                    info = json.loads(info)
                except Exception:
                    info = None
            if isinstance(info, dict):
                tok_in += int(info.get("tokensIn") or 0)
                tok_out += int(info.get("tokensOut") or 0)
                tok_cache_read += int(info.get("cacheReads") or 0)
                tok_cache_write += int(info.get("cacheWrites") or 0)
                model = info.get("model") or model
        elif say == "text" and title is None and isinstance(ev.get("text"), str):
            t = ev["text"].strip()
            if t and not t.startswith("<"):
                title = t.splitlines()[0]
        if ev.get("type") in ("say", "ask"):
            messages += 1

    for msg in (api if isinstance(api, list) else []):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "tool_use":
                        has_tools = True
                    if title is None and part.get("type") == "text" and msg.get("role") == "user":
                        t = (part.get("text") or "").strip()
                        if t and not t.startswith("<"):
                            title = t.splitlines()[0]

    if not messages and not tok_out:
        return None

    meta = _read_json(task_dir / "task_metadata.json") or {}
    cwd = None
    if isinstance(meta, dict):
        cwd = meta.get("cwd") or meta.get("workspace") or meta.get("workspacePath")
        files = meta.get("files_in_context") or meta.get("filesInContext")
        if isinstance(files, list) and files:
            has_tools = True

    if not first_ts:
        try:
            first_ts = int(task_dir.stat().st_mtime * 1000)
        except Exception:
            first_ts = 0
        last_ts = first_ts

    return _session_record(
        id=f"{harness}_{task_dir.name}",
        harness=harness,
        title=title or f"{'Cline' if harness == 'cline' else 'Roo Code'} task",
        directory=cwd or "",
        model=model or "local",
        provider=harness,
        time_created=first_ts,
        duration_s=max(0.0, ((last_ts or 0) - (first_ts or 0)) / 1000.0),
        tokens_input=tok_in,
        tokens_output=tok_out,
        tokens_cache_read=tok_cache_read,
        tokens_cache_write=tok_cache_write,
        message_count=messages,
        has_tool_calls=has_tools,
        source_path=task_dir,
    )


def _get_cline_like_sessions(kind, limit=400):
    rows = []
    for root in _cline_like_roots(kind):
        try:
            dirs = sorted(root.iterdir(), key=lambda d: d.stat().st_mtime if d.exists() else 0, reverse=True)
        except Exception:
            continue
        for task_dir in dirs[:limit]:
            if not task_dir.is_dir():
                continue
            try:
                rec = parse_cline_task(task_dir, kind)
            except Exception:
                rec = None
            if rec:
                rows.append(rec)
    return rows


_CLINE_CACHE = {"at": 0.0, "rows": None}
_ROO_CACHE = {"at": 0.0, "rows": None}


def get_cline_sessions():
    now = time.time()
    if _CLINE_CACHE["rows"] is not None and (now - _CLINE_CACHE["at"]) < 60:
        return _CLINE_CACHE["rows"]
    rows = _get_cline_like_sessions("cline")
    _CLINE_CACHE.update({"at": now, "rows": rows})
    return rows


def get_roo_sessions():
    now = time.time()
    if _ROO_CACHE["rows"] is not None and (now - _ROO_CACHE["at"]) < 60:
        return _ROO_CACHE["rows"]
    rows = _get_cline_like_sessions("roo")
    _ROO_CACHE.update({"at": now, "rows": rows})
    return rows


# ------------------------------------------------------------
# Zed
# The built-in agent keeps threads in a SQLite database. Column names have
# moved around between releases, so columns are discovered rather than assumed
# and the JSON blob is only read if it is actually there.
# ------------------------------------------------------------

def zed_thread_dbs():
    h = _home()
    candidates = [
        h / "Library" / "Application Support" / "Zed" / "threads" / "threads.db",
        h / ".local" / "share" / "zed" / "threads" / "threads.db",
        h / ".config" / "zed" / "threads" / "threads.db",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Zed" / "threads" / "threads.db")
    return [c for c in candidates if c.exists()]


def get_zed_sessions(limit=400):
    rows = []
    for db_path in zed_thread_dbs():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except Exception:
            continue
        try:
            cols = _table_columns(conn, "threads")
            if not cols:
                continue
            id_col = "id" if "id" in cols else next(iter(cols), None)
            wanted = [c for c in ("id", "summary", "title", "updated_at", "created_at", "data_type", "data") if c in cols]
            if id_col and id_col not in wanted:
                wanted.insert(0, id_col)
            order = "updated_at" if "updated_at" in cols else id_col
            c = conn.cursor()
            c.execute(f"SELECT {', '.join(wanted)} FROM threads ORDER BY {order} DESC LIMIT ?", (limit,))
            for record in c.fetchall():
                r = dict(zip(wanted, record))
                created = _ms(r.get("updated_at") or r.get("created_at"))
                title = r.get("summary") or r.get("title")
                msg_count = 0
                model = None
                blob = r.get("data")
                if isinstance(blob, (bytes, str)):
                    try:
                        text = blob.decode("utf-8", "ignore") if isinstance(blob, bytes) else blob
                        payload = json.loads(text)
                        msgs = payload.get("messages") if isinstance(payload, dict) else None
                        if isinstance(msgs, list):
                            msg_count = len(msgs)
                        if isinstance(payload, dict):
                            model = (payload.get("model") or {}).get("model") if isinstance(payload.get("model"), dict) else payload.get("model")
                            title = title or payload.get("summary")
                    except Exception:
                        pass
                rows.append(_session_record(
                    id=f"zed_{r.get(id_col)}",
                    harness="zed",
                    title=title or "Zed thread",
                    model=model or "local",
                    provider="zed",
                    time_created=created,
                    message_count=msg_count,
                    source_path=db_path,
                ))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return rows


# ------------------------------------------------------------
# Crush (charmbracelet/crush)
# One SQLite database per project at <project>/.crush/crush.db. Crush keeps
# an index of every project it has opened in <data>/crush/projects.json
# (internal/projects/projects.go), where <data> is $CRUSH_GLOBAL_DATA,
# $XDG_DATA_HOME, %LOCALAPPDATA% or ~/.local/share (internal/config/load.go).
# sessions carries token totals; messages.parts is a JSON list of
# {"type", "data"} parts. Timestamps are Unix seconds.
# ------------------------------------------------------------

def _crush_data_dir():
    env = os.environ.get("CRUSH_GLOBAL_DATA")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "crush"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or (_home() / "AppData" / "Local")) / "crush"
    return _home() / ".local" / "share" / "crush"


def crush_dbs():
    found = []
    index = _read_json(_crush_data_dir() / "projects.json")
    for p in ((index or {}).get("projects") or []) if isinstance(index, dict) else []:
        if not isinstance(p, dict):
            continue
        for d in (p.get("data_dir"), os.path.join(p.get("path") or "", ".crush")):
            if d and (Path(d) / "crush.db").exists():
                found.append(Path(d) / "crush.db")
                break
    local = Path.cwd() / ".crush" / "crush.db"
    if local.exists():
        found.append(local)
    seen, uniq = set(), []
    for f in found:
        key = str(f.resolve())
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


_CRUSH_CACHE = {"at": 0.0, "rows": None}


def get_crush_sessions(limit=400):
    now = time.time()
    if _CRUSH_CACHE["rows"] is not None and (now - _CRUSH_CACHE["at"]) < 45:
        return _CRUSH_CACHE["rows"]
    rows = []
    for db in crush_dbs():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        except Exception:
            continue
        try:
            cols = _table_columns(conn, "sessions")
            if "id" not in cols:
                continue
            c = conn.cursor()
            want = [k for k in ("id", "parent_session_id", "title", "message_count", "prompt_tokens",
                                "completion_tokens", "cost", "created_at", "updated_at") if k in cols]
            c.execute(f"SELECT {', '.join(want)} FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,))
            sessions = [dict(zip(want, r)) for r in c.fetchall()]

            # Generation time and model come from the assistant messages:
            # finished_at - created_at is how long each reply took.
            mcols = _table_columns(conn, "messages")
            per = {}
            if {"session_id", "role", "created_at", "finished_at"} <= mcols:
                sel = ["session_id", "role", "created_at", "finished_at", "parts"]
                sel += [k for k in ("model", "provider") if k in mcols]
                c.execute(f"SELECT {', '.join(sel)} FROM messages")
                for r in c.fetchall():
                    m = dict(zip(sel, r))
                    agg = per.setdefault(m["session_id"], {"gen_s": 0.0, "tools": False, "model": None, "provider": None})
                    if m["role"] == "assistant":
                        if m.get("finished_at") and m.get("created_at") and m["finished_at"] >= m["created_at"]:
                            agg["gen_s"] += m["finished_at"] - m["created_at"]
                        agg["model"] = m.get("model") or agg["model"]
                        agg["provider"] = m.get("provider") or agg["provider"]
                        if '"tool_call"' in (m.get("parts") or ""):
                            agg["tools"] = True
            for s in sessions:
                agg = per.get(s["id"], {})
                rows.append(_session_record(
                    id=f"crush_{s['id']}",
                    harness="crush",
                    title=s.get("title") or "Crush session",
                    directory=str(db.parent.parent),
                    model=agg.get("model"),
                    provider=agg.get("provider") or "crush",
                    time_created=_ms(s.get("created_at")),
                    duration_s=agg.get("gen_s") or 0.0,
                    tokens_input=s.get("prompt_tokens") or 0,
                    tokens_output=s.get("completion_tokens") or 0,
                    message_count=s.get("message_count") or 0,
                    has_tool_calls=agg.get("tools"),
                    source_path=db,
                ))
        except Exception:
            pass
        finally:
            conn.close()
    _CRUSH_CACHE.update(at=now, rows=rows)
    return rows


# ------------------------------------------------------------
# AnythingLLM
# Desktop storage (docs.anythingllm.com/installation-desktop/storage):
#   macOS   ~/Library/Application Support/anythingllm-desktop/storage/anythingllm.db
#   Linux   ~/.config/anythingllm-desktop/storage/anythingllm.db
#   Windows %APPDATA%/anythingllm-desktop/storage/anythingllm.db
# workspace_chats holds one prompt/response pair per row; response is a JSON
# string whose "metrics" has prompt_tokens, completion_tokens, duration (s)
# and model (server/utils/helpers/chat/LLMPerformanceMonitor.js). Threads
# group chats; unthreaded chats belong to the workspace itself. Prisma
# stores DateTime as epoch milliseconds.
# ------------------------------------------------------------

def anythingllm_dbs():
    h = _home()
    cands = [h / "Library" / "Application Support" / "anythingllm-desktop" / "storage" / "anythingllm.db",
             h / ".config" / "anythingllm-desktop" / "storage" / "anythingllm.db"]
    if os.environ.get("APPDATA"):
        cands.append(Path(os.environ["APPDATA"]) / "anythingllm-desktop" / "storage" / "anythingllm.db")
    if os.environ.get("STORAGE_DIR"):
        cands.append(Path(os.environ["STORAGE_DIR"]) / "anythingllm.db")
    return [c for c in cands if c.exists()]


_ALLM_CACHE = {"at": 0.0, "rows": None}


def get_anythingllm_sessions(limit=2000):
    now = time.time()
    if _ALLM_CACHE["rows"] is not None and (now - _ALLM_CACHE["at"]) < 45:
        return _ALLM_CACHE["rows"]
    rows = []
    for db in anythingllm_dbs():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        except Exception:
            continue
        try:
            if "response" not in _table_columns(conn, "workspace_chats"):
                continue
            c = conn.cursor()
            c.execute("SELECT id, name, chatProvider, chatModel FROM workspaces")
            workspaces = {r[0]: {"name": r[1], "provider": r[2], "model": r[3]} for r in c.fetchall()}
            threads = {}
            if _table_columns(conn, "workspace_threads"):
                c.execute("SELECT id, name FROM workspace_threads")
                threads = {r[0]: r[1] for r in c.fetchall()}
            c.execute("SELECT workspaceId, thread_id, response, createdAt FROM workspace_chats "
                      "ORDER BY createdAt DESC LIMIT ?", (limit,))
            groups = {}
            for ws_id, thread_id, response, created in c.fetchall():
                key = (ws_id, thread_id)
                g = groups.setdefault(key, {"first": None, "turns": 0, "in": 0, "out": 0, "dur": 0.0, "model": None, "provider": None})
                ts = _ms(created)
                g["first"] = ts if g["first"] is None else min(g["first"], ts)
                g["turns"] += 1
                try:
                    metrics = (json.loads(response or "{}") or {}).get("metrics") or {}
                except Exception:
                    metrics = {}
                g["in"] += int(metrics.get("prompt_tokens") or 0)
                g["out"] += int(metrics.get("completion_tokens") or 0)
                g["dur"] += float(metrics.get("duration") or 0.0)
                g["model"] = g["model"] or metrics.get("model")
                g["provider"] = g["provider"] or metrics.get("provider")
            for (ws_id, thread_id), g in groups.items():
                ws = workspaces.get(ws_id, {})
                rows.append(_session_record(
                    id=f"anythingllm_{ws_id}_{thread_id or 'main'}",
                    harness="anythingllm",
                    title=threads.get(thread_id) or ws.get("name") or "AnythingLLM chat",
                    model=g["model"] or ws.get("model"),
                    provider=g["provider"] or ws.get("provider") or "anythingllm",
                    time_created=g["first"],
                    duration_s=g["dur"],
                    tokens_input=g["in"],
                    tokens_output=g["out"],
                    message_count=g["turns"] * 2,
                    source_path=db,
                ))
        except Exception:
            pass
        finally:
            conn.close()
    _ALLM_CACHE.update(at=now, rows=rows)
    return rows


# ------------------------------------------------------------
# Open WebUI
# SQLite at $DATA_DIR/webui.db (backend/open_webui/env.py). Newer releases
# write one row per message to chat_message, with model_id and a usage JSON
# normalised to input_tokens/output_tokens but keeping the provider's own
# keys (models/chat_messages.py). Older ones only have the chat table, whose
# JSON "chat" column holds history.messages. Encrypted (sqlcipher) or
# Postgres installs are not readable and are skipped.
# ------------------------------------------------------------

def openwebui_dbs():
    h = _home()
    cands = [h / ".open-webui" / "webui.db", h / "open-webui" / "backend" / "data" / "webui.db",
             h / ".local" / "share" / "open-webui" / "webui.db"]
    if os.environ.get("DATA_DIR"):
        cands.insert(0, Path(os.environ["DATA_DIR"]) / "webui.db")
    # pip installs keep data inside the package.
    for lib in (h / ".local" / "lib", Path("/opt/homebrew/lib"), Path("/usr/local/lib")):
        try:
            cands.extend(lib.glob("python3*/site-packages/open_webui/data/webui.db"))
        except Exception:
            pass
    seen, out = set(), []
    for c in cands:
        if c.exists() and str(c) not in seen:
            seen.add(str(c))
            out.append(c)
    return out


def _owui_usage(u):
    if isinstance(u, str):
        try:
            u = json.loads(u)
        except Exception:
            u = None
    if not isinstance(u, dict):
        return 0, 0
    tin = u.get("input_tokens", u.get("prompt_tokens", u.get("prompt_eval_count", u.get("prompt_n"))))
    tout = u.get("output_tokens", u.get("completion_tokens", u.get("eval_count", u.get("predicted_n"))))
    return int(tin or 0), int(tout or 0)


_OWUI_CACHE = {"at": 0.0, "rows": None}


def get_openwebui_sessions(limit=400):
    now = time.time()
    if _OWUI_CACHE["rows"] is not None and (now - _OWUI_CACHE["at"]) < 45:
        return _OWUI_CACHE["rows"]
    rows = []
    for db in openwebui_dbs():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")  # fails when encrypted
        except Exception:
            continue
        try:
            if "chat" not in _table_columns(conn, "chat"):
                continue
            c = conn.cursor()
            c.execute("SELECT id, title, chat, created_at, updated_at FROM chat ORDER BY updated_at DESC LIMIT ?", (limit,))
            chats = c.fetchall()
            per_msg = {}
            if {"chat_id", "usage"} <= _table_columns(conn, "chat_message"):
                c.execute("SELECT chat_id, role, model_id, usage FROM chat_message")
                for chat_id, role, model_id, usage in c.fetchall():
                    agg = per_msg.setdefault(chat_id, {"n": 0, "in": 0, "out": 0, "model": None})
                    agg["n"] += 1
                    tin, tout = _owui_usage(usage)
                    agg["in"] += tin
                    agg["out"] += tout
                    if role == "assistant" and model_id:
                        agg["model"] = model_id
            for chat_id, title, blob, created, updated in chats:
                agg = per_msg.get(chat_id)
                model = None
                if agg is None:
                    agg = {"n": 0, "in": 0, "out": 0, "model": None}
                    try:
                        data = json.loads(blob) if isinstance(blob, str) else (blob or {})
                    except Exception:
                        data = {}
                    msgs = ((data.get("history") or {}).get("messages") or {}).values() if isinstance(data, dict) else []
                    for m in msgs:
                        if not isinstance(m, dict):
                            continue
                        agg["n"] += 1
                        tin, tout = _owui_usage(m.get("usage") or (m.get("info") or {}).get("usage"))
                        agg["in"] += tin
                        agg["out"] += tout
                        if m.get("role") == "assistant" and m.get("model"):
                            agg["model"] = m["model"]
                    if not agg["model"] and isinstance(data, dict) and data.get("models"):
                        model = (data.get("models") or [None])[0]
                rows.append(_session_record(
                    id=f"openwebui_{chat_id}",
                    harness="openwebui",
                    title=title or "Open WebUI chat",
                    model=agg["model"] or model,
                    provider="openwebui",
                    time_created=_ms(created),
                    tokens_input=agg["in"],
                    tokens_output=agg["out"],
                    message_count=agg["n"],
                    source_path=db,
                ))
        except Exception:
            pass
        finally:
            conn.close()
    _OWUI_CACHE.update(at=now, rows=rows)
    return rows


# ------------------------------------------------------------
# Goose
# Up to 1.10 each session was a .jsonl file; newer builds import those into
# sessions.db. Both are read, and the database wins when a session appears in
# both so upgrading does not double-count.
# ------------------------------------------------------------

def _goose_dirs():
    h = _home()
    xdg = Path(os.environ.get("XDG_DATA_HOME", h / ".local" / "share"))
    out = [xdg / "goose", h / ".local" / "share" / "goose"]
    local = os.environ.get("APPDATA")
    if local:
        out.append(Path(local) / "goose")
    # XDG_DATA_HOME often points at ~/.local/share, so the same directory can
    # appear twice and every session would be counted twice.
    seen, uniq = set(), []
    for d in out:
        try:
            key = d.resolve()
        except Exception:
            key = d
        if d.is_dir() and key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def get_goose_sessions(limit=400):
    rows = []
    seen = set()

    for base in _goose_dirs():
        db = base / "sessions.db"
        if db.exists():
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            except Exception:
                conn = None
            if conn:
                try:
                    for table in ("sessions", "session"):
                        cols = _table_columns(conn, table)
                        if not cols or "id" not in cols:
                            continue
                        wanted = [c for c in ("id", "name", "description", "working_dir", "cwd",
                                              "created_at", "updated_at", "message_count",
                                              "total_tokens", "input_tokens", "output_tokens") if c in cols]
                        order = "updated_at" if "updated_at" in cols else "id"
                        c = conn.cursor()
                        c.execute(f"SELECT {', '.join(wanted)} FROM {table} ORDER BY {order} DESC LIMIT ?", (limit,))
                        for record in c.fetchall():
                            r = dict(zip(wanted, record))
                            sid = str(r.get("id"))
                            seen.add(sid)
                            rows.append(_session_record(
                                id=f"goose_{sid}",
                                harness="goose",
                                title=r.get("description") or r.get("name") or "Goose session",
                                directory=r.get("working_dir") or r.get("cwd") or "",
                                model="local",
                                provider="goose",
                                time_created=_ms(r.get("created_at") or r.get("updated_at")),
                                duration_s=max(0.0, (_ms(r.get("updated_at")) - _ms(r.get("created_at"))) / 1000.0),
                                tokens_input=r.get("input_tokens") or 0,
                                tokens_output=r.get("output_tokens") or 0,
                                message_count=r.get("message_count") or 0,
                                source_path=db,
                            ))
                        break
                except Exception:
                    pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

        sess_dir = base / "sessions"
        if sess_dir.is_dir():
            try:
                files = sorted(sess_dir.glob("*.jsonl"),
                               key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True)
            except Exception:
                files = []
            for f in files[:limit]:
                if f.stem in seen:
                    continue
                events = _read_jsonl(f)
                if not events:
                    continue
                first_ts = last_ts = None
                msgs = 0
                cwd = None
                desc = None
                tok_in = tok_out = 0
                has_tools = False
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    if "working_dir" in ev or "description" in ev:
                        cwd = ev.get("working_dir") or cwd
                        desc = ev.get("description") or desc
                    ts = _ms(ev.get("created") or ev.get("timestamp") or ev.get("created_at"))
                    if ts:
                        first_ts = ts if first_ts is None else min(first_ts, ts)
                        last_ts = ts if last_ts is None else max(last_ts, ts)
                    role = ev.get("role")
                    if role in ("user", "assistant"):
                        msgs += 1
                    content = ev.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") in ("toolRequest", "toolResponse", "tool_use"):
                                has_tools = True
                    usage = ev.get("usage") or (ev.get("metadata") or {}).get("usage") if isinstance(ev.get("metadata"), dict) else ev.get("usage")
                    if isinstance(usage, dict):
                        tok_in += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                        tok_out += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                if not msgs:
                    continue
                if not first_ts:
                    first_ts = int(f.stat().st_mtime * 1000)
                    last_ts = first_ts
                rows.append(_session_record(
                    id=f"goose_{f.stem}",
                    harness="goose",
                    title=desc or "Goose session",
                    directory=cwd or "",
                    model="local",
                    provider="goose",
                    time_created=first_ts,
                    duration_s=max(0.0, ((last_ts or 0) - (first_ts or 0)) / 1000.0),
                    tokens_input=tok_in,
                    tokens_output=tok_out,
                    message_count=msgs,
                    has_tool_calls=has_tools,
                    source_path=f,
                ))
    return rows


# ------------------------------------------------------------
# LM Studio
# Chats are single JSON files. LM Studio explicitly does not promise this
# shape, so every field is probed in a few likely places and anything
# unrecognised is skipped rather than guessed at.
# ------------------------------------------------------------

def _lmstudio_dirs():
    h = _home()
    out = [h / ".lmstudio" / "conversations", h / ".cache" / "lm-studio" / "conversations"]
    local = os.environ.get("APPDATA")
    if local:
        out.append(Path(local) / "LM Studio" / "conversations")
    return [d for d in out if d.is_dir()]


def get_lmstudio_sessions(limit=400):
    rows = []
    for base in _lmstudio_dirs():
        try:
            files = sorted(base.rglob("*.json"),
                           key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True)
        except Exception:
            continue
        for f in files[:limit]:
            payload = _read_json(f)
            if not isinstance(payload, dict):
                continue
            msgs = payload.get("messages")
            if not isinstance(msgs, list):
                continue
            model = payload.get("modelIdentifier") or payload.get("model") or payload.get("lastUsedModel")
            if isinstance(model, dict):
                model = model.get("identifier") or model.get("path") or model.get("name")
            tok_out = 0
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                stats = m.get("genInfo") or m.get("stats") or {}
                if isinstance(stats, dict):
                    tok_out += int(stats.get("predictedTokensCount") or stats.get("tokenCount") or 0)
            created = _ms(payload.get("createdAt") or payload.get("created_at"))
            if not created:
                try:
                    created = int(f.stat().st_mtime * 1000)
                except Exception:
                    created = 0
            rows.append(_session_record(
                id=f"lmstudio_{f.stem}",
                harness="lmstudio",
                title=payload.get("name") or payload.get("title") or "LM Studio chat",
                model=model or "local",
                provider="lmstudio",
                time_created=created,
                tokens_output=tok_out,
                message_count=len(msgs),
                source_path=f,
            ))
    return rows


# ------------------------------------------------------------
# Jan
# One folder per thread: thread.json holds the metadata and the chosen model,
# messages.jsonl holds the exchange.
# ------------------------------------------------------------

def _jan_dirs():
    h = _home()
    out = [h / "jan" / "threads",
           h / "Library" / "Application Support" / "jan" / "threads",
           h / ".jan" / "threads"]
    local = os.environ.get("APPDATA")
    if local:
        out.append(Path(local) / "jan" / "threads")
    return [d for d in out if d.is_dir()]


def get_jan_sessions(limit=400):
    rows = []
    for base in _jan_dirs():
        try:
            dirs = sorted([d for d in base.iterdir() if d.is_dir()],
                          key=lambda d: d.stat().st_mtime, reverse=True)
        except Exception:
            continue
        for d in dirs[:limit]:
            meta = _read_json(d / "thread.json")
            msgs = _read_jsonl(d / "messages.jsonl")
            if not isinstance(meta, dict) and not msgs:
                continue
            meta = meta if isinstance(meta, dict) else {}
            model = meta.get("model")
            if isinstance(model, dict):
                model = model.get("id") or model.get("name")
            if not model:
                assistants = meta.get("assistants")
                if isinstance(assistants, list) and assistants:
                    a = assistants[0]
                    if isinstance(a, dict):
                        m = a.get("model")
                        model = m.get("id") if isinstance(m, dict) else m
            created = _ms(meta.get("created") or meta.get("created_at") or meta.get("updated"))
            first_ts = last_ts = None
            for m in msgs:
                ts = _ms(m.get("created_at") or m.get("created") or m.get("createdAt"))
                if ts:
                    first_ts = ts if first_ts is None else min(first_ts, ts)
                    last_ts = ts if last_ts is None else max(last_ts, ts)
            if not created:
                created = first_ts or 0
            if not created:
                try:
                    created = int(d.stat().st_mtime * 1000)
                except Exception:
                    created = 0
            rows.append(_session_record(
                id=f"jan_{d.name}",
                harness="jan",
                title=(meta.get("title") or meta.get("name") or "Jan thread"),
                model=model or "local",
                provider="jan",
                time_created=created,
                duration_s=max(0.0, ((last_ts or 0) - (first_ts or 0)) / 1000.0) if first_ts and last_ts else 0.0,
                message_count=len(msgs),
                source_path=d,
            ))
    return rows


# ============================================================
# APP CATALOG & DETECTION
# Only the tools actually present on this machine belong in the sidebar. The
# catalog lists everything known; detection decides what is shown. "detected"
# means artifacts exist on disk; "readable" means this app can also parse them,
# which is a smaller set - the two are reported separately rather than
# pretending a detected app is already understood.
# ============================================================

def _home():
    return Path.home()


def _first_existing(candidates):
    for c in candidates:
        try:
            pc = Path(c).expanduser()
        except Exception:
            continue
        if pc.exists():
            return pc
    return None


def _count_glob(root, pattern, cap=5000):
    try:
        n = 0
        for _ in Path(root).glob(pattern):
            n += 1
            if n >= cap:
                break
        return n
    except Exception:
        return 0


def source_catalog():
    """Every local-agent / chat app we know how to look for."""
    h = _home()
    app = h / "Library" / "Application Support"
    xdg = Path(os.environ.get("XDG_DATA_HOME", h / ".local" / "share"))
    return [
        # --- Coding agents (readable) ---
        {"id": "opencode",  "name": "OpenCode",    "icon": "opencode", "kind": "agent", "readable": True,
         "paths": [CONFIG["opencode_db"]]},
        {"id": "openclaw",  "name": "OpenClaw",    "icon": "paw", "kind": "agent", "readable": True,
         "paths": [hh / "agents" for hh in CONFIG.get("openclaw_homes", [])] or [h / ".openclaw" / "agents"],
         "glob": "*/sessions/*.jsonl"},
        {"id": "aider",     "name": "Aider",       "icon": "squareTerminal", "kind": "agent", "readable": True,
         "paths": [h / ".aider.chat.history.md", Path.cwd() / ".aider.chat.history.md"]},
        {"id": "continue",  "name": "Continue",    "icon": "arrows", "kind": "agent", "readable": True,
         "paths": [h / ".continue" / "sessions"], "glob": "*.json"},
        {"id": "hermes",    "name": "Hermes Agent", "icon": "hermes", "kind": "agent", "readable": True,
         "paths": [hh / "state.db" for hh in resolve_hermes_homes()]
                  or [h / ".hermes" / "state.db", h / ".hermes"]},

        # --- Coding agents ---
        {"id": "goose",     "name": "Goose",       "icon": "bird", "kind": "agent", "readable": True,
         "paths": [xdg / "goose" / "sessions"], "glob": "*.jsonl"},
        {"id": "crush",     "name": "Crush",       "icon": "shapes", "kind": "agent", "readable": True,
         "paths": crush_dbs() or [_crush_data_dir() / "projects.json"]},
        {"id": "cline",     "name": "Cline",       "icon": "cline", "kind": "agent", "readable": True,
         "paths": [app / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "tasks",
                   h / ".vscode" / "globalStorage" / "saoudrizwan.claude-dev"]},
        {"id": "roo",       "name": "Roo Code",    "icon": "rabbit", "kind": "agent", "readable": True,
         "paths": [app / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "tasks"]},
        {"id": "zed",       "name": "Zed",         "icon": "zed", "kind": "agent", "readable": True,
         "paths": [app / "Zed" / "conversations", app / "Zed" / "threads"]},

        # --- Desktop chat apps ---
        {"id": "lmstudio_chat", "name": "LM Studio chats", "icon": "lmstudio", "kind": "chat", "readable": True,
         "paths": [h / ".lmstudio" / "conversations", h / ".cache" / "lm-studio" / "conversations"]},
        {"id": "openwebui", "name": "Open WebUI",  "icon": "globe", "kind": "chat", "readable": True,
         "paths": openwebui_dbs() or [h / ".open-webui" / "webui.db"]},
        {"id": "librechat", "name": "LibreChat",   "icon": "messagesSquare", "kind": "chat", "readable": False,
         "reason": "Stores chats in MongoDB, which Tach cannot read without a database driver.",
         "paths": [h / "LibreChat", app / "LibreChat"]},
        {"id": "jan",       "name": "Jan",         "icon": "atom", "kind": "chat", "readable": True,
         "paths": [h / "jan" / "threads", app / "jan" / "threads"]},
        {"id": "anythingllm", "name": "AnythingLLM", "icon": "libraryBig", "kind": "chat", "readable": True,
         "paths": anythingllm_dbs() or [app / "anythingllm-desktop" / "storage" / "anythingllm.db"]},
        {"id": "msty",      "name": "Msty",        "icon": "hexagon", "kind": "chat", "readable": False,
         "reason": "Uses a SQLite msty.db whose schema is not published; a reader needs a real install to map it.",
         "paths": [app / "Msty"]},
        {"id": "chatbox",   "name": "Chatbox",     "icon": "box", "kind": "chat", "readable": False,
         "reason": "Keeps conversations in the app's browser storage (IndexedDB/LevelDB), which cannot be read from outside it.",
         "paths": [app / "xyz.chatboxapp.app"]},
        {"id": "gpt4all",   "name": "GPT4All",     "icon": "blocks", "kind": "chat", "readable": False,
         "reason": "Its .chat files record no timestamps or token counts, so there is nothing to measure.",
         "paths": [app / "nomic.ai" / "GPT4All", h / ".config" / "nomic.ai" / "GPT4All"]},
    ]


# ============================================================
# SERVER DISCOVERY
# Default ports are only a guess: MLX servers are routinely started on :8081,
# :8082 and so on, and several engines default to the same port. Discovery
# reads what is actually listening, recognises the engine from the owning
# process, and the user can pin extra ports in the config file for anything
# discovery cannot name.
# ============================================================

CONFIG_PATH = Path(os.environ.get("TACH_CONFIG") or (Path.home() / ".config" / "tach" / "config.json"))

# Substrings of a process command line that identify the engine behind it.
_PROCESS_HINTS = (
    ("mlx", ("mlx_lm.server", "mlx_vlm.server", "mlx_lm server", "mlx_vlm server", "mlx-lm", "mlx_lm", "mlx_vlm")),
    # "ollama serve" only: the Ollama.app shell and model runners listen on
    # their own ports too, and neither is the API.
    ("ollama", ("ollama serve",)),
    ("llamacpp", ("llama-server", "llama_cpp.server")),
    ("vllm", ("vllm",)),
    ("sglang", ("sglang",)),
    ("koboldcpp", ("koboldcpp",)),
    ("lmstudio", ("lm studio", "lmstudio")),
    ("localai", ("local-ai",)),
    ("tabbyapi", ("tabbyapi",)),
    ("textgenwebui", ("text-generation-webui",)),
    ("cortex", ("cortex-server", "cortexcpp")),
)

_LOOPBACK_HOSTS = {"*", "127.0.0.1", "localhost", "0.0.0.0", "[::]", "[::1]", "::", "::1"}


def load_user_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_user_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def configured_ports():
    """{engine_id: [port, ...]} from the config file, validated."""
    raw = (load_user_config().get("servers") or {})
    out = {}
    if not isinstance(raw, dict):
        return out
    for eid, ports in raw.items():
        if not isinstance(ports, list):
            continue
        clean = [p for p in ports if isinstance(p, int) and 0 < p < 65536]
        if clean:
            out[str(eid)] = clean
    return out


def _listening_processes():
    """
    [(port, pid, command line)] for every TCP listener reachable on loopback.
    lsof ships with macOS and most Linux installs; without it discovery is
    skipped and only default and configured ports are probed.
    """
    try:
        raw = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:
        return []

    listeners = []
    pid = None
    for line in raw.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("n") and pid:
            host, _, port = line[1:].rpartition(":")
            if port.isdigit() and host in _LOOPBACK_HOSTS:
                listeners.append((int(port), pid))

    pids = sorted({p for _, p in listeners})
    commands = {}
    if pids:
        try:
            ps = subprocess.run(
                ["ps", "-o", "pid=,command=", "-p", ",".join(map(str, pids))],
                capture_output=True, text=True, timeout=2,
            ).stdout
            for row in ps.splitlines():
                head, _, cmd = row.strip().partition(" ")
                if head.isdigit():
                    commands[int(head)] = cmd.strip()
        except Exception:
            pass

    seen = set()
    out = []
    for port, p in listeners:
        if port in seen:
            continue
        seen.add(port)
        out.append((port, p, commands.get(p, "")))
    return out


def _engine_for_command(cmd):
    low = cmd.lower()
    for eid, needles in _PROCESS_HINTS:
        if any(n in low for n in needles):
            return eid
    return None


_DISCOVERY_CACHE = {"at": 0.0, "data": None}


def discover_servers(force=False):
    """
    {engine_id: [port, ...]} for engines recognised on a listening port.

    Named processes are trusted; an unnamed Python listener is claimed for MLX
    only if its /metrics answers in the MLX shape, since custom MLX servers are
    often launched as a plain script. Cached because /api/live polls often.
    """
    now = time.time()
    if not force and _DISCOVERY_CACHE["data"] is not None and (now - _DISCOVERY_CACHE["at"]) < 20:
        return _DISCOVERY_CACHE["data"]

    found = {}
    for port, _pid, cmd in _listening_processes():
        if port == PORT:
            continue
        eid = _engine_for_command(cmd)
        if eid is None and "python" in cmd.lower():
            if _probe_server(port, "/metrics", kind="mlx")[0]:
                eid = "mlx"
        if eid:
            found.setdefault(eid, []).append(port)
    for ports in found.values():
        ports.sort()

    _DISCOVERY_CACHE["at"] = now
    _DISCOVERY_CACHE["data"] = found
    return found


def hermes_gateway_running():
    """
    The Hermes gateway talks to its CLI over a Unix socket and opens no TCP
    port unless its optional API server is enabled, so a port probe reports a
    running gateway as offline. Its own state file, plus a live pid, is the
    reliable signal.
    """
    for home in resolve_hermes_homes():
        try:
            with open(home / "gateway_state.json", "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            continue
        pid = st.get("pid")
        if st.get("gateway_state") != "running" or not isinstance(pid, int):
            continue
        try:
            os.kill(pid, 0)
        except PermissionError:
            pass  # exists, owned by someone else
        except OSError:
            continue
        return {"pid": pid, "version": st.get("code_version"),
                "platforms": sorted((st.get("platforms") or {}).keys()),
                "active_agents": st.get("active_agents", 0)}
    return None


def _pid_alive(pid):
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _iso_to_epoch(value):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return None


def read_hermes_live():
    """
    What Hermes is doing right now, from the files it keeps current: the
    gateway's heartbeat and lifecycle, the CLI's active-session registry, turn
    leases (a turn in flight holds one), and per-session counters in state.db.
    None when no Hermes home exists.
    """
    homes = [hh for hh in resolve_hermes_homes() if (hh / "state.db").exists()]
    if not homes:
        return None
    home = homes[0]
    now = time.time()

    gw = hermes_gateway_running()
    gateway = None
    if gw:
        beat = _read_json(home / "state" / "gateway.heartbeat") or {}
        life = _read_json(home / "state" / "gateway.lifecycle.json") or {}
        started = life.get("start_time") or beat.get("start_time")
        beat_at = _iso_to_epoch(beat.get("updated_at"))
        tick = None
        try:
            tick = float((home / "cron" / "ticker_last_success").read_text().strip()[:18])
        except Exception:
            pass
        gateway = dict(gw)
        gateway.update({
            "uptime_s": round(now - started) if started else None,
            "heartbeat_age_s": round(now - beat_at) if beat_at else None,
            "cron_tick_age_s": round(now - tick) if tick else None,
        })

    registry = _read_json(home / "runtime" / "active_sessions.json") or {}
    live_entries = [e for e in (registry.get("entries") or [])
                    if isinstance(e, dict) and _pid_alive(e.get("pid"))]

    active, today = [], None
    try:
        conn = sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True, timeout=1)
    except Exception:
        conn = None
    if conn:
        try:
            c = conn.cursor()
            cols = _table_columns(conn, "sessions")
            leases = {}
            if _table_columns(conn, "session_turn_leases"):
                c.execute("SELECT conversation_id, acquired_at, expires_at FROM session_turn_leases WHERE expires_at > ?", (now,))
                leases = {r[0]: r[1] for r in c.fetchall()}
            has_usage = bool(_table_columns(conn, "session_model_usage"))

            want = [k for k in ("title", "source", "model", "started_at", "message_count", "tool_call_count",
                                "api_call_count", "input_tokens", "output_tokens", "cache_read_tokens",
                                "reasoning_tokens", "last_activity_at", "last_activity_description", "cwd")
                    if k in cols]
            for e in live_entries:
                sid = e.get("session_id")
                c.execute(f"SELECT {', '.join(want)} FROM sessions WHERE id = ?", (sid,))
                row = c.fetchone()
                if not row:
                    continue
                rec = dict(zip(want, row))
                endpoint = None
                if has_usage:
                    c.execute("SELECT billing_base_url FROM session_model_usage WHERE session_id = ? "
                              "ORDER BY last_seen DESC LIMIT 1", (sid,))
                    r = c.fetchone()
                    endpoint = (r[0] or "").rstrip("/") if r else None
                active.append({
                    "session_id": sid,
                    "surface": e.get("surface") or rec.get("source"),
                    "pid": e.get("pid"),
                    "title": rec.get("title"),
                    "model": rec.get("model"),
                    "cwd": sanitize_path(rec.get("cwd") or ""),
                    "endpoint": endpoint,
                    "turn_started_at": leases.get(sid),
                    "activity": rec.get("last_activity_description"),
                    "activity_age_s": round(now - rec["last_activity_at"]) if rec.get("last_activity_at") else None,
                    "age_s": round(now - rec["started_at"]) if rec.get("started_at") else None,
                    "messages": rec.get("message_count") or 0,
                    "tool_calls": rec.get("tool_call_count") or 0,
                    "api_calls": rec.get("api_call_count") or 0,
                    "input_tokens": rec.get("input_tokens") or 0,
                    "output_tokens": rec.get("output_tokens") or 0,
                    "cache_read_tokens": rec.get("cache_read_tokens") or 0,
                })

            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            agg = [k for k in ("api_call_count", "tool_call_count", "input_tokens", "output_tokens", "cache_read_tokens") if k in cols]
            c.execute(f"SELECT COUNT(*), {', '.join(f'COALESCE(SUM({k}),0)' for k in agg)} FROM sessions "
                      f"WHERE COALESCE(last_activity_at, started_at) >= ?", (midnight,))
            r = c.fetchone()
            today = {"sessions": r[0]}
            today.update(dict(zip(agg, r[1:])))
        except Exception:
            pass
        finally:
            conn.close()

    return {
        "online": bool(gateway) or bool(active),
        "details": {"gateway": gateway, "active": active, "today": today},
    }


def invalidate_discovery():
    _DISCOVERY_CACHE["data"] = None
    _CATALOG_CACHE["data"] = None


def engine_ports(eid, default=None):
    """
    Ports to probe for one engine, most trusted first: the ones the user
    pinned, then discovered ones, then the default. A port that discovery or
    config gave to a different engine is never probed as this one, so an MLX
    server on :8081 is not mistaken for LocalAI.
    """
    cfg = configured_ports()
    disc = discover_servers()
    claimed = {}
    for source in (disc, cfg):
        for other, ports in source.items():
            for p in ports:
                claimed[p] = other

    ordered = []
    for p in cfg.get(eid, []) + disc.get(eid, []) + ([default] if default else []):
        if p in ordered:
            continue
        if claimed.get(p, eid) != eid:
            continue
        ordered.append(p)
    return ordered


def server_catalog():
    """
    Local inference servers. Several share port 8080, so each probe carries the
    endpoint that identifies it rather than trusting the port alone.
    """
    return [
        {"id": "mlx",       "name": "MLX LM",        "icon": "apple", "port": 8080,  "probe": "/metrics", "kind": "mlx", "installs": ["~/.cache/huggingface/hub", "/opt/homebrew/bin/mlx_lm.server"]},
        {"id": "ollama",    "name": "Ollama",        "icon": "ollama", "port": 11434, "probe": "/api/tags", "kind": "ollama", "installs": ["/Applications/Ollama.app", "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "~/.ollama"]},
        {"id": "llamacpp",  "name": "llama.cpp",     "icon": "feather", "port": 8077,  "probe": "/props", "kind": "llamacpp", "installs": ["/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server"]},
        {"id": "lmstudio",  "name": "LM Studio",     "icon": "lmstudio", "port": 1234,  "probe": "/v1/models", "installs": ["/Applications/LM Studio.app", "~/.lmstudio"]},
        {"id": "vllm",      "name": "vLLM",          "icon": "vllm", "port": 8000,  "probe": "/v1/models", "installs": ["/opt/homebrew/bin/vllm"]},
        {"id": "jan_server", "name": "Jan server",   "icon": "atom", "port": 1337,  "probe": "/openapi.json", "kind": "jan", "installs": ["/Applications/Jan.app", "~/jan"]},
        {"id": "koboldcpp", "name": "KoboldCpp",     "icon": "book", "port": 5001,  "probe": "/api/v1/model", "kind": "kobold", "installs": ["~/koboldcpp", "/opt/homebrew/bin/koboldcpp"]},
        {"id": "textgenwebui", "name": "Text-gen WebUI", "icon": "panelsTopLeft", "port": 5000, "probe": "/v1/models", "installs": ["~/text-generation-webui"]},
        {"id": "localai",   "name": "LocalAI",       "icon": "server", "port": 8080,  "probe": "/system", "kind": "localai", "installs": ["/opt/homebrew/bin/local-ai"]},
        {"id": "sglang",    "name": "SGLang",        "icon": "zap", "port": 30000, "probe": "/v1/models", "installs": ["/opt/homebrew/bin/sglang"]},
        {"id": "cortex",    "name": "Cortex",        "icon": "brain", "port": 39281, "probe": "/v1/models", "installs": ["/Applications/Cortex.app", "~/cortexcpp"]},
        {"id": "tabbyapi",  "name": "TabbyAPI",      "icon": "cat", "port": 5000,  "probe": "/health", "kind": "tabby", "installs": ["~/tabbyAPI"]},
        {"id": "openwebui_srv", "name": "Open WebUI", "icon": "globe", "port": 3000, "probe": "/health", "kind": "health", "installs": ["~/.open-webui"]},
        {"id": "hermes_gw", "name": "Hermes gateway", "icon": "hermes",
         "port": int(os.environ.get("API_SERVER_PORT") or 8642), "probe": "/health",
         "kind": "health", "allow_tcp": True,
         "installs": ["~/.hermes", "/opt/homebrew/bin/hermes"]},
        {"id": "openclaw_gw", "name": "OpenClaw gateway", "icon": "paw", "port": None, "probe": "/health", "kind": "health", "allow_tcp": True, "installs": ["/opt/homebrew/bin/openclaw", "~/.openclaw"]},
    ]


def detect_sources():
    out = []
    for entry in source_catalog():
        found = _first_existing([p for p in entry.get("paths", []) if p])
        count = None
        if found and entry.get("glob"):
            count = _count_glob(found, entry["glob"])
        elif found and found.is_dir():
            count = _count_glob(found, "*")
        out.append({
            "id": entry["id"],
            "name": entry["name"],
            "icon": entry["icon"],
            "kind": entry["kind"],
            "readable": entry["readable"],
            "reason": entry.get("reason"),
            "detected": found is not None,
            "path": sanitize_path(str(found)) if found else None,
            "artifacts": count,
        })
    return out


def detect_servers():
    port_overrides = {"openclaw_gw": resolve_openclaw_gateway()}
    cfg = configured_ports()
    disc = discover_servers(force=True)
    out = []
    for entry in server_catalog():
        eid = entry["id"]
        default = port_overrides.get(eid, entry["port"])
        candidates = engine_ports(eid, default)
        if not candidates:
            continue
        instances = []
        for port in candidates:
            online, detail = _probe_server(
                port, entry["probe"],
                kind=entry.get("kind", "openai"),
                allow_tcp=entry.get("allow_tcp", False),
            )
            # A process named as this engine is running even if its probe
            # route differs (stock mlx_lm.server has no /metrics).
            if not online and port in disc.get(eid, []):
                online = _probe_server(port, "/v1/models")[0]
            source = ("config" if port in cfg.get(eid, [])
                      else "discovered" if port in disc.get(eid, [])
                      else "default")
            instances.append({"port": port, "online": online, "source": source, "detail": detail})
        via = "tcp"
        if eid == "hermes_gw" and not any(i["online"] for i in instances):
            gw = hermes_gateway_running()
            if gw:
                via = "socket"
                instances = [{"port": None, "online": True, "source": "state", "detail": gw}]
        # Drop the offline default when the engine was found elsewhere.
        if any(i["online"] for i in instances):
            instances = [i for i in instances if i["online"] or i["source"] == "config"]
        primary = next((i for i in instances if i["online"]), instances[0])
        install = _first_existing(entry.get("installs", []))
        out.append({
            "id": eid,
            "name": entry["name"],
            "icon": entry["icon"],
            "port": primary["port"],
            "default_port": default,
            "via": via,
            "online": primary["online"],
            "instances": [{k: i[k] for k in ("port", "online", "source")} for i in instances],
            "installed": install is not None or bool(disc.get(eid)) or via == "socket",
            "install_path": sanitize_path(str(install)) if install else None,
            "detail": primary["detail"],
        })
    return out


def _looks_like(kind, payload):
    """
    A port being open proves nothing about what is on it: :5000 is macOS
    Control Center and :8000 is often a dev server. Each probe must recognise
    its own response shape before the server is reported as running.
    """
    if not isinstance(payload, dict):
        return False
    if kind == "openai":
        data = payload.get("data")
        return payload.get("object") == "list" or isinstance(data, list)
    if kind == "ollama":
        return isinstance(payload.get("models"), list)
    if kind == "mlx":
        return any(k in payload for k in ("summary", "recent", "server", "latest"))
    if kind == "llamacpp":
        return any(k in payload for k in ("default_generation_settings", "model_path", "total_slots"))
    if kind == "localai":
        return "backends" in payload or "loaded_models" in payload
    if kind == "jan":
        return "openapi" in payload or "paths" in payload
    if kind == "tabby":
        return payload.get("status") in ("healthy", "unhealthy")
    if kind == "kobold":
        return "result" in payload
    if kind == "health":
        return True
    return False


def _probe_server(port, probe_path, kind="openai", allow_tcp=False):
    url = f"http://127.0.0.1:{port}{probe_path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Telemetry"})
        with urllib.request.urlopen(req, timeout=0.6) as resp:
            body = resp.read(8192)
            try:
                payload = json.loads(body.decode())
            except Exception:
                return False, None
            return (True, payload) if _looks_like(kind, payload) else (False, None)
    except urllib.error.HTTPError:
        # Answered but refused us. Only meaningful where the server is known to
        # require a token, so it is opt-in per entry.
        return (True, None) if allow_tcp else (False, None)
    except Exception:
        pass
    if allow_tcp:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.35):
                return True, None
        except Exception:
            pass
    return False, None


_CATALOG_CACHE = {"at": 0.0, "data": None}


def get_catalog(force=False):
    """Detection is cached briefly so a page render does not re-stat the disk."""
    now = time.time()
    if not force and _CATALOG_CACHE["data"] and (now - _CATALOG_CACHE["at"]) < 30:
        return _CATALOG_CACHE["data"]
    data = {
        "sources": detect_sources(),
        "servers": detect_servers(),
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
    }
    _CATALOG_CACHE["at"] = now
    _CATALOG_CACHE["data"] = data
    return data


def get_session_detail(session_id):
    # OpenClaw transcripts live on disk as JSONL, not in the OpenCode database.
    openclaw = get_openclaw_session_detail(session_id)
    if openclaw:
        return openclaw

    hermes = get_hermes_session_detail(session_id)
    if hermes:
        return hermes

    conn = get_db_connection()
    if not conn:
        return {"error": "Database not found"}

    c = conn.cursor()

    c.execute("SELECT id, title, directory, model, time_created, time_updated, tokens_input, tokens_output, tokens_reasoning FROM session WHERE id=?", (session_id,))
    s_row = c.fetchone()
    if not s_row:
        conn.close()
        return {"error": "Session not found"}

    sid, title, directory, model_raw, t_created, t_updated, t_in, t_out, t_reas = s_row
    folder = Path(directory).name if directory else "root"

    model_name = "unknown"
    provider_name = "unknown"
    if model_raw:
        try:
            m_obj = json.loads(model_raw)
            model_name = m_obj.get("id", "unknown")
            provider_name = m_obj.get("providerID", "unknown")
        except Exception:
            model_name = str(model_raw)

    c.execute("""
        SELECT id, time_created, data 
        FROM message 
        WHERE session_id=? 
        ORDER BY time_created ASC;
    """, (session_id,))
    msg_rows = c.fetchall()

    messages = []
    total_calc_gen_s = 0.0
    total_calc_tokens = 0

    for mid, m_time, m_data in msg_rows:
        try:
            d = json.loads(m_data)
            role = d.get("role")
            t = d.get("time", {})
            created = t.get("created", m_time)
            completed = t.get("completed")
            dur = (completed - created) / 1000.0 if (completed and created and completed > created) else 0.0

            toks = d.get("tokens", {})
            inp_t = toks.get("input", 0)
            out_t = toks.get("output", 0)
            reas_t = toks.get("reasoning", 0)
            total_gen_tok = out_t + reas_t

            turn_tps = round(total_gen_tok / dur, 1) if (dur > 0.05 and total_gen_tok > 0) else 0.0
            if turn_tps > 0 and turn_tps < 250:
                total_calc_gen_s += dur
                total_calc_tokens += total_gen_tok

            c.execute("SELECT id, data FROM part WHERE message_id=? ORDER BY time_created ASC;", (mid,))
            part_rows = c.fetchall()
            parts = []
            for pid, pdata in part_rows:
                try:
                    p = json.loads(pdata)
                    ptype = p.get("type")
                    if ptype == "text":
                        parts.append({"type": "text", "content": p.get("text", "")})
                    elif ptype == "reasoning":
                        parts.append({"type": "reasoning", "content": p.get("text", "")})
                    elif ptype == "tool":
                        state = p.get("state", {})
                        parts.append({
                            "type": "tool",
                            "tool": p.get("tool"),
                            "status": state.get("status"),
                            "input": state.get("input", {}),
                            "output": str(state.get("output", ""))[:2000],
                        })
                except Exception:
                    continue

            messages.append({
                "id": mid,
                "role": role,
                "time_created": created,
                "date_str": datetime.fromtimestamp(created / 1000).strftime("%H:%M:%S") if created else "",
                "duration_s": round(dur, 2),
                "tokens_input": inp_t,
                "tokens_output": out_t,
                "tokens_reasoning": reas_t,
                "total_tokens": inp_t + total_gen_tok,
                "tps": turn_tps,
                "parts": parts,
            })
        except Exception:
            continue

    conn.close()

    duration_s = (t_updated - t_created) / 1000.0 if (t_updated and t_created and t_updated > t_created) else 0.0
    overall_tps = round(total_calc_tokens / total_calc_gen_s, 1) if total_calc_gen_s > 0 else 0.0

    return {
        "id": sid,
        "harness": "opencode",
        "title": title or "Untitled Session",
        "directory": sanitize_path(directory),
        "folder": folder,
        "model": model_name,
        "provider": provider_name,
        "time_created": t_created,
        "date_str": datetime.fromtimestamp(t_created / 1000).strftime("%Y-%m-%d %H:%M:%S") if t_created else "",
        "duration_s": round(duration_s, 1),
        "tokens_input": t_in,
        "tokens_output": t_out,
        "tokens_reasoning": t_reas,
        "tokens_total": t_in + t_out + t_reas,
        "avg_tps": overall_tps,
        "messages": messages,
    }


def get_timeseries(query_params=None):
    harness_filter = query_params.get("harness", [""])[0].lower() if query_params else ""
    if harness_filter and harness_filter not in ["opencode", "all"]:
        return []

    conn = get_db_connection()
    if not conn:
        return {"error": "Database not found"}

    c = conn.cursor()

    start_ms, end_ms = resolve_window(query_params)

    where_sess = "WHERE time_created IS NOT NULL"
    params = []
    if start_ms and end_ms:
        where_sess += " AND time_created >= ? AND time_created <= ?"
        params = [start_ms, end_ms]
    elif start_ms:
        where_sess += " AND time_created >= ?"
        params = [start_ms]

    # Bucket size follows the span, so a custom range gets the same
    # granularity as the preset of similar length.
    span_h = ((end_ms or int(time.time() * 1000)) - start_ms) / 3.6e6 if start_ms else None
    if span_h is not None and span_h <= 6:
        fmt = "%H:%M"
    elif span_h is not None and span_h <= 72:
        fmt = "%m-%d %H:00"
    else:
        fmt = "%Y-%m-%d"

    c.execute(f"""
        SELECT 
            strftime('{fmt}', time_created / 1000, 'unixepoch', 'localtime') as day,
            COUNT(*) as session_count,
            COALESCE(SUM(tokens_input), 0) as tokens_in,
            COALESCE(SUM(tokens_output), 0) as tokens_out,
            COALESCE(SUM(tokens_reasoning), 0) as tokens_reas,
            MIN(time_created) as min_t
        FROM session 
        {where_sess}
        GROUP BY day
        ORDER BY min_t ASC;
    """, params)
    day_rows = c.fetchall()

    days = []
    for r in day_rows:
        day_str, sess_cnt, t_in, t_out, t_reas, min_t = r
        if not day_str:
            continue

        c.execute(f"""
            SELECT m.data 
            FROM message m
            WHERE strftime('{fmt}', m.time_created / 1000, 'unixepoch', 'localtime') = ?
              AND m.data LIKE '%assistant%'
              AND m.data LIKE '%completed%';
        """, (day_str,))
        msgs = c.fetchall()

        day_dur = 0.0
        day_toks = 0
        peak_tps = 0.0

        for (mdata,) in msgs:
            try:
                d = json.loads(mdata)
                t = d.get("time", {})
                cr = t.get("created")
                co = t.get("completed")
                dur = (co - cr) / 1000.0 if (co and cr and co > cr) else 0.0
                toks = d.get("tokens", {})
                gen_tok = toks.get("output", 0) + toks.get("reasoning", 0)
                if gen_tok > 0 and dur > 0.05:
                    tps = gen_tok / dur
                    if tps < 250:
                        day_dur += dur
                        day_toks += gen_tok
                        if tps > peak_tps:
                            peak_tps = tps
            except Exception:
                continue

        avg_tps = round(day_toks / day_dur, 1) if day_dur > 0 else 0.0

        days.append({
            "date": day_str,
            "sessions": sess_cnt,
            "tokens_input": t_in,
            "tokens_output": t_out,
            "tokens_reasoning": t_reas,
            "tokens_total": t_in + t_out + t_reas,
            "avg_tps": avg_tps,
            "peak_tps": round(peak_tps, 1),
        })

    conn.close()
    return days


class TelemetryHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    # A page on attacker.example can point its DNS at 127.0.0.1, at which point
    # the browser treats it as same-origin and the CORS fix no longer helps.
    # Only answer to the names this server is actually reachable under.
    ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", ""}

    # The interface is built on inline event handlers, so script-src cannot be
    # locked down without rewriting every onclick. The directive that earns its
    # place here is connect-src: even if an injection does execute, it cannot
    # post your transcripts anywhere off this machine. frame-ancestors has to
    # come from a header, which is the other reason this is not a meta tag.
    CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    )

    def end_headers(self):
        self.send_header("Content-Security-Policy", self.CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _host_ok(self):
        raw = (self.headers.get("Host") or "").strip()
        host = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
        if raw.startswith("["):
            host = raw.split("]")[0] + "]"
        return host in self.ALLOWED_HOSTS

    def do_GET(self):
        if not self._host_ok():
            self.send_error(403, "Invalid Host header")
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/stats":
            self.send_json(get_all_stats(query))
        elif path == "/api/sessions":
            self.send_json(get_sessions(query))
        elif path == "/api/timeseries":
            self.send_json(get_timeseries(query))
        elif path == "/api/export":
            sessions = get_sessions(query)
            # Include deep turn telemetry for each session
            conn = get_db_connection()
            deep_records = []
            for s in sessions:
                record = dict(s)
                if conn and s.get("harness") == "opencode":
                    c = conn.cursor()
                    c.execute("""
                        SELECT id, data
                        FROM message WHERE session_id = ? AND data LIKE '%assistant%' ORDER BY time_created ASC
                    """, (s["id"],))
                    turns = []
                    for (t_id, raw_mdata) in c.fetchall():
                        try:
                            d = json.loads(raw_mdata)
                            if d.get("role") != "assistant":
                                continue
                            t = d.get("time", {})
                            t_cr = t.get("created")
                            t_co = t.get("completed")
                            toks = d.get("tokens", {})
                            t_out = toks.get("output", 0) + toks.get("reasoning", 0)
                            turn_dur = (t_co - t_cr) / 1000.0 if (t_co and t_cr and t_co > t_cr) else 0.0
                            tps = round(t_out / turn_dur, 2) if (turn_dur > 0.05 and t_out) else 0.0
                            turns.append({
                                "turn_id": t_id,
                                "time_created": t_cr,
                                "time_completed": t_co,
                                "duration_seconds": round(turn_dur, 3),
                                "tokens_output": t_out or 0,
                                "decode_tps": tps,
                                "finish_reason": d.get("finish"),
                            })
                        except Exception:
                            continue
                    record["turns"] = turns
                deep_records.append(record)
            if conn:
                conn.close()

            export_payload = {
                "exported_at": datetime.now().isoformat(),
                "time_window": query.get("window", ["all"])[0],
                "from": query.get("from", [""])[0],
                "to": query.get("to", [""])[0],
                "harness": query.get("harness", ["all"])[0],
                "total_records": len(deep_records),
                "records": deep_records,
            }
            self.send_json(export_payload)
        elif path.startswith("/api/session/"):
            session_id = path.split("/")[-1]
            self.send_json(get_session_detail(session_id))
        elif path == "/api/timeseries":
            self.send_json(get_timeseries())
        elif path == "/api/turns":
            self.send_json(get_turns(query))
        elif path == "/api/tools":
            self.send_json(get_tool_stats(get_turns(query)))
        elif path == "/api/models":
            self.send_json(get_model_matrix(get_turns(query)))
        elif path == "/api/projects":
            self.send_json(get_project_stats(get_turns(query)))
        elif path == "/api/catalog":
            force = query.get("refresh", ["0"])[0] in ("1", "true", "yes")
            self.send_json(get_catalog(force=force))
        elif path == "/api/live":
            self.send_json(check_live_status())
        elif path == "/api/config":
            self.send_json({"servers": configured_ports(), "path": sanitize_path(str(CONFIG_PATH))})
        elif path == "/" or path == "/index.html":
            index_file = STATIC_DIR / "index.html"
            if index_file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(index_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "index.html not found")
        else:
            super().do_GET()

    def do_POST(self):
        if not self._host_ok():
            self.send_error(403, "Invalid Host header")
            return
        # A cross-site form can post text/plain without a preflight; requiring
        # JSON forces one, which this server never grants.
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            self.send_error(415, "Expected application/json")
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in self.ALLOWED_HOSTS:
            self.send_error(403, "Cross-origin request refused")
            return
        path = urlparse(self.path).path
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 65536)
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            self.send_error(400, "Invalid JSON")
            return

        if path == "/api/config/servers":
            # {"engine": "mlx", "ports": [8081, 8082]}; an empty list clears it.
            known = {e["id"] for e in server_catalog()}
            eid = body.get("engine")
            ports = body.get("ports")
            if eid not in known or not isinstance(ports, list) or not all(
                    isinstance(p, int) and 0 < p < 65536 and p != PORT for p in ports):
                self.send_error(400, "Expected a known engine and a list of ports")
                return
            cfg = load_user_config()
            servers = cfg.get("servers") if isinstance(cfg.get("servers"), dict) else {}
            if ports:
                servers[eid] = sorted(set(ports))
            else:
                servers.pop(eid, None)
            cfg["servers"] = servers
            try:
                save_user_config(cfg)
            except OSError as e:
                self.send_error(500, f"Could not write config: {e}")
                return
            invalidate_discovery()
            self.send_json({"servers": configured_ports(), "catalog": get_catalog(force=True)})
        else:
            self.send_error(404, "Not found")

    def send_json(self, data):
        # Every response leaves through here, so this is the one place the
        # masking cannot be forgotten when a new endpoint is added.
        response_bytes = json.dumps(scrub_payload(data)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def log_message(self, format, *args):
        if "/api/live" not in self.path:
            sys.stderr.write(f"[Telemetry API] {self.path}\n")


def run(port=PORT):
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, TelemetryHandler)
    # Report every source that was actually found, rather than the two that
    # happened to exist when this was first written.
    found = []
    for label, fetch in (
        ("OpenCode", count_opencode_sessions),
        ("OpenClaw", lambda: len(get_openclaw_sessions())),
        ("Hermes", lambda: len(get_hermes_sessions())),
        ("Cline", lambda: len(get_cline_sessions())),
        ("Roo Code", lambda: len(get_roo_sessions())),
        ("Zed", lambda: len(get_zed_sessions())),
        ("Goose", lambda: len(get_goose_sessions())),
        ("LM Studio", lambda: len(get_lmstudio_sessions())),
        ("Jan", lambda: len(get_jan_sessions())),
        ("Crush", lambda: len(get_crush_sessions())),
        ("AnythingLLM", lambda: len(get_anythingllm_sessions())),
        ("Open WebUI", lambda: len(get_openwebui_sessions())),
        ("Aider", lambda: len(scan_aider_history())),
        ("Continue", lambda: len(scan_continue_sessions())),
    ):
        try:
            n = fetch()
        except Exception:
            n = 0
        if n:
            found.append((label, n))

    try:
        live = check_live_status()
        engines = sorted(k for k, v in live.items() if isinstance(v, dict) and v.get("online"))
    except Exception:
        engines = []

    print("\n=========================================================")
    print("  Tach")
    print(f"  http://127.0.0.1:{port}")
    print()
    if found:
        width = max(len(name) for name, _ in found)
        print("  Reading:")
        for name, n in found:
            print(f"    {name.ljust(width)}  {n} session{'' if n == 1 else 's'}")
    else:
        print("  No agent history found yet. Run a session and reload.")
    if engines:
        print(f"  Engines online: {', '.join(engines)}")
    print("=========================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tach Server")
    parser.add_argument("port_pos", nargs="?", type=int, default=None, help="Port number (positional)")
    parser.add_argument("--port", type=int, default=None, help="Port number")
    parser.add_argument("--db", "--opencode-db", type=str, default=None, help="Path to opencode.db SQLite file")
    parser.add_argument("--openclaw-db", type=str, default=None, help="Path to openclaw.sqlite file")
    parser.add_argument("--openclaw-home", type=str, action="append", default=None,
                        help="Extra OpenClaw home directory to scan for transcripts (repeatable)")

    args = parser.parse_args()

    if args.db:
        CONFIG["opencode_db"] = Path(args.db)
    if args.openclaw_home:
        CONFIG["openclaw_homes"] = resolve_openclaw_homes(args.openclaw_home)
        CONFIG["openclaw_db"] = resolve_openclaw_db(homes=CONFIG["openclaw_homes"])
    if args.openclaw_db:
        CONFIG["openclaw_db"] = Path(args.openclaw_db)

    selected_port = args.port or args.port_pos or PORT
    run(selected_port)
