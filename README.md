# Apex PDF

PDF reader and editor for Windows.

## Features

**Read**
- Open by double-click, drag & drop, or File ▸ Open (recent files, password-protected PDFs)
- Page thumbnails, zoom (Ctrl + wheel, fit width), go to page
- Search (Ctrl+F, F3 next), copy text (Select tool), print

**Annotate**
- Highlight, underline, strike-out text
- Freehand drawing, boxes, text boxes, sticky notes
- Delete any annotation (Delete mark tool)
- Choose colours and text size from the second toolbar

**Fill & sign**
- Fill form fields (text, check boxes, lists): Fill & Sign ▸ Fill form
- Draw your signature once (saved for next time), then ✍ Sign and drag where it goes
- Insert stamps or logos (Image tool)
- Flatten: make everything permanent before sending

**Edit text**
- ✎ Edit text: click on a word or line, change it, adjust size and colour
- White-out: permanently erase an area
- Note: the new text uses the nearest standard font (Helvetica / Times / Courier),
  so it may look slightly different from the original font. Scanned pages have
  no editable text.

**Pages**
- Rotate, delete, reorder (drag thumbnails or Ctrl+Up/Down), duplicate
- Insert blank page, merge another PDF, add a photo/scan as a page
- Extract pages (e.g. `1-3, 5`) or split every N pages

Undo / redo (Ctrl+Z / Ctrl+Y) for all edits until you close the file.

## Build

Push to `main` on GitHub: the **Build Windows app** workflow runs the tests and
builds `ApexPDF.exe`. Download it from the run's **Artifacts** section
(`ApexPDF-windows`).

Run from source:

```
pip install -r requirements.txt
python app/main.py [file.pdf]
```

Tests: `pip install pytest` then `pytest tests`.

## Licence note

Uses PyMuPDF (AGPL-3.0) and PySide6 (LGPL-3.0). Fine for personal and internal
use; distributing it to customers would require publishing the source under AGPL
or buying a PyMuPDF commercial licence.
