# Jarvis.AI Network Setup

Jarvis.AI can run from any normal network that allows HTTPS web traffic.

It cannot force its way through school, work, country, or parent network blocks. If a network blocks Render, Groq, or your custom domain, the correct fix is to use an allowed host/domain or ask the network owner to whitelist it.

## Best Setup

1. Keep the Render deployment live.
2. Add a custom domain later, such as `jarvis.yourdomain.com`.
3. Use Cloudflare DNS for the domain if you want stable HTTPS and easier DNS management.
4. Keep a backup host ready, such as Railway, Fly.io, Koyeb, or Hugging Face Spaces.

## Health Checks

Use these URLs to check whether Jarvis.AI is online:

```text
https://jarvis-web-cloud.onrender.com/health
https://jarvis-web-cloud.onrender.com/status
```

## Phone And Tablet

Open the Render URL in Safari or Chrome. Then use Add to Home Screen. Jarvis.AI includes a manifest, app icon, and offline shell, so it behaves more like a web app.

## Important Limits

The cloud web version can chat, search, show image ideas, and save per-device chat history.

The cloud web version cannot open apps, read files, run terminal commands, or control the owner's laptop. Those features require the desktop Jarvis running on the computer.
