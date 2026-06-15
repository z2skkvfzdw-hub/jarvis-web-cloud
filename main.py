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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

try:
    from ddgs import DDGS
except Exception:
    DDGS = None


APP_TITLE = "Jarvis.web"
DATA_DIR = Path(os.environ.get("JARVIS_CLOUD_DATA_DIR", "cloud_chats"))
DATA_DIR.mkdir(exist_ok=True)

DEFAULT_PROVIDER = os.environ.get("JARVIS_CLOUD_PROVIDER", "groq").strip().lower()
DEFAULT_MODEL = os.environ.get("JARVIS_CLOUD_MODEL", "deepseek-r1-distill-llama-70b")
MAX_HISTORY_MESSAGES = int(os.environ.get("JARVIS_CLOUD_CONTEXT_MESSAGES", "10"))

app = FastAPI(title=APP_TITLE, version="1.0")


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
    if provider == "openrouter":
        return os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("JARVIS_OPENROUTER_API_KEY", "")
    return os.environ.get("GROQ_API_KEY", "") or os.environ.get("JARVIS_GROQ_API_KEY", "")


def cloud_generate(prompt: str, history: list[dict[str, str]] | None = None) -> str | None:
    provider = DEFAULT_PROVIDER
    model = DEFAULT_MODEL
    key = cloud_key(provider)
    if not key:
        return None

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You are Jarvis.web, a public cloud version of Jarvis. "
                "You are not running on the owner's laptop, so you cannot open local apps, read local files, "
                "control Windows, use local Ollama, or access private owner memory. "
                "You can chat, explain, tutor, brainstorm, summarize, write, help with coding concepts, "
                "and use web search results when they are provided. "
                "Sound calm, intelligent, direct, and natural. Do not pretend to have device control. "
                "If the user asks for a local-device action, explain that the desktop Jarvis must be running."
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

    try:
        if provider == "openrouter":
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": os.environ.get("JARVIS_OPENROUTER_REFERER", "https://jarvis.web"),
                    "X-OpenRouter-Title": APP_TITLE,
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.55,
                    "max_tokens": 900,
                },
                timeout=35,
            )
        else:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.55,
                    "max_tokens": 900,
                },
                timeout=35,
            )
        response.raise_for_status()
        data = response.json()
        return clean_text(data["choices"][0]["message"]["content"])
    except Exception as exc:
        return f"Cloud brain error: {exc}"


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


def ddgs_image_search(query: str) -> str:
    if DDGS is None:
        return "Image search is not available because the ddgs package is missing."
    try:
        images: list[dict[str, str]] = []
        with DDGS() as ddgs:
            for item in ddgs.images(query, region="wt-wt", safesearch="moderate", max_results=12):
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

    if lowered.endswith("?"):
        return (
            "The cloud brain is not configured yet. Add a GROQ_API_KEY or OPENROUTER_API_KEY to the hosting service, "
            "then I can answer properly from the cloud."
        )
    return "I am online. The cloud brain needs an API key before deeper conversation works."


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
            <h1>How can I help?</h1>
        </div>
        """
    suggestions = "" if load_chat(chat_id) else """
    <div class="suggestions" id="composer-suggestions">
        <button class="suggestion" type="button">Create an image</button>
        <button class="suggestion" type="button">Write or edit</button>
        <button class="suggestion" type="button">Look something up</button>
    </div>
    """
    sidebar = build_sidebar(chat_id, device_id)
    return f"""<!doctype html>
<html lang="en">
<head>
    <title>{APP_TITLE}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
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
        .brand-row {{
            display: flex;
            align-items: center;
            gap: 10px;
            color: #fff;
            text-decoration: none;
            padding: 0 8px;
            min-height: 42px;
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
        .nav {{ display: grid; gap: 4px; margin: 26px 0 28px; }}
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
        @media (max-width: 760px) {{
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
    </style>
</head>
<body>
    <div class="app">
        <aside class="sidebar">
            <a class="brand-row" href="/">
                <span class="brand-mark">J</span>
                <span class="brand-name">{APP_TITLE}</span>
            </a>
            <nav class="nav">
                <a class="nav-item nav-primary" href="/new"><span>New chat</span></a>
                <a class="nav-item" href="/"><span>Search chats</span></a>
                <a class="nav-item" href="/"><span>Library</span></a>
                <a class="nav-item" href="/"><span>Projects</span></a>
                <a class="nav-item" href="/"><span>Apps</span></a>
            </nav>
            <div class="recents-header">Recents</div>
            {sidebar}
        </aside>
        <main class="main">
            <header class="topbar"><div class="mode">Cloud Safe</div></header>
            <section class="chat" id="chat">
                <div class="chat-inner" id="messages">
                    {empty_state}
                    {history_html}
                </div>
            </section>
            <section class="composer">
                <form class="chat-form" id="chat-form">
                    <textarea id="message-input" name="message" placeholder="Message Jarvis..." autocomplete="off" autofocus></textarea>
                    <button class="send-button" id="send-button" type="submit">↑</button>
                </form>
                {suggestions}
                <div class="hint">Cloud Jarvis works without the owner's computer. Local device controls require desktop Jarvis.</div>
            </section>
        </main>
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

        function scrollDown() {{ chat.scrollTop = chat.scrollHeight; }}
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
                const text = item.textContent.trim();
                if (text === "Create an image") input.value = "image: futuristic assistant interface";
                else if (text === "Write or edit") input.value = "Write a clear paragraph about my idea";
                else if (text === "Look something up") input.value = "search: latest science news";
                input.focus();
            }});
        }});
        async function sendMessage() {{
            const text = input.value.trim();
            if (!text) return;
            addMessage("user", text);
            input.value = "";
            button.disabled = true;
            const placeholder = addMessage("Jarvis", "Analysing.");
            try {{
                const response = await fetch(`/api/chat/${{chatId}}`, {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{ message: text }})
                }});
                const data = await response.json();
                placeholder.querySelector(".bubble").innerHTML = renderContent(data.answer || "No response.");
            }} catch (error) {{
                placeholder.querySelector(".bubble").textContent = "Connection error. Jarvis.web did not respond.";
            }} finally {{
                button.disabled = false;
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
        scrollDown();
    </script>
</body>
</html>"""


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "online", "app": APP_TITLE, "time": now_stamp()}


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
    answer = jarvis_reply(text, chat_id)
    messages.append({"role": "user", "content": text, "time": now_stamp()})
    messages.append({"role": "Jarvis", "content": answer, "time": now_stamp()})
    save_chat(chat_id, messages)
    return JSONResponse({"answer": answer})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8010")))
