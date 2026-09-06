"""
Token Telemetry & Model Observatory Server
Serves real-time and historic performance data from local LLM harnesses:
OpenCode, OpenClaw, Aider, Continue, MLX Server, and Ollama.
"""

import os
import sys
import json
import sqlite3
import datetime
import urllib.request
import urllib.error
import argparse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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


def resolve_openclaw_db(cli_path=None):
    """Resolves OpenClaw state database if present on the system."""
    if cli_path and os.path.exists(cli_path):
        return Path(cli_path)
    env_path = os.environ.get("OPENCLAW_DB")
    if env_path and os.path.exists(env_path):
        return Path(env_path)
    home = Path.home()
    candidates = [
        home / ".openclaw" / "state" / "openclaw.sqlite",
        home / ".openclaw" / "openclaw.sqlite",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


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
CONFIG = {
    "opencode_db": resolve_opencode_db(),
    "openclaw_db": resolve_openclaw_db(),
}


def get_db_connection():
    db_path = CONFIG["opencode_db"]
    if not db_path or not os.path.exists(db_path):
        return None
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def check_live_status():
    status = {
        "mlx": {"online": False, "details": None, "requests": [], "apc": None, "summary": None},
        "ollama": {"online": False, "details": None},
        "llamacpp": {"online": False, "details": None},
    }

    # Check MLX Server (:8080)
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/metrics", headers={"User-Agent": "Telemetry"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode())
            status["mlx"]["online"] = True
            recent_list = data.get("recent") or []
            last_req = data.get("latest") or {}
            summary = data.get("summary") or {}
            server_rt = data.get("server") or {}
            apc = server_rt.get("apc") or {}
            
            status["mlx"]["details"] = {
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
            status["mlx"]["apc"] = {
                "enabled": apc.get("enabled", False),
                "hit_rate": round(float(apc.get("token_hit_rate", 0.0)) * 100, 1),
                "matched_tokens": apc.get("matched_tokens", 0),
                "exact_hits": apc.get("exact_hits", 0),
                "lookups_hit": apc.get("lookups_hit", 0),
                "lookups_miss": apc.get("lookups_miss", 0),
            }
            status["mlx"]["summary"] = {
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
            status["mlx"]["requests"] = cleaned_reqs
    except Exception:
        pass

    # Check Ollama (:11434)
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/ps", headers={"User-Agent": "Telemetry"})
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
            }
    except Exception:
        pass

    # Check llama.cpp (:8077)
    try:
        req = urllib.request.Request("http://127.0.0.1:8077/props", headers={"User-Agent": "Telemetry"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            data = json.loads(resp.read().decode())
            status["llamacpp"]["online"] = True
            status["llamacpp"]["details"] = {
                "context": data.get("default_generation_settings", {}).get("n_ctx", 0)
            }
    except Exception:
        pass

    return status


def get_all_stats():
    conn = get_db_connection()
    if not conn:
        return {"error": "Database not found", "db_path": sanitize_path(str(CONFIG["opencode_db"]))}

    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM session;")
    total_sessions = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM message;")
    total_messages = c.fetchone()[0]

    c.execute("""
        SELECT 
            COALESCE(SUM(tokens_input), 0),
            COALESCE(SUM(tokens_output), 0),
            COALESCE(SUM(tokens_reasoning), 0)
        FROM session;
    """)
    sum_in, sum_out, sum_reas = c.fetchone()

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

    c.execute("""
        SELECT 
            COALESCE(json_extract(model, '$.providerID'), 'local') || '/' || COALESCE(json_extract(model, '$.id'), 'model') as full_name,
            COALESCE(SUM(tokens_input), 0), 
            COALESCE(SUM(tokens_output), 0), 
            COALESCE(SUM(tokens_reasoning), 0),
            COUNT(*)
        FROM session 
        WHERE model IS NOT NULL 
        GROUP BY full_name
        ORDER BY SUM(tokens_output) DESC;
    """)
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

    c.execute("""
        SELECT json_extract(data, '$.tool') as tool_name, COUNT(*) 
        FROM part 
        WHERE json_extract(data, '$.type') = 'tool' 
        GROUP BY tool_name 
        ORDER BY COUNT(*) DESC 
        LIMIT 12;
    """)
    tools = [{"name": r[0] or "tool", "count": r[1]} for r in c.fetchall()]

    c.execute("SELECT directory, COUNT(*), COALESCE(SUM(tokens_output), 0) FROM session WHERE directory IS NOT NULL GROUP BY directory ORDER BY COUNT(*) DESC;")
    dir_rows = c.fetchall()
    directories = [{
        "path": sanitize_path(r[0]),
        "folder": Path(r[0]).name or "root" if r[0] else "root",
        "count": r[1],
        "tokens_output": r[2]
    } for r in dir_rows]

    duration_buckets = {"< 1 min": 0, "1 - 5 mins": 0, "5 - 15 mins": 0, "15 - 30 mins": 0, "> 30 mins": 0}
    c.execute("SELECT time_created, time_updated FROM session WHERE time_created IS NOT NULL AND time_updated IS NOT NULL;")
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

    # Discovered Harnesses
    harnesses = [
        {"id": "all", "name": "All Harnesses", "detected": True, "count": total_sessions},
        {"id": "opencode", "name": "OpenCode", "detected": True, "path": sanitize_path(str(CONFIG["opencode_db"])), "count": total_sessions},
        {"id": "openclaw", "name": "OpenClaw", "detected": bool(CONFIG["openclaw_db"]), "path": sanitize_path(str(CONFIG["openclaw_db"])) if CONFIG["openclaw_db"] else None, "count": 0},
        {"id": "aider", "name": "Aider", "detected": len(scan_aider_history()) > 0, "count": len(scan_aider_history())},
        {"id": "continue", "name": "Continue", "detected": len(scan_continue_sessions()) > 0, "count": len(scan_continue_sessions())},
    ]

    return {
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
        }
    }


def get_sessions(query_params):
    conn = get_db_connection()
    if not conn:
        return []

    c = conn.cursor()

    search = query_params.get("q", [""])[0].lower()
    harness_filter = query_params.get("harness", [""])[0]
    folder_filter = query_params.get("folder", [""])[0]
    provider_filter = query_params.get("provider", [""])[0]
    model_filter = query_params.get("model", [""])[0]
    speed_tier = query_params.get("speed_tier", [""])[0]
    status_filter = query_params.get("status", [""])[0]
    date_from = query_params.get("from", [""])[0]
    date_to = query_params.get("to", [""])[0]
    sort_by = query_params.get("sort", ["date_desc"])[0]

    if harness_filter and harness_filter not in ["opencode", "all"]:
        conn.close()
        return []

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

    results = []
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
                model_name = str(model_raw)

        if search and search not in (title or "").lower() and search not in folder.lower() and search not in model_name.lower():
            continue
        if folder_filter and folder != folder_filter:
            continue
        if provider_filter and provider_name != provider_filter:
            continue
        if model_filter and model_name != model_filter:
            continue

        created_dt = datetime.datetime.fromtimestamp(t_created / 1000) if t_created else None
        if created_dt:
            if date_from and created_dt.strftime("%Y-%m-%d") < date_from:
                continue
            if date_to and created_dt.strftime("%Y-%m-%d") > date_to:
                continue

        duration_s = (t_updated - t_created) / 1000.0 if (t_updated and t_created and t_updated > t_created) else 0.0

        c.execute("""
            SELECT data FROM message 
            WHERE session_id = ? AND data LIKE '%assistant%' AND data LIKE '%completed%';
        """, (sid,))
        sess_msgs = c.fetchall()

        t_gen_s = 0.0
        t_gen_toks = 0
        peak_turn_tps = 0.0
        has_tool_calls = False

        for (mdata,) in sess_msgs:
            try:
                d = json.loads(mdata)
                t = d.get("time", {})
                cr = t.get("created")
                co = t.get("completed")
                dur = (co - cr) / 1000.0 if (co and cr and co > cr) else 0.0
                toks = d.get("tokens", {})
                gen = toks.get("output", 0) + toks.get("reasoning", 0)
                if d.get("finish") == "tool-calls" or "tool" in str(d):
                    has_tool_calls = True
                if gen > 0 and dur > 0.05:
                    tps = gen / dur
                    if tps < 250:
                        t_gen_s += dur
                        t_gen_toks += gen
                        if tps > peak_turn_tps:
                            peak_turn_tps = tps
            except Exception:
                pass

        session_tps = round(t_gen_toks / t_gen_s, 1) if t_gen_s > 0 else 0.0

        # Filter by speed tier
        if speed_tier:
            if speed_tier == "<15" and session_tps >= 15:
                continue
            elif speed_tier == "15-30" and not (15 <= session_tps < 30):
                continue
            elif speed_tier == "30-45" and not (30 <= session_tps < 45):
                continue
            elif speed_tier == "45-60" and not (45 <= session_tps < 60):
                continue
            elif speed_tier == "60+" and session_tps < 60:
                continue

        # Filter by status / finish reason
        if status_filter == "tool_calls" and not has_tool_calls:
            continue
        elif status_filter == "stop" and has_tool_calls:
            continue

        results.append({
            "id": sid,
            "harness": "opencode",
            "title": title or "Untitled Session",
            "directory": sanitized_dir,
            "folder": folder,
            "model": model_name,
            "provider": provider_name,
            "date_str": created_dt.strftime("%Y-%m-%d %H:%M") if created_dt else "",
            "time_created": t_created,
            "duration_s": round(duration_s, 1),
            "tokens_input": t_in,
            "tokens_output": t_out,
            "tokens_reasoning": t_reas,
            "tokens_total": t_in + t_out + t_reas,
            "cost": cost,
            "message_count": msg_count,
            "tps": session_tps,
            "peak_tps": round(peak_turn_tps, 1),
            "has_tool_calls": has_tool_calls,
        })

    # Sorting
    if sort_by == "date_desc":
        results.sort(key=lambda x: x["time_created"], reverse=True)
    elif sort_by == "date_asc":
        results.sort(key=lambda x: x["time_created"])
    elif sort_by == "tps_desc":
        results.sort(key=lambda x: x["tps"], reverse=True)
    elif sort_by == "tokens_desc":
        results.sort(key=lambda x: x["tokens_output"], reverse=True)
    elif sort_by == "duration_desc":
        results.sort(key=lambda x: x["duration_s"], reverse=True)

    conn.close()
    return results


def get_session_detail(session_id):
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
                "date_str": datetime.datetime.fromtimestamp(created / 1000).strftime("%H:%M:%S") if created else "",
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
        "date_str": datetime.datetime.fromtimestamp(t_created / 1000).strftime("%Y-%m-%d %H:%M:%S") if t_created else "",
        "duration_s": round(duration_s, 1),
        "tokens_input": t_in,
        "tokens_output": t_out,
        "tokens_reasoning": t_reas,
        "tokens_total": t_in + t_out + t_reas,
        "avg_tps": overall_tps,
        "messages": messages,
    }


def get_timeseries():
    conn = get_db_connection()
    if not conn:
        return {"error": "Database not found"}

    c = conn.cursor()

    c.execute("""
        SELECT 
            strftime('%Y-%m-%d', time_created / 1000, 'unixepoch') as day,
            COUNT(*) as session_count,
            COALESCE(SUM(tokens_input), 0) as tokens_in,
            COALESCE(SUM(tokens_output), 0) as tokens_out,
            COALESCE(SUM(tokens_reasoning), 0) as tokens_reas
        FROM session 
        WHERE time_created IS NOT NULL
        GROUP BY day
        ORDER BY day ASC;
    """ )
    day_rows = c.fetchall()

    days = []
    for r in day_rows:
        day_str, sess_cnt, t_in, t_out, t_reas = r
        if not day_str:
            continue

        c.execute("""
            SELECT m.data 
            FROM message m
            WHERE strftime('%Y-%m-%d', m.time_created / 1000, 'unixepoch') = ?
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

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/stats":
            self.send_json(get_all_stats())
        elif path == "/api/sessions":
            self.send_json(get_sessions(query))
        elif path == "/api/export":
            sessions = get_sessions(query)
            export_payload = {
                "exported_at": datetime.datetime.now().isoformat(),
                "total_records": len(sessions),
                "records": sessions,
            }
            self.send_json(export_payload)
        elif path.startswith("/api/session/"):
            session_id = path.split("/")[-1]
            self.send_json(get_session_detail(session_id))
        elif path == "/api/timeseries":
            self.send_json(get_timeseries())
        elif path == "/api/live":
            self.send_json(check_live_status())
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

    def send_json(self, data):
        response_bytes = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def log_message(self, format, *args):
        if "/api/live" not in self.path:
            sys.stderr.write(f"[Telemetry API] {self.path}\n")


def run(port=PORT):
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, TelemetryHandler)
    print("\n=========================================================")
    print("  ⚡ Token Telemetry & Model Observatory Running!")
    print(f"  URL: http://127.0.0.1:{port}")
    print(f"  OpenCode DB: {sanitize_path(str(CONFIG['opencode_db']))}")
    if CONFIG["openclaw_db"]:
        print(f"  OpenClaw DB: {sanitize_path(str(CONFIG['openclaw_db']))}")
    print("=========================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Token Telemetry & Model Observatory Server")
    parser.add_argument("port_pos", nargs="?", type=int, default=None, help="Port number (positional)")
    parser.add_argument("--port", type=int, default=None, help="Port number")
    parser.add_argument("--db", "--opencode-db", type=str, default=None, help="Path to opencode.db SQLite file")
    parser.add_argument("--openclaw-db", type=str, default=None, help="Path to openclaw.sqlite file")

    args = parser.parse_args()

    if args.db:
        CONFIG["opencode_db"] = Path(args.db)
    if args.openclaw_db:
        CONFIG["openclaw_db"] = Path(args.openclaw_db)

    selected_port = args.port or args.port_pos or PORT
    run(selected_port)
