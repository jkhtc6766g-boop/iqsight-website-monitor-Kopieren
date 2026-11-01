

import json
import re
import time
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.request import Request, urlopen
from html.parser import HTMLParser

BASE_URL = "https://www.iqsight.com/"
DOMAIN = "www.iqsight.com"
MAX_PAGES = 500
DATA_FILE = "data.json"
TIMEOUT = 20

HEADERS = {
    "User-Agent": "IQSIGHTWebsiteMonitor/1.0 (public website change monitor)"
}


def fetch(url):
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=TIMEOUT) as response:
            return response.read(), response.headers.get("Content-Type", "")
    except Exception as exc:
        print(f"Could not fetch {url}: {exc}")
        return None, ""


def clean_url(url):
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() not in (DOMAIN, "iqsight.com"):
        return None
    if parsed.path.lower().endswith(
        (".jpg", ".jpeg", ".png", ".gif", ".svg",
         ".webp", ".mp4", ".zip", ".woff", ".css", ".js")
    ):
        return None
    return url.rstrip("/") or BASE_URL


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.links = []
        self.title = []
        self.in_title = False
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag in ("script", "style", "noscript", "svg"):
            self.skip_depth += 1
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in ("script", "style", "noscript", "svg") and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text:
            return
        if self.in_title:
            self.title.append(text)
        if not self.skip_depth:
            self.text.append(text)


def sitemap_urls():
    candidates = [
        urljoin(BASE_URL, "robots.txt"),
        urljoin(BASE_URL, "sitemap.xml"),
        urljoin(BASE_URL, "sitemap_index.xml"),
    ]
    sitemap_files = set()
    page_urls = set()

    for candidate in candidates:
        content, _ = fetch(candidate)
        if not content:
            continue

        if candidate.endswith("robots.txt"):
            for line in content.decode("utf-8", "ignore").splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_files.add(line.split(":", 1)[1].strip())
        else:
            sitemap_files.add(candidate)

    visited = set()
    while sitemap_files and len(visited) < 100:
        sitemap = sitemap_files.pop()
        if sitemap in visited:
            continue
        visited.add(sitemap)
        content, _ = fetch(sitemap)
        if not content:
            continue

        try:
            root = ET.fromstring(content)
            for element in root.iter():
                if element.tag.endswith("loc") and element.text:
                    loc = element.text.strip()
                    if loc.endswith(".xml") or "sitemap" in loc.lower():
                        if loc not in visited:
                            sitemap_files.add(loc)
                    else:
                        clean = clean_url(loc)
                        if clean:
                            page_urls.add(clean)
        except ET.ParseError:
            continue
        time.sleep(0.2)

    return page_urls


def crawl():
    queue = list(sitemap_urls())
    if not queue:
        queue = [BASE_URL]

    seen = set()
    pages = {}

    while queue and len(seen) < MAX_PAGES:
        url = queue.pop(0)
        url = clean_url(url)
        if not url or url in seen:
            continue
        seen.add(url)

        content, content_type = fetch(url)
        if not content or "html" not in content_type.lower():
            continue

        parser = PageParser()
        try:
            parser.feed(content.decode("utf-8", "replace"))
        except Exception:
            continue

        title = " ".join(parser.title).strip() or url
        text = re.sub(r"\s+", " ", " ".join(parser.text)).strip()
        if not text:
            continue

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        pages[url] = {
            "url": url,
            "title": title[:300],
            "text": text[:50000],
            "hash": digest,
            "language": (
                "de" if "/de/" in url.lower()
                or "lang=\"de" in content.decode("utf-8", "ignore")[:5000].lower()
                else "en"
            ),
        }

        for href in parser.links:
            target = clean_url(urljoin(url, href))
            if target and target not in seen and target not in queue:
                queue.append(target)

        time.sleep(0.3)

    return pages


def main():
    now = datetime.now(timezone.utc).isoformat()
    current = crawl()

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            previous_data = json.load(file)
        previous = previous_data.get("pages", {})
    except (OSError, json.JSONDecodeError):
        previous = {}

    changes = []
    for url, page in current.items():
        old = previous.get(url)
        if old is None:
            status = "new"
        elif old.get("hash") != page["hash"]:
            status = "changed"
        else:
            continue

        old_text = old.get("text", "") if old else ""
        page["change"] = status
        page["checked_at"] = now

        if status == "new":
            summary = "Neue öffentliche Seite entdeckt."
        else:
            summary = "Der Seiteninhalt wurde geändert."

        changes.append({
            "url": url,
            "title": page["title"],
            "language": page["language"],
            "status": status,
            "summary": summary,
            "previous_text": old_text[:5000],
            "current_text": page["text"][:5000],
            "checked_at": now,
        })

    removed = set(previous) - set(current)
    for url in sorted(removed):
        changes.append({
            "url": url,
            "title": previous[url].get("title", url),
            "language": previous[url].get("language", "en"),
            "status": "possibly_removed",
            "summary": (
                "Seite beim aktuellen Lauf nicht gefunden. "
                "Löschung bitte manuell überprüfen."
            ),
            "previous_text": previous[url].get("text", "")[:5000],
            "current_text": "",
            "checked_at": now,
        })

    output = {
        "last_checked": now,
        "pages_scanned": len(current),
        "changes": changes + previous_data_changes(previous_data=None),
        "pages": current,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)

    print(f"Pages scanned: {len(current)}")
    print(f"Changes found: {len(changes)}")


def previous_data_changes(previous_data):
    return []


if __name__ == "__main__":
    main()
