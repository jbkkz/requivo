# Golden requests

A fixed set of discovery inputs used to watch the engine for regressions. Each block below is one
request: `golden_run.py` reads this file, runs a single-pass discovery **K times** per block (K=3 by
default) and saves all K models to `fixtures/golden/<slug>.runs.json`. `golden_diff.py` then compares
a fresh K-run capture against the committed one and reports a slot as *moved* only when the change
clears the measured noise floor — the engine is non-deterministic and the model family exposes no
sampling controls, so a single capture can't be pinned, only sampled. See `scripts/golden_lib.py`.

The set is deliberately small and diverse: **one request per problem *form***, so a change to a prompt
or a context card that shifts how the engine reasons about a form shows up on the request that
exercises it. The `card:` line is documentary — it names the context card that *should* shape this
request. It is not a loading switch: `load_context()` concatenates every non-`_` card in `context/`,
so every run sees every card. The mapping tells you which card a diff on this request is likely
attributable to.

Format (parsed by `golden_run.py`): each run is a `### <slug>` heading followed by `key: value`
lines. `request:` holds the single-line discovery input; `form:` and `card:` are metadata.

## Interactive requests

A block that also carries `answer.<slot>:` lines is captured differently: instead of one discovery
call, it drives the **interactive** shape — `DiscoveryService.draft_turn`, the loop behind
`requivo discover` — for up to `GOLDEN_TURNS` turns (5 by default), answering the engine's questions
off those lines. It exists because a single-pass capture cannot see what #77 changed: from turn 3 the
loop is grounded on the carried model alone, where the old one re-sent the whole transcript. Turns 1
and 2 are byte-identical between the two shapes, so only a capture that runs deep says anything.

Each `answer.<slot>:` line is one **layer** — the next thing this client has to say when the engine
comes back to that slot. Layers are handed out in order and then run out, which is what keeps the
conversation moving instead of looping on the same reply; a question the sheet cannot answer is
skipped, exactly as a user pressing Enter skips it, and a turn that answers nothing ends the capture.

**Cost:** an interactive request is `K × GOLDEN_TURNS` calls (15 at the defaults) where a single-pass
one is `K`. Capture it on its own, not as part of a full-set run.

## The non-English request

`maintenance-rounds-fr` is the one block that varies the **language** rather than the problem form,
and it is a deliberate exception to the one-request-per-form rule above. The output-language policy
(`docs/requirements-model.md`, "The language of the outputs") says the questions and the
understanding mirror the client's request while every buildable artifact anchors English — behaviour
that until #277 was emergent, unstated, and measured by nothing, because every request here was
English. A capture of it answers, per run, whether the questions really did come back in French; a
`--brief` capture answers the other half, whether the assessment held English.

It is single-pass, so a bare `golden_run.py` picks it up with the rest of the set at the ordinary
`K` calls. Nothing in the harness reads the language: `golden_diff`'s lenses compare slots,
questions and the assessment exactly as they do for the English blocks, and the language claim is
read off `--questions` by a person.

### leave-approval
form: approval
card: b2b-platform
request: We'd like managers to approve employee leave requests, with an escalation if the manager is away.

### invoice-on-signature
form: auto-create-on-event
card: b2b-platform
request: When a contract is signed, we want an invoice to be created automatically.

### notify-mission-end
form: notify
card: b2b-platform
request: We want to notify the right people when a freelancer's mission is about to end.

### export-financials
form: export-report
card: financial-reporting
request: Let users edit the reported totals and export the figures for the finance team.

### event-checkin
form: one-shot-app
card: event-ops
request: We need an app for staff to check attendees in at the venue entrance on the event day.

### doc-reapproval
form: mutate-signed-artifact
card: document-management
request: We'd like managers to edit and re-approve documents after they've already been signed.

### maintenance-rounds-fr
form: schedule-recurring-visits
card: b2b-platform
request: Nous aimerions planifier les visites de maintenance chez nos clients, avec une tournée par technicien et une replanification quand un client annule au dernier moment.

