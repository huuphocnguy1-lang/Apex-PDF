"""PDF editing logic for Apex PDF (no GUI code here, so it can be tested).

All functions that take coordinates expect *display* coordinates in PDF points
(i.e. the page as it is shown, rotation applied). They convert to the
unrotated page space that PyMuPDF annotation/redaction methods need.
"""

from __future__ import annotations

import fitz  # PyMuPDF

MAX_UNDO = 30


# --------------------------------------------------------------------------
# Coordinates
# --------------------------------------------------------------------------
def to_page_point(page: fitz.Page, x: float, y: float) -> fitz.Point:
    """Display point (rotation applied) -> unrotated page point."""
    return fitz.Point(x, y) * page.derotation_matrix


def to_page_rect(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    """Display rect -> unrotated page rect."""
    r = fitz.Rect(rect) * page.derotation_matrix
    r.normalize()
    return r


def to_display_rect(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    """Unrotated page rect -> display rect."""
    r = fitz.Rect(rect) * page.rotation_matrix
    r.normalize()
    return r


def rgb(color_int: int) -> tuple[float, float, float]:
    """Span colour (sRGB int) -> PDF colour tuple 0..1."""
    return tuple(c / 255 for c in fitz.sRGB_to_rgb(color_int))


# --------------------------------------------------------------------------
# Document with undo/redo
# --------------------------------------------------------------------------
class PdfDocument:
    def __init__(self, path: str | None = None, data: bytes | None = None):
        if data is not None:
            self.doc = fitz.open("pdf", data)
        elif path:
            self.doc = fitz.open(path)
        else:
            self.doc = fitz.open()
        self.path = path
        self.modified = False
        self._undo: list[bytes] = []
        self._redo: list[bytes] = []

    # ---- basics ---------------------------------------------------------
    @property
    def page_count(self) -> int:
        return self.doc.page_count

    def page(self, index: int) -> fitz.Page:
        return self.doc[index]

    def needs_password(self) -> bool:
        return self.doc.needs_pass

    def authenticate(self, password: str) -> bool:
        return bool(self.doc.authenticate(password))

    def render(self, index: int, zoom: float) -> fitz.Pixmap:
        return self.doc[index].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)

    # ---- undo ------------------------------------------------------------
    def snapshot(self) -> None:
        """Call before every change."""
        self._undo.append(self.doc.tobytes())
        if len(self._undo) > MAX_UNDO:
            self._undo.pop(0)
        self._redo.clear()
        self.modified = True

    def _restore(self, data: bytes) -> None:
        self.doc.close()
        self.doc = fitz.open("pdf", data)

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self.doc.tobytes())
        self._restore(self._undo.pop())
        self.modified = True
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self.doc.tobytes())
        self._restore(self._redo.pop())
        self.modified = True
        return True

    # ---- saving ------------------------------------------------------------
    def save(self, path: str | None = None) -> str:
        """Save to path (or the original path). Works even when overwriting
        the file that is currently open."""
        target = path or self.path
        if not target:
            raise ValueError("No file name given")
        data = self.doc.tobytes(garbage=3, deflate=True)
        self.doc.close()
        with open(target, "wb") as f:
            f.write(data)
        self.doc = fitz.open(target)
        self.path = target
        self.modified = False
        return target

    def close(self) -> None:
        self.doc.close()

    # ---- search ------------------------------------------------------------
    def search(self, text: str) -> list[tuple[int, fitz.Rect]]:
        """All hits as (page index, display rect)."""
        hits = []
        for i, page in enumerate(self.doc):
            for r in page.search_for(text):
                hits.append((i, to_display_rect(page, r)))
        return hits

    # ======================================================================
    # Annotations
    # ======================================================================
    def highlight(self, index: int, rect: fitz.Rect,
                  color=(1, 0.9, 0)) -> int:
        """Highlight the words inside a display rect. Returns words highlighted.
        If no text is there, highlights the rectangle itself."""
        page = self.doc[index]
        area = to_page_rect(page, rect)
        words = [w for w in page.get_text("words")
                 if fitz.Rect(w[:4]).intersects(area)]
        self.snapshot()
        if words:
            quads = [fitz.Rect(w[:4]).quad for w in words]
            annot = page.add_highlight_annot(quads)
        else:
            annot = page.add_highlight_annot(area)
        annot.set_colors(stroke=color)
        annot.update()
        return len(words)

    def underline(self, index: int, rect: fitz.Rect, color=(0, 0, 1)) -> int:
        page = self.doc[index]
        area = to_page_rect(page, rect)
        words = [w for w in page.get_text("words")
                 if fitz.Rect(w[:4]).intersects(area)]
        if not words:
            return 0
        self.snapshot()
        annot = page.add_underline_annot([fitz.Rect(w[:4]).quad for w in words])
        annot.set_colors(stroke=color)
        annot.update()
        return len(words)

    def strikeout(self, index: int, rect: fitz.Rect, color=(1, 0, 0)) -> int:
        page = self.doc[index]
        area = to_page_rect(page, rect)
        words = [w for w in page.get_text("words")
                 if fitz.Rect(w[:4]).intersects(area)]
        if not words:
            return 0
        self.snapshot()
        annot = page.add_strikeout_annot([fitz.Rect(w[:4]).quad for w in words])
        annot.set_colors(stroke=color)
        annot.update()
        return len(words)

    def ink(self, index: int, strokes: list[list[tuple[float, float]]],
            color=(1, 0, 0), width: float = 2) -> None:
        page = self.doc[index]
        pts = [[tuple(to_page_point(page, x, y)) for x, y in s]
               for s in strokes if len(s) > 1]
        if not pts:
            return
        self.snapshot()
        annot = page.add_ink_annot(pts)
        annot.set_colors(stroke=color)
        annot.set_border(width=width)
        annot.update()

    def rectangle(self, index: int, rect: fitz.Rect, color=(1, 0, 0),
                  width: float = 2) -> None:
        page = self.doc[index]
        self.snapshot()
        annot = page.add_rect_annot(to_page_rect(page, rect))
        annot.set_colors(stroke=color)
        annot.set_border(width=width)
        annot.update()

    def text_box(self, index: int, rect: fitz.Rect, text: str,
                 fontsize: float = 12, color=(0, 0, 0)) -> None:
        """Typed text on the page (FreeText annotation, editable later)."""
        page = self.doc[index]
        r = to_page_rect(page, rect)
        if r.width < 20 or r.height < fontsize * 1.5:
            r = fitz.Rect(r.x0, r.y0, r.x0 + max(r.width, 200),
                          r.y0 + max(r.height, fontsize * 2))
        self.snapshot()
        annot = page.add_freetext_annot(
            r, text, fontsize=fontsize, fontname="helv", text_color=color,
            rotate=page.rotation)
        annot.update()

    def note(self, index: int, x: float, y: float, text: str) -> None:
        page = self.doc[index]
        self.snapshot()
        annot = page.add_text_annot(to_page_point(page, x, y), text)
        annot.update()

    def annot_at(self, index: int, x: float, y: float):
        page = self.doc[index]
        p = to_page_point(page, x, y)
        found = None
        for a in page.annots():
            if a.rect.contains(p):
                found = a  # topmost = last
        return found

    def delete_annot_at(self, index: int, x: float, y: float) -> bool:
        page = self.doc[index]
        annot = self.annot_at(index, x, y)
        if annot is None:
            return False
        xref = annot.xref
        self.snapshot()
        page = self.doc[index]  # snapshot doesn't reload, page still valid
        for a in page.annots():
            if a.xref == xref:
                page.delete_annot(a)
                return True
        return False

    # ======================================================================
    # Signature / images
    # ======================================================================
    def insert_image(self, index: int, rect: fitz.Rect, image_bytes: bytes) -> None:
        page = self.doc[index]
        self.snapshot()
        page.insert_image(to_page_rect(page, rect), stream=image_bytes,
                          keep_proportion=True, rotate=page.rotation)

    # ======================================================================
    # Forms
    # ======================================================================
    def form_fields(self) -> list[dict]:
        """List all fillable fields in the document."""
        fields = []
        for i, page in enumerate(self.doc):
            for w in page.widgets() or []:
                fields.append({
                    "page": i,
                    "xref": w.xref,
                    "name": w.field_name or f"field {w.xref}",
                    "type": w.field_type,
                    "type_name": w.field_type_string,
                    "value": w.field_value,
                    "choices": list(w.choice_values or []),
                    "on_state": w.on_state() if w.field_type in (
                        fitz.PDF_WIDGET_TYPE_CHECKBOX,
                        fitz.PDF_WIDGET_TYPE_RADIOBUTTON) else None,
                })
        return fields

    def set_form_values(self, values: dict[int, object]) -> int:
        """values: {widget xref: new value}. For check boxes / radio buttons
        pass True/False. Returns number of fields changed."""
        if not values:
            return 0
        self.snapshot()
        changed = 0
        for page in self.doc:
            for w in page.widgets() or []:
                if w.xref not in values:
                    continue
                v = values[w.xref]
                if w.field_type in (fitz.PDF_WIDGET_TYPE_CHECKBOX,
                                    fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
                    w.field_value = w.on_state() if v else "Off"
                else:
                    w.field_value = "" if v is None else str(v)
                w.update()
                changed += 1
        return changed

    def flatten_forms(self) -> None:
        """Make filled forms and annotations permanent (no longer editable)."""
        self.snapshot()
        self.doc.bake(annots=True, widgets=True)

    # ======================================================================
    # Edit existing text
    # ======================================================================
    def span_at(self, index: int, x: float, y: float) -> dict | None:
        """The text span (run of text with one font) under a display point."""
        page = self.doc[index]
        p = to_page_point(page, x, y)
        best = None
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if not span["text"].strip():
                        continue
                    r = fitz.Rect(span["bbox"])
                    if r.contains(p):
                        return dict(span, line_dir=line["dir"])
                    # nearest within 4 pt, for thin text
                    d = abs(r.y0 + r.height / 2 - p.y)
                    if r.x0 - 4 <= p.x <= r.x1 + 4 and d < r.height / 2 + 4:
                        if best is None or d < best[0]:
                            best = (d, dict(span, line_dir=line["dir"]))
        return best[1] if best else None

    @staticmethod
    def _base_font(fontname: str, flags: int) -> str:
        """Pick a built-in PDF font close to the original."""
        name = fontname.lower()
        bold = bool(flags & 16) or "bold" in name
        italic = bool(flags & 2) or "italic" in name or "oblique" in name
        serif_name = (any(k in name for k in ("times", "serif", "georgia", "garamond"))
                      and "sans" not in name)
        if flags & 8 or any(k in name for k in ("courier", "mono", "consol")):
            base = "cour"
        elif flags & 4 or serif_name:
            base = "tiro"
        else:
            base = "helv"
        table = {
            ("helv", False, False): "helv", ("helv", True, False): "hebo",
            ("helv", False, True): "heit", ("helv", True, True): "hebi",
            ("tiro", False, False): "tiro", ("tiro", True, False): "tibo",
            ("tiro", False, True): "tiit", ("tiro", True, True): "tibi",
            ("cour", False, False): "cour", ("cour", True, False): "cobo",
            ("cour", False, True): "coit", ("cour", True, True): "cobi",
        }
        return table[(base, bold, italic)]

    def replace_span_text(self, index: int, span: dict, new_text: str,
                          fontsize: float | None = None,
                          color: tuple | None = None) -> None:
        """Remove the original text of a span and write new text in its place.
        The original font is replaced by the closest standard font."""
        page = self.doc[index]
        bbox = fitz.Rect(span["bbox"])
        size = fontsize or span["size"]
        col = color if color is not None else rgb(span["color"])
        font = self._base_font(span.get("font", ""), span.get("flags", 0))
        origin = fitz.Point(span["origin"])

        self.snapshot()
        page = self.doc[index]
        # shrink a little vertically so neighbouring lines are not touched
        shrink = bbox.height * 0.15
        area = fitz.Rect(bbox.x0, bbox.y0 + shrink, bbox.x1, bbox.y1 - shrink)
        page.add_redact_annot(area, fill=False)
        try:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        except (TypeError, AttributeError):  # older PyMuPDF
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        if new_text:
            # keep text direction (rotated text)
            dx, dy = span.get("line_dir", (1, 0))
            angle = 0
            if (dx, dy) == (0, -1):
                angle = 90
            elif (dx, dy) == (-1, 0):
                angle = 180
            elif (dx, dy) == (0, 1):
                angle = 270
            page.insert_text(origin, new_text, fontsize=size, fontname=font,
                             color=col, rotate=angle)

    def erase_area(self, index: int, rect: fitz.Rect, white: bool = True) -> None:
        """Permanently remove everything (text, images, drawings) in an area."""
        page = self.doc[index]
        self.snapshot()
        page = self.doc[index]
        page.add_redact_annot(to_page_rect(page, rect),
                              fill=(1, 1, 1) if white else False)
        page.apply_redactions()

    # ======================================================================
    # Pages
    # ======================================================================
    def rotate_page(self, index: int, degrees: int = 90) -> None:
        self.snapshot()
        page = self.doc[index]
        page.set_rotation((page.rotation + degrees) % 360)

    def delete_pages(self, indexes: list[int]) -> None:
        if len(indexes) >= self.page_count:
            raise ValueError("A PDF must keep at least one page")
        self.snapshot()
        self.doc.delete_pages(sorted(set(indexes)))

    def move_page(self, src: int, dest: int) -> None:
        """Move page src so it ends up at position dest."""
        if src == dest:
            return
        self.snapshot()
        # PyMuPDF move_page inserts *before* 'to'; -1 = end
        if dest >= self.page_count - 1:
            self.doc.move_page(src, -1)
        elif dest > src:
            self.doc.move_page(src, dest + 1)
        else:
            self.doc.move_page(src, dest)

    def insert_blank(self, index: int) -> None:
        """Insert a blank page after index, same size as that page."""
        ref = self.doc[index].rect if self.page_count else fitz.paper_rect("a4")
        self.snapshot()
        self.doc.new_page(index + 1, width=ref.width, height=ref.height)

    def duplicate_page(self, index: int) -> None:
        self.snapshot()
        self.doc.fullcopy_page(index, index + 1)

    def insert_pdf(self, path: str, after: int) -> int:
        """Insert all pages of another PDF after page 'after'. Returns pages added."""
        other = fitz.open(path)
        try:
            n = other.page_count
            self.snapshot()
            self.doc.insert_pdf(other, start_at=after + 1)
        finally:
            other.close()
        return n

    def insert_image_page(self, path: str, after: int) -> None:
        """Insert an image (photo/scan) as a new page."""
        img = fitz.open(path)
        try:
            pdf_bytes = img.convert_to_pdf()
        finally:
            img.close()
        imgpdf = fitz.open("pdf", pdf_bytes)
        try:
            self.snapshot()
            self.doc.insert_pdf(imgpdf, start_at=after + 1)
        finally:
            imgpdf.close()

    def extract_pages(self, indexes: list[int], path: str) -> None:
        """Save the chosen pages as a new PDF."""
        out = fitz.open()
        try:
            for i in sorted(set(indexes)):
                out.insert_pdf(self.doc, from_page=i, to_page=i)
            out.save(path, garbage=3, deflate=True)
        finally:
            out.close()

    def split_every(self, n: int, base_path: str) -> list[str]:
        """Split into files of n pages: base_1.pdf, base_2.pdf ..."""
        if base_path.lower().endswith(".pdf"):
            base_path = base_path[:-4]
        paths = []
        for k, start in enumerate(range(0, self.page_count, n), 1):
            p = f"{base_path}_{k}.pdf"
            self.extract_pages(list(range(start, min(start + n, self.page_count))), p)
            paths.append(p)
        return paths


def parse_page_ranges(text: str, page_count: int) -> list[int]:
    """'1-3, 5, 8-' -> [0,1,2,4,7,...] (0-based). Raises ValueError."""
    result: list[int] = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = int(a) if a.strip() else 1
            end = int(b) if b.strip() else page_count
        else:
            start = end = int(part)
        if start < 1 or end > page_count or start > end:
            raise ValueError(f"Invalid range: {part}")
        result.extend(range(start - 1, end))
    if not result:
        raise ValueError("No pages given")
    return sorted(set(result))
