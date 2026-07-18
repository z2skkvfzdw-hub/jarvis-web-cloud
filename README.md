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
GROQ_API_KEY=<secret Groq key>
JARVIS_CLOUD_PROVIDER=groq
JARVIS_GROQ_MODEL=llama-3.3-70b-versatile
JARVIS_SESSION_SECRET=<long random secret>
JARVIS_PUBLIC_ORIGIN=https://jarvis-web-cloud.onrender.com
DATABASE_URL=<private PostgreSQL connection string>
```

`OPENROUTER_API_KEY` and `JARVIS_OPENROUTER_MODEL=openrouter/free` are optional
fallback settings. Never commit secret values. Without `DATABASE_URL`, Render's
temporary filesystem can lose chat history after a restart or redeploy.

## Public capabilities

- Anonymous, browser-owned chat history
- Six chat modes backed by Groq, with optional OpenRouter fallback
- Web and image search commands
- Mobile chat navigation and installable PWA shell
- Signed sessions, rate limits, security headers, and privacy controls
- PostgreSQL persistence when `DATABASE_URL` is configured

This build deliberately has no access to desktop files, apps, commands, Ollama,
or private Jarvis memory.
