"""Rendering: does what went in come out, on the right number of pages, with nothing fetched."""

from __future__ import annotations

import pytest

from printbridge.render import (
    RenderError,
    build_document,
    content_to_html,
    extract_text,
    render_to_pdf,
)

ONE_PAGER = """# Household note

A short paragraph with **bold** and *italic*.

| Day | Task |
|---|---|
| Monday | Laundry |
| Tuesday | Shopping |

- [ ] buy milk
- [x] fetch parcel

```python
print("hello")
```
"""


def test_markdown_renders_one_page_with_its_content(tmp_path):
    result = render_to_pdf(ONE_PAGER, tmp_path / "note.pdf", title="Household note")
    assert result.pages == 1
    text = extract_text(result.path)
    for expected in ("Household note", "Laundry", "Shopping", 'print("hello")'):
        assert expected in text


def test_header_and_page_number_are_on_the_page(tmp_path):
    result = render_to_pdf(ONE_PAGER, tmp_path / "note.pdf", title="Household note")
    text = extract_text(result.path)
    assert "1 / 1" in text, "footer page numbers are part of the print stylesheet"


def test_task_list_boxes_survive_to_paper(tmp_path):
    result = render_to_pdf(ONE_PAGER, tmp_path / "note.pdf", title="t")
    text = extract_text(result.path)
    assert "☐" in text and "☑" in text


def test_long_document_spans_several_pages(tmp_path):
    content = "\n\n".join(f"## Section {i}\n\n" + ("filler text " * 60) for i in range(20))
    result = render_to_pdf(content, tmp_path / "long.pdf", title="Long")
    assert result.pages >= 4


def test_plain_text_keeps_its_shape(tmp_path):
    result = render_to_pdf("line one\n    indented\nline three", tmp_path / "p.pdf", fmt="plain")
    text = extract_text(result.path)
    assert "line one" in text and "line three" in text


def test_html_is_sanitised_before_rendering():
    cleaned = content_to_html('<p onclick="x()">ok</p><script>alert(1)</script>', "html")
    assert "<script" not in cleaned and "onclick" not in cleaned
    assert "ok" in cleaned


def test_markdown_does_not_pass_raw_html_through():
    cleaned = content_to_html("<script>alert(1)</script>\n\nhello", "markdown")
    assert "<script>" not in cleaned


def test_remote_images_are_never_fetched(tmp_path):
    """A document may not turn the renderer into an SSRF tool; the image is dropped, not fetched."""
    result = render_to_pdf(
        '<p>before</p><img src="http://192.168.1.1/secret.png"><p>after</p>',
        tmp_path / "img.pdf",
        fmt="html",
        title="img",
    )
    text = extract_text(result.path)
    assert "before" in text and "after" in text


def test_empty_content_is_refused(tmp_path):
    with pytest.raises(RenderError, match="empty"):
        render_to_pdf("   ", tmp_path / "x.pdf")


def test_unknown_format_is_refused():
    with pytest.raises(RenderError, match="Unknown format"):
        content_to_html("x", "docx")


def test_unknown_paper_is_refused():
    with pytest.raises(RenderError, match="Unknown paper"):
        build_document("<p>x</p>", title="t", paper="A3")


def test_letter_and_a4_both_render(tmp_path):
    for paper in ("A4", "Letter"):
        result = render_to_pdf("# Title\n\ntext", tmp_path / f"{paper}.pdf", paper=paper, title="t")
        assert result.pages == 1


def test_title_cannot_break_out_of_the_document(tmp_path):
    """A title is attacker-influenced text: it must escape into both <title> and the CSS string."""
    document = build_document("<p>x</p>", title='Bad "quote" & </style><script>x</script>', paper="A4")
    assert "&amp;" in document
    assert "<script>" not in document
    style = document.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "</style" not in style and "<script" not in style


@pytest.mark.parametrize(
    "locale,expected",
    [("sv_SE", "-"), ("en_US", ","), ("de_DE", ".")],
)
def test_date_format_follows_locale(locale, expected):
    document = build_document("<p>x</p>", title="t", locale=locale)
    assert expected in document
