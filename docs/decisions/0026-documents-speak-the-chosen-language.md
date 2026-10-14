# The documents speak the language the user chose

**Slug:** `documents-speak-the-chosen-language`

Supersedes `decision: the-decision-briefs-quoted-half` (0007) and the English anchor it rested on.

## Context

The documents anchored English whatever the request's language, so that dev teams and trackers met
one language; the conversation mirrored the request. The brief was English judgment around quoted
sections in the request's language (0007). In #779 three French clients received English briefs, PRDs
and criteria, and the judge counted it against Requivo each time: the brief is the one document
addressed to the client, and 0007's revisit condition — its primary reader is the client or the PM,
not the build side — had come true (#783 rewrote it as a client document).

## Decision

**The user chooses the documents' language, once, and the session remembers it** (#790). A first
`discover` or `run` at a terminal asks, offering the request's language; `--docs-language` answers
ahead, on `session init` too, which the keyless run skill passes after asking at its first checkpoint.
A run nobody watches takes the request's language, or English when it cannot be told. The answer is
`SessionMeta.docs_language`, optional and additive; `null` reads as English, so a session from before
keeps what it had.

- **The default is a deterministic guess, never a call.** Function words of four named languages,
  counted; no hit, a tie or a single word is English. Discovery returns no language field to reuse,
  and a paid call to name a language the user is about to confirm is the wrong price.
- **The language travels in the user message, not the system prompt.** One cached prompt per
  operation whatever the language, and `prompt_version` keeps meaning "which instructions".
- **The documents' own words follow.** Every saved document renders its headings, labels, verdicts
  and computed lines through `render/phrases.py` (French, Spanish, German; the build documents since
  #823); with the default, projection and judgment are one language, so no document mixes two.
- **Not identity, not staleness.** Re-recording a language makes no new session and stales no
  document: staleness stays the dependency graph (invariant 1).

## What breaking it cost

The English anchor cost #779's three French runs a document the client could not use as written.
Forcing the request's language with no question would cost the opposite case — a French request
whose build team reads English — so the choice is asked, not inferred.

## Alternatives rejected

- **Mirror the request in every document, unasked.** Right for the client, wrong for a team that
  builds in English from a French request, and it leaves no way to say so.
- **Detect the language with the provider.** A paid call for what a function-word count gets right on
  the requests Requivo sees, and the user confirms it anyway.
- **Translate the projected sections.** 0007's reason still holds: the client's own words are not
  improved by a paraphrase, and a projection cannot drift.
- **Name the language in the system prompt.** Changes the cached prefix and the prompt hash per
  language for one sentence of instruction.
