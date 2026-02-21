#!/usr/bin/env python3
"""
Fetch The Verge RSS, rewrite the latest article in style from style.md via OpenRouter,
save to content/*.md. Skips if the article was already processed.
"""
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import urlopen, Request
from html import unescape

import json

# Namespaces for Atom RSS (default NS in The Verge feed)
ATOM = "http://www.w3.org/2005/Atom"
ATOM_NS = {"atom": ATOM}


def fetch_rss(url: str) -> str:
    with urlopen(url, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def _find(el, path_with_ns: str, path_fallback: str):
    if el is None:
        return None
    ns_path = path_with_ns.replace("atom:", "{" + ATOM + "}")
    found = el.find(ns_path)
    if found is not None:
        return found
    return el.find(path_fallback)


def _findall(root, path_with_ns: str, path_fallback: str):
    ns_path = path_with_ns.replace("atom:", "{" + ATOM + "}")
    lst = root.findall(".//" + ns_path)
    if lst:
        return lst
    return root.findall(".//" + path_fallback)


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    entries = []
    for entry in _findall(root, "atom:entry", "entry"):
        def text(el):
            if el is None:
                return ""
            return (el.text or "") + "".join(ET.tostring(c, encoding="unicode", method="text") for c in el)

        title_el = _find(entry, "atom:title", "title")
        link_el = None
        for tag in ["{" + ATOM + "}link", "link"]:
            for el in entry.findall(".//" + tag):
                if el.get("rel") == "alternate" or el.get("rel") is None:
                    link_el = el
                    break
            if link_el is not None:
                break
        if link_el is None:
            link_el = _find(entry, "atom:link", "link")
        link = link_el.get("href", "") if link_el is not None else ""
        id_el = _find(entry, "atom:id", "id")
        updated_el = _find(entry, "atom:updated", "updated") or _find(entry, "atom:published", "published")
        content_el = _find(entry, "atom:content", "content")
        summary_el = _find(entry, "atom:summary", "summary")
        author_el = _find(entry, "atom:author/atom:name", "author/name")
        if author_el is None:
            author_el = _find(entry, "atom:author/name", "author/name")

        title = unescape(text(title_el)).strip() if title_el is not None else ""
        entry_id = text(id_el).strip() if id_el is not None else link
        updated = text(updated_el).strip() if updated_el is not None else ""
        content = (content_el.get("content") or text(content_el)) if content_el is not None else ""
        summary = (summary_el.get("content") or text(summary_el)) if summary_el is not None else ""
        author = text(author_el).strip() if author_el is not None else ""

        # Prefer summary if content is huge
        body = summary or content
        if not body and content:
            body = content
        # Strip HTML tags for prompt (keep first ~12k chars to stay within context)
        body_plain = re.sub(r"<[^>]+>", " ", body)
        body_plain = re.sub(r"\s+", " ", body_plain).strip()[:12000]

        entries.append({
            "id": entry_id,
            "title": title,
            "link": link,
            "updated": updated,
            "author": author,
            "body_plain": body_plain,
            "summary": summary,
        })
    return entries


def slug_from_title(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", title.lower())
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return s[:60] or "article"


def date_prefix(updated: str) -> str:
    # ISO date like 2026-02-21T08:10:42+00:00 -> 2026-02-21
    m = re.match(r"(\d{4}-\d{2}-\d{2})", updated)
    return m.group(1) if m else ""


def already_processed(content_dir: Path, source_url: str) -> bool:
    for f in content_dir.glob("*.md"):
        try:
            raw = f.read_text(encoding="utf-8")
            if raw.startswith("---"):
                end = raw.index("---", 3) if "---" in raw[3:] else -1
                if end > 0 and source_url in raw[:end]:
                    return True
        except Exception:
            continue
    return False


def rewrite_with_openrouter(style_md: str, title: str, body: str, api_key: str) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    system = f"""You are a writer. Rewrite the given article in the following style and voice. Output only the rewritten article body in Markdown, no meta-commentary.

{style_md}

Additional instructions:
- Preserve the factual content and meaning; change only style and wording.
- Apply SEO: natural keyword placement, clear headings (##), and a concise meta-friendly tone where appropriate.
- Highlight key SEO terms and important phrases in the body by wrapping them in **bold** so they stand out in the text.
- Output valid Markdown only (no YAML block at the start for this task)."""

    user = f"Article to rewrite:\n\n# {title}\n\n{body}"

    payload = {
        "model": "stepfun/step-3.5-flash:free",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 4096,
        "temperature": 0.6,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/rssgit",
    }
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content = msg.get("content") or ""
    if not content:
        raise RuntimeError("Empty response from OpenRouter: " + json.dumps(data)[:500])
    return content.strip()


def main():
    repo_root = Path(__file__).resolve().parent.parent
    content_dir = repo_root / "content"
    style_path = repo_root / "style.md"
    content_dir.mkdir(exist_ok=True)

    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPEN_ROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY (or OPEN_ROUTER_API_KEY) not set", file=sys.stderr)
        sys.exit(1)

    style_md = style_path.read_text(encoding="utf-8")

    rss_url = "https://www.theverge.com/rss/index.xml"
    print("Fetching RSS...")
    xml_text = fetch_rss(rss_url)
    entries = parse_feed(xml_text)
    if not entries:
        print("No entries in feed")
        sys.exit(0)

    entry = entries[0]
    if already_processed(content_dir, entry["link"]):
        print("Latest article already processed:", entry["link"])
        sys.exit(0)

    print("Rewriting:", entry["title"][:60], "...")
    body_md = rewrite_with_openrouter(
        style_md, entry["title"], entry["body_plain"], api_key
    )

    slug = slug_from_title(entry["title"])
    date_prefix_str = date_prefix(entry["updated"])
    filename = f"{date_prefix_str}-{slug}.md" if date_prefix_str else f"{slug}.md"
    out_path = content_dir / filename

    title_esc = entry["title"].replace('"', '\\"')
    author_esc = entry["author"].replace('"', '\\"')
    frontmatter = f"""---
title: "{title_esc}"
source_url: "{entry["link"]}"
source_id: "{entry["id"]}"
date: "{entry["updated"]}"
author: "{author_esc}"
---

"""
    out_path.write_text(frontmatter + body_md, encoding="utf-8")
    print("Wrote", out_path)


if __name__ == "__main__":
    main()
