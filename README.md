# Jarvis.web Cloud Deploy

This folder is the clean cloud-hosted version of Jarvis.

It can run on Render, Koyeb, Hugging Face Spaces, or any host that supports FastAPI.

## Render settings

Build command:

```text
pip install -r requirements.txt
```

Start command:

```text
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Environment variable required:

```text
GROQ_API_KEY=your_key_here
```

## What this cloud version can do

- Chat
- Search with `search: topic`
- Show image ideas with `image: topic`
- Save separate chat history per device

## What this cloud version cannot do

- Open apps on your laptop
- Read your local laptop files
- Run terminal commands on your laptop
- Use local Ollama
- Access private desktop Jarvis memory
