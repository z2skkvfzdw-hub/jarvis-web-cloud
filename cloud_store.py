"""Persistent storage backends for the public Jarvis service."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any


def canonical_id(value: str) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


class FileStore:
    """Atomic local storage for development and disposable previews."""

    backend_name = "files"

    def __init__(self, data_dir: Path, persistent: bool = False) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.persistent = persistent
        self._lock = threading.RLock()

    def _device_path(self, device_id: str) -> Path:
        return self.data_dir / f"device_{device_id}.json"

    def _chat_path(self, chat_id: str, channel: str = "main") -> Path:
        prefix = "chat" if channel == "main" else "pet"
        return self.data_dir / f"{prefix}_{chat_id}.json"

    @staticmethod
    def _read(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError):
            return default

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        temporary = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = handle.name
            Path(temporary).replace(path)
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def get_device_chats(self, device_id: str) -> list[str]:
        device_id = canonical_id(device_id) or ""
        if not device_id:
            return []
        with self._lock:
            values = self._read(self._device_path(device_id), [])
            result: list[str] = []
            for value in values if isinstance(values, list) else []:
                chat_id = canonical_id(value)
                if chat_id and chat_id not in result:
                    result.append(chat_id)
            return result

    def add_device_chat(self, device_id: str, chat_id: str) -> None:
        device_id = canonical_id(device_id) or ""
        chat_id = canonical_id(chat_id) or ""
        if not device_id or not chat_id:
            raise ValueError("Invalid device or chat identifier")
        with self._lock:
            chats = self.get_device_chats(device_id)
            if chat_id not in chats:
                chats.append(chat_id)
                self._write(self._device_path(device_id), chats[-80:])

    def create_chat(self, device_id: str) -> str:
        device_id = canonical_id(device_id) or ""
        if not device_id:
            raise ValueError("Invalid device identifier")
        chat_id = str(uuid.uuid4())
        with self._lock:
            self.save_chat(chat_id, [], "main")
            self.add_device_chat(device_id, chat_id)
        return chat_id

    def chat_exists(self, chat_id: str) -> bool:
        chat_id = canonical_id(chat_id) or ""
        return bool(chat_id and self._chat_path(chat_id).is_file())

    def owns_chat(self, device_id: str, chat_id: str) -> bool:
        chat_id = canonical_id(chat_id) or ""
        return bool(chat_id and chat_id in self.get_device_chats(device_id) and self.chat_exists(chat_id))

    def load_chat(self, chat_id: str, channel: str = "main") -> list[dict[str, str]]:
        chat_id = canonical_id(chat_id) or ""
        if not chat_id:
            return []
        with self._lock:
            values = self._read(self._chat_path(chat_id, channel), [])
            if not isinstance(values, list):
                return []
            return [item for item in values if isinstance(item, dict)]

    def save_chat(self, chat_id: str, messages: list[dict[str, str]], channel: str = "main") -> None:
        chat_id = canonical_id(chat_id) or ""
        if not chat_id:
            raise ValueError("Invalid chat identifier")
        limit = 200 if channel == "main" else 120
        with self._lock:
            self._write(self._chat_path(chat_id, channel), messages[-limit:])

    def delete_chat(self, device_id: str, chat_id: str) -> bool:
        if not self.owns_chat(device_id, chat_id):
            return False
        chat_id = canonical_id(chat_id) or ""
        device_id = canonical_id(device_id) or ""
        with self._lock:
            self._chat_path(chat_id, "main").unlink(missing_ok=True)
            self._chat_path(chat_id, "pet").unlink(missing_ok=True)
            remaining = [item for item in self.get_device_chats(device_id) if item != chat_id]
            self._write(self._device_path(device_id), remaining)
        return True

    def delete_device(self, device_id: str) -> int:
        device_id = canonical_id(device_id) or ""
        if not device_id:
            return 0
        chats = self.get_device_chats(device_id)
        deleted = 0
        for chat_id in list(chats):
            deleted += int(self.delete_chat(device_id, chat_id))
        with self._lock:
            self._device_path(device_id).unlink(missing_ok=True)
        return deleted

    def health(self) -> bool:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            return self.data_dir.is_dir()
        except OSError:
            return False


class PostgresStore:
    """Postgres-backed chat storage for the deployed service."""

    backend_name = "postgres"
    persistent = True

    def __init__(self, database_url: str) -> None:
        try:
            import psycopg  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on deployment extra
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed") from exc
        self._psycopg = psycopg
        self.database_url = database_url
        self._ensure_schema()

    def _connect(self):
        return self._psycopg.connect(self.database_url, connect_timeout=8)

    def _ensure_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS jarvis_devices (
                id UUID PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS jarvis_chats (
                id UUID PRIMARY KEY,
                device_id UUID NOT NULL REFERENCES jarvis_devices(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS jarvis_chats_device_updated
            ON jarvis_chats(device_id, updated_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS jarvis_messages (
                id BIGSERIAL PRIMARY KEY,
                chat_id UUID NOT NULL REFERENCES jarvis_chats(id) ON DELETE CASCADE,
                channel TEXT NOT NULL CHECK (channel IN ('main', 'pet')),
                position INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                client_time TEXT NOT NULL DEFAULT '',
                UNIQUE(chat_id, channel, position)
            )
            """,
        )
        with self._connect() as conn:
            with conn.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)

    def _ensure_device(self, cursor: Any, device_id: str) -> None:
        cursor.execute("INSERT INTO jarvis_devices (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (device_id,))

    def get_device_chats(self, device_id: str) -> list[str]:
        device_id = canonical_id(device_id) or ""
        if not device_id:
            return []
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT id::text FROM jarvis_chats WHERE device_id = %s ORDER BY updated_at ASC LIMIT 80",
                (device_id,),
            )
            return [row[0] for row in cursor.fetchall()]

    def add_device_chat(self, device_id: str, chat_id: str) -> None:
        device_id = canonical_id(device_id) or ""
        chat_id = canonical_id(chat_id) or ""
        if not device_id or not chat_id:
            raise ValueError("Invalid device or chat identifier")
        with self._connect() as conn, conn.cursor() as cursor:
            self._ensure_device(cursor, device_id)
            cursor.execute("SELECT device_id::text FROM jarvis_chats WHERE id = %s", (chat_id,))
            row = cursor.fetchone()
            if row and row[0] != device_id:
                raise PermissionError("Chat belongs to another device")
            if not row:
                cursor.execute("INSERT INTO jarvis_chats (id, device_id) VALUES (%s, %s)", (chat_id, device_id))

    def create_chat(self, device_id: str) -> str:
        device_id = canonical_id(device_id) or ""
        if not device_id:
            raise ValueError("Invalid device identifier")
        chat_id = str(uuid.uuid4())
        self.add_device_chat(device_id, chat_id)
        return chat_id

    def chat_exists(self, chat_id: str) -> bool:
        chat_id = canonical_id(chat_id) or ""
        if not chat_id:
            return False
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM jarvis_chats WHERE id = %s", (chat_id,))
            return cursor.fetchone() is not None

    def owns_chat(self, device_id: str, chat_id: str) -> bool:
        device_id = canonical_id(device_id) or ""
        chat_id = canonical_id(chat_id) or ""
        if not device_id or not chat_id:
            return False
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM jarvis_chats WHERE id = %s AND device_id = %s",
                (chat_id, device_id),
            )
            return cursor.fetchone() is not None

    def load_chat(self, chat_id: str, channel: str = "main") -> list[dict[str, str]]:
        chat_id = canonical_id(chat_id) or ""
        if not chat_id:
            return []
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT role, content, client_time
                FROM jarvis_messages
                WHERE chat_id = %s AND channel = %s
                ORDER BY position ASC
                """,
                (chat_id, channel),
            )
            return [
                {"role": role, "content": content, "time": client_time}
                for role, content, client_time in cursor.fetchall()
            ]

    def save_chat(self, chat_id: str, messages: list[dict[str, str]], channel: str = "main") -> None:
        chat_id = canonical_id(chat_id) or ""
        if not chat_id:
            raise ValueError("Invalid chat identifier")
        limit = 200 if channel == "main" else 120
        selected = messages[-limit:]
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("DELETE FROM jarvis_messages WHERE chat_id = %s AND channel = %s", (chat_id, channel))
            cursor.executemany(
                """
                INSERT INTO jarvis_messages (chat_id, channel, position, role, content, client_time)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        chat_id,
                        channel,
                        index,
                        str(item.get("role", ""))[:40],
                        str(item.get("content", ""))[:20000],
                        str(item.get("time", ""))[:80],
                    )
                    for index, item in enumerate(selected)
                ],
            )
            cursor.execute("UPDATE jarvis_chats SET updated_at = NOW() WHERE id = %s", (chat_id,))

    def delete_chat(self, device_id: str, chat_id: str) -> bool:
        device_id = canonical_id(device_id) or ""
        chat_id = canonical_id(chat_id) or ""
        if not device_id or not chat_id:
            return False
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM jarvis_chats WHERE id = %s AND device_id = %s RETURNING id",
                (chat_id, device_id),
            )
            return cursor.fetchone() is not None

    def delete_device(self, device_id: str) -> int:
        device_id = canonical_id(device_id) or ""
        if not device_id:
            return 0
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM jarvis_chats WHERE device_id = %s", (device_id,))
            count = int(cursor.fetchone()[0])
            cursor.execute("DELETE FROM jarvis_devices WHERE id = %s", (device_id,))
            return count

    def health(self) -> bool:
        try:
            with self._connect() as conn, conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone() == (1,)
        except Exception:
            return False


def build_store(data_dir: Path):
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        return PostgresStore(database_url)
    persistent = os.environ.get("JARVIS_CLOUD_DATA_PERSISTENT", "").strip().lower() in {"1", "true", "yes", "on"}
    return FileStore(data_dir, persistent=persistent)
