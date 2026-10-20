from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import re
import asyncio
import httpx
import json
import redis.asyncio as aioredis
from urllib.parse import urlparse, urljoin
from contextlib import asynccontextmanager

EMAIL_REGEX = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}"

# Global variables, we open just one session of HTTPX and Redis.
redis_client = None
http_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, http_client
    
    redis_client = aioredis.from_url(
        "redis://localhost:6379", 
        decode_responses=True,
        max_connections=10000 # We allow up to 10k connections to Redis, because we will have many concurrent requests
    )
    
    # Initialize HTTPX Global Unlimited (Prevents "Too many open files").
    unlimited_httpx_limit = httpx.Limits(max_connections=None, max_keepalive_connections=None)
    http_client = httpx.AsyncClient(verify=False, timeout=8.0, follow_redirects=True, limits=unlimited_httpx_limit)

    # Forward port 8000 to 8001 so both ports work
    async def forward_8000(reader, writer):
        try:
            r, w = await asyncio.open_connection('127.0.0.1', 8001)
            async def pipe(src, dst):
                while True:
                    data = await src.read(4096)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            await asyncio.gather(pipe(reader, w), pipe(r, writer), return_exceptions=True)
        except Exception:
            pass
        finally:
            writer.close()

    server_8000 = None
    try:
        server_8000 = await asyncio.start_server(forward_8000, '0.0.0.0', 8000)
    except Exception:
        pass
    
    yield
    
    if server_8000:
        server_8000.close()
        await server_8000.wait_closed()

    # When the app is shutting down, we close the Redis and HTTPX clients.
    await redis_client.close()
    await http_client.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
@app.get("/ping")
async def health_check():
    return {"status": "ok"}

class ScrapeRequest(BaseModel):
    domain: str

