# Jarvis.AI Cloud Deploy

This folder is the clean cloud-hosted version of Jarvis.

Live app:

```text
https://jarvis-web-cloud.onrender.com
```

It can run on Render, Railway, Fly.io, Koyeb, Hugging Face Spaces, or any host that supports FastAPI.

## Render Settings

Build command:

```text
pip install -r requirements.txt
```

Start command:

```text
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Environment variables:

```text
GROQ_API_KEY=your_key_here
JARVIS_CLOUD_PROVIDER=auto
JARVIS_GROQ_MODEL=openai/gpt-oss-120b
```

## Network Support

Jarvis.AI now includes:

- Installable web app manifest
- App icon
- Service worker
- Offline shell page
- `/health` status check
- `/status` diagnostics check
- Backup deploy files for Railway and Fly.io

This makes Jarvis easier to open from phones, tablets, and other networks. It does not bypass blocked networks. If a network blocks Render or the Jarvis domain, use a different allowed host/domain or ask the network owner to whitelist it.

More notes are in `NETWORK_DEPLOY.md`.

## What This Cloud Version Can Do

- Chat naturally
- Search with `search: topic`
- Show image ideas with `image: topic`
- Save separate chat history per device
- Work without the owner's laptop being on
- Be added to a phone or tablet home screen

## What This Cloud Version Cannot Do

- Open apps on your laptop
- Read your local laptop files
- Run terminal commands on your laptop
- Use local Ollama
- Access private desktop Jarvis memory
- Control a computer that is off
