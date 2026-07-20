# Jarvis.AI Cloud App

This folder contains the canonical public Jarvis web app. The live address is:

```text
https://jarvis-web-cloud.onrender.com
```

From the repository root, start it with:

```text
py -3.11 -m uvicorn cloud_deploy.main:app --host 127.0.0.1 --port 8020
```

If `cloud_deploy` is used as the service root directory, use:

```text
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Render needs these environment variables:

```text
JARVIS_CLOUD_PROVIDER=nvidia
JARVIS_PROVIDER_CHAIN=nvidia,openrouter
JARVIS_DEVICE_MEMORY=true
JARVIS_ENABLE_GROQ=false
NVIDIA_API_KEY=<secret NVIDIA key>
NVIDIA_MODEL=openai/gpt-oss-120b
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
JARVIS_SESSION_SECRET=<long random secret>
JARVIS_PUBLIC_ORIGIN=https://jarvis-web-cloud.onrender.com
DATABASE_URL=<private PostgreSQL connection string>
JARVIS_ADSENSE_CLIENT=ca-pub-xxxxxxxxxxxxxxxx
JARVIS_ADSENSE_SLOT_SIDEBAR=<AdSense slot id>
JARVIS_ADSENSE_SLOT_COMPOSER=<AdSense slot id>
```

`OPENROUTER_API_KEY` and `JARVIS_OPENROUTER_MODEL=openrouter/free`
are optional fallback settings. Groq is ignored unless `JARVIS_ENABLE_GROQ=true`;
if you re-enable it, add `GROQ_API_KEY` and optionally
`JARVIS_GROQ_MODEL=llama-3.3-70b-versatile`. Never commit secret values. With `JARVIS_DEVICE_MEMORY=true`,
main chat memory is stored in the user's browser, so Render does not need PostgreSQL for
public chat history. `DATABASE_URL` is still useful for server-owned features later.
The `JARVIS_ADSENSE_*` settings are optional. Ads stay disabled unless a valid
AdSense client id and at least one numeric slot id are configured.

## Public capabilities

- Anonymous, device-owned chat history stored in the user's browser
- Nine chat modes backed by NVIDIA, with optional OpenRouter fallback
- In-memory document extraction for PDF, DOCX, PPTX, text, code, CSV, JSON, and Markdown files
- Streaming response endpoint for chat clients that want incremental output
- Web and image search commands
- Mobile chat navigation and installable PWA shell
- Signed sessions, rate limits, security headers, and privacy controls
- Optional Google AdSense slots controlled by Render environment variables
- PostgreSQL persistence when `DATABASE_URL` is configured

This build deliberately has no access to desktop files, apps, commands, Ollama,
or private Jarvis memory.
