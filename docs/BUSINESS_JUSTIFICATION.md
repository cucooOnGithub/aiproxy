# Why aiproxy — business justification

aiproxy sits between your AI tools (Claude Code, Claude/OpenAI SDKs, etc.)
and the LLM provider. It detects sensitive content locally, replaces it with
reversible tokens before the request leaves your machine, and restores the
originals in the reply. The provider sees tokens; your users see real data.

## The problem

Using a hosted LLM means sending your prompt to a third party. That prompt
routinely contains data you are not free to disclose:

- **Personal data (GDPR / CCPA / LGPD):** names, emails, phone numbers,
  addresses of customers and employees. Sending them to a processor you have
  not vetted or covered by a DPA is a compliance exposure.
- **Customer / client confidential data:** company names, project names,
  contract terms — often under NDA. One pasted stack trace or ticket can
  breach a confidentiality clause.
- **Secrets:** API keys, tokens, IBANs accidentally included in code or logs.
- **Trade secrets and IP:** internal system names, architecture, roadmaps.

The data also leaves your control: it may be logged upstream, retained,
cached, or used for abuse monitoring, and it crosses a jurisdictional border.

## How aiproxy addresses it

- **Data minimization by default.** The provider receives only tokenized
  placeholders (`⟦PERSON_1⟧`), never the underlying values — directly
  supporting the GDPR Art. 5 minimization and purpose-limitation principles.
- **Pseudonymization at the boundary.** Mapping token↔original stays local
  (encrypted SQLite); the upstream payload is pseudonymized, reducing the
  classification and risk of what you transmit.
- **Covers more than text.** Images, scanned PDFs, and Office
  documents are inspected and masked before they are sent.
- **Auditable and verifiable.** `/dryrun` shows exactly what would be
  forwarded; the dashboard reports how much was redacted (counts only, never
  values). You can prove to a reviewer what left the building.
- **No new trust boundary.** It is self-hosted and open (noncommercial
  license); detection runs on a local LLM, so nothing is shipped to a second
  vendor to solve the first vendor's exposure.

## Who benefits

- **Regulated teams (finance, health, legal, public sector):** use modern
  LLMs without routing regulated data to an external processor.
- **Agencies / consultancies:** honor client NDAs while still using AI tools
  internally — client and project names never reach the provider.
- **Enterprises:** a single enforcement point and policy (allow/deny lists,
  `fail_closed`) for what may and may not leave the network.

## Limits (state these honestly)

- Detection is not perfect; an LLM can miss an entity. Use `fail_closed`,
  `/dryrun`, and deny lists to bound the risk.
- Image/PDF masking is one-way and OCR-quality-bounded.
- The mapping store is the de-anonymization key — protect and encrypt it.
- aiproxy reduces disclosure risk; it is a control, not a legal guarantee.
  It does not replace a DPA, contractual review, or sign-off from your DPO /
  legal / security team.
