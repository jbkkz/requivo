# Examples

These examples show Requivo end to end: a request → a saved `model.json` → the artifacts generated
from it (assessment, PRD, acceptance criteria, epic, estimate, exports, release notes). They double as
documentation and as inputs the tests and golden harness rely on.

## What's here

**Start here — the canonical example:**

- **`leave-approval/`** — a one-line request taken through the questions, the decision brief, the full
  document chain, and a changed answer that moves the scope. This is the one to read first; the last
  section is the change-impact walkthrough, which is the behaviour the whole engine exists for.

**Then, a harder one:**

- **`event-checkin-reconciliation/`** — a messy, multi-feature client email, and the brief that refuses
  to conflate its distinct problems. Also the `requivo demo` payload, which is why its files are
  byte-pinned by a test.

**Additional request prompts** (just the input — run them yourself):

- `case1_leave.md` … `case6_freelancer_payment.md` — short standalone requests to try.

**For automation:**

- **`n8n/`** — three importable n8n workflows (intake, reply, epic to GitHub issues) over the CLI's
  machine reads. See [n8n/README.md](n8n/README.md).

**Not examples:** the golden-harness fixtures under `fixtures/golden/` are internal test inputs, not a
getting-started path — see [../docs/evaluations.md](../docs/evaluations.md).

## Every public example must be synthetic or properly anonymised

These are illustrative, invented scenarios. Anything committed here **must** be free of:

- real client or company names;
- real email addresses, identifiers, or account references;
- confidential business rules, pricing, or commercial data;
- secrets, API keys, or credentials;
- personal data of real individuals.

**Do not commit a real user session, request, or context card** — even one that "looks generic".
Real-world material belongs in a private workspace (or, for cards, `REQUIVO_CONTEXT_DIR`), never in
this repository. See the data boundary in
[../docs/open-source-strategy.md](../docs/open-source-strategy.md#data-what-may-be-public-what-stays-private).

If you want to *report* how the engine did on a real request, use the **Real-world discovery
feedback** issue template, which asks only for anonymised information.

## Contributing an example safely

1. Invent a scenario, or fully anonymise a real one — change names, numbers, domains, and any detail
   that could identify a real party. Removing the name is not enough if the surrounding facts still
   identify the source.
2. Keep it small and focused on one requirements *form* (the golden request set follows the same
   rule).
3. If it includes a generated model or artifacts, generate them from the anonymised request so
   nothing confidential leaks through the model.
4. Open a PR and confirm in the description that the example contains no confidential data.