### training-budget
form: allocate-scarce-pool
card: b2b-platform
request: We need to hand out a yearly training budget across departments, with rules for who gets priority when it runs out.
answer.problem: Department heads fight over the budget by email today and the loudest one wins, and finance only discovers the overspend in March.
answer.problem: The real cost is not the money, it is that mandatory certifications get bumped by discretionary courses and we then fail the audit.
answer.current_process: One shared spreadsheet per department, consolidated by HR twice a year, and the two consolidations never match.
answer.current_process: There is no reservation step at all: a manager books with the vendor first and tells HR afterwards.
answer.success_metrics: Zero certification lapses, and finance seeing committed spend within a week of the booking rather than at year end.
answer.success_metrics: We would also count it a success if HR stopped spending two weeks each January reconciling the spreadsheets.
answer.actors: Department heads request, HR validates eligibility, finance owns the envelope, and the employee books the course.
answer.actors: For the three regulated entities a compliance officer has to countersign anything that is a certification renewal.
answer.business_objects: A request carries the employee, the course, the vendor quote, the department and the fiscal year.
answer.business_objects: The envelope is an object too: an amount, a period and a scope, and it can be split by cost centre.
answer.business_rules: Certifications outrank everything, then seniority within the department, then first come first served.
answer.business_rules: Unused budget carries over one year for the regulated entities and is lost everywhere else.
answer.business_rules: A mid-year joiner gets a pro-rata entitlement, and a leaver's committed but unspent amount returns to the envelope.
answer.business_rules: A department that transfers headcount mid-year takes a pro-rata share of its old envelope with it, not a fresh allocation.
answer.business_rules: Manager training is mandatory and sits outside the discretionary pool entirely -- it is never bumped, even by a certification.
answer.business_rules: Two employees from the same department competing for the last seat are resolved by tenure, not by who asked first.
answer.business_rules: A course that satisfies two certifications at once is charged to the envelope only once, whichever certification is checked first.
answer.business_rules: Finance can freeze an entire department's envelope mid-year if that department is over budget elsewhere, and only finance can lift the freeze.
answer.business_rules: A vendor discount for booking multiple seats has to be shared proportionally across the departments that booked into it, not credited to whichever department triggered the discount.
answer.business_rules: Cross-charging a course to two departments splitting an employee's time is allowed, but only if both department heads sign off before the booking, not after.
answer.workflow: Request, eligibility check, budget reservation, approval, booking, then invoice reconciliation.
answer.workflow: A reservation has to be able to expire: if nobody has booked within thirty days the money goes back to the pool.
answer.workflow: The eligibility check and the approval step can happen in either order -- a manager can approve in principle before HR has confirmed the employee is eligible, and the reservation only firms up once both have happened.
answer.workflow: A rejected request does not disappear: it stays visible to the employee with the reason, and they can resubmit against a different course without restarting the whole intake.
answer.workflow: Booking with the vendor is a separate step from reservation -- the reservation holds the money, the booking is the vendor confirming a seat exists, and the two can fail independently.
answer.workflow: Invoice reconciliation happens per course, not per request: one invoice can cover ten employees' bookings, and the workflow has to split it back across their individual reservations.
answer.workflow: Cancelling after the vendor has already confirmed the seat triggers a different path than cancelling before -- the first needs a refund negotiation, the second just releases the hold.
answer.workflow: A compliance countersignature, where it applies, happens after the manager approves and before the reservation firms up -- it is a gate on the reservation, not a parallel step.
answer.workflow: An expired reservation that gets renewed by the same employee for the same course does not restart eligibility -- only the budget reservation step runs again.
answer.workflow: The workflow has to support a request being split into two bookings -- part of a course now, part deferred to next fiscal year -- without treating that as two separate requests.
answer.permissions: A department head sees only their own envelope; HR and finance see all of them.
answer.permissions: The compliance officer sees certification requests across every entity but must not see amounts.
answer.permissions: A department head who also manages a second department temporarily -- covering a parental leave -- needs to see both envelopes for the duration, and that access has to expire on its own.
answer.permissions: HR can see every envelope but can only approve eligibility, never override a budget decision -- that authority stops at finance.
answer.permissions: An employee can see their own request's status and the reason for a rejection, but never the envelope balance behind it.
answer.permissions: Finance's freeze power from the business rules has to be visible in an audit log to the compliance officer even though the officer cannot see amounts -- the freeze itself is not a financial figure.
answer.permissions: A department head leaving the company loses envelope access immediately, but their historical approvals must stay attributed to them, not anonymised.
answer.permissions: Two department heads sharing one envelope for a joint programme both need write access, and either one's approval alone is sufficient -- it is not a two-signature requirement.
answer.permissions: The vendor-facing side of the tool, if it ever gets a login, must not see any envelope figures at all -- only which seats were confirmed.
answer.permissions: An auditor brought in externally for the year-end review needs read access to every envelope's history, but only for a bounded date range, not indefinitely.
answer.integrations: Bookings come back from the vendor portal as a CSV, and the invoices land in the accounting system.
answer.integrations: We have not decided whether the tool pushes to accounting or accounting pulls from it.
answer.constraints: The fiscal year is not the calendar year for two of the entities, and the tool has to close within five working days of year end.
answer.constraints: Everything has to work for a department head on a phone, because half of them are never at a desk.
answer.config_vs_custom: Every client we roll this out to orders the priorities differently, so that has to be configurable rather than coded.
answer.edge_cases: A vendor can cancel a course after the money is committed, and the refund can land in the next fiscal year.
answer.edge_cases: Two department heads can request the last remaining seat within the same minute.
answer.edge_cases: An employee books through the tool and then the same course independently through the vendor portal, so the same seat is committed twice against two different reservations.
answer.edge_cases: A certification renewal deadline falls inside the blackout week between fiscal years, when neither year's envelope is technically open yet.
answer.edge_cases: A department is dissolved mid-year and its remaining envelope has to be redistributed, but two of its employees have reservations still pending.
answer.edge_cases: A vendor changes the price of a course after a reservation was made but before the invoice arrives, and the committed amount no longer matches what gets billed.
answer.edge_cases: An employee is on the list for a mandatory certification renewal and gets made redundant before the course runs -- the seat and the money are both now orphaned.
answer.edge_cases: A course is cancelled and re-listed by the vendor under a new name and a new price, and the tool has no way to know it is the same commitment.
answer.edge_cases: A compliance officer's countersignature is still pending when the reservation's thirty-day expiry hits -- does the expiry win, or does the pending signature hold it open.
answer.edge_cases: A regulated entity's carried-over budget from last year and this year's fresh allocation both get spent against the same certification renewal, and nobody flagged the double-draw until the audit.
answer.reporting: Finance needs committed versus spent versus remaining per envelope, and an audit trail of who overrode a priority.
answer.reporting: The auditor asks for the state of an envelope as it stood on a given date, not just as it stands now.
answer.acceptance: It is accepted when a full year can be replayed from the audit trail and matches the accounting system to the cent.
answer.risks: The main risk is that department heads keep booking with the vendor first, and the tool then records fiction.
answer.risks: The second is that we roll it out mid-year and nobody can say what the opening balances should be.