@app.post("/scrape")
async def scrape_endpoint(req: ScrapeRequest):
    url = req.domain if req.domain.startswith("http") or \
        req.domain.startswith("www") or req.domain.startswith("https") \
            else f"https://{req.domain}"
    
    cache_key = f"scrape:{url}"
    try:
        cached_data = await redis_client.get(cache_key)
        if cached_data:
            return json.loads(cached_data)
    except Exception:
        pass

    results = {
        "target_url": url, "emails": [], "phones": [],
        "socials": {"instagram": None, "facebook": None, "whatsapp": None, "linkedin": None, "twitter": None, "youtube": None, "spotify": None, "tiktok": None},
        "followed_links": []  
    }

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
        }
        
        resp = await http_client.get(url, headers=headers) # Use the global http_client instead of creating a new one for each request
        
        if resp.status_code == 200:
            html = resp.text
            text_content = re.sub(r'<[^>]+>', ' ', html) # Remove HTML tags to extract text content.
            
            # Email extraction using regex from both HTML and text content.
            emails_raw = list(set(re.findall(EMAIL_REGEX, html) + re.findall(EMAIL_REGEX, text_content)))
            valid_emails = []
            
            for e in emails_raw:
                e = e.lower().strip()
                if any(x in e for x in ["domain.com", "2.3.1", "version", "example.com", "sentry.io", "wixpress"]): continue
                if any(e.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg']): continue
                if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$", e): continue
                valid_emails.append(e)
            
            hrefs = re.findall(r'href=[\'"]?([^\'" >]+)', html) # Extract hrefs from HTML to find mailto links and other useful links.
            for href in hrefs:
                if href.lower().startswith("mailto:"):
                    clean_mailto = href.replace("mailto:", "").split("?")[0].strip().lower()
                    if re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$", clean_mailto):
                        valid_emails.append(clean_mailto)
            results["emails"] = list(set(valid_emails))[:30] # Limit to 30 emails to avoid excessive data.
            
            # Phone extraction using regex from both hrefs and text content.
            raw_phones = set()
            for href in hrefs:
                if href.lower().startswith("tel:"):
                    raw_phones.add(href.replace("tel:", ""))
            found_in_text = re.findall(r'\+?56[\s\-\.]?9[\s\-\.]?\d{4}[\s\-\.]?\d{4}|\+?\d{1,3}[\s\-\.]?\(?\d{1,4}\)?[\s\-\.]?\d{3,4}[\s\-\.]?\d{3,4}', text_content)
            raw_phones.update(found_in_text)
            
            clean_phones = set()
            for p in raw_phones:
                clean = re.sub(r'[^\d+]', '', p)
                if 8 <= len(clean) <= 15: clean_phones.add(clean) # Only consider phone numbers with a reasonable length.
            results["phones"] = list(clean_phones)[:30] # Limit to 30 phones to avoid excessive data.
            
            # Social media link extraction from hrefs.
            for h in hrefs:
                if not h: continue
                h_lower = h.lower()
                if "facebook.com" in h_lower and not results["socials"]["facebook"]: results["socials"]["facebook"] = h
                elif ("instagram.com" in h_lower or "instagr.am" in h_lower) and not results["socials"]["instagram"]: results["socials"]["instagram"] = h
                elif "linkedin.com" in h_lower and not results["socials"]["linkedin"]: results["socials"]["linkedin"] = h
                elif "wa.me" in h_lower or "api.whatsapp.com" in h_lower and not results["socials"]["whatsapp"]: results["socials"]["whatsapp"] = h
                elif ("youtube.com" in h_lower or "youtu.be" in h_lower) and not results["socials"]["youtube"]: results["socials"]["youtube"] = h
                elif ("twitter.com" in h_lower or "x.com" in h_lower) and not results["socials"]["twitter"]: results["socials"]["twitter"] = h
                elif "tiktok.com" in h_lower and not results["socials"]["tiktok"]: results["socials"]["tiktok"] = h
                elif "spotify.com" in h_lower and not results["socials"]["spotify"]: results["socials"]["spotify"] = h

            # Intelligent filtering of the followed links to avoid social media, bad extensions, and other unwanted links.
            valid_links = set()
            social_domains = ["facebook.com", "instagram.com", "instagr.am", "linkedin.com", "wa.me", "whatsapp.com", "youtube.com", "youtu.be", "twitter.com", "x.com", "tiktok.com", "spotify.com"]
            bad_extensions = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.css', '.js', '.ico', '.woff', '.woff2', '.ttf', '.xml', '.json', '.pdf']

            for href in hrefs:
                if not href or href.startswith('#') or href.startswith('javascript:') or href.startswith('mailto:') or href.startswith('tel:'):
                    continue
                
                full_url = urljoin(url, href)
                parsed_url = urlparse(full_url)
                
                if parsed_url.scheme not in ['http', 'https']:
                    continue # Ignore data://, file://, ftp://, etc. Only allow http and https.
                
                if any(social in parsed_url.netloc.lower() for social in social_domains):
                    continue
                    
                path_lower = parsed_url.path.lower()
                if any(path_lower.endswith(ext) for ext in bad_extensions):
                    continue
                    
                clean_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}".rstrip('/')
                
                if clean_url and clean_url != url.rstrip('/'):
                    valid_links.add(clean_url)
                    
                if len(valid_links) >= 30: # Limit to 30 links to avoid excessive data.
                    break

            results["followed_links"] = list(valid_links)[:30] # Limit to 30 links to avoid excessive data.

    except Exception:
        pass

    try:
        await redis_client.set(cache_key, json.dumps(results), ex=3600)
    except Exception:
        pass

    return results

