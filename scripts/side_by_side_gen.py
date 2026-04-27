#!/usr/bin/env python3
"""Generate a side-by-side HTML comparison of matching Markdown files from two directories."""

import argparse
import re
import sys
from pathlib import Path

from markdown_it import MarkdownIt


HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: sans-serif; background: #f5f5f5; }}
  h1.page-title {{ padding: 16px 24px; background: #222; color: #fff; font-size: 1.2rem; }}
  .toc {{ padding: 12px 24px; background: #333; }}
  .toc a {{ color: #adf; text-decoration: none; margin-right: 16px; font-size: 0.9rem; }}
  .toc a:hover {{ text-decoration: underline; }}

  /* File-level block */
  .file-block {{ border-bottom: 4px solid #555; margin-bottom: 4px; }}
  .file-header {{
    display: flex; background: #444; color: #eee;
    font-size: 0.9rem; font-weight: bold;
  }}
  .file-header span {{
    flex: 1; padding: 8px 14px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .file-header span:first-child {{ border-right: 1px solid #666; }}

  /* Section row inside a file block */
  .sec-row {{ display: flex; border-top: 1px solid #ddd; }}
  .sec-row:first-child {{ border-top: none; }}
  .pane {{
    flex: 1; padding: 16px 20px;
    background: #fff; overflow-x: auto;
    border-right: 2px solid #e0e0e0;
    /* align tops — each pane is independent height */
    align-self: start;
  }}
  .pane:last-child {{ border-right: none; }}
  .pane h1, .pane h2, .pane h3, .pane h4 {{ margin: 0.6em 0 0.3em; }}
  .pane h1:first-child, .pane h2:first-child,
  .pane h3:first-child, .pane h4:first-child {{ margin-top: 0; }}
  .pane p {{ margin: 0.45em 0; line-height: 1.6; }}
  .pane pre {{ background: #f4f4f4; padding: 10px; overflow-x: auto; border-radius: 4px; }}
  .pane code {{ background: #f4f4f4; padding: 1px 4px; border-radius: 3px; font-size: 0.88em; }}
  .pane pre code {{ background: none; padding: 0; }}
  .pane ul, .pane ol {{ margin: 0.4em 0 0.4em 1.4em; }}
  .pane table {{ border-collapse: collapse; width: 100%; }}
  .pane th, .pane td {{ border: 1px solid #ccc; padding: 5px 9px; }}
  .pane th {{ background: #f0f0f0; }}
  .missing {{ color: #aaa; font-style: italic; }}
  .sec-label {{
    font-size: 0.72rem; font-weight: bold; letter-spacing: 0.03em;
    color: #888; text-transform: uppercase; margin-bottom: 8px;
    border-bottom: 1px solid #eee; padding-bottom: 4px;
  }}
</style>
</head>
<body>
<h1 class="page-title">{title}</h1>
<div class="toc">{toc}</div>
{file_blocks}
</body>
</html>
"""

FILE_BLOCK_TEMPLATE = """\
<div class="file-block" id="{anchor}">
  <div class="file-header">
    <span>{left_label}</span><span>{right_label}</span>
  </div>
  {sec_rows}
</div>
"""

SEC_ROW_TEMPLATE = """\
<div class="sec-row">
  <div class="pane">{left_html}</div>
  <div class="pane">{right_html}</div>
</div>
"""

_md = MarkdownIt("commonmark").enable("table")

# Split on H1/H2 headings; H3+ stay inside their section body.
_HEADING_RE = re.compile(r"^(#{1,2} .+)$", re.MULTILINE)


def md_to_html(text: str) -> str:
    return _md.render(text)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def split_sections(text: str) -> list[tuple[str, str]]:
    """Return [(heading_line, body_text), ...].

    A leading chunk before the first heading gets an empty heading key.
    Heading line is included in the rendered body so the level is preserved.
    """
    parts = _HEADING_RE.split(text)
    sections: list[tuple[str, str]] = []
    # parts[0] is pre-heading text; then alternating heading / body
    if parts[0].strip():
        sections.append(("", parts[0]))
    for i in range(1, len(parts), 2):
        heading = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections.append((heading, heading + "\n" + body))
    return sections


def pair_sections(
    left: list[tuple[str, str]], right: list[tuple[str, str]]
) -> list[tuple[str | None, str | None, str | None]]:
    """Return [(heading, left_body, right_body)] matched by heading text.

    Order: left-file order first, then right-only sections appended.
    """
    right_map = {h: b for h, b in right}
    seen: set[str] = set()
    rows: list[tuple[str | None, str | None, str | None]] = []

    for heading, left_body in left:
        right_body = right_map.get(heading)
        rows.append((heading, left_body, right_body))
        seen.add(heading)

    for heading, right_body in right:
        if heading not in seen:
            rows.append((heading, None, right_body))

    return rows


def render_pane(body: str | None) -> str:
    if body is None:
        return '<span class="missing">Section not present</span>'
    return md_to_html(body)


def build_page(left_dir: Path, right_dir: Path) -> str:
    left_mds = {p.name: p for p in left_dir.glob("*.md")}
    right_mds = {p.name: p for p in right_dir.glob("*.md")}
    common = sorted(left_mds.keys() & right_mds.keys())
    left_only = sorted(left_mds.keys() - right_mds.keys())
    right_only = sorted(right_mds.keys() - left_mds.keys())
    all_files = common + left_only + right_only

    if not all_files:
        print("No Markdown files found.", file=sys.stderr)
        sys.exit(1)

    title = f"{left_dir.name} vs {right_dir.name}"
    toc_links = " ".join(
        f'<a href="#{slugify(name)}">{name}</a>' for name in all_files
    )

    file_blocks = []
    for name in all_files:
        anchor = slugify(name)
        left_path = left_mds.get(name)
        right_path = right_mds.get(name)

        left_label = f"{left_dir.name}/{name}" if left_path else f"{left_dir.name} — missing"
        right_label = f"{right_dir.name}/{name}" if right_path else f"{right_dir.name} — missing"

        left_sections = split_sections(left_path.read_text(encoding="utf-8")) if left_path else []
        right_sections = split_sections(right_path.read_text(encoding="utf-8")) if right_path else []

        if not left_sections and not right_sections:
            pairs = [(None, None, None)]
        elif not left_sections:
            pairs = [(h, None, b) for h, b in right_sections]
        elif not right_sections:
            pairs = [(h, b, None) for h, b in left_sections]
        else:
            pairs = pair_sections(left_sections, right_sections)

        sec_rows = "\n  ".join(
            SEC_ROW_TEMPLATE.format(
                left_html=render_pane(lb),
                right_html=render_pane(rb),
            )
            for _, lb, rb in pairs
        )

        file_blocks.append(
            FILE_BLOCK_TEMPLATE.format(
                anchor=anchor,
                left_label=left_label,
                right_label=right_label,
                sec_rows=sec_rows,
            )
        )

    return HTML_TEMPLATE.format(
        title=title,
        toc=toc_links,
        file_blocks="\n".join(file_blocks),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a side-by-side HTML comparison of Markdown files."
    )
    parser.add_argument("left", type=Path, help="First directory")
    parser.add_argument("right", type=Path, help="Second directory")
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=Path("."),
        help="Directory to write the HTML file into (default: current dir)",
    )
    args = parser.parse_args()

    left_dir = args.left.resolve()
    right_dir = args.right.resolve()
    for d in (left_dir, right_dir):
        if not d.is_dir():
            print(f"Not a directory: {d}", file=sys.stderr)
            sys.exit(1)

    html = build_page(left_dir, right_dir)
    out_name = f"{left_dir.name}_vs_{right_dir.name}.html"
    out_path = args.output_dir / out_name
    out_path.write_text(html, encoding="utf-8")
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
