# Knowledge documents

Put Markdown files here (e.g. `about.md`, `faq.md`). Every `*.md` file except this README is indexed.

Use `#`/`##` headings to separate topics. Each section becomes its own retrievable chunk, so
headings like "Work experience", "What I'm looking for", "Contact" make answers more accurate.

Each section also becomes a clickable follow-up question in the chat. Headings that are already
questions (like the FAQ) are used as-is. For other sections, add a note right under the heading:

    ## Music
    <!-- ask: What music does he like? -->

Without one, the bot falls back to "Tell me about <heading>".

Projects and blog posts are pulled from Firestore automatically; you don't need to repeat them here.

After editing, rebuild the index: `python -m ingest.run`
