#!/usr/bin/env python3
"""
Find and render a specific figure from a bioRxiv preprint (or any local PDF):
render whole PAGES with pdftoppm (never
pdfimages — a multi-panel figure is many embedded objects and comes out as
fragments), locate the figure page by its CAPTION text, render at high DPI.

Figures can sit anywhere — inline, or dumped after the references at the very
end — so we search every page, don't assume a position.

Usage:
  get_figure.py --doi <DOI> --fig 1
  get_figure.py --pdf cache/papers/foo.pdf --fig 2 --dpi 250
  get_figure.py --doi <DOI> --fig 1 --json     # machine-readable output

Prints the rendered PNG path(s), best candidate first. The caller then Reads the
PNG and attaches it to a Discord reply (files=[...]).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deep_analyze as d  # reuse fetch_biorxiv_pdf / parse_doi / PAPERS_DIR

# Poppler's pdftoppm. Set LIT_PDFTOPPM to an absolute path, or leave the
# placeholder if `pdftoppm` is already on $PATH.
PDFTOPPM = os.environ.get("LIT_PDFTOPPM", "/PATH/TO/pdftoppm")


def resolve_pdf(args) -> tuple[Path, str]:
    """Return (pdf_path, stem) for either --pdf or --doi."""
    if args.pdf:
        p = Path(args.pdf).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"PDF not found: {p}")
        return p, p.stem
    doi = d.parse_doi(args.doi or args.url)
    meta = d.fetch_metadata(doi)
    version = str(meta.get("version", "1"))
    pdf = d.fetch_biorxiv_pdf(doi, version)
    if not pdf:
        raise RuntimeError(f"could not fetch PDF for {doi} (Cloudflare throttle?)")
    return pdf, re.sub(r"[/]", "-", doi) + f"v{version}"


def find_figure_pages(pdf_path: Path, fig_num: int) -> list[tuple[int, int]]:
    """Return [(page_number, page_text_len), ...] whose text has a line STARTING
    with the figure's caption label (e.g. 'Figure 1.', 'Fig. 1 |', 'Figure 1:').
    Starting-at-line-begin avoids matching in-text mentions like '...in Figure 1'.
    Sorted graphic-heavy first (shorter text = more likely the real figure page).
    """
    # 'Figure 1' / 'Fig. 1' / 'Fig 1' at line start, optionally 'Supplementary',
    # followed by a caption delimiter . : | │ or an em/space then title text.
    cap_re = re.compile(
        rf"(?im)^\s*(?:supp(?:lementary|l)?\.?\s+)?fig(?:ure)?\.?\s*{fig_num}\b\s*[.:|│—-]"
    )
    hits: list[tuple[int, int]] = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        for i, page in enumerate(reader.pages, start=1):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if cap_re.search(t):
                hits.append((i, len(t)))
    except Exception as e:
        print(f"[fig] pypdf failed ({e}); using poppler", file=sys.stderr)

    # Fallback: some publisher PDFs (Cell Press) trip pypdf's font parser, so
    # pypdf yields nothing. poppler pdftotext handles them; it separates pages
    # with form-feed (\x0c), so split on that to keep page numbers aligned.
    if not hits:
        txt = d._pdftotext_poppler(pdf_path)
        for i, pagetext in enumerate(txt.split("\x0c"), start=1):
            if cap_re.search(pagetext):
                hits.append((i, len(pagetext)))

    hits.sort(key=lambda x: x[1])
    return hits


def render_page(pdf_path: Path, page: int, out_prefix: Path, dpi: int) -> Path | None:
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [PDFTOPPM, "-png", "-r", str(dpi), "-f", str(page), "-l", str(page),
         str(pdf_path), str(out_prefix)],
        check=True, capture_output=True,
    )
    # pdftoppm names output <prefix>-<page>.png (page zero-padded to page-count width)
    matches = sorted(glob.glob(f"{out_prefix}-*.png"))
    return Path(matches[-1]) if matches else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doi")
    ap.add_argument("--url")
    ap.add_argument("--pdf")
    ap.add_argument("--fig", type=int, required=True, help="figure number to find")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--max-pages", type=int, default=3,
                    help="render at most this many candidate pages")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not (args.doi or args.url or args.pdf):
        ap.error("provide --doi / --url / --pdf")

    pdf_path, stem = resolve_pdf(args)
    pages = find_figure_pages(pdf_path, args.fig)
    print(f"[fig] {pdf_path.name}: Figure {args.fig} caption on page(s) "
          f"{[p for p, _ in pages]}", file=sys.stderr)

    out_dir = d.PAPERS_DIR / f"{stem}_pages"
    rendered: list[str] = []
    for page, _ in pages[: args.max_pages]:
        prefix = out_dir / f"fig{args.fig}-p{page}"
        png = render_page(pdf_path, page, prefix, args.dpi)
        if png:
            rendered.append(str(png))
            print(f"[fig] rendered page {page} → {png}", file=sys.stderr)

    if args.json:
        print(json.dumps({"pdf": str(pdf_path), "fig": args.fig,
                          "pages": [p for p, _ in pages], "pngs": rendered},
                         ensure_ascii=False))
    else:
        for r in rendered:
            print(r)
    return 0 if rendered else 1


if __name__ == "__main__":
    raise SystemExit(main())
