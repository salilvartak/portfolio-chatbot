"""Rebuild the vector index:  python -m ingest.run  [--dry-run]"""

import argparse

from app.config import get_settings
from ingest.sources import load_all


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print chunks without embedding or writing")
    args = parser.parse_args()

    s = get_settings()
    chunks = load_all(s.site_url)
    print(f"Loaded {len(chunks)} chunks")
    for c in chunks:
        print(f"  [{c.source}] {len(c.text)} chars")
    if args.dry_run:
        return

    from app.embeddings import embed_documents
    from app.store import replace_chunks

    vectors = embed_documents([c.text for c in chunks])
    replace_chunks(chunks, vectors)
    print(f"Wrote {len(chunks)} chunks to Firestore collection '{s.chunks_collection}'")


if __name__ == "__main__":
    main()
