"""
Parse WhatsApp exported .txt files into time-based sessions and POST them to an n8n webhook.

This script supports common WhatsApp export formats, including:
- Android/Desktop: "DD/MM/YYYY, HH:MM - Name: message"
- iOS/locale variants: "[DD/MM/YYYY, HH:MM:SS] Name: message"
  and "[DD/MM/YYYY HH:MM:SS] Name: message" (space / narrow no-break space U+202F)

It also tolerates invisible Unicode marks occasionally present in exports (LRM/RLM/ZWSP/BOM).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

AGENT_LABEL = "Agent"
CUSTOMER_LABEL = "Customer"

# Android/Desktop export
LINE_RE_PLAIN = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)\s*-\s*([^:]+):\s*(.*)$"
)

# iOS/locale export: allow comma OR whitespace between date/time (incl. U+202F, U+00A0)
# WhatsApp exports may use various spaces between date and time (comma, normal space,
# NBSP U+00A0, narrow NBSP U+202F, thin space U+2009, tabs).
_WS_DATE_TIME = r"[, \t\u202f\u00a0\u2009]+"
LINE_RE_BRACKET = re.compile(
    rf"^\[(\d{{1,2}}/\d{{1,2}}/\d{{2,4}}){_WS_DATE_TIME}"
    rf"(\d{{1,2}}:\d{{2}}(?::\d{{2}})?(?:\s?[AP]M)?)\]\s*([^:]+):\s*(.*)$"
)

# Optional: system lines in Android/Desktop export (no "Name:" part)
LINE_RE_SYSTEM_PLAIN = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)\s*-\s*(.*)$"
)

_INVISIBLE = re.compile(r"[\u200e\u200f\u200b]")


def _clean_wa_field(s: str) -> str:
    return _INVISIBLE.sub("", s).strip()


def _parse_datetime(date_str: str, time_str: str) -> datetime | None:
    """
    Parse WhatsApp date/time strings into a datetime for time-gap segmentation.
    Returns None if parsing fails (we prefer skipping timestamps over using now()).
    """
    try:
        has_seconds = time_str.count(":") >= 2
        if "M" in time_str.upper():
            fmt = "%d/%m/%Y %I:%M:%S %p" if has_seconds else "%d/%m/%Y %I:%M %p"
        else:
            fmt = "%d/%m/%Y %H:%M:%S" if has_seconds else "%d/%m/%Y %H:%M"
        if len(date_str.split("/")[-1]) == 2:
            fmt = fmt.replace("%Y", "%y")
        return datetime.strptime(f"{date_str} {time_str}", fmt)
    except ValueError:
        return None


def _try_parse_message_line(line: str) -> tuple[str, str, str, str, bool] | None:
    """Return (date, time, sender, message, is_system)."""
    m = LINE_RE_BRACKET.match(line)
    if m:
        return (m.group(1), m.group(2), _clean_wa_field(m.group(3)), _clean_wa_field(m.group(4)), False)

    m = LINE_RE_PLAIN.match(line)
    if m:
        return (m.group(1), m.group(2), _clean_wa_field(m.group(3)), _clean_wa_field(m.group(4)), False)

    m_sys = LINE_RE_SYSTEM_PLAIN.match(line)
    if m_sys:
        return (m_sys.group(1), m_sys.group(2), "System", _clean_wa_field(m_sys.group(3)), True)

    return None


def post_sessions_to_webhook(
    webhook_url: str,
    data: dict[str, list[list[dict[str, str]]]],
    *,
    timeout: float = 60.0,
    continue_on_error: bool = True,
) -> tuple[int, int]:
    """
    POST each session to a webhook.

    Payload format (enveloped for routing):
    {"session": [...], "source_file": "...", "session_index": n}
    """
    try:
        import requests  # type: ignore[import-unresolved]
    except ImportError as e:
        raise SystemExit("Install requests first: pip install requests") from e

    ok, failed = 0, 0
    for source_file, sessions in data.items():
        for session_index, session in enumerate(sessions):
            if not session:
                continue
            payload: Any = {
                "session": session,
                "source_file": source_file,
                "session_index": session_index,
            }
            try:
                response = requests.post(webhook_url, json=payload, timeout=timeout)
                response.raise_for_status()
                ok += 1
            except Exception as exc:
                failed += 1
                msg = f"Webhook error ({source_file} session_index={session_index}): {exc}"
                if continue_on_error:
                    print(msg, file=sys.stderr)
                else:
                    raise SystemExit(msg) from exc
    return ok, failed


def parse_chat_text(chat_data: str) -> list[dict[str, Any]]:
    """
    Parse an export into message rows:
    {"sender": str, "message": str, "timestamp": datetime}

    Continuation lines are appended to the previous message.
    System lines are ignored by default (they often add noise in group chats).
    """
    messages: list[dict[str, Any]] = []
    for raw in chat_data.splitlines():
        line = raw.rstrip("\r")
        line = re.sub(r"^[\u200e\u200f\ufeff\u200b\s]+", "", line)

        if not line and messages:
            messages[-1]["message"] += "\n"
            continue

        parsed = _try_parse_message_line(line)
        if parsed:
            date, time, sender, message, is_system = parsed
            if is_system:
                continue
            ts = _parse_datetime(date, time)
            if ts is None:
                # If a line matches but timestamp parsing fails, skip it to avoid breaking session gaps.
                continue
            messages.append({"sender": sender, "message": message, "timestamp": ts})
        elif messages:
            messages[-1]["message"] += "\n" + line
    return messages


def group_into_sessions_by_time(
    messages: list[dict[str, Any]],
    *,
    hours_gap: float = 2.0,
) -> list[list[dict[str, str]]]:
    """Start a new session when the time gap between messages exceeds hours_gap."""
    if not messages:
        return []

    sessions: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    last_time: datetime | None = None

    for msg in messages:
        current_time: datetime = msg["timestamp"]
        if last_time is not None:
            time_diff_hours = (current_time - last_time).total_seconds() / 3600.0
            if time_diff_hours > hours_gap and current:
                sessions.append(current)
                current = []

        current.append({"sender": msg["sender"], "message": msg["message"]})
        last_time = current_time

    if current:
        sessions.append(current)

    return sessions


def _build_role_map_for_chat(
    messages: list[dict[str, Any]],
    agent_names: set[str],
) -> dict[str, str] | None:
    """
    Build a mapping from raw WhatsApp sender -> Agent / Customer / Customer{n}.
    Returns None when we cannot identify any agent speaker in this chat.
    """
    if not agent_names:
        return None

    has_agent = any(m["sender"] in agent_names for m in messages)
    if not has_agent:
        return None

    mapping: dict[str, str] = {name: AGENT_LABEL for name in agent_names}

    order: list[str] = []
    seen: set[str] = set()
    for m in messages:
        s = m["sender"]
        if s in agent_names:
            continue
        if s not in seen:
            seen.add(s)
            order.append(s)

    if len(order) <= 1:
        for s in order:
            mapping[s] = CUSTOMER_LABEL
    else:
        for i, s in enumerate(order, start=1):
            mapping[s] = f"{CUSTOMER_LABEL}{i}"

    return mapping


def _turn_aggregate(
    messages: list[dict[str, Any]],
    role_map: dict[str, str],
    *,
    max_gap_hours: float,
) -> list[dict[str, Any]]:
    """
    Merge consecutive messages from the same normalized role.
    If time gap between messages exceeds max_gap_hours, force a new turn.
    Output rows keep: role, text, start_ts, end_ts.
    """
    turns: list[dict[str, Any]] = []
    last_end: datetime | None = None

    for m in messages:
        role = role_map.get(m["sender"], CUSTOMER_LABEL)
        text = m["message"]
        ts: datetime = m["timestamp"]

        gap_break = False
        if last_end is not None:
            gap_hours = (ts - last_end).total_seconds() / 3600.0
            gap_break = gap_hours > max_gap_hours

        if (not turns) or gap_break or turns[-1]["role"] != role:
            turns.append({"role": role, "text": text, "start_ts": ts, "end_ts": ts})
        else:
            turns[-1]["text"] += "\n" + text
            turns[-1]["end_ts"] = ts

        last_end = ts

    return turns


def group_into_qa_sessions_from_turns(
    turns: list[dict[str, Any]],
    *,
    preserve_customer_ids: bool,
    group_strategy: str,
) -> list[list[dict[str, str]]]:
    """
    Convert turns into Q/A sessions:
    [{"sender": "Customer", "message": "..."},
     {"sender": "Agent", "message": "..."}]

    - Customer-side turns (Customer / CustomerN) are merged until an Agent turn appears.
    - Agent turns are merged until the next customer-side turn starts.
    - Leading Agent-only turns are ignored.
    """
    if group_strategy not in {"recent-customer"}:
        raise ValueError(f"Unsupported group_strategy: {group_strategy}")

    sessions: list[list[dict[str, str]]] = []

    # Per-customer buffers. Keys are roles like "Customer", "Customer1", ...
    pending_q: dict[str, list[str]] = {}
    pending_a: dict[str, list[str]] = {}
    last_customer_role: str | None = None

    def _customer_sender_label(role: str) -> str:
        return role if preserve_customer_ids else CUSTOMER_LABEL

    def flush_role(role: str) -> None:
        q = pending_q.get(role) or []
        a = pending_a.get(role) or []
        if not q or not a:
            return
        sessions.append(
            [
                {"sender": _customer_sender_label(role), "message": "\n".join(q).strip()},
                {"sender": AGENT_LABEL, "message": "\n".join(a).strip()},
            ]
        )
        pending_q[role] = []
        pending_a[role] = []

    for t in turns:
        role = t["role"]
        text = t["text"]

        if role == AGENT_LABEL:
            # Assign the agent answer to the most recent customer role with a pending question.
            if last_customer_role is None:
                continue  # preamble
            if not pending_q.get(last_customer_role):
                continue
            pending_a.setdefault(last_customer_role, []).append(text)
            continue

        # Customer side
        customer_role = role
        last_customer_role = customer_role

        # If this customer already has a completed Q/A, flush before starting a new question.
        if pending_q.get(customer_role) and pending_a.get(customer_role):
            flush_role(customer_role)

        pending_q.setdefault(customer_role, []).append(text)

    # Flush completed pairs in a stable order (first-seen order in turns)
    seen_roles: list[str] = []
    seen_set: set[str] = set()
    for t in turns:
        r = t["role"]
        if r == AGENT_LABEL:
            continue
        if r not in seen_set:
            seen_set.add(r)
            seen_roles.append(r)

    for r in seen_roles:
        flush_role(r)

    return sessions


def parse_file(
    file_path: Path,
    *,
    split_mode: str,
    hours_gap: float,
    agent_names: set[str],
    max_gap_hours: float,
    preserve_customer_ids: bool,
    group_strategy: str,
) -> list[list[dict[str, str]]]:
    text = file_path.read_text(encoding="utf-8-sig", errors="replace")
    messages = parse_chat_text(text)
    if split_mode == "time":
        return group_into_sessions_by_time(messages, hours_gap=hours_gap)

    # split_mode == "turn": requires identifying at least one agent in this chat.
    role_map = _build_role_map_for_chat(messages, agent_names)
    if role_map is None:
        # Fallback: keep the whole file as a single session with raw senders (names/numbers).
        if not messages:
            return []
        return [[{"sender": m["sender"], "message": m["message"]} for m in messages]]

    turns = _turn_aggregate(messages, role_map, max_gap_hours=max_gap_hours)
    return group_into_qa_sessions_from_turns(
        turns,
        preserve_customer_ids=preserve_customer_ids,
        group_strategy=group_strategy,
    )


def collect_txt_files(folder: Path, recursive: bool) -> list[Path]:
    return sorted(folder.rglob("*.txt") if recursive else folder.glob("*.txt"))


def parse_folder(
    folder: Path,
    *,
    recursive: bool,
    split_mode: str,
    hours_gap: float,
    agent_names: set[str],
    max_gap_hours: float,
    preserve_customer_ids: bool,
    group_strategy: str,
) -> dict[str, list[list[dict[str, str]]]]:
    out: dict[str, list[list[dict[str, str]]]] = {}
    for path in collect_txt_files(folder, recursive):
        if not path.is_file():
            continue
        rel = path.relative_to(folder).as_posix()
        out[rel] = parse_file(
            path,
            split_mode=split_mode,
            hours_gap=hours_gap,
            agent_names=agent_names,
            max_gap_hours=max_gap_hours,
            preserve_customer_ids=preserve_customer_ids,
            group_strategy=group_strategy,
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse WhatsApp exports and send sessions to an n8n webhook.")
    parser.add_argument("-i", "--input", type=Path, default=Path("."), help="Folder containing .txt files")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("cleaned_conversations.json"),
        help="Output JSON path (default: cleaned_conversations.json in the current directory).",
    )
    parser.add_argument(
        "--webhook-url",
        default="",
        help="n8n Webhook URL (or set env N8N_WEBHOOK_URL)",
    )
    parser.add_argument(
        "--hours-gap",
        type=float,
        default=2.0,
        help="Time gap (hours) to start a new session (default: 2.0)",
    )
    parser.add_argument(
        "--split-mode",
        choices=["time", "turn"],
        default="turn",
        help="How to segment sessions: time-based (time) or Q/A turns (turn). Default: turn.",
    )
    parser.add_argument(
        "--agent-names",
        default="",
        help=(
            "Comma-separated support agent display names (exact match). "
            "Required for --split-mode turn; otherwise the script falls back to raw senders per file."
        ),
    )
    parser.add_argument(
        "--max-gap-hours",
        type=float,
        default=24.0,
        help=(
            "In --split-mode turn, force a new turn if messages are separated by more than this gap "
            "(default: 24). Helps avoid merging unrelated cases."
        ),
    )
    parser.add_argument(
        "--preserve-customer-ids",
        action="store_true",
        help=(
            "In --split-mode turn, keep Customer1/Customer2/... in output when multiple customers exist. "
            "If not set, all customer sides are labeled as 'Customer'."
        ),
    )
    parser.add_argument(
        "--group-strategy",
        choices=["recent-customer"],
        default="recent-customer",
        help=(
            "In group chats for --split-mode turn, how to assign an Agent reply. "
            "recent-customer = attach to the most recent customer with a pending question."
        ),
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Only scan .txt files in the input folder (do not traverse subfolders).",
    )
    parser.add_argument(
        "--webhook-timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for each webhook POST (default: 60).",
    )
    parser.add_argument(
        "--webhook-stop-on-error",
        action="store_true",
        help="Exit immediately on the first webhook error (default: continue).",
    )

    args = parser.parse_args()
    folder = args.input.resolve()
    if not folder.is_dir():
        raise SystemExit(f"Input is not a directory: {folder}")

    webhook_url = (args.webhook_url or os.environ.get("N8N_WEBHOOK_URL", "")).strip()

    print(f"Scanning folder: {folder}")
    agent_names = {s.strip() for s in args.agent_names.split(",") if s.strip()}
    data = parse_folder(
        folder,
        recursive=not args.no_recursive,
        split_mode=args.split_mode,
        hours_gap=args.hours_gap,
        agent_names=agent_names,
        max_gap_hours=args.max_gap_hours,
        preserve_customer_ids=args.preserve_customer_ids,
        group_strategy=args.group_strategy,
    )
    total_sessions = sum(len(v) for v in data.values())
    print(f"Found {len(data)} file(s), produced {total_sessions} session(s)")

    # Always write JSON output (handy for debugging and offline processing)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote JSON: {args.output.resolve()}")

    # Webhook is optional: if provided, post each session to n8n.
    if webhook_url:
        print("Posting sessions to webhook...")
        posted, failed = post_sessions_to_webhook(
            webhook_url,
            data,
            timeout=args.webhook_timeout,
            continue_on_error=not args.webhook_stop_on_error,
        )
        print(f"Webhook: posted={posted} failed={failed}")
    else:
        print("Webhook: skipped (no --webhook-url and N8N_WEBHOOK_URL not set)")


if __name__ == "__main__":
    main()