import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
from contextlib import asynccontextmanager
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from playwright.async_api import Browser, Playwright, TimeoutError as PlaywrightTimeoutError, async_playwright
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scraper-api")

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------
MAX_CONCURRENT_SCRAPES = int(os.getenv("MAX_CONCURRENT_SCRAPES", "5"))
PAGE_TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "20000"))

EMAIL_REGEX = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}"
PHONE_REGEX = r"\+?56[\s\-\.]?9[\s\-\.]?\d{4}[\s\-\.]?\d{4}|\+?\d{1,3}[\s\-\.]?\(?\d{1,4}\)?[\s\-\.]?\d{3,4}[\s\-\.]?\d{3,4}"

SOCIAL_DOMAINS = [
    "facebook.com",
    "instagram.com",
    "instagr.am",
    "linkedin.com",
    "wa.me",
    "whatsapp.com",
    "youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "spotify.com",
]

BAD_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".css", ".js", ".ico", ".woff", ".woff2", ".ttf",
    ".xml", ".json", ".pdf", ".zip", ".tar", ".gz",
    ".mp4", ".mp3", ".avi", ".mov", ".exe", ".bin"
)

CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
]

scrape_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCRAPES)
browser_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# SSRF Protection
# ---------------------------------------------------------------------------
FORBIDDEN_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # IPv4 loopback
    ipaddress.ip_network("10.0.0.0/8"),        # Private network
    ipaddress.ip_network("172.16.0.0/12"),     # Private network
    ipaddress.ip_network("192.168.0.0/16"),    # Private network
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local / Cloud metadata
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
    ipaddress.ip_network("0.0.0.0/8"),         # Current network
    ipaddress.ip_network("224.0.0.0/4"),       # Multicast
    ipaddress.ip_network("240.0.0.0/4"),       # Reserved
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 Unique Local
    ipaddress.ip_network("fe80::/10"),         # IPv6 Link-local
]

