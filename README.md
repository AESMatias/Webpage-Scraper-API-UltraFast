# Webpage-Scraper-API-UltraFast
High-speed API engineered to cleanly extract emails, phone numbers, and verified official social media profiles from any web domain.

### Tech Stack
* **FastAPI & Uvicorn:** Lightweight async framework and server.
* **HTTPX (Async):** Global client with unlimited connections to prevent network bottlenecks.
* **Redis:** High-speed caching layer to instantly return data for previously scraped URLs.

### Why Regex?
Instead of using heavy HTML parsers (like BeautifulSoup) that waste CPU and RAM building a DOM tree, this scraper uses native **Regex (`re`)** on raw text. This approach is lightning-fast, frees up the event loop immediately, and allows the API to scale effortlessly under massive concurrent traffic from potential users.

### How to serve de API 
x

### Ways to consume the API
x

### How can I test the API usage and limits on my server?
x

### Future things to do
x