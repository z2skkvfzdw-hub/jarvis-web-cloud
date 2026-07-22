# Jarivs Cloud App

This folder contains the canonical public Jarivs web app. The live address is:

```text
https://jarvis-web-cloud.onrender.com
```

The live app currently reports its name and version through `/status`. Check it
after every deploy:

```text
py -3.11 scripts/check_deploy.py
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
JARVIS_GOOGLE_CLIENT_ID=<Google OAuth client id>
JARVIS_GOOGLE_CLIENT_SECRET=<Google OAuth client secret>
JARVIS_GOOGLE_REDIRECT_URI=https://jarvis-web-cloud.onrender.com/auth/google/callback
```

`OPENROUTER_API_KEY` and `JARVIS_OPENROUTER_MODEL=openrouter/free`
are optional fallback settings. Groq is ignored unless `JARVIS_ENABLE_GROQ=true`;
if you re-enable it, add `groq` to `JARVIS_PROVIDER_CHAIN`, add `GROQ_API_KEY`,
and optionally set `JARVIS_GROQ_MODEL=llama-3.3-70b-versatile`. Never commit secret values. With `JARVIS_DEVICE_MEMORY=true`,
main chat memory is stored in the user's browser, so Render does not need PostgreSQL for
public chat history. `DATABASE_URL` is still useful for server-owned features later.
The `JARVIS_ADSENSE_*` settings are optional. Ads stay disabled unless a valid
AdSense client id and at least one numeric slot id are configured.
The `JARVIS_GOOGLE_*` settings are optional. Google sign-in stays hidden unless
both the client id and client secret are configured. The OAuth redirect URI in
Google Cloud must exactly match `/auth/google/callback` on the public Render URL.

To finish Google sign-in setup:

1. Create a Web application OAuth client in Google Cloud Console.
2. Add `https://jarvis-web-cloud.onrender.com` as an authorized JavaScript origin.
3. Add `https://jarvis-web-cloud.onrender.com/auth/google/callback` as an authorized redirect URI.
4. Copy the client id and client secret into Render's environment variables.
5. Redeploy, open `/status`, and confirm `google_login_configured` is `true`.

Signed-in users can open `/account` to view their profile, download a combined
export of server-owned records and browser-held chat memory, delete their data,
or sign out. Anonymous users have the same export and deletion controls for the
current browser without being required to create an account.

## Render URL note

The current Render subdomain is:

```text
https://jarvis-web-cloud.onrender.com
```

The user wants:

```text
https://jarvis.onrender.com
```

Render does not expose a normal setting to rename an existing `onrender.com`
subdomain after the service has been created. To get that exact address, create
a new Render web service named `jarvis` if the subdomain is available, or attach
a custom domain that the user owns.

## Deploy check

After pushing and deploying, run:

```text
py -3.11 scripts/check_deploy.py
```

From the repository root, use:

```text
py -3.11 cloud_deploy\scripts\check_deploy.py
```

The script checks the live `/status` endpoint and fails if the app name or
version is not the expected deployed build.

## Public capabilities

- Anonymous, device-owned chat history stored in the user's browser
- Optional Google sign-in, account profile, cross-device chat routing, data export, and account deletion
- Per-conversation essay workspace with rubric and teacher-feedback uploads, autosaved drafts, restorable versions, and AI rubric checks
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
