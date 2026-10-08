# Observed Gorelo API behavior: where production differed from, or went beyond, the published spec

This is a factual record, kept so that people reading the code can see why some checks look
stricter or odder than the published OpenAPI spec would suggest. It is not a complaint. The
Gorelo Public API is young and changes quickly; where the published spec did not mention
something, production simply answered differently, and this server was changed to match what
production does.

How to read it. Each entry gives the date it was seen (UTC dates; the hour is mostly not
recorded), what the published spec said at that time, what production did, and what this server
does about it. Everything here was observed with live calls against a test client, never against
real client records. Production can change at any time, so treat this as a log, not a promise.
The reasoning behind the design is in `docs/why/`, and the tools that result are in
`docs/tool-catalog.md`.

A caution that applies to all of it: an unchanged spec hash says nothing about live behavior.
Several entries below were invisible to any spec comparison. Only live calls can find them, so
the live test harness (`docs/why/07-live-test-harness.txt`) is run after any Gorelo release.

## 1. Cursor paging introduced (announced in the changelog, 2026-07-24)

- Seen: 2026-07-26, in a spec snapshot and live calls.
- Spec said: the spec before the change is not recorded here. The 2026-07-26 snapshot was
  checked against live calls and differed from live answers in places listed below. The change was
  announced in the changelog.
- Production did: most collection endpoints moved from a bare JSON array to a cursor envelope
  (`data`, `totalCount`, `nextCursor`, `hasMore`). The migration was partial: clients, contacts,
  agent assets, users and tickets were paged, while groups, ticket statuses, types and tags still
  returned bare arrays.
- This server: an operation is treated as paged if and only if it has a `Cursor` query
  parameter in the spec index. Paging helpers refuse the wrong kind of operation.
  See `docs/why/06-api-watcher.txt` for how changes are now noticed.

## 2. Response envelope key casing changed (between 2026-07-26 and 2026-10-01)

- Seen: by 2026-10-01. The exact day was not recorded; it was in the weeks after the July
  paging change.
- Spec said: by 2026-10-01 the spec described every response as
  `{StatusCode, IsSuccess, Data, DataContext, Notifications}`, with PascalCase keys, and paging in
  `DataContext.Pagination`.
- Production did: answered in that PascalCase shape. A client written for the earlier lowercase
  keys (`data`, `nextCursor`) read zero rows from paged lists and failed on unpaged ones.
- This server: the client was rewritten for the PascalCase envelope. It checks `IsSuccess`, not
  only the HTTP status, and raises a "shape" error for anything that is not an envelope, rather
  than returning an empty list. The invoice PDF is the one raw (non-envelope) answer, and a
  429 answer is a gateway JSON, not an envelope.

## 3. Page size above 200 is rejected although some spec text says "clamped" (2026-10-01)

- Spec said: the text of some operations says an out-of-range `PageSize` is clamped; others say
  rejected.
- Production did: HTTP 400 "PageSize must be between 1 and 200" on a probe that the text implied
  would be clamped.
- This server: clamps to 1 to 200 on the client side for every paged operation and reports the
  size used.

## 4. Unknown query parameters and body fields are rejected (2026-10-01; body fields since 2026-08-05)

- Spec said: nothing explicit about unknown query names. Each object schema declares no
  additional properties.
- Production did: an unknown query name is a 400 (code 070101, "not recognized by this
  endpoint"). An unknown body field is a 400 (code 070201, "Invalid or malformed request body")
  that does not say which field.
- This server: every query name and body field is checked against the spec index before sending
  (`docs/why/02-spec-index-and-request-validation.txt`).

## 5. POST /v1/invoices was announced but not in the spec (2026-10-01)

- Spec said: the 2026-10-01 spec had no `POST /v1/invoices`, although a changelog entry
  mentioned invoice creation.
- Spec later: the spec published `POST /v1/invoices` and `GET /v1/invoices/{invoiceId}` on
  2026-10-02.
- This server: `create_invoice` makes Drafts only; approved invoices have a separate gated tool
  (entry 13).

## 6. PATCH on clients and contacts moved to /{id} before the spec said so (2026-10-02)

- Seen: a live write matrix, 2026-10-02.
- Spec said: the published spec still listed `PATCH /v1/clients` and `PATCH /v1/contacts` with the
  id in the body.
- Production did: answered HTTP 405 (`Allow: GET, POST`) on both, although they worked the day
  before. `PATCH /v1/clients/{clientId}` (a partial update) and `PATCH /v1/contacts/{contactId}`
  (a full-body command) existed and worked. Gorelo published the path forms in a new spec the
  same day.
- This server: for a few hours a local override file corrected the index, then the override was
  retired when the refreshed spec caught up. The override mechanism and its retirement rule remain
  (`spec.py`, `spec/live_overrides.json`) and a test forces the entry to be removed once the
  published spec agrees. Earlier (2026-10-01) the contact collection PATCH replaced the whole
  record, wiping unsent fields, so `update_contact` reads the contact and sends the full command.

## 7. Path placeholders were renamed (2026-10-02)

- Spec said: 13 operations in 6 families changed from `{id}` to names such as `{clientId}`,
  `{contactId}` and `{deviceId}`, with the same URLs.
- Production did: nothing different; the URLs are the same.
- This server: anything keyed by exact operation text would have silently broken, so forbidden
  and side-effect operations are matched by shape (every placeholder reads as `{}`).

## 8. Time entries came back flat, then as objects (2026-10-02 to 2026-10-03)

- Spec said: `User`, `Ticket` and `Task` as objects (`{Id, Name}` and `{Id, Number, Title}`).
- Production did: on 2026-10-02 it returned flat `UserId`, `TicketId` and `TaskId`, and kept
  doing so after the spec showed objects. A read-only probe on 2026-10-03 found it aligned.
- This server: tools pass records through unchanged and read neither shape; the live matrix
  accepts the old flat shape with a note.

## 9. Uptime maintenance needs StartDateTime (2026-10-02)

- Spec said: nothing marked `StartDateTime` required.
- Production did: `MaintenanceMode.Enabled` true without `StartDateTime` is a 400.
- This server: `set_uptime_maintenance` requires `start` as well as `duration_minutes` when
  enabling, and refuses locally otherwise.

## 10. Approvers need an approver contact tag (2026-10-02)

- Spec said: approvers must be active contacts of the ticket's client (task approvals mentioned
  the tag).
