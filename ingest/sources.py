"""Load knowledge from the portfolio's Firestore data and from markdown files in content/."""

import hashlib
import re
from pathlib import Path

from app.store import Chunk, get_db
from ingest.chunking import split_markdown, split_text

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"


def _id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


def project_chunks(projects: list[dict]) -> list[Chunk]:
    chunks = []
    for p in projects:
        title = p.get("title", "Untitled project")
        lines = [f"Project: {title}"]
        for key, label in [("kind", "Type"), ("status", "Status"), ("tagline", "Summary")]:
            if p.get(key):
                lines.append(f"{label}: {p[key]}")
        if p.get("stack"):
            lines.append("Tech stack: " + ", ".join(p["stack"]))
        for h in p.get("highlights", []):
            lines.append(f"- {h.get('bold', '')} {h.get('text', '')}".strip())
        for link in p.get("links", []):
            lines.append(f"Link ({link.get('label', '')}): {link.get('href', '')}")
        url = next((l.get("href", "") for l in p.get("links", []) if l.get("href")), "")
        for i, part in enumerate(split_text("\n".join(lines))):
            chunks.append(Chunk(_id("project", title, str(i)), part, f"Project: {title}", url, {"type": "project"}))
    return chunks


def blog_chunks(posts: list[dict], site_url: str = "") -> list[Chunk]:
    chunks = []
    for post in posts:
        title, slug = post.get("title", "Untitled post"), post.get("slug", "")
        url = f"{site_url.rstrip('/')}/blog/{slug}" if site_url and slug else ""
        header = f"Blog post by the portfolio owner: {title} ({post.get('date', '')})"
        if post.get("tags"):
            header += "\nTags: " + ", ".join(post["tags"])
        texts = [f"{header}\nSummary: {post.get('excerpt', '')}"]
        for heading, section in split_markdown(post.get("body") or ""):
            for part in split_text(section):
                texts.append(f"{title}{' > ' + heading if heading else ''}\n{part}")
        for i, t in enumerate(texts):
            chunks.append(Chunk(_id("blog", slug or title, str(i)), t, f"Blog: {title}", url, {"type": "blog"}))
    return chunks


def markdown_chunks(path: Path) -> list[Chunk]:
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)  # drop author notes
    h1 = re.search(r"^#\s+(.+)$", text, flags=re.M)
    doc_title = h1.group(1).strip() if h1 else path.stem.replace("-", " ").replace("_", " ").title()
    chunks = []
    for heading, section in split_markdown(text):
        if heading == doc_title:
            heading = ""
        label = f"{doc_title}: {heading}" if heading else doc_title
        for i, part in enumerate(split_text(section)):
            chunks.append(Chunk(_id("doc", path.name, heading, str(i)), f"{label}\n{part}", label, "", {"type": "doc"}))
    return chunks


def load_all(site_url: str = "") -> list[Chunk]:
    db = get_db()
    projects = [d.to_dict() for d in db.collection("projects").order_by("order").stream()]
    blog_doc = db.collection("blog").document("list").get()
    posts = (blog_doc.to_dict() or {}).get("posts", []) if blog_doc.exists else []

    chunks = project_chunks(projects) + blog_chunks(posts, site_url)
    for path in sorted(CONTENT_DIR.glob("*.md")):
        if path.name.lower() != "readme.md":
            chunks += markdown_chunks(path)
    return chunks
