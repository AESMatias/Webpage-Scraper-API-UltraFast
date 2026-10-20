import asyncio
import ipaddress
import logging
import os
import re
import socket
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, HTTPException, status
from playwright.async_api import Browser, Playwright, Route, TimeoutError as PlaywrightTimeoutError, async_playwright
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] [scraper-api]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scraper-api")

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------
PORT = int(os.getenv("PORT", "8000"))
MAX_CONCURRENT_SCRAPES = int(os.getenv("MAX_CONCURRENT_SCRAPES", "5"))
PAGE_TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "15000"))

EMAIL_REGEX = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}"
PHONE_REGEX = r"\+?56[\s\-\.]?9[\s\-\.]?\d{4}[\s\-\.]?\d{4}|\+?\d{1,3}[\s\-\.]?\(?\d{1,4}\)?[\s\-\.]?\d{3,4}[\s\-\.]?\d{3,4}"

BLOCK_RESOURCE_TYPES = {"image", "media", "font", "stylesheet", "other"}
BLOCK_DOMAINS = [
    "google-analytics.com",
    "googletagmanager.com",
    "facebook.net",
    "facebook.com/tr",
    "doubleclick.net",
    "hotjar.com",
    "sentry.io"
]

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

    if hostname.lower() in ("localhost", "127.0.0.1", "::1", "metadata.google.internal"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access to internal hosts is forbidden")

    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in addr_info:
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
# Subpage Finder
# ---------------------------------------------------------------------------
def find_key_subpages(html_content: str, base_url: str) -> List[str]:
    subpages: Set[str] = set()
    keywords = ['contacto', 'contact', 'nosotros', 'ayuda', 'informacion', 'about', 'soporte']
    hrefs = re.findall(r'href=[\'"]?([^\'" >]+)', html_content)
    base_netloc = urlparse(base_url).netloc.lower()
    for href in hrefs:
        full_url = urljoin(base_url, href)
        parsed_sub = urlparse(full_url)
        if parsed_sub.netloc.lower() == base_netloc and parsed_sub.scheme in ("http", "https"):
            if any(k in href.lower() for k in keywords):
                subpages.add(full_url)
    return list(subpages)[:3]

# ---------------------------------------------------------------------------
# Browser Lifecycle & Self-Healing
# ---------------------------------------------------------------------------
async def ensure_browser(app: FastAPI) -> Browser:
    """
    Returns an active Chromium browser. Auto-recovers if Chromium was killed or closed.
    """
    browser: Optional[Browser] = getattr(app.state, "browser", None)
    if browser and browser.is_connected():
        return browser

    async with browser_lock:
        browser = getattr(app.state, "browser", None)
        if browser and browser.is_connected():
            return browser

        logger.warning("Chromium instance disconnected or missing. Relaunching Chromium...")
        playwright: Playwright = app.state.playwright
        app.state.browser = await playwright.chromium.launch(
            headless=True,
            args=CHROMIUM_ARGS
        )
        logger.info("Chromium instance successfully launched.")
        return app.state.browser

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Playwright and Chromium...")
    playwright = await async_playwright().start()
    app.state.playwright = playwright
    
    browser = await playwright.chromium.launch(
        headless=True,
        args=CHROMIUM_ARGS
    )
    app.state.browser = browser
    logger.info(f"Playwright and Chromium ready. Serving on port {PORT}.")

    yield

    logger.info("Shutting down Playwright and Chromium...")
    try:
        if hasattr(app.state, "browser") and app.state.browser:
            await app.state.browser.close()
            logger.info("Browser closed.")
    except Exception as e:
        logger.warning(f"Error closing browser: {e}")

    try:
        if hasattr(app.state, "playwright") and app.state.playwright:
            # FIX: Coroutine MUST be awaited
            await app.state.playwright.stop()
            logger.info("Playwright stopped cleanly.")
    except Exception as e:
        logger.warning(f"Error stopping Playwright: {e}")

# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Webpage Scraper API UltraFast",
    description="High-speed Playwright-powered scraper for contact info, socials, and subpages",
    version="2.0.0",
    lifespan=lifespan,
)

class ScrapeRequest(BaseModel):
    domain: str = Field(..., example="example.com", description="Target domain or full URL")

