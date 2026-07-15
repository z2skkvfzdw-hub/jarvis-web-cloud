from __future__ import annotations

import base64
import html
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

try:
    from ddgs import DDGS
except Exception:
    DDGS = None


APP_TITLE = "Jarvis.Ai"
APP_VERSION = "1.3.1"
CACHE_VERSION = "jarvis-ai-1-3-1"
DATA_DIR = Path(os.environ.get("JARVIS_CLOUD_DATA_DIR", "cloud_chats"))
DATA_DIR.mkdir(exist_ok=True)

DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODEL = os.environ.get("JARVIS_CLOUD_MODEL", "").strip()
MAX_HISTORY_MESSAGES = int(os.environ.get("JARVIS_CLOUD_CONTEXT_MESSAGES", "10"))

app = FastAPI(title=APP_TITLE, version=APP_VERSION)


@app.middleware("http")
async def add_web_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=()")
    if request.url.path == "/sw.js":
        response.headers["Cache-Control"] = "no-cache"
    elif request.url.path in {"/manifest.json", "/icon.svg", "/offline"}:
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


class ChatRequest(BaseModel):
    message: str


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_text(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def device_id_from_request(request: Request) -> str:
    current = request.cookies.get("jarvis_cloud_device", "").strip()
    if re.fullmatch(r"[a-f0-9-]{20,80}", current, flags=re.IGNORECASE):
        return current
    return str(uuid.uuid4())


def device_index_path(device_id: str) -> Path:
    return DATA_DIR / f"device_{device_id}.json"


def chat_path(chat_id: str) -> Path:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", chat_id)
    return DATA_DIR / f"chat_{safe_id}.json"


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_device_chats(device_id: str) -> list[str]:
    data = read_json(device_index_path(device_id), [])
    return [str(item) for item in data if isinstance(item, str)]


def save_device_chat(device_id: str, chat_id: str) -> None:
    chats = get_device_chats(device_id)
    if chat_id not in chats:
        chats.append(chat_id)
        write_json(device_index_path(device_id), chats[-80:])


def create_chat(device_id: str) -> str:
    chat_id = str(uuid.uuid4())
    save_chat(chat_id, [])
    save_device_chat(device_id, chat_id)
    return chat_id


def load_chat(chat_id: str) -> list[dict[str, str]]:
    data = read_json(chat_path(chat_id), [])
    return data if isinstance(data, list) else []


def save_chat(chat_id: str, messages: list[dict[str, str]]) -> None:
    write_json(chat_path(chat_id), messages[-200:])


def chat_title(chat_id: str) -> str:
    for item in load_chat(chat_id):
        if item.get("role") == "user" and item.get("content", "").strip():
            title = item["content"].strip()
            return title[:36] + ("..." if len(title) > 36 else "")
    return "New Chat"


def list_chats(device_id: str) -> list[tuple[str, str]]:
    chats = []
    for chat_id in get_device_chats(device_id):
        if chat_path(chat_id).exists():
            chats.append((chat_id, chat_title(chat_id)))
    return chats


def cloud_key(provider: str) -> str:
    return os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("JARVIS_OPENROUTER_API_KEY", "")


def available_cloud_providers() -> list[str]:
    return ["openrouter"] if cloud_key("openrouter") else []


def provider_model(provider: str) -> str:
    return os.environ.get("JARVIS_OPENROUTER_MODEL", "").strip() or DEFAULT_MODEL or "tencent/hy3:free"


def cloud_generate(prompt: str, history: list[dict[str, str]] | None = None) -> str | None:
    providers = available_cloud_providers()
    if not providers:
        return None

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You are Jarvis.AI, a public cloud version of Jarvis. "
                "You are not running on the owner's laptop, so you cannot open local apps, read local files, "
                "control Windows, use local Ollama, or access private owner memory. "
                "You can reason, plan, explain, tutor, brainstorm, summarize, write, review code, analyse designs, "
                "and use web search results when provided. Read the complete conversation and infer reasonable intent. "
                "Begin with the useful answer, recommendation, or next action. For complex work, privately check assumptions, "
                "constraints, alternatives, risks, and test criteria, then present only the conclusion and useful reasoning. "
                "Never reveal chain-of-thought. Sound calm, intelligent, candid, and natural. Avoid canned acknowledgements "
                "and unnecessary follow-up questions. Do not pretend to have device control."
            ),
        }
    ]
    if history:
        for item in history[-MAX_HISTORY_MESSAGES:]:
            role = "assistant" if item.get("role") == "Jarvis" else "user"
            content = clean_text(item.get("content", ""))
            if content:
                messages.append({"role": role, "content": content[:2500]})
    messages.append({"role": "user", "content": prompt})

    for provider in providers:
        model = provider_model(provider)
        key = cloud_key(provider)
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": os.environ.get("JARVIS_OPENROUTER_REFERER", "https://jarvis.web"),
                    "X-OpenRouter-Title": APP_TITLE,
                },
                json={"model": model, "messages": messages, "reasoning": {"enabled": True}, "temperature": 0.5, "max_tokens": 1200},
                timeout=35,
            )
            response.raise_for_status()
            data = response.json()
            answer = clean_text(data["choices"][0]["message"]["content"])
            if answer:
                return answer
        except Exception as exc:
            print(f"[cloud brain warning] {provider}: {exc}")
            continue
    return None


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


