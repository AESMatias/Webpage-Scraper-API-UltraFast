# Webpage-Scraper-API-UltraFast

High-speed, production-grade contact & social scraper API built with **FastAPI**, **Playwright (Chromium Headless)**, and **Docker Compose**.

Engineered to extract emails, phone numbers, verified official social media profiles, and key subpages (`contacto`, `nosotros`, `about`) with anti-bot stealth and high-concurrency memory isolation.

---

## Architecture & Features

* **Isolated Per-Request Contexts:** Eliminates `TargetClosedError` by creating an isolated `BrowserContext` per incoming request, preventing cookie/state leaks and browser lockups.
* **Self-Healing Browser:** Automatically detects browser disconnections or crashes and relaunches Chromium on the fly without service interruptions.
* **Bounded Concurrency (`asyncio.Semaphore`):** Limits simultaneous Chromium browser tabs to prevent memory exhaustion (OOM Killer).
* **Resource Blocking:** Blocks images, media, fonts, stylesheets, and tracking domains (`google-analytics`, `sentry`, `facebook.net`) to accelerate scraping by up to 10x and conserve RAM.
* **SSRF Protection:** Filters and blocks private IP ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, `169.254.0.0/16`) and in-flight redirects to protect host infrastructure.
* **Dockerized with PID 1 Reaper:** Runs with `init: true`, `shm_size: 1gb`, resource limits, and `restart: unless-stopped`.
* **Zero Host Collisions:** Binds to `127.0.0.1:8000` and `127.0.0.1:8001`, integrating cleanly with Host Nginx and Cloudflare Tunnel.

---

## API Endpoints

### 1. Healthcheck
* **GET** `/health` or `/ping`
```json
{
  "status": "ok",
  "browser_connected": true,
  "max_concurrent_workers": 5
}
```

### 2. Scrape Domain
* **POST** `/scrape`
* **Request:**
```json
{
  "domain": "example.com"
}
```
* **Response:**
```json
{
  "target_url": "https://example.com",
  "emails": ["contact@example.com"],
  "phones": ["+56912345678"],
  "socials": {
    "instagram": "https://instagram.com/...",
    "facebook": "https://facebook.com/...",
    "whatsapp": null,
    "linkedin": "https://linkedin.com/company/...",
    "twitter": null,
    "youtube": null,
    "spotify": null,
    "tiktok": null
  },
  "followed_links": [
    "https://example.com/contacto",
    "https://example.com/nosotros"
  ]
}
```

---

## Server Operations & Management

Inside `/root/scraper_api/`:

### View Live Logs
```bash
docker compose logs -f --tail=100 scraper-api
```

### Check Container Status
```bash
docker compose ps
```

### Restart Service
```bash
docker compose restart scraper-api
```

### Update & Rebuild
```bash
git pull
docker compose build --pull
docker compose up -d
```
