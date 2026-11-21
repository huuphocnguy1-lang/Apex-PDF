import os
import sys

import fitz
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from pdf_core import PdfDocument, parse_page_ranges  # noqa: E402


def make_pdf(path, pages=3, with_form=False):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"Hello page {i + 1}", fontsize=14, fontname="helv")
        page.insert_text((72, 140), "Steel coil 1250 mm", fontsize=12, fontname="tiro")
    if with_form:
        page = doc[0]
        w = fitz.Widget()
        w.field_name = "name"
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.rect = fitz.Rect(72, 200, 300, 220)
        page.add_widget(w)
        c = fitz.Widget()
        c.field_name = "agree"
        c.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
        c.rect = fitz.Rect(72, 240, 90, 258)
        page.add_widget(c)
    doc.save(path)
    doc.close()
    return path


def png_bytes():
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 10), 1)
    pix.clear_with(0)
    return pix.tobytes("png")


@pytest.fixture
def pdf(tmp_path):
    p = make_pdf(str(tmp_path / "in.pdf"))
    d = PdfDocument(p)
    yield d
    d.close()


def test_open_render_search(pdf):
    assert pdf.page_count == 3
    pix = pdf.render(0, 1.0)
    assert pix.width > 500
    hits = pdf.search("Steel")
    assert len(hits) == 3
    assert hits[0][0] == 0


def test_highlight_and_undo_redo(pdf):
    n = pdf.highlight(0, fitz.Rect(60, 80, 300, 110))
    assert n >= 2
    assert len(list(pdf.page(0).annots())) == 1
    assert pdf.undo()
    assert len(list(pdf.page(0).annots())) == 0
    assert pdf.redo()
    assert len(list(pdf.page(0).annots())) == 1


def test_other_annotations(pdf):
    pdf.underline(0, fitz.Rect(60, 125, 300, 145))
    pdf.strikeout(1, fitz.Rect(60, 125, 300, 145))
    pdf.ink(0, [[(100, 300), (150, 320), (200, 300)]])
    pdf.rectangle(0, fitz.Rect(100, 400, 200, 450))
    pdf.text_box(0, fitz.Rect(100, 500, 300, 540), "Checked OK")
    pdf.note(0, 400, 100, "remark")
    types = sorted(a.type[1] for a in pdf.page(0).annots())
    assert types == sorted(["Underline", "Ink", "Square", "FreeText", "Text"])
    assert pdf.delete_annot_at(0, 150, 425)
    assert len(list(pdf.page(0).annots())) == 4


def test_edit_text(pdf):
    span = pdf.span_at(0, 100, 95)
    assert span and span["text"].startswith("Hello page 1")
    pdf.replace_span_text(0, span, "Bonjour page 1")
    text = pdf.page(0).get_text()
    assert "Bonjour page 1" in text
    assert "Hello page 1" not in text
    assert "Steel coil" in text  # neighbouring line untouched


def test_edit_text_delete(pdf):
    span = pdf.span_at(0, 100, 136)
    pdf.replace_span_text(0, span, "")
    assert "Steel coil" not in pdf.page(0).get_text()
    assert "Hello page 1" in pdf.page(0).get_text()


def test_whiteout(pdf):
    pdf.erase_area(0, fitz.Rect(60, 80, 400, 110))
    assert "Hello" not in pdf.page(0).get_text()


def test_signature_image(pdf):
    pdf.insert_image(0, fitz.Rect(300, 600, 450, 650), png_bytes())
    assert len(pdf.page(0).get_images()) == 1


def test_forms(tmp_path):
    d = PdfDocument(make_pdf(str(tmp_path / "f.pdf"), with_form=True))
    fields = d.form_fields()
    names = {f["name"]: f for f in fields}
    assert set(names) == {"name", "agree"}
    d.set_form_values({names["name"]["xref"]: "Farah", names["agree"]["xref"]: True})
    vals = {w.field_name: w.field_value for w in d.page(0).widgets()}
    assert vals["name"] == "Farah"
    assert vals["agree"] not in ("Off", "", None, False)
    out = str(tmp_path / "filled.pdf")
    d.save(out)
    d.close()
    d2 = PdfDocument(out)
    assert {w.field_name: w.field_value for w in d2.page(0).widgets()}["name"] == "Farah"
    d2.flatten_forms()
    assert not list(d2.page(0).widgets())
    d2.close()


def test_pages(pdf, tmp_path):
    pdf.rotate_page(0, 90)
    assert pdf.page(0).rotation == 90
    pdf.move_page(0, 2)
    assert "Hello page 2" in pdf.page(0).get_text()
    assert "Hello page 1" in pdf.page(2).get_text()
    pdf.move_page(2, 0)
    assert "Hello page 1" in pdf.page(0).get_text()
    pdf.insert_blank(0)
    assert pdf.page_count == 4 and pdf.page(1).get_text().strip() == ""
    pdf.duplicate_page(0)
    assert pdf.page_count == 5
    pdf.delete_pages([1, 2])
    assert pdf.page_count == 3
    other = make_pdf(str(tmp_path / "other.pdf"), pages=2)
    assert pdf.insert_pdf(other, after=pdf.page_count - 1) == 2
    assert pdf.page_count == 5
    out = str(tmp_path / "ex.pdf")
    pdf.extract_pages([0, 4], out)
    assert fitz.open(out).page_count == 2
    parts = pdf.split_every(2, str(tmp_path / "part.pdf"))
    assert len(parts) == 3


def test_annotations_on_rotated_page(pdf):
    pdf.rotate_page(0, 90)
    # display coords of a rotated page: highlight should still find words
    page = pdf.page(0)
    from pdf_core import to_display_rect
    words = page.get_text("words")
    disp = to_display_rect(page, fitz.Rect(words[0][:4]))
    assert pdf.highlight(0, disp) >= 1


def test_save_overwrite(tmp_path):
    p = make_pdf(str(tmp_path / "s.pdf"))
    d = PdfDocument(p)
    d.highlight(0, fitz.Rect(60, 80, 300, 110))
    d.save()  # overwrite the open file
    assert not d.modified
    d.close()
    assert len(list(fitz.open(p)[0].annots())) == 1


def test_page_ranges():
    assert parse_page_ranges("1-3, 5, 8-", 9) == [0, 1, 2, 4, 7, 8]
    with pytest.raises(ValueError):
        parse_page_ranges("0-2", 5)
    with pytest.raises(ValueError):
        parse_page_ranges("", 5)