def jarvis_reply(user_text: str, chat_id: str) -> str:
    text = clean_text(user_text)
    lowered = text.lower()
    history = load_chat(chat_id)

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
        )
        return brain or search_results

    if lowered in {"hello", "hi", "hey", "yo", "sup", "what up", "whats up", "what's up", "hey what up"}:
        return "Hey. What would you like to talk about?"

    if any(phrase in lowered for phrase in ("open spotify", "open app", "close app", "run command", "read my file", "control my computer")):
        return (
            "That needs desktop Jarvis running on the owner's computer. "
            "This cloud version can chat, search, tutor, and brainstorm, but it cannot control a laptop that is off."
        )

    reply = cloud_generate(text, history)
    if reply:
        return reply

    return fallback_conversation(text, history)


def render_content(content: str) -> str:
    escaped = html.escape(content)
    escaped = re.sub(r"\[\[JARVIS_IMAGE_GALLERY:[A-Za-z0-9_\-=]+\]\]", "", escaped).strip()
    gallery = render_gallery(content)
    return escaped + gallery


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
    html_out = ""
    for item in load_chat(chat_id):
        role = item.get("role", "")
        content = render_content(item.get("content", ""))
        if role == "user":
            html_out += f'<article class="message user"><div class="bubble">{content}</div></article>'
        elif role == "Jarvis":
            html_out += f'<article class="message jarvis"><div class="bubble">{content}</div></article>'
    return html_out


def build_sidebar(current_chat_id: str, device_id: str) -> str:
    rows = []
    for chat_id, title in reversed(list_chats(device_id)[-50:]):
        active = " active" if chat_id == current_chat_id else ""
        rows.append(
            f'<div class="chat-row{active}">'
            f'<a class="chat-title" href="/chat/{chat_id}" title="{html.escape(title)}">{html.escape(title)}</a>'
            "</div>"
        )
    return "\n".join(rows)