@app.post("/testing")
async def testing_endpoint(req: ScrapeRequest):
    url = req.domain if req.domain.startswith("http") else f"https://{req.domain}"
    
    # ❌ Redis caching is removed here, to force every request to process RegEx and saturate the CPU for testing purposes.

    results = {
        "target_url": url, "emails": [], "phones": [],
        "socials": {"instagram": None, "facebook": None, "whatsapp": None, "linkedin": None, "twitter": None, "youtube": None, "spotify": None, "tiktok": None},
        "followed_links": []  
    }

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
        }
        
        resp = await http_client.get(url, headers=headers)
        
        if resp.status_code == 200:
            html = resp.text
            text_content = re.sub(r'<[^>]+>', ' ', html)
            
            emails_raw = list(set(re.findall(EMAIL_REGEX, html) + re.findall(EMAIL_REGEX, text_content)))
            valid_emails = []
            for e in emails_raw:
                e = e.lower().strip()
                if any(x in e for x in ["domain.com", "2.3.1", "version", "example.com", "sentry.io", "wixpress"]): continue
                if any(e.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg']): continue
                if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$", e): continue
                valid_emails.append(e)
            
            hrefs = re.findall(r'href=[\'"]?([^\'" >]+)', html)
            for href in hrefs:
                if href.lower().startswith("mailto:"):
                    clean_mailto = href.replace("mailto:", "").split("?")[0].strip().lower()
                    if re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$", clean_mailto):
                        valid_emails.append(clean_mailto)
            results["emails"] = list(set(valid_emails))[:30]
            
            raw_phones = set()
            for href in hrefs:
                if href.lower().startswith("tel:"):
                    raw_phones.add(href.replace("tel:", ""))
            found_in_text = re.findall(r'\+?56[\s\-\.]?9[\s\-\.]?\d{4}[\s\-\.]?\d{4}|\+?\d{1,3}[\s\-\.]?\(?\d{1,4}\)?[\s\-\.]?\d{3,4}[\s\-\.]?\d{3,4}', text_content)
            raw_phones.update(found_in_text)
            clean_phones = set()
            for p in raw_phones:
                clean = re.sub(r'[^\d+]', '', p)
                if 8 <= len(clean) <= 15: clean_phones.add(clean)
            results["phones"] = list(clean_phones)[:30]
            
            for h in hrefs:
                if not h: continue
                h_lower = h.lower()
                if "facebook.com" in h_lower and not results["socials"]["facebook"]: results["socials"]["facebook"] = h
                elif ("instagram.com" in h_lower or "instagr.am" in h_lower) and not results["socials"]["instagram"]: results["socials"]["instagram"] = h
                elif "linkedin.com" in h_lower and not results["socials"]["linkedin"]: results["socials"]["linkedin"] = h
                elif "wa.me" in h_lower or "api.whatsapp.com" in h_lower and not results["socials"]["whatsapp"]: results["socials"]["whatsapp"] = h
                elif ("youtube.com" in h_lower or "youtu.be" in h_lower) and not results["socials"]["youtube"]: results["socials"]["youtube"] = h
                elif ("twitter.com" in h_lower or "x.com" in h_lower) and not results["socials"]["twitter"]: results["socials"]["twitter"] = h
                elif "tiktok.com" in h_lower and not results["socials"]["tiktok"]: results["socials"]["tiktok"] = h
                elif "spotify.com" in h_lower and not results["socials"]["spotify"]: results["socials"]["spotify"] = h

            valid_links = set()
            social_domains = ["facebook.com", "instagram.com", "instagr.am", "linkedin.com", "wa.me", "whatsapp.com", "youtube.com", "youtu.be", "twitter.com", "x.com", "tiktok.com", "spotify.com"]
            bad_extensions = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.css', '.js', '.ico', '.woff', '.woff2', '.ttf', '.xml', '.json', '.pdf']

            for href in hrefs:
                if not href or href.startswith('#') or href.startswith('javascript:') or href.startswith('mailto:') or href.startswith('tel:'):
                    continue
                
                full_url = urljoin(url, href)
                parsed_url = urlparse(full_url)
                
                if parsed_url.scheme not in ['http', 'https']:
                    continue
                
                if any(social in parsed_url.netloc.lower() for social in social_domains):
                    continue
                    
                path_lower = parsed_url.path.lower()
                if any(path_lower.endswith(ext) for ext in bad_extensions):
                    continue
                    
                clean_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}".rstrip('/')
                
                if clean_url and clean_url != url.rstrip('/'):
                    valid_links.add(clean_url)
                    
                if len(valid_links) >= 30:
                    break

            results["followed_links"] = list(valid_links)[:30]

    except Exception:
        pass

    return results

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001)
