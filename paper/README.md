# arxivMedia — Draft Preprint

This directory contains a **draft preprint** (a systems/demonstration paper)
describing arxivMedia. It is **unsubmitted** and under review by the author; it
has not been posted to arXiv or submitted anywhere. It reports no user study and
no quantitative evaluation — it describes the system as built and positions it
against related work.

## Build

Requires a LaTeX distribution with `pdflatex` and `bibtex` (e.g. TeX Live):

```bash
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
```

This produces `main.pdf`. Build artifacts (`*.aux`, `*.log`, `*.out`, `*.pdf`,
`*.bbl`, `*.blg`) are gitignored and should not be committed.

## Files

- `main.tex` — the paper (standard `article` class, two-column; compiles with
  plain `pdflatex` + `bibtex`, no conference style files).
- `refs.bib` — bibliography (uses `natbib` numeric citations).
- `README.md` — this file.
