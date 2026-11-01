
import json
import re
import time
import hashlib
import os
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
MAX_CHANGE_HISTORY = 100

HEADERS = {
    "User-Agent": "IQSIGHTWebsiteMonitor/1.1 (public website change monitor)"
}

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".mp4",
    ".zip", ".woff", ".woff2", ".ttf", ".css", ".js", ".ico",
    ".pdf", ".xlsx", ".docx", ".pptx"
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def fetch(url):
    """Fetch a URL with retries."""
    for attempt in range(RETRIES + 1):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=TIMEOUT) as response:
                return response.read(), response.headers.get(
                    "Content-Type", ""
                )
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

    path = parsed.path or "/"
    query_parts = []

    if parsed.query:
        for part in parsed.query.split("&"):
            key = part.split("=", 1)[0].lower()
            if not (
                key.startswith("utm_")
                or key in {"gclid", "fbclid", "msclkid", "campaignid"}
            ):
                query_parts.append(part)

    result = f"https://{DOMAIN}{path}"

    if query_parts:
        result += "?" + "&".join(query_parts)

    if path != "/":
        result = result.rstrip("/")

    return result


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.links = []
        self.title = []
        self.in_title = False
        self.skip_tags = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag == "title":
            self.in_title = True

        if tag in ("script", "style", "noscript", "svg"):
            self.skip_tags.append(tag)

        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

        if tag in ("script", "style", "noscript", "svg"):
            for i in range(len(self.skip_tags) - 1, -1, -1):
                if self.skip_tags[i] == tag:
                    del self.skip_tags[i]
                    break

    def handle_data(self, data):
        value = " ".join(data.split())

        if not value:
            return

        if self.in_title:
            self.title.append(value)

        if not self.skip_tags:
            self.text.append(value)


def parse_sitemap(content):
    page_urls = set()
    child_sitemaps = set()

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return page_urls, child_sitemaps

    root_name = root.tag.rsplit("}", 1)[-1].lower()

    for element in root.iter():
        if not element.tag.endswith("loc") or not element.text:
            continue

        loc = element.text.strip()

        if root_name == "sitemapindex":
            child_sitemaps.add(loc)
        elif loc.lower().endswith(".xml"):
            child_sitemaps.add(loc)
        else:
            cleaned = clean_url(loc)
            if cleaned:
                page_urls.add(cleaned)

    return page_urls, child_sitemaps


def sitemap_urls():
    sitemap_files = deque()
    page_urls = set()

    robots_content, _ = fetch(urljoin(BASE_URL, "robots.txt"))

    if robots_content:
        for line in robots_content.decode(
            "utf-8", "ignore"
        ).splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap_files.append(
                    line.split(":", 1)[1].strip()
                )

    sitemap_files.extend([
        urljoin(BASE_URL, "sitemap.xml"),
        urljoin(BASE_URL, "sitemap_index.xml"),
    ])

    visited = set()

    while sitemap_files and len(visited) < MAX_SITEMAPS:
        sitemap = sitemap_files.popleft()

        if sitemap in visited:
            continue

        visited.add(sitemap)
        content, content_type = fetch(sitemap)

        if not content:
            continue

        if (
            "xml" not in content_type.lower()
            and not content.lstrip().startswith(b"<?xml")
            and b"<urlset" not in content[:500]
            and b"<sitemapindex" not in content[:1000]
        ):
            continue

        found_pages, children = parse_sitemap(content)
        page_urls.update(found_pages)

        for child in children:
            if child not in visited:
                sitemap_files.append(child)

        time.sleep(REQUEST_DELAY)

    print(f"Sitemap URLs discovered: {len(page_urls)}")
    return page_urls


