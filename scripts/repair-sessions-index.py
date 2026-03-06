#!/usr/bin/env python3
"""
Session Index Repair Script

Repairs Claude Code sessions-index.json by:
- Finding orphaned session files (on disk but not in index)
- Detecting dead entries (in index but no file on disk)
- Fixing customTitle collisions (append date suffix)
- CREATING sessions-index.json from scratch when missing (needed for per-thread CWDs)
- Backing up before modifications

Usage:
  python repair-sessions-index.py           # Dry-run (report only)
  python repair-sessions-index.py --fix     # Apply fixes
  python repair-sessions-index.py --verbose # Detailed output
  python repair-sessions-index.py --hook    # SessionStart hook mode (auto-fix silently)
  python repair-sessions-index.py --quiet   # Silent when nothing to fix
"""

import argparse
import io
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Fix Windows cp1252 encoding for Unicode output
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$",
    re.IGNORECASE,
)

MAX_VERBOSE_SESSIONS = 10
MAX_DEFAULT_SESSIONS = 5


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes // 1024}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def parse_session_file(session_path: Path) -> dict[str, Any] | None:
    """Parse a session JSONL file to extract metadata."""
    try:
        lines = session_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        print(f"  Failed to read {session_path.name}: {e}", file=sys.stderr)
        return None

    if not lines:
        return None

    session_id = None
    first_prompt = "No prompt"
    custom_title = None
    message_count = 0
    created = None
    modified = None
    git_branch = None
    is_sidechain = False

    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not session_id and "sessionId" in data:
            session_id = data["sessionId"]
        if git_branch is None and "gitBranch" in data:
            git_branch = data.get("gitBranch", "main")
        if "isSidechain" in data:
            is_sidechain = data.get("isSidechain", False)
        if "timestamp" in data:
            ts = data["timestamp"]
            if not created:
                created = ts
            modified = ts
        if data.get("type") in ("user", "assistant", "tool_use", "tool_result"):
            message_count += 1
        if data.get("type") == "user" and first_prompt == "No prompt":
            message = data.get("message", {})
            content = message.get("content", "")
            if isinstance(content, str) and content.strip():
                first_prompt = content[:80].strip()
                if len(content) > 80:
                    first_prompt += "..."
        if data.get("type") == "custom-title":
            custom_title = data.get("customTitle")

    if not session_id:
        session_id = session_path.stem

    return {
        "sessionId": session_id,
        "fullPath": str(session_path.absolute()),
        "fileMtime": int(session_path.stat().st_mtime * 1000),
        "firstPrompt": first_prompt,
        "customTitle": custom_title,
        "summary": first_prompt,
        "messageCount": message_count,
        "created": created
        or datetime.fromtimestamp(
            session_path.stat().st_ctime, tz=timezone.utc
        ).isoformat(),
        "modified": modified
        or datetime.fromtimestamp(
            session_path.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
        "gitBranch": git_branch or "main",
        "projectPath": str(session_path.parent.parent.absolute()),
        "isSidechain": is_sidechain,
    }


def find_orphaned_sessions(project_dir: Path, index_data: dict) -> list[dict]:
    indexed_ids = {entry["sessionId"] for entry in index_data.get("entries", [])}
    orphaned = []
    for session_file in project_dir.glob("*.jsonl"):
        if not UUID_PATTERN.match(session_file.name):
            continue
        if session_file.stem not in indexed_ids:
            metadata = parse_session_file(session_file)
            if metadata:
                orphaned.append(metadata)
    orphaned.sort(key=lambda x: x.get("created", ""), reverse=True)
    return orphaned


def find_dead_entries(project_dir: Path, index_data: dict) -> list[dict]:
    return [
        entry
        for entry in index_data.get("entries", [])
        if not Path(entry["fullPath"]).exists()
    ]


def detect_title_collisions(entries: list[dict]) -> dict[str, list[dict]]:
    title_map = defaultdict(list)
    for entry in entries:
        ct = entry.get("customTitle")
        if ct:
            title_map[ct].append(entry)
    return {t: e for t, e in title_map.items() if len(e) > 1}


def fix_title_collisions(entries: list[dict]) -> None:
    for title, dupes in detect_title_collisions(entries).items():
        dupes.sort(key=lambda x: x.get("created", ""))
        for entry in dupes[1:]:
            created = entry.get("created", "")
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                suffix = dt.strftime("%Y-%m-%d")
            except (ValueError, AttributeError, TypeError):
                suffix = "unknown"
            entry["customTitle"] = f"{title} ({suffix})"


def backup_index(index_path: Path) -> Path:
    backup_path = index_path.with_suffix(".json.bak")
    backup_path.write_text(index_path.read_text(encoding="utf-8"), encoding="utf-8")
    return backup_path


def create_index_from_scratch(project_dir: Path) -> int:
    """Create sessions-index.json from scratch for dirs that never had one.
    Returns number of sessions indexed."""
    jsonl_files = [f for f in project_dir.glob("*.jsonl") if UUID_PATTERN.match(f.name)]
    if not jsonl_files:
        return 0

    entries = []
    for f in jsonl_files:
        metadata = parse_session_file(f)
        if metadata:
            entries.append(metadata)

    if not entries:
        return 0

    entries.sort(key=lambda x: x.get("created", ""), reverse=True)
    index_path = project_dir / "sessions-index.json"
    index_path.write_text(
        json.dumps({"entries": entries}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(entries)


def repair_project(project_dir: Path, fix: bool, verbose: bool) -> dict:
    index_path = project_dir / "sessions-index.json"
    if not index_path.exists():
        return {"error": f"sessions-index.json not found in {project_dir}"}

    try:
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"Failed to read index: {e}"}

    existing = index_data.get("entries", [])
    orphaned = find_orphaned_sessions(project_dir, index_data)
    dead = find_dead_entries(project_dir, index_data)
    disk_count = len(
        [f for f in project_dir.glob("*.jsonl") if UUID_PATTERN.match(f.name)]
    )
    collisions = detect_title_collisions(existing + orphaned)

    stats = {
        "project_dir": str(project_dir),
        "indexed": len(existing),
        "on_disk": disk_count,
        "orphaned": len(orphaned),
        "dead": len(dead),
        "collisions": len(collisions),
        "collision_entries": sum(len(e) for e in collisions.values()),
        "orphaned_sessions": orphaned[
            : MAX_VERBOSE_SESSIONS if verbose else MAX_DEFAULT_SESSIONS
        ],
    }

    if fix:
        stats["backup"] = str(backup_index(index_path))
        cleaned = [e for e in existing if e not in dead]
        merged = cleaned + orphaned
        fix_title_collisions(merged)
        merged.sort(key=lambda x: x.get("created", ""), reverse=True)
        index_data["entries"] = merged
        index_path.write_text(
            json.dumps(index_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        stats["fixed"] = True
        stats["new_total"] = len(merged)

    return stats


def run_hook_mode() -> None:
    """Run as SessionStart hook - auto-fix silently, output hook JSON."""
    try:
        sys.stdin.read()
    except Exception:
        pass

    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        sys.stdout.write('{"continue":true,"suppressOutput":true}')
        return

    total_fixed = 0

    for d in claude_dir.iterdir():
        if not d.is_dir():
            continue

        index_path = d / "sessions-index.json"

        if index_path.exists():
            # Repair existing index (add orphaned sessions)
            stats = repair_project(d, fix=True, verbose=False)
            if stats.get("fixed") and stats.get("orphaned", 0) > 0:
                total_fixed += stats["orphaned"]
        else:
            # Create index from scratch (handles per-thread CWDs that never had one)
            created = create_index_from_scratch(d)
            if created > 0:
                total_fixed += created
                print(
                    f"[session-repair] Created sessions-index.json in {d.name} ({created} session(s))",
                    file=sys.stderr,
                )

    if total_fixed > 0:
        print(
            f"[session-repair] Re-indexed {total_fixed} orphaned session(s)",
            file=sys.stderr,
        )
    sys.stdout.write('{"continue":true,"suppressOutput":true}')


def main() -> None:
    ap = argparse.ArgumentParser(description="Repair Claude Code sessions-index.json")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--hook", action="store_true")
    ap.add_argument("--project", type=str)
    args = ap.parse_args()

    if args.hook:
        return run_hook_mode()

    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        sys.exit("Claude projects directory not found")

    if args.project:
        dirs = [claude_dir / args.project]
    else:
        dirs = [d for d in claude_dir.iterdir() if d.is_dir()]

    totals = {"orphaned": 0, "dead": 0, "collisions": 0, "created_indexes": 0}
    results = []

    for d in dirs:
        index_path = d / "sessions-index.json"
        if not index_path.exists():
            jsonl_count = len(
                [f for f in d.glob("*.jsonl") if UUID_PATTERN.match(f.name)]
            )
            if jsonl_count > 0:
                totals["created_indexes"] += 1
                if args.fix:
                    n = create_index_from_scratch(d)
                    results.append((d, {"created_from_scratch": n, "on_disk": jsonl_count}))
                else:
                    results.append((d, {"missing_index": True, "on_disk": jsonl_count}))
            continue

        s = repair_project(d, args.fix, args.verbose)
        results.append((d, s))
        if "error" not in s:
            for k in ("orphaned", "dead", "collisions"):
                totals[k] += s[k]

    if args.quiet and all(v == 0 for v in totals.values()):
        return

    print("Session Index Repair Report")
    print("=" * 60)
    if not args.fix:
        print("DRY-RUN MODE (use --fix to apply changes)\n")

    for d, s in results:
        if "error" in s:
            print(f"\n{d.name}: {s['error']}", file=sys.stderr)
            continue
        if "missing_index" in s:
            print(f"\nProject: {d.name}")
            print(f"  No sessions-index.json — {s['on_disk']} .jsonl file(s) on disk")
            print(f"  Run with --fix to create index")
            continue
        if "created_from_scratch" in s:
            print(f"\nProject: {d.name}")
            print(f"  Created sessions-index.json with {s['created_from_scratch']} session(s)")
            continue
        print(f"\nProject: {d.name}")
        print(f"  Indexed: {s['indexed']}, On disk: {s['on_disk']}")
        print(f"  Orphaned: {s['orphaned']}, Dead: {s['dead']}")
        print(f"  Collisions: {s['collisions']}")
        if args.fix and s.get("fixed"):
            print(f"  -> Repaired ({s['new_total']} total)")

    print(f"\n{'='*60}")
    print(f"Totals: {totals}")
    if not args.fix and any(v > 0 for v in totals.values()):
        print("Run with --fix to repair")


if __name__ == "__main__":
    main()
