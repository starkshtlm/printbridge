"""markdown / plain text / HTML → PDF, and the page count that every guard depends on.

The order is always render → count → spool, never the other way round: a page limit that is
checked after the paper has moved is not a limit.

Two things this module refuses to do:

* fetch anything. WeasyPrint gets a URL fetcher that allows `data:` URIs and nothing else, so a
  document containing `<img src="http://192.168.1.1/...">` cannot turn the renderer into an SSRF
  tool. Remote images are dropped, not fetched.
* trust HTML. `format="html"` goes through a bleach allowlist first — no scripts, no styles, no
  event handlers, no objects.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import bleach
from markdown_it import MarkdownIt
from pypdf import PdfReader
from weasyprint import CSS, HTML
from weasyprint.urls import URLFetchingError

_TEMPLATE_DIR = Path(__file__).parent / "templates"

PAGE_SIZES = {"A4": "A4", "Letter": "Letter"}

ALLOWED_TAGS = [
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "col", "colgroup", "dd", "del",
    "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    "i", "img", "ins", "kbd", "li", "mark", "ol", "p", "pre", "q", "s", "samp", "small", "span",
    "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
]
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["colspan", "rowspan", "align"],
    "th": ["colspan", "rowspan", "align", "scope"],
    "col": ["span"],
    "*": ["lang"],
}
ALLOWED_PROTOCOLS = ["http", "https", "mailto", "data"]


class RenderError(ValueError):
    """The document could not be rendered; the message is written for the assistant."""


@dataclass(frozen=True)
class RenderResult:
    path: Path
    pages: int
    title: str
    bytes_written: int


def _local_only_fetcher(url: str) -> dict:
    """Allow `data:` URIs; refuse every network fetch during rendering."""
    if url.startswith("data:"):
        from weasyprint.urls import default_url_fetcher

        return default_url_fetcher(url)
    raise URLFetchingError(
        f"printbridge does not fetch remote resources while rendering ({url[:40]}…). "
        "Embed images as data: URIs, or print the page with print_url instead."
    )


def _markdown_parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"typographer": True, "html": False, "linkify": False})
    md.enable(["table", "strikethrough"])
    return md


def _task_list_glyphs(text: str) -> str:
    """`- [ ]` / `- [x]` become printable boxes; markdown-it core has no task-list rule."""
    text = re.sub(r"(?m)^(\s*[-*+]\s+)\[[ ]\]\s+", r"\1☐ ", text)
    return re.sub(r"(?m)^(\s*[-*+]\s+)\[[xX]\]\s+", r"\1☑ ", text)


def content_to_html(content: str, fmt: str = "markdown") -> str:
    """The body fragment for the template. `fmt` is markdown | plain | html."""
    fmt = (fmt or "markdown").lower()
    if fmt in {"markdown", "md"}:
        return _markdown_parser().render(_task_list_glyphs(content))
    if fmt in {"plain", "text", "txt"}:
        return f'<pre class="pb-plain">{html_module.escape(content)}</pre>'
    if fmt == "html":
        return bleach.clean(
            content,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
            protocols=ALLOWED_PROTOCOLS,
            strip=True,
            strip_comments=True,
        )
    raise RenderError(f"Unknown format {fmt!r}. Use markdown, plain or html.")


def _format_date(when: datetime, locale: str) -> str:
    """Locale-aware enough to be right where it matters: day-first vs month-first."""
    loc = (locale or "").lower()
    if loc.startswith(("en_us", "en-us")):
        return when.strftime("%b %-d, %Y %H:%M")
    if loc.startswith(("de", "fr", "es", "it", "nl", "pt", "pl")):
        return when.strftime("%d.%m.%Y %H:%M")
    return when.strftime("%Y-%m-%d %H:%M")  # ISO — the sv_SE default and a safe fallback


def build_document(
    body_html: str,
    *,
    title: str,
    paper: str = "A4",
    locale: str = "sv_SE",
    when: datetime | None = None,
) -> str:
    """Wrap a body fragment in the print template. Returns a complete HTML document."""
    if paper not in PAGE_SIZES:
        raise RenderError(f"Unknown paper {paper!r}. Use one of: {', '.join(PAGE_SIZES)}.")
    css = (_TEMPLATE_DIR / "document.css").read_text("utf-8")
    shell = (_TEMPLATE_DIR / "document.html").read_text("utf-8")
    safe_title = html_module.escape(title or "")
    # The header title is substituted into a CSS string inside <style>: escape the quote, the
    # backslash, and "<" (as a CSS escape) so a title containing "</style>" cannot break out.
    css_title = (
        (title or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("<", "\\3c ")
        .replace(">", "\\3e ")
    )
    css = (
        css.replace("@PAGE_SIZE@", PAGE_SIZES[paper])
        .replace("@TITLE@", css_title)
        .replace("@DATE@", _format_date(when or datetime.now(), locale))
    )
    lang = (locale or "en").split("_")[0].split("-")[0] or "en"
    return (
        shell.replace("@CSS@", css)
        .replace("@TITLE@", safe_title)
        .replace("@LANG@", html_module.escape(lang))
        .replace("@BODY@", body_html)
    )


def count_pages(pdf_path: Path) -> int:
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception as exc:  # noqa: BLE001 - a corrupt PDF is a render failure, not a crash
        raise RenderError(f"Could not read the rendered PDF ({exc}).") from exc


def render_to_pdf(
    content: str,
    out_path: Path,
    *,
    fmt: str = "markdown",
    title: str = "",
    paper: str = "A4",
    locale: str = "sv_SE",
) -> RenderResult:
    """Render `content` to `out_path` and report how many pages came out."""
    if content is None or not str(content).strip():
        raise RenderError("Nothing to print — `content` is empty.")
    document = build_document(
        content_to_html(str(content), fmt), title=title, paper=paper, locale=locale
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        HTML(string=document, url_fetcher=_local_only_fetcher, base_url=None).write_pdf(
            str(out_path), stylesheets=[CSS(string="")]
        )
    except Exception as exc:  # noqa: BLE001
        raise RenderError(f"Rendering failed: {exc}") from exc
    pages = count_pages(out_path)
    return RenderResult(
        path=out_path, pages=pages, title=title, bytes_written=out_path.stat().st_size
    )


def pdf_page_count(data: bytes, tmp_path: Path) -> int:
    """Page count for a PDF we did not render ourselves (print_file / print_url passthrough)."""
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_bytes(data)
    return count_pages(tmp_path)


def extract_text(pdf_path: Path) -> str:
    """Used by the golden tests: what actually ended up on the page."""
    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)
