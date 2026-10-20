# Webpage-Scraper-API-UltraFast
High-speed API engineered to cleanly extract emails, phone numbers, and verified official social media profiles from any web domain.

### Tech Stack
* **FastAPI & Uvicorn:** Lightweight async framework and server.
* **HTTPX (Async):** Global client with unlimited connections to prevent network bottlenecks.
* **Redis:** High-speed caching layer to instantly return data for previously scraped URLs.

### Why Regex?
Instead of using heavy HTML parsers (like BeautifulSoup) that waste CPU and RAM building a DOM tree, this scraper uses native **Regex (`re`)** on raw text. This approach is lightning-fast, frees up the event loop immediately, and allows the API to scale effortlessly under massive concurrent traffic from potential users.

### How to serve the API
```bash
docker compose up -d --build
```

### Ways to consume the API
```bash
curl -X POST "http://127.0.0.1:8001/scrape" \
     -H "Content-Type: application/json" \
     -d '{"domain": "example.com"}'
```
