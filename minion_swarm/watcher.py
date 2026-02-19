from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CommsMessage:
    id: int
    from_agent: str
    to_agent: str
    content: str
    timestamp: str
    is_broadcast: bool
    is_cc: bool
    cc_original_to: Optional[str]


class _DbFileEventHandler(FileSystemEventHandler):
    def __init__(self, db_path: Path, signal: threading.Event) -> None:
        super().__init__()
        self.db_path = db_path.resolve()
        self.signal = signal

    def _signal_if_target(self, path: str) -> None:
        try:
            if Path(path).resolve() == self.db_path:
                self.signal.set()
        except FileNotFoundError:
            return

    def on_modified(self, event: FileSystemEvent) -> None:
        self._signal_if_target(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        self._signal_if_target(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._signal_if_target(event.dest_path)


class CommsWatcher:
    def __init__(self, agent_name: str, db_path: Path, debounce_seconds: float = 0.5) -> None:
        self.agent_name = agent_name
        self.db_path = db_path.expanduser().resolve()
        self.debounce_seconds = debounce_seconds

        self._observer: Optional[Observer] = None
        self._change_signal = threading.Event()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def start(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self._observer is not None:
            return

        observer = Observer()
        handler = _DbFileEventHandler(self.db_path, self._change_signal)
        observer.schedule(handler, str(self.db_path.parent), recursive=False)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=5.0)
        self._observer = None

    def wait_for_update(self, timeout: float = 60.0) -> bool:
        changed = self._change_signal.wait(timeout=timeout)
        if changed:
            time.sleep(self.debounce_seconds)
            self._change_signal.clear()
        return changed

    def register_agent(self, role: Optional[str] = None, description: Optional[str] = None, status: str = "online") -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agents (name, registered_at, last_seen, last_inbox_check, role, description, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    role = COALESCE(excluded.role, agents.role),
                    description = COALESCE(excluded.description, agents.description),
                    status = excluded.status
                """,
                (self.agent_name, now, now, now, role, description, status),
            )

    def set_agent_status(self, status: str) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE agents SET status = ?, last_seen = ? WHERE name = ?",
                (status, now, self.agent_name),
            )

    def unread_count(self) -> int:
        with self._connect() as conn:
            direct = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE to_agent = ? AND read_flag = 0",
                (self.agent_name,),
            ).fetchone()["c"]
            broadcast = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM messages m
                LEFT JOIN broadcast_reads br
                    ON br.agent_name = ? AND br.message_id = m.id
                WHERE m.to_agent = 'all' AND br.message_id IS NULL
                """,
                (self.agent_name,),
            ).fetchone()["c"]
        return int(direct) + int(broadcast)

    def pop_next_message(self) -> Optional[CommsMessage]:
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, from_agent, to_agent, content, timestamp, is_broadcast, is_cc, cc_original_to
                FROM (
                    SELECT id, from_agent, to_agent, content, timestamp,
                           0 AS is_broadcast, is_cc, cc_original_to
                    FROM messages
                    WHERE to_agent = ? AND read_flag = 0

                    UNION ALL

                    SELECT m.id, m.from_agent, m.to_agent, m.content, m.timestamp,
                           1 AS is_broadcast, m.is_cc, m.cc_original_to
                    FROM messages m
                    LEFT JOIN broadcast_reads br
                        ON br.agent_name = ? AND br.message_id = m.id
                    WHERE m.to_agent = 'all' AND br.message_id IS NULL
                )
                ORDER BY id ASC
                LIMIT 1
                """,
                (self.agent_name, self.agent_name),
            ).fetchone()

            if row is None:
                conn.execute(
                    "UPDATE agents SET last_seen = ?, last_inbox_check = ? WHERE name = ?",
                    (now, now, self.agent_name),
                )
                return None

            message = CommsMessage(
                id=int(row["id"]),
                from_agent=str(row["from_agent"]),
                to_agent=str(row["to_agent"]),
                content=str(row["content"]),
                timestamp=str(row["timestamp"]),
                is_broadcast=bool(row["is_broadcast"]),
                is_cc=bool(row["is_cc"]),
                cc_original_to=row["cc_original_to"],
            )

            if message.is_broadcast:
                conn.execute(
                    "INSERT OR IGNORE INTO broadcast_reads (agent_name, message_id) VALUES (?, ?)",
                    (self.agent_name, message.id),
                )
            else:
                conn.execute(
                    "UPDATE messages SET read_flag = 1 WHERE id = ?",
                    (message.id,),
                )

            conn.execute(
                "UPDATE agents SET last_seen = ?, last_inbox_check = ? WHERE name = ?",
                (now, now, self.agent_name),
            )

            return message

    def send_message(self, from_agent: str, to_agent: str, content: str, cc: Optional[str] = None) -> int:
        now = utc_now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO messages (from_agent, to_agent, content, timestamp, read_flag, is_cc, cc_original_to)
                VALUES (?, ?, ?, ?, 0, 0, NULL)
                """,
                (from_agent, to_agent, content, now),
            )
            message_id = int(cur.lastrowid)

            if cc:
                cc_content = f"{content}\n\n[CC] originally to: {to_agent}"
                conn.execute(
                    """
                    INSERT INTO messages (from_agent, to_agent, content, timestamp, read_flag, is_cc, cc_original_to)
                    VALUES (?, ?, ?, ?, 0, 1, ?)
                    """,
                    (from_agent, cc, cc_content, now, to_agent),
                )

            return message_id

    def find_lead_agent(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM agents WHERE role = 'lead' ORDER BY last_seen DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return str(row["name"])
