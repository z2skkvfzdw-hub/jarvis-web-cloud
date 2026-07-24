"""Persistent storage backends for the public Jarvis service."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def canonical_id(value: str) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


USAGE_FIELDS = (
    "requests",
    "errors",
    "estimated_requests",
    "input_tokens",
    "output_tokens",
    "estimated_cost_micros",
)


def canonical_day(value: str | date) -> date | None:
    try:
        if isinstance(value, date):
            return value
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def canonical_metric(value: str, limit: int = 80) -> str | None:
    cleaned = str(value).strip().lower()
    if not cleaned or len(cleaned) > limit:
        return None
    if not all(character.isalnum() or character in {"_", "-", ".", "/", ":", "@", "+"} for character in cleaned):
        return None
    return cleaned


def empty_usage() -> dict[str, int]:
    return {field: 0 for field in USAGE_FIELDS}


def add_usage(target: dict[str, int], values: dict[str, Any]) -> None:
    for field in USAGE_FIELDS:
        try:
            amount = max(0, int(values.get(field, 0)))
        except (TypeError, ValueError):
            amount = 0
        target[field] = target.get(field, 0) + amount


def build_launch_snapshot(
    event_rows: list[tuple[Any, str, int]],
    usage_rows: list[tuple[Any, str, str, int, int, int, int, int, int]],
    feedback: list[dict[str, Any]],
) -> dict[str, Any]:
    daily: dict[str, dict[str, Any]] = {}
    event_totals: dict[str, int] = {}
    usage_totals = empty_usage()
    provider_totals: dict[tuple[str, str], dict[str, Any]] = {}

    for raw_day, event, count in event_rows:
        day_key = str(raw_day)
        day_entry = daily.setdefault(day_key, {"day": day_key, "events": {}, "usage": empty_usage()})
        amount = max(0, int(count))
        day_entry["events"][event] = day_entry["events"].get(event, 0) + amount
        event_totals[event] = event_totals.get(event, 0) + amount

    for row in usage_rows:
        raw_day, provider, model, requests, errors, estimated, input_tokens, output_tokens, cost = row
        day_key = str(raw_day)
        values = {
            "requests": requests,
            "errors": errors,
            "estimated_requests": estimated,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_micros": cost,
        }
        day_entry = daily.setdefault(day_key, {"day": day_key, "events": {}, "usage": empty_usage()})
        add_usage(day_entry["usage"], values)
        add_usage(usage_totals, values)
        key = (provider, model)
        provider_entry = provider_totals.setdefault(
            key,
            {"provider": provider, "model": model, **empty_usage()},
        )
        add_usage(provider_entry, values)

    return {
        "events": dict(sorted(event_totals.items())),
        "usage": usage_totals,
        "daily": [daily[key] for key in sorted(daily)],
        "providers": sorted(provider_totals.values(), key=lambda item: (item["provider"], item["model"])),
        "feedback": feedback,
    }


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

    def _launch_path(self) -> Path:
        return self.data_dir / "public_launch.json"

    def _launch_data(self) -> dict[str, Any]:
        value = self._read(self._launch_path(), {})
        if not isinstance(value, dict):
            value = {}
        return {
            "events": value.get("events") if isinstance(value.get("events"), dict) else {},
            "usage": value.get("usage") if isinstance(value.get("usage"), dict) else {},
            "feedback": value.get("feedback") if isinstance(value.get("feedback"), list) else [],
        }

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

    def record_event(self, day: str, event: str, retention_days: int = 120) -> None:
        event_day = canonical_day(day)
        event_name = canonical_metric(event)
        if not event_day or not event_name:
            raise ValueError("Invalid launch event")
        cutoff = (event_day - timedelta(days=max(1, retention_days) - 1)).isoformat()
        with self._lock:
            data = self._launch_data()
            data["events"] = {
                key: value for key, value in data["events"].items() if key >= cutoff and isinstance(value, dict)
            }
            counts = data["events"].setdefault(event_day.isoformat(), {})
            counts[event_name] = max(0, int(counts.get(event_name, 0))) + 1
            self._write(self._launch_path(), data)

    def record_usage(
        self,
        day: str,
        provider: str,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_micros: int = 0,
        success: bool = True,
        estimated: bool = False,
        retention_days: int = 120,
    ) -> None:
        usage_day = canonical_day(day)
        provider_name = canonical_metric(provider, 60)
        model_name = canonical_metric(model, 160)
        if not usage_day or not provider_name or not model_name:
            raise ValueError("Invalid provider usage")
        cutoff = (usage_day - timedelta(days=max(1, retention_days) - 1)).isoformat()
        values = {
            "requests": 1,
            "errors": 0 if success else 1,
            "estimated_requests": int(bool(estimated and success)),
            "input_tokens": max(0, int(input_tokens)),
            "output_tokens": max(0, int(output_tokens)),
            "estimated_cost_micros": max(0, int(estimated_cost_micros)),
        }
        with self._lock:
            data = self._launch_data()
            data["usage"] = {
                key: value for key, value in data["usage"].items() if key >= cutoff and isinstance(value, dict)
            }
            day_entry = data["usage"].setdefault(usage_day.isoformat(), {})
            provider_entry = day_entry.setdefault(provider_name, {})
            model_entry = provider_entry.setdefault(model_name, empty_usage())
            add_usage(model_entry, values)
            self._write(self._launch_path(), data)

    def save_feedback(self, entry: dict[str, Any], retention_days: int = 365) -> str:
        feedback_id = canonical_id(str(entry.get("id", ""))) or str(uuid.uuid4())
        created_at = str(entry.get("created_at", "")).strip()
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            created = datetime.now(timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
        clean_entry = {
            "id": feedback_id,
            "created_at": created.astimezone(timezone.utc).isoformat(),
            "category": (canonical_metric(str(entry.get("category", "general")), 40) or "general"),
            "message": str(entry.get("message", ""))[:2000],
            "contact": str(entry.get("contact", ""))[:240],
        }
        with self._lock:
            data = self._launch_data()
            retained: list[dict[str, Any]] = []
            for item in data["feedback"]:
                if not isinstance(item, dict):
                    continue
                try:
                    item_time = datetime.fromisoformat(str(item.get("created_at", "")).replace("Z", "+00:00"))
                    if item_time.tzinfo is None:
                        item_time = item_time.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if item_time >= cutoff:
                    retained.append(item)
            data["feedback"] = (retained + [clean_entry])[-500:]
            self._write(self._launch_path(), data)
        return feedback_id

    def launch_snapshot(self, days: int = 30, feedback_limit: int = 50) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=max(1, days) - 1)).isoformat()
        with self._lock:
            data = self._launch_data()
        event_rows: list[tuple[Any, str, int]] = []
        for day_key, events in data["events"].items():
            if day_key < cutoff or not isinstance(events, dict):
                continue
            event_rows.extend((day_key, str(event), int(count)) for event, count in events.items())
        usage_rows: list[tuple[Any, str, str, int, int, int, int, int, int]] = []
        for day_key, providers in data["usage"].items():
            if day_key < cutoff or not isinstance(providers, dict):
                continue
            for provider, models in providers.items():
                if not isinstance(models, dict):
                    continue
                for model, values in models.items():
                    if not isinstance(values, dict):
                        continue
                    usage_rows.append(
                        (
                            day_key,
                            provider,
                            model,
                            *(max(0, int(values.get(field, 0))) for field in USAGE_FIELDS),
                        )
                    )
        feedback = [item for item in data["feedback"] if isinstance(item, dict)]
        feedback = list(reversed(feedback[-max(0, feedback_limit):]))
        return build_launch_snapshot(event_rows, usage_rows, feedback)

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
            """
            CREATE TABLE IF NOT EXISTS jarvis_daily_events (
                day DATE NOT NULL,
                event VARCHAR(80) NOT NULL,
                total BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (day, event)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS jarvis_daily_usage (
                day DATE NOT NULL,
                provider VARCHAR(60) NOT NULL,
                model VARCHAR(160) NOT NULL,
                requests BIGINT NOT NULL DEFAULT 0,
                errors BIGINT NOT NULL DEFAULT 0,
                estimated_requests BIGINT NOT NULL DEFAULT 0,
                input_tokens BIGINT NOT NULL DEFAULT 0,
                output_tokens BIGINT NOT NULL DEFAULT 0,
                estimated_cost_micros BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (day, provider, model)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS jarvis_feedback (
                id UUID PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                category VARCHAR(40) NOT NULL,
                message TEXT NOT NULL,
                contact VARCHAR(240) NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS jarvis_feedback_created
            ON jarvis_feedback(created_at DESC)
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

    def record_event(self, day: str, event: str, retention_days: int = 120) -> None:
        event_day = canonical_day(day)
        event_name = canonical_metric(event)
        if not event_day or not event_name:
            raise ValueError("Invalid launch event")
        cutoff = event_day - timedelta(days=max(1, retention_days) - 1)
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO jarvis_daily_events (day, event, total) VALUES (%s, %s, 1)
                ON CONFLICT (day, event) DO UPDATE SET total = jarvis_daily_events.total + 1
                """,
                (event_day, event_name),
            )
            cursor.execute("DELETE FROM jarvis_daily_events WHERE day < %s", (cutoff,))

    def record_usage(
        self,
        day: str,
        provider: str,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_micros: int = 0,
        success: bool = True,
        estimated: bool = False,
        retention_days: int = 120,
    ) -> None:
        usage_day = canonical_day(day)
        provider_name = canonical_metric(provider, 60)
        model_name = canonical_metric(model, 160)
        if not usage_day or not provider_name or not model_name:
            raise ValueError("Invalid provider usage")
        cutoff = usage_day - timedelta(days=max(1, retention_days) - 1)
        values = (
            1,
            0 if success else 1,
            int(bool(estimated and success)),
            max(0, int(input_tokens)),
            max(0, int(output_tokens)),
            max(0, int(estimated_cost_micros)),
        )
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO jarvis_daily_usage (
                    day, provider, model, requests, errors, estimated_requests,
                    input_tokens, output_tokens, estimated_cost_micros
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (day, provider, model) DO UPDATE SET
                    requests = jarvis_daily_usage.requests + EXCLUDED.requests,
                    errors = jarvis_daily_usage.errors + EXCLUDED.errors,
                    estimated_requests = jarvis_daily_usage.estimated_requests + EXCLUDED.estimated_requests,
                    input_tokens = jarvis_daily_usage.input_tokens + EXCLUDED.input_tokens,
                    output_tokens = jarvis_daily_usage.output_tokens + EXCLUDED.output_tokens,
                    estimated_cost_micros = jarvis_daily_usage.estimated_cost_micros + EXCLUDED.estimated_cost_micros
                """,
                (usage_day, provider_name, model_name, *values),
            )
            cursor.execute("DELETE FROM jarvis_daily_usage WHERE day < %s", (cutoff,))

    def save_feedback(self, entry: dict[str, Any], retention_days: int = 365) -> str:
        feedback_id = canonical_id(str(entry.get("id", ""))) or str(uuid.uuid4())
        try:
            created_at = datetime.fromisoformat(str(entry.get("created_at", "")).replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except ValueError:
            created_at = datetime.now(timezone.utc)
        category = canonical_metric(str(entry.get("category", "general")), 40) or "general"
        message = str(entry.get("message", ""))[:2000]
        contact = str(entry.get("contact", ""))[:240]
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO jarvis_feedback (id, created_at, category, message, contact)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (feedback_id, created_at, category, message, contact),
            )
            cursor.execute("DELETE FROM jarvis_feedback WHERE created_at < %s", (cutoff,))
        return feedback_id

    def launch_snapshot(self, days: int = 30, feedback_limit: int = 50) -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=max(1, days) - 1)
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT day, event, total FROM jarvis_daily_events WHERE day >= %s ORDER BY day, event",
                (cutoff,),
            )
            event_rows = list(cursor.fetchall())
            cursor.execute(
                """
                SELECT day, provider, model, requests, errors, estimated_requests,
                       input_tokens, output_tokens, estimated_cost_micros
                FROM jarvis_daily_usage WHERE day >= %s ORDER BY day, provider, model
                """,
                (cutoff,),
            )
            usage_rows = list(cursor.fetchall())
            cursor.execute(
                """
                SELECT id::text, created_at, category, message, contact
                FROM jarvis_feedback ORDER BY created_at DESC LIMIT %s
                """,
                (max(0, feedback_limit),),
            )
            feedback = [
                {
                    "id": feedback_id,
                    "created_at": created_at.isoformat(),
                    "category": category,
                    "message": message,
                    "contact": contact,
                }
                for feedback_id, created_at, category, message, contact in cursor.fetchall()
            ]
        return build_launch_snapshot(event_rows, usage_rows, feedback)

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
