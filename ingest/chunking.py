"""Split text into retrieval-sized chunks, preferring markdown heading and paragraph boundaries."""

import re

MAX_CHARS = 1500
OVERLAP = 200


def split_markdown(text: str) -> list[tuple[str, str]]:
    """Return (heading, section_text) pairs, splitting on #, ## and ### headings."""
    sections: list[tuple[str, str]] = []
    heading, buf = "", []
    for line in text.splitlines():
        m = re.match(r"^#{1,3}\s+(.*)", line)
        if m:
            if "".join(buf).strip():
                sections.append((heading, "\n".join(buf).strip()))
            heading, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((heading, "\n".join(buf).strip()))
    return sections


def split_text(text: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[str]:
    """Pack paragraphs into chunks of at most max_chars, carrying a small overlap between chunks."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > max_chars:  # a single huge paragraph: hard-split it
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:max_chars])
            p = p[max_chars - overlap :]
        if cur and len(cur) + len(p) + 2 > max_chars:
            chunks.append(cur)
            cur = cur[-overlap:] + "\n\n" + p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks
