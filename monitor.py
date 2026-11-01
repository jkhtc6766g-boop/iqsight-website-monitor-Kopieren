import json
import re
import time
import hashlib
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from html.parser import HTMLParser

BASE_URL = "https://www.iqsight.com/"
DOMAIN = "www.iqsight.com"
MAX_PAGES = 5000
DATA_FILE = "data.json"
TIMEOUT = 20
RETRIES = 2
RETRY_DELAY = 1.0
REQUEST_DELAY = 0.05
MAX_SITEMAPS = 200
MAX_CHANGE_HISTORY = 10000

HEADERS = {
    "User-Agent": "IQSIGHTWebsiteMonitor/1.1 (public website change monitor)"
}
SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".mp4",
    ".zip", ".woff", ".woff2", ".ttf", ".css", ".js", ".ico",
    ".pdf", ".xml", ".xlsx", ".docx", ".pptx"
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def fetch(url):
    """Fetch a URL with a small retry budget; return (bytes, content type)."""
    for attempt in range(RETRIES + 1):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=TIMEOUT) as response:
                return response.read(), response.headers.get("Content-Type", "")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                print(f"Could not fetch {url}: {exc}")
        except Exception as exc:
            print(f"Could not fetch {url}: {exc}")
            break
    return None, ""


def clean_url(url):
    if not url:
        return None
    url, _ = urldefrag(url.strip())
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() not in (DOMAIN, "iqsight.com"):
        return None
    if parsed.path.lower().endswith(SKIP_EXTENSIONS):
        return None
    # Normalize host and remove common tracking parameters.
    host = DOMAIN
    path = parsed.path or "/"
    query_parts = []
    if parsed.query:
        for part in parsed.query.split("&"):
            key = part.split("=", 1)[0].lower()
            if not (key.startswith("utm_") or key in {
                "gclid", "fbclid", "msclkid", "campaignid"
            }):
                query_parts.append(part)
    query = "&".join(query_parts)
    normalized = f"https://{host}{path}"
    if query:
        normalized += f"?{query}"
    if path != "/":
        normalized = normalized.rstrip("/")
    return normalized


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.links = []
        self.title = []
        self.in_title = False
        self.skip_tags = []

    def handle_starttag(self, tag, attrs):
