# Observed Gorelo API behavior: where production differed from, or went beyond, the published spec

This is a factual record, kept so that people reading the code can see why some checks look
stricter or odder than the published OpenAPI spec would suggest. It is not a complaint. The
Gorelo Public API is young and changes quickly; where the published spec did not mention
something, production simply answered differently, and this server was changed to match what
production does.

How to read it. Each entry gives the date it was seen (UTC dates; the hour is mostly not
recorded), what the published spec said at that time, what production did, and what this server
does about it. Everything here was observed with live calls, except entries 5, 7, 17, 18, 20 and 28,
which rest on the published description, its changelog or its text and say so, and the parts of entries
23 to 26 that say they rest on the published text. Every write was made against a test client, and the
reads of 2026-10-17 and 2026-10-24 were read-only requests. Production can change at any time, so
treat this as a log, not a promise. The reasoning behind the design is in `docs/why/`, and the tools that result are in `docs/tool-catalog.md`.

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
  What the path form does with a field that is left out was measured on 2026-10-24 (entry 32).

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

## 19. Phone validation became stricter (2026-10-12)

- Spec said: nothing new; the spec did not change.
- Production did: a fictional national number in area code 555 (5555550142) with region `US`, which
  was accepted before, is now refused with "Phone number is not valid for the specified country
  code", on a client location phone and on a contact mobile phone. A 555-01xx number in a real
  area code (2025550142) is accepted.
- This server: it does not validate numbers itself and passes Gorelo's refusal on; the live test
  harness now uses a number in a real area code.

## 20. Behavior changed only in description text (2026-10-12)

- Spec said: besides the operations it added (custom assets, API keys), the 2026-10-12 spec changed
  existing operations only in their descriptions. The create-ticket description now says `SendTicketCreatedEmail` and `IsUnread` are ignored when
  `CreatedOn` or `ClosedOn` is set (a backdated import), and that backdated records send no emails
  or notifications. Contact lists can include temporary contacts (no client; contact status 3,
  Temporary).
- Production did: not re-tested; the spec text is the source.
- This server: `create_ticket` refuses `send_created_email=true` or `is_unread=true` together with
  `created_on` or `closed_on`, so a request cannot promise an email that will not be sent. The tool
  texts for backdated tickets, comments, projects and tasks say no emails or notifications are
  sent. `scripts/spec_diff.py` now also lists description changes, because a change like this
  would otherwise not show in a diff.

## 21. Custom assets (2026-10-12)

- Spec said: `POST /v1/assets/custom`, `PATCH /v1/assets/custom/{customAssetId}`, a list and a
  hardware-types list; no read of one custom asset and no delete the server uses.
- Production did: create, update, and clearing the description, IP address and warranty date work
  as published (clearing the contact and the serial number was not tried live). A PATCH that names another client as `ClientId` is a 400 and
  moves nothing, so the client of a custom asset cannot be changed. There is no read of a single
  custom asset. A custom asset created through the API cannot be deleted through this server.
  Superseded in part on 2026-10-17: the new contract adds a read of one custom asset and says that
  a PATCH with a new `ClientId` moves the asset (entry 26; the move worked when it was first
  tried, on 2026-10-24).
- This server: `create_custom_asset` and `update_custom_asset` read the result back through the
  list. `update_custom_asset` needs the asset's current `client_id` and clears fields only through
  `clear_fields`. Delete stays forbidden.

## 22. Smaller items

- A null in a ticket PATCH counts as absent: a request with only `{"LeadAssigneeId": null}` is a 400
  ("must contain at least one field"), so nulls cannot clear ticket fields (2026-10-01).
- A client's `AlternateName` could not be cleared by sending "" or null (2026-10-01).
- A ticket source `Id` of 7 appears on existing tickets with no name and is not in the spec's enum
  (2026-10-01); the server returns 1 to 6 with labels and notes 7 as unnamed.
- Deleted ticket comments may still come back with their body (2026-10-01).
- A deleted catalog item reads as a 404 afterwards (2026-10-04).
- Gorelo answers HTTP 403, code 080203, when the API key lacks the scope that an area needs (for example `Project`,
  `Forms` or `Documents`); this is why those toolsets are off by default (`docs/why/10-toolsets.txt`).
- 50 operations read on 2026-10-24 (one record or page each: the core lists and records, agent sub-resources,
  documents, projects, tasks, comments, invoices, items, contracts, time entries, uptime, alerts, forms) returned
  exactly the fields of their published schemas: none extra and none missing.

## 23. The 2026-10-17 contract: documents, deletions and one custom asset (2026-10-17)

- Spec said: 14 operations were added: twelve for documents (list, one document, create, update,
  duplicate, PDF, versions, related items), `GET /v1/deletions` and
  `GET /v1/assets/custom/{customAssetId}`. Two comment bodies lost `BodyText`, a custom field
  can say `IsHidden`, and a PATCH of a custom asset with a new `ClientId` moves the asset.
