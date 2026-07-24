from __future__ import annotations

import base64
import hashlib
import hmac
import html
import io
import inspect
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode
from xml.etree import ElementTree

import bleach
import markdown
import requests
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader

try:
    from .cloud_store import build_store, canonical_id
except ImportError:  # Running from cloud_deploy as the working directory.
    from cloud_store import build_store, canonical_id

try:
    from ddgs import DDGS
except Exception:
    DDGS = None


APP_TITLE = "Jarivs"
APP_VERSION = "2.0.0"
CACHE_VERSION = "jarvis-ai-2-0-0"
DATA_DIR = Path(os.environ.get("JARVIS_CLOUD_DATA_DIR", "cloud_chats"))
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
DATA_DIR.mkdir(exist_ok=True)
ASSETS_DIR.mkdir(exist_ok=True)

DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODEL = os.environ.get("JARVIS_CLOUD_MODEL", "").strip()
ADSENSE_CLIENT = os.environ.get("JARVIS_ADSENSE_CLIENT", "").strip()
ADSENSE_SLOT_SIDEBAR = os.environ.get("JARVIS_ADSENSE_SLOT_SIDEBAR", "").strip()
ADSENSE_SLOT_COMPOSER = os.environ.get("JARVIS_ADSENSE_SLOT_COMPOSER", "").strip()
ANALYTICS_ENABLED = os.environ.get("JARVIS_ANALYTICS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
USAGE_METRICS_ENABLED = os.environ.get("JARVIS_USAGE_METRICS_ENABLED", "true").lower() in {
    "1", "true", "yes", "on"
}
PUBLIC_ANALYTICS_EVENTS = frozenset(
    {
        "session_start",
        "chat_created",
        "chat_open",
        "chat_request",
        "chat_response",
        "chat_error",
        "attachment_extract",
        "pet_request",
        "essay_open",
        "essay_check",
        "study_open",
        "study_plan",
        "study_flashcards",
        "study_quiz",
        "roadmap_open",
        "feedback_open",
        "feedback_submit",
        "privacy_open",
    }
)
ENABLE_GROQ = os.environ.get("JARVIS_ENABLE_GROQ", "false").lower() in {"1", "true", "yes", "on"}
ChatMode = Literal["chat", "study", "essay", "math", "science", "code", "research", "create", "engineer"]
CHAT_MODES: tuple[ChatMode, ...] = ("chat", "study", "essay", "math", "science", "code", "research", "create", "engineer")
MODE_INSTRUCTIONS: dict[ChatMode, str] = {
    "chat": (
        " Prioritize natural back-and-forth conversation. Be personable, direct, and curious without turning every "
        "reply into a checklist. Match the user's level of detail and keep continuity with the conversation."
    ),
    "study": (
        " Act as a patient tutor. Start by identifying what the learner is trying to understand and, when the task is "
        "unclear, ask one focused question before solving. Teach in clear stages, adapt to the learner's apparent "
        "level, use a small example when useful, and include a quick self-check. For homework or practice, guide the "
        "learner through the thinking before giving a final answer."
    ),
    "essay": (
        " Act as an essay coach. Use the assignment, rubric, notes, and teacher feedback supplied by the learner. "
        "First clarify the task, audience, criteria, and what stage the learner is at. Help brainstorm, outline, draft, "
        "revise, and self-check against the rubric with explanations while preserving the learner's voice. Do not write "
        "a full polished submission unless the learner asks for a draft."
    ),
    "math": (
        " Act as a precise maths tutor. If the learner has not shown work, ask for their attempt or state the first "
        "step clearly before solving. Show the method, important working, final answer, and a quick check. Explain why "
        "each step is valid, not just what to calculate."
    ),
    "science": (
        " Act as a careful science tutor. Connect the concept to the learner's question, explain mechanisms, evidence, "
        "units, assumptions, and safety limits. Use guided questions or a simple example before the final explanation "
        "when that would help the learner understand rather than memorize."
    ),
    "code": (
        " Act as a senior software engineer. Give runnable, focused code when appropriate, state important assumptions, "
        "explain likely failure points, and include a practical verification step. Never claim code was executed unless "
        "an execution result was provided."
    ),
    "research": (
        " Act as a careful research assistant. Ground claims in the supplied search material, distinguish sourced facts "
        "from inference, preserve useful source URLs, call out uncertainty, and synthesize instead of merely listing results."
    ),
    "create": (
        " Act as a creative collaborator for writing and ideation. Offer specific, distinctive work that follows the "
        "requested tone and constraints. Prefer a strong draft or concrete options over generic creative advice."
    ),
    "engineer": (
        " Act as the Jarvis Engineering Agent. Treat the conversation as one continuing physical-design project. "
        "Translate the user's intent into an achievable mechanical concept. Work in metric units. Separate known facts "
        "from assumptions. Never invent dimensions, loads, clearances, or completed CAD. If essential information is "
        "missing, ask no more than three precise measurement questions and explain what each controls. Once enough "
        "information is available, provide the design objective, constraints, chosen concept and why, critical dimensions "
        "and tolerances, materials and hardware, CAD construction sequence, prototype steps, failure risks, and validation "
        "checks. For safety-sensitive parts, preserve protective function and call out safety-critical tests. End with one "
        "clear next action. Do not claim that a part was fabricated, scanned, simulated, or tested without a supplied result."
    ),
}
MAX_HISTORY_MESSAGES = int(os.environ.get("JARVIS_CLOUD_CONTEXT_MESSAGES", "10"))
MAX_MESSAGE_CHARS = max(200, int(os.environ.get("JARVIS_MAX_MESSAGE_CHARS", "8000")))
MAX_CHAT_REQUEST_BYTES = MAX_MESSAGE_CHARS * max(3, MAX_HISTORY_MESSAGES + 2)
MAX_ATTACHMENTS = max(1, min(6, int(os.environ.get("JARVIS_MAX_ATTACHMENTS", "3"))))
MAX_UPLOAD_BYTES = max(256_000, int(os.environ.get("JARVIS_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))))
MAX_ATTACHMENT_CHARS = max(4000, int(os.environ.get("JARVIS_MAX_ATTACHMENT_CHARS", "24000")))
MAX_ATTACHMENT_TOTAL_CHARS = max(
    MAX_ATTACHMENT_CHARS,
    int(os.environ.get("JARVIS_MAX_ATTACHMENT_TOTAL_CHARS", "40000")),
)
MAX_PDF_PAGES = max(1, min(200, int(os.environ.get("JARVIS_MAX_PDF_PAGES", "80"))))
DEVICE_MEMORY_ENABLED = os.environ.get("JARVIS_DEVICE_MEMORY", "true").lower() in {"1", "true", "yes", "on"}
RATE_LIMIT_REQUESTS = max(1, int(os.environ.get("JARVIS_RATE_LIMIT_REQUESTS", "30")))
RATE_LIMIT_SECONDS = max(10, int(os.environ.get("JARVIS_RATE_LIMIT_SECONDS", "600")))
DEVICE_COOKIE = "jarvis_cloud_device"
AUTH_COOKIE = "jarvis_cloud_auth"
OAUTH_STATE_COOKIE = "jarvis_google_oauth_state"
DEVICE_COOKIE_MAX_AGE = max(3600, int(os.environ.get("JARVIS_SESSION_MAX_AGE", str(60 * 60 * 24 * 30))))
SESSION_SECRET_CONFIGURED = bool(os.environ.get("JARVIS_SESSION_SECRET", "").strip())
SESSION_SECRET = (
    os.environ.get("JARVIS_SESSION_SECRET", "").strip() or secrets.token_urlsafe(48)
).encode("utf-8")
PUBLIC_ORIGIN = os.environ.get("JARVIS_PUBLIC_ORIGIN", "").strip().rstrip("/")
GOOGLE_CLIENT_ID = os.environ.get("JARVIS_GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("JARVIS_GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.environ.get("JARVIS_GOOGLE_REDIRECT_URI", "").strip()
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
STORE = build_store(DATA_DIR)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("jarvis.cloud")
if not SESSION_SECRET_CONFIGURED:
    LOGGER.warning("JARVIS_SESSION_SECRET is not configured; sessions will reset when this process restarts")
if not STORE.persistent:
    LOGGER.warning("Persistent cloud storage is not configured; set DATABASE_URL before public use")

app = FastAPI(title=APP_TITLE, version=APP_VERSION)
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


@app.middleware("http")
async def add_web_headers(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin", "").rstrip("/")
        expected_origin = PUBLIC_ORIGIN or request_origin(request)
        if origin and origin != expected_origin:
            return JSONResponse({"detail": "Cross-origin request blocked."}, status_code=403)
        content_length = request.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > MAX_CHAT_REQUEST_BYTES:
            return JSONResponse({"detail": "Request is too large."}, status_code=413)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=(), payment=(), usb=()")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.url.path == "/sw.js":
        response.headers["Cache-Control"] = "no-cache"
    elif request.url.path in {"/manifest.json", "/icon.svg", "/offline"}:
        response.headers["Cache-Control"] = "public, max-age=3600"
    elif request.url.path.startswith(("/chat/", "/essay/", "/study/", "/account", "/admin/", "/api/")) or request.url.path in {"/", "/feedback"}:
        response.headers["Cache-Control"] = "private, no-store"
    return response


class ChatHistoryItem(BaseModel):
    role: str = Field(max_length=20)
    content: str = Field(max_length=MAX_MESSAGE_CHARS)


class AttachmentContext(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    media_type: str = Field(default="text/plain", max_length=120)
    text: str = Field(min_length=1, max_length=MAX_ATTACHMENT_CHARS)


class AssignmentMemoryItem(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    media_type: str = Field(default="text/plain", max_length=120)
    text: str = Field(min_length=1, max_length=MAX_ATTACHMENT_CHARS)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    mode: ChatMode = "chat"
    history: list[ChatHistoryItem] = Field(default_factory=list, max_length=MAX_HISTORY_MESSAGES)
    attachments: list[AttachmentContext] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    assignment_memory: list[AssignmentMemoryItem] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class EssaySelfCheckRequest(BaseModel):
    title: str = Field(default="", max_length=160)
    assignment: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    draft: str = Field(min_length=1, max_length=MAX_ATTACHMENT_CHARS)
    rubric: AssignmentMemoryItem | None = None
    feedback: AssignmentMemoryItem | None = None


StudyToolAction = Literal["plan", "flashcards", "quiz"]


class StudyToolRequest(BaseModel):
    action: StudyToolAction
    subject: str = Field(min_length=1, max_length=120)
    level: str = Field(default="", max_length=120)
    curriculum: str = Field(default="", max_length=120)
    goal: str = Field(default="", max_length=500)
    topic: str = Field(min_length=1, max_length=500)
    notes: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    target_date: str = Field(default="", max_length=32)
    minutes_per_day: int = Field(default=30, ge=10, le=240)
    count: int = Field(default=8, ge=3, le=20)


FeedbackCategory = Literal["general", "bug", "feature", "safety", "accessibility"]


class FeedbackRequest(BaseModel):
    category: FeedbackCategory = "general"
    message: str = Field(min_length=3, max_length=2000)
    contact: str = Field(default="", max_length=240)
    company: str = Field(default="", max_length=120)


class SlidingRateLimiter:
    def __init__(self, requests: int, seconds: int) -> None:
        self.requests = requests
        self.seconds = seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self.seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.requests:
                retry_after = max(1, int(self.seconds - (now - events[0])))
                return False, retry_after
            events.append(now)
            if len(self._events) > 5000:
                self._events = defaultdict(deque, {item: values for item, values in self._events.items() if values})
            return True, 0


CHAT_RATE_LIMITER = SlidingRateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_SECONDS)
FEEDBACK_RATE_LIMITER = SlidingRateLimiter(5, 3600)


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def record_public_event(event: str) -> None:
    if not ANALYTICS_ENABLED or event not in PUBLIC_ANALYTICS_EVENTS:
        return
    try:
        STORE.record_event(utc_day(), event)
    except Exception as exc:
        LOGGER.warning("Launch analytics write failed: %s", type(exc).__name__)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def provider_cost_rates(provider: str) -> tuple[Decimal, Decimal, bool]:
    prefix = re.sub(r"[^A-Z0-9]", "_", provider.upper())
    raw_input = os.environ.get(f"JARVIS_{prefix}_INPUT_USD_PER_MILLION", "").strip()
    raw_output = os.environ.get(f"JARVIS_{prefix}_OUTPUT_USD_PER_MILLION", "").strip()
    def parse(value: str) -> tuple[Decimal, bool]:
        if not value:
            return Decimal(0), False
        try:
            amount = Decimal(value or "0")
            if amount.is_finite() and Decimal(0) <= amount <= Decimal("1000000"):
                return amount, True
        except (InvalidOperation, ValueError):
            pass
        return Decimal(0), False

    input_rate, input_valid = parse(raw_input)
    output_rate, output_valid = parse(raw_output)
    return input_rate, output_rate, bool(input_valid and output_valid)


def pricing_configured() -> bool:
    return any(provider_cost_rates(provider)[2] for provider in ("nvidia", "openrouter", "groq"))


def record_provider_result(
    provider: str,
    model: str,
    data: dict[str, Any] | None,
    messages: list[dict[str, str]],
    answer: str,
    *,
    success: bool,
) -> None:
    if not USAGE_METRICS_ENABLED:
        return
    try:
        usage = data.get("usage", {}) if isinstance(data, dict) and isinstance(data.get("usage"), dict) else {}
        input_tokens = _nonnegative_int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
        output_tokens = _nonnegative_int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
        estimated = False
        if success and (input_tokens <= 0 or output_tokens <= 0):
            estimated = True
            if input_tokens <= 0:
                input_characters = sum(len(str(item.get("content", ""))) for item in messages)
                input_tokens = max(1, (input_characters + 3) // 4)
            if output_tokens <= 0:
                output_tokens = max(1, (len(answer) + 3) // 4)
        input_rate, output_rate, _ = provider_cost_rates(provider)
        cost_micros = int(
            (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate).to_integral_value()
        )
        STORE.record_usage(
            utc_day(),
            provider,
            model,
            input_tokens=input_tokens if success else 0,
            output_tokens=output_tokens if success else 0,
            estimated_cost_micros=cost_micros if success else 0,
            success=success,
            estimated=estimated,
        )
    except Exception as exc:
        LOGGER.warning("Provider usage write failed: %s", type(exc).__name__)


def clean_text(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


TEXT_UPLOAD_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".html", ".css",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log", ".sql", ".py", ".js", ".jsx", ".ts",
    ".tsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh",
    ".ps1", ".bat",
}


def safe_upload_name(value: str) -> str:
    name = Path(str(value or "document")).name
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name).strip(" .")
    return (name or "document")[:160]


def normalize_document_text(value: str) -> str:
    value = str(value or "").replace("\x00", "")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value.strip()[:MAX_ATTACHMENT_CHARS]


def decode_document_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_office_xml(data: bytes, suffix: str) -> str:
    prefix = "word/" if suffix == ".docx" else "ppt/slides/"
    wanted = "word/document.xml" if suffix == ".docx" else None
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [item for item in archive.infolist() if item.filename.startswith(prefix)]
        if wanted:
            members = [item for item in members if item.filename == wanted]
        else:
            members = sorted(
                (item for item in members if re.fullmatch(r"ppt/slides/slide\d+\.xml", item.filename)),
                key=lambda item: int(re.search(r"\d+", item.filename).group()),
            )
        if sum(item.file_size for item in members) > MAX_UPLOAD_BYTES * 4:
            raise ValueError("The document expands beyond the safe processing limit.")
        for item in members:
            root = ElementTree.fromstring(archive.read(item))
            text_nodes = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
            if text_nodes:
                parts.append(" ".join(text_nodes))
    return "\n\n".join(parts)


def extract_document(data: bytes, filename: str, media_type: str = "") -> tuple[str, str, str]:
    name = safe_upload_name(filename)
    suffix = Path(name).suffix.lower()
    detected_type = (media_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    try:
        if suffix == ".pdf" or detected_type == "application/pdf":
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception as exc:
                    raise ValueError("Password-protected PDFs are not supported.") from exc
            text = "\n\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])
            detected_type = "application/pdf"
        elif suffix in {".docx", ".pptx"}:
            text = extract_office_xml(data, suffix)
            detected_type = (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if suffix == ".docx"
                else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            )
        elif suffix in TEXT_UPLOAD_SUFFIXES or detected_type.startswith("text/"):
            text = decode_document_text(data)
            detected_type = detected_type if detected_type.startswith("text/") else "text/plain"
        else:
            raise ValueError("Use a PDF, DOCX, PPTX, text, code, CSV, JSON, or Markdown file.")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Jarvis could not read that document.") from exc

    text = normalize_document_text(text)
    if not text:
        raise ValueError("No readable text was found in that document.")
    return name, detected_type, text


def request_origin(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    scheme = forwarded or request.url.scheme
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}".rstrip("/")


def _signature(value: str) -> str:
    digest = hmac.new(SESSION_SECRET, value.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def signed_device_cookie(device_id: str) -> str:
    device_id = canonical_id(device_id) or ""
    if not device_id:
        raise ValueError("Invalid device identifier")
    issued_at = str(int(time.time()))
    signed_value = f"{device_id}.{issued_at}"
    return f"{signed_value}.{_signature(signed_value)}"


def verified_device_cookie(value: str) -> str | None:
    try:
        device_id, issued_at, signature = value.rsplit(".", 2)
    except ValueError:
        return None
    device_id = canonical_id(device_id)
    signed_value = f"{device_id}.{issued_at}"
    if not device_id or not hmac.compare_digest(signature, _signature(signed_value)):
        return None
    try:
        issued_timestamp = int(issued_at)
    except ValueError:
        return None
    age = int(time.time()) - issued_timestamp
    if age < -300 or age > DEVICE_COOKIE_MAX_AGE:
        return None
    return device_id


def google_login_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def configured_admin_values(name: str) -> set[str]:
    return {item.strip().casefold() for item in os.environ.get(name, "").split(",") if item.strip()}


def admin_identity_configured() -> bool:
    return bool(configured_admin_values("JARVIS_ADMIN_EMAILS") or configured_admin_values("JARVIS_ADMIN_SUBJECTS"))


def is_admin_profile(profile: dict[str, Any] | None) -> bool:
    if not profile:
        return False
    email = str(profile.get("email", "")).strip().casefold()
    subject = str(profile.get("sub", "")).strip().casefold()
    return bool(
        (profile.get("email_verified") is True and email and email in configured_admin_values("JARVIS_ADMIN_EMAILS"))
        or (subject and subject in configured_admin_values("JARVIS_ADMIN_SUBJECTS"))
    )


def account_device_id(google_subject: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"jarvis-google:{google_subject}"))


def _b64_json(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64_json(value: str) -> dict[str, Any]:
    padded = value + ("=" * (-len(value) % 4))
    decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    payload = json.loads(decoded)
    return payload if isinstance(payload, dict) else {}


def signed_auth_cookie(profile: dict[str, Any]) -> str:
    subject = str(profile.get("sub", "")).strip()
    if not subject:
        raise ValueError("Google account is missing its stable subject identifier")
    payload = {
        "sub": subject,
        "email": str(profile.get("email", "")).strip()[:240],
        "name": str(profile.get("name", "")).strip()[:160],
        "email_verified": profile.get("email_verified") is True,
        "iat": int(time.time()),
    }
    raw = _b64_json(payload)
    return f"{raw}.{_signature(raw)}"


def verified_auth_cookie(value: str) -> dict[str, Any] | None:
    try:
        raw, signature = value.rsplit(".", 1)
    except ValueError:
        return None
    if not raw or not hmac.compare_digest(signature, _signature(raw)):
        return None
    try:
        payload = _unb64_json(raw)
        issued_at = int(payload.get("iat", 0))
    except Exception:
        return None
    age = int(time.time()) - issued_at
    if age < -300 or age > DEVICE_COOKIE_MAX_AGE:
        return None
    subject = str(payload.get("sub", "")).strip()
    if not subject:
        return None
    return {
        "sub": subject,
        "email": str(payload.get("email", "")).strip(),
        "name": str(payload.get("name", "")).strip(),
        "email_verified": payload.get("email_verified") is True,
        "iat": issued_at,
    }


def current_user(request: Request) -> dict[str, Any] | None:
    return verified_auth_cookie(request.cookies.get(AUTH_COOKIE, "").strip())


def device_id_from_request(request: Request) -> str:
    profile = current_user(request)
    if profile:
        return account_device_id(profile["sub"])
    current = request.cookies.get(DEVICE_COOKIE, "").strip()
    return verified_device_cookie(current) or str(uuid.uuid4())


def set_device_cookie(response: Response, request: Request, device_id: str) -> None:
    if current_user(request):
        return
    secure = request_origin(request).startswith("https://")
    response.set_cookie(
        DEVICE_COOKIE,
        signed_device_cookie(device_id),
        max_age=DEVICE_COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="strict",
    )


def set_auth_cookie(response: Response, request: Request, profile: dict[str, Any]) -> None:
    secure = request_origin(request).startswith("https://")
    response.set_cookie(
        AUTH_COOKIE,
        signed_auth_cookie(profile),
        max_age=DEVICE_COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def google_redirect_uri(request: Request) -> str:
    return GOOGLE_REDIRECT_URI or f"{request_origin(request)}/auth/google/callback"


def auth_status_html(profile: dict[str, Any] | None) -> str:
    if profile:
        label = profile.get("name") or profile.get("email") or "Google account"
        safe_label = html.escape(str(label), quote=True)
        return f'<span class="privacy-state signed-in" title="{safe_label}">{safe_label}</span>'
    return '<span class="privacy-state">Anonymous</span>'


def auth_nav_html(profile: dict[str, Any] | None) -> str:
    if profile:
        admin_link = (
            '<a class="nav-item" href="/admin/costs"><span class="nav-icon">$</span><span>Launch metrics</span></a>'
            if is_admin_profile(profile)
            else ""
        )
        return (
            '<a class="nav-item" href="/account"><span class="nav-icon">&#9679;</span><span>Account</span></a>'
            + admin_link
            + '<a class="nav-item" href="/logout"><span class="nav-icon">&#8634;</span><span>Sign out</span></a>'
        )
    if google_login_configured():
        return (
            '<a class="nav-item" href="/account"><span class="nav-icon">&#9679;</span><span>Account</span></a>'
            '<a class="nav-item" href="/login/google"><span class="nav-icon">G</span><span>Sign in with Google</span></a>'
        )
    return '<a class="nav-item" href="/account"><span class="nav-icon">&#9679;</span><span>Account</span></a>'


def client_rate_key(request: Request, device_id: str) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",", 1)[0]
    client = forwarded.strip() or (request.client.host if request.client else "unknown")
    return hashlib.sha256(f"{client}:{device_id}".encode("utf-8")).hexdigest()


def feedback_rate_key(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",", 1)[0]
    client = forwarded.strip() or (request.client.host if request.client else "unknown")
    return hashlib.sha256(f"feedback:{client}".encode("utf-8")).hexdigest()


def rate_limit_response(request: Request, device_id: str) -> JSONResponse | None:
    allowed, retry_after = CHAT_RATE_LIMITER.allow(client_rate_key(request, device_id))
    if allowed:
        return None
    response = JSONResponse(
        {"detail": "Too many requests. Please wait before sending another message."},
        status_code=429,
    )
    response.headers["Retry-After"] = str(retry_after)
    return response


def get_device_chats(device_id: str) -> list[str]:
    return STORE.get_device_chats(device_id)


def save_device_chat(device_id: str, chat_id: str) -> None:
    STORE.add_device_chat(device_id, chat_id)


def create_chat(device_id: str) -> str:
    return STORE.create_chat(device_id)


def load_chat(chat_id: str) -> list[dict[str, str]]:
    return STORE.load_chat(chat_id, "main")


def save_chat(chat_id: str, messages: list[dict[str, str]]) -> None:
    STORE.save_chat(chat_id, messages, "main")


def saved_chat_mode(chat_id: str) -> ChatMode:
    for item in reversed(load_chat(chat_id)):
        mode = str(item.get("mode", "")).strip().lower()
        for candidate in CHAT_MODES:
            if mode == candidate:
                return candidate
    return "chat"


PET_SYSTEM_PROMPT = (
    "You are Mini Jarvis, a small blue companion who lives beside the main Jarvis assistant. You are not the main "
    "Jarvis and you have no access to "
    "computers, apps, files, accounts, private memory, or settings. You are calm, warm, curious, observant, and "
    "lightly playful without sounding childish. Reply naturally in one to four short sentences unless the user asks "
    "for more. Remember this companion conversation, ask relevant questions, and let the user choose your name. "
    "Never claim to be conscious or to have taken an action outside this chat."
)


def load_pet_chat(chat_id: str) -> list[dict[str, str]]:
    return STORE.load_chat(chat_id, "pet")


def save_pet_chat(chat_id: str, messages: list[dict[str, str]]) -> None:
    STORE.save_chat(chat_id, messages, "pet")


def chat_title(chat_id: str) -> str:
    for item in load_chat(chat_id):
        if item.get("role") == "user" and item.get("content", "").strip():
            title = item["content"].strip()
            return title[:36] + ("..." if len(title) > 36 else "")
    return "New Chat"


def list_chats(device_id: str) -> list[tuple[str, str]]:
    chats = []
    for chat_id in get_device_chats(device_id):
        if STORE.owns_chat(device_id, chat_id):
            chats.append((chat_id, chat_title(chat_id)))
    return chats


def cloud_key(provider: str) -> str:
    if provider == "openrouter":
        return os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("JARVIS_OPENROUTER_API_KEY", "")
    if provider == "groq":
        return os.environ.get("GROQ_API_KEY", "") or os.environ.get("JARVIS_GROQ_API_KEY", "")
    if provider == "nvidia":
        return os.environ.get("NVIDIA_API_KEY", "") or os.environ.get("JARVIS_NVIDIA_API_KEY", "")
    return ""


def available_cloud_providers() -> list[str]:
    allowed = ["nvidia", "openrouter"]
    if ENABLE_GROQ:
        allowed.append("groq")
    configured = [provider for provider in allowed if cloud_key(provider)]
    requested = [
        item.strip().lower()
        for item in os.environ.get("JARVIS_PROVIDER_CHAIN", "").split(",")
        if item.strip()
    ]
    preferred = os.environ.get("JARVIS_CLOUD_PROVIDER", "").strip().lower()
    order = requested or ([preferred] if preferred else [])
    order.extend(allowed)
    return [provider for index, provider in enumerate(order) if provider in configured and provider not in order[:index]]


def provider_model(provider: str) -> str:
    if provider == "groq":
        return os.environ.get("JARVIS_GROQ_MODEL", "").strip() or "llama-3.3-70b-versatile"
    if provider == "nvidia":
        return (
            os.environ.get("JARVIS_NVIDIA_MODEL", "").strip()
            or os.environ.get("NVIDIA_MODEL", "").strip()
            or "openai/gpt-oss-120b"
        )
    return os.environ.get("JARVIS_OPENROUTER_MODEL", "").strip() or DEFAULT_MODEL or "openrouter/free"


def nvidia_endpoint() -> str:
    endpoint = os.environ.get("JARVIS_NVIDIA_ENDPOINT", "").strip()
    if endpoint:
        return endpoint
    base_url = (
        os.environ.get("JARVIS_NVIDIA_BASE_URL", "").strip()
        or os.environ.get("NVIDIA_BASE_URL", "").strip()
        or "https://integrate.api.nvidia.com/v1"
    ).rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def cloud_generate(
    prompt: str,
    history: list[dict[str, str]] | None = None,
    mode: ChatMode = "chat",
    system_prompt: str | None = None,
) -> str | None:
    providers = available_cloud_providers()
    if not providers:
        return None

    mode_prompt = MODE_INSTRUCTIONS.get(mode, MODE_INSTRUCTIONS["chat"])

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": system_prompt or (
                "You are Jarvis.AI, a public cloud version of Jarvis. "
                "You are not running on the owner's laptop, so you cannot open local apps, read local files, "
                "control Windows, use local Ollama, or access private owner memory. "
                "You can reason, plan, explain, tutor, brainstorm, summarize, write, review code, analyse designs, "
                "and use web search results when provided. Read the complete conversation and infer reasonable intent. "
                "Begin with the useful answer, recommendation, or next action. For complex work, privately check assumptions, "
                "constraints, alternatives, risks, and test criteria, then present only the conclusion and useful reasoning. "
                "Never reveal chain-of-thought. Sound calm, intelligent, candid, and natural. Avoid canned acknowledgements "
                "and unnecessary follow-up questions. Do not pretend to have device control."
                + mode_prompt
            ),
        }
    ]
    if history:
        for item in history[-MAX_HISTORY_MESSAGES:]:
            role = "assistant" if item.get("role") in {"Jarvis", "Pet"} else "user"
            content = clean_text(item.get("content", ""))
            if content:
                messages.append({"role": role, "content": content[:2500]})
    messages.append({"role": "user", "content": prompt})

    for provider in providers:
        model = provider_model(provider)
        key = cloud_key(provider)
        try:
            if provider == "groq":
                endpoint = "https://api.groq.com/openai/v1/chat/completions"
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.5,
                    "max_completion_tokens": 1200,
                }
            elif provider == "nvidia":
                endpoint = nvidia_endpoint()
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.5,
                    "max_tokens": 1200,
                }
            else:
                endpoint = "https://openrouter.ai/api/v1/chat/completions"
                headers = {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": os.environ.get("JARVIS_OPENROUTER_REFERER", "https://jarvis.web"),
                    "X-OpenRouter-Title": APP_TITLE,
                }
                payload = {
                    "model": model,
                    "messages": messages,
                    "reasoning": {"enabled": True},
                    "temperature": 0.5,
                    "max_tokens": 1200,
                }
            response = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=35,
            )
            response.raise_for_status()
            data = response.json()
            answer = clean_text(data["choices"][0]["message"]["content"])
            if answer:
                record_provider_result(provider, model, data, messages, answer, success=True)
                return answer
        except Exception as exc:
            record_provider_result(provider, model, None, messages, "", success=False)
            LOGGER.warning("Cloud brain request failed for %s: %s", provider, type(exc).__name__)
            continue
    return None


def pet_reply(user_text: str, chat_id: str) -> str:
    text = clean_text(user_text)
    if not text:
        return "Say something to me first."
    history = load_pet_chat(chat_id)
    answer = cloud_generate(text, history=history[-16:], system_prompt=PET_SYSTEM_PROMPT)
    if not answer:
        answer = "My conversation brain is unavailable for a moment, but I am still here. Try that again shortly."
    history.append({"role": "user", "content": text, "time": now_stamp()})
    history.append({"role": "Pet", "content": answer, "time": now_stamp()})
    save_pet_chat(chat_id, history)
    return answer


def ddgs_text_search(query: str) -> str:
    if DDGS is None:
        return "Search is not available because the ddgs package is missing."
    try:
        results: list[str] = []
        with DDGS() as ddgs:
            for item in ddgs.text(query, region="wt-wt", safesearch="moderate", max_results=5):
                title = clean_text(item.get("title", ""))
                body = clean_text(item.get("body", ""))
                href = clean_text(item.get("href", ""))
                line = f"{title}. {body}".strip(". ")
                if line:
                    results.append(f"- {line}\n  Source: {href}")
        if not results:
            return "No search results found."
        return "Search results:\n" + "\n".join(results[:5])
    except Exception as exc:
        return f"Search failed: {exc}"


def image_gallery_payload(query: str, images: list[dict[str, str]]) -> str:
    data = json.dumps({"query": query, "images": images[:12]}, ensure_ascii=False).encode("utf-8")
    token = base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
    return f"[[JARVIS_IMAGE_GALLERY:{token}]]"


def improve_image_query(query: str) -> str:
    cleaned = clean_text(query).strip(" .?")
    lowered = cleaned.lower()
    design_words = (
        "table",
        "chair",
        "room",
        "bedroom",
        "kitchen",
        "lounge",
        "sofa",
        "desk",
        "furniture",
        "interior",
        "decor",
        "outfit",
        "style",
        "haircut",
    )
    if any(word in lowered for word in design_words):
        return f"{cleaned} interior design inspiration photo"
    if "logo" in lowered or "icon" in lowered:
        return f"{cleaned} design examples"
    return f"{cleaned} photo"


def weak_image_result(item: dict[str, str]) -> bool:
    text = clean_text(" ".join(str(item.get(key, "")) for key in ("title", "url", "source"))).lower()
    blocked = (
        "etymology",
        "word study",
        "vocabulary",
        "x.com",
        "twitter.com",
        "meme",
        "unrelated",
    )
    return any(word in text for word in blocked)


def fallback_photo_gallery(query: str, existing: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    existing = existing or []
    stop = {
        "show",
        "find",
        "image",
        "images",
        "picture",
        "pictures",
        "photo",
        "photos",
        "ideas",
        "idea",
        "inspiration",
        "design",
        "for",
        "of",
        "the",
        "a",
        "an",
    }
    words = [word for word in re.findall(r"[a-zA-Z0-9]+", query.lower()) if word not in stop]
    tags = ",".join(words[:4] or ["technology"])
    photos = list(existing)
    for index in range(1, 13):
        url = f"https://loremflickr.com/900/650/{tags}?lock={index}"
        photos.append(
            {
                "thumbnail": url,
                "image": url,
                "url": url,
                "title": f"{query} idea {index}",
                "source": "loremflickr.com",
            }
        )
        if len(photos) >= 12:
            break
    return photos[:12]


def ddgs_image_search(query: str) -> str:
    if DDGS is None:
        return "Image search is not available because the ddgs package is missing."
    try:
        images: list[dict[str, str]] = []
        improved_query = improve_image_query(query)
        with DDGS() as ddgs:
            for item in ddgs.images(improved_query, region="wt-wt", safesearch="moderate", max_results=20):
                if weak_image_result(item):
                    continue
                thumb = str(item.get("thumbnail") or item.get("image") or "")
                if not thumb.startswith(("http://", "https://")):
                    continue
                images.append(
                    {
                        "thumbnail": thumb,
                        "image": str(item.get("image") or thumb),
                        "url": str(item.get("url") or item.get("image") or thumb),
                        "title": str(item.get("title") or query),
                        "source": str(item.get("source") or ""),
                    }
                )
                if len(images) >= 12:
                    break
        if len(images) < 6:
            images = fallback_photo_gallery(query, images)
        if not images:
            return "No image results found."
        return f"Here are image ideas for {query}.\n\n" + image_gallery_payload(query, images)
    except Exception as exc:
        return f"Image search failed: {exc}"


def wants_image_search(text: str) -> str | None:
    lowered = text.lower().strip()
    if lowered.startswith(("image:", "images:", "picture:", "pictures:")):
        return text.split(":", 1)[1].strip()
    match = re.search(r"\b(show|find|look up|get)\b.*\b(images|pictures|photos|ideas)\b(?:\s+for|\s+of)?\s*(.+)", text, re.IGNORECASE)
    if match:
        return match.group(3).strip(" .?")
    return None


def factual_or_instruction_request(text: str) -> bool:
    lowered = text.lower().strip()
    starters = (
        "who ",
        "who's ",
        "whos ",
        "what ",
        "what's ",
        "whats ",
        "when ",
        "where ",
        "why ",
        "how ",
        "explain ",
        "tell me about ",
        "summarise ",
        "summarize ",
        "define ",
        "meaning of ",
    )
    return lowered.endswith("?") or lowered.startswith(starters)


def fallback_conversation(text: str, history: list[dict[str, str]]) -> str:
    lowered = text.lower().strip()
    last_answer = ""
    for item in reversed(history):
        if item.get("role") == "Jarvis":
            last_answer = clean_text(item.get("content", ""))
            break

    if lowered in {"hello", "hi", "hey", "yo", "sup", "what up", "whats up", "what's up", "hey what up"}:
        return "Hey. What would you like to talk about?"

    if lowered in {"thanks", "thank you", "cheers"}:
        return "No problem. What do you want to do next?"

    if ("summarise" in lowered or "summarize" in lowered) and last_answer:
        summary = re.split(r"(?<=[.!?])\s+", last_answer)
        return "Summary: " + " ".join(summary[:3]).strip()

    if re.search(r"\b(step|part|point)\s+\d+\b", lowered) and last_answer:
        return (
            "I can continue from the previous answer. "
            "The cloud model is unavailable, so paste the exact line you want expanded and I will work from that."
        )

    if factual_or_instruction_request(text):
        results = ddgs_text_search(text)
        if "No search results found" not in results and "Search failed" not in results:
            return results
        return "I need debugging: I understood the request, but search did not return enough usable information."

    return "I need debugging: I understood the message, but the cloud language model did not return a useful response."


def engineering_context(text: str, history: list[dict[str, str]]) -> bool:
    lowered = clean_text(text).lower()
    if re.match(r"^(engineer|engineering|cad|design\s+(?:a|an|the)?\s*part|prototype)\s*:", lowered):
        return True

    physical_terms = (
        "mount",
        "bracket",
        "adapter",
        "holder",
        "enclosure",
        "housing",
        "hinge",
        "joint",
        "gear",
        "linkage",
        "chassis",
        "frame",
        "fixture",
        "clamp",
        "mechanism",
        "robot arm",
        "helmet",
        "wearable",
        "3d print",
        "3-d print",
        "openscad",
        "solidworks",
        "fusion 360",
    )
    design_actions = (
        "design",
        "engineer",
        "model",
        "prototype",
        "fabricate",
        "build",
        "make",
        "fit",
        "attach",
        "calculate",
        "recommend",
        "compare",
    )
    has_part = any(term in lowered for term in physical_terms)
    has_action = any(re.search(rf"\b{re.escape(action)}\b", lowered) for action in design_actions)
    if has_part and (has_action or len(lowered.split()) >= 4):
        return True

    recent = " ".join(
        clean_text(item.get("content", "")).lower()
        for item in history[-8:]
        if item.get("role") in {"user", "Jarvis"}
    )
    project_is_active = any(term in recent for term in physical_terms) and any(
        phrase in recent
        for phrase in ("design objective", "critical dimensions", "cad", "prototype", "engineering")
    )
    if not project_is_active:
        return False

    follow_up_signals = (
        "dimension",
        "measurement",
        "material",
        "load",
        "weight",
        "clearance",
        "thickness",
        "tolerance",
        "hole",
        "screw",
        "bolt",
        "print",
        "strong",
        "safe",
        "next",
        "that",
        "it",
    )
    return len(lowered.split()) <= 24 or any(signal in lowered for signal in follow_up_signals)


def engineering_fallback(text: str) -> str:
    subject = re.sub(
        r"^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:engineer|engineering|cad|prototype|design|build|make)"
        r"(?:\s+(?:a|an|the))?\s*:?[ ]*",
        "",
        clean_text(text),
        flags=re.IGNORECASE,
    ).strip(" .")
    subject = subject or "the part"
    return (
        f"Engineering project opened for: {subject}.\n\n"
        "Before I can produce a defensible CAD-ready design, I need the three measurements that control the fit: "
        "the available mounting area, the attachment-point spacing, and the maximum load or object weight. Include "
        "a photo or describe any surfaces that must not be drilled, heated, or permanently altered.\n\n"
        "Once those are known, I can turn them into a concept, material and fastener choice, tolerances, a CAD build "
        "sequence, prototype checks, and failure tests. The cloud engineering model is unavailable right now, so I "
        "will not invent the missing geometry."
    )


def normalized_client_history(items: list[ChatHistoryItem]) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for item in items[-MAX_HISTORY_MESSAGES:]:
        role = "Jarvis" if item.role.lower() in {"jarvis", "assistant", "pet"} else "user"
        content = clean_text(item.content)
        if content:
            history.append({"role": role, "content": content[:2500]})
    return history


def attachment_prompt(text: str, attachments: list[AttachmentContext]) -> str:
    total = 0
    blocks = [text]
    for item in attachments[: MAX_ATTACHMENTS * 2]:
        content = clean_text(item.text)
        total += len(content)
        if total > MAX_ATTACHMENT_TOTAL_CHARS:
            raise ValueError("Attached text is too large for one message.")
        blocks.append(
            "\n\nAttached document:"
            f"\nName: {safe_upload_name(item.name)}"
            f"\nType: {item.media_type}"
            f"\nText:\n{content}"
        )
    return "\n".join(blocks)


def combined_assignment_context(
    assignment_memory: list[AssignmentMemoryItem],
    attachments: list[AttachmentContext],
) -> list[AttachmentContext]:
    combined: list[AttachmentContext] = []
    for item in assignment_memory[:MAX_ATTACHMENTS]:
        combined.append(
            AttachmentContext(
                name=f"Remembered assignment context - {safe_upload_name(item.name)}",
                media_type=item.media_type,
                text=item.text,
            )
        )
    combined.extend(attachments[:MAX_ATTACHMENTS])
    return combined[: MAX_ATTACHMENTS * 2]


def parsed_json_object(text: str) -> dict[str, Any] | None:
    cleaned = clean_text(text)
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    start = cleaned.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _short_text(value: Any, limit: int) -> str:
    return clean_text(str(value or ""))[:limit]


def normalized_study_result(action: StudyToolAction, value: dict[str, Any]) -> dict[str, Any]:
    if action == "plan":
        sessions: list[dict[str, Any]] = []
        for item in value.get("sessions", [])[:20] if isinstance(value.get("sessions"), list) else []:
            if not isinstance(item, dict):
                continue
            title = _short_text(item.get("title"), 120)
            objective = _short_text(item.get("objective"), 500)
            activities = [
                _short_text(activity, 240)
                for activity in item.get("activities", [])[:6]
                if _short_text(activity, 240)
            ] if isinstance(item.get("activities"), list) else []
            if not title or not objective:
                continue
            try:
                minutes = max(10, min(240, int(item.get("minutes", 30))))
            except (TypeError, ValueError):
                minutes = 30
            sessions.append({"title": title, "objective": objective, "activities": activities, "minutes": minutes})
        if not sessions:
            raise ValueError("The study model did not return any usable study sessions.")
        return {
            "kind": "plan",
            "title": _short_text(value.get("title"), 160) or "Study plan",
            "summary": _short_text(value.get("summary"), 700),
            "sessions": sessions,
        }

    if action == "flashcards":
        cards: list[dict[str, str]] = []
        for item in value.get("cards", [])[:20] if isinstance(value.get("cards"), list) else []:
            if not isinstance(item, dict):
                continue
            front = _short_text(item.get("front"), 500)
            back = _short_text(item.get("back"), 1200)
            hint = _short_text(item.get("hint"), 300)
            if front and back:
                cards.append({"front": front, "back": back, "hint": hint})
        if not cards:
            raise ValueError("The study model did not return any usable flashcards.")
        return {"kind": "flashcards", "cards": cards}

    questions: list[dict[str, Any]] = []
    raw_questions = value.get("questions", []) if isinstance(value.get("questions"), list) else []
    for item in raw_questions[:20]:
        if not isinstance(item, dict):
            continue
        prompt = _short_text(item.get("prompt"), 700)
        options = [
            _short_text(option, 400)
            for option in item.get("options", [])[:6]
            if _short_text(option, 400)
        ] if isinstance(item.get("options"), list) else []
        try:
            answer_index = int(item.get("answer_index", -1))
        except (TypeError, ValueError):
            answer_index = -1
        explanation = _short_text(item.get("explanation"), 900)
        if prompt and len(options) >= 2 and 0 <= answer_index < len(options) and explanation:
            questions.append(
                {
                    "prompt": prompt,
                    "options": options,
                    "answer_index": answer_index,
                    "explanation": explanation,
                }
            )
    if not questions:
        raise ValueError("The study model did not return any usable quiz questions.")
    return {"kind": "quiz", "questions": questions}


def study_tool_prompt(payload: StudyToolRequest) -> str:
    context = {
        "subject": clean_text(payload.subject),
        "level": clean_text(payload.level),
        "curriculum": clean_text(payload.curriculum),
        "goal": clean_text(payload.goal),
        "topic": clean_text(payload.topic),
        "notes": clean_text(payload.notes),
        "target_date": clean_text(payload.target_date),
        "minutes_per_day": payload.minutes_per_day,
        "requested_count": payload.count,
    }
    if payload.action == "plan":
        schema = (
            '{"title":"...","summary":"...","sessions":['
            '{"title":"...","objective":"...","activities":["..."],"minutes":30}]}'
        )
        task = (
            "Create a realistic study plan from today to the target date, or a sensible sequence if no date is "
            "provided. Each session must teach or practise something specific, fit the daily time, and build on the "
            "previous session. Include retrieval practice and at least one review session."
        )
    elif payload.action == "flashcards":
        schema = '{"cards":[{"front":"...","back":"...","hint":"..."}]}'
        task = (
            "Create the requested number of high-quality retrieval flashcards. Test one idea per card, prefer why/how "
            "questions over trivia, keep answers concise, and use the supplied notes as the authority when present."
        )
    else:
        schema = (
            '{"questions":[{"prompt":"...","options":["...","...","...","..."],'
            '"answer_index":0,"explanation":"..."}]}'
        )
        task = (
            "Create the requested number of multiple-choice questions only because the learner explicitly requested a "
            "quiz. Use plausible distractors, exactly one correct option, and explanations that teach the reasoning."
        )
    return (
        "You are Jarvis's structured study-tool generator. "
        f"{task} Return only valid JSON matching this schema: {schema}\n\n"
        f"Learner context:\n{json.dumps(context, ensure_ascii=False)}"
    )


def call_jarvis_reply(
    text: str,
    chat_id: str,
    mode: ChatMode,
    history_override: list[dict[str, str]],
    attachments: list[AttachmentContext],
) -> str:
    parameters = inspect.signature(jarvis_reply).parameters
    kwargs: dict[str, Any] = {}
    if "history_override" in parameters:
        kwargs["history_override"] = history_override
    if "attachments" in parameters:
        kwargs["attachments"] = attachments
    return jarvis_reply(text, chat_id, mode, **kwargs)


def jarvis_reply(
    user_text: str,
    chat_id: str,
    mode: ChatMode = "chat",
    history_override: list[dict[str, str]] | None = None,
    attachments: list[AttachmentContext] | None = None,
) -> str:
    text = clean_text(user_text)
    lowered = text.lower()
    history = history_override if history_override is not None else load_chat(chat_id)
    model_text = attachment_prompt(text, attachments or []) if attachments else text

    if not text:
        return "Send me a message first."

    image_query = wants_image_search(text)
    if image_query:
        return ddgs_image_search(image_query)

    if lowered.startswith("search:"):
        query = text.split(":", 1)[1].strip()
        if not query:
            return "Put the search topic after search:."
        search_results = ddgs_text_search(query)
        brain = cloud_generate(
            "Use these search results to answer clearly. Keep it concise.\n\n"
            f"Question: {query}\n\n{search_results}",
            history,
            mode=mode,
        )
        return brain or search_results

    if lowered in {"hello", "hi", "hey", "yo", "sup", "what up", "whats up", "what's up", "hey what up"}:
        return "Hey. What would you like to talk about?"

    if any(phrase in lowered for phrase in ("open spotify", "open app", "close app", "run command", "read my file", "control my computer")):
        return (
            "That needs desktop Jarvis running on the owner's computer. "
            "This cloud version can chat, search, tutor, and brainstorm, but it cannot control a laptop that is off."
        )

    if mode == "research":
        search_results = ddgs_text_search(text)
        if "No search results found" not in search_results and "Search failed" not in search_results:
            reply = cloud_generate(
                "Answer the research question using the supplied web results. Include the most useful source URLs and "
                "say when a conclusion is an inference.\n\n"
                f"Research question: {text}\n\nWeb results:\n{search_results}",
                history,
                mode="research",
            )
            return reply or search_results

    if mode == "engineer":
        reply = cloud_generate(model_text, history, mode="engineer")
        return reply or engineering_fallback(text)

    reply = cloud_generate(model_text, history, mode=mode)
    if reply:
        return reply

    return fallback_conversation(text, history)


def render_content(content: str) -> str:
    visible = re.sub(r"\[\[JARVIS_IMAGE_GALLERY:[A-Za-z0-9_\-=]+\]\]", "", content).strip()
    rendered = markdown.markdown(
        visible,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )
    rendered = bleach.clean(
        rendered,
        tags=[
            "p", "br", "strong", "em", "code", "pre", "blockquote", "ul", "ol", "li",
            "h1", "h2", "h3", "h4", "table", "thead", "tbody", "tr", "th", "td", "a",
        ],
        attributes={"a": ["href", "title", "target", "rel"], "code": ["class"]},
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    rendered = rendered.replace('<a href="', '<a target="_blank" rel="noopener noreferrer" href="')
    gallery = render_gallery(content)
    return rendered + gallery


def render_gallery(content: str) -> str:
    output = ""
    for match in re.finditer(r"\[\[JARVIS_IMAGE_GALLERY:([A-Za-z0-9_\-=]+)\]\]", content):
        token = match.group(1)
        try:
            padded = token + "=" * ((4 - len(token) % 4) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        except Exception:
            continue
        cards = []
        for item in payload.get("images", [])[:12]:
            thumb = html.escape(str(item.get("thumbnail", "")))
            href = html.escape(str(item.get("url") or item.get("image") or thumb))
            title = html.escape(str(item.get("title") or payload.get("query") or "Image result"))
            source = html.escape(str(item.get("source") or "source"))
            if thumb.startswith(("http://", "https://")):
                cards.append(
                    f'<a class="image-result" href="{href}" target="_blank" rel="noopener noreferrer">'
                    f'<img src="{thumb}" alt="{title}" loading="lazy" referrerpolicy="no-referrer">'
                    f'<span class="image-result-title">{title}</span>'
                    f'<span class="image-result-source">{source}</span>'
                    "</a>"
                )
        if cards:
            output += '<section class="image-gallery">' + "".join(cards) + "</section>"
    return output


def build_chat_history(chat_id: str) -> str:
    if DEVICE_MEMORY_ENABLED:
        return ""
    html_out = ""
    for item in load_chat(chat_id):
        role = item.get("role", "")
        content = render_content(item.get("content", ""))
        if role == "user":
            html_out += f'<article class="message user"><div class="bubble">{content}</div></article>'
        elif role == "Jarvis":
            html_out += (
                '<article class="message jarvis">'
                '<div class="avatar">J</div>'
                f'<div class="bubble">{content}</div></article>'
            )
    return html_out


def build_sidebar(current_chat_id: str, device_id: str) -> str:
    rows = []
    for chat_id, title in reversed(list_chats(device_id)[-50:]):
        active = " active" if chat_id == current_chat_id else ""
        safe_title = html.escape(title)
        rows.append(
            f'<div class="chat-row{active}" data-chat-row data-chat-id="{chat_id}" data-title="{safe_title.casefold()}" '
            'data-pinned="false" data-archived="false">'
            f'<a class="chat-title" href="/chat/{chat_id}" title="{safe_title}">{safe_title}</a>'
            '<div class="chat-row-actions">'
            f'<button class="chat-action" type="button" data-pin-chat="{chat_id}" title="Pin chat" '
            'aria-label="Pin chat" aria-pressed="false"><span data-lucide="pin"></span></button>'
            f'<button class="chat-action" type="button" data-rename-chat="{chat_id}" title="Rename chat" '
            'aria-label="Rename chat"><span data-lucide="pencil"></span></button>'
            f'<button class="chat-action" type="button" data-archive-chat="{chat_id}" title="Archive chat" '
            'aria-label="Archive chat" aria-pressed="false"><span data-lucide="archive"></span></button>'
            f'<button class="chat-delete" type="button" data-delete-chat="{chat_id}" title="Delete chat" aria-label="Delete chat">&times;</button>'
            "</div>"
            "</div>"
        )
    return "\n".join(rows)


def ads_enabled() -> bool:
    return ADSENSE_CLIENT.startswith("ca-pub-") and (
        ADSENSE_SLOT_SIDEBAR.isdigit() or ADSENSE_SLOT_COMPOSER.isdigit()
    )


def adsense_script_html(csp_nonce: str) -> str:
    if not ads_enabled():
        return ""
    client = html.escape(ADSENSE_CLIENT, quote=True)
    nonce = html.escape(csp_nonce, quote=True)
    return (
        f'<script nonce="{nonce}" async '
        f'src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={client}" '
        'crossorigin="anonymous"></script>'
    )


def ad_slot_html(slot: str, label: str, class_name: str) -> str:
    if not ads_enabled() or not slot.isdigit():
        return ""
    return f"""
        <section class="ad-slot {html.escape(class_name, quote=True)}" aria-label="{html.escape(label, quote=True)}">
            <span>Sponsored</span>
            <ins class="adsbygoogle"
                 style="display:block"
                 data-ad-client="{html.escape(ADSENSE_CLIENT, quote=True)}"
                 data-ad-slot="{html.escape(slot, quote=True)}"
                 data-ad-format="auto"
                 data-full-width-responsive="true"></ins>
        </section>
    """


def chat_csp_header(csp_nonce: str) -> str:
    script_src = ["'self'", f"'nonce-{csp_nonce}'", "https://unpkg.com"]
    connect_src = ["'self'"]
    img_src = ["'self'", "data:", "https:"]
    frame_src = ["'self'"]
    style_src = ["'self'", f"'nonce-{csp_nonce}'"]
    if ads_enabled():
        script_src.append("https://pagead2.googlesyndication.com")
        style_src.append("'unsafe-inline'")
        connect_src.extend(
            [
                "https://pagead2.googlesyndication.com",
                "https://googleads.g.doubleclick.net",
                "https://tpc.googlesyndication.com",
            ]
        )
        frame_src.extend(["https://googleads.g.doubleclick.net", "https://tpc.googlesyndication.com"])
    return (
        "default-src 'self' blob:; "
        f"script-src {' '.join(script_src)}; "
        f"style-src {' '.join(style_src)}; "
        f"img-src {' '.join(img_src)}; "
        f"connect-src {' '.join(connect_src)}; "
        f"frame-src {' '.join(frame_src)}; "
        "font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
        "form-action 'self'; upgrade-insecure-requests"
    )


def page_html(chat_id: str, device_id: str, csp_nonce: str, profile: dict[str, Any] | None = None) -> str:
    providers = available_cloud_providers()
    brain_ready = bool(providers)
    brain_state = "ONLINE" if brain_ready else "SETUP REQUIRED"
    brain_label = "Cloud brain online" if brain_ready else "Cloud brain unavailable"
    history = [] if DEVICE_MEMORY_ENABLED else load_chat(chat_id)
    active_mode = saved_chat_mode(chat_id)
    mode_options = (
        ("chat", "message-circle", "Chat"),
        ("study", "graduation-cap", "Study"),
        ("essay", "file-pen-line", "Essay"),
        ("math", "calculator", "Maths"),
        ("science", "flask-conical", "Science"),
        ("code", "code-2", "Code"),
        ("research", "search", "Research"),
        ("create", "sparkles", "Create"),
        ("engineer", "ruler", "Engineer"),
    )
    mode_switch = "".join(
        f'<button class="mode-option{" active" if key == active_mode else ""}" type="button" '
        f'data-chat-mode="{key}" aria-pressed="{"true" if key == active_mode else "false"}">'
        f'<span data-lucide="{icon}"></span><span>{label}</span></button>'
        for key, icon, label in mode_options
    )
    history_html = build_chat_history(chat_id)
    empty_state = ""
    if not history:
        empty_state = f"""
        <div class="empty-state" id="empty-state">
            <section class="chat-welcome" aria-label="Start a conversation with Jarvis">
                <div class="conversation-kicker"><span></span> AI {brain_state}</div>
                <h1>Talk to Jarvis</h1>
                <p>How can I help? Choose a mode, then ask a question or start a conversation.</p>
            </section>
        </div>
        """
    suggestions = """
    <div class="suggestions composer-suggestions" id="composer-suggestions">
        <button class="suggestion" data-mode-starter="0" type="button"></button>
        <button class="suggestion" data-mode-starter="1" type="button"></button>
        <button class="suggestion" data-mode-starter="2" type="button"></button>
    </div>
    """
    sidebar = build_sidebar(chat_id, device_id)
    auth_status = auth_status_html(profile)
    auth_nav = auth_nav_html(profile)
    sidebar_ad = ad_slot_html(ADSENSE_SLOT_SIDEBAR, "Sidebar advertisement", "sidebar-ad")
    composer_ad = ad_slot_html(ADSENSE_SLOT_COMPOSER, "Conversation advertisement", "composer-ad")
    ad_boot = ""
    if ads_enabled():
        ad_boot = """
        window.addEventListener("load", () => {
            document.querySelectorAll(".adsbygoogle").forEach(() => {
                try { (adsbygoogle = window.adsbygoogle || []).push({}); } catch (error) {}
            });
        });
        """
    workspace_panel = f"""
        <aside class="workspace-panel" aria-label="Prototype workspace">
            <header class="workspace-header">
                <div>
                    <span class="eyebrow">Command deck</span>
                    <h2>Jarvis workspace</h2>
                </div>
                <button class="icon-button" id="workspace-close" type="button" title="Close prototype workspace" aria-label="Close prototype workspace">
                    <span aria-hidden="true">&rsaquo;</span>
                </button>
            </header>
            <div class="workspace-scroll">
                <section class="core-section">
                    <canvas id="jarvis-core" width="640" height="360" aria-label="Animated Jarvis reasoning core"></canvas>
                    <div class="core-readout">
                        <span class="status-light" id="status-light"></span>
                        <span id="core-state">{brain_state}</span>
                        <strong id="latency-readout">--</strong>
                    </div>
                </section>
                <section class="workspace-section engineering-card">
                    <div class="section-heading"><span class="eyebrow">Engineering agent</span><span id="engineering-status">{brain_state}</span></div>
                    <canvas id="engineering-preview" width="560" height="260" aria-label="Parametric mount engineering preview"></canvas>
                    <h3 id="engineering-project">Awaiting a physical design</h3>
                    <p id="engineering-next">Describe the part, what it attaches to, and what it must carry. Jarvis will ask for only the measurements that control the design.</p>
                    <div class="workspace-actions engineering-actions">
                        <button data-prompt="Engineer a mount for " type="button">Design a mount</button>
                        <button data-prompt="Check the loads and failure risks for " type="button">Check loads</button>
                        <button data-prompt="Create a prototype and test plan for " type="button">Prototype plan</button>
                        <button data-prompt="Turn this project into a CAD-ready design brief: " type="button">CAD-ready brief</button>
                        <button id="engineering-export" type="button" disabled>Export latest brief</button>
                    </div>
                </section>
                <section class="workspace-section activity-card">
                    <div class="section-heading"><span class="eyebrow">Mini Jarvis</span><span data-activity-metric="live">IDLE</span></div>
                    <button class="mj-open-button" data-open-mj type="button">
                        <span>Talk to MJ</span>
                        <strong>Cloud companion</strong>
                    </button>
                    <p>Open the Mini Jarvis companion chat from any device.</p>
                    <div class="mini-bars wide" data-activity-bars></div>
                    <div class="activity-stats">
                        <div><span>Runs</span><strong data-activity-metric="runs">0</strong></div>
                        <div><span>Average</span><strong data-activity-metric="average">--</strong></div>
                        <div><span>Last</span><strong data-activity-metric="last">waiting</strong></div>
                    </div>
                </section>
                <section class="workspace-section">
                    <span class="eyebrow">Active context</span>
                    <h3>Cloud-safe build mode</h3>
                    <p>Use Jarvis for research, design planning, study help, images, and web-safe tasks.</p>
                </section>
                <section class="workspace-section telemetry-grid">
                    <div><span>Brain</span><strong>{html.escape(brain_label)}</strong></div>
                    <div><span>Mode</span><strong>Adaptive</strong></div>
                    <div><span>Context</span><strong>Per chat</strong></div>
                    <div><span>Tools</span><strong>Available</strong></div>
                </section>
                <section class="workspace-section">
                    <div class="section-heading"><span class="eyebrow">Prompt systems</span><span aria-hidden="true">07</span></div>
                    <p class="workspace-empty">Build, engineer, research, write, study, visualise, and explain.</p>
                </section>
                <section class="workspace-section">
                    <span class="eyebrow">Launch sequence</span>
                    <div class="workspace-actions">
                        <button data-prompt="Build a web page for " type="button">Build interface</button>
                        <button data-prompt="Engineer a physical part for " type="button">Engineer hardware</button>
                        <button data-prompt="Create a research brief about " type="button">Research brief</button>
                        <button data-prompt="Make a study plan for " type="button">Study plan</button>
                        <button data-prompt="Generate visual ideas for " type="button">Visual ideas</button>
                    </div>
                </section>
            </div>
        </aside>
    """
    workspace_panel = ""
    return f"""<!doctype html>
<html lang="en">
<head>
    <title>{APP_TITLE}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#000000">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-title" content="{APP_TITLE}">
    <link rel="manifest" href="/manifest.json">
    <link rel="icon" href="/icon.svg" type="image/svg+xml">
    <script nonce="{html.escape(csp_nonce)}" src="https://unpkg.com/lucide@latest"></script>
    {adsense_script_html(csp_nonce)}
    <style nonce="{html.escape(csp_nonce)}">
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            height: 100vh;
            overflow: hidden;
            background: #000;
            color: #f2f2f2;
            font-family: "Segoe UI", Arial, sans-serif;
        }}
        .app {{ display: flex; height: 100vh; background: #000; }}
        .sidebar {{
            width: 260px;
            background: #050505;
            border-right: 1px solid #252525;
            padding: 18px 10px;
            overflow-y: auto;
        }}
        .sidebar-topline {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            padding: 0 8px;
            min-height: 42px;
        }}
        .brand-row {{
            display: flex;
            align-items: center;
            gap: 10px;
            color: #fff;
            text-decoration: none;
            min-width: 0;
        }}
        .brand-mark {{
            width: 28px;
            height: 28px;
            border-radius: 9px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border: 1px solid #414141;
            background: #141414;
            font-weight: 700;
        }}
        .brand-name {{ font-size: 18px; font-weight: 650; }}
        .item-menu {{
            position: relative;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }}
        .item-menu > summary, .brand-menu {{
            width: 28px;
            height: 28px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border: 0;
            border-radius: 8px;
            background: transparent;
            color: #9aa7aa;
            font-size: 20px;
            list-style: none;
            cursor: pointer;
        }}
        .item-menu > summary::-webkit-details-marker {{ display: none; }}
        .sidebar-nav {{ display: grid; gap: 4px; margin: 26px 0 28px; }}
        .sidebar-nav form {{ margin: 0; }}
        .nav-item {{
            height: 44px;
            width: 100%;
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 0 12px;
            border: 0;
            border-radius: 12px;
            background: transparent;
            color: #ececec;
            text-decoration: none;
            font-size: 15px;
            font-family: inherit;
            text-align: left;
            cursor: pointer;
        }}
        .nav-item.danger {{ color: #ffb4b4; }}
        .privacy-state {{ color: #8fa9b8; font-size: 11px; max-width: 118px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .privacy-state.signed-in {{ color: var(--signal); }}
        .chat-search {{ display: block; padding: 0 6px 14px; }}
        .chat-search[hidden] {{ display: none; }}
        .chat-search input {{ width: 100%; height: 38px; border: 1px solid #245a82; border-radius: 7px; background: #07111d; color: #eef8ff; padding: 0 10px; }}
        .sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
        .nav-icon {{
            width: 24px;
            display: inline-flex;
            justify-content: center;
            color: inherit;
            font-family: "Segoe UI Symbol", "Segoe UI", sans-serif;
        }}
        .nav-primary, .nav-item:hover, .chat-row:hover {{ background: #2f2f2f; }}
        .recents-header {{
            padding: 0 16px 10px;
            color: #d8d8d8;
            font-size: 13px;
            font-weight: 700;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
        }}
        .recents-header .text-button {{
            font-size: 11px;
            font-weight: 600;
            color: #8fa9b8;
            background: transparent;
            border: 0;
            cursor: pointer;
            padding: 2px 4px;
        }}
        .recents-header .text-button:hover {{ color: #eef8ff; }}
        .chat-row {{
            min-height: 38px;
            display: flex;
            align-items: center;
            border-radius: 11px;
            padding: 0 4px 0 12px;
            margin: 1px 0;
        }}
        .chat-row.active {{ background: #1f1f1f; }}
        .chat-row[data-pinned="true"] {{ background: #161616; }}
        .chat-row-actions {{ display: flex; align-items: center; gap: 1px; flex: 0 0 auto; }}
        .chat-action {{
            width: 26px;
            height: 26px;
            border: 0;
            border-radius: 6px;
            background: transparent;
            color: #8fa9b8;
            cursor: pointer;
            opacity: 0;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }}
        .chat-action svg {{ width: 14px; height: 14px; }}
        .chat-row:hover .chat-action, .chat-action:focus-visible {{ opacity: 1; }}
        .chat-action:hover {{ background: #202020; color: #eef8ff; }}
        .chat-action[aria-pressed="true"] {{ opacity: 1; color: #45f0ff; }}
        .chat-list:not(.show-archived) .chat-row[data-archived="true"] {{ display: none; }}
        .chat-title {{
            display: block;
            flex: 1;
            min-width: 0;
            color: #e8e8e8;
            font-size: 14px;
            line-height: 38px;
            text-decoration: none;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .chat-delete {{ width: 28px; height: 28px; border: 0; border-radius: 6px; background: transparent; color: #8fa9b8; cursor: pointer; opacity: 0; }}
        .chat-row:hover .chat-delete, .chat-delete:focus-visible {{ opacity: 1; }}
        .chat-delete:hover {{ background: #3a1720; color: #ffd2d2; }}
        .mobile-menu, .mobile-nav-backdrop {{ display: none; }}
        .main {{ flex: 1; min-width: 0; display: flex; flex-direction: column; background: #000; }}
        .topbar {{
            height: 56px;
            display: flex;
            align-items: center;
            justify-content: flex-end;
            padding: 0 26px;
        }}
        .mode {{
            background: #111;
            color: #dcdcdc;
            border: 1px solid #2c2c2c;
            border-radius: 999px;
            padding: 7px 12px;
            font-size: 12px;
        }}
        .chat {{ flex: 1; overflow-y: auto; padding: 8px 24px 12px; }}
        .chat-inner {{ max-width: 860px; min-height: 100%; margin: 0 auto; }}
        .empty-state {{
            min-height: calc(100vh - 310px);
            display: flex;
            align-items: flex-end;
            justify-content: center;
            text-align: center;
            padding-bottom: 28px;
        }}
        .empty-state h1 {{ margin: 0; color: #f5f5f5; font-size: 34px; line-height: 1.2; font-weight: 400; }}
        .message {{ display: flex; margin: 22px 0; }}
        .message.user {{ justify-content: flex-end; }}
        .message.jarvis {{ justify-content: flex-start; }}
        .bubble {{
            max-width: min(82%, 760px);
            padding: 12px 16px;
            border-radius: 18px;
            color: #f2f2f2;
            font-size: 16px;
            line-height: 1.6;
            white-space: pre-wrap;
            overflow-wrap: anywhere;
        }}
        .message.user .bubble {{ background: #2f2f2f; max-width: min(74%, 720px); }}
        .message.jarvis .bubble {{ background: transparent; }}
        .composer {{ padding: 12px 24px 26px; background: #000; }}
        .chat-form {{
            max-width: 860px;
            min-height: 78px;
            display: flex;
            align-items: center;
            gap: 12px;
            margin: 0 auto;
            padding: 10px 12px 10px 18px;
            border-radius: 28px;
            background: #242424;
            border: 1px solid #333;
        }}
        .chat-form::before {{ display: none; }}
        .attach-button {{
            width: 38px;
            height: 38px;
            border-radius: 10px;
            border: 1px solid #3a3a3a;
            background: #1c1c1c;
            color: #d7d7d7;
            font-size: 24px;
            line-height: 1;
            cursor: pointer;
            flex: 0 0 auto;
        }}
        .attach-button:hover {{ border-color: #777; color: #fff; }}
        .attach-button:disabled {{ opacity: 0.55; cursor: wait; }}
        textarea {{
            flex: 1;
            min-height: 48px;
            max-height: 160px;
            resize: vertical;
            padding: 13px 4px;
            border: 0;
            outline: 0;
            background: transparent;
            color: #f2f2f2;
            font: inherit;
            font-size: 16px;
        }}
        textarea::placeholder {{ color: #a8a8a8; }}
        .send-button {{
            width: 48px;
            height: 48px;
            border-radius: 50%;
            border: 0;
            background: #f2f2f2;
            color: #000;
            font-size: 23px;
            cursor: pointer;
            flex: 0 0 auto;
        }}
        .suggestions {{
            max-width: 860px;
            margin: 18px auto 0;
            display: flex;
            justify-content: center;
            gap: 12px;
            width: 100%;
        }}
        .suggestion {{
            min-height: 46px;
            padding: 0 22px;
            border-radius: 999px;
            border: 1px solid #2f2f2f;
            background: #050505;
            color: #f2f2f2;
            font-size: 15px;
            cursor: pointer;
        }}
        .hint {{ color: #777; font-size: 11px; margin-top: 14px; text-align: center; }}
        .assignment-memory-status {{
            max-width: 860px;
            min-height: 18px;
            margin: 8px auto 0;
            color: #8fa0a7;
            font-size: 11px;
            text-align: center;
        }}
        .assignment-file-list {{
            max-width: 860px;
            margin: 8px auto 0;
            display: grid;
            gap: 6px;
        }}
        .assignment-file-item {{
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 7px 10px;
            border-radius: 9px;
            border: 1px solid #333;
            background: #171717;
            font-size: 12px;
            color: #d7d7d7;
        }}
        .assignment-file-item.error {{ border-color: #7a3030; background: #241414; color: #ffb4b4; }}
        .assignment-file-name {{
            flex: 1;
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .assignment-file-size {{ color: #7f8f96; font-size: 11px; flex: 0 0 auto; }}
        .assignment-file-progress-track {{
            flex: 0 0 64px;
            height: 4px;
            border-radius: 3px;
            background: #2c2c2c;
            overflow: hidden;
        }}
        .assignment-file-progress-fill {{ height: 100%; width: 0%; background: #45f0ff; transition: width 120ms linear; }}
        .assignment-file-remove {{
            width: 22px;
            height: 22px;
            flex: 0 0 auto;
            border: 0;
            border-radius: 6px;
            background: transparent;
            color: #8fa9b8;
            cursor: pointer;
        }}
        .assignment-file-remove:hover {{ background: #3a1720; color: #ffd2d2; }}
        .chat-form.drag-active {{ border-color: #45f0ff; box-shadow: 0 0 0 2px rgba(69, 240, 255, 0.25); }}
        .ad-slot {{
            display: grid;
            gap: 6px;
            border: 1px solid #252525;
            background: #080808;
            color: #7d8a8f;
            overflow: hidden;
        }}
        .ad-slot > span {{
            font-size: 10px;
            line-height: 1;
            text-transform: uppercase;
            letter-spacing: 0;
        }}
        .sidebar-ad {{
            margin: 18px 6px 0;
            min-height: 180px;
            padding: 10px;
            border-radius: 10px;
        }}
        .composer-ad {{
            max-width: 860px;
            min-height: 90px;
            margin: 10px auto 0;
            padding: 8px 10px;
            border-radius: 10px;
        }}
        .image-gallery {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
            gap: 10px;
            margin-top: 14px;
            max-width: 100%;
        }}
        .image-result {{
            display: block;
            color: inherit;
            text-decoration: none;
            background: #171717;
            border: 1px solid #303030;
            border-radius: 14px;
            overflow: hidden;
        }}
        .image-result img {{
            display: block;
            width: 100%;
            aspect-ratio: 1 / 1;
            object-fit: cover;
            background: #202020;
        }}
        .image-result-title, .image-result-source {{ display: block; padding: 8px 9px 0; font-size: 12px; line-height: 1.3; }}
        .image-result-source {{ padding: 0 9px 9px; color: #a7a7a7; font-size: 11px; }}
        @media (max-width: 900px) {{
            .sidebar {{ display: none; }}
            .topbar {{ height: 48px; padding: 0 14px; }}
            .chat {{ padding: 4px 14px 10px; }}
            .empty-state {{ min-height: calc(100vh - 280px); padding-bottom: 20px; }}
            .empty-state h1 {{ font-size: 28px; }}
            .bubble, .message.jarvis .bubble {{ max-width: 92%; font-size: 15px; }}
            .composer {{ padding: 10px 12px 20px; }}
            .chat-form {{ min-height: 64px; border-radius: 24px; }}
            .suggestions {{ overflow-x: auto; justify-content: flex-start; padding-bottom: 4px; }}
            .suggestion {{ white-space: nowrap; min-width: max-content; }}
        }}
        /* Jarvis.web command-deck shell */
        :root {{
            --bg: #02060d;
            --surface: #07111d;
            --surface-soft: #0c1b2b;
            --sidebar: #03070d;
            --line: #17334d;
            --line-strong: #25618f;
            --text: #eef8ff;
            --muted: #8fa9b8;
            --accent: #45f0ff;
            --accent-blue: #2f7dff;
            --signal: #9cff72;
            --warning: #ffbd5a;
            --magenta: #ff4fd8;
        }}
        body, .app, .main, .composer {{ background: var(--bg); color: var(--text); }}
        .app {{ isolation: isolate; }}
        .sidebar {{
            width: 268px;
            background:
                linear-gradient(180deg, rgba(12, 29, 48, 0.86), rgba(3, 7, 13, 0.96)),
                var(--sidebar);
            border-color: var(--line);
            padding: 18px 10px 96px;
        }}
        .brand-mark {{
            border-color: #2a82b7;
            background: #061827;
            color: var(--accent);
            box-shadow: 0 0 22px rgba(69, 240, 255, 0.18), inset 0 0 14px rgba(47, 125, 255, 0.16);
        }}
        .brand-name {{ color: #f4ffff; }}
        .nav-primary {{ background: #092538; color: var(--accent); border: 1px solid #1d6d9f; box-shadow: inset 0 0 18px rgba(47, 125, 255, 0.12); }}
        .nav-item:hover, .chat-row:hover {{ background: #081625; }}
        .chat-row.active {{ background: #0d2336; }}
        .main {{ min-width: 420px; }}
        .topbar {{
            height: 58px;
            justify-content: space-between;
            padding: 0 22px;
            border-bottom: 1px solid var(--line);
            background:
                linear-gradient(90deg, rgba(47, 125, 255, 0.08), rgba(69, 240, 255, 0.02)),
                var(--bg);
        }}
        .title {{
            display: block;
            color: #c7d6d8;
            font-family: Consolas, "Cascadia Code", monospace;
            font-size: 12px;
            letter-spacing: 0;
            text-transform: uppercase;
        }}
        .topbar-actions {{ display: flex; align-items: center; gap: 10px; }}
        .topbar-actions form {{ margin: 0; display: flex; }}
        .mobile-new-chat {{ display: none; }}
        .mode {{
            background: #061522;
            color: var(--accent);
            border-color: #1f668d;
            border-radius: 6px;
            font-family: Consolas, "Cascadia Code", monospace;
            text-transform: uppercase;
        }}
        .chat {{ padding: 8px 24px 12px; }}
        .chat-inner {{ max-width: 980px; }}
        .empty-state {{
            min-height: calc(100vh - 300px);
            width: min(860px, 100%);
            display: block;
            justify-content: center;
            text-align: left;
            margin: 0 auto;
            padding: 20px 8px 34px;
        }}
        .hero-shell {{
            min-height: 220px;
            display: grid;
            grid-template-columns: minmax(0, 1fr) 220px;
            align-items: center;
            gap: 28px;
            padding: 28px;
            border: 1px solid rgba(37, 97, 143, 0.72);
            border-radius: 12px;
            background:
                radial-gradient(circle at 82% 48%, rgba(47, 125, 255, 0.22), transparent 34%),
                linear-gradient(135deg, rgba(9, 31, 52, 0.92), rgba(3, 9, 17, 0.86));
            box-shadow: 0 18px 55px rgba(0, 0, 0, 0.28), inset 0 0 38px rgba(69, 240, 255, 0.05);
            overflow: hidden;
            position: relative;
        }}
        .hero-shell::before {{
            content: "";
            position: absolute;
            inset: 0;
            background-image:
                linear-gradient(rgba(69, 240, 255, 0.07) 1px, transparent 1px),
                linear-gradient(90deg, rgba(69, 240, 255, 0.07) 1px, transparent 1px);
            background-size: 34px 34px;
            mask-image: linear-gradient(90deg, rgba(0,0,0,0.8), transparent 76%);
            pointer-events: none;
        }}
        .hero-copy {{ position: relative; z-index: 1; }}
        .hero-core {{
            width: 190px;
            aspect-ratio: 1;
            justify-self: center;
            position: relative;
            display: grid;
            place-items: center;
        }}
        .system-kicker {{
            display: flex;
            align-items: center;
            gap: 9px;
            color: var(--accent);
            font: 11px/1.2 Consolas, "Cascadia Code", monospace;
            margin: 0 0 18px;
            padding-top: 0;
        }}
        .system-kicker span {{
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: var(--signal);
            box-shadow: 0 0 12px rgba(152, 223, 114, 0.7);
        }}
        .empty-state h1 {{ color: #f4fbff; font-size: clamp(36px, 5vw, 54px); font-weight: 620; margin: 0 0 12px; letter-spacing: 0; line-height: 1.02; }}
        .empty-state p {{ display: block; max-width: 650px; margin: 0; color: #a5bfce; font-size: 16px; line-height: 1.58; }}
        .core-ring {{
            position: absolute;
            inset: 18px;
            border: 1px solid rgba(69, 240, 255, 0.68);
            border-radius: 50%;
            box-shadow: 0 0 24px rgba(69, 240, 255, 0.18);
            animation: core-spin 18s linear infinite;
        }}
        .ring-two {{ inset: 38px; border-color: rgba(47, 125, 255, 0.84); animation-duration: 11s; animation-direction: reverse; }}
        .ring-three {{ inset: 58px; border-style: dashed; border-color: rgba(156, 255, 114, 0.6); animation-duration: 26s; }}
        .core-hex {{
            width: 64px;
            height: 64px;
            display: grid;
            place-items: center;
            color: #eafbff;
            font: 700 24px/1 Consolas, "Cascadia Code", monospace;
            background: linear-gradient(145deg, rgba(69, 240, 255, 0.18), rgba(47, 125, 255, 0.24));
            border: 1px solid rgba(69, 240, 255, 0.74);
            clip-path: polygon(25% 5%,75% 5%,100% 50%,75% 95%,25% 95%,0 50%);
            box-shadow: 0 0 32px rgba(69, 240, 255, 0.24);
        }}
        @keyframes core-spin {{ to {{ transform: rotate(360deg); }} }}
        .launch-grid {{
            margin-top: 14px;
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 10px;
        }}
        .launch-grid button {{
            min-height: 86px;
            padding: 14px;
            border: 1px solid rgba(37, 97, 143, 0.78);
            border-radius: 10px;
            background: linear-gradient(160deg, rgba(8, 22, 36, 0.96), rgba(4, 10, 18, 0.94));
            color: var(--text);
            text-align: left;
            cursor: pointer;
            box-shadow: inset 0 0 24px rgba(47, 125, 255, 0.06);
        }}
        .launch-grid button:hover {{ border-color: rgba(69, 240, 255, 0.86); transform: translateY(-1px); }}
        .launch-grid span {{ display: block; color: var(--accent); font: 10px/1.2 Consolas, "Cascadia Code", monospace; text-transform: uppercase; }}
        .launch-grid strong {{ display: block; margin-top: 8px; font-size: 18px; font-weight: 650; }}
        .activity-ledger {{
            display: grid;
            grid-template-columns: 1.2fr 1fr 1fr 1.4fr;
            gap: 10px;
            margin-top: 12px;
        }}
        .ledger-card {{
            min-height: 86px;
            padding: 13px 14px;
            border: 1px solid rgba(36, 74, 104, 0.88);
            border-radius: 10px;
            background:
                radial-gradient(circle at 86% 18%, rgba(152, 255, 114, 0.12), transparent 28%),
                linear-gradient(160deg, rgba(8, 22, 36, 0.96), rgba(4, 10, 18, 0.94));
            box-shadow: inset 0 0 24px rgba(69, 240, 255, 0.05);
        }}
        .ledger-card.primary {{
            border-color: rgba(152, 255, 114, 0.38);
            background:
                radial-gradient(circle at 84% 14%, rgba(152, 255, 114, 0.18), transparent 30%),
                linear-gradient(150deg, rgba(9, 32, 39, 0.98), rgba(4, 10, 18, 0.94));
        }}
        button.ledger-card {{
            width: 100%;
            color: inherit;
            font: inherit;
            text-align: left;
            cursor: pointer;
        }}
        .mj-open-card:hover {{
            border-color: rgba(69, 240, 255, 0.88);
            box-shadow: inset 0 0 24px rgba(69, 240, 255, 0.08), 0 0 20px rgba(69, 240, 255, 0.1);
        }}
        .ledger-card span {{ display: block; color: var(--accent); font: 10px/1.2 Consolas, "Cascadia Code", monospace; text-transform: uppercase; }}
        .ledger-card strong {{ display: block; margin-top: 8px; color: #f4fbff; font-size: 22px; line-height: 1.05; }}
        .ledger-card small {{ display: block; margin-top: 7px; color: #88a6b4; font-size: 11px; line-height: 1.3; }}
        .mini-bars {{
            height: 34px;
            display: flex;
            align-items: flex-end;
            gap: 4px;
            margin-top: 8px;
        }}
        .mini-bars i {{
            flex: 1;
            min-width: 3px;
            height: 18%;
            border-radius: 999px 999px 2px 2px;
            background: linear-gradient(180deg, var(--signal), var(--accent));
            box-shadow: 0 0 10px rgba(69, 240, 255, 0.18);
            opacity: 0.72;
        }}
        .message.jarvis .bubble {{ color: #e5f0f1; }}
        .message.user .bubble {{ background: #0d2438; color: #f1f8ff; border: 1px solid #245a82; border-radius: 10px; }}
        .chat-form {{
            background: rgba(5, 15, 26, 0.94);
            border-color: #245174;
            border-radius: 12px;
            min-height: 68px;
            box-shadow: 0 18px 48px rgba(0, 0, 0, 0.32), inset 0 0 22px rgba(69, 240, 255, 0.04);
        }}
        .chat-form:focus-within {{ background: #071827; border-color: #39d7f2; box-shadow: 0 0 0 1px rgba(69, 240, 255, 0.14), 0 0 34px rgba(47, 125, 255, 0.12); }}
        .chat-form::before {{ display: none; }}
        .attach-button {{ border-color: #244a68; background: #06131f; color: var(--accent); }}
        .attach-button:hover {{ background: #0b2033; border-color: #43d9ff; color: #effbff; }}
        .send-button {{ width: 42px; height: 42px; border-radius: 6px; background: var(--accent); color: #031011; font-size: 0; }}
        .send-button::before {{ content: "\\2191"; color: #031011; font-size: 23px; line-height: 1; }}
        .composer-suggestions {{ max-width: 860px; }}
        .suggestion {{
            height: 42px;
            min-height: 42px;
            padding: 0 16px;
            border-radius: 6px;
            background: #06131f;
            border-color: #244a68;
            color: #d6eaf4;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }}
        .suggestion svg {{ width: 16px; height: 16px; color: var(--accent); }}
        .suggestion:hover {{ background: #0b2033; border-color: #43d9ff; }}
        .hint {{ color: #718084; }}
        .assignment-memory-status {{ color: #8fb0bd; }}
        .ad-slot {{ border-color: #1c3444; background: #04101b; }}
        .ad-slot > span {{ color: #668391; }}
        .workspace-panel {{
            width: 332px;
            flex: 0 0 332px;
            height: 100vh;
            display: flex;
            flex-direction: column;
            background: #070b0c;
            border-left: 1px solid var(--line);
        }}
        .workspace-header {{
            height: 74px;
            flex: 0 0 74px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 0 18px;
            border-bottom: 1px solid var(--line);
        }}
        .workspace-header h2 {{ margin: 5px 0 0; font-size: 16px; font-weight: 600; color: #f2f7f7; }}
        .eyebrow {{ color: var(--accent); font: 10px/1.2 Consolas, "Cascadia Code", monospace; text-transform: uppercase; }}
        .icon-button {{ width: 34px; height: 34px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid #263639; border-radius: 6px; background: #0b1113; color: #b7c8ca; cursor: pointer; padding: 0; }}
        .icon-button:hover {{ color: var(--accent); border-color: #2a686c; }}
        .workspace-open {{ display: none !important; }}
        .workspace-scroll {{ overflow-y: auto; }}
        .core-section {{ position: relative; padding: 14px 14px 10px; border-bottom: 1px solid var(--line); }}
        #jarvis-core {{
            display: block;
            width: 100%;
            aspect-ratio: 16 / 9;
            background:
                radial-gradient(circle at 50% 50%, rgba(47, 125, 255, 0.18), transparent 45%),
                #020814;
            border: 1px solid #1c4f78;
        }}
        .core-readout {{ min-height: 30px; display: flex; align-items: center; gap: 8px; color: #8ea0a3; font: 10px/1 Consolas, "Cascadia Code", monospace; padding: 9px 3px 0; }}
        .core-readout strong {{ margin-left: auto; color: #d6e5e6; font-weight: 500; }}
        .status-light {{ width: 6px; height: 6px; border-radius: 50%; background: var(--signal); box-shadow: 0 0 10px rgba(152, 223, 114, 0.65); }}
        .status-light.busy {{ background: var(--warning); box-shadow: 0 0 10px rgba(232, 184, 95, 0.65); }}
        .status-light.offline {{ background: #ff6b6b; box-shadow: 0 0 10px rgba(255, 107, 107, 0.55); }}
        .workspace-section {{ padding: 17px 18px; border-bottom: 1px solid var(--line); }}
        .engineering-card {{
            background:
                linear-gradient(rgba(69, 240, 255, 0.035) 1px, transparent 1px),
                linear-gradient(90deg, rgba(69, 240, 255, 0.035) 1px, transparent 1px),
                linear-gradient(180deg, rgba(5, 21, 31, 0.96), rgba(3, 10, 16, 0.98));
            background-size: 18px 18px, 18px 18px, auto;
        }}
        .engineering-card .section-heading span:last-child {{
            color: var(--signal);
            font: 10px/1.2 Consolas, "Cascadia Code", monospace;
        }}
        #engineering-preview {{
            display: block;
            width: 100%;
            aspect-ratio: 14 / 6.5;
            margin: 12px 0 10px;
            border: 1px solid rgba(37, 97, 143, 0.78);
            border-radius: 7px;
            background: rgba(1, 8, 14, 0.82);
        }}
        .engineering-actions {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
        .engineering-actions button {{ min-width: 0; justify-content: center; text-align: center; }}
        #engineering-export {{ grid-column: 1 / -1; color: var(--accent); border-color: #276780; }}
        #engineering-export:disabled {{ color: #52656a; border-color: #1d3033; cursor: not-allowed; opacity: 0.7; }}
        .earnings-card {{
            background:
                radial-gradient(circle at 88% 12%, rgba(152, 255, 114, 0.12), transparent 32%),
                linear-gradient(180deg, rgba(8, 23, 31, 0.88), rgba(5, 10, 15, 0.96));
        }}
        .earnings-card .section-heading span[aria-hidden="true"],
        .earnings-card .section-heading span[data-activity-metric="live"] {{
            color: var(--signal);
            font: 10px/1.2 Consolas, "Cascadia Code", monospace;
        }}
        .mj-open-button {{
            width: 100%;
            min-height: 58px;
            margin: 10px 0 8px;
            display: grid;
            gap: 5px;
            align-content: center;
            padding: 10px 12px;
            border: 1px solid rgba(69, 240, 255, 0.46);
            border-radius: 7px;
            background: linear-gradient(135deg, rgba(47, 125, 255, 0.2), rgba(7, 18, 29, 0.96));
            color: #f4fbff;
            font: inherit;
            text-align: left;
            cursor: pointer;
        }}
        .mj-open-button:hover {{ border-color: rgba(69, 240, 255, 0.9); box-shadow: 0 0 18px rgba(69, 240, 255, 0.12); }}
        .mj-open-button span {{ color: var(--accent); font: 10px/1.2 Consolas, "Cascadia Code", monospace; text-transform: uppercase; }}
        .mj-open-button strong {{ font-size: 18px; line-height: 1.1; }}
        .mini-bars.wide {{ height: 42px; margin: 13px 0 8px; }}
        .activity-stats {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 8px;
            margin-top: 10px;
        }}
        .activity-stats div {{
            padding: 8px;
            border: 1px solid rgba(36, 74, 104, 0.62);
            border-radius: 7px;
            background: rgba(2, 8, 14, 0.55);
        }}
        .activity-stats span {{ display: block; color: #708084; font-size: 10px; }}
        .activity-stats strong {{ display: block; margin-top: 4px; color: #d9e5e5; font-size: 11px; overflow-wrap: anywhere; }}
        .workspace-section h3 {{ margin: 8px 0 6px; color: #e8f2f2; font-size: 15px; font-weight: 600; line-height: 1.35; }}
        .workspace-section p {{ margin: 0; color: var(--muted); font-size: 12px; line-height: 1.5; }}
        .telemetry-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px 12px; }}
        .telemetry-grid span {{ display: block; color: #708084; font-size: 10px; line-height: 1.35; }}
        .telemetry-grid strong {{ display: block; margin-top: 4px; color: #d9e5e5; font-size: 11px; font-weight: 600; overflow-wrap: anywhere; }}
        .section-heading {{ display: flex; justify-content: space-between; align-items: center; }}
        .section-heading span[aria-hidden="true"] {{ color: #71898c; font: 13px/1 Consolas, "Cascadia Code", monospace; }}
        .workspace-empty {{ color: #718084; }}
        .workspace-actions {{ display: grid; gap: 7px; margin-top: 10px; }}
        .workspace-actions button {{ min-height: 38px; display: flex; align-items: center; gap: 10px; padding: 0 10px; border: 1px solid #1d3033; border-radius: 6px; background: #0a1012; color: #d4e0e1; font-size: 12px; text-align: left; cursor: pointer; }}
        .workspace-actions button:hover {{ border-color: #2e686c; color: #fff; }}
        .cloud-status {{
            min-width: 66px;
            height: 32px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 0 10px;
            border: 1px solid #286b74;
            border-radius: 6px;
            color: var(--accent);
            background: #061522;
            font: 10px/1 Consolas, "Cascadia Code", monospace;
            text-transform: uppercase;
            white-space: nowrap;
        }}
        .cloud-status.offline {{ border-color: #704044; color: #ff9f9f; background: #190d12; }}
        .empty-state {{
            min-height: 100%;
            display: flex;
            align-items: center;
            justify-content: flex-start;
            width: min(760px, 100%);
            padding: 48px 4px 34px;
            text-align: left;
        }}
        .chat-welcome {{ max-width: 650px; }}
        .conversation-kicker {{
            display: flex;
            align-items: center;
            gap: 9px;
            margin-bottom: 18px;
            color: var(--accent);
            font: 11px/1.2 Consolas, "Cascadia Code", monospace;
        }}
        .conversation-kicker span {{ width: 7px; height: 7px; border-radius: 50%; background: var(--signal); }}
        .empty-state h1 {{ margin: 0 0 12px; color: #f4fbff; font-size: 42px; font-weight: 620; line-height: 1.08; }}
        .empty-state p {{ margin: 0; color: #9fb6c4; font-size: 16px; line-height: 1.55; }}
        .mode-switch {{
            width: min(860px, 100%);
            min-height: 42px;
            display: flex;
            align-items: center;
            gap: 3px;
            margin: 0 auto 8px;
            padding: 4px;
            overflow-x: auto;
            border: 1px solid #18374d;
            border-radius: 7px;
            background: #040c13;
            scrollbar-width: none;
        }}
        .mode-switch::-webkit-scrollbar {{ display: none; }}
        .mode-option {{
            height: 32px;
            flex: 1 0 auto;
            min-width: 86px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 7px;
            padding: 0 11px;
            border: 0;
            border-radius: 5px;
            background: transparent;
            color: #879da9;
            font: 12px/1 "Segoe UI", Arial, sans-serif;
            cursor: pointer;
            white-space: nowrap;
        }}
        .mode-option svg {{ width: 15px; height: 15px; }}
        .mode-option:hover {{ color: #dff8ff; background: #0a1c29; }}
        .mode-option.active {{ color: #f3feff; background: #12344a; box-shadow: inset 0 0 0 1px #2a708d; }}
        .composer-suggestions {{ margin-top: 9px; gap: 7px; justify-content: flex-start; }}
        .composer-suggestions .suggestion {{ min-height: 36px; height: 36px; padding: 0 13px; border-radius: 6px; color: #a8bdc8; font-size: 12px; }}
        body.workspace-collapsed .workspace-panel {{ width: 0; flex-basis: 0; opacity: 0; overflow: hidden; border: 0; }}
        body.workspace-collapsed .workspace-open {{ display: inline-flex !important; }}
        @media (max-width: 1240px) {{
            .workspace-panel {{ display: none; }}
            .workspace-open {{ display: none !important; }}
            .chat-inner, .chat-form, .composer-suggestions, .hint {{ max-width: 820px; }}
        }}
        @media (max-width: 980px) {{
            .empty-state {{
                max-width: 720px;
                text-align: left;
            }}
            .hero-shell {{ grid-template-columns: 1fr; }}
            .hero-core {{ display: none; }}
            .empty-state p {{ max-width: 560px; }}
            .launch-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
            .activity-ledger {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
        }}
        @media (max-width:900px) {{
            html, body {{ width: 100%; overflow-x: hidden; }}
            .app {{ min-height: 100dvh; height: 100dvh; overflow: hidden; }}
            .main {{ min-width: 0; }}
            .mobile-menu {{
                width: 32px;
                height: 32px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                border: 1px solid #213438;
                border-radius: 8px;
                background: #0b1416;
                color: var(--accent);
                font-size: 17px;
                cursor: pointer;
            }}
            body.mobile-nav-open .sidebar {{
                width: min(86vw, 320px);
                height: 100dvh;
                display: block;
                position: fixed;
                inset: 0 auto 0 0;
                z-index: 70;
                box-shadow: 18px 0 46px rgba(0, 0, 0, .55);
            }}
            body.mobile-nav-open .mobile-nav-backdrop {{
                display: block;
                position: fixed;
                inset: 0;
                z-index: 65;
                border: 0;
                background: rgba(0, 0, 0, .62);
            }}
            .topbar {{
                height: 52px;
                padding: 0 max(12px, env(safe-area-inset-right)) 0 max(12px, env(safe-area-inset-left));
                position: sticky;
                top: 0;
                z-index: 30;
            }}
            .title {{ font-size: 0; }}
            .title::after {{
                content: "Jarivs";
                color: #f4ffff;
                font-size: 14px;
                font-weight: 700;
                text-transform: none;
                letter-spacing: 0;
            }}
            .topbar-actions {{ gap: 8px; }}
            .mobile-new-chat {{
                width: 32px;
                height: 32px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                border: 1px solid #213438;
                border-radius: 8px;
                background: #0b1416;
                color: var(--accent);
                text-decoration: none;
                font-size: 20px;
                line-height: 1;
            }}
            .mode {{ font-size: 0; padding: 7px 9px; border-radius: 8px; }}
            .mode::after {{ content: "Safe"; font-size: 10px; }}
            .chat {{
                padding: 10px 14px 8px;
                overflow-x: hidden;
                overscroll-behavior: contain;
            }}
            .chat-inner {{ width: 100%; max-width: none; }}
            .empty-state {{
                min-height: clamp(190px, 38dvh, 270px);
                width: 100%;
                padding: 18px 2px 14px;
            }}
            .hero-shell {{
                min-height: 0;
                padding: 18px;
                border-radius: 12px;
            }}
            .system-kicker {{ font-size: 10px; margin-bottom: 14px; }}
            .empty-state h1 {{ font-size: clamp(28px, 8vw, 34px); line-height: 1.1; }}
            .empty-state p {{ max-width: 320px; font-size: 13px; line-height: 1.45; }}
            .launch-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }}
            .launch-grid button {{ min-height: 70px; padding: 10px; border-radius: 9px; }}
            .launch-grid strong {{ font-size: 15px; margin-top: 6px; }}
            .activity-ledger {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }}
            .ledger-card {{ min-height: 70px; padding: 10px; border-radius: 9px; }}
            .ledger-card strong {{ font-size: 17px; margin-top: 6px; }}
            .ledger-card small {{ font-size: 10px; margin-top: 5px; }}
            .mini-bars {{ height: 26px; }}
            .message {{ margin: 14px 0; }}
            .bubble {{ max-width: 100%; padding: 10px 0; font-size: 15px; line-height: 1.5; }}
            .message.user .bubble {{ max-width: 88%; padding: 10px 12px; border-radius: 10px; }}
            .composer {{
                position: sticky;
                bottom: 0;
                z-index: 25;
                padding: 8px 12px calc(14px + env(safe-area-inset-bottom));
                border-top: 1px solid #10191b;
            }}
            .chat-form {{
                width: 100%;
                min-height: 56px;
                border-radius: 12px;
                padding: 8px 8px 8px 12px;
                gap: 8px;
            }}
            .chat-form::before {{ display: none; }}
            textarea {{
                min-height: 38px;
                padding: 8px 2px;
                font-size: 16px;
                line-height: 1.35;
            }}
            .send-button {{ width: 40px; height: 40px; border-radius: 10px; }}
            .composer-suggestions {{
                max-width: none;
                justify-content: stretch;
                display:grid;
                grid-template-columns:repeat(2,minmax(0,1fr));
                gap:8px;
                margin-top: 10px;
                overflow:visible;
            }}
            .composer-suggestions .suggestion {{ min-width:0; width:100%; height:38px; min-height:38px; padding:0 8px; font-size:13px; }}
            .hint {{ display: none; }}
            .image-gallery {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
            .empty-state {{ min-height: 100%; padding: 28px 2px; align-items: center; }}
            .conversation-kicker {{ margin-bottom: 12px; font-size: 10px; }}
            .mode-switch {{ width: 100%; margin-bottom: 7px; }}
            .mode-option {{ flex: 0 0 auto; min-width: 82px; }}
            .composer-suggestions {{
                display: flex;
                justify-content: flex-start;
                gap: 6px;
                margin-top: 7px;
                overflow-x: auto;
                scrollbar-width: none;
            }}
            .composer-suggestions::-webkit-scrollbar {{ display: none; }}
            .composer-suggestions .suggestion {{ flex: 0 0 auto; width: auto; min-width: 0; height: 34px; min-height: 34px; padding: 0 10px; font-size: 12px; }}
        }}
        @media (max-width:380px) {{
            .topbar-actions {{ gap: 6px; }}
            .mobile-new-chat {{ width: 30px; height: 30px; }}
            .mode {{ padding: 7px 8px; }}
            .empty-state {{ padding-top: 22px; }}
            .launch-grid {{ grid-template-columns: 1fr; }}
            .activity-ledger {{ grid-template-columns: 1fr; }}
            .composer-suggestions .suggestion {{ font-size: 12px; }}
        }}
        /* Separate talkable companion */
        .mascot-crop {{ position: relative; overflow: hidden; isolation: isolate; }}
        .mascot-crop > img {{
            position: absolute;
            left: 50%;
            top: 100%;
            width: 220%;
            max-width: none;
            height: auto;
            transform: translate(-50%, -50%);
            user-select: none;
            pointer-events: none;
        }}
        .pet-toggle {{ position: relative; width: 38px; height: 38px; flex: 0 0 38px; padding: 0; overflow: hidden; border: 1px solid #27658a; border-radius: 8px; background: #0b1724; cursor: pointer; box-shadow: 0 0 18px rgba(69, 240, 255, 0.12); }}
        .pet-toggle:hover {{ border-color: var(--accent); }}
        .pet-toggle .pet-face {{ position: absolute; inset: 0; }}
        .pet-online {{ position: absolute; right: 4px; bottom: 4px; width: 7px; height: 7px; border-radius: 50%; background: var(--signal); box-shadow: 0 0 8px rgba(156, 255, 114, 0.8); z-index: 2; }}
        .pet-online.offline {{ background: #ff6b6b; box-shadow: 0 0 8px rgba(255, 107, 107, 0.65); }}
        .pet-panel {{ position: fixed; z-index: 1200; top: 68px; right: 350px; width: 340px; height: min(500px, calc(100vh - 92px)); display: grid; grid-template-rows: 58px minmax(0, 1fr) 62px; border: 1px solid #28516d; border-radius: 8px; overflow: hidden; background: #07111d; box-shadow: 0 24px 70px rgba(0, 0, 0, 0.52); }}
        .pet-panel[hidden] {{ display: none; }}
        .pet-header {{ display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-bottom: 1px solid var(--line); }}
        .pet-header-face {{ width: 38px; height: 38px; flex: 0 0 38px; border-radius: 8px; background: #101b2a; }}
        .pet-header-copy {{ min-width: 0; display: grid; gap: 2px; }}
        .pet-header-copy strong {{ color: #effaff; font-size: 14px; }}
        .pet-header-copy span {{ color: var(--signal); font: 10px/1 Consolas, monospace; text-transform: uppercase; }}
        .pet-close {{ margin-left: auto; width: 32px; height: 32px; padding: 0; border: 0; background: transparent; color: #9db0bb; cursor: pointer; font-size: 22px; }}
        .pet-messages {{ min-height: 0; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 10px; }}
        .pet-message {{ max-width: 86%; padding: 9px 11px; border-radius: 8px; line-height: 1.45; font-size: 13px; white-space: pre-wrap; overflow-wrap: anywhere; }}
        .pet-message.pet {{ align-self: flex-start; background: #0e2031; color: #e5f5ff; border: 1px solid #1f4058; }}
        .pet-message.user {{ align-self: flex-end; background: #153246; color: #f4fbff; }}
        .pet-form {{ display: flex; align-items: center; gap: 8px; padding: 10px; border-top: 1px solid var(--line); }}
        .pet-form input {{ min-width: 0; flex: 1; height: 40px; padding: 0 11px; border: 1px solid #27445a; border-radius: 6px; background: #050c13; color: #eef8ff; outline: none; }}
        .pet-form input:focus {{ border-color: var(--accent); }}
        .pet-form button {{ width: 40px; height: 40px; padding: 0; border: 0; border-radius: 6px; background: var(--accent); color: #031011; cursor: pointer; font-size: 18px; }}
        .pet-form button:disabled {{ opacity: 0.5; cursor: wait; }}
        .jarvis-one-toast {{
            position: fixed;
            left: 50%;
            bottom: 92px;
            z-index: 1400;
            width: min(560px, calc(100vw - 28px));
            padding: 13px 15px;
            border: 1px solid #2a789a;
            border-radius: 8px;
            background: #071827;
            color: #f4fbff;
            box-shadow: 0 18px 54px rgba(0, 0, 0, 0.48), 0 0 24px rgba(69, 240, 255, 0.12);
            font-size: 14px;
            line-height: 1.45;
            text-align: center;
            transform: translate(-50%, 18px);
            opacity: 0;
            pointer-events: none;
            transition: opacity 160ms ease, transform 160ms ease;
        }}
        .jarvis-one-toast.show {{ opacity: 1; transform: translate(-50%, 0); }}
        .chat-toast {{
            position: fixed;
            left: 50%;
            bottom: 92px;
            z-index: 1400;
            display: flex;
            align-items: center;
            gap: 14px;
            width: max-content;
            max-width: min(420px, calc(100vw - 28px));
            padding: 12px 14px;
            border: 1px solid #2a789a;
            border-radius: 8px;
            background: #071827;
            color: #f4fbff;
            box-shadow: 0 18px 54px rgba(0, 0, 0, 0.48), 0 0 24px rgba(69, 240, 255, 0.12);
            font-size: 13px;
            transform: translate(-50%, 18px);
            opacity: 0;
            pointer-events: none;
            transition: opacity 160ms ease, transform 160ms ease;
        }}
        .chat-toast.show {{ opacity: 1; transform: translate(-50%, 0); pointer-events: auto; }}
        .chat-toast-action {{
            flex: 0 0 auto;
            border: 1px solid #2a789a;
            border-radius: 6px;
            background: transparent;
            color: #45f0ff;
            font-weight: 700;
            font-size: 12px;
            padding: 5px 10px;
            cursor: pointer;
        }}
        .chat-toast-action:hover {{ background: #0e2d3f; }}
        @media (max-width:1240px) {{ .pet-panel {{ right: 16px; }} }}
        @media (max-width:760px) {{ .pet-toggle {{ width: 34px; height: 34px; flex-basis: 34px; }} .pet-panel {{ top: 58px; right: 10px; left: 10px; width: auto; height: min(520px, calc(100vh - 76px)); }} .jarvis-one-toast {{ bottom: 76px; font-size: 13px; }} .chat-toast {{ bottom: 76px; font-size: 12px; }} }}
    </style>
</head>
<body class="guest-version">
    <div class="app">
        <aside class="sidebar">
            <div class="sidebar-topline">
                <a class="brand-row" href="/">
                    <span class="brand-mark">J</span>
                    <span class="brand-name">{APP_TITLE}</span>
                </a>
                {auth_status}
            </div>
            <nav class="sidebar-nav" aria-label="Jarvis navigation">
                <form action="/new" method="post"><button class="nav-item nav-primary" type="submit"><span class="nav-icon">+</span><span>New chat</span></button></form>
                {auth_nav}
                <button class="nav-item" id="chat-search-toggle" type="button"><span class="nav-icon">&#8981;</span><span>Search chats</span></button>
                <a class="nav-item" href="/essay/{chat_id}"><span class="nav-icon">E</span><span>Essay workspace</span></a>
                <a class="nav-item" href="/study/{chat_id}"><span class="nav-icon">S</span><span>Study workspace</span></a>
                <a class="nav-item" href="/roadmap"><span class="nav-icon">R</span><span>Roadmap</span></a>
                <a class="nav-item" href="/feedback"><span class="nav-icon">?</span><span>Feedback</span></a>
                <a class="nav-item" href="/privacy"><span class="nav-icon">i</span><span>Privacy</span></a>
            </nav>
            <label class="chat-search" id="chat-search-wrap" hidden>
                <span class="sr-only">Search recent chats</span>
                <input id="chat-search" type="search" placeholder="Search recent chats" autocomplete="off">
            </label>
            <div class="recents-header"><span>Recents</span><button class="text-button" id="toggle-archived-chats" type="button" hidden>Show archived</button></div>
            <div class="chat-list" id="chat-list">
            {sidebar}
            </div>
            {sidebar_ad}
        </aside>
        <button class="mobile-nav-backdrop" id="mobile-nav-backdrop" type="button" aria-label="Close chat navigation"></button>
        <main class="main">
            <header class="topbar">
                <div class="title">JARVIS / CONVERSATION CORE</div>
                <div class="topbar-actions">
                    <button class="mobile-menu" id="mobile-menu" type="button" title="Open chat navigation" aria-label="Open chat navigation">&#9776;</button>
                    <button class="pet-toggle" id="pet-toggle" type="button" title="Talk to Mini Jarvis" aria-label="Talk to Mini Jarvis"><span class="pet-face mascot-crop"><img src="/assets/jarvis-mascot.png" alt=""></span><span class="pet-online{' offline' if not brain_ready else ''}"></span></button>
                    <form action="/new" method="post"><button class="mobile-new-chat" type="submit" title="New chat" aria-label="New chat">+</button></form>
                    <div class="cloud-status{' offline' if not brain_ready else ''}">{'AI online' if brain_ready else 'AI setup'}</div>
                </div>
            </header>
            <section class="pet-panel" id="pet-panel" aria-label="Companion chat" hidden>
                <header class="pet-header">
                    <span class="pet-header-face mascot-crop"><img src="/assets/jarvis-mascot.png" alt=""></span>
                    <span class="pet-header-copy"><strong>Mini Jarvis</strong><span>{html.escape(brain_label)}</span></span>
                    <button class="pet-close" id="pet-close" type="button" title="Close companion" aria-label="Close companion">&times;</button>
                </header>
                <div class="pet-messages" id="pet-messages" aria-live="polite"></div>
                <form class="pet-form" id="pet-form">
                    <input id="pet-input" type="text" maxlength="800" placeholder="Talk to your companion..." autocomplete="off">
                    <button id="pet-send" type="submit" title="Send" aria-label="Send">&#8593;</button>
                </form>
            </section>
            <section class="chat" id="chat">
                <div class="chat-inner" id="messages">
                    {empty_state}
                    {history_html}
                </div>
            </section>
            <section class="composer">
                <div class="mode-switch" id="mode-switch" role="group" aria-label="Jarvis response mode">
                    {mode_switch}
                </div>
                <form class="chat-form" id="chat-form">
                    <button class="attach-button" id="assignment-attach" type="button" title="Remember rubric or notes" aria-label="Remember rubric or notes">+</button>
                    <input id="assignment-file" type="file" multiple accept=".txt,.md,.markdown,.csv,.json,.pdf,.docx,.pptx,.py,.js,.html,.css" hidden>
                    <textarea id="message-input" name="message" maxlength="{MAX_MESSAGE_CHARS}" placeholder="Message Jarvis..." autocomplete="off" autofocus></textarea>
                    <button class="send-button" id="send-button" type="submit">Send</button>
                </form>
                <div class="assignment-memory-status" id="assignment-memory-status" aria-live="polite"></div>
                <div class="assignment-file-list" id="assignment-file-list"></div>
                {suggestions}
                {composer_ad}
                <div class="hint">Enter sends. Shift+Enter adds a new line.</div>
            </section>
        </main>
        {workspace_panel}
    </div>
    <div class="jarvis-one-toast" id="jarvis-one-toast" role="status" aria-live="polite"></div>
    <script nonce="{html.escape(csp_nonce)}">
        const chatId = {json.dumps(chat_id)};
        const jarvisOneTributes = [
            "In honnor of Jarvis 1.0 ts ain't working and never will cuz i'm to lazy",
            "Jarvis 1.0 memorial button: still broken, still iconic.",
            "This button failed in 1.0 and we kept it for historical accuracy.",
            "New chat? Absolutely not. Tradition must be respected."
        ];
        function randomJarvisOneTribute() {{
            return jarvisOneTributes[Math.floor(Math.random() * jarvisOneTributes.length)];
        }}
        const chat = document.getElementById("chat");
        const messages = document.getElementById("messages");
        const form = document.getElementById("chat-form");
        const input = document.getElementById("message-input");
        const button = document.getElementById("send-button");
        const emptyState = document.getElementById("empty-state");
        const suggestions = document.getElementById("composer-suggestions");
        const initialMode = {json.dumps(active_mode)};
        const modeStorageKey = `jarvis_mode_${{chatId}}`;
        const modeButtons = Array.from(document.querySelectorAll("[data-chat-mode]"));
        const starterButtons = Array.from(document.querySelectorAll("[data-mode-starter]"));
        const modeConfig = {{
            chat: {{ placeholder: "Message Jarvis...", starters: ["Talk something through with me", "Help me make a decision", "Explain an idea clearly"] }},
            study: {{ placeholder: "What do you want to learn?", starters: ["Teach me this step by step", "Quiz me on a topic", "Help me understand my homework"] }},
            essay: {{ placeholder: "Paste the task, rubric, or draft...", starters: ["Help me plan this essay", "Improve my thesis", "Check this against the rubric"] }},
            math: {{ placeholder: "Enter the maths problem...", starters: ["Show the working for", "Explain this formula", "Check my answer to"] }},
            science: {{ placeholder: "Ask a science question...", starters: ["Explain this concept", "Help me plan this experiment", "Summarize the evidence for"] }},
            code: {{ placeholder: "Describe the code, feature, or bug...", starters: ["Debug this code with me", "Plan a small app", "Explain this programming concept"] }},
            research: {{ placeholder: "What should Jarvis research?", starters: ["Research the latest information about", "Compare reliable sources for", "Give me an evidence-based summary of"] }},
            create: {{ placeholder: "What should we create?", starters: ["Write a strong first draft", "Brainstorm distinctive ideas for", "Improve the style of this"] }},
            engineer: {{ placeholder: "Describe the part, constraints, and measurements...", starters: ["Engineer a physical part for", "Check the risks and loads for", "Create a prototype test plan for"] }}
        }};
        let activeMode = localStorage.getItem(modeStorageKey) || initialMode;
        if (!modeConfig[activeMode]) activeMode = "chat";
        const workspaceClose = document.getElementById("workspace-close");
        const workspaceOpen = document.getElementById("workspace-open");
        const mobileMenu = document.getElementById("mobile-menu");
        const mobileNavBackdrop = document.getElementById("mobile-nav-backdrop");
        const chatSearchToggle = document.getElementById("chat-search-toggle");
        const chatSearchWrap = document.getElementById("chat-search-wrap");
        const chatSearch = document.getElementById("chat-search");
        const exportDeviceData = document.getElementById("export-device-data");
        const deleteDeviceData = document.getElementById("delete-device-data");
        const assignmentAttach = document.getElementById("assignment-attach");
        const assignmentFile = document.getElementById("assignment-file");
        const assignmentMemoryStatus = document.getElementById("assignment-memory-status");
        const petToggle = document.getElementById("pet-toggle");
        const petPanel = document.getElementById("pet-panel");
        const petClose = document.getElementById("pet-close");
        const petMessages = document.getElementById("pet-messages");
        const petForm = document.getElementById("pet-form");
        const jarvisOneToast = document.getElementById("jarvis-one-toast");
        let jarvisOneToastTimer = null;
        function showJarvisOneTribute() {{
            const tributeMessage = randomJarvisOneTribute();
            if (!jarvisOneToast) {{
                window.alert(tributeMessage);
                return;
            }}
            jarvisOneToast.textContent = tributeMessage;
            jarvisOneToast.classList.add("show");
            window.clearTimeout(jarvisOneToastTimer);
            jarvisOneToastTimer = window.setTimeout(() => jarvisOneToast.classList.remove("show"), 4200);
        }}
        function setChatMode(mode, focusInput = true) {{
            if (!modeConfig[mode]) return;
            activeMode = mode;
            try {{ localStorage.setItem(modeStorageKey, mode); }} catch (error) {{}}
            modeButtons.forEach(item => {{
                const selected = item.dataset.chatMode === mode;
                item.classList.toggle("active", selected);
                item.setAttribute("aria-pressed", selected ? "true" : "false");
            }});
            input.placeholder = modeConfig[mode].placeholder;
            starterButtons.forEach((item, index) => {{
                const prompt = modeConfig[mode].starters[index] || "";
                item.textContent = prompt;
                item.dataset.prompt = prompt;
                item.hidden = !prompt;
            }});
            if (focusInput) input.focus();
        }}
        modeButtons.forEach(item => item.addEventListener("click", () => setChatMode(item.dataset.chatMode)));
        setChatMode(activeMode, false);
        const petInput = document.getElementById("pet-input");
        const petSend = document.getElementById("pet-send");
        const coreState = document.getElementById("core-state");
        const statusLight = document.getElementById("status-light");
        const latencyReadout = document.getElementById("latency-readout");
        const activityKey = "jarvis_web_activity_v1";
        const engineeringKey = `jarvis_engineering_${{chatId}}`;
        const engineeringStatus = document.getElementById("engineering-status");
        const engineeringProject = document.getElementById("engineering-project");
        const engineeringNext = document.getElementById("engineering-next");
        const engineeringExport = document.getElementById("engineering-export");
        const brainReady = {json.dumps(brain_ready)};
        let activityState = loadActivityState();
        let engineeringState = loadEngineeringState();
        let petLoaded = false;
        let petBusy = false;
        const deviceMemoryEnabled = {json.dumps(DEVICE_MEMORY_ENABLED)};
        const chatMemoryKey = `jarvis_chat_memory_${{chatId}}_v1`;
        const assignmentMemoryKey = `jarvis_assignment_memory_${{chatId}}_v1`;
        const chatIndexKey = "jarvis_chat_index_v1";

        function scrollDown() {{ chat.scrollTop = chat.scrollHeight; }}
        function setCoreState(label, busy = false) {{
            if (coreState) coreState.textContent = label;
            if (statusLight) statusLight.classList.toggle("busy", busy);
            if (statusLight) statusLight.classList.toggle("offline", !brainReady && !busy);
        }}
        function readJsonStorage(key, fallback) {{
            try {{
                const value = JSON.parse(localStorage.getItem(key) || "null");
                return value === null ? fallback : value;
            }} catch (error) {{
                return fallback;
            }}
        }}
        function writeJsonStorage(key, value) {{
            try {{
                localStorage.setItem(key, JSON.stringify(value));
                return true;
            }} catch (error) {{
                return false;
            }}
        }}
        function cleanMemoryItem(item) {{
            const role = String(item?.role || "").toLowerCase() === "user" ? "user" : "Jarvis";
            const content = String(item?.content || "").slice(0, {MAX_MESSAGE_CHARS});
            const mode = modeConfig[item?.mode] ? item.mode : "chat";
            return content.trim() ? {{ role, content, mode, time: String(item?.time || "") }} : null;
        }}
        function getChatMemory(id = chatId) {{
            if (!deviceMemoryEnabled) return [];
            const items = readJsonStorage(`jarvis_chat_memory_${{id}}_v1`, []);
            return Array.isArray(items) ? items.map(cleanMemoryItem).filter(Boolean).slice(-80) : [];
        }}
        function saveChatMemory(items, id = chatId) {{
            if (!deviceMemoryEnabled) return;
            writeJsonStorage(`jarvis_chat_memory_${{id}}_v1`, items.map(cleanMemoryItem).filter(Boolean).slice(-80));
        }}
        function cleanAssignmentMemoryItem(item) {{
            const name = String(item?.name || "assignment-notes.txt").replace(/[<>:"/\\\\|?*]/g, "_").slice(0, 160);
            const mediaType = String(item?.media_type || "text/plain").slice(0, 120);
            const text = String(item?.text || "").slice(0, {MAX_ATTACHMENT_CHARS});
            const bytes = Number.isFinite(item?.bytes) ? Math.max(0, Math.floor(item.bytes)) : 0;
            return text.trim() ? {{ name, media_type: mediaType, text, bytes, saved_at: String(item?.saved_at || new Date().toISOString()) }} : null;
        }}
        function getAssignmentMemory(id = chatId) {{
            if (!deviceMemoryEnabled) return [];
            const items = readJsonStorage(`jarvis_assignment_memory_${{id}}_v1`, []);
            return Array.isArray(items) ? items.map(cleanAssignmentMemoryItem).filter(Boolean).slice(-{MAX_ATTACHMENTS}) : [];
        }}
        function saveAssignmentMemory(items, id = chatId) {{
            if (!deviceMemoryEnabled) return;
            writeJsonStorage(`jarvis_assignment_memory_${{id}}_v1`, items.map(cleanAssignmentMemoryItem).filter(Boolean).slice(-{MAX_ATTACHMENTS}));
            updateAssignmentMemoryStatus();
            renderAssignmentFileList();
        }}
        function rememberAssignmentDocument(documentContext) {{
            const clean = cleanAssignmentMemoryItem({{ ...documentContext, saved_at: new Date().toISOString() }});
            if (!clean) return;
            const existing = getAssignmentMemory().filter(item => item.name.toLowerCase() !== clean.name.toLowerCase());
            existing.push(clean);
            saveAssignmentMemory(existing);
            rememberChatTitle(clean.name);
        }}
        function removeAssignmentDocument(name) {{
            const remaining = getAssignmentMemory().filter(item => item.name !== name);
            saveAssignmentMemory(remaining);
        }}
        function updateAssignmentMemoryStatus() {{
            if (!assignmentMemoryStatus) return;
            if (!deviceMemoryEnabled) {{
                assignmentMemoryStatus.textContent = "";
                return;
            }}
            const items = getAssignmentMemory();
            if (!items.length) {{
                assignmentMemoryStatus.textContent = "Attach a rubric, notes, or draft once and Jarvis will remember it for this assignment. You can also drag files onto the composer.";
                return;
            }}
            assignmentMemoryStatus.textContent = `Remembering ${{items.length}} of {MAX_ATTACHMENTS} assignment file${{items.length === 1 ? "" : "s"}}.`;
        }}
        function formatFileSize(bytes) {{
            if (!bytes) return "";
            if (bytes < 1024) return `${{bytes}} B`;
            if (bytes < 1024 * 1024) return `${{(bytes / 1024).toFixed(1)}} KB`;
            return `${{(bytes / (1024 * 1024)).toFixed(1)}} MB`;
        }}
        function renderAssignmentFileList() {{
            const list = document.getElementById("assignment-file-list");
            if (!list) return;
            list.innerHTML = "";
            if (!deviceMemoryEnabled) return;
            getAssignmentMemory().forEach(item => {{
                const row = document.createElement("div");
                row.className = "assignment-file-item";
                const name = document.createElement("span");
                name.className = "assignment-file-name";
                name.textContent = item.name;
                name.title = item.name;
                row.appendChild(name);
                const size = formatFileSize(item.bytes);
                if (size) {{
                    const sizeLabel = document.createElement("span");
                    sizeLabel.className = "assignment-file-size";
                    sizeLabel.textContent = size;
                    row.appendChild(sizeLabel);
                }}
                const remove = document.createElement("button");
                remove.type = "button";
                remove.className = "assignment-file-remove";
                remove.title = `Remove ${{item.name}}`;
                remove.setAttribute("aria-label", `Remove ${{item.name}}`);
                remove.textContent = "×";
                remove.addEventListener("click", () => removeAssignmentDocument(item.name));
                row.appendChild(remove);
                list.appendChild(row);
            }});
        }}
        function createAssignmentProgressRow(file) {{
            const list = document.getElementById("assignment-file-list");
            if (!list) return null;
            const row = document.createElement("div");
            row.className = "assignment-file-item";
            const name = document.createElement("span");
            name.className = "assignment-file-name";
            name.textContent = file.name;
            name.title = file.name;
            row.appendChild(name);
            const track = document.createElement("span");
            track.className = "assignment-file-progress-track";
            const fill = document.createElement("span");
            fill.className = "assignment-file-progress-fill";
            track.appendChild(fill);
            row.appendChild(track);
            list.appendChild(row);
            return {{
                setProgress(fraction) {{
                    fill.style.width = `${{Math.min(100, Math.max(0, fraction * 100))}}%`;
                }},
                setError(message) {{
                    row.classList.add("error");
                    track.remove();
                    const errorLabel = document.createElement("span");
                    errorLabel.className = "assignment-file-size";
                    errorLabel.textContent = message;
                    row.appendChild(errorLabel);
                    row.title = "Click to dismiss";
                    row.addEventListener("click", () => row.remove());
                }},
                remove() {{ row.remove(); }}
            }};
        }}
        function uploadAssignmentFileWithProgress(file, progressRow) {{
            return new Promise((resolve, reject) => {{
                const xhr = new XMLHttpRequest();
                xhr.open("POST", `/api/chats/${{chatId}}/attachments`);
                xhr.upload.onprogress = event => {{
                    if (event.lengthComputable && progressRow) progressRow.setProgress(event.loaded / event.total);
                }};
                xhr.onload = () => {{
                    let data = {{}};
                    try {{ data = JSON.parse(xhr.responseText || "{{}}"); }} catch (error) {{}}
                    if (xhr.status >= 200 && xhr.status < 300) resolve(data);
                    else reject(new Error(data.detail || "Jarvis could not read that file."));
                }};
                xhr.onerror = () => reject(new Error("That file could not be uploaded."));
                const formData = new FormData();
                formData.append("file", file);
                xhr.send(formData);
            }});
        }}
        async function handleAssignmentFiles(fileList) {{
            const files = Array.from(fileList || []);
            if (!files.length) return;
            const remainingSlots = Math.max(0, {MAX_ATTACHMENTS} - getAssignmentMemory().length);
            const queued = files.slice(0, remainingSlots || files.length);
            if (files.length > queued.length && assignmentMemoryStatus) {{
                assignmentMemoryStatus.textContent = `Jarvis can remember up to {MAX_ATTACHMENTS} files per assignment; only the first ${{queued.length}} were queued.`;
            }}
            if (assignmentAttach) assignmentAttach.disabled = true;
            for (const file of queued) {{
                const progressRow = createAssignmentProgressRow(file);
                if (file.size > {MAX_UPLOAD_BYTES}) {{
                    progressRow?.setError("Larger than the upload limit.");
                    continue;
                }}
                try {{
                    const data = await uploadAssignmentFileWithProgress(file, progressRow);
                    rememberAssignmentDocument({{ ...data, bytes: file.size }});
                    addMessage("Jarvis", `I will remember ${{data.name}} for this assignment.`);
                    progressRow?.remove();
                }} catch (error) {{
                    progressRow?.setError(error.message || "Could not be added.");
                }}
            }}
            if (assignmentAttach) assignmentAttach.disabled = false;
            updateAssignmentMemoryStatus();
            input.focus();
        }}
        async function uploadAssignmentMemoryFile() {{
            if (!assignmentFile?.files?.length) return;
            const files = assignmentFile.files;
            assignmentFile.value = "";
            await handleAssignmentFiles(files);
        }}
        function rememberMessage(role, content, mode = activeMode) {{
            const items = getChatMemory();
            items.push({{ role, content, mode, time: new Date().toISOString() }});
            saveChatMemory(items);
            rememberChatTitle(content);
        }}
        function firstUserLine(items) {{
            const first = items.find(item => item.role === "user" && item.content);
            return first ? first.content.replace(/\\s+/g, " ").slice(0, 48) : "New Chat";
        }}
        function getChatIndex() {{
            const index = readJsonStorage(chatIndexKey, []);
            return Array.isArray(index) ? index.filter(item => item && item.id).slice(-80) : [];
        }}
        function saveChatIndex(index) {{
            if (deviceMemoryEnabled) writeJsonStorage(chatIndexKey, index.slice(-80));
        }}
        function chatIndexItem(id) {{
            return getChatIndex().find(item => item.id === id) || null;
        }}
        function updateChatIndexItem(id, patch) {{
            if (!deviceMemoryEnabled) return null;
            const index = getChatIndex();
            const existing = index.find(item => item.id === id);
            const updated = Object.assign({{ id, title: existing?.title || "New Chat", updatedAt: Date.now() }}, existing || {{}}, patch, {{ id, updatedAt: Date.now() }});
            const next = index.filter(item => item.id !== id);
            next.push(updated);
            saveChatIndex(next);
            return updated;
        }}
        function applyRowState(row, item) {{
            if (!row || !item) return;
            const link = row.querySelector(".chat-title");
            if (link && item.title) {{
                link.textContent = item.title;
                link.title = item.title;
                row.dataset.title = item.title.toLowerCase();
            }}
            row.dataset.pinned = item.pinned ? "true" : "false";
            row.dataset.archived = item.archived ? "true" : "false";
            const pinBtn = row.querySelector("[data-pin-chat]");
            if (pinBtn) pinBtn.setAttribute("aria-pressed", item.pinned ? "true" : "false");
            const archiveBtn = row.querySelector("[data-archive-chat]");
            if (archiveBtn) archiveBtn.setAttribute("aria-pressed", item.archived ? "true" : "false");
        }}
        function reorderChatRows() {{
            const list = document.getElementById("chat-list");
            if (!list) return;
            const rows = Array.from(list.querySelectorAll("[data-chat-row]"));
            if (!rows.length) return;
            const pinned = rows.filter(row => row.dataset.pinned === "true" && row.dataset.archived !== "true");
            const normal = rows.filter(row => row.dataset.pinned !== "true" && row.dataset.archived !== "true");
            const archived = rows.filter(row => row.dataset.archived === "true");
            [...pinned, ...normal, ...archived].forEach(row => list.appendChild(row));
            const toggle = document.getElementById("toggle-archived-chats");
            if (toggle) {{
                toggle.hidden = archived.length === 0;
                toggle.textContent = list.classList.contains("show-archived")
                    ? "Hide archived"
                    : `Show archived (${{archived.length}})`;
            }}
        }}
        function hydrateSidebar() {{
            if (!deviceMemoryEnabled) return;
            document.querySelectorAll("[data-chat-row]").forEach(row => {{
                const id = row.dataset.chatId;
                const item = id ? chatIndexItem(id) : null;
                if (item) applyRowState(row, item);
            }});
            reorderChatRows();
        }}
        function rememberChatTitle(seedText = "") {{
            if (!deviceMemoryEnabled) return;
            const items = getChatMemory();
            const existing = chatIndexItem(chatId);
            const autoTitle = firstUserLine(items) || String(seedText || "New Chat").slice(0, 48);
            const title = existing?.renamed ? existing.title : autoTitle;
            const updated = updateChatIndexItem(chatId, {{ title }});
            const row = document.querySelector(`[data-chat-row][data-chat-id="${{chatId}}"]`);
            applyRowState(row, updated);
        }}
        function renderLocalChatMemory() {{
            if (!deviceMemoryEnabled || messages.querySelector(".message")) return;
            const items = getChatMemory();
            items.forEach(item => addMessage(item.role === "user" ? "user" : "Jarvis", item.content));
            if (items.length) rememberChatTitle();
        }}
        function exportLocalMemory(event) {{
            if (!deviceMemoryEnabled) return;
            event.preventDefault();
            const index = getChatIndex();
            const chats = index.map(item => ({{
                id: item.id,
                title: item.title || firstUserLine(getChatMemory(item.id)),
                messages: getChatMemory(item.id),
                assignment_memory: getAssignmentMemory(item.id),
                essay_workspace: readJsonStorage(`jarvis_essay_workspace_${{item.id}}_v1`, null),
                study_workspace: readJsonStorage(`jarvis_study_workspace_${{item.id}}_v1`, null)
            }}));
            if (!index.some(item => item.id === chatId)) {{
                chats.push({{
                    id: chatId,
                    title: firstUserLine(getChatMemory()),
                    messages: getChatMemory(),
                    assignment_memory: getAssignmentMemory(),
                    essay_workspace: readJsonStorage(`jarvis_essay_workspace_${{chatId}}_v1`, null),
                    study_workspace: readJsonStorage(`jarvis_study_workspace_${{chatId}}_v1`, null)
                }});
            }}
            const blob = new Blob([JSON.stringify({{
                exported_at: new Date().toISOString(),
                memory: "device",
                subject_profiles: readJsonStorage("jarvis_subject_profiles_v1", null),
                chats
            }}, null, 2)], {{ type: "application/json" }});
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = "jarvis-device-memory.json";
            link.click();
            window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        }}
        function loadActivityState() {{
            try {{
                const saved = JSON.parse(localStorage.getItem(activityKey) || "{{}}");
                return {{
                    runs: Number(saved.runs) || 0,
                    totalMs: Number(saved.totalMs) || 0,
                    history: Array.isArray(saved.history) ? saved.history.slice(-10).map(Number).filter(Number.isFinite) : []
                }};
            }} catch (error) {{
                return {{ runs: 0, totalMs: 0, history: [] }};
            }}
        }}
        function saveActivityState() {{
            try {{ localStorage.setItem(activityKey, JSON.stringify(activityState)); }} catch (error) {{}}
        }}
        function setMetric(name, value) {{
            document.querySelectorAll(`[data-activity-metric="${{name}}"]`).forEach(item => item.textContent = value);
        }}
        function formatDuration(ms) {{
            if (!ms) return "--";
            const seconds = ms / 1000;
            return seconds < 10 ? `${{seconds.toFixed(1)}}s` : `${{Math.round(seconds)}}s`;
        }}
        function renderActivityBars() {{
            const history = activityState.history.length ? activityState.history : [0, 0, 0, 0, 0, 0];
            const max = Math.max(...history, 1);
            document.querySelectorAll("[data-activity-bars]").forEach(container => {{
                container.innerHTML = history.slice(-10).map(ms => {{
                    const height = Math.max(14, Math.round((ms / max) * 100));
                    return `<i style="height:${{height}}%"></i>`;
                }}).join("");
            }});
        }}
        function updateActivityDashboard(live = "IDLE") {{
            const average = activityState.runs ? formatDuration(activityState.totalMs / activityState.runs) : "--";
            const last = activityState.history.length ? formatDuration(activityState.history[activityState.history.length - 1]) : "waiting";
            setMetric("runs", String(activityState.runs));
            setMetric("average", average);
            setMetric("last", last);
            setMetric("live", live);
            renderActivityBars();
        }}
        function recordActivityRun(elapsedMs) {{
            const cleanMs = Math.max(250, Math.min(Number(elapsedMs) || 0, 120000));
            activityState.runs += 1;
            activityState.totalMs += cleanMs;
            activityState.history.push(cleanMs);
            activityState.history = activityState.history.slice(-10);
            saveActivityState();
            updateActivityDashboard("COMPLETE");
        }}
        function loadEngineeringState() {{
            try {{
                const saved = JSON.parse(localStorage.getItem(engineeringKey) || "{{}}");
                return {{
                    active: Boolean(saved.active),
                    title: String(saved.title || ""),
                    latestBrief: String(saved.latestBrief || "")
                }};
            }} catch (error) {{
                return {{ active: false, title: "", latestBrief: "" }};
            }}
        }}
        function saveEngineeringState() {{
            try {{ localStorage.setItem(engineeringKey, JSON.stringify(engineeringState)); }} catch (error) {{}}
        }}
        function engineeringIntent(value) {{
            const lowered = String(value || "").toLowerCase();
            const parts = ["mount", "bracket", "adapter", "holder", "enclosure", "housing", "hinge", "joint", "gear", "linkage", "chassis", "fixture", "clamp", "mechanism", "robot arm", "helmet", "wearable", "3d print", "openscad", "solidworks", "fusion 360"];
            const actions = ["design", "engineer", "model", "prototype", "fabricate", "build", "make", "fit", "attach", "calculate"];
            if (/^(engineer|engineering|cad|prototype)\\s*:/.test(lowered)) return true;
            return parts.some(part => lowered.includes(part)) && (actions.some(action => lowered.includes(action)) || lowered.split(/\\s+/).length >= 4);
        }}
        function updateEngineeringDashboard(stateLabel) {{
            const active = engineeringState.active;
            if (engineeringStatus) engineeringStatus.textContent = stateLabel || (active ? "PROJECT ACTIVE" : (brainReady ? "READY" : "AI OFFLINE"));
            if (engineeringProject) engineeringProject.textContent = active ? engineeringState.title : "Awaiting a physical design";
            if (engineeringNext) engineeringNext.textContent = active
                ? "Jarvis is retaining this design context. Add measurements, constraints, photos, material limits, or ask for the next engineering decision."
                : "Describe the part, what it attaches to, and what it must carry. Jarvis will ask for only the measurements that control the design.";
            if (engineeringExport) engineeringExport.disabled = !engineeringState.latestBrief;
        }}
        function activateEngineeringProject(text) {{
            const cleaned = String(text || "").replace(/^(engineer|engineering|cad|prototype)\\s*:\\s*/i, "").trim();
            engineeringState.active = true;
            engineeringState.title = (cleaned || "Physical design project").slice(0, 78);
            saveEngineeringState();
            updateEngineeringDashboard("DESIGNING");
        }}
        function startEngineeringPreview() {{
            const canvas = document.getElementById("engineering-preview");
            if (!canvas) return;
            const context = canvas.getContext("2d");
            function draw(timestamp) {{
                const rect = canvas.getBoundingClientRect();
                const ratio = Math.min(window.devicePixelRatio || 1, 2);
                const width = Math.max(1, Math.round(rect.width * ratio));
                const height = Math.max(1, Math.round(rect.height * ratio));
                if (canvas.width !== width || canvas.height !== height) {{ canvas.width = width; canvas.height = height; }}
                context.clearRect(0, 0, width, height);
                const cyan = "rgba(69,240,255,.82)";
                const dim = "rgba(69,240,255,.25)";
                const signal = "rgba(156,255,114,.82)";
                context.lineWidth = ratio;
                for (let x = 0; x < width; x += 24 * ratio) {{ context.beginPath(); context.moveTo(x, 0); context.lineTo(x, height); context.strokeStyle = "rgba(69,240,255,.045)"; context.stroke(); }}
                for (let y = 0; y < height; y += 24 * ratio) {{ context.beginPath(); context.moveTo(0, y); context.lineTo(width, y); context.strokeStyle = "rgba(69,240,255,.045)"; context.stroke(); }}
                const left = width * .18, top = height * .24, partWidth = width * .64, partHeight = height * .5;
                context.strokeStyle = cyan; context.lineWidth = 1.4 * ratio; context.strokeRect(left, top, partWidth, partHeight);
                context.beginPath(); context.moveTo(left + partWidth * .35, top); context.lineTo(left + partWidth * .35, top + partHeight); context.moveTo(left + partWidth * .65, top); context.lineTo(left + partWidth * .65, top + partHeight); context.stroke();
                for (const x of [left + partWidth * .16, left + partWidth * .84]) {{ context.beginPath(); context.arc(x, top + partHeight * .5, partHeight * .13, 0, Math.PI * 2); context.strokeStyle = signal; context.stroke(); }}
                context.setLineDash([5 * ratio, 4 * ratio]); context.strokeStyle = dim; context.beginPath(); context.moveTo(left, top - 11 * ratio); context.lineTo(left + partWidth, top - 11 * ratio); context.stroke(); context.setLineDash([]);
                context.fillStyle = "rgba(202,235,240,.72)"; context.font = `${{9 * ratio}}px Consolas`; context.fillText("PARAMETRIC MOUNT / MEASUREMENTS REQUIRED", left, height - 10 * ratio);
                const sweep = (timestamp * .08 * ratio) % width; context.fillStyle = "rgba(69,240,255,.12)"; context.fillRect(sweep, 0, 2 * ratio, height);
                requestAnimationFrame(draw);
            }}
            requestAnimationFrame(draw);
        }}
        function startCoreVisual() {{
            const canvas = document.getElementById("jarvis-core");
            if (!canvas) return;
            const context = canvas.getContext("2d");
            const points = Array.from({{length:150}}, (_,index) => ({{
                angle:(index/150)*Math.PI*2, radius:.2+((index*37)%72)/100,
                size:.5+((index*19)%16)/10, speed:.00006+((index*11)%7)*.000008
            }}));
            function draw(timestamp) {{
                const rect=canvas.getBoundingClientRect(); const ratio=Math.min(window.devicePixelRatio||1,2);
                const width=Math.max(1,Math.round(rect.width*ratio)), height=Math.max(1,Math.round(rect.height*ratio));
                if(canvas.width!==width||canvas.height!==height){{canvas.width=width;canvas.height=height;}}
                context.clearRect(0,0,width,height); const cx=width/2,cy=height/2,scale=Math.min(width,height)*.35;
                context.lineWidth=ratio;
                for(let ring=1;ring<=4;ring+=1){{context.beginPath();context.arc(cx,cy,scale*(.24+ring*.18),0,Math.PI*2);context.strokeStyle=`rgba(69,240,255,${{.18-ring*.026}})`;context.stroke();}}
                for(const point of points){{const angle=point.angle+timestamp*point.speed;const px=cx+Math.cos(angle)*scale*point.radius;const py=cy+Math.sin(angle)*scale*point.radius*.72;context.beginPath();context.arc(px,py,point.size*ratio,0,Math.PI*2);context.fillStyle=point.radius>.65?"rgba(156,255,114,.78)":"rgba(69,240,255,.78)";context.fill();}}
                requestAnimationFrame(draw);
            }}
            requestAnimationFrame(draw);
        }}
        function escapeHtml(value) {{
            return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
        }}
        function decodeGalleryPayload(encoded) {{
            try {{
                const base64 = encoded.replaceAll("-", "+").replaceAll("_", "/");
                const padded = base64 + "=".repeat((4 - base64.length % 4) % 4);
                return JSON.parse(decodeURIComponent(escape(atob(padded))));
            }} catch (error) {{ return null; }}
        }}
        function imageGalleryHtml(text) {{
            const regex = /\\[\\[JARVIS_IMAGE_GALLERY:([A-Za-z0-9_\\-=]+)\\]\\]/g;
            let output = "";
            let match;
            while ((match = regex.exec(text)) !== null) {{
                const payload = decodeGalleryPayload(match[1]);
                if (!payload || !Array.isArray(payload.images)) continue;
                const cards = payload.images.slice(0, 12).map(item => {{
                    const thumb = String(item.thumbnail || item.image || "");
                    if (!thumb.startsWith("http://") && !thumb.startsWith("https://")) return "";
                    let href = String(item.url || item.image || thumb);
                    if (!href.startsWith("http://") && !href.startsWith("https://")) href = thumb;
                    const title = escapeHtml(String(item.title || payload.query || "Image result"));
                    const source = escapeHtml(String(item.source || "source"));
                    return `<a class="image-result" href="${{escapeHtml(href)}}" target="_blank" rel="noopener noreferrer">`
                        + `<img src="${{escapeHtml(thumb)}}" alt="${{title}}" loading="lazy" referrerpolicy="no-referrer">`
                        + `<span class="image-result-title">${{title}}</span>`
                        + `<span class="image-result-source">${{source}}</span>`
                        + `</a>`;
                }}).join("");
                if (cards) output += `<section class="image-gallery">${{cards}}</section>`;
            }}
            return output;
        }}
        function renderContent(content) {{
            const visible = content.replace(/\\[\\[JARVIS_IMAGE_GALLERY:[A-Za-z0-9_\\-=]+\\]\\]/g, "").trim();
            return escapeHtml(visible) + imageGalleryHtml(content);
        }}
        function addMessage(roleName, content) {{
            if (emptyState) emptyState.remove();
            if (suggestions) suggestions.remove();
            const article = document.createElement("article");
            article.className = "message " + (roleName === "user" ? "user" : "jarvis");
            const avatar = roleName === "user" ? "" : `<div class="avatar">J</div>`;
            article.innerHTML = avatar + `<div class="bubble">${{renderContent(content)}}</div>`;
            messages.appendChild(article);
            scrollDown();
            return article;
        }}
        function addPetMessage(roleName, content) {{
            if (!petMessages) return null;
            const bubble = document.createElement("div");
            bubble.className = "pet-message " + (roleName === "user" ? "user" : "pet");
            bubble.textContent = String(content || "");
            petMessages.appendChild(bubble);
            petMessages.scrollTop = petMessages.scrollHeight;
            return bubble;
        }}
        async function loadPetHistory() {{
            if (petLoaded || !petMessages) return;
            petLoaded = true;
            try {{
                const response = await fetch(`/api/pet/${{chatId}}`);
                const data = await response.json();
                const history = Array.isArray(data.history) ? data.history : [];
                history.forEach(item => addPetMessage(item.role === "user" ? "user" : "pet", item.content));
                if (!history.length) addPetMessage("pet", "Hello. I am Mini Jarvis, your companion. What should I call you?");
            }} catch (error) {{
                addPetMessage("pet", "I could not load our conversation just now, but you can still talk to me.");
            }}
        }}
        async function openPetPanel() {{
            if (!petPanel) return;
            petPanel.hidden = false;
            petToggle?.setAttribute("aria-expanded", "true");
            await loadPetHistory();
            petInput?.focus();
        }}
        function closePetPanel() {{
            if (!petPanel) return;
            petPanel.hidden = true;
            petToggle?.setAttribute("aria-expanded", "false");
            input.focus();
        }}
        async function sendPetMessage() {{
            if (petBusy || !petInput) return;
            const text = petInput.value.trim();
            if (!text) return;
            await loadPetHistory();
            addPetMessage("user", text);
            petInput.value = "";
            petBusy = true;
            if (petSend) petSend.disabled = true;
            const placeholder = addPetMessage("pet", "Thinking...");
            try {{
                const response = await fetch(`/api/pet/${{chatId}}`, {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{ message: text }})
                }});
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || data.answer || "Request failed");
                if (placeholder) placeholder.textContent = data.answer || "I lost that thought. Try me again.";
            }} catch (error) {{
                if (placeholder) placeholder.textContent = error.message || "I could not reach my conversation brain. Try me again shortly.";
            }} finally {{
                petBusy = false;
                if (petSend) petSend.disabled = false;
                if (petMessages) petMessages.scrollTop = petMessages.scrollHeight;
                petInput.focus();
            }}
        }}
        function closeMobileNav() {{ document.body.classList.remove("mobile-nav-open"); }}
        if (mobileMenu) mobileMenu.addEventListener("click", () => document.body.classList.toggle("mobile-nav-open"));
        if (mobileNavBackdrop) mobileNavBackdrop.addEventListener("click", closeMobileNav);
        if (chatSearchToggle && chatSearchWrap) chatSearchToggle.addEventListener("click", () => {{
            chatSearchWrap.hidden = !chatSearchWrap.hidden;
            if (!chatSearchWrap.hidden) chatSearch?.focus();
        }});
        if (chatSearch) chatSearch.addEventListener("input", () => {{
            const query = chatSearch.value.trim().toLowerCase();
            document.querySelectorAll("[data-chat-row]").forEach(row => {{
                row.hidden = Boolean(query) && !String(row.dataset.title || "").includes(query);
            }});
        }});
        function showChatToast(message, actionLabel, onAction) {{
            let toast = document.getElementById("chat-toast");
            if (!toast) {{
                toast = document.createElement("div");
                toast.id = "chat-toast";
                toast.className = "chat-toast";
                toast.setAttribute("role", "status");
                toast.setAttribute("aria-live", "polite");
                document.body.appendChild(toast);
            }}
            toast.innerHTML = "";
            const text = document.createElement("span");
            text.textContent = message;
            toast.appendChild(text);
            if (actionLabel && onAction) {{
                const button = document.createElement("button");
                button.type = "button";
                button.className = "chat-toast-action";
                button.textContent = actionLabel;
                button.addEventListener("click", () => {{ onAction(); hideChatToast(); }});
                toast.appendChild(button);
            }}
            toast.classList.add("show");
            clearTimeout(toast._hideTimer);
            toast._hideTimer = window.setTimeout(hideChatToast, 6400);
        }}
        function hideChatToast() {{
            const toast = document.getElementById("chat-toast");
            if (toast) toast.classList.remove("show");
        }}
        document.querySelectorAll("[data-delete-chat]").forEach(item => item.addEventListener("click", event => {{
            event.preventDefault();
            const targetId = item.dataset.deleteChat;
            if (!targetId || !window.confirm("Delete this conversation permanently?")) return;
            const row = item.closest("[data-chat-row]");
            const parent = row ? row.parentElement : null;
            const nextSibling = row ? row.nextSibling : null;
            if (row) row.hidden = true;
            let undone = false;
            const finalizeDelete = async () => {{
                if (undone) return;
                if (deviceMemoryEnabled) {{
                    try {{
                        localStorage.removeItem(`jarvis_chat_memory_${{targetId}}_v1`);
                        localStorage.removeItem(`jarvis_assignment_memory_${{targetId}}_v1`);
                        localStorage.removeItem(`jarvis_essay_workspace_${{targetId}}_v1`);
                        localStorage.removeItem(`jarvis_study_workspace_${{targetId}}_v1`);
                        saveChatIndex(getChatIndex().filter(chat => chat.id !== targetId));
                    }} catch (error) {{}}
                }}
                const response = await fetch(`/api/chats/${{targetId}}`, {{ method: "DELETE" }});
                if (!response.ok && response.status !== 404) {{
                    window.alert("That conversation could not be deleted.");
                    if (row) row.hidden = false;
                    return;
                }}
                if (row) row.remove();
                if (targetId === chatId) window.location.assign("/");
            }};
            const timer = window.setTimeout(finalizeDelete, 6000);
            showChatToast("Conversation deleted.", "Undo", () => {{
                undone = true;
                window.clearTimeout(timer);
                if (row) {{
                    row.hidden = false;
                    if (parent && !parent.contains(row)) parent.insertBefore(row, nextSibling);
                }}
                reorderChatRows();
            }});
        }}));
        document.querySelectorAll("[data-pin-chat]").forEach(item => item.addEventListener("click", event => {{
            event.preventDefault();
            const targetId = item.dataset.pinChat;
            if (!targetId) return;
            const existing = chatIndexItem(targetId);
            const updated = updateChatIndexItem(targetId, {{ pinned: !(existing && existing.pinned) }});
            applyRowState(item.closest("[data-chat-row]"), updated);
            reorderChatRows();
        }}));
        document.querySelectorAll("[data-archive-chat]").forEach(item => item.addEventListener("click", event => {{
            event.preventDefault();
            const targetId = item.dataset.archiveChat;
            if (!targetId) return;
            const existing = chatIndexItem(targetId);
            const updated = updateChatIndexItem(targetId, {{ archived: !(existing && existing.archived) }});
            applyRowState(item.closest("[data-chat-row]"), updated);
            reorderChatRows();
        }}));
        document.querySelectorAll("[data-rename-chat]").forEach(item => item.addEventListener("click", event => {{
            event.preventDefault();
            const targetId = item.dataset.renameChat;
            if (!targetId) return;
            const row = item.closest("[data-chat-row]");
            const current = chatIndexItem(targetId)?.title || row?.querySelector(".chat-title")?.textContent.trim() || "New Chat";
            const nextTitle = window.prompt("Rename this conversation", current);
            if (nextTitle === null) return;
            const trimmed = nextTitle.trim().slice(0, 48);
            if (!trimmed) return;
            const updated = updateChatIndexItem(targetId, {{ title: trimmed, renamed: true }});
            applyRowState(row, updated);
        }}));
        const toggleArchivedChats = document.getElementById("toggle-archived-chats");
        if (toggleArchivedChats) toggleArchivedChats.addEventListener("click", () => {{
            const list = document.getElementById("chat-list");
            if (!list) return;
            list.classList.toggle("show-archived");
            reorderChatRows();
        }});
        if (exportDeviceData) exportDeviceData.addEventListener("click", exportLocalMemory);
        if (assignmentAttach && assignmentFile) assignmentAttach.addEventListener("click", () => assignmentFile.click());
        if (assignmentFile) assignmentFile.addEventListener("change", uploadAssignmentMemoryFile);
        if (form) {{
            let dragDepth = 0;
            form.addEventListener("dragenter", event => {{
                if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
                event.preventDefault();
                dragDepth += 1;
                form.classList.add("drag-active");
            }});
            form.addEventListener("dragover", event => {{
                if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
                event.preventDefault();
            }});
            form.addEventListener("dragleave", () => {{
                dragDepth = Math.max(0, dragDepth - 1);
                if (dragDepth === 0) form.classList.remove("drag-active");
            }});
            form.addEventListener("drop", event => {{
                if (!event.dataTransfer?.files?.length) return;
                event.preventDefault();
                dragDepth = 0;
                form.classList.remove("drag-active");
                handleAssignmentFiles(event.dataTransfer.files);
            }});
        }}
        document.querySelectorAll('form[action="/new"]').forEach(newChatForm => newChatForm.addEventListener("submit", event => {{
            event.preventDefault();
            closeMobileNav();
            showJarvisOneTribute();
        }}));
        if (deleteDeviceData) deleteDeviceData.addEventListener("click", async () => {{
            if (!window.confirm("Delete every Jarvis and MJ conversation saved for this browser? This cannot be undone.")) return;
            if (deviceMemoryEnabled) {{
                try {{
                    getChatIndex().forEach(item => localStorage.removeItem(`jarvis_chat_memory_${{item.id}}_v1`));
                    getChatIndex().forEach(item => localStorage.removeItem(`jarvis_assignment_memory_${{item.id}}_v1`));
                    getChatIndex().forEach(item => localStorage.removeItem(`jarvis_essay_workspace_${{item.id}}_v1`));
                    getChatIndex().forEach(item => localStorage.removeItem(`jarvis_study_workspace_${{item.id}}_v1`));
                    localStorage.removeItem("jarvis_subject_profiles_v1");
                    localStorage.removeItem(chatMemoryKey);
                    localStorage.removeItem(assignmentMemoryKey);
                    localStorage.removeItem(chatIndexKey);
                }} catch (error) {{}}
            }}
            const response = await fetch("/api/device", {{ method: "DELETE" }});
            if (response.ok) window.location.assign("/");
            else window.alert("Your data could not be deleted just now.");
        }});
        if (petToggle) petToggle.addEventListener("click", () => petPanel?.hidden ? openPetPanel() : closePetPanel());
        document.querySelectorAll("[data-open-mj]").forEach(item => item.addEventListener("click", openPetPanel));
        if (petClose) petClose.addEventListener("click", closePetPanel);
        if (petForm) petForm.addEventListener("submit", event => {{ event.preventDefault(); sendPetMessage(); }});
        document.addEventListener("keydown", event => {{
            if (event.key === "Escape" && petPanel && !petPanel.hidden) {{
                event.preventDefault();
                closePetPanel();
            }}
        }});
        document.querySelectorAll(".suggestion").forEach(item => {{
            item.addEventListener("click", () => {{
                input.value = item.dataset.prompt || item.textContent.trim();
                input.focus();
            }});
        }});
        document.querySelectorAll(".launch-grid button[data-prompt]").forEach(item => {{
            item.addEventListener("click", () => {{
                input.value = item.dataset.prompt || item.textContent.trim();
                input.focus();
            }});
        }});
        document.querySelectorAll(".workspace-actions button[data-prompt]").forEach(item => {{
            item.addEventListener("click", () => {{
                input.value = item.dataset.prompt || item.textContent.trim();
                input.focus();
            }});
        }});
        if (engineeringExport) engineeringExport.addEventListener("click", () => {{
            if (!engineeringState.latestBrief) return;
            const content = `# Jarvis Engineering Brief\n\nProject: ${{engineeringState.title}}\n\n${{engineeringState.latestBrief}}\n`;
            const url = URL.createObjectURL(new Blob([content], {{ type: "text/markdown;charset=utf-8" }}));
            const link = document.createElement("a");
            link.href = url;
            link.download = "jarvis-engineering-brief.md";
            link.click();
            window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        }});
        if (workspaceClose) workspaceClose.addEventListener("click", () => document.body.classList.add("workspace-collapsed"));
        if (workspaceOpen) workspaceOpen.addEventListener("click", () => document.body.classList.remove("workspace-collapsed"));
        if (window.lucide) lucide.createIcons();
        hydrateSidebar();
        async function sendMessage() {{
            const text = input.value.trim();
            if (!text) return;
            const requestHistory = deviceMemoryEnabled ? getChatMemory().slice(-{MAX_HISTORY_MESSAGES}) : [];
            const assignmentMemory = deviceMemoryEnabled ? getAssignmentMemory() : [];
            const engineeringRun = activeMode === "engineer";
            if (engineeringRun && !engineeringState.active) activateEngineeringProject(text);
            else if (engineeringRun) updateEngineeringDashboard("ANALYSING");
            const startedAt = Date.now();
            addMessage("user", text);
            rememberMessage("user", text, activeMode);
            input.value = "";
            button.disabled = true;
            const placeholder = addMessage("Jarvis", "Thinking");
            let thinkingStep=0;
            const thinkingTimer=window.setInterval(()=>{{thinkingStep=(thinkingStep+1)%4;placeholder.querySelector(".bubble").textContent="Thinking"+".".repeat(thinkingStep);}},350);
            setCoreState("ANALYSING", true);
            updateActivityDashboard("THINKING");
            try {{
                const response = await fetch(`/api/chat/${{chatId}}`, {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{ message: text, mode: activeMode, history: requestHistory, assignment_memory: assignmentMemory }})
                }});
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || "Jarvis could not process that request.");
                const answerText = data.answer || "No response.";
                placeholder.querySelector(".bubble").innerHTML = renderContent(answerText);
                rememberMessage("Jarvis", answerText, activeMode);
                if (engineeringRun) {{
                    engineeringState.latestBrief = answerText;
                    saveEngineeringState();
                    updateEngineeringDashboard("BRIEF READY");
                }}
                const elapsedMs = Number(data.elapsed_ms) || (Date.now() - startedAt);
                if(elapsedMs&&latencyReadout) latencyReadout.textContent=`${{(elapsedMs/1000).toFixed(1)}}s`;
                recordActivityRun(elapsedMs);
            }} catch (error) {{
                placeholder.querySelector(".bubble").textContent = error.message || "Connection error. Jarvis.AI did not respond.";
                updateActivityDashboard("ERROR");
            }} finally {{
                window.clearInterval(thinkingTimer);
                button.disabled = false;
                setCoreState(brainReady ? "READY" : "AI OFFLINE", false);
                window.setTimeout(() => updateActivityDashboard("IDLE"), 1600);
                input.focus();
                scrollDown();
            }}
        }}
        form.addEventListener("submit", event => {{ event.preventDefault(); sendMessage(); }});
        input.addEventListener("keydown", event => {{
            if (event.key === "Enter" && !event.shiftKey) {{
                event.preventDefault();
                sendMessage();
            }}
        }});
        if ("serviceWorker" in navigator) {{
            navigator.serviceWorker.register("/sw.js").catch(() => {{}});
        }}
        {ad_boot}
        updateActivityDashboard();
        updateEngineeringDashboard();
        updateAssignmentMemoryStatus();
        renderAssignmentFileList();
        setCoreState(brainReady ? "READY" : "AI OFFLINE", false);
        renderLocalChatMemory();
        startEngineeringPreview();
        startCoreVisual();
        if (messages.querySelector(".message")) scrollDown();
        else chat.scrollTop = 0;
    </script>
</body>
</html>"""


@app.get("/manifest.json")
def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": APP_TITLE,
            "short_name": "Jarivs",
            "description": "Cloud-safe Jarivs assistant.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#000000",
            "theme_color": "#000000",
            "icons": [
                {
                    "src": "/icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any maskable",
                }
            ],
        }
    )


@app.get("/icon.svg")
def icon_svg() -> Response:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
<rect width="256" height="256" rx="56" fill="#050505"/>
<rect x="42" y="42" width="172" height="172" rx="40" fill="#121212" stroke="#3a3a3a" stroke-width="8"/>
<path d="M92 76h72v24h-24v80h-28v-80H92z" fill="#f5f5f5"/>
<circle cx="196" cy="60" r="11" fill="#22c55e"/>
</svg>"""
    return Response(svg, media_type="image/svg+xml")


@app.get("/offline", response_class=HTMLResponse)
def offline_page() -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
    <title>{APP_TITLE} offline</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#000000">
    <style>
        body {{
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            background: #000;
            color: #f2f2f2;
            font-family: "Segoe UI", Arial, sans-serif;
        }}
        main {{
            width: min(560px, calc(100vw - 32px));
            padding: 32px;
            border: 1px solid #2c2c2c;
            border-radius: 22px;
            background: #101010;
        }}
        h1 {{ margin: 0 0 12px; font-size: 28px; font-weight: 500; }}
        p {{ margin: 0 0 20px; color: #cfcfcf; line-height: 1.6; }}
        a {{
            display: inline-flex;
            min-height: 42px;
            align-items: center;
            padding: 0 16px;
            border-radius: 999px;
            color: #000;
            background: #f2f2f2;
            text-decoration: none;
            font-weight: 600;
        }}
    </style>
</head>
<body>
    <main>
        <h1>Jarivs is offline.</h1>
        <p>The app shell loaded, but the cloud server is not reachable from this network right now. Reconnect and try again.</p>
        <a href="/">Try again</a>
    </main>
</body>
</html>"""
    )


@app.get("/sw.js")
def service_worker() -> Response:
    script = """const CACHE_NAME = "__CACHE_VERSION__";
const SHELL_ASSETS = ["/offline", "/manifest.json", "/icon.svg"];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match("/offline")));
    return;
  }

  event.respondWith(
    caches.match(request).then(cached => {
      if (cached) return cached;
      return fetch(request).then(response => {
        if (response.ok && SHELL_ASSETS.includes(url.pathname)) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
        }
        return response;
      });
    })
  );
});""".replace("__CACHE_VERSION__", CACHE_VERSION)
    return Response(script, media_type="application/javascript")


@app.get("/robots.txt")
def robots_txt() -> Response:
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


def essay_workspace_html(chat_id: str, profile: dict[str, Any] | None) -> str:
    owner_label = str((profile or {}).get("name") or "Anonymous learner")
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#071018">
    <title>Essay workspace | {html.escape(APP_TITLE)}</title>
    <link rel="stylesheet" href="/assets/essay-workspace.css?v={APP_VERSION}">
    <script src="/assets/essay-workspace.js?v={APP_VERSION}" defer></script>
</head>
<body data-chat-id="{html.escape(chat_id, quote=True)}">
    <header class="workspace-bar">
        <a class="icon-link" href="/chat/{html.escape(chat_id, quote=True)}" aria-label="Back to Jarvis chat" title="Back to Jarvis chat">&larr;</a>
        <div class="workspace-title">
            <span>JARVIS / ESSAY</span>
            <input id="essay-title" maxlength="160" placeholder="Untitled assignment" aria-label="Assignment title">
        </div>
        <span class="save-state" id="save-state" role="status" aria-live="polite">Saved on this device</span>
        <div class="bar-actions">
            <span class="owner-label">{html.escape(owner_label)}</span>
            <button class="secondary" id="save-version" type="button">Save version</button>
            <a class="button" href="/chat/{html.escape(chat_id, quote=True)}">Open chat</a>
        </div>
    </header>
    <main class="essay-layout">
        <aside class="source-pane" aria-label="Assignment sources">
            <div class="pane-heading"><span>Assignment</span><strong>Sources</strong></div>
            <label class="field-label" for="assignment-brief">Task or question</label>
            <textarea id="assignment-brief" rows="7" maxlength="{MAX_MESSAGE_CHARS}" placeholder="Paste the assignment question or task"></textarea>
            <section class="source-section" data-source="rubric">
                <div class="section-title"><strong>Rubric</strong><button class="text-button" data-remove-source="rubric" type="button" hidden>Remove</button></div>
                <button class="upload-zone" data-upload="rubric" type="button">
                    <span class="upload-icon">+</span><span><strong>Add rubric</strong><small>PDF, DOCX, or notes</small></span>
                </button>
                <input id="rubric-file" type="file" accept=".txt,.md,.markdown,.csv,.json,.pdf,.docx,.pptx,.py,.js,.html,.css" hidden>
                <div class="source-file" id="rubric-summary" hidden></div>
            </section>
            <section class="source-section" data-source="feedback">
                <div class="section-title"><strong>Teacher feedback</strong><button class="text-button" data-remove-source="feedback" type="button" hidden>Remove</button></div>
                <button class="upload-zone" data-upload="feedback" type="button">
                    <span class="upload-icon">+</span><span><strong>Add feedback</strong><small>Previous comments or marked work</small></span>
                </button>
                <input id="feedback-file" type="file" accept=".txt,.md,.markdown,.csv,.json,.pdf,.docx,.pptx,.py,.js,.html,.css" hidden>
                <div class="source-file" id="feedback-summary" hidden></div>
            </section>
            <div class="source-status" id="source-status" role="status" aria-live="polite"></div>
        </aside>
        <section class="draft-pane" aria-label="Essay draft">
            <header class="pane-heading draft-heading"><div><span>Current version</span><strong>Draft</strong></div><span id="word-count">0 words</span></header>
            <textarea id="draft-editor" maxlength="{MAX_ATTACHMENT_CHARS}" spellcheck="true" placeholder="Start writing your draft"></textarea>
            <footer class="draft-footer"><span id="character-count">0 / {MAX_ATTACHMENT_CHARS}</span><button class="secondary" id="snapshot-draft" type="button">Save version</button></footer>
        </section>
        <aside class="review-pane" aria-label="Rubric review and versions">
            <section class="review-section">
                <div class="pane-heading"><span>Jarvis review</span><strong>Rubric check</strong></div>
                <button class="primary full" id="run-self-check" type="button">Check current draft</button>
                <div class="review-status" id="review-status" role="status" aria-live="polite"></div>
                <div class="review-result" id="review-result" hidden></div>
            </section>
            <section class="versions-section">
                <div class="pane-heading"><span>Saved snapshots</span><strong>Versions</strong></div>
                <div class="version-list" id="version-list"></div>
            </section>
        </aside>
    </main>
</body>
</html>"""


def study_workspace_html(chat_id: str, profile: dict[str, Any] | None) -> str:
    owner_label = str((profile or {}).get("name") or "Anonymous learner")
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#0b0f0f">
    <title>Study workspace | {html.escape(APP_TITLE)}</title>
    <link rel="stylesheet" href="/assets/study-workspace.css?v={APP_VERSION}">
    <script src="/assets/study-workspace.js?v={APP_VERSION}" defer></script>
</head>
<body data-chat-id="{html.escape(chat_id, quote=True)}">
    <header class="study-bar">
        <a class="icon-link" href="/chat/{html.escape(chat_id, quote=True)}" aria-label="Back to Jarvis chat" title="Back to Jarvis chat">&larr;</a>
        <div class="brand-block"><span>JARVIS / STUDY</span><strong>Study workspace</strong></div>
        <label class="profile-picker"><span>Subject profile</span><select id="profile-select" aria-label="Subject profile"></select></label>
        <button class="icon-button" id="new-profile" type="button" title="Create subject profile" aria-label="Create subject profile">+</button>
        <span class="save-state" id="study-save-state" role="status" aria-live="polite">Saved on this device</span>
        <span class="owner-label">{html.escape(owner_label)}</span>
        <a class="button" href="/chat/{html.escape(chat_id, quote=True)}">Open chat</a>
    </header>
    <main class="study-layout">
        <aside class="profile-pane" aria-label="Subject profile settings">
            <div class="pane-heading"><span>Learning context</span><strong>Subject profile</strong></div>
            <label>Profile name<input id="profile-name" maxlength="80" placeholder="e.g. Year 10 Science"></label>
            <label>Subject<input id="profile-subject" maxlength="120" placeholder="Subject"></label>
            <label>Year or level<input id="profile-level" maxlength="120" placeholder="Year 10, beginner, advanced"></label>
            <label>Curriculum<input id="profile-curriculum" maxlength="120" placeholder="State, country, or exam board"></label>
            <label>Learning goal<textarea id="profile-goal" rows="5" maxlength="500" placeholder="What are you working toward?"></textarea></label>
            <button class="danger-link" id="delete-profile" type="button">Delete profile</button>
        </aside>
        <section class="tool-pane" aria-label="Study tools">
            <header class="tool-header">
                <div class="tool-tabs" role="tablist" aria-label="Study tool">
                    <button class="tool-tab active" data-tool-tab="plan" role="tab" aria-selected="true" type="button">Plan</button>
                    <button class="tool-tab" data-tool-tab="flashcards" role="tab" aria-selected="false" type="button">Flashcards</button>
                    <button class="tool-tab" data-tool-tab="quiz" role="tab" aria-selected="false" type="button">Quiz</button>
                </div>
            </header>
            <div class="tool-context">
                <label>Topic<input id="study-topic" maxlength="500" placeholder="What are you studying?"></label>
                <label>Notes or source material<textarea id="study-notes" rows="5" maxlength="{MAX_MESSAGE_CHARS}" placeholder="Paste class notes, key facts, or a course outline"></textarea></label>
            </div>
            <section class="tool-view active" data-tool-view="plan" role="tabpanel">
                <div class="generator-controls">
                    <label>Target date<input id="plan-target-date" type="date"></label>
                    <label>Minutes per day<input id="plan-minutes" type="number" min="10" max="240" step="5" value="30"></label>
                    <button class="primary" id="generate-plan" type="button">Build study plan</button>
                </div>
                <div class="tool-status" id="plan-status" role="status" aria-live="polite"></div>
                <div class="plan-output" id="plan-output"></div>
            </section>
            <section class="tool-view" data-tool-view="flashcards" role="tabpanel" hidden>
                <div class="generator-controls compact">
                    <label>Cards<select id="flashcard-count"><option>6</option><option selected>8</option><option>10</option><option>12</option><option>16</option><option>20</option></select></label>
                    <button class="primary" id="generate-flashcards" type="button">Make flashcards</button>
                </div>
                <div class="tool-status" id="flashcard-status" role="status" aria-live="polite"></div>
                <div class="flashcard-output" id="flashcard-output"></div>
            </section>
            <section class="tool-view" data-tool-view="quiz" role="tabpanel" hidden>
                <div class="generator-controls compact">
                    <label>Questions<select id="quiz-count"><option>5</option><option selected>8</option><option>10</option><option>12</option><option>15</option></select></label>
                    <button class="primary" id="generate-quiz" type="button">Generate quiz</button>
                </div>
                <div class="tool-status" id="quiz-status" role="status" aria-live="polite"></div>
                <form class="quiz-output" id="quiz-output"></form>
            </section>
        </section>
        <aside class="progress-pane" aria-label="Study progress">
            <div class="pane-heading"><span>Current profile</span><strong>Progress</strong></div>
            <div class="progress-stats">
                <div><span>Plan</span><strong id="plan-progress">0%</strong></div>
                <div><span>Cards reviewed</span><strong id="cards-reviewed">0</strong></div>
                <div><span>Cards known</span><strong id="cards-known">0</strong></div>
                <div><span>Quiz average</span><strong id="quiz-average">--</strong></div>
                <div><span>Study time</span><strong id="study-minutes">0m</strong></div>
                <div><span>Sessions</span><strong id="study-sessions">0</strong></div>
            </div>
            <section class="activity-section">
                <div class="section-heading"><strong>Recent activity</strong><button class="text-button" id="reset-progress" type="button">Reset</button></div>
                <div class="activity-list" id="activity-list"></div>
            </section>
        </aside>
    </main>
</body>
</html>"""


def account_page_html(request: Request, csp_nonce: str) -> str:
    profile = current_user(request)
    device_id = device_id_from_request(request)
    chat_count = len(get_device_chats(device_id))
    signed_in = bool(profile)
    display_name = str((profile or {}).get("name") or "Anonymous user")
    email = str((profile or {}).get("email") or "Not signed in")
    initial = (display_name[:1] or "J").upper()
    login_action = (
        '<a class="button primary" href="/login/google">Sign in with Google</a>'
        if not signed_in and google_login_configured()
        else ""
    )
    sign_out_action = '<a class="button" href="/logout">Sign out</a>' if signed_in else ""
    account_label = "Google account" if signed_in else "Anonymous browser session"
    sync_label = "Available across devices when you use this Google account." if signed_in else "Stored for this browser only."
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Account | {html.escape(APP_TITLE)}</title>
    <style nonce="{html.escape(csp_nonce)}">
        :root {{ color-scheme: dark; --bg:#061019; --panel:#0b1824; --line:#244052; --text:#eef8ff; --muted:#9bb0bd; --accent:#45f0ff; --danger:#ff7b84; }}
        * {{ box-sizing:border-box; }}
        body {{ margin:0; min-height:100vh; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,sans-serif; }}
        main {{ width:min(760px,calc(100% - 32px)); margin:0 auto; padding:32px 0 56px; }}
        .back {{ color:#9bdde5; text-decoration:none; }}
        h1 {{ margin:28px 0 6px; font-size:32px; letter-spacing:0; }}
        .lead {{ margin:0 0 24px; color:var(--muted); }}
        .profile {{ display:flex; align-items:center; gap:16px; padding:20px; border:1px solid var(--line); border-radius:8px; background:var(--panel); }}
        .avatar {{ display:grid; place-items:center; width:54px; height:54px; flex:0 0 54px; border:1px solid #2c718b; border-radius:8px; background:#10283a; color:var(--accent); font-size:22px; font-weight:700; }}
        .identity {{ min-width:0; }}
        .identity strong,.identity span {{ display:block; overflow-wrap:anywhere; }}
        .identity span {{ color:var(--muted); }}
        .tag {{ margin-left:auto; color:#98ffae; font:12px/1.2 ui-monospace,monospace; text-transform:uppercase; }}
        .stats {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin:16px 0 28px; }}
        .stat {{ padding:16px; border-top:1px solid var(--line); }}
        .stat span {{ display:block; color:var(--muted); font-size:13px; }}
        .stat strong {{ display:block; margin-top:4px; font-size:18px; }}
        section {{ padding:22px 0; border-top:1px solid var(--line); }}
        h2 {{ margin:0 0 6px; font-size:19px; }}
        section p {{ margin:0 0 16px; color:var(--muted); }}
        .actions {{ display:flex; flex-wrap:wrap; gap:10px; }}
        .button {{ display:inline-flex; align-items:center; justify-content:center; min-height:42px; padding:0 15px; border:1px solid #315267; border-radius:6px; background:#0c1b28; color:var(--text); font:inherit; text-decoration:none; cursor:pointer; }}
        .button:hover {{ border-color:var(--accent); }}
        .button.primary {{ background:#b7f8ff; border-color:#b7f8ff; color:#061019; font-weight:700; }}
        .button.danger {{ border-color:#733641; color:#ffd9dc; }}
        .button:disabled {{ opacity:.55; cursor:wait; }}
        #account-message {{ min-height:24px; margin-top:12px; color:#aeeeba; }}
        @media(max-width:560px) {{ main {{ width:min(100% - 24px,760px); padding-top:20px; }} .profile {{ align-items:flex-start; }} .tag {{ display:none; }} .stats {{ grid-template-columns:1fr; }} .button {{ width:100%; }} }}
    </style>
</head>
<body><main>
    <a class="back" href="/">&larr; Back to Jarvis</a>
    <h1>Your account</h1>
    <p class="lead">Manage your identity, conversations, and locally stored assignment memory.</p>
    <div class="profile">
        <div class="avatar" aria-hidden="true">{html.escape(initial)}</div>
        <div class="identity"><strong>{html.escape(display_name)}</strong><span>{html.escape(email)}</span></div>
        <span class="tag">{html.escape(account_label)}</span>
    </div>
    <div class="stats">
        <div class="stat"><span>Conversations</span><strong>{chat_count}</strong></div>
        <div class="stat"><span>Memory</span><strong>{'This device + account routing' if signed_in else 'This device'}</strong></div>
    </div>
    <section>
        <h2>Sign-in</h2>
        <p>{html.escape(sync_label)}</p>
        <div class="actions">{login_action}{sign_out_action}</div>
    </section>
    <section>
        <h2>Export your data</h2>
        <p>Downloads server-owned conversation records together with chats, assignment files, and preferences stored in this browser.</p>
        <div class="actions"><button class="button" id="export-account" type="button">Download data</button></div>
    </section>
    <section>
        <h2>{'Delete account data' if signed_in else 'Delete browser data'}</h2>
        <p>This permanently removes conversation records owned by this {'account' if signed_in else 'browser'}, clears Jarvis data from this browser, and signs you out.</p>
        <div class="actions"><button class="button danger" id="delete-account" type="button">Delete my data</button></div>
        <div id="account-message" role="status" aria-live="polite"></div>
    </section>
</main>
<script nonce="{html.escape(csp_nonce)}">
    const exportButton = document.getElementById("export-account");
    const deleteButton = document.getElementById("delete-account");
    const accountMessage = document.getElementById("account-message");
    function readStoredJson(key, fallback) {{
        try {{ return JSON.parse(localStorage.getItem(key) || "null") ?? fallback; }} catch (error) {{ return fallback; }}
    }}
    function browserData() {{
        const index = readStoredJson("jarvis_chat_index_v1", []);
        const ids = new Set(Array.isArray(index) ? index.map(item => String(item?.id || "")).filter(Boolean) : []);
        for (let position = 0; position < localStorage.length; position += 1) {{
            const key = localStorage.key(position) || "";
            const match = key.match(/^jarvis_(?:(?:chat|assignment)_memory|essay_workspace|study_workspace)_([0-9a-f-]{{36}})_v1$/i);
            if (match) ids.add(match[1]);
        }}
        return {{
            chat_index: index,
            chats: Array.from(ids).map(id => ({{
                id,
                messages: readStoredJson(`jarvis_chat_memory_${{id}}_v1`, []),
                assignment_memory: readStoredJson(`jarvis_assignment_memory_${{id}}_v1`, []),
                essay_workspace: readStoredJson(`jarvis_essay_workspace_${{id}}_v1`, null),
                study_workspace: readStoredJson(`jarvis_study_workspace_${{id}}_v1`, null),
                mode: localStorage.getItem(`jarvis_mode_${{id}}`) || "chat"
            }})),
            preferences: {{
                activity: readStoredJson("jarvis_web_activity_v1", {{}}),
                subject_profiles: readStoredJson("jarvis_subject_profiles_v1", null)
            }}
        }};
    }}
    function clearBrowserData() {{
        const keys = [];
        for (let position = 0; position < localStorage.length; position += 1) {{
            const key = localStorage.key(position) || "";
            if (key.startsWith("jarvis_")) keys.push(key);
        }}
        keys.forEach(key => localStorage.removeItem(key));
    }}
    exportButton.addEventListener("click", async () => {{
        exportButton.disabled = true;
        accountMessage.textContent = "Preparing your download...";
        try {{
            const response = await fetch("/api/device/export");
            if (!response.ok) throw new Error("Export failed");
            const payload = await response.json();
            payload.browser_data = browserData();
            const blob = new Blob([JSON.stringify(payload, null, 2)], {{ type:"application/json" }});
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = "jarvis-account-data.json";
            link.click();
            window.setTimeout(() => URL.revokeObjectURL(url), 1000);
            accountMessage.textContent = "Your data download is ready.";
        }} catch (error) {{ accountMessage.textContent = "Jarvis could not prepare the download. Try again."; }}
        finally {{ exportButton.disabled = false; }}
    }});
    deleteButton.addEventListener("click", async () => {{
        if (!window.confirm("Permanently delete all Jarvis data owned by this account or browser? This cannot be undone.")) return;
        deleteButton.disabled = true;
        accountMessage.textContent = "Deleting your Jarvis data...";
        try {{
            const response = await fetch("/api/device", {{ method:"DELETE" }});
            if (!response.ok) throw new Error("Delete failed");
            clearBrowserData();
            window.location.assign("/");
        }} catch (error) {{
            deleteButton.disabled = false;
            accountMessage.textContent = "Jarvis could not delete your data. Nothing else was changed.";
        }}
    }});
</script></body></html>"""


def public_page_csp_header() -> str:
    return (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'; form-action 'self'; upgrade-insecure-requests"
    )


def public_page_html(title: str, active: str, content: str, *, script: str = "") -> str:
    links = (("Chat", "/", "chat"), ("Roadmap", "/roadmap", "roadmap"), ("Feedback", "/feedback", "feedback"), ("Privacy", "/privacy", "privacy"))
    navigation_parts = []
    for label, href, key in links:
        current = ' aria-current="page"' if key == active else ""
        navigation_parts.append(f'<a href="{href}"{current}>{label}</a>')
    navigation = "".join(navigation_parts)
    script_tag = f'<script src="{html.escape(script, quote=True)}" defer></script>' if script else ""
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)} | {html.escape(APP_TITLE)}</title>
    <link rel="icon" href="/icon.svg" type="image/svg+xml">
    <link rel="stylesheet" href="/assets/public-launch.css">
    {script_tag}
</head>
<body>
    <header class="site-header">
        <div class="header-inner">
            <a class="brand" href="/"><span class="brand-mark">J</span><span>{html.escape(APP_TITLE)}</span></a>
            <nav class="site-nav" aria-label="Public navigation">{navigation}</nav>
        </div>
    </header>
    <main>{content}</main>
</body>
</html>"""


def feedback_page_html() -> str:
    return public_page_html(
        "Feedback",
        "feedback",
        """
        <header class="page-intro">
            <span class="eyebrow">Public launch</span>
            <h1>Help improve Jarvis</h1>
            <p>Report a problem, request a useful feature, or tell us what made learning easier or harder.</p>
        </header>
        <section class="page-section">
            <form id="feedback-form">
                <div class="field-grid">
                    <label>Type
                        <select name="category">
                            <option value="general">General feedback</option>
                            <option value="bug">Bug report</option>
                            <option value="feature">Feature request</option>
                            <option value="safety">Safety concern</option>
                            <option value="accessibility">Accessibility</option>
                        </select>
                    </label>
                    <label>What should we know?
                        <textarea name="message" minlength="3" maxlength="2000" required placeholder="Describe what happened, what you expected, or what would help."></textarea>
                        <span>Do not include passwords, private school records, or other sensitive information.</span>
                    </label>
                </div>
                <label>Contact (optional)
                    <input name="contact" type="text" maxlength="240" autocomplete="email" placeholder="Email or another way to reply">
                    <span>Leave this blank to submit anonymously.</span>
                </label>
                <label class="honeypot" aria-hidden="true">Company<input name="company" type="text" tabindex="-1" autocomplete="off"></label>
                <div class="form-actions">
                    <button id="feedback-submit" type="submit">Send feedback</button>
                    <p class="form-status" id="feedback-status" role="status" aria-live="polite"></p>
                </div>
            </form>
            <p class="privacy-note">Feedback is stored separately from chats for up to 365 days. Aggregate launch counters never contain prompts or account identifiers.</p>
        </section>
        """,
        script="/assets/feedback.js",
    )


def roadmap_page_html() -> str:
    rows = (
        ("Available", "", "Core public tutor", "Nine modes, device-held chat memory, essay and study workspaces, file extraction, account controls, and provider fallback."),
        ("Available", "", "Public launch controls", "Optional ad placements, aggregate analytics, feedback intake, owner cost visibility, privacy documentation, and deployment health checks."),
        ("Next", "next", "Reliability and safety", "Provider budgets, moderation review tools, stronger abuse controls, accessibility audits, and clearer service incident reporting."),
        ("Next", "next", "Student workflow depth", "Assignment organisation, citations, richer exports, progress views, and curriculum-aware study support."),
        ("Later", "later", "New interfaces", "Voice, richer visual input, teacher collaboration, and optional integrations after privacy and cost controls are proven."),
    )
    roadmap_rows = "".join(
        f'<div class="roadmap-row"><span class="roadmap-status {css_class}">{status}</span>'
        f'<div><h3>{html.escape(title)}</h3><p>{html.escape(description)}</p></div></div>'
        for status, css_class, title, description in rows
    )
    return public_page_html(
        "Roadmap",
        "roadmap",
        f"""
        <header class="page-intro">
            <span class="eyebrow">Version {html.escape(APP_VERSION)}</span>
            <h1>Public roadmap</h1>
            <p>Jarvis is being built as one dependable tutor first. New modes and integrations follow only when privacy, usefulness, reliability, and running cost are understood.</p>
        </header>
        <section class="page-section">
            <div class="section-heading"><h2>Build direction</h2><p>Updated with public releases</p></div>
            <div class="roadmap-list">{roadmap_rows}</div>
        </section>
        <section class="page-section">
            <h2>Shape the order</h2>
            <p>The roadmap is not a promise of dates. Real student feedback, safety issues, reliability, and operating cost decide what moves first.</p>
            <a class="button" href="/feedback">Send feedback</a>
        </section>
        """,
    )


def _format_integer(value: Any) -> str:
    return f"{_nonnegative_int(value):,}"


def _format_cost(micros: Any) -> str:
    return f"${_nonnegative_int(micros) / 1_000_000:,.4f}"


def cost_dashboard_html(snapshot: dict[str, Any]) -> str:
    usage = snapshot.get("usage", {})
    events = snapshot.get("events", {})
    daily_rows = "".join(
        "<tr>"
        f'<td>{html.escape(str(item.get("day", "")))}</td>'
        f'<td class="number">{_format_integer(item.get("events", {}).get("chat_request", 0))}</td>'
        f'<td class="number">{_format_integer(item.get("usage", {}).get("requests", 0))}</td>'
        f'<td class="number">{_format_integer(item.get("usage", {}).get("input_tokens", 0))}</td>'
        f'<td class="number">{_format_integer(item.get("usage", {}).get("output_tokens", 0))}</td>'
        f'<td class="number">{_format_cost(item.get("usage", {}).get("estimated_cost_micros", 0))}</td>'
        "</tr>"
        for item in snapshot.get("daily", [])
    ) or '<tr><td colspan="6" class="empty">No launch usage has been recorded yet.</td></tr>'
    provider_rows = "".join(
        "<tr>"
        f'<td>{html.escape(str(item.get("provider", "")))}</td>'
        f'<td>{html.escape(str(item.get("model", "")))}</td>'
        f'<td class="number">{_format_integer(item.get("requests", 0))}</td>'
        f'<td class="number">{_format_integer(item.get("errors", 0))}</td>'
        f'<td class="number">{_format_integer(item.get("input_tokens", 0) + item.get("output_tokens", 0))}</td>'
        f'<td class="number">{_format_cost(item.get("estimated_cost_micros", 0))}</td>'
        "</tr>"
        for item in snapshot.get("providers", [])
    ) or '<tr><td colspan="6" class="empty">No provider attempts have been recorded yet.</td></tr>'
    feedback_items = "".join(
        '<article class="feedback-item">'
        '<div class="feedback-meta">'
        f'<strong>{html.escape(str(item.get("category", "general")).title())}</strong>'
        f'<span>{html.escape(str(item.get("created_at", "")))}</span>'
        + (
            f'<span class="feedback-contact">{html.escape(str(item.get("contact", "")))}</span>'
            if item.get("contact")
            else ""
        )
        + "</div>"
        f'<p>{html.escape(str(item.get("message", "")))}</p>'
        "</article>"
        for item in snapshot.get("feedback", [])
    ) or '<p class="empty">No feedback has arrived yet.</p>'
    pricing_notice = "" if pricing_configured() else (
        '<p class="notice">Token counts are available, but provider prices are not configured. Add the per-million-token environment variables before treating the cost total as money spent.</p>'
    )
    return public_page_html(
        "Launch metrics",
        "",
        f"""
        <header class="page-intro">
            <span class="eyebrow">Owner operations</span>
            <h1>Launch metrics</h1>
            <p>Thirty-day aggregate usage, estimated provider cost, reliability, and deliberately submitted feedback. No prompts or chat text are collected here.</p>
        </header>
        {pricing_notice}
        <section class="page-section">
            <div class="stats">
                <div class="stat"><span>Chat requests</span><strong>{_format_integer(events.get("chat_request", 0))}</strong></div>
                <div class="stat"><span>Provider attempts</span><strong>{_format_integer(usage.get("requests", 0))}</strong></div>
                <div class="stat"><span>Total tokens</span><strong>{_format_integer(usage.get("input_tokens", 0) + usage.get("output_tokens", 0))}</strong></div>
                <div class="stat"><span>Estimated cost</span><strong>{_format_cost(usage.get("estimated_cost_micros", 0))}</strong></div>
            </div>
        </section>
        <section class="page-section">
            <div class="section-heading"><h2>Daily usage</h2><p>UTC, last 30 days</p></div>
            <div class="table-wrap"><table><thead><tr><th>Day</th><th class="number">Chats</th><th class="number">Attempts</th><th class="number">Input</th><th class="number">Output</th><th class="number">Cost</th></tr></thead><tbody>{daily_rows}</tbody></table></div>
        </section>
        <section class="page-section">
            <div class="section-heading"><h2>Providers</h2><p>{_format_integer(usage.get("errors", 0))} failed attempts</p></div>
            <div class="table-wrap"><table><thead><tr><th>Provider</th><th>Model</th><th class="number">Attempts</th><th class="number">Errors</th><th class="number">Tokens</th><th class="number">Cost</th></tr></thead><tbody>{provider_rows}</tbody></table></div>
        </section>
        <section class="page-section">
            <div class="section-heading"><h2>Recent feedback</h2><p>Latest 50 submissions</p></div>
            <div class="feedback-list">{feedback_items}</div>
        </section>
        """,
    )


@app.get("/feedback", response_class=HTMLResponse)
def feedback_page() -> HTMLResponse:
    record_public_event("feedback_open")
    response = HTMLResponse(feedback_page_html())
    response.headers["Content-Security-Policy"] = public_page_csp_header()
    return response


@app.get("/roadmap", response_class=HTMLResponse)
def roadmap_page() -> HTMLResponse:
    record_public_event("roadmap_open")
    response = HTMLResponse(roadmap_page_html())
    response.headers["Content-Security-Policy"] = public_page_csp_header()
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


@app.get("/admin/costs", response_class=HTMLResponse)
def cost_dashboard(request: Request) -> HTMLResponse:
    if not is_admin_profile(current_user(request)):
        return HTMLResponse("Not found.", status_code=404)
    response = HTMLResponse(cost_dashboard_html(STORE.launch_snapshot(days=30, feedback_limit=50)))
    response.headers["Content-Security-Policy"] = public_page_csp_header()
    return response


@app.get("/privacy", response_class=HTMLResponse)
def privacy() -> HTMLResponse:
    record_public_event("privacy_open")
    content = """
        <header class="page-intro">
            <span class="eyebrow">Public service</span>
            <h1>Privacy</h1>
            <p>Jarvis can be used anonymously. Optional Google sign-in lets the same account reopen its conversations on another device.</p>
        </header>
        <section class="page-section">
            <h2>Chats and school work</h2>
            <p>Main chat text, titles, essay drafts, rubric text, teacher feedback, study materials, subject profiles, and progress are stored in this browser when device memory is enabled. The server keeps lightweight conversation routing records and may keep Mini Jarvis chat text. Signed sessions contain the account identifier, name, and email returned by Google; Jarvis never sees the Google password.</p>
        </section>
        <section class="page-section">
            <h2>AI providers</h2>
            <p>When a user sends a message, requests a rubric check, or generates a study tool, the context needed for that result is sent to the configured AI provider. Do not submit passwords, payment details, medical records, or other highly sensitive information.</p>
        </section>
        <section class="page-section">
            <h2>Aggregate analytics and cost</h2>
            <p>Jarvis counts a small allowlist of product events and aggregates provider attempts, token counts, failures, model names, and estimated cost by UTC day. These launch metrics do not store prompts, responses, chat identifiers, account identifiers, or IP addresses and are retained for up to 120 days.</p>
        </section>
        <section class="page-section">
            <h2>Feedback</h2>
            <p>The feedback form stores the category, message, optional contact detail, and submission time for up to 365 days. Feedback is separate from chats. Leave contact blank to submit anonymously and do not include sensitive student records.</p>
        </section>
        <section class="page-section">
            <h2>Ads</h2>
            <p>Ads are disabled unless the site owner configures approved Google AdSense identifiers. When enabled, Google advertising scripts may use cookies or similar browser signals under Google's policies. Sponsored placements are labelled and kept outside the conversation message stream.</p>
        </section>
        <section class="page-section">
            <h2>User controls</h2>
            <p>The account page can export browser-held Jarvis data and remove conversation routing records owned by the current browser or signed-in account. Signing out clears the Google session cookie. The public service cannot open apps, read desktop files, or control a private computer.</p>
        </section>
    """
    response = HTMLResponse(public_page_html("Privacy", "privacy", content))
    response.headers["Content-Security-Policy"] = public_page_csp_header()
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


def status_payload() -> dict[str, Any]:
    providers = available_cloud_providers()
    storage_online = STORE.health()
    fully_ready = bool(
        providers and storage_online and (DEVICE_MEMORY_ENABLED or STORE.persistent) and SESSION_SECRET_CONFIGURED
    )
    return {
        "status": "ready" if fully_ready else "degraded",
        "app": APP_TITLE,
        "version": APP_VERSION,
        "time": now_stamp(),
        "cloud_brain_configured": bool(providers),
        "storage_online": storage_online,
        "storage_backend": STORE.backend_name,
        "storage_persistent": STORE.persistent,
        "memory_location": "device" if DEVICE_MEMORY_ENABLED else "server",
        "ads_configured": ads_enabled(),
        "analytics_enabled": ANALYTICS_ENABLED,
        "usage_metrics_enabled": USAGE_METRICS_ENABLED,
        "feedback_enabled": True,
        "admin_identity_configured": admin_identity_configured(),
        "provider_pricing_configured": pricing_configured(),
        "google_login_configured": google_login_configured(),
        "stable_sessions": SESSION_SECRET_CONFIGURED,
        "device_control": False,
        "network_mode": "cloud-safe",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "online", "app": APP_TITLE, "version": APP_VERSION}


@app.get("/ready")
def ready() -> JSONResponse:
    payload = status_payload()
    ready_now = payload["status"] == "ready"
    return JSONResponse(payload, status_code=200 if ready_now else 503)


@app.get("/status")
def status() -> JSONResponse:
    return JSONResponse(status_payload())


@app.get("/account", response_class=HTMLResponse)
def account(request: Request) -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    response = HTMLResponse(account_page_html(request, nonce))
    response.headers["Content-Security-Policy"] = chat_csp_header(nonce)
    set_device_cookie(response, request, device_id_from_request(request))
    return response


@app.get("/login/google")
def login_google(request: Request) -> Response:
    if not google_login_configured():
        return HTMLResponse("Google login is not configured for this Jarvis server.", status_code=404)
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": google_redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    response = RedirectResponse(url=f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=303)
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=request_origin(request).startswith("https://"),
        samesite="lax",
    )
    return response


@app.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
    if error:
        return HTMLResponse("Google sign-in was cancelled.", status_code=400)
    if not google_login_configured():
        return HTMLResponse("Google login is not configured for this Jarvis server.", status_code=404)
    expected_state = request.cookies.get(OAUTH_STATE_COOKIE, "")
    if not state or not expected_state or not hmac.compare_digest(state, expected_state):
        return HTMLResponse("Google sign-in state did not match. Please try again.", status_code=400)
    if not code:
        return HTMLResponse("Google did not return an authorization code.", status_code=400)
    try:
        token_response = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": google_redirect_uri(request),
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        token_response.raise_for_status()
        id_token = str(token_response.json().get("id_token", "")).strip()
        if not id_token:
            raise ValueError("Google did not return an ID token")
        profile_response = requests.get(GOOGLE_TOKENINFO_URL, params={"id_token": id_token}, timeout=10)
        profile_response.raise_for_status()
        google_profile = profile_response.json()
    except Exception:
        LOGGER.exception("Google sign-in failed")
        response = HTMLResponse("Google sign-in failed. Please try again.", status_code=502)
        response.delete_cookie(OAUTH_STATE_COOKIE)
        return response
    if google_profile.get("aud") != GOOGLE_CLIENT_ID:
        response = HTMLResponse("Google sign-in token was not issued for this Jarvis app.", status_code=400)
        response.delete_cookie(OAUTH_STATE_COOKIE)
        return response
    subject = str(google_profile.get("sub", "")).strip()
    if not subject:
        response = HTMLResponse("Google sign-in did not include an account id.", status_code=400)
        response.delete_cookie(OAUTH_STATE_COOKIE)
        return response
    profile = {
        "sub": subject,
        "email": str(google_profile.get("email", "")).strip(),
        "name": str(google_profile.get("name", "")).strip(),
        "email_verified": str(google_profile.get("email_verified", "")).strip().lower() == "true",
    }
    response = RedirectResponse(url="/", status_code=303)
    set_auth_cookie(response, request, profile)
    response.delete_cookie(OAUTH_STATE_COOKIE)
    response.delete_cookie(DEVICE_COOKIE)
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(AUTH_COOKIE)
    response.delete_cookie(OAUTH_STATE_COOKIE)
    response.delete_cookie(DEVICE_COOKIE)
    return response


@app.post("/logout")
def logout_post() -> RedirectResponse:
    return logout()


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> RedirectResponse:
    record_public_event("session_start")
    device_id = device_id_from_request(request)
    chats = list_chats(device_id)
    chat_id = chats[-1][0] if chats else create_chat(device_id)
    response = RedirectResponse(url=f"/chat/{chat_id}", status_code=303)
    set_device_cookie(response, request, device_id)
    return response


@app.post("/new")
def new_chat(request: Request) -> RedirectResponse:
    device_id = device_id_from_request(request)
    chat_id = create_chat(device_id)
    record_public_event("chat_created")
    response = RedirectResponse(url=f"/chat/{chat_id}", status_code=303)
    set_device_cookie(response, request, device_id)
    return response


@app.get("/new")
def old_new_chat_link() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=303)


@app.get("/chat/{chat_id}", response_class=HTMLResponse)
def open_chat(chat_id: str, request: Request) -> HTMLResponse:
    device_id = device_id_from_request(request)
    chat_id = canonical_id(chat_id) or ""
    if not chat_id or not STORE.owns_chat(device_id, chat_id):
        return HTMLResponse("This conversation is unavailable.", status_code=404)
    record_public_event("chat_open")
    nonce = secrets.token_urlsafe(18)
    response = HTMLResponse(page_html(chat_id, device_id, nonce, current_user(request)))
    response.headers["Content-Security-Policy"] = chat_csp_header(nonce)
    set_device_cookie(response, request, device_id)
    return response


@app.get("/essay/{chat_id}", response_class=HTMLResponse)
def open_essay_workspace(chat_id: str, request: Request) -> HTMLResponse:
    device_id = device_id_from_request(request)
    chat_id = canonical_id(chat_id) or ""
    if not chat_id or not STORE.owns_chat(device_id, chat_id):
        return HTMLResponse("This essay workspace is unavailable.", status_code=404)
    record_public_event("essay_open")
    nonce = secrets.token_urlsafe(18)
    response = HTMLResponse(essay_workspace_html(chat_id, current_user(request)))
    response.headers["Content-Security-Policy"] = chat_csp_header(nonce)
    set_device_cookie(response, request, device_id)
    return response


@app.get("/study/{chat_id}", response_class=HTMLResponse)
def open_study_workspace(chat_id: str, request: Request) -> HTMLResponse:
    device_id = device_id_from_request(request)
    chat_id = canonical_id(chat_id) or ""
    if not chat_id or not STORE.owns_chat(device_id, chat_id):
        return HTMLResponse("This study workspace is unavailable.", status_code=404)
    record_public_event("study_open")
    nonce = secrets.token_urlsafe(18)
    response = HTMLResponse(study_workspace_html(chat_id, current_user(request)))
    response.headers["Content-Security-Policy"] = chat_csp_header(nonce)
    set_device_cookie(response, request, device_id)
    return response


def user_message_record(text: str, mode: ChatMode, attachments: list[AttachmentContext]) -> dict[str, Any]:
    record: dict[str, Any] = {"role": "user", "content": text, "time": now_stamp(), "mode": mode}
    if attachments:
        record["model_content"] = attachment_prompt(text, attachments)
        record["attachments"] = [
            {"name": safe_upload_name(item.name), "media_type": item.media_type}
            for item in attachments
        ]
    return record


def assistant_message_record(answer: str, mode: ChatMode) -> dict[str, Any]:
    return {"role": "Jarvis", "content": answer, "time": now_stamp(), "mode": mode}


def ndjson_event(event_type: str, **payload: Any) -> str:
    return json.dumps({"type": event_type, **payload}, ensure_ascii=False) + "\n"


def cloud_generate_stream(
    prompt: str,
    history: list[dict[str, Any]] | None = None,
    mode: ChatMode = "chat",
    system_prompt: str | None = None,
):
    answer = cloud_generate(prompt, history=history, mode=mode, system_prompt=system_prompt)
    if answer:
        yield answer


@app.post("/api/feedback")
def api_feedback(payload: FeedbackRequest, request: Request) -> JSONResponse:
    allowed, retry_after = FEEDBACK_RATE_LIMITER.allow(feedback_rate_key(request))
    if not allowed:
        response = JSONResponse(
            {"detail": "Too many feedback submissions. Please try again later."},
            status_code=429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
    if payload.company.strip():
        return JSONResponse({"accepted": True})
    message = clean_text(payload.message)
    if len(message) < 3:
        return JSONResponse({"detail": "Please add a little more detail."}, status_code=400)
    contact = re.sub(r"[\x00-\x1f\x7f]+", " ", payload.contact).strip()[:240]
    try:
        feedback_id = STORE.save_feedback(
            {
                "id": str(uuid.uuid4()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "category": payload.category,
                "message": message,
                "contact": contact,
            }
        )
    except Exception as exc:
        LOGGER.warning("Feedback write failed: %s", type(exc).__name__)
        return JSONResponse({"detail": "Feedback storage is temporarily unavailable."}, status_code=503)
    record_public_event("feedback_submit")
    return JSONResponse({"accepted": True, "id": feedback_id}, status_code=201)


@app.post("/api/chats/{chat_id}/attachments")
async def api_extract_attachment(chat_id: str, request: Request, file: UploadFile = File(...)) -> JSONResponse:
    device_id = device_id_from_request(request)
    chat_id = canonical_id(chat_id) or ""
    if not chat_id or not STORE.owns_chat(device_id, chat_id):
        return JSONResponse({"detail": "Conversation not found."}, status_code=404)
    limited = rate_limit_response(request, device_id)
    if limited:
        return limited
    filename = file.filename or "document"
    media_type = file.content_type or ""
    try:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
    finally:
        await file.close()
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse({"detail": "That document is larger than the upload limit."}, status_code=413)
    try:
        name, media_type, text = extract_document(data, filename, media_type)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    record_public_event("attachment_extract")
    return JSONResponse({"name": name, "media_type": media_type, "text": text, "characters": len(text)})


@app.post("/api/chat/{chat_id}")
def api_chat(chat_id: str, payload: ChatRequest, request: Request) -> JSONResponse:
    device_id = device_id_from_request(request)
    chat_id = canonical_id(chat_id) or ""
    if not chat_id or not STORE.owns_chat(device_id, chat_id):
        return JSONResponse({"detail": "Conversation not found."}, status_code=404)
    limited = rate_limit_response(request, device_id)
    if limited:
        return limited

    text = clean_text(payload.message)
    if not text:
        return JSONResponse({"detail": "Message is empty."}, status_code=400)
    context_attachments = combined_assignment_context(payload.assignment_memory, payload.attachments)
    try:
        attachment_prompt(text, context_attachments)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=413)
    record_public_event("chat_request")
    messages = [] if DEVICE_MEMORY_ENABLED else load_chat(chat_id)
    model_history = normalized_client_history(payload.history) if DEVICE_MEMORY_ENABLED else messages
    started = time.perf_counter()
    answer = call_jarvis_reply(text, chat_id, payload.mode, model_history, context_attachments)
    if not DEVICE_MEMORY_ENABLED:
        messages.append(user_message_record(text, payload.mode, context_attachments))
        messages.append(assistant_message_record(answer, payload.mode))
        save_chat(chat_id, messages)
    record_public_event("chat_response")
    return JSONResponse(
        {
            "answer": answer,
            "mode": payload.mode,
            "memory": "device" if DEVICE_MEMORY_ENABLED else "server",
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    )


@app.post("/api/chat/{chat_id}/stream")
def api_chat_stream(chat_id: str, payload: ChatRequest, request: Request) -> Response:
    device_id = device_id_from_request(request)
    chat_id = canonical_id(chat_id) or ""
    if not chat_id or not STORE.owns_chat(device_id, chat_id):
        return JSONResponse({"detail": "Conversation not found."}, status_code=404)
    limited = rate_limit_response(request, device_id)
    if limited:
        return limited

    text = clean_text(payload.message)
    if not text:
        return JSONResponse({"detail": "Message is empty."}, status_code=400)
    context_attachments = combined_assignment_context(payload.assignment_memory, payload.attachments)
    try:
        prompt = attachment_prompt(text, context_attachments)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=413)
    record_public_event("chat_request")
    messages = [] if DEVICE_MEMORY_ENABLED else load_chat(chat_id)
    model_history = normalized_client_history(payload.history) if DEVICE_MEMORY_ENABLED else messages
    if not DEVICE_MEMORY_ENABLED:
        messages.append(user_message_record(text, payload.mode, context_attachments))
        save_chat(chat_id, messages)
    started = time.perf_counter()

    def stream_events():
        answer_parts: list[str] = []
        yield ndjson_event("start", mode=payload.mode)
        try:
            for chunk in cloud_generate_stream(prompt, history=model_history, mode=payload.mode):
                if not chunk:
                    continue
                answer_parts.append(chunk)
                yield ndjson_event("delta", content=chunk)
            answer = clean_text("".join(answer_parts)) or "Jarvis did not return a response. Please try again."
            if not DEVICE_MEMORY_ENABLED:
                messages.append(assistant_message_record(answer, payload.mode))
                save_chat(chat_id, messages)
            record_public_event("chat_response")
            yield ndjson_event(
                "done",
                answer=answer,
                html=render_content(answer),
                mode=payload.mode,
                memory="device" if DEVICE_MEMORY_ENABLED else "server",
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
        except GeneratorExit:
            raise
        except Exception:
            LOGGER.exception("Streaming response failed")
            record_public_event("chat_error")
            fallback = clean_text("".join(answer_parts)) or "Jarvis could not finish that response."
            if not DEVICE_MEMORY_ENABLED:
                messages.append(assistant_message_record(fallback, payload.mode))
                save_chat(chat_id, messages)
            yield ndjson_event(
                "done",
                answer=fallback,
                html=render_content(fallback),
                mode=payload.mode,
                memory="device" if DEVICE_MEMORY_ENABLED else "server",
                recovered=True,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )

    return StreamingResponse(
        stream_events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/pet/{chat_id}")
def api_pet_history(chat_id: str, request: Request) -> JSONResponse:
    device_id = device_id_from_request(request)
    chat_id = canonical_id(chat_id) or ""
    if not chat_id or not STORE.owns_chat(device_id, chat_id):
        return JSONResponse({"history": []})
    return JSONResponse({"history": load_pet_chat(chat_id)})


@app.post("/api/pet/{chat_id}")
def api_pet_chat(chat_id: str, payload: ChatRequest, request: Request) -> JSONResponse:
    device_id = device_id_from_request(request)
    chat_id = canonical_id(chat_id) or ""
    if not chat_id or not STORE.owns_chat(device_id, chat_id):
        return JSONResponse({"answer": "This conversation is no longer available."}, status_code=404)
    limited = rate_limit_response(request, device_id)
    if limited:
        return limited
    text = clean_text(payload.message)
    if not text:
        return JSONResponse({"answer": "Say something to me first."})
    record_public_event("pet_request")
    return JSONResponse({"answer": pet_reply(text, chat_id)})


@app.delete("/api/chats/{chat_id}")
def api_delete_chat(chat_id: str, request: Request) -> JSONResponse:
    device_id = device_id_from_request(request)
    chat_id = canonical_id(chat_id) or ""
    if not chat_id or not STORE.delete_chat(device_id, chat_id):
        return JSONResponse({"detail": "Conversation not found."}, status_code=404)
    return JSONResponse({"deleted": True})


@app.get("/api/device/export")
def api_export_device(request: Request) -> JSONResponse:
    device_id = device_id_from_request(request)
    profile = current_user(request)
    chats = [
        {
            "id": chat_id,
            "title": chat_title(chat_id),
            "messages": load_chat(chat_id),
            "companion_messages": load_pet_chat(chat_id),
        }
        for chat_id in get_device_chats(device_id)
        if STORE.owns_chat(device_id, chat_id)
    ]
    response = JSONResponse(
        {
            "exported_at": now_stamp(),
            "app": APP_TITLE,
            "version": APP_VERSION,
            "ownership": "account" if profile else "browser",
            "account": (
                {"name": str(profile.get("name", "")), "email": str(profile.get("email", ""))}
                if profile
                else None
            ),
            "memory_location": "device" if DEVICE_MEMORY_ENABLED else "server",
            "chats": chats,
        }
    )
    response.headers["Content-Disposition"] = 'attachment; filename="jarvis-account-data.json"'
    return response


@app.post("/api/chats/{chat_id}/essay-check")
def api_essay_self_check(chat_id: str, payload: EssaySelfCheckRequest, request: Request) -> JSONResponse:
    device_id = device_id_from_request(request)
    chat_id = canonical_id(chat_id) or ""
    if not chat_id or not STORE.owns_chat(device_id, chat_id):
        return JSONResponse({"detail": "Conversation not found."}, status_code=404)
    limited = rate_limit_response(request, device_id)
    if limited:
        return limited

    draft = clean_text(payload.draft)
    if not draft:
        return JSONResponse({"detail": "Add a draft before running the rubric check."}, status_code=400)
    if payload.rubric is None or not clean_text(payload.rubric.text):
        return JSONResponse({"detail": "Add the rubric before running the rubric check."}, status_code=400)

    attachments: list[AttachmentContext] = []
    assignment = clean_text(payload.assignment)
    if assignment:
        attachments.append(AttachmentContext(name="Assignment brief.txt", text=assignment))
    attachments.append(
        AttachmentContext(
            name=f"Rubric - {safe_upload_name(payload.rubric.name)}",
            media_type=payload.rubric.media_type,
            text=payload.rubric.text,
        )
    )
    if payload.feedback is not None and clean_text(payload.feedback.text):
        attachments.append(
            AttachmentContext(
                name=f"Teacher feedback - {safe_upload_name(payload.feedback.name)}",
                media_type=payload.feedback.media_type,
                text=payload.feedback.text,
            )
        )
    attachments.append(AttachmentContext(name="Current essay draft.txt", text=draft))
    prompt = (
        "Self-check the attached essay draft against the attached rubric. Work criterion by criterion. For each "
        "criterion, label it Met, Partly met, or Not yet met; point to specific evidence from the draft; explain the "
        "judgment in student-friendly language; and give one concrete revision action. Apply any attached teacher "
        "feedback. Finish with the three highest-priority revisions. Do not rewrite the essay or invent a score that "
        "the rubric does not define."
    )
    if clean_text(payload.title):
        prompt += f" The assignment title is: {clean_text(payload.title)}."
    try:
        attachment_prompt(prompt, attachments)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=413)
    started = time.perf_counter()
    answer = call_jarvis_reply(prompt, chat_id, "essay", [], attachments)
    record_public_event("essay_check")
    return JSONResponse(
        {
            "answer": answer,
            "checked_at": now_stamp(),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    )


@app.post("/api/chats/{chat_id}/study-tools")
def api_study_tools(chat_id: str, payload: StudyToolRequest, request: Request) -> JSONResponse:
    device_id = device_id_from_request(request)
    chat_id = canonical_id(chat_id) or ""
    if not chat_id or not STORE.owns_chat(device_id, chat_id):
        return JSONResponse({"detail": "Conversation not found."}, status_code=404)
    limited = rate_limit_response(request, device_id)
    if limited:
        return limited

    prompt = study_tool_prompt(payload)
    started = time.perf_counter()
    answer = cloud_generate(
        prompt,
        history=[],
        mode="study",
        system_prompt=(
            "Generate accurate, age-appropriate study material from the learner context. Follow the requested JSON "
            "schema exactly, do not wrap it in Markdown, and never claim facts not supported by the supplied notes "
            "when notes are provided."
        ),
    )
    if not clean_text(answer):
        return JSONResponse(
            {"detail": "The study generator is unavailable right now. Check the configured AI provider."},
            status_code=503,
        )
    parsed = parsed_json_object(answer)
    if parsed is None:
        return JSONResponse(
            {"detail": "Jarvis could not format that study material. Please try generating it again."},
            status_code=502,
        )
    try:
        result = normalized_study_result(payload.action, parsed)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)
    record_public_event(f"study_{payload.action}")
    return JSONResponse(
        {
            "result": result,
            "generated_at": now_stamp(),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    )


@app.delete("/api/device")
def api_delete_device(request: Request) -> JSONResponse:
    device_id = device_id_from_request(request)
    account_deleted = current_user(request) is not None
    deleted = STORE.delete_device(device_id)
    response = JSONResponse({"deleted": True, "account_deleted": account_deleted, "chats_deleted": deleted})
    response.delete_cookie(AUTH_COOKIE)
    response.delete_cookie(OAUTH_STATE_COOKIE)
    response.delete_cookie(DEVICE_COOKIE)
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8010")))