def crawl():
    seed_urls = sitemap_urls()
    queue = deque(
        sorted(seed_urls) if seed_urls else [BASE_URL]
    )

    queued = set(queue)
    attempted = set()
    pages = {}
    failures = []
    html_responses = 0

    while queue and len(attempted) < MAX_PAGES:
        url = clean_url(queue.popleft())

        if not url or url in attempted:
            continue

        attempted.add(url)

        content, content_type = fetch(url)

        if not content:
            failures.append({
                "url": url,
                "reason": "fetch_failed"
            })
            continue

        if "html" not in content_type.lower():
            continue

        html_responses += 1
        parser = PageParser()

        try:
            parser.feed(content.decode("utf-8", "replace"))
            parser.close()
        except Exception as exc:
            failures.append({
                "url": url,
                "reason": f"parse_failed: {exc}"
            })
            continue

        title = " ".join(parser.title).strip() or url
        text = re.sub(
            r"\s+", " ", " ".join(parser.text)
        ).strip()

        if not text:
            failures.append({
                "url": url,
                "reason": "empty_text"
            })
            continue

        raw = content.decode("utf-8", "replace")

        language = (
            "de"
            if "/de/" in url.lower()
            or re.search(
                r'<html[^>]*\blang=["\']de',
                raw[:10000],
                re.I
            )
            else "en"
        )

        digest = hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()

        pages[url] = {
            "url": url,
            "title": title[:300],
            "text": text[:50000],
            "hash": digest,
            "language": language,
        }

        for href in parser.links:
            target = clean_url(urljoin(url, href))

            if (
                target
                and target not in attempted
                and target not in queued
            ):
                queue.append(target)
                queued.add(target)

        if REQUEST_DELAY:
            time.sleep(REQUEST_DELAY)

    limit_reached = bool(queue) and len(attempted) >= MAX_PAGES

    complete = (
        not limit_reached
        and not failures
        and bool(pages)
    )

    print(f"URLs attempted: {len(attempted)}")
    print(f"HTML responses: {html_responses}")
    print(f"Fetch/parse failures: {len(failures)}")
    print(f"Crawl complete: {complete}")

    if failures:
        print("Example failed URLs:")
        for failure in failures[:10]:
            print(
                f"  {failure['url']} ({failure['reason']})"
            )

    return pages, complete, failures, limit_reached


def load_previous():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            raise ValueError(
                "data.json root must be an object"
            )

        return data

    except FileNotFoundError:
        return {}

    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot safely read {DATA_FILE}: {exc}"
        ) from exc


def main():
    now = utc_now()
    previous_data = load_previous()
    previous_pages = previous_data.get("pages", {})

    if not isinstance(previous_pages, dict):
        raise RuntimeError(
            "Existing data.json has no valid 'pages' object."
        )

    current, crawl_complete, failures, limit_reached = crawl()

    if not current:
        raise RuntimeError(
            "No pages were captured. "
            "Existing monitoring data was not changed."
        )

    changes = []

    for url, page in current.items():
        old = previous_pages.get(url)

        if old is None:
            status = "new"
        elif old.get("hash") != page["hash"]:
            status = "changed"
        else:
            for key in ("change", "checked_at"):
                if key in old:
                    page[key] = old[key]
            continue

        page["change"] = status
        page["checked_at"] = now

        changes.append({
            "url": url,
            "title": page["title"],
            "language": page["language"],
            "status": status,
            "summary": (
                "Neue öffentliche Seite entdeckt."
                if status == "new"
                else "Der Seiteninhalt wurde geändert."
            ),
            "previous_text": (
                old.get("text", "") if old else ""
            )[:5000],
            "current_text": page["text"][:5000],
            "checked_at": now,
        })

    # Only report possible removals after a clean crawl.
    if crawl_complete and not limit_reached:
        for url in sorted(set(previous_pages) - set(current)):
            old = previous_pages[url]

            changes.append({
                "url": url,
                "title": old.get("title", url),
                "language": old.get("language", "en"),
                "status": "possibly_removed",
                "summary": (
                    "Seite im vollständigen Crawl nicht gefunden. "
                    "Löschung bitte manuell überprüfen."
                ),
                "previous_text": old.get("text", "")[:5000],
                "current_text": "",
                "checked_at": now,
            })

    old_history = previous_data.get("history", [])

    if not isinstance(old_history, list):
        old_history = []

    history = old_history + [{
        "checked_at": now,
        "pages_scanned": len(current),
        "changes_found": len(changes),
        "crawl_complete": crawl_complete,
        "limit_reached": limit_reached,
        "failed_fetches": len(failures),
        "changes": changes,
    }]

    history = history[-MAX_CHANGE_HISTORY:]

    # Preserve unseen old pages when the crawl is incomplete.
    stored_pages = dict(current)

    if not crawl_complete:
        for url, old_page in previous_pages.items():
            if url not in stored_pages:
                stored_pages[url] = old_page

    output = {
        "last_checked": now,
        "pages_scanned": len(current),
        "changes": changes,
        "history": history,
        "crawl_complete": crawl_complete,
        "limit_reached": limit_reached,
        "failed_fetches": len(failures),
        "pages": stored_pages,
    }

    temp_file = DATA_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(
            output,
            file,
            ensure_ascii=False,
            indent=2
        )

    os.replace(temp_file, DATA_FILE)

    print(f"Pages scanned: {len(current)}")
    print(f"Changes found: {len(changes)}")
    print(f"Crawl complete: {crawl_complete}")
    print(f"Limit reached: {limit_reached}")
    print(f"Failed fetches: {len(failures)}")


if __name__ == "__main__":
    main()