def page_html(chat_id: str, device_id: str) -> str:
    history_html = build_chat_history(chat_id)
    empty_state = ""
    if not load_chat(chat_id):
        empty_state = """
        <div class="empty-state" id="empty-state">
            <section class="hero-shell" aria-label="Jarvis command centre">
                <div class="hero-copy">
                    <div class="system-kicker"><span></span> JARVIS WEB INTERFACE ONLINE</div>
                    <h1>What shall we build?</h1>
                    <p>Describe a site, study task, research problem, image idea, or plan. Jarvis will route it through the cloud-safe workspace.</p>
                </div>
                <div class="hero-core" aria-hidden="true">
                    <div class="core-ring ring-one"></div>
                    <div class="core-ring ring-two"></div>
                    <div class="core-ring ring-three"></div>
                    <div class="core-hex">J</div>
                </div>
            </section>
            <section class="activity-ledger" aria-label="Jarvis activity ledger">
                <div class="ledger-card primary">
                    <span>Today</span>
                    <strong data-activity-metric="credits">0.00 JC</strong>
                    <small>Jarvis credits, not cash</small>
                </div>
                <div class="ledger-card">
                    <span>Runs</span>
                    <strong data-activity-metric="runs">0</strong>
                    <small>responses completed</small>
                </div>
                <div class="ledger-card">
                    <span>Average</span>
                    <strong data-activity-metric="average">--</strong>
                    <small>thinking time</small>
                </div>
                <div class="ledger-card activity">
                    <span>Activity</span>
                    <div class="mini-bars" data-activity-bars></div>
                    <small data-activity-metric="last">waiting</small>
                </div>
            </section>
            <section class="launch-grid" aria-label="Quick launch prompts">
                <button data-prompt="Build a modern web page for " type="button"><span>Build</span><strong>Website</strong></button>
                <button data-prompt="Research and summarise " type="button"><span>Research</span><strong>Brief</strong></button>
                <button data-prompt="Create study notes for " type="button"><span>Study</span><strong>Lesson</strong></button>
                <button data-prompt="Show image ideas for " type="button"><span>Visual</span><strong>Ideas</strong></button>
                <button data-prompt="Plan this project: " type="button"><span>Plan</span><strong>Project</strong></button>
                <button data-prompt="Explain this simply: " type="button"><span>Explain</span><strong>Concept</strong></button>
            </section>
        </div>
        """
    suggestions = "" if load_chat(chat_id) else """
    <div class="suggestions composer-suggestions" id="composer-suggestions">
        <button class="suggestion" data-prompt="Find " type="button"><span data-lucide="search"></span>Find</button>
        <button class="suggestion" data-prompt="Think through " type="button"><span data-lucide="brain-circuit"></span>Think</button>
        <button class="suggestion" data-prompt="Show visual ideas for " type="button"><span data-lucide="image"></span>Visuals</button>
        <button class="suggestion" data-prompt="Help me study " type="button"><span data-lucide="book-open"></span>Study</button>
    </div>
    """
    sidebar = build_sidebar(chat_id, device_id)
    providers = available_cloud_providers()
    brain_label = provider_model(providers[0]) if providers else "Not configured"
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
                        <span id="core-state">READY</span>
                        <strong id="latency-readout">--</strong>
                    </div>
                </section>
                <section class="workspace-section earnings-card">
                    <div class="section-heading"><span class="eyebrow">AI time ledger</span><span data-activity-metric="live">IDLE</span></div>
                    <strong class="credit-amount" data-activity-metric="credits">0.00 JC</strong>
                    <p>Local session activity. No real money, no ads, no payout claim.</p>
                    <div class="mini-bars wide" data-activity-bars></div>
                    <div class="earnings-stats">
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
                    <div class="section-heading"><span class="eyebrow">Prompt systems</span><span aria-hidden="true">06</span></div>
                    <p class="workspace-empty">Build, research, write, study, visualise, and explain.</p>
                </section>
                <section class="workspace-section">
                    <span class="eyebrow">Launch sequence</span>
                    <div class="workspace-actions">
                        <button data-prompt="Build a web page for " type="button">Build interface</button>
                        <button data-prompt="Create a research brief about " type="button">Research brief</button>
                        <button data-prompt="Make a study plan for " type="button">Study plan</button>
                        <button data-prompt="Generate visual ideas for " type="button">Visual ideas</button>
                    </div>
                </section>
            </div>
        </aside>
    """
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
    <script src="https://unpkg.com/lucide@latest"></script>
    <style>
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
        .nav-item {{
            height: 44px;
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 0 12px;
            border-radius: 12px;
            color: #ececec;
            text-decoration: none;
            font-size: 15px;
        }}
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
            border-radius: 11px;
            padding: 0 12px;
            margin: 1px 0;
        }}
        .chat-row.active {{ background: #1f1f1f; }}
        .chat-title {{
            display: block;
            color: #e8e8e8;
            font-size: 14px;
            line-height: 38px;
            text-decoration: none;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
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
        .workspace-section {{ padding: 17px 18px; border-bottom: 1px solid var(--line); }}
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
        .credit-amount {{
            display: block;
            margin: 9px 0 6px;
            color: #f4fbff;
            font-size: 30px;
            line-height: 1;
            letter-spacing: 0;
        }}
        .mini-bars.wide {{ height: 42px; margin: 13px 0 8px; }}
        .earnings-stats {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 8px;
            margin-top: 10px;
        }}
        .earnings-stats div {{
            padding: 8px;
            border: 1px solid rgba(36, 74, 104, 0.62);
            border-radius: 7px;
            background: rgba(2, 8, 14, 0.55);
        }}
        .earnings-stats span {{ display: block; color: #708084; font-size: 10px; }}
        .earnings-stats strong {{ display: block; margin-top: 4px; color: #d9e5e5; font-size: 11px; overflow-wrap: anywhere; }}
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
                <span class="item-menu global-menu" aria-hidden="true"><span class="brand-menu">&#8942;</span></span>
            </div>
            <nav class="sidebar-nav" aria-label="Jarvis navigation">
                <a class="nav-item nav-primary" href="/new"><span class="nav-icon">&#9998;</span><span>New chat</span></a>
                <a class="nav-item" href="/"><span class="nav-icon">&#8981;</span><span>Search chats</span></a>
                <a class="nav-item" href="/"><span class="nav-icon">&#9636;</span><span>Library</span></a>
                <a class="nav-item" href="/"><span class="nav-icon">&#128193;</span><span>Projects</span></a>
                <a class="nav-item" href="/"><span class="nav-icon">&#8759;</span><span>Apps</span></a>
            </nav>
            <div class="recents-header"><span>Recents</span></div>
            {sidebar}
        </aside>
        <main class="main">
            <header class="topbar">
                <div class="title">JARVIS / CONVERSATION CORE</div>
                <div class="topbar-actions">
                    <a class="mobile-new-chat" href="/new" title="New chat" aria-label="New chat">+</a>
                    <button class="icon-button workspace-open" id="workspace-open" type="button" title="Open prototype workspace" aria-label="Open prototype workspace"><span aria-hidden="true">&lsaquo;</span></button>
                    <div class="mode">Secure &amp; Safe</div>
                </div>
            </header>
            <section class="chat" id="chat">
                <div class="chat-inner" id="messages">
                    {empty_state}
                    {history_html}
                </div>
            </section>
            <section class="composer">
                <form class="chat-form" id="chat-form">
                    <textarea id="message-input" name="message" placeholder="Message Jarvis..." autocomplete="off" autofocus></textarea>
                    <button class="send-button" id="send-button" type="submit">Send</button>
                </form>
                {suggestions}
                <div class="hint">Enter sends. Shift+Enter adds a new line.</div>
            </section>
        </main>
        {workspace_panel}
    </div>
    <script>
        const chatId = {json.dumps(chat_id)};
        const chat = document.getElementById("chat");
        const messages = document.getElementById("messages");
        const form = document.getElementById("chat-form");
        const input = document.getElementById("message-input");
        const button = document.getElementById("send-button");
        const emptyState = document.getElementById("empty-state");
        const suggestions = document.getElementById("composer-suggestions");
        const workspaceClose = document.getElementById("workspace-close");
        const workspaceOpen = document.getElementById("workspace-open");
        const coreState = document.getElementById("core-state");
        const statusLight = document.getElementById("status-light");
        const latencyReadout = document.getElementById("latency-readout");
        const activityKey = "jarvis_web_activity_v1";
        let activityState = loadActivityState();

        function scrollDown() {{ chat.scrollTop = chat.scrollHeight; }}
        function setCoreState(label, busy = false) {{
            if (coreState) coreState.textContent = label;
            if (statusLight) statusLight.classList.toggle("busy", busy);
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
            const credits = ((activityState.totalMs / 1000) * 0.03).toFixed(2) + " JC";
            const average = activityState.runs ? formatDuration(activityState.totalMs / activityState.runs) : "--";
            const last = activityState.history.length ? formatDuration(activityState.history[activityState.history.length - 1]) : "waiting";
            setMetric("credits", credits);
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
            updateActivityDashboard("CREDITED");
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
            article.innerHTML = `<div class="bubble">${{renderContent(content)}}</div>`;
            messages.appendChild(article);
            scrollDown();
            return article;
        }}
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
        if (workspaceClose) workspaceClose.addEventListener("click", () => document.body.classList.add("workspace-collapsed"));
        if (workspaceOpen) workspaceOpen.addEventListener("click", () => document.body.classList.remove("workspace-collapsed"));
        if (window.lucide) lucide.createIcons();
        async function sendMessage() {{
            const text = input.value.trim();
            if (!text) return;
            const startedAt = Date.now();
            addMessage("user", text);
            input.value = "";
            button.disabled = true;
            const placeholder = addMessage("Jarvis", "Analysing");
            let thinkingStep=0;
            const thinkingTimer=window.setInterval(()=>{{thinkingStep=(thinkingStep+1)%4;placeholder.querySelector(".bubble").textContent="Analysing"+".".repeat(thinkingStep);}},350);
            setCoreState("ANALYSING", true);
            updateActivityDashboard("THINKING");
            try {{
                const response = await fetch(`/api/chat/${{chatId}}`, {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{ message: text }})
                }});
                const data = await response.json();
                placeholder.querySelector(".bubble").innerHTML = renderContent(data.answer || "No response.");
                const elapsedMs = Number(data.elapsed_ms) || (Date.now() - startedAt);
                if(elapsedMs&&latencyReadout) latencyReadout.textContent=`${{(elapsedMs/1000).toFixed(1)}}s`;
                recordActivityRun(elapsedMs);
            }} catch (error) {{
                placeholder.querySelector(".bubble").textContent = "Connection error. Jarvis.AI did not respond.";
                updateActivityDashboard("ERROR");
            }} finally {{
                window.clearInterval(thinkingTimer);
                button.disabled = false;
                setCoreState("READY", false);
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
        startCoreVisual();
        scrollDown();
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
    return Response("User-agent: *\nAllow: /\n", media_type="text/plain")


def status_payload() -> dict[str, Any]:
    providers = available_cloud_providers()
    selected = providers[0] if providers else DEFAULT_PROVIDER
    return {
        "status": "online",
        "app": APP_TITLE,
        "version": APP_VERSION,
        "time": now_stamp(),
        "provider": selected,
        "model": provider_model("openrouter") if providers else "not configured",
        "cloud_brain_configured": bool(providers),
        "device_control": False,
        "network_mode": "cloud-safe",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return status_payload()


@app.get("/status")
def status() -> JSONResponse:
    return JSONResponse(status_payload())


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> RedirectResponse:
    device_id = device_id_from_request(request)
    chats = list_chats(device_id)
    chat_id = chats[-1][0] if chats else create_chat(device_id)
    response = RedirectResponse(url=f"/chat/{chat_id}", status_code=303)
    response.set_cookie("jarvis_cloud_device", device_id, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return response


@app.get("/new")
def new_chat(request: Request) -> RedirectResponse:
    device_id = device_id_from_request(request)
    chat_id = create_chat(device_id)
    response = RedirectResponse(url=f"/chat/{chat_id}", status_code=303)
    response.set_cookie("jarvis_cloud_device", device_id, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return response


@app.get("/chat/{chat_id}", response_class=HTMLResponse)
def open_chat(chat_id: str, request: Request) -> HTMLResponse:
    device_id = device_id_from_request(request)
    if not chat_path(chat_id).exists():
        chat_id = create_chat(device_id)
    save_device_chat(device_id, chat_id)
    response = HTMLResponse(page_html(chat_id, device_id))
    response.set_cookie("jarvis_cloud_device", device_id, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return response


@app.post("/api/chat/{chat_id}")
def api_chat(chat_id: str, payload: ChatRequest, request: Request) -> JSONResponse:
    device_id = device_id_from_request(request)
    if not chat_path(chat_id).exists():
        save_chat(chat_id, [])
    save_device_chat(device_id, chat_id)

    text = clean_text(payload.message)
    messages = load_chat(chat_id)
    started = time.perf_counter()
    answer = jarvis_reply(text, chat_id)
    messages.append({"role": "user", "content": text, "time": now_stamp()})
    messages.append({"role": "Jarvis", "content": answer, "time": now_stamp()})
    save_chat(chat_id, messages)
    return JSONResponse({"answer": answer, "elapsed_ms": round((time.perf_counter() - started) * 1000)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8010")))