def is_ip_forbidden(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        for net in FORBIDDEN_NETWORKS:
            if ip in net:
                return True
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        return True

def validate_target_url(raw_input: str) -> str:
    """
    Validates, sanitizes, and ensures the target host is not resolving to private/internal IPs.
    """
    cleaned = raw_input.strip()
    if not cleaned:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Domain cannot be empty")

    if not cleaned.startswith(("http://", "https://")):
        target_url = f"https://{cleaned}"
    else:
        target_url = cleaned

    parsed = urlparse(target_url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsupported scheme: {parsed.scheme}")

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid domain or hostname")

    # Check for localhost / loopback aliases directly
    if hostname.lower() in ("localhost", "127.0.0.1", "::1", "metadata.google.internal"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access to internal hosts is forbidden")

    # DNS Resolution & IP Check
    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            if is_ip_forbidden(ip_str):
                logger.warning(f"SSRF blocked: hostname '{hostname}' resolved to forbidden IP '{ip_str}'")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access to private or restricted network addresses is forbidden"
                )
    except socket.gaierror as e:
        logger.info(f"DNS resolution failed for '{hostname}': {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Could not resolve domain: {hostname}")

    return target_url

# ---------------------------------------------------------------------------
# Browser Lifecycle & Self-Healing
# ---------------------------------------------------------------------------
async def ensure_browser(app: FastAPI) -> Browser:
    """
    Returns an active browser instance. If closed/crashed, reinitializes it automatically.
    """
    browser: Optional[Browser] = getattr(app.state, "browser", None)
    if browser and browser.is_connected():
        return browser

    async with browser_lock:
        # Double check after acquiring lock
        browser = getattr(app.state, "browser", None)
        if browser and browser.is_connected():
            return browser

        logger.warning("Chromium instance is not running or was disconnected. Relaunching...")
        playwright: Playwright = app.state.playwright
        app.state.browser = await playwright.chromium.launch(
            headless=True,
            args=CHROMIUM_ARGS,
        )
        logger.info("Chromium launched successfully.")
        return app.state.browser

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan managing the global Playwright and Browser instances cleanly.
    """
    logger.info("Starting up Playwright and Chromium...")
    playwright = await async_playwright().start()
    app.state.playwright = playwright
    
    browser = await playwright.chromium.launch(
        headless=True,
        args=CHROMIUM_ARGS,
    )
    app.state.browser = browser
    logger.info("Playwright and Chromium ready.")

    yield

    # Clean, awaited shutdown sequence
    logger.info("Shutting down Playwright and Chromium...")
    try:
        if hasattr(app.state, "browser") and app.state.browser:
            await app.state.browser.close()
            logger.info("Browser closed successfully.")
    except Exception as e:
        logger.warning(f"Error closing browser: {e}")

    try:
        if hasattr(app.state, "playwright") and app.state.playwright:
            # FIXED: Coroutine must be awaited!
            await app.state.playwright.stop()
            logger.info("Playwright stopped successfully.")
    except Exception as e:
        logger.warning(f"Error stopping Playwright: {e}")

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Webpage Scraper API UltraFast",
    description="High-speed Playwright-powered scraper for emails, phone numbers, and social media profiles",
    version="2.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ScrapeRequest(BaseModel):
    domain: str = Field(..., example="example.com", description="Target domain or URL to scrape")

class SocialsResult(BaseModel):
    instagram: Optional[str] = None
    facebook: Optional[str] = None
    whatsapp: Optional[str] = None
    linkedin: Optional[str] = None
    twitter: Optional[str] = None
    youtube: Optional[str] = None
    spotify: Optional[str] = None
    tiktok: Optional[str] = None

class ScrapeResponse(BaseModel):
    target_url: str
    emails: List[str]
    phones: List[str]
    socials: SocialsResult
    followed_links: List[str]

# ---------------------------------------------------------------------------
# Health & Status Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
@app.get("/ping")
async def health_check():
    browser: Optional[Browser] = getattr(app.state, "browser", None)
    is_connected = bool(browser and browser.is_connected())
    return {
        "status": "ok" if is_connected else "degraded",
        "browser_connected": is_connected,
        "max_concurrent_workers": MAX_CONCURRENT_SCRAPES,
    }

# ---------------------------------------------------------------------------
# Scraping Route Guard (In-flight SSRF protection)
# ---------------------------------------------------------------------------
async def route_guard(route):
    """
    Aborts any sub-request or navigation redirecting to private / internal network IPs.
    """
    req_url = route.request.url
    parsed = urlparse(req_url)
    hostname = parsed.hostname

    if hostname:
        if hostname.lower() in ("localhost", "127.0.0.1", "::1", "metadata.google.internal"):
            await route.abort()
            return
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            for _, _, _, _, sockaddr in addr_info:
                if is_ip_forbidden(sockaddr[0]):
                    await route.abort()
                    return
        except Exception:
            pass

    await route.continue_()

# ---------------------------------------------------------------------------
# Scraping Endpoint
# ---------------------------------------------------------------------------
@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_endpoint(req: ScrapeRequest):
    # 1. SSRF and Domain Validation
    target_url = validate_target_url(req.domain)

    results: Dict = {
        "target_url": target_url,
        "emails": [],
        "phones": [],
        "socials": {
            "instagram": None, "facebook": None, "whatsapp": None,
            "linkedin": None, "twitter": None, "youtube": None,
            "spotify": None, "tiktok": None,
        },
        "followed_links": []
    }

    # 2. Acquire Concurrency Permit
    async with scrape_semaphore:
        browser = await ensure_browser(app)
        
        # 3. Create isolated BrowserContext per request (prevents state leakage and TargetClosedError cascading)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 800},
            locale="es-ES",
        )
        page = await context.new_page()

        try:
            # Attach SSRF route guard for redirects & subresources
            await page.route("**/*", route_guard)

            # Navigate to target page
            logger.info(f"Navigating to {target_url}...")
            response = await page.goto(
                target_url,
                timeout=PAGE_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )

            if response and response.status >= 400:
                logger.warning(f"Target {target_url} returned HTTP {response.status}")

            # Wait briefly for dynamic JS hydration without blocking indefinitely
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

            html = await page.content()
            text_content = re.sub(r'<[^>]+>', ' ', html)

            # --- A. Emails Extraction ---
            emails_raw = list(set(re.findall(EMAIL_REGEX, html) + re.findall(EMAIL_REGEX, text_content)))
            valid_emails = []
            for e in emails_raw:
                e = e.lower().strip()
                if any(x in e for x in ["domain.com", "2.3.1", "version", "example.com", "sentry.io", "wixpress", "schema.org"]):
                    continue
                if any(e.endswith(ext) for ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')):
                    continue
                if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$", e):
                    continue
                valid_emails.append(e)

            # Find mailto links
            hrefs = await page.eval_on_selector_all("a[href]", "elements => elements.map(el => el.getAttribute('href'))")
            for href in hrefs:
                if href and href.lower().startswith("mailto:"):
                    clean_mailto = href.replace("mailto:", "").split("?")[0].strip().lower()
                    if re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$", clean_mailto):
                        valid_emails.append(clean_mailto)
            results["emails"] = list(dict.fromkeys(valid_emails))[:30]

            # --- B. Phones Extraction ---
            raw_phones = set()
            for href in hrefs:
                if href and href.lower().startswith("tel:"):
                    raw_phones.add(href.replace("tel:", ""))
            found_in_text = re.findall(PHONE_REGEX, text_content)
            raw_phones.update(found_in_text)

            clean_phones = set()
            for p in raw_phones:
                clean = re.sub(r'[^\d+]', '', p)
                if 8 <= len(clean) <= 15:
                    clean_phones.add(clean)
            results["phones"] = list(clean_phones)[:30]

            # --- C. Social Links Extraction ---
            for h in hrefs:
                if not h:
                    continue
                h_lower = h.lower()
                if "facebook.com" in h_lower and not results["socials"]["facebook"]:
                    results["socials"]["facebook"] = h
                elif ("instagram.com" in h_lower or "instagr.am" in h_lower) and not results["socials"]["instagram"]:
                    results["socials"]["instagram"] = h
                elif "linkedin.com" in h_lower and not results["socials"]["linkedin"]:
                    results["socials"]["linkedin"] = h
                elif ("wa.me" in h_lower or "api.whatsapp.com" in h_lower) and not results["socials"]["whatsapp"]:
                    results["socials"]["whatsapp"] = h
                elif ("youtube.com" in h_lower or "youtu.be" in h_lower) and not results["socials"]["youtube"]:
                    results["socials"]["youtube"] = h
                elif ("twitter.com" in h_lower or "x.com" in h_lower) and not results["socials"]["twitter"]:
                    results["socials"]["twitter"] = h
                elif "tiktok.com" in h_lower and not results["socials"]["tiktok"]:
                    results["socials"]["tiktok"] = h
                elif "spotify.com" in h_lower and not results["socials"]["spotify"]:
                    results["socials"]["spotify"] = h

            # --- D. Followed Internal Links ---
            valid_links = set()
            target_netloc = urlparse(target_url).netloc.lower()

            for href in hrefs:
                if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                    continue

                full_url = urljoin(target_url, href)
                parsed_link = urlparse(full_url)

                if parsed_link.scheme not in ('http', 'https'):
                    continue

                # Filter social media and bad file extensions
                if any(social in parsed_link.netloc.lower() for social in SOCIAL_DOMAINS):
                    continue
                if parsed_link.path.lower().endswith(BAD_EXTENSIONS):
                    continue

                clean_url = f"{parsed_link.scheme}://{parsed_link.netloc}{parsed_link.path}".rstrip('/')
                if clean_url and clean_url != target_url.rstrip('/'):
                    valid_links.add(clean_url)

                if len(valid_links) >= 30:
                    break

            results["followed_links"] = list(valid_links)[:30]

        except PlaywrightTimeoutError:
            logger.warning(f"Timeout reached while scraping {target_url} (limit: {PAGE_TIMEOUT_MS}ms)")
            # Return whatever was partially collected or an informative response
            results["error"] = "Navigation timeout"
        except Exception as e:
            logger.error(f"Unexpected error scraping {target_url}: {e}", exc_info=True)
            results["error"] = "Scraping partially failed due to page error"
        finally:
            # GUARANTEED cleanup: page and context MUST be closed to avoid memory/tab leaks
            try:
                await page.close()
            except Exception:
                pass
            try:
                await context.close()
            except Exception:
                pass

    return results

if __name__ == "__main__":
    import uvicorn
    # Default port changed from 80 to 8000 to prevent port conflict with Nginx
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