# ---------------------------------------------------------------------------
# Health & Status
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
# Scrape Endpoint
# ---------------------------------------------------------------------------
@app.post("/scrape")
async def scrape_endpoint(req: ScrapeRequest):
    url = validate_target_url(req.domain)
    
    results: Dict = {
        "target_url": url,
        "emails": [],
        "phones": [],
        "socials": {
            "instagram": None, "facebook": None, "whatsapp": None,
            "linkedin": None, "twitter": None, "youtube": None,
            "spotify": None, "tiktok": None
        },
        "followed_links": []
    }

    async with scrape_semaphore:
        browser = await ensure_browser(app)
        
        # Isolated context per request: prevents TargetClosedError cascade and state leakage
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1,
            ignore_https_errors=True,
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        page = await context.new_page()

        # Route handler: Block unnecessary media/trackers and enforce SSRF filter
        async def route_interceptor(route: Route):
            req_url = route.request.url
            res_type = route.request.resource_type

            # 1. Performance filter
            if res_type in BLOCK_RESOURCE_TYPES or any(d in req_url for d in BLOCK_DOMAINS):
                await route.abort()
                return

            # 2. In-flight SSRF filter (block redirect to loopback/private)
            parsed_req = urlparse(req_url)
            if parsed_req.hostname:
                if parsed_req.hostname.lower() in ("localhost", "127.0.0.1", "::1"):
                    await route.abort()
                    return
                try:
                    addr_info = socket.getaddrinfo(parsed_req.hostname, None)
                    for _, _, _, _, sockaddr in addr_info:
                        if is_ip_forbidden(sockaddr[0]):
                            await route.abort()
                            return
                except Exception:
                    pass

            await route.continue_()

        await page.route("**/*", route_interceptor)

        try:
            # 15s timeout to avoid hoarding RAM
            logger.info(f"Navigating to {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await page.wait_for_timeout(1500)

            text_content = await page.evaluate("document.body ? document.body.innerText : ''")
            html = await page.content()

            # 1. Emails
            emails_raw = list(set(re.findall(EMAIL_REGEX, html) + re.findall(EMAIL_REGEX, text_content)))
            valid_emails = []
            for e in emails_raw:
                e = e.lower().strip()
                if any(x in e for x in ["domain.com", "2.3.1", "version", "sentry.io", "w3.org", "schema.org"]):
                    continue
                if any(e.endswith(ext) for ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')):
                    continue
                if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$", e):
                    continue
                valid_emails.append(e)

            mailto_links = await page.locator("a[href^='mailto:']").evaluate_all("els => els.map(el => el.href)")
            for mailto in mailto_links:
                clean_mailto = mailto.replace("mailto:", "").split("?")[0].strip().lower()
                if re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$", clean_mailto):
                    valid_emails.append(clean_mailto)

            results["emails"] = list(dict.fromkeys(valid_emails))

            # 2. Teléfonos
            hrefs = await page.locator("a").evaluate_all("els => els.map(el => el.href)")
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
            results["phones"] = list(clean_phones)

            # 3. Redes sociales
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

            # 4. Subpáginas de contacto / nosotros
            subpages = find_key_subpages(html, url)
            for s in subpages:
                try:
                    resp = await context.request.get(s, timeout=3000)
                    if resp.ok:
                        content = await resp.text()
                        sub_emails = re.findall(EMAIL_REGEX, content)
                        valid_sub_emails = [
                            se.lower().strip() for se in sub_emails
                            if re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$", se)
                            and not any(x in se.lower() for x in ["domain.com", "2.3.1", "version"])
                        ]
                        results["emails"].extend(valid_sub_emails)
                        results["followed_links"].append(s)
                except Exception as sub_err:
                    logger.debug(f"Subpage fetch error for {s}: {sub_err}")
                    continue

            results["emails"] = list(dict.fromkeys(results["emails"]))

        except PlaywrightTimeoutError:
            logger.warning(f"Timeout (15s) navigating to {url}. Returning partial results.")
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}", exc_info=True)
        finally:
            # Cleanup guaranteed
            try:
                await asyncio.wait_for(page.close(), timeout=2.0)
            except Exception:
                pass
            try:
                await asyncio.wait_for(context.close(), timeout=2.0)
            except Exception:
                pass

    return results

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