- Production did: a ticket approval also rejects a contact without a contact tag marked as
  approver. The API cannot set that tag; it is set in the Gorelo app.
- This server: `create_ticket_approval` says so in its text.

## 11. Lead and watcher cannot be the same technician in one request (2026-10-02)

- Spec said: nothing.
- Production did: a ticket update that set the same technician as lead and as watcher answered
  400 "Technician already exists".
- This server: `update_ticket` documents it; the live matrix sets them in separate requests.

## 12. Phone country codes are region codes (2026-10-01)

- Spec said: examples such as "+1".
- Production did: stores two-letter regions such as `US`. "1" and "+1" are a 400 ("Missing or
  invalid default region").
- This server: validates `^[A-Z]{2}$` and never maps dial codes to regions.

## 13. Approving an invoice pushes it to accounting, and a void does not follow (2026-10-04)

- Spec said: approving on create pushes the invoice to the connected accounting system at once.
  Nothing said what a void does there.
- Production did: the push to the accounting system (seen with Xero) took more than a
  minute. Voiding through the API (`DELETE /v1/invoices/{invoiceId}`, answer `StatusId` 4) left
  the accounting system copy open and unpaid when checked a few minutes later; whether it ever follows was not
  observed. A voided invoice stays listed as Void and keeps its `AmountDue`. An Approved invoice may
  also be emailed by Gorelo on its own, for example when a contract generates it.
- This server: approved creation and delete/void are gated tools (`docs/why/03-forbidden-and-gated-operations.txt`); their texts
  say the user must void the accounting copy by hand; the harness never creates an Approved
  invoice unless explicitly asked.

## 14. Alerts client filter matches through the device (2026-10-04)

- Spec said: an alert type with no client id never matches the `ClientIds` filter.
- Production did: an alert with a null `ClientId` was returned when its device belonged to the
  filtered client.
- This server: the `list_alerts` text says the filter may leave some alerts out and notes the
  device match; tests keep the spec sentence as a fact about the spec.

## 15. Angle brackets are dropped from ticket text (2026-10-04)

- Spec said: descriptions and bodies are text/HTML.
- Production did: text in angle brackets vanished from a ticket description.
- This server: tool texts tell the model to write placeholders as `[an item]`, not `<an item>`.

## 16. A location's BillingContactIds came back as the text "[]" (2026-10-04)

- Spec said: a string of comma-separated ids.
- Production did: returned the text `[]` for a location with no billing contacts.
- This server: the harness reads `[]` as none and treats anything it cannot parse as unproven.

## 17. Payments endpoints and a looser contact create (2026-10-08)

- Spec said: the 2026-10-08 spec added `POST /v1/payments` and `DELETE /v1/payments/{paymentId}`, a
  `Payments` list on the invoice detail, and made `ClientId` optional when creating a contact.
- Seen: `POST /v1/payments` and `DELETE /v1/payments/{paymentId}` appeared in the published spec
  on 2026-10-07/08.
- This server: it has no tool for payments (accounting data that reaches the connected system),
  and its live guard refuses them. `create_contact` still requires `client_id`, although the
  spec now makes `ClientId` optional.

## 18. The spec renders differently between fetches (2026-10-08)

- Seen: 2026-10-08. The newly fetched spec stated a serialization style on every path
  (`simple`) and query (`form`) parameter, which are the OpenAPI defaults, and a diff against the
  previous index listed 76 changed operations, nearly all only for this.
- Production did: the operations behaved the same.
- This server: the diff and watcher compare a normalized contract: `normalize_spec()` in `scripts/spec_snapshot.py` drops a
  parameter `style` or `explode` that equals the OpenAPI default for its location before the
  index is built and the contract hash is computed, so both renderings give the same hash and
  rendering noise does not raise a false alarm
  (`docs/why/06-api-watcher.txt`).

## 19. Smaller items

- A null in a ticket PATCH counts as absent: a request with only `{"LeadAssigneeId": null}` is a 400
  ("must contain at least one field"), so nulls cannot clear ticket fields (2026-10-01).
- A client's `AlternateName` could not be cleared by sending "" or null (2026-10-01).
- A ticket source `Id` of 7 appears on existing tickets with no name and is not in the spec's enum
  (2026-10-01); the server returns 1 to 6 with labels and notes 7 as unnamed.
- Deleted ticket comments may still come back with their body (2026-10-01).
- A deleted catalog item reads as a 404 afterwards (2026-10-04).
- A key without the `Project` and `Forms` scopes gets HTTP 403 code
  080203 (2026-10-01); this is why those toolsets are off by default (`docs/why/10-toolsets.txt`).
