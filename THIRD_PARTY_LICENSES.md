# Third-party licenses

aiproxy is distributed under the PolyForm Noncommercial License 1.0.0
(see `LICENSE`). It depends on the open-source components below. Each keeps
its own license and copyright; all are permissive (MIT / BSD / Apache-2.0 /
PSF) and none impose copyleft or restrict aiproxy's noncommercial terms.

## Core dependencies

| Component | License | Notes |
|-----------|---------|-------|
| fastapi | MIT | web framework |
| starlette | BSD-3-Clause | pulled in by fastapi |
| uvicorn | BSD-3-Clause | ASGI server |
| httpx | BSD-3-Clause | upstream HTTP client |
| pyyaml | MIT | config parsing |
| cryptography | Apache-2.0 OR BSD-3-Clause | store encryption (Fernet) |
| ruamel.yaml | MIT | comment-preserving config editor |

## Media extras (`pip install -e ".[media]"`)

| Component | License | Notes |
|-----------|---------|-------|
| pillow | MIT-CMU (HPND) | image masking + PDF assembly |
| pytesseract | Apache-2.0 | wrapper for the Tesseract OCR binary |
| pypdfium2 | BSD-3-Clause + Apache-2.0 | PDF rasterization (wraps Google PDFium) |

`pypdfium2` bundles PDFium, which is BSD-3-Clause (Google) and includes
further components under their own permissive licenses (see the pypdfium2
distribution's `LICENSES/` for the full set). It replaced PyMuPDF, whose
AGPL-3.0 license is incompatible with offering aiproxy under
noncommercial-only terms.

## External programs (not bundled; installed separately)

- **Tesseract OCR** — Apache-2.0. A system binary invoked via pytesseract;
  not redistributed with aiproxy.
- **Ollama** — MIT. Runs the local NER model; a separate service aiproxy
  talks to over HTTP.

## Local models (user-supplied; NOT bundled or redistributed)

aiproxy calls whatever model you pull into Ollama for NER. Those model
weights are licensed by their own publishers and are your responsibility:

- **Llama 3.2** (e.g. `llama3.2:latest`) — Meta Llama 3.2 Community License.
  Has use restrictions and an acceptable-use policy; review before any
  commercial deployment.
- **Qwen2.5** (e.g. `qwen2.5:3b-instruct`) — Apache-2.0 for most sizes;
  confirm the license of the specific tag you pull.

aiproxy does not ship, host, or redistribute any model weights. Choosing a
model and complying with its license is up to the operator.
