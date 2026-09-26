"""Build catalog.json: the vault pages a capture can be routed to.

Reads your vault READ-ONLY and writes, per page: name, a one-line description,
the frontmatter `purpose`/`description`, and its H2 headings. Page bodies are
never copied, so no page prose is sent to the TypeSafe API.

Two ways to find pages:
  1. An index note (default `_index.md`) with lines like
         - [[Page Name]] — one-line description
     grouped under `## Section` headings.
  2. No index note: every .md file in the vault becomes a page, described by its
     frontmatter `purpose` or `description` (or its first paragraph).

Usage:
    python build_catalog.py "/path/to/vault"
    python build_catalog.py "/path/to/vault" --exclude-folders Journal Daily --exclude-sections "Meta"
"""
import argparse
import json
import re
from pathlib import Path

LINE = re.compile(r"^- \[\[([^\]|#]+)(?:\|[^\]]*)?\]\]\s*[—–-]\s*(.+)$")
FOLDER_HINT = re.compile(r"\(`([^`]+)`\)\s*$")


def frontmatter_and_body(text: str):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[3:end], text[end + 4:]
    return "", text


def describe(path: Path):
    fm, body = frontmatter_and_body(path.read_text(encoding="utf-8", errors="ignore"))
    m = re.search(r"^(?:purpose|description):\s*(.+)$", fm, re.M)
    purpose = m.group(1).strip().strip('"') if m else ""
    headings = [h.strip() for h in re.findall(r"^##\s+(.+)$", body, re.M)][:12]
    first_para = next((p.strip() for p in re.split(r"\n\s*\n", body)
                       if p.strip() and not p.lstrip().startswith(("#", ">", "-", "!"))), "")
    return purpose, headings, first_para[:200]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vault")
    ap.add_argument("--index", default="_index.md", help="index note inside the vault (skipped if missing)")
    ap.add_argument("--out", default=str(Path(__file__).with_name("catalog.json")))
    ap.add_argument("--exclude-folders", nargs="*", default=[],
                    help="top-level vault folders whose pages are never routing targets (e.g. Journal Personal)")
    ap.add_argument("--exclude-sections", nargs="*", default=[],
                    help="index sections that are not routing targets (e.g. meta or to-do lists)")
    ap.add_argument("--skip-folders", nargs="*", default=["Sources", "Templates", "Assets"],
                    help="folders ignored entirely when looking up page files")
    args = ap.parse_args()

    vault = Path(args.vault).expanduser()
    paths = {}  # page name -> file, one in-process walk
    for p in vault.rglob("*.md"):
        rel = p.relative_to(vault)
        if rel.parts[0].startswith(".") or rel.parts[0] in args.skip_folders or p.name.startswith("_"):
            continue
        paths.setdefault(p.stem, p)

    pages, skipped, seen = [], [], set()
    index = vault / args.index

    def add(name, section, blurb, path):
        rel = path.relative_to(vault).parts if path else ()
        folder = rel[0] if len(rel) > 1 else ""
        if name in seen:
            return
        seen.add(name)
        if folder in args.exclude_folders or section in args.exclude_sections:
            skipped.append(name)
            return
        purpose, headings, first_para = describe(path) if path else ("", [], "")
        pages.append({"name": name, "section": section, "folder": folder,
                      "blurb": blurb or purpose or first_para or name,
                      "purpose": purpose if blurb else "", "headings": headings, "found": bool(path)})

    if index.exists():
        section = ""
        for raw in index.read_text(encoding="utf-8").splitlines():
            if raw.startswith("## "):
                section = raw[3:].strip()
                continue
            m = LINE.match(raw.strip())
            if m:
                name = m.group(1).strip()
                add(name, section, FOLDER_HINT.sub("", m.group(2).strip()).strip(), paths.get(name))
        mode = f"index {args.index}"
    else:
        for name, path in sorted(paths.items()):
            add(name, "", "", path)
        mode = "all notes (no index found)"

    Path(args.out).write_text(json.dumps(pages, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(pages)} pages from {mode} -> {args.out}  ({len(skipped)} excluded, "
          f"{sum(not p['found'] for p in pages)} index entries without a file)")


if __name__ == "__main__":
    main()
