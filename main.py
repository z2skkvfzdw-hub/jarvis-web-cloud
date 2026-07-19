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
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
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


APP_TITLE = "Jarvis.Ai"
APP_VERSION = "1.6.0"
CACHE_VERSION = "jarvis-ai-1-6-0"
DATA_DIR = Path(os.environ.get("JARVIS_CLOUD_DATA_DIR", "cloud_chats"))
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
DATA_DIR.mkdir(exist_ok=True)
ASSETS_DIR.mkdir(exist_ok=True)

DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODEL = os.environ.get("JARVIS_CLOUD_MODEL", "").strip()
ChatMode = Literal["chat", "study", "essay", "math", "science", "code", "research", "create", "engineer"]
CHAT_MODES: tuple[ChatMode, ...] = ("chat", "study", "essay", "math", "science", "code", "research", "create", "engineer")
MODE_INSTRUCTIONS: dict[ChatMode, str] = {
    "chat": (
        " Prioritize natural back-and-forth conversation. Be personable, direct, and curious without turning every "
        "reply into a checklist. Match the user's level of detail and keep continuity with the conversation."
    ),
    "study": (
        " Act as a patient tutor. Explain ideas in clear stages, adapt to the learner's apparent level, use a small "
        "example when useful, and check understanding without withholding the answer. For practice requests, guide "
        "the learner before revealing a complete solution."
    ),
    "essay": (
        " Act as an essay coach. Use the assignment, rubric, notes, and teacher feedback supplied by the learner. "
        "Help brainstorm, outline, draft, revise, and self-check while preserving the learner's voice."
    ),
    "math": (
        " Act as a precise maths tutor. Show the method, important working, final answer, and a quick check."
    ),
    "science": (
        " Act as a careful science tutor. Explain mechanisms, evidence, units, assumptions, and safety limits."
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
DEVICE_COOKIE_MAX_AGE = max(3600, int(os.environ.get("JARVIS_SESSION_MAX_AGE", str(60 * 60 * 24 * 30))))
SESSION_SECRET_CONFIGURED = bool(os.environ.get("JARVIS_SESSION_SECRET", "").strip())
SESSION_SECRET = (
    os.environ.get("JARVIS_SESSION_SECRET", "").strip() or secrets.token_urlsafe(48)
).encode("utf-8")
PUBLIC_ORIGIN = os.environ.get("JARVIS_PUBLIC_ORIGIN", "").strip().rstrip("/")
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
    elif request.url.path.startswith(("/chat/", "/api/")) or request.url.path == "/":
        response.headers["Cache-Control"] = "private, no-store"
    return response


class ChatHistoryItem(BaseModel):
    role: str = Field(max_length=20)
    content: str = Field(max_length=MAX_MESSAGE_CHARS)


class AttachmentContext(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    media_type: str = Field(default="text/plain", max_length=120)
    text: str = Field(min_length=1, max_length=MAX_ATTACHMENT_CHARS)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    mode: ChatMode = "chat"
    history: list[ChatHistoryItem] = Field(default_factory=list, max_length=MAX_HISTORY_MESSAGES)
    attachments: list[AttachmentContext] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


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


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def device_id_from_request(request: Request) -> str:
    current = request.cookies.get(DEVICE_COOKIE, "").strip()
    return verified_device_cookie(current) or str(uuid.uuid4())


def set_device_cookie(response: Response, request: Request, device_id: str) -> None:
    secure = request_origin(request).startswith("https://")
    response.set_cookie(
        DEVICE_COOKIE,
        signed_device_cookie(device_id),
        max_age=DEVICE_COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="strict",
    )


def client_rate_key(request: Request, device_id: str) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",", 1)[0]
    client = forwarded.strip() or (request.client.host if request.client else "unknown")
    return hashlib.sha256(f"{client}:{device_id}".encode("utf-8")).hexdigest()


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
    configured = [provider for provider in ("nvidia", "groq", "openrouter") if cloud_key(provider)]
    requested = [
        item.strip().lower()
        for item in os.environ.get("JARVIS_PROVIDER_CHAIN", "").split(",")
        if item.strip()
    ]
    preferred = os.environ.get("JARVIS_CLOUD_PROVIDER", "").strip().lower()
    order = requested or ([preferred] if preferred else [])
    order.extend(("nvidia", "groq", "openrouter"))
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
                return answer
        except Exception as exc:
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
    for item in attachments[:MAX_ATTACHMENTS]:
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
            f'<div class="chat-row{active}" data-chat-row data-title="{safe_title.casefold()}">'
            f'<a class="chat-title" href="/chat/{chat_id}" title="{safe_title}">{safe_title}</a>'
            f'<button class="chat-delete" type="button" data-delete-chat="{chat_id}" title="Delete chat" aria-label="Delete chat">&times;</button>'
            "</div>"
        )
    return "\n".join(rows)


def page_html(chat_id: str, device_id: str, csp_nonce: str) -> str:
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
    <meta name="apple-mobile-web-app-title" content="Jarvis">
    <link rel="manifest" href="/manifest.json">
    <link rel="icon" href="/icon.svg" type="image/svg+xml">
    <script nonce="{html.escape(csp_nonce)}" src="https://unpkg.com/lucide@latest"></script>
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
        .privacy-state {{ color: #8fa9b8; font-size: 11px; }}
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
        }}
        .chat-row {{
            min-height: 38px;
            display: flex;
            align-items: center;
            border-radius: 11px;
            padding: 0 4px 0 12px;
            margin: 1px 0;
        }}
        .chat-row.active {{ background: #1f1f1f; }}
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
        .chat-form::before {{
            content: "+";
            width: 32px;
            height: 32px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: #d7d7d7;
            font-size: 28px;
            font-weight: 300;
            flex: 0 0 auto;
        }}
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
        .chat-form::before {{ content: "+"; color: var(--accent); font-size: 23px; }}
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
                content: "Jarvis.Ai";
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
        @media (max-width:1240px) {{ .pet-panel {{ right: 16px; }} }}
        @media (max-width:760px) {{ .pet-toggle {{ width: 34px; height: 34px; flex-basis: 34px; }} .pet-panel {{ top: 58px; right: 10px; left: 10px; width: auto; height: min(520px, calc(100vh - 76px)); }} }}
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
                <span class="privacy-state">Anonymous</span>
            </div>
            <nav class="sidebar-nav" aria-label="Jarvis navigation">
                <form action="/new" method="post"><button class="nav-item nav-primary" type="submit"><span class="nav-icon">+</span><span>New chat</span></button></form>
                <button class="nav-item" id="chat-search-toggle" type="button"><span class="nav-icon">&#8981;</span><span>Search chats</span></button>
                <a class="nav-item" href="/api/device/export" id="export-device-data"><span class="nav-icon">&#8681;</span><span>Export my data</span></a>
                <a class="nav-item" href="/privacy"><span class="nav-icon">i</span><span>Privacy</span></a>
                <button class="nav-item danger" id="delete-device-data" type="button"><span class="nav-icon">&#215;</span><span>Delete my data</span></button>
            </nav>
            <label class="chat-search" id="chat-search-wrap" hidden>
                <span class="sr-only">Search recent chats</span>
                <input id="chat-search" type="search" placeholder="Search recent chats" autocomplete="off">
            </label>
            <div class="recents-header"><span>Recents</span></div>
            {sidebar}
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
                    <textarea id="message-input" name="message" maxlength="{MAX_MESSAGE_CHARS}" placeholder="Message Jarvis..." autocomplete="off" autofocus></textarea>
                    <button class="send-button" id="send-button" type="submit">Send</button>
                </form>
                {suggestions}
                <div class="hint">Enter sends. Shift+Enter adds a new line.</div>
            </section>
        </main>
        {workspace_panel}
    </div>
    <script nonce="{html.escape(csp_nonce)}">
        const chatId = {json.dumps(chat_id)};
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
        const petToggle = document.getElementById("pet-toggle");
        const petPanel = document.getElementById("pet-panel");
        const petClose = document.getElementById("pet-close");
        const petMessages = document.getElementById("pet-messages");
        const petForm = document.getElementById("pet-form");
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
        function rememberChatTitle(seedText = "") {{
            if (!deviceMemoryEnabled) return;
            const items = getChatMemory();
            const title = firstUserLine(items) || String(seedText || "New Chat").slice(0, 48);
            const index = getChatIndex().filter(item => item.id !== chatId);
            index.push({{ id: chatId, title, updatedAt: Date.now() }});
            saveChatIndex(index);
            document.querySelectorAll(`[data-chat-row]`).forEach(row => {{
                const link = row.querySelector(`[href="/chat/${{chatId}}"]`);
                if (link) {{
                    link.textContent = title;
                    link.title = title;
                    row.dataset.title = title.toLowerCase();
                }}
            }});
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
                messages: getChatMemory(item.id)
            }}));
            if (!index.some(item => item.id === chatId)) {{
                chats.push({{ id: chatId, title: firstUserLine(getChatMemory()), messages: getChatMemory() }});
            }}
            const blob = new Blob([JSON.stringify({{ exported_at: new Date().toISOString(), memory: "device", chats }}, null, 2)], {{ type: "application/json" }});
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
            if (/^(engineer|engineering|cad|prototype)\s*:/.test(lowered)) return true;
            return parts.some(part => lowered.includes(part)) && (actions.some(action => lowered.includes(action)) || lowered.split(/\s+/).length >= 4);
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
            const cleaned = String(text || "").replace(/^(engineer|engineering|cad|prototype)\s*:\s*/i, "").trim();
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
        document.querySelectorAll("[data-delete-chat]").forEach(item => item.addEventListener("click", async event => {{
            event.preventDefault();
            const targetId = item.dataset.deleteChat;
            if (!targetId || !window.confirm("Delete this conversation permanently?")) return;
            if (deviceMemoryEnabled) {{
                try {{
                    localStorage.removeItem(`jarvis_chat_memory_${{targetId}}_v1`);
                    saveChatIndex(getChatIndex().filter(chat => chat.id !== targetId));
                }} catch (error) {{}}
            }}
            const response = await fetch(`/api/chats/${{targetId}}`, {{ method: "DELETE" }});
            if (!response.ok) {{ window.alert("That conversation could not be deleted."); return; }}
            if (targetId === chatId) window.location.assign("/");
            else item.closest("[data-chat-row]")?.remove();
        }}));
        if (exportDeviceData) exportDeviceData.addEventListener("click", exportLocalMemory);
        if (deleteDeviceData) deleteDeviceData.addEventListener("click", async () => {{
            if (!window.confirm("Delete every Jarvis and MJ conversation saved for this browser? This cannot be undone.")) return;
            if (deviceMemoryEnabled) {{
                try {{
                    getChatIndex().forEach(item => localStorage.removeItem(`jarvis_chat_memory_${{item.id}}_v1`));
                    localStorage.removeItem(chatMemoryKey);
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
        async function sendMessage() {{
            const text = input.value.trim();
            if (!text) return;
            const requestHistory = deviceMemoryEnabled ? getChatMemory().slice(-{MAX_HISTORY_MESSAGES}) : [];
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
                    body: JSON.stringify({{ message: text, mode: activeMode, history: requestHistory }})
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
        updateActivityDashboard();
        updateEngineeringDashboard();
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
            "short_name": "Jarvis",
            "description": "Cloud-safe Jarvis assistant.",
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
        <h1>Jarvis.AI is offline.</h1>
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


@app.get("/privacy", response_class=HTMLResponse)
def privacy() -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Privacy | {html.escape(APP_TITLE)}</title>
    <style>
        body {{ margin: 0; background: #071018; color: #eaf6ff; font: 16px/1.6 system-ui, sans-serif; }}
        main {{ width: min(720px, calc(100% - 40px)); margin: 56px auto; }}
        h1, h2 {{ line-height: 1.2; }}
        h2 {{ margin-top: 30px; font-size: 19px; }}
        p, li {{ color: #b9cad5; }}
        a {{ color: #70e4dc; }}
        code {{ color: #fff; }}
    </style>
</head>
<body><main>
    <p><a href="/">Back to Jarvis</a></p>
    <h1>Privacy</h1>
    <p>Jarvis is an anonymous public chat service. It does not require an account. Main chat memory is stored in this browser so this device can reopen and continue its conversations.</p>
    <h2>What is stored</h2>
    <p>Main chat text and chat titles are stored in this browser's local storage. The server keeps lightweight conversation identifiers for routing and may keep companion-chat text. Jarvis does not request your camera, microphone, location, contacts, or local computer files.</p>
    <h2>AI requests</h2>
    <p>When you send a message, the recent browser-stored conversation context needed for the answer is sent to the configured AI provider. That context is not used by Jarvis as permanent server memory.</p>
    <h2>AI providers</h2>
    <p>Messages sent for an AI response are forwarded to the configured cloud AI provider. Do not enter passwords, payment details, medical records, or other information you would not want processed by that provider.</p>
    <h2>Your controls</h2>
    <p>Use <strong>Export my data</strong> to download this browser's stored conversations. Use <strong>Delete my data</strong> to remove them from this browser and clear server routing data for this browser.</p>
    <h2>Device control</h2>
    <p>This public version cannot open apps, read files, or control the computer running your private Jarvis installation.</p>
</main></body></html>"""
    )


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


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> RedirectResponse:
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
    nonce = secrets.token_urlsafe(18)
    response = HTMLResponse(page_html(chat_id, device_id, nonce))
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' blob:; "
        f"script-src 'self' 'nonce-{nonce}' https://unpkg.com; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data: https:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; upgrade-insecure-requests"
    )
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
    try:
        attachment_prompt(text, payload.attachments)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=413)
    messages = [] if DEVICE_MEMORY_ENABLED else load_chat(chat_id)
    model_history = normalized_client_history(payload.history) if DEVICE_MEMORY_ENABLED else messages
    started = time.perf_counter()
    answer = call_jarvis_reply(text, chat_id, payload.mode, model_history, payload.attachments)
    if not DEVICE_MEMORY_ENABLED:
        messages.append(user_message_record(text, payload.mode, payload.attachments))
        messages.append(assistant_message_record(answer, payload.mode))
        save_chat(chat_id, messages)
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
    try:
        prompt = attachment_prompt(text, payload.attachments)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=413)
    messages = [] if DEVICE_MEMORY_ENABLED else load_chat(chat_id)
    model_history = normalized_client_history(payload.history) if DEVICE_MEMORY_ENABLED else messages
    if not DEVICE_MEMORY_ENABLED:
        messages.append(user_message_record(text, payload.mode, payload.attachments))
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
    response = JSONResponse({"exported_at": now_stamp(), "chats": chats})
    response.headers["Content-Disposition"] = 'attachment; filename="jarvis-data-export.json"'
    return response


@app.delete("/api/device")
def api_delete_device(request: Request) -> JSONResponse:
    device_id = device_id_from_request(request)
    deleted = STORE.delete_device(device_id)
    response = JSONResponse({"deleted": True, "chats_deleted": deleted})
    response.delete_cookie(DEVICE_COOKIE)
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8010")))