- Production did: the document reads answered with the published shapes: the list, one document (the fields of the
  published model), its versions and its related items. The write calls were first run on 2026-10-24, once, against
  a test client: create, edit, attach a file, move under another document and back, duplicate, list and restore a
  version, archive and restore, and export the PDF all worked as published, and a changed body kept the attached
  file. Sharing has not been tried. This record does not claim an answer to the other questions that were open
  before that run: whether a url read from a document is accepted in `Attachments` of an update, whether a changed
  title keeps the attachments,
  how far an archive reaches below the document and what a restore does to the documents that were
  archived with it, whether the public link stays the same after it is turned off and on again,
  whether a move resets the sharing flags, whether the PDF export is recorded as an event, and what
  linking a structured document or a password does.
- This server: the `documents` toolset (off by default) follows the published text and checks
  every answer; its texts and `docs/OVERVIEW.md` say what is not proven.

## 24. Status filters: another id is a 400, and the defaults leave some records out (2026-10-17)

- Spec said: the status parameters of the lists name their scales and say "any other id is a
  400", and several lists leave Archived or Disabled records out when no status is given.
- Production did: each claim that was tried held. An id outside a status scale is a 400 with code 070101 and a
  message such as "Status id 2 is invalid." for every scale that was tried; items and tasks rest on the spec text.
  The list defaults follow the published text. An alert `TypeIds` of 21 answered 400 with the literal message
  "Type id {0} is invalid." (an unfilled placeholder in Gorelo's own message). The project status names are the
  spec's: 1 Not started, 2 In progress, 3 On hold, 4 Completed, 5 Closed.
- This server: it checks the same scales locally before any request, with a message that names the
  parameter and the scale, and its texts say what an omitted filter returns.

## 25. The deletions feed (2026-10-17)

- Spec said: `GET /v1/deletions` needs `TypeIds` (1 to 9, up to 10 values) and lists each record once
  with its latest deletion.
- Production did: the deletions feed answered with the published shapes; `TypeIds` 10 answered 400
  "Type id 10 is invalid."; no `TypeIds` answered 400 "TypeIds is required. Accepted values: 1
  Client, 2 Contact, 3 Contract, 4 Ticket, 5 Item, 6 Invoice, 7 Custom asset, 8 Project, 9 Task.".
- This server: `list_deletions` requires `type_ids` and passes a 403 for a type the key cannot read
  on, naming the parameter.

## 26. One custom asset, and moving it (2026-10-17)

- Spec said: `GET /v1/assets/custom/{customAssetId}` returns one custom asset, and a PATCH with a
  new `ClientId` moves it (its location and contact must belong to the new client).
- Production did: the read of an id that does not exist answers 404, code 070202, "The requested
  resource was not found.". The move was first run on 2026-10-24 against a test client: an asset moved to a second
  client and back kept the same `Id`.
- This server: `get_custom_asset` reads one asset; `create_custom_asset` and `update_custom_asset`
  read the asset back with it; `update_custom_asset` can move an asset and refuses a move that does
  not give a location of the new client.

## 27. An API key of the wrong form is answered by the gateway (2026-10-17)

- Spec said: a 401 comes back for a missing or wrong key, in the usual envelope.
- Production did: a value that is not an API key at all gets a 401 whose body is the gateway's own
  JSON, `{"error": "Invalid or missing API key format", "timestamp": ...}`, not a Gorelo envelope.
- This server: `health_check` reports any 401 as `ok: false` with status 401 and a hint that names
  the Gorelo host it called and `GORELO_BASE_URL`.

## 28. Custom fields can be hidden (2026-10-17)

- Spec said: a custom field entry has `IsHidden`; when it is true the `Value` is always null,
  whether or not a value is saved, `IsSet` is still reported, and the value can still be written.
- Production did: not probed beyond the published shape; a hidden field has not been tested live.
- This server: `update_client` confirms a saved hidden field by `IsSet` alone and says that the
  value was not read back.

## 29. The rate limit covers the whole API key (2026-10-24)

- Seen: 2026-10-24, with read-only requests on cheap lists, one after another and at fixed spacings.
- Spec said: nothing; no operation lists a 429.
- Production did: one bucket for the whole API key, not one per endpoint. A burst of about 6 requests
  gets through, then about 1 request per second is let through, and the bucket never holds more than
  about 6: after 10 or 30 seconds of idle a burst of 6 got through and the seventh was refused. When
  one endpoint answers 429 the others answer 429 at once. The 429 is not an envelope: it is gateway
  JSON (`error`, `message`, `retry_after`, `timestamp`) with content type `application/octet-stream`
  and the header `Retry-After: 1`. Every program that uses the same key draws on the same bucket.
- This server: the client has one token bucket for all its requests (a burst of 5, then 1 per
  second, first come first served); every attempt takes a token. A 429 empties the bucket, so every
  queued request waits together (also a request that already has its token and waits for one of the
  4 in-flight slots), and the request that got it is retried, for every method, up to 5 times with
  at most 30 seconds of `Retry-After` waits in all; time spent queued behind other requests is not
  counted in the 30 seconds. A read that changes nothing is also tried once more, after 2 seconds,
  when Gorelo answers 502, 503 or 504 or the connection is refused. A write, a multipart upload and
  the invoice PDF export are never sent again after anything but a 429 (Gorelo did not process a
  429). A script or a second connector that uses the same key as the server shares its budget.

## 30. An unknown agent id answers success with no record (2026-10-24)

- Spec said: the read of one agent can answer 404, as the other single-record reads do.
- Production did: `GET /v1/assets/agents/{agentAssetId}` with an agent id that does not exist answers
  HTTP 200 with `IsSuccess` true and `Data` null. Every other single-record read that was tried answers
  404, code 070202, "The requested resource was not found." (the apps, hardware and tags of an unknown
  agent, and a custom asset, ticket, document, script, uptime check, project, contact, client and time
  entry).
- This server: `get_agent` says that Gorelo has no agent with that id and points to `list_agents`,
  instead of reporting an answer it cannot use.

## 31. Limits of the lists that the spec does not state (2026-10-24)

- Spec said: some status filters say "up to 100 values"; the rest of the limits below are not stated.
- Production did, with read-only requests:
  - Ids in one filter: 100 are accepted; 101 and 500 are a 400 "A filter accepts at most 100 ids."
    (also on lists whose text does not say so).
  - `Query`: 200 characters are accepted; 201 and 1000 are a 400 "Query must be 200 characters or fewer.".
  - `PageSize` outside 1 to 200 is a 400 "PageSize must be between 1 and 200.".
  - A URL with a query of 8,000 characters reaches the application; one of 16,000 characters gets a 414
    from the gateway.
  - A date without a zone or without a time is read as UTC. A range whose end is before its start, and a
    date in the future, answer 200 with no rows.
  - A cursor may be reused with another page size. With another filter, or edited, it is a 400 "The supplied
    cursor is invalid.".
  - A query name that no description lists is a 400 every time: 47 such names were tried on 9 lists and
    every one was refused (code 070101, "This query parameter is not recognized by this endpoint.").
  - Sort fields: the ticket list takes `updatedOn` or `createdOn` only, the ticket comments `createdOn` only.
  - Answers carry no compression, no `ETag` and no `Last-Modified`.
- This server: clamps `PageSize`, checks every query name and refuses an id filter of more than 100 ids before
  sending (id filters only; the catalog's text filters, skus, vendors and part numbers, are not capped, because
  their limit has not been measured); any other refusal from Gorelo is passed on with its message, which names
  the filter and the limit.

## 32. Updating a contact keeps the location and the secondary emails (2026-10-24)

- Seen: live writes on a test client.
- Spec said: `PATCH /v1/contacts/{contactId}` requires the first name, last name, client and primary
  email; the text of `LocationId` says "Omit to keep the current location", and that of `SecondaryEmail`
  "Omit to keep the current secondary emails; send an empty array to clear them".
- Production did: a request with only the four required fields cleared the job title and the mobile
  phone it left out (the published text says the other such fields, region codes, department, time zone
  and description, are cleared too), and kept the location and the secondary emails, as the text says.
  `LocationId` null kept the location too, so the location of a contact cannot be cleared through the
  API. `SecondaryEmail` `[]` cleared the secondary emails. A region code without its phone, such as
  `OfficePhoneCountryCode` without `OfficePhone`, is a 400 (code 070101, "OfficePhone is required when
  OfficePhoneCountryCode is provided") on create and on update. A read never shows the secondary emails,
  but the `Query` of the contact list matches them.
- This server: `update_contact` reads the contact and sends the complete command, sends the secondary
  emails only when the caller gives the whole list (an empty list clears them; the user can see the
  secondary emails in the Gorelo app, so the model asks the user for the complete list before changing
  them), and no longer refuses an update until the user has decided about them. `clear_fields` does not accept `location_id`. A stored
  region code that has no phone is left out of the update and named in the `note` of the result (the
  note says the code was cleared only when Gorelo's answer shows it gone, otherwise that the answer still
  shows it). A region code given without its phone is refused by `create_contact` before any request and
  by `update_contact` after its read and before any write, when the contact has no such phone.

## 33. Size limits of a document (2026-10-24)

- Spec said: a title is at most 200 characters; no limit is stated for the body.
- Production did: a title of 201 characters is a 400 "Title can be at most 200 characters."; a body of
  524,026 characters was saved and read back whole; a body of about two million characters is a 400
  "Body can be at most 900,000 characters.".
- This server: checks the title length before sending; for the body it passes Gorelo's refusal on.
