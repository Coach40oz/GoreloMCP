"""The live write matrix: the write tools against the test client and the second client, then clean up.

    python -m scripts.live.write_matrix [--skip-email] [--only AREA,...] [--leftovers] [--with-items] [--with-invoices]
                                        [--with-custom-assets] [--with-asset-move] [--with-custom-properties]
                                        [--with-documents] [--with-approved-invoice [--approve-via-update]]
                                        [--with-alert-status] [--with-asset-types] [--with-client-locations]
                                        [--with-client-domains] [--with-contact-tags] [--with-product-charges]
                                        [--with-invoice-update] [--with-contracts] [--with-comment-edits]
                                        [--with-ticket-source]

Run id MCPTEST-<UTC yyyymmddHHMMSS>. Every record the run creates is named with it, announced in the run manifest
(.live-runs/<run id>.json) BEFORE the create call and recorded with its id right after (the details contract of
scripts/live/manifest.py: tickets record contact_id and cc_contact_ids, comments ticket_id and private, tasks
and sections project_id, project comments project_id and task_id, attachments, side conversations and approvals
their ticket_id, invoices status_id, number and display_number). The server is built in process behind ONE write-mode LiveGuard with require_intents=True plus a
pacer (one request per second); everything the matrix sends is a tool call (there is no raw HTTP client). A guard
refusal stops the run at once (the remaining areas are "not run"); any other failure only fails its own area.
scripts.live.cleanup.run_cleanup ALWAYS runs afterwards, in a `finally`, also when an area raised or the run was
interrupted, and its leftovers decide the exit status together with the areas.

Before anything is created the run reads each configured client (guard.verify_site_clients: the name must be the configured one) and
refuses to start, with exit status 2 and "cannot start: ..." (guard.ClientNotActive), unless the TEST CLIENT is Active (Status.Id 1;
since contract a7954b24844b a client is 1 Active, 3 Credit hold or 4 Archived). Nothing is sent and no manifest is made. The operator has to
make the test client Active again in the Gorelo app: this harness never changes the status of a client itself. The cleanup (also the
standalone one) has no such rule, so it can always clean up what an earlier run left.

Areas, in this order (--only takes a comma separated list and keeps this order; "forms" is another name for
"projects"). Names are prefixed with the run id; the test client unless noted.

    clients    create_client of a temporary client with a location phone and region US (the region fix),
               update_client its alternate name (this is the only update_client call of this area: the test client itself is
               never given a name or a status, and only the custom_properties area, with its flag, may set a custom field),
               read it back
    contacts   create_contact (mobile phone, region US); update_contact job_title WITHOUT secondary_email (Gorelo keeps the
               secondary emails an update leaves out, live 2026-10-24): it must go through with at least its read and exactly
               one write (the client may send the read again after a 429, a 502, 503 or 504 or a refused connection, which
               adds reads and no write), then check by a read that the mobile phone survived (the tool re-sends what a PATCH
               would clear)
    tickets    status New, type Incident, group Everyone, source Api (6) and priority Normal are resolved by name;
               create_ticket plain; create_ticket backdated and Closed (offsets in the past); get_ticket by GUID,
               number and display number; update_ticket title and the operator user as watcher (WatcherIds must read back [operator user]),
               then clear_fields watcher_ids (WatcherIds must read back empty), and only then lead assignee the operator user in
               a later update (a technician cannot be lead and watcher at once: Gorelo answers 400 "Technician
               already exists", so the two never share a PATCH); no tenant tag is ever set; billing read-fill (role
               and work type, then only billable_status_id 2: the other two must survive); search_tickets
               (client_id=the test client) finds both run tickets
    comments   private comment, list it, get it, delete it (confirm=true); upload_attachment (text) and attach it
               to a new private comment
    email      the second client and the operator contact ONLY: create_ticket with send_created_email=true, a public comment,
               a status change, a side conversation to the operator address with a side_conversation comment, an
               approval for the operator contact with an approval comment (recorded as skipped when Gorelo rejects the
               approver, or when the stored approvers are not only the operator contact); --skip-email leaves this
               area out
    time       create_time_entry (the operator user, no service line, billable status 2, explicit times; the entry's User.Id
               must read back the operator user: the published User object, which production answers since 2026-10-03. The
               flat UserId (the operator user) that production answered on 2026-10-02, with no User object, still passes,
               with a note that it is drift; any other id, or neither field, fails the area), update_time_entry comment, delete_time_entry
               (confirm=true) again while Outcome is Reopened (at most 3 calls)
    items      create_item (product scoped to the test client), update_item, delete_item (confirm=true). SKIPPED unless
               --with-items is given (it creates, updates and deletes a catalog product on the test client), and also
               skipped, with the same reason, when --only items is given without it: the three item tools cannot
               even be called then. A live run passed it, and a deleted item answers GET /v1/items/{itemId}
               with a 404 (code 070202)
    custom_assets
               list_custom_asset_hardware_types (the first row is used, no tenant id is hardcoded), the test client's default
               location (list_client_locations), create_custom_asset (name `<run id>-asset`, a serial number, ip 192.0.2.10 from
               the TEST-NET-1 documentation range and a warranty date; the tool reads the asset back by id), a fresh get_custom_asset,
               update_custom_asset WITHOUT client_id (new name suffix, ip 192.0.2.11, a description), then update_custom_asset with
               client_id = the asset's current client and clear_fields description, ip_address and warranty_end_date (they must read
               back null or empty; naming the current client moves nothing), and a negative check: update_custom_asset with the
               second client as client_id and NO location_id must be refused LOCALLY by the tool after its read of the asset
               (since contract a7954b24844b a different ClientId MOVES the asset and needs a LocationId of the new client; the
               check passes only when every request of that call was a GET (that read, and the client's own retry of it after a
               429, a 502, 503 or 504 or a refused connection, if any), the guard let no write through and the tool's
               error has no HTTP status and says "location_id: required when client_id moves the asset"; anything else fails
               the area, and a PATCH that carried the second client would be refused by the guard, which stops the run).
               With --with-asset-move the area then MOVES the asset to the second client and back: the second client must be
               Active and have a location (list_client_locations; else the move is skipped with a note), the manifest details of
               the asset are set to the second client BEFORE the move is sent (the guard relies on them), and the record's residue
               text says the asset MAY be on the second client until Gorelo answers the move, then names the client; then
               update_custom_asset with client_id = the second client and location_id = its location (the answer must read back
               ClientId and LocationId as asked and the name and serial number as before), a fresh get_custom_asset, and the way
               back: update_custom_asset with client_id = the test client and location_id = the location the asset was created
               at (the answer and a fresh get_custom_asset must read the test client again); the details go back to the test client
               when Gorelo answers the way back. The way back is tried even when a check of the way out failed, and when it fails
               the record says where the asset may be (the second client, or "unknown"). The asset has no contact, so the contact rules
               of a move (a contact of the new client or a clear) are not exercised live. SKIPPED unless --with-custom-assets is
               given (and also skipped, with the same reason, under --only custom_assets without it: its tools cannot even be called
               then); --with-asset-move needs --with-custom-assets (usage error otherwise).
               A custom asset is not deleted by this harness (the guard never allows DELETE /v1/assets/custom/{id}, and this server
               has no tool for it), so this area leaves a residue: the asset stays on the test client (or, after a move that could
               not be undone, on the second client) until somebody removes it in the Gorelo app. The manifest records it before and
               after the create; the cleanup sends nothing for it and reports it as a known undeletable record
               (CUSTOM_ASSET_RESIDUE), and the summary prints a line telling the operator to remove it in the Gorelo app. The cleanup
               never moves a stranded asset back: one left on the second client is moved back by hand, in the Gorelo app (a manual
               step). Never part of a default run
    custom_properties
               get_client of the TEST CLIENT, then ONE client custom field value on that client itself (every other area
               creates records of its own, so this is the only write to the test client; the guard otherwise refuses any PATCH of it).
               SKIPPED unless --with-custom-properties is given (also under --only custom_properties without it: nothing is
               sent, not even a read). Every update_client call with custom fields reads the client first (a GET) and refuses
               locally an Id the client does not list (no PATCH, no second GET); otherwise it sends the PATCH, reads the client
               again (a second GET) and checks on that read that every custom field it did not send is as it was. The area keeps {Id: (IsSet, Value)} from its first read and is SKIPPED, with the
               Ids of the fields that are set (never a value), while ANY custom field of the test client is set (IsSet true): an
               update that Gorelo applies as a replace would then have something to destroy, and with nothing set it has not. The
               field is the unset one (IsSet false) whose Type.Name contains "text" (case-insensitive) with the lowest Id among
               those whose Value is not null (an unset field shows its default there, so a clear can restore it); only when none
               has a default, the lowest Id (the note says which case applied). With no custom field at all, or no unset text
               field, the area is SKIPPED (the reason lists each field's Id, Type.Name and IsSet, never a value: the first eight in
               the reason, all of them in a note). The field is recorded in the manifest (kind custom_property:
               Id, Name, Type.Name, was_set false, the text, value_before: the Value of the first read) BEFORE the first write
               (the note "recorded in the manifest" is written then): the guard accepts a write only to a recorded field, and the
               cleanup knows what to undo. Then update_client with custom_properties=[{id, value: the run id}] (the answer's entry
               must have IsSet true and Value equal to the text), a fresh get_client (the same), update_client with
               clear_custom_property_ids=[id] (IsSet false in the answer), a fresh get_client (the same); after each of these four
               answers every other custom field must read exactly as at the first read (IsSet, Value, still listed, none new) and,
               after the clear, the chosen field must be back at its first IsSet false and Value: any difference FAILS the area
               at once, naming the field Ids only and telling the operator to check them in the Gorelo app. Only then is the
               record marked cleaned and the note "set, read, cleared, read" written. Then a negative check: update_client with
               custom_properties naming CUSTOM_PROPERTY_UNKNOWN_ID (9000000000000, an Id that is not a client custom field) must be
               REFUSED LOCALLY by the tool, after its read of the client and before any PATCH: it passes only when the one
               request of that call was that GET, the guard let no write through and the tool's error has no HTTP status and names
               the Id (a retry of that GET by the client, after a 429, a 502, 503 or 504 or a refused connection, is not another request
               of the tool and does not fail the check); anything else fails the area (a PATCH that carried the Id would be refused by the guard, which stops the run).
               A last get_client must show every custom field as at the first read. No custom property PATCH error is expected, so
               the run learns nothing of Gorelo's own names for such errors. If the run stops after the set and before
               the clear succeeded, the cleanup clears the field ONLY when the manifest says it was unset before AND its current Value
               still equals the run id (somebody may have changed it since) AND the cleanup's own read shows no other custom field
               of the test client set (a clear that Gorelo applied as a replace would wipe it: the cleanup then sends nothing); otherwise
               the field Id and the client are reported as a LEFTOVER (exit status 1) with how to clear it by hand. Other custom
               fields that read differently after the cleanup's clear are a LEFTOVER too, named by their ids first and kept in the
               manifest until somebody acknowledges them (scripts/live/cleanup.py). A required text field with no default cannot be
               cleared back to nothing (Gorelo refuses the null), so it would stay set: that too is a leftover. Never part of a default run
    documents  ONE top-level document of the test client and a copy of it (two documents in all; every title starts with the run id), run
               through every document tool except share_document, which is not a tool of the matrix: create_document; update_document (title,
               body, alternate name, star: the tool reads the document first); a fresh get_document; duplicate_document (the copy
               must start with the star, the public link and the portal off); list_documents (client, run id as query: both are listed);
               upload_attachment (item_type document) and update_document attachments (the tool reads the document first and the answer
               must list the file); the NEGATIVE CHECK: update_document with a list that leaves the current file out must be refused
               LOCALLY after its read (GETs only, no write, no HTTP status, "would be removed"); update_document body only (the file must
               still be attached: it probes whether a changed body keeps the attachments); update_document clear_fields attachments
               (the answer lists none); export_document_pdf (a file: it must start with %PDF); list_document_versions, get_document_version
               and restore_document_version of the OLDEST version (the title is the original one again); add_document_related_items (the
               copy), list_document_related_items, remove_document_related_item (confirm=true) and a list that no longer shows the link;
               move_document under the copy and back to the top level of the test client (the parent must list it as a child and then
               not); unshare_document (public_link and publish_to_portal: both must read false); archive_document and restore_document
               of the copy; and at the end archive_document of both, then list_documents (the active list shows neither, status_ids [2]
               shows both). Sharing is NEVER turned on: share_document is not callable here and the guard refuses a true flag. A document
               CANNOT BE DELETED through the API, so every document is a known residue from the moment it exists: the area archives it
               and records the outcome (DOCUMENT_RESIDUE_TAIL), the cleanup archives one a stopped run left active, and the summary prints
               a reminder line (the Documents area of Gorelo, search for the run id). The uploaded file is a known undeletable record too.
               SKIPPED unless --with-documents is given (and also skipped, with the same reason, under --only documents without it: the
               document tools cannot even be called then). The steps are also the probes of the unprobed document assumptions: each
               answer is checked, a note records what Gorelo did. Never part of a default run
    invoices   list_items for the first active product that belongs to nobody or to the test client, create_invoice (always a
               Draft for the test client with one line and no recipients), get_invoice, list_invoices by the run label,
               export_invoice_pdf (a PDF file; the export event is recorded on the test invoice), then delete_invoice
               (expected status Draft, confirm=true; the answer says StatusId 6) and a last list_invoices that must no
               longer show it. A Draft uses up one invoice number. SKIPPED unless --with-invoices is given, and also
               skipped, with the same reason, when --only invoices is given without it: the invoice tools cannot even
               be called then. create_approved_invoice is never called here (it pushes the invoice to the accounting
               system): only the approved_invoice area below calls it
    approved_invoice
               (with --approve-via-update the one invoice is a DRAFT of $1 made with create_invoice (the tax-exempt rate as tax_id, no recipient, no dates) whose
               stored total (above 0 and at most 1) and total_tax (0) are checked and recorded in the manifest, then approved with
               approve_invoice (confirm=true; a PATCH of StatusId 5; the 429 retries are off around that call), instead of create_approved_invoice,
               which that run never calls; everything after the approval is as below; the accounting system's copy
               is voided by hand afterwards)
               ONE Approved invoice of $1 for the test client, created, waited for until Gorelo has pushed it to the connected
               accounting system, then voided at once. SKIPPED unless --with-approved-invoice is given, and then it is
               the ONLY area of the run and the run has no other flag (--only approved_invoice alone: any other area,
               and --with-items, --with-invoices, --leftovers or --skip-email, is a usage error, exit status 2, before a
               request is sent), with its own tool set (APPROVED_INVOICE_TOOLS) and a guard built with
               allow_approved_invoice. The create call alone (create_approved_invoice, which also reads the new invoice
               back) runs with the 429 retries of the run's Gorelo client switched off (max_429_retries=0, put back right
               after the call, also when it raises), so the one create is never sent twice: a 429 on it is a plain
               rejected create that settles its intent ("Gorelo is rate limiting requests (HTTP 429) ...; it did not
               process this request"), not a guard refusal. Every other request of the run keeps the client's normal
               retries.
               Preconditions, checked before any write (a skip with the reason, nothing sent, when one does not hold): the gate that
               nobody can be emailed. No published contact status says that a contact cannot be emailed, so NOTHING PROVES IT and the
               area FAILS CLOSED until UNEMAILABLE_CONTACT_STATUS_IDS names one: while it is empty the area is skipped before its
               first request, with APPROVED_INVOICE_GATE_CLOSED as the reason. The operator has to decide which state proves it. Once
               that constant names
               one, the area needs at least one contact of the test client (list_contacts with status_ids 1, 3 and 4) and every one
               must be in such a state (no contact at all proves nothing), no location of the test client names a billing contact
               (list_client_locations: BillingContactIds empty, null or the text []), an item as in the invoices area
               (list_items) and the tax-exempt rate: list_taxes must hold exactly ONE rate with Percentage 0 whose Name or Code is
               exactly "Tax Exempt" (case-insensitive); with none, or more than one, the area is skipped with a note (nothing is
               guessed). Then the intent ("invoice", status_id 5, tax_id and tax_percentage 0 of that rate, the label
               `<run> approved invoice`) and create_approved_invoice (the test client, ONE line: that item, quantity 1, unit price
               1.0, the label as its text, tax_id of the tax-exempt rate, which the guard compares with the id the intent recorded: an
               omitted or null TaxId would give the item's own tax, so this area names the id itself (no_tax would send the same id); the
               label as reference; no recipients and no dates, Gorelo's defaults; confirm=true), recorded in the manifest the moment it answers
               with the status Gorelo STORED (5 when the answer has none, never 1), its number and display number.
               Checks: Status.Id 5 (a Paid 3, which Gorelo created because the total was 0, fails the area and is left
               for the user: Gorelo refuses to delete or void a Paid invoice), the client, the reference, the one line,
               Total above 0, IsEmailSent false and EmailSentOn null (a note, written loudly, when not), and a note of
               the Number and DisplayNumber it used up. Then get_invoice every 5 s for at most 120 s until ExternalId is
               set (the push to the accounting system). A read that fails meanwhile (an HTTP error, a timeout or a 429
               that outlasts the client's own retries: a GET writes nothing) only means "not synced yet": it is noted
               (once per distinct text; the trace id that ends an error is left out when texts are compared) and asked
               again at the next poll, and the wait ends only on a status other than Approved or when the 120 s run
               out. If ExternalId never is set, nothing is voided and the area FAILS saying "approved invoice
               <DisplayNumber> not yet synced to accounting; check the accounting system, then void it with: python -m
               scripts.live.cleanup <manifest> --void-approved (this voids it in Gorelo only: void its copy in the
               accounting system by hand too)". Once ExternalId is set (noted, with whether PaymentLink is set):
               delete_invoice (the number, expected status Approved, confirm=true; the answer must say StatusId 4, Void;
               when Gorelo still answers 429 to its lookup or its DELETE after the client's own retries, the area asks
               again, up to 3 attempts in all, because Gorelo did not process that request), then a note that
               the void is in Gorelo only (check the accounting system and void the invoice there too: Gorelo's void is
               not pushed to the connected accounting system, seen with Xero), get_invoice (Status.Id 4) and list_invoices (the test client, the label
               as query), which must still list it. The read after the void also checks TotalTax 0 and Total 1.0 (the
               line carried the tax-exempt rate and billed $1): when it reads otherwise, a loud note says the no-tax check FAILED. That is
               only a note, made after the void, so it can never stop or skip the void or its bookkeeping. A voided
               invoice cannot be removed, so it is recorded with the outcome "voided by the write matrix (still listed
               as Void)": a known residue, neither cleaned nor a failure (like the uploaded file). The summary of the run
               repeats the reminder about the void, for every approved invoice of the run that was voided, as its last
               line before the result
    alert_status
               ONE existing New alert of the test client (list_alerts for the client, then list_alerts with status dismissed on EVERY page; an
               alert is skipped when any Dismissed alert of the client shares its Type and subject ids (AgentAssetId, CheckId, UptimeId,
               DomainId, ContractId, CustomAssetId; the Title is not compared), because Gorelo moves the whole STACK of an alert with it and
               restoring to New would move that one back too; none left: skipped with a note). The alert is recorded in the manifest (kind
               alert, client_id and status_before 1) BEFORE the first change; set_alert_status dismissed (UpdatedIds must name it and are
               recorded as dismissed_ids; get_alert reads Dismissed with DismissedOn set), set_alert_status new (get_alert reads New with
               DismissedOn null; an id in its UpdatedIds that the dismissal did not move FAILS the area, naming the ids as a leftover for
               the user); the cleanup restores an alert a stopped run left dismissed, only when its DismissedOn is not before the run's start
               and only the ids recorded (any other id it moved is a leftover that names it). post_alert is never called. SKIPPED unless
               --with-alert-status is given
    asset_types
               create_asset_type (Name = the run id plus text), update_asset_type (rename), list_asset_types (shows the new name once),
               delete_asset_type (confirm=true) and a list that no longer shows it. Asset types belong to the whole account, not to a client,
               so the name starts with the run id. SKIPPED unless --with-asset-types is given
    client_locations
               create_client_location (the test client, Name = the run label), update_client_location (city, address2), get_client_location,
               list_client_locations (lists it), delete_client_location (confirm=true; Gorelo marks the location inactive, so it must leave the
               list) and a last get_client_location whose answer is only noted. Never the default location. SKIPPED unless
               --with-client-locations is given
    client_domains
               create_client_domain (the test client; Name mcptest-<run>.example under the reserved .example TLD, with an explicit expiration
               date a year out, so no registry lookup is asked), update_client_domain (the date), delete_client_domain (confirm=true) and a
               get_client whose Domains no longer lists it. SKIPPED unless --with-client-domains is given
    contact_tags
               list_contact_tags (the first tag), create_contact (an MCPTEST contact of the test client), update_contact tag_ids=[tag] (TagIds
               must read back [tag]), update_contact job_title WITHOUT tag_ids (update_contact sends no TagIds then, so this step tests Gorelo's
               own omit rule, "omit to leave it unchanged": the NOTE says whether Gorelo kept the tags or not, and a Gorelo that dropped them
               is a loud note, not a failure of the area) and update_contact tag_ids=[] (TagIds must read back []). The contact is removed by
               the cleanup. SKIPPED unless --with-contact-tags is given
    product_charges
               list_items (the first active product of nobody or of the test client), the run's plain MCPTEST ticket (made here when the
               area runs alone), create_product_charge (No charge, billable status 2, so it can never be invoiced), update_product_charge,
               get_product_charge, list_product_charges (lists it), delete_product_charge (confirm=true) and a list that no longer shows
               it. SKIPPED unless --with-product-charges is given
    invoice_update
               list_items, create_invoice (a Draft with two lines A and B), then update_invoice: reference and both dates; add_lines (line C);
               update_lines (line A); remove_line_ids (line C). After EVERY step the lines it did not name must read exactly as before (the tool
               sends {Id} alone for them: the note records that Gorelo keeps the fields an entry with only an Id leaves out). delete_invoice
               of the Draft. Then the TAX PROBE: list_taxes, the first rate with Percentage above 0 (none: the probe is skipped with a note) and
               the ONE Tax Exempt rate (Percentage 0, Name or Code "Tax Exempt"; none or several: skipped with a note, the tool would refuse
               no_tax), a temporary MCPTEST item taxed at the first rate (create_item with tax_id), the intent of a second Draft (records tax_id
               of the exempt rate and tax_percentage 0 before the write, the guard allows no other TaxId on a Draft line), that Draft with one
               line of the item with no_tax=true (the tool looks up the Tax Exempt rate and sends its id as TaxId), a fresh get_invoice, and a
               NOTE that says which TaxId the tool used and which TaxId and tax amount Gorelo applied. The area FAILS unless the line shows
               TaxAmount 0 and the invoice TotalTax 0 (the Draft and the item are deleted first); the `warning` create_invoice added when it saw
               a no_tax line that still shows tax is recorded in the note too. Each Draft uses up an invoice
               number. SKIPPED unless --with-invoice-update is given
    contracts  list_items, create_contract (confirm=true; an MCPTEST Hourly contract, labor terms 2, on the test client with one service line
               and two line items billed as No charge; never a Block Hours line, never auto_approve_and_send true), update_contract (name and
               the quantity of line item A), add a second service line, remove it, check after each step that the service line and the
               line item that were sent with an Id only kept their values (Quantity, UnitPrice, Description, DiscountPercent, Tax,
               BillableStatus), then archive it (update_contract status=archived, StatusId 3). A contract CANNOT be deleted through the API
               (the guard refuses the DELETE): the archived contract is a KNOWN RESIDUE, like an archived document. SKIPPED unless
               --with-contracts is given, and SKIPPED whatever the flags say while create_contract and update_contract are held back
               (tools._common.HELD_TOOLS, since 1.5.1: Gorelo has not released contract writes), with the reason in the summary
    comment_edits
               an MCPTEST project of the test client (as in the projects area: the Project scope, a project type and the Everyone group),
               a section and a task, a Private project comment and a Private task comment, update_project_comment and update_task_comment
               (BodyHtml must read back the new body, also through get_project_comment), then delete both comments and the task; the project
               is soft deleted by the cleanup. SKIPPED unless --with-comment-edits is given (or with the Project scope missing)
    ticket_source
               the run's plain MCPTEST ticket (made here when the area runs alone): update_ticket source_id = another source of 1 to 6 (7
               Messenger is read back only, never written), get_ticket (Source.Id must read back), then update_ticket back to Api. SKIPPED
               unless --with-ticket-source is given
    uptime     create_uptime_check (http, the probe domain URL, seattle, every 60 minutes), set_uptime_maintenance
               starting now (start is the current UTC time written with an offset, 60 minutes, a reason: Gorelo
               refuses a window without a start), update_uptime_check description, delete_uptime_check
               (confirm=true)
    projects   list_projects and list_forms first: a 403 with the missing-scope message is recorded as "skipped:
               scope missing" and nothing else is sent; with the Project scope: list_project_types (the first
               type) and the Everyone group (both are required by create_project), create_project, a section, a task
               in it, a private task comment, then delete the comment and the task (the project itself is soft
               deleted by the cleanup). Per the tool texts, a plain create_project fires the app's project-created
               notification (Gorelo does not say who receives it) and creating the task fires the app's automation
               triggers; the area sends no created_on, the one documented way to suppress both, so it says so in a note
               (see the create_project and create_project_task texts). It makes no forms write: the Forms scope is only probed

Who a ticket would email is checked on what Gorelo STORED, before anything else is sent for that ticket. Right
after create_ticket's read-back (every run ticket, in Matrix.new_ticket) the stored ticket must have exactly the
audience the matrix asked for: the two test-client tickets ContactId null and CcContactIds empty, the second-client email ticket
ContactId the operator contact and CcContactIds only the operator contact. The STORED values are written into the manifest (ticket details
contact_id and cc_contact_ids, with manifest.update_details), so the guard's public-comment rule judges the truth;
until then the ticket has no contact_id in its details and the guard allows no public comment on it. A mismatch,
a read-back that failed and a ContactId or CcContactIds that is missing or of an odd shape all fail the area at
once: no comment, no status change, no approval follows. For the approval, create_ticket_approval's read-back
must list approvers and every one must be the operator contact before the approval comment is posted; otherwise the
comment is skipped and the reason recorded. The side conversation and the approval are recorded with their
ticket_id, and the guard lets a side conversation (approval) comment go only into the one this run created on
that ticket.

Live requests per area (method and spec path, in the order they are sent; the tests pin these lists):

    clients:
        POST /v1/clients  # create_client: temporary client, location phone and region US
        GET /v1/clients/{clientId}/locations  # the location it got
        PATCH /v1/clients/{clientId}  # update_client: alternate name of the temporary client
        GET /v1/clients/{clientId}  # read it back
    contacts:
        POST /v1/contacts  # create_contact
        GET /v1/contacts/{contactId}  # update_contact reads the contact first
        PATCH /v1/contacts/{contactId}  # and sends the complete contact
        GET /v1/contacts/{contactId}  # read back: the mobile phone survived
    tickets:
        GET /v1/tickets/statuses  # lookups by name, once per run (cached for the later areas)
        GET /v1/tickets/types
        GET /v1/organization/groups
        GET /v1/tickets/sources  # network reads since 1.5.0 (the priorities and the sources were local tables before)
        GET /v1/tickets/priorities
        POST /v1/tickets  # plain ticket
        GET /v1/tickets/{ticketId}  # create_ticket reads the ticket back; its stored audience is verified here
        POST /v1/tickets  # backdated and Closed
        GET /v1/tickets/{ticketId}  # read back, audience verified again
        GET /v1/tickets/{ticketId}  # get_ticket by GUID
        GET /v1/tickets  # get_ticket by number (Query)
        GET /v1/tickets/{ticketId}
        GET /v1/tickets  # get_ticket by display number (Query)
        GET /v1/tickets/{ticketId}
        PATCH /v1/tickets/{ticketId}  # title and the operator user as watcher
        GET /v1/tickets/{ticketId}
        PATCH /v1/tickets/{ticketId}  # clear_fields watcher_ids
        GET /v1/tickets/{ticketId}  # WatcherIds is empty
        PATCH /v1/tickets/{ticketId}  # lead assignee the operator user, never in the same PATCH as a watcher
        GET /v1/tickets/{ticketId}
        GET /v1/billing-roles
        GET /v1/work-types
        GET /v1/tickets/{ticketId}  # update_ticket reads the current billing override
        PATCH /v1/tickets/{ticketId}  # billing role and work type
        GET /v1/tickets/{ticketId}
        GET /v1/tickets/{ticketId}  # billable status 2 alone: the other two parts are read and sent back
        PATCH /v1/tickets/{ticketId}
        GET /v1/tickets/{ticketId}
        GET /v1/tickets  # search_tickets for the test client
    comments:
        POST /v1/tickets/{ticketId}/comments  # private comment
        GET /v1/tickets/{ticketId}/comments/{commentId}
        GET /v1/tickets/{ticketId}/comments  # list_ticket_comments
        GET /v1/tickets/{ticketId}/comments/{commentId}  # get_ticket_comment
        DELETE /v1/tickets/{ticketId}/comments/{commentId}  # delete_ticket_comment
        POST /v1/attachments  # upload_attachment (text file)
        POST /v1/tickets/{ticketId}/comments  # private comment carrying the attachment
        GET /v1/tickets/{ticketId}/comments/{commentId}
    email:
        POST /v1/tickets  # the second client, the operator contact, send_created_email=true
        GET /v1/tickets/{ticketId}  # read back: ContactId the operator contact and CcContactIds only the operator contact, else the area stops here
        POST /v1/tickets/{ticketId}/comments  # public comment (emails the operator contact)
        GET /v1/tickets/{ticketId}/comments/{commentId}
        PATCH /v1/tickets/{ticketId}  # status change
        GET /v1/tickets/{ticketId}
        POST /v1/tickets/{ticketId}/conversations/side-conversation  # to the operator address
        POST /v1/tickets/{ticketId}/comments  # side_conversation comment, into the side conversation just created
        GET /v1/tickets/{ticketId}/comments/{commentId}
        POST /v1/tickets/{ticketId}/conversations/approval  # approver the operator contact
        GET /v1/tickets/{ticketId}/approvals/{approvalId}  # every stored approver must be the operator contact, else no comment
        POST /v1/tickets/{ticketId}/comments  # approval comment, into the approval just created
        GET /v1/tickets/{ticketId}/comments/{commentId}
    time:
        POST /v1/time-entries  # the operator user, no service line, billable status 2
        GET /v1/time-entries/{timeEntryId}
        PATCH /v1/time-entries/{timeEntryId}  # comment
        DELETE /v1/time-entries/{timeEntryId}  # again while Outcome is Reopened, at most 3 calls
        GET /v1/time-entries/{timeEntryId}  # recorded only: a deleted entry should be a 404
    items:
        POST /v1/items  # product scoped to the test client (only with --with-items)
        GET /v1/items/{itemId}
        PATCH /v1/items/{itemId}
        GET /v1/items/{itemId}
        DELETE /v1/items/{itemId}
    invoices:
        GET /v1/items  # list_items: the first active product of nobody or of the test client (only with --with-invoices)
        POST /v1/invoices  # create_invoice: a Draft for the test client, one line, no recipients
        GET /v1/invoices/{invoiceId}  # create_invoice reads the invoice back
        GET /v1/invoices/{invoiceId}  # get_invoice
        GET /v1/invoices  # list_invoices by the run label
        GET /v1/invoices/{invoiceId}/pdf  # export_invoice_pdf: an export event on the test invoice
        GET /v1/invoices/{invoiceId}  # get_invoice again: still a Draft, else the area stops and leaves it
        GET /v1/invoices  # delete_invoice finds the invoice by its Number
        DELETE /v1/invoices/{invoiceId}  # Draft: deleted (no longer listed); the answer says StatusId 6
        GET /v1/invoices  # list_invoices: no longer listed
    custom_assets:
        GET /v1/assets/custom/hardware-types  # the first row is used (only with --with-custom-assets)
        GET /v1/clients/{clientId}/locations  # the test client's default location
        POST /v1/assets/custom  # create_custom_asset: name <run id>-asset, serial number, ip 192.0.2.10, warranty date
        GET /v1/assets/custom/{customAssetId}  # create_custom_asset reads the asset back by id
        GET /v1/assets/custom/{customAssetId}  # get_custom_asset: a fresh read
        PATCH /v1/assets/custom/{customAssetId}  # update_custom_asset without client_id: rename, ip 192.0.2.11, description
        GET /v1/assets/custom/{customAssetId}  # and its read-back by id
        GET /v1/assets/custom/{customAssetId}  # update_custom_asset with client_id = the current client reads the asset first
        PATCH /v1/assets/custom/{customAssetId}  # clear_fields description, ip_address, warranty_end_date (the same client moves nothing)
        GET /v1/assets/custom/{customAssetId}
        GET /v1/assets/custom/{customAssetId}  # negative check: the second client without location_id: the tool reads the asset and refuses locally
        GET /v1/clients/{clientId}/locations  # the second client's location (only with --with-asset-move)
        GET /v1/assets/custom/{customAssetId}  # update_custom_asset with the second client and its location_id reads the asset first
        PATCH /v1/assets/custom/{customAssetId}  # the MOVE: ClientId = the second client, LocationId = its location
        GET /v1/assets/custom/{customAssetId}  # read-back by id: the asset reads back at the second client
        GET /v1/assets/custom/{customAssetId}  # get_custom_asset
        GET /v1/assets/custom/{customAssetId}  # the move back reads the asset first
        PATCH /v1/assets/custom/{customAssetId}  # ClientId = the test client, LocationId = the location it was created at
        GET /v1/assets/custom/{customAssetId}  # read-back by id: the asset is on the test client again
        GET /v1/assets/custom/{customAssetId}  # get_custom_asset
    custom_properties:
        GET /v1/clients/{clientId}  # get_client of the test client: its custom fields (only with --with-custom-properties)
        GET /v1/clients/{clientId}  # update_client reads the client first: every Id it sends must be one of its custom fields
        PATCH /v1/clients/{clientId}  # update_client custom_properties: the run id as the text of the chosen unset text field
        GET /v1/clients/{clientId}  # update_client reads the client again: the fields it did not send must be as they were
        GET /v1/clients/{clientId}  # fresh get_client: the field is set to that text, every other field as at the first read
        GET /v1/clients/{clientId}  # update_client reads the client first
        PATCH /v1/clients/{clientId}  # update_client clear_custom_property_ids: the field is cleared
        GET /v1/clients/{clientId}  # update_client reads the client again
        GET /v1/clients/{clientId}  # fresh get_client: the field is back at its first state, every other field as at the first read
        GET /v1/clients/{clientId}  # negative check: update_client reads the client and refuses an Id that is not a custom field; no PATCH, no second read
        GET /v1/clients/{clientId}  # last get_client: every custom field as at the first read
    documents:
        GET /v1/documents  # scope probe (page size 1; only with --with-documents); a 403 with code 080203 ends the area here (skipped)
        POST /v1/documents  # create_document: a top-level document of the test client
        GET /v1/documents/{documentId}  # update_document reads the document first
        PATCH /v1/documents/{documentId}  # title, body, alternate name, star
        GET /v1/documents/{documentId}  # a fresh get_document
        POST /v1/documents/{documentId}/duplicate  # duplicate_document: the copy
        GET /v1/documents  # list_documents: the test client's documents with the run id in the title (both are listed)
        POST /v1/attachments  # upload_attachment (item_type document): a small text file
        GET /v1/documents/{documentId}  # update_document attachments reads the document first
        PATCH /v1/documents/{documentId}  # the complete attachment list: the file
        GET /v1/documents/{documentId}  # negative check: a list that leaves the file out is refused locally after this read
        GET /v1/documents/{documentId}  # update_document body only reads the document first
        PATCH /v1/documents/{documentId}  # the file must still be attached
        PATCH /v1/documents/{documentId}  # clear_fields attachments (no read first)
        GET /v1/documents/{documentId}/pdf  # export_document_pdf: a file
        GET /v1/documents/{documentId}/versions  # list_document_versions
        GET /v1/documents/{documentId}/versions/{versionId}  # get_document_version of the oldest version
        POST /v1/documents/{documentId}/versions/{versionId}/restore  # restore_document_version
        GET /v1/documents/{documentId}/versions  # and the tool's confirmation read
        GET /v1/documents/{documentId}  # the title is the original one again
        POST /v1/documents/{documentId}/related-items  # add_document_related_items: the copy
        GET /v1/documents/{documentId}/related-items  # the full list add_document_related_items reads after its POST
        GET /v1/documents/{documentId}/related-items  # list_document_related_items
        DELETE /v1/documents/{documentId}/related-items/{relatedItemId}  # remove_document_related_item
        GET /v1/documents/{documentId}/related-items  # the link is gone
        PATCH /v1/documents/{documentId}  # move_document under the copy
        GET /v1/documents/{documentId}  # the copy lists it as a child
        PATCH /v1/documents/{documentId}  # move_document back to the top level of the test client
        GET /v1/documents/{documentId}  # the copy has no child any more
        PATCH /v1/documents/{documentId}  # unshare_document: the public link and the portal off
        PATCH /v1/documents/{documentId}  # archive_document of the copy
        GET /v1/documents/{documentId}  # restore_document reads the document first: it must be archived
        PATCH /v1/documents/{documentId}  # restore_document
        PATCH /v1/documents/{documentId}  # archive_document of the copy, for good
        PATCH /v1/documents/{documentId}  # archive_document of the first document
        GET /v1/documents  # list_documents: the active list shows neither
        GET /v1/documents  # list_documents with status_ids 2: both
    approved_invoice:
        GET /v1/contacts  # list_contacts for the test client (status_ids 1, 3, 4): every contact must be in a state that cannot be emailed (only with --with-approved-invoice, and only once UNEMAILABLE_CONTACT_STATUS_IDS names one)
        GET /v1/clients/{clientId}/locations  # list_client_locations: no location may name a billing contact
        GET /v1/items  # list_items: the first active product of nobody or of the test client
        GET /v1/taxes  # list_taxes: the one tax-exempt rate (Percentage 0, Name or Code "Tax Exempt")
        POST /v1/invoices  # create_approved_invoice: StatusId 5, one line of $1 with that TaxId, no recipient, no dates
        GET /v1/invoices/{invoiceId}  # create_approved_invoice reads the invoice back
        GET /v1/invoices/{invoiceId}  # get_invoice: ExternalId set (the push to accounting); repeated every 5 s until it is
        GET /v1/invoices  # delete_invoice finds the invoice by its Number
        DELETE /v1/invoices/{invoiceId}  # Approved: voided (stays listed); the answer says StatusId 4
        GET /v1/invoices/{invoiceId}  # get_invoice: Status.Id 4
        GET /v1/invoices  # list_invoices: still listed, as Void
    alert_status (--with-alert-status):
        GET /v1/alerts  # list_alerts for the test client (only with --with-alert-status)
        GET /v1/alerts  # list_alerts with status dismissed: every Dismissed alert of the test client, one request per page
        PATCH /v1/alerts  # set_alert_status dismissed
        GET /v1/alerts/{alertId}  # Dismissed, DismissedOn set
        PATCH /v1/alerts  # set_alert_status new
        GET /v1/alerts/{alertId}  # New, DismissedOn null
    asset_types (--with-asset-types):
        POST /v1/assets/types  # create_asset_type (only with --with-asset-types)
        GET /v1/assets/types  # create_asset_type reads the type back from the list
        PATCH /v1/assets/types/{assetTypeId}  # update_asset_type: rename
        GET /v1/assets/types  # and its read-back
        GET /v1/assets/types  # list_asset_types: the renamed type
        DELETE /v1/assets/types/{assetTypeId}  # delete_asset_type
        GET /v1/assets/types  # list_asset_types: gone
    client_locations (--with-client-locations):
        POST /v1/clients/{clientId}/locations  # create_client_location (only with --with-client-locations)
        GET /v1/clients/{clientId}/locations/{locationId}  # its read-back
        PATCH /v1/clients/{clientId}/locations/{locationId}  # update_client_location
        GET /v1/clients/{clientId}/locations/{locationId}  # its read-back
        GET /v1/clients/{clientId}/locations/{locationId}  # get_client_location
        GET /v1/clients/{clientId}/locations  # list_client_locations: it is listed
        DELETE /v1/clients/{clientId}/locations/{locationId}  # delete_client_location: Gorelo marks it inactive
        GET /v1/clients/{clientId}/locations  # list_client_locations: it left the list
        GET /v1/clients/{clientId}/locations/{locationId}  # get_client_location after the delete (only noted)
    client_domains (--with-client-domains):
        POST /v1/clients/{clientId}/domains  # create_client_domain (only with --with-client-domains)
        GET /v1/clients/{clientId}  # the new domain is read from the client's Domains
        PATCH /v1/clients/{clientId}/domains/{domainId}  # update_client_domain: the expiration date
        GET /v1/clients/{clientId}
        DELETE /v1/clients/{clientId}/domains/{domainId}  # delete_client_domain
        GET /v1/clients/{clientId}  # get_client: no longer in Domains
    contact_tags (--with-contact-tags):
        GET /v1/contacts/tags  # list_contact_tags (only with --with-contact-tags)
        POST /v1/contacts  # create_contact
        GET /v1/contacts/{contactId}  # update_contact reads the contact first
        PATCH /v1/contacts/{contactId}  # tag_ids=[tag]
        GET /v1/contacts/{contactId}  # update_contact job_title without tag_ids reads the contact first
        PATCH /v1/contacts/{contactId}  # the tool re-sends the TagIds its read returned
        GET /v1/contacts/{contactId}  # get_contact: the tags are kept
        GET /v1/contacts/{contactId}  # update_contact tag_ids=[] reads the contact first
        PATCH /v1/contacts/{contactId}
        GET /v1/contacts/{contactId}  # get_contact: no tags
    product_charges (--with-product-charges):
        GET /v1/items  # list_items: the first active product of nobody or of the test client (only with --with-product-charges)
        GET /v1/tickets/statuses  # the plain ticket and its lookups (cached: not sent when an earlier area made them)
        GET /v1/tickets/types
        GET /v1/organization/groups
        GET /v1/tickets/sources
        GET /v1/tickets/priorities
        POST /v1/tickets
        GET /v1/tickets/{ticketId}
        POST /v1/product-charges  # create_product_charge: No charge
        GET /v1/product-charges/{productChargeId}  # its read-back
        PATCH /v1/product-charges/{productChargeId}  # update_product_charge
        GET /v1/product-charges/{productChargeId}  # get_product_charge
        GET /v1/product-charges  # list_product_charges for the ticket
        DELETE /v1/product-charges/{productChargeId}  # delete_product_charge
        GET /v1/product-charges  # list_product_charges: gone
    invoice_update (--with-invoice-update):
        GET /v1/items  # list_items (only with --with-invoice-update)
        POST /v1/invoices  # create_invoice: a Draft with two lines
        GET /v1/invoices/{invoiceId}  # create_invoice reads the invoice back
        GET /v1/invoices/{invoiceId}  # update_invoice reads the invoice first
        PATCH /v1/invoices/{invoiceId}  # reference and dates
        GET /v1/invoices/{invoiceId}  # its read-back
        GET /v1/invoices/{invoiceId}  # add_lines reads first
        PATCH /v1/invoices/{invoiceId}  # lines A and B as {Id}, line C new
        GET /v1/invoices/{invoiceId}
        GET /v1/invoices/{invoiceId}  # update_lines reads first
        PATCH /v1/invoices/{invoiceId}  # line A changed, B and C as {Id}
        GET /v1/invoices/{invoiceId}
        GET /v1/invoices/{invoiceId}  # remove_line_ids reads first
        PATCH /v1/invoices/{invoiceId}  # line C left out
        GET /v1/invoices/{invoiceId}
        GET /v1/invoices/{invoiceId}  # get_invoice: still a Draft
        GET /v1/invoices  # delete_invoice finds the invoice by its Number
        DELETE /v1/invoices/{invoiceId}  # Draft: deleted
        GET /v1/taxes  # the tax probe: list_taxes
        POST /v1/items  # create_item: a product taxed at the first rate above 0
        GET /v1/items/{itemId}
        GET /v1/taxes  # create_invoice: the lookup of the Tax Exempt rate that no_tax sends
        POST /v1/invoices  # create_invoice: one line of that item with no_tax=true (TaxId of the Tax Exempt rate)
        GET /v1/invoices/{invoiceId}
        GET /v1/invoices/{invoiceId}  # a fresh get_invoice: which TaxId and tax amount Gorelo applied
        GET /v1/invoices  # delete_invoice finds the invoice by its Number
        DELETE /v1/invoices/{invoiceId}
        DELETE /v1/items/{itemId}  # delete_item
    contracts (--with-contracts):
        GET /v1/items  # list_items (only with --with-contracts)
        POST /v1/contracts  # create_contract: Hourly, one service line, one line item
        GET /v1/contracts/{contractId}  # its read-back
        GET /v1/contracts/{contractId}  # update_contract reads the contract first
        PATCH /v1/contracts/{contractId}  # name and the quantity of the line item
        GET /v1/contracts/{contractId}
        GET /v1/contracts/{contractId}  # add a second service line
        PATCH /v1/contracts/{contractId}
        GET /v1/contracts/{contractId}
        GET /v1/contracts/{contractId}  # remove it
        PATCH /v1/contracts/{contractId}
        GET /v1/contracts/{contractId}
        PATCH /v1/contracts/{contractId}  # status archived (no read first)
        GET /v1/contracts/{contractId}
    comment_edits (--with-comment-edits):
        GET /v1/projects  # scope probe (only with --with-comment-edits); a 403 with code 080203 ends the area here (skipped)
        GET /v1/clients/{clientId}/locations  # the test client's location (cached when an earlier area read it)
        GET /v1/tickets/statuses  # the lookups (cached when an earlier area made them)
        GET /v1/tickets/types
        GET /v1/organization/groups
        GET /v1/tickets/sources
        GET /v1/tickets/priorities
        GET /v1/projects/types
        POST /v1/projects
        GET /v1/projects/{projectId}
        POST /v1/projects/{projectId}/sections
        POST /v1/projects/{projectId}/tasks
        GET /v1/projects/{projectId}/tasks/{taskId}
        POST /v1/projects/{projectId}/comments  # a Private project comment
        GET /v1/projects/{projectId}/comments/{commentId}
        PATCH /v1/projects/{projectId}/comments/{commentId}  # update_project_comment
        GET /v1/projects/{projectId}/comments/{commentId}
        GET /v1/projects/{projectId}/comments/{commentId}  # get_project_comment
        POST /v1/projects/{projectId}/tasks/{taskId}/comments  # a Private task comment
        GET /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}
        PATCH /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}  # update_task_comment
        GET /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}
        GET /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}  # get_project_comment
        DELETE /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}
        DELETE /v1/projects/{projectId}/comments/{commentId}
        DELETE /v1/projects/{projectId}/tasks/{taskId}
    ticket_source (--with-ticket-source):
        GET /v1/tickets/statuses  # the plain ticket and its lookups (cached when an earlier area made them; only with --with-ticket-source)
        GET /v1/tickets/types
        GET /v1/organization/groups
        GET /v1/tickets/sources
        GET /v1/tickets/priorities
        POST /v1/tickets
        GET /v1/tickets/{ticketId}
        PATCH /v1/tickets/{ticketId}  # source_id = another source
        GET /v1/tickets/{ticketId}
        GET /v1/tickets/{ticketId}  # get_ticket by GUID
        PATCH /v1/tickets/{ticketId}  # back to Api
        GET /v1/tickets/{ticketId}
    uptime:
        GET /v1/clients/{clientId}/locations  # the test client's location (Matrix.location() is cached: not sent when an earlier area in the same run, custom_assets with --with-custom-assets, already read it)
        POST /v1/uptime  # http check, the probe domain URL, seattle, every 60 minutes
        GET /v1/uptime/{uptimeId}
        PATCH /v1/uptime/{uptimeId}  # maintenance window: start now, 60 minutes
        GET /v1/uptime/{uptimeId}
        PATCH /v1/uptime/{uptimeId}  # description
        GET /v1/uptime/{uptimeId}
        DELETE /v1/uptime/{uptimeId}
    projects:
        GET /v1/projects  # scope probe; a 403 with code 080203 ends the area here (skipped)
        GET /v1/forms  # scope probe (no forms write step)
        GET /v1/clients/{clientId}/locations  # the test client's location (cached: not sent when an earlier area of the run, uptime or custom_assets, already read it)
        GET /v1/tickets/statuses  # the ticket lookups (cached: not sent when the tickets area ran first); only the Everyone group is used here
        GET /v1/tickets/types
        GET /v1/organization/groups
        GET /v1/tickets/sources
        GET /v1/tickets/priorities
        GET /v1/projects/types  # list_project_types: the first type, required by create_project
        POST /v1/projects  # with that type and the Everyone group (group_id)
        GET /v1/projects/{projectId}
        POST /v1/projects/{projectId}/sections
        POST /v1/projects/{projectId}/tasks  # in that section
        GET /v1/projects/{projectId}/tasks/{taskId}
        POST /v1/projects/{projectId}/tasks/{taskId}/comments  # private
        GET /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}
        DELETE /v1/projects/{projectId}/tasks/{taskId}/comments/{commentId}
        DELETE /v1/projects/{projectId}/tasks/{taskId}
    cleanup:
        DELETE /v1/projects/{projectId}  # then the rest, newest first; what each area already deleted is skipped
        DELETE /v1/tickets/{ticketId}  # the email ticket
        DELETE /v1/tickets/{ticketId}/comments/{commentId}  # the private comment that was not deleted
        DELETE /v1/tickets/{ticketId}  # the backdated ticket
        DELETE /v1/tickets/{ticketId}  # the plain ticket
        DELETE /v1/contacts/{contactId}
        DELETE /v1/clients/{clientId}  # the temporary client

The lists above are the full path (the approver is accepted and stored as asked, the Project scope is granted, the
time entry is deleted at once, --with-items, --with-invoices, --with-custom-assets, --with-asset-move,
--with-custom-properties, --with-documents and --with-approved-invoice are given, the invoice has a Number and, for the
approved one, Gorelo has pushed it to the accounting system by the first get_invoice; the approved list is also the one of a
gate that is open). What varies: the custom_assets area without --with-asset-move ends after its negative check (the first
eleven requests), and with the move it skips the move, with a note, when the second client is not Active (nothing is sent for
it) or has no location (only the read of its locations is sent); the documents area
without the Documents scope is its first request only (a skip), lists a document again up to 3 times when a search lags behind a
write, and stops at the first failed check (the cleanup then archives what is still Active: one GET and one PATCH for each such
document); the five lookups come with the first area that needs them (--only comments or time starts with them and the plain
ticket; --only projects sends them, and the test client's location, right after its two scope probes, which is why the
projects list above names them with the note that an earlier area of the same run has read them already, and a full run
sends the other twelve requests of that area only); get_ticket by number
or display number is tried up to 3 times; delete_time_entry repeats while Outcome is Reopened (the cleanup tries
again up to 3 times if it never turns Deleted); search_tickets reads every page and adds a second search by run id
when a big client truncates it; a rejected approver ends the email area after its POST (no GET, no approval
comment), and so do stored approvers that are not only the operator contact after the approval's GET; a ticket stored with
an audience the matrix did not ask for (or a read-back that failed) ends its area right after that ticket's GET;
without the Project scope the projects area is its first two requests; the items, invoices and approved_invoice areas
send nothing at all unless --with-items, respectively --with-invoices or --with-approved-invoice, is given (the
custom_assets area likewise, unless --with-custom-assets is given, and the custom_properties area unless
--with-custom-properties is given); the custom_properties area is its first request only (a skip, with the reason, and
nothing written) when the test client has no custom field, when any of its custom fields is set or when none of them is an
unset text field, and a set that Gorelo's answer does not confirm (or after which another custom field reads differently) ends it
right after that PATCH (the cleanup then reads the client and clears the field it recorded: its own GET, then the GET, the
PATCH and the second GET of update_client); with
--with-invoices and no usable catalog item the invoices area is its first request; the two list_invoices checks of that
area and delete_invoice's lookup by its
Number are tried up to 3 times, like the ticket lookups, because a search can lag behind a write; an invoice that
reads back with no Number is not deleted by the area (no delete_invoice, no lookup, no last list: the cleanup
removes it with its raw DELETE), a lookup by Number that still finds nothing after the 3 tries fails the area without
any DELETE (the cleanup then removes the invoice, which it reads as a Draft by its id, with the same raw DELETE), and
one that is no longer a Draft at the second get_invoice is not touched at all (the area fails there and the cleanup
leaves it for the user). The approved_invoice area is skipped before its first request until UNEMAILABLE_CONTACT_STATUS_IDS
names the contact statuses that cannot be emailed (empty since 1.3.0, so the gate is closed); then it ends after its first request
when a contact of the test client is in none of them, after its second when a location names a billing contact and after its
third when there is no usable item (each a skip, with the reason); its get_invoice is repeated every 5 s for at most 120 s
(24 waits) until ExternalId is set (a read that fails is one of those polls, not a stop: the next one asks again), and an invoice
that never gets one
is NOT voided (no lookup by Number, no DELETE: the area fails, and the cleanup, which never voids without
--void-approved, leaves it a leftover for the user); delete_invoice's lookup by Number is tried up to 3 times, like the
Draft's, and a voided invoice sends nothing in the cleanup (a known residue). An approved invoice that reads with no
Number is not voided by the area either (delete_invoice finds an invoice by its Number): the area fails with "approved
invoice <DisplayNumber> has no Number, so delete_invoice cannot find it; void it with: python -m scripts.live.cleanup
<manifest> --void-approved (this voids it in Gorelo only: void its copy in the accounting system by hand too)", and the
cleanup, which never voids without that flag, leaves it for the user. A 429 on the create is not retried (one POST, a
failed area, an intent settled as failed), and neither is one on its read-back, which belongs to the same call (the area
fails, and the invoice stays Approved, recorded as such, for the user to void with --void-approved); every other request
of the run keeps the client's own retries, and a delete_invoice whose lookup or DELETE is still answered 429 after those
is asked again by the area (the same up to 3 attempts as the lookup by Number: Gorelo did not process it). The cleanup
after the run has a client of its own and keeps the default retries.

Cleanup then reads the manifest backwards: gated delete tools for what has one (private comments, time entries,
items, uptime checks, project tasks and comments), raw soft deletes for tickets, contacts, the temporary client and
the project, and "removed with its parent" for sections, side conversations and approvals. An invoice is read first;
a Draft is deleted (delete_invoice, or the raw DELETE backstop when it has no Number or when delete_invoice refused
before any DELETE because its lookup by Number found nothing). The cleanup the matrix runs NEVER voids (it is not given
void_approved: only the approved_invoice area voids, the one invoice it approved itself, and the user's own command
voids a leftover): an Approved invoice recorded as 5 is reported as a leftover for the user with its display number and
the command that voids it (python -m scripts.live.cleanup <manifest> --void-approved, which voids it with delete_invoice
or, as for a Draft, the raw DELETE backstop) and the note that this voids it in Gorelo only (VOID_COMMAND_NOTE: its copy
in the accounting system is voided by hand too), a voided one is a known residue (sent nothing), and any other status
(Paid, ...) is never deleted or voided, it is reported as a leftover for the user with its display number. A create
that was announced without an id (create_invoice or create_approved_invoice) is searched by the run label
(list_invoices for the test client). The uploaded attachment has no delete in the API: it is reported as "soft-deleted with
its ticket; the file itself cannot be deleted through the API", counted as known undeletable (not as cleaned, not as a
leftover) and never alone fails the run; a voided invoice is counted the same way (it cannot be removed either).

Output: a line as each area finishes (the run takes a few minutes at one request per second), then the cleanup's own
report, then the summary: one line per area (pass, FAIL with the reason, skipped, not run), the probe answers and
notes, records created, cleaned, left over, known undeletable and announced without an id, a line per known
undeletable record, the scopes that answered 403, and the guard counts; then, for every approved invoice of the run that
was voided (by the area, or found void by the cleanup), a reminder line that the void is in Gorelo only: check the
accounting system and void the invoice there too (Gorelo's void is not pushed to the connected accounting system, seen with Xero). The two sentences
live in scripts/live/cleanup.py (VOID_REMINDER and VOID_COMMAND_NOTE), which says the first for each invoice it voids
itself and the second in its leftover text and in the help of --void-approved; every hint printed here that names that
command (the area's FAIL for an invoice that never synced or has no Number, and the summary's line for an Approved invoice
left over) ends with the second. Exit status 0 only when no area FAILED, nothing is left over (a known undeletable record,
an attachment file or a voided invoice, is not a leftover) and the guard refused nothing; a skipped area (scope missing,
--skip-email, items without --with-items, custom_assets without --with-custom-assets, custom_properties without
--with-custom-properties, while a custom field of the test client is set or with no unset text field to use, invoices without
--with-invoices, documents without --with-documents or with the Documents scope missing, approved_invoice without
--with-approved-invoice, a precondition of the approved invoice that does not hold, which is always the case while
UNEMAILABLE_CONTACT_STATUS_IDS is empty) is not a failure; 1 otherwise; 2 for a usage or setup error (an unknown area,
--with-approved-invoice without --only approved_invoice or beside another flag, --with-asset-move without --with-custom-assets,
no API key, a tool the matrix calls that the server would not offer, a test client that is not Active). A document of the run is a
known undeletable record (archived, it stays in Gorelo) and never alone a failure; the summary ends with a reminder line for the
documents of the run, after the one for the custom assets.
130 when interrupted (the cleanup has run by then).
A custom field value the run set on the test client and the cleanup could not clear is a LEFTOVER (exit status 1): the summary
names the custom field Id and the client and says how to clear it, and its line depends on the kind of leftover: the field may
still hold the run id (run the cleanup again, or clear it in the Gorelo app), another custom field of the client is set so the
cleanup sent no write (clear the field in the Gorelo app), or other custom fields read differently after the clear (their ids,
to be checked in the Gorelo app; no re-run is advised, and the cleanup reports them until they are acknowledged).
Output holds the API host, the manifest path, the run id, the areas and the scopes it found missing, counts, kinds, ids, labels,
the run's own values, the names of the tenant's own lookups when one fails, the script's own words and Gorelo's messages (which
can quote a name or a value), never the API key. The text of an update_client error that a summary prints has its quoted strings cut out
("..."), because that tool quotes the name and the previous value of a custom field in its error for fields that changed
without being sent (_env.without_quotes).

Safety nets besides the guard: the matrix may call only the tools in MATRIX_TOOLS (no post_alert, no
create_approved_invoice; the three item tools only with --with-items; the four custom asset tools only with
--with-custom-assets; the sixteen document tools (DOCUMENT_TOOLS, never share_document) only with --with-documents; list_items,
list_invoices, get_invoice, create_invoice, export_invoice_pdf and delete_invoice only with --with-invoices). The move of the
custom asset needs no tool of its own: the guard of the run gets allow_custom_asset_move only with --with-asset-move, and refuses a
PATCH that names the second client unless the manifest announced it. The custom_properties area needs no tool of
its own (get_client and update_client are in MATRIX_TOOLS); what stops update_client from changing the test client in any
other run is the guard: it refuses every PATCH of the test client unless it is built with allow_custom_properties
(--with-custom-properties) and the body is one recorded custom field (a field whose record is cleaned takes the null only). With --with-approved-invoice the run
may call ONLY APPROVED_INVOICE_TOOLS (create_approved_invoice, approve_invoice, create_invoice, get_invoice, list_invoices,
delete_invoice, list_items, list_taxes, list_contacts, list_client_locations; eight of the ten per run) and its guard is built with allow_approved_invoice; create_approved_invoice is
named nowhere else in this module. That run also switches off the 429 retries of its Gorelo client around the create call
alone (no_429_retries, put back right after it), because the guard allows the create once and a retried POST would count as
a second one; the client is looked up before the first request (gorelo_client_behind), or the run does not start. The
matrix checks before it creates anything that the server offers every tool it may call.

The opt-in areas of 1.5.0 (OPT_INS; contract 6c30770dffc0) follow the same rules, each with its own flag: its tools (OptIn.tools) are callable
only with the flag, the guard gets the matching allow_areas name (guard.OPT_IN_AREAS: alert_status, asset_types, client_locations,
client_domains, product_charges, invoice_update, contracts; contact_tags, comment_edits and ticket_source need none, because the
guard's old rules for a run-created contact, project comment and ticket already cover them), every record is announced and recorded in the
manifest (an alert, which the run does not create, is recorded before its first change), and the cleanup knows its kinds (asset_type,
client_location, client_domain, product_charge, contract, alert: scripts/live/cleanup.py). All of them need the TEST CLIENT Active, like
every write run (the check of run_matrix, before a manifest is made; the operator makes the client Active in the Gorelo app, this
harness never does). Known residue per area: contracts (an archived contract stays in Gorelo; the summary ends with a reminder line);
client_locations and client_domains (Gorelo marks a deleted one inactive: it leaves the lists but is not removed); the invoice numbers
used up by invoice_update (two Drafts) and the approved invoice; nothing for the others. The tax probe of invoice_update and the answer of
contact_tags to an update that names no tag_ids are recorded in the notes of the run summary.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import math
import os
import re
import sys
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastmcp import Client

from gorelo_client import GoreloClient
from scripts.live._env import EnvError, Pacer, live_settings, scrub, without_quotes
from scripts.live.cleanup import (
    ALERT_DISMISSED,
    ALERT_NEW,
    CONTRACT_ARCHIVED,
    CONTRACT_RESIDUE_TAIL,
    CUSTOM_ASSET_CLIENT_UNKNOWN,
    CUSTOM_ASSET_RESIDUE,
    CUSTOM_ASSET_RESIDUE_TAIL,
    CHANGED_FIELDS,
    DOCUMENT_ARCHIVED,
    DOCUMENT_RESIDUE_TAIL,
    contract_residue,
    custom_asset_residue,
    custom_property_leftover_kind,
    document_residue,
    MAX_REOPEN_ATTEMPTS,
    NO_SUCH_NUMBER,
    PACE_SECONDS,
    VOID_COMMAND_NOTE,
    VOID_REMINDER,
    VOID_RESIDUE,
    CleanupReport,
    changed_fields_of,
    run_cleanup,
    shown_ids,
)
from scripts.live.guard import CUSTOM_PROPERTY_UNKNOWN_ID, LiveGuard
from scripts.live.manifest import Manifest
from scripts.live.smoke import (
    GuardTripped,
    ShapeError,
    ToolFailed,
    ToolSession,
    check_file,
    custom_properties_of,
    emit,
    is_hidden,
    is_text_field,
    quiet_logs,
)
from scripts.live import guard as live_guard
from scripts.site_config import SITE, SiteConfigError, site
from server import build_server
from settings import Settings
from tools._common import REGISTRY

LOOKUP_ATTEMPTS = 3  # get_ticket by number or display number may need a moment before the new ticket is searchable
LOOKUP_WAIT = 3.0  # (the invoice lists and delete_invoice's lookup by Number get the same patience)
# NO_SUCH_NUMBER (imported above from the cleanup, which reads the same refusal) is what delete_invoice says, before it
# sends any DELETE, when its lookup by Number finds nothing.
CLIENT_PHONE = "2025550142"  # national number of the temporary client's location: a fictional 555-01xx number in a real area code (Gorelo refuses area code 555)
CONTACT_PHONE = CLIENT_PHONE  # accepted with region US
REGION = "US"
NO_CHARGE = 2  # billable status id
APPROVER_HINT = re.compile(r"contact_ids|approvers?|eligible|\btags?\b", re.IGNORECASE)
APPROVER_STATUSES = (400, 409, 422)  # Gorelo refuses an approver with one of these
# The most an area's detail (or the cleanup error) shows. The hints that print the cleanup command and say what it does to the
# accounting system (VOID_COMMAND_NOTE) reach about 300 characters with the usual manifest path, and a clipped hint would lose
# the end of the command or of that sentence, so the limit leaves room for a longer path.
DETAIL_LIMIT = 400
FAILURE_LIMIT = 160  # characters of a failed read's text that go into a note
# What a tool says when Gorelo answered 429 to every try of a request (the client has retried within its own budget by then).
# Both parts together say that Gorelo did not process the request, so asking again is safe (see Matrix.remove_invoice). The words
# are those of tools/_common.format_gorelo_error; a test builds the real text and checks that rate_limited reads it.
RATE_LIMITED = "Gorelo is rate limiting requests (HTTP 429)"
NOT_PROCESSED = "it did not process this request"
# VOID_REMINDER (imported above from the cleanup, which says it too) is what is said after an approved invoice was voided:
# Gorelo's void does not reach the accounting system, so void the copy there yourself. VOID_COMMAND_NOTE (imported too)
# ends every hint below that sends the user to `cleanup --void-approved`: that command voids in Gorelo only as well.
# Why the items area does not run by default (it is also the area's detail in the summary).
ITEMS_SKIP_REASON = "not live-tested by default: creates and deletes a catalog product on the test client"
# Why the custom_assets area does not run by default (it is also the area's detail in the summary).
CUSTOM_ASSETS_SKIP_REASON = (
    "not live-tested by default: creates a custom asset on the test client; this harness never deletes a custom asset "
    "(the spec's soft DELETE is not used), so it stays there until removed in the Gorelo app"
)
CUSTOM_ASSETS_AREA = "custom_assets"
# TEST-NET-1 (RFC 5737): addresses reserved for documentation, never routable, so no real host is named by the test asset.
CUSTOM_ASSET_IP = "192.0.2.10"
CUSTOM_ASSET_IP_CHANGED = "192.0.2.11"
CUSTOM_ASSET_WARRANTY_DAYS = 365
# The summary's line about a stranded asset: the cleanup never sends a PATCH for a custom asset, so the move back is a manual step.
CUSTOM_ASSET_NEVER_MOVED_BACK = (
    "the cleanup never moves a stranded custom asset back: moving it back to the test client is a manual step in the Gorelo app"
)
# Why the documents area does not run by default (it is also the area's detail in the summary).
DOCUMENTS_SKIP_REASON = (
    "not live-tested by default: creates two documents on the test client, which cannot be deleted through the API (the run archives "
    "them; they stay in Gorelo until removed in the Gorelo app)"
)
DOCUMENTS_AREA = "documents"
DOCUMENT_ACTIVE = 1  # Document StatusId 1 Active (DOCUMENT_ARCHIVED, 2, comes from the cleanup)
# Why the custom_properties area does not run by default (it is also the area's detail in the summary).
CUSTOM_PROPERTIES_SKIP_REASON = (
    "not live-tested by default: sets one custom field value on the test client itself and clears it again, the one write "
    "the live guard otherwise never allows on that client"
)
CUSTOM_PROPERTIES_AREA = "custom_properties"
FIELDS_SHOWN = 8  # custom fields (or, for the set ones, Ids) named in the reason of a skip; the area's note names all of them
# Why the invoices area does not run by default (it is also the area's detail in the summary).
INVOICES_SKIP_REASON = (
    "not live-tested by default: creates and deletes a Draft invoice on the test client, which uses up one invoice number"
)
# Why the approved_invoice area does not run by default (it is also the area's detail in the summary).
APPROVED_INVOICE_SKIP_REASON = (
    "not live-tested by default: approves a $1 invoice on the test client, which pushes it to the connected accounting system, "
    "then voids it"
)
APPROVED_INVOICE_AREA = "approved_invoice"  # the key of the area, which --with-approved-invoice runs alone
# Why the approved_invoice area is skipped, before its first request, while UNEMAILABLE_CONTACT_STATUS_IDS is empty.
APPROVED_INVOICE_GATE_CLOSED = (
    "precondition not met: nothing proves that nobody can be emailed the Approved invoice, so the area did not start and nothing was "
    "sent. No published contact status says that a contact cannot be emailed, so the area stays closed until "
    "UNEMAILABLE_CONTACT_STATUS_IDS names one (scripts/live/write_matrix.py). The operator has to decide which state proves it "
    "before this area runs again"
)
INVOICE_DRAFT = 1  # Invoice StatusId of a Draft: the only status the invoices area creates
INVOICE_PAID = 3  # what Gorelo makes of an Approved invoice whose total is exactly 0 (it neither deletes nor voids it)
INVOICE_VOID = 4  # the StatusId Gorelo answers a DELETE of an Approved invoice with (it stays listed)
INVOICE_APPROVED = 5  # Invoice StatusId of an Approved invoice: only the approved_invoice area creates one
INVOICE_DELETED = 6  # the StatusId Gorelo answers a DELETE of a Draft invoice with
INVOICE_LINE_QUANTITY = 1
INVOICE_LINE_PRICE = 1.0
INVOICE_DUE_DAYS = 14
TAX_EXEMPT_NAME = "tax exempt"  # the Name or Code (compared case-insensitively, whole text) of the one rate the approved invoice may use
CATALOG_PAGE = 50  # catalog items read to find one to bill
# The approved invoice is polled every SYNC_INTERVAL seconds, SYNC_POLLS times after the first read: at most 120 s in all,
# for Gorelo to push it to the connected accounting system (ExternalId is set once it has).
SYNC_INTERVAL = 5.0
SYNC_POLLS = 24
CONTACT_PAGES = 5  # pages of 200 contacts read to check that every contact of the test client is in a state that cannot be emailed
CONTACT_STATUS_IDS = [1, 3, 4]  # every status of the contact scale of contract a7954b24844b: 1 Active, 3 Temporary, 4 Archived
# The Status.Id values of a contact that is PROVEN not to receive an invoice. EMPTY since 1.3.0 on purpose: no published contact
# status says that a contact cannot be emailed, so the area stays closed until this tuple names one.
# While it is empty the approved_invoice area FAILS CLOSED: it is skipped before its first request (APPROVED_INVOICE_GATE_CLOSED).
# Only the operator may decide which state proves it; then this tuple names it and the area needs at least one contact, all in it.
UNEMAILABLE_CONTACT_STATUS_IDS: tuple[int, ...] = ()
# What the matrix records for the invoice it voided itself (a voided invoice cannot be removed: a known residue).
VOIDED_BY_MATRIX = f"voided by the write matrix {VOID_RESIDUE}"
# Who the stored ticket may say it emails: nobody, or the operator contact (the matrix asks for nothing else).

# The only tools the matrix may call. Anything else (post_alert, create_approved_invoice, the smoke's many reads) is a
# RuntimeError before a request is made, on top of what the guard refuses. create_approved_invoice is deliberately not
# here: it pushes the invoice to the connected accounting system. Only the approved_invoice area may call it, with the
# separate tool set below.
MATRIX_TOOLS = frozenset(
    {
        "list_ticket_statuses", "list_ticket_types", "list_org_groups", "list_ticket_sources",
        "list_ticket_priorities", "list_billing_roles", "list_work_types",
        "list_client_locations", "list_projects", "list_forms", "list_project_types", "search_tickets",
        "list_ticket_comments",
        "get_client", "get_contact", "get_ticket", "get_ticket_comment", "get_time_entry",
        "create_client", "update_client", "create_contact", "update_contact", "create_ticket", "update_ticket",
        "create_ticket_comment", "upload_attachment", "create_ticket_side_conversation", "create_ticket_approval",
        "create_time_entry", "update_time_entry", "create_item", "update_item", "create_uptime_check",
        "set_uptime_maintenance", "update_uptime_check", "create_project", "create_project_section",
        "create_project_task", "create_project_comment",
        "list_items", "list_invoices", "get_invoice", "create_invoice", "export_invoice_pdf", "delete_invoice",
        "delete_ticket_comment", "delete_time_entry", "delete_item", "delete_uptime_check",
        "delete_project_comment", "delete_project_task",
    }
)
# The custom asset tools: NOT part of MATRIX_TOOLS; tools_for adds them only with --with-custom-assets; this harness never deletes
# a custom asset (the spec's soft DELETE is not used).
CUSTOM_ASSET_TOOLS = frozenset(
    {"list_custom_asset_hardware_types", "create_custom_asset", "update_custom_asset", "get_custom_asset"}
)
# The document tools: NOT part of MATRIX_TOOLS; tools_for adds them only with --with-documents (a document cannot be deleted through
# the API). share_document is deliberately NOT here: the live harness never turns sharing on (a public link or the client portal makes
# a document readable outside the MSP), and the guard refuses a true flag as well. remove_document_related_item is a gated tool: it
# is registered because live_settings() turns the destructive tools on, and it needs confirm=true.
DOCUMENT_TOOLS = frozenset(
    {
        "list_documents", "get_document", "export_document_pdf", "list_document_versions", "get_document_version",
        "list_document_related_items", "create_document", "update_document", "move_document", "archive_document",
        "restore_document", "duplicate_document", "restore_document_version", "add_document_related_items",
        "unshare_document", "remove_document_related_item",
    }
)
# The catalog tools: callable only with --with-items (it creates, updates and deletes a catalog product on the test client).
ITEM_TOOLS = frozenset({"create_item", "update_item", "delete_item"})
# The invoice tools: callable only with --with-invoices (a Draft invoice uses up an invoice number, and the PDF export
# is recorded on the invoice). list_items is only the lookup of the item to put on the invoice.
INVOICE_TOOLS = frozenset(
    {"list_items", "list_invoices", "get_invoice", "create_invoice", "export_invoice_pdf", "delete_invoice"}
)


# The tools of the approved_invoice run (--with-approved-invoice): the ONLY tools that run may call. This set is the one
# place besides the area itself where create_approved_invoice is named; it replaces MATRIX_TOOLS for that run.
# The tools both ways of the approved invoice run share: the reads, the lookup of the item and of the tax-exempt rate, and the void.
APPROVED_RUN_COMMON = frozenset(
    {"get_invoice", "list_invoices", "delete_invoice", "list_items", "list_taxes", "list_contacts", "list_client_locations"}
)
# A run may make its invoice Approved in ONE of two ways, never both (tools_for): create_approved_invoice (the default), or
# create_invoice and approve_invoice (--approve-via-update, see APPROVE_VIA_UPDATE_TOOLS).
APPROVED_INVOICE_TOOLS = frozenset({"create_approved_invoice", "approve_invoice", "create_invoice", *APPROVED_RUN_COMMON})


@dataclass(frozen=True)
class OptIn:
    """One opt-in area of release 1.5.0 (contract 6c30770dffc0): off by default, run only with its flag (--with-<area>), skipped with
    a reason otherwise (also under --only <area>), and callable only through its own tools."""

    key: str  # the area name (also the --only name); the flag is --with-<key with dashes>
    what: str  # what it writes, for the skip reason and the help text
    tools: frozenset[str]  # the tools only this area may call (on top of MATRIX_TOOLS)
    guard_area: str | None = None  # the name the guard opens for it (guard.OPT_IN_AREAS), None: no guard area is needed

    @property
    def flag(self) -> str:
        return "--with-" + self.key.replace("_", "-")

    @property
    def skip_reason(self) -> str:
        return f"not live-tested by default: {self.what}"

    @property
    def held_tools(self) -> list[str]:
        """The tools of this area that the server holds back (tools._common.HELD_TOOLS): none is registered, so the area cannot run."""
        return sorted(name for name in self.tools if REGISTRY.is_held(name))

    @property
    def held_reason(self) -> str:
        return f"{' and '.join(self.held_tools)} held back until Gorelo releases contract writes: the server does not register them, so nothing was created"


OPT_INS: tuple[OptIn, ...] = (
    OptIn(
        "alert_status",
        "dismisses and then restores to New ONE existing New alert of the test client (Gorelo moves the whole stack of the alert with it)",
        frozenset({"list_alerts", "get_alert", "set_alert_status"}),
        "alert_status",
    ),
    OptIn(
        "asset_types",
        "creates, renames and deletes an MCPTEST asset type (asset types belong to the whole account, not to a client)",
        frozenset({"list_asset_types", "create_asset_type", "update_asset_type", "delete_asset_type"}),
        "asset_types",
    ),
    OptIn(
        "client_locations",
        "creates, updates and deletes one MCPTEST location of the test client (Gorelo marks a deleted location inactive)",
        frozenset({"get_client_location", "create_client_location", "update_client_location", "delete_client_location"}),
        "client_locations",
    ),
    OptIn(
        "client_domains",
        "creates, updates and deletes one MCPTEST domain under the reserved .example TLD on the test client",
        frozenset({"create_client_domain", "update_client_domain", "delete_client_domain"}),
        "client_domains",
    ),
    OptIn(
        "contact_tags",
        "sets, keeps and clears the tags of an MCPTEST contact of the test client",
        frozenset({"list_contact_tags"}),
    ),
    OptIn(
        "product_charges",
        "creates, updates and deletes a No charge product charge on an MCPTEST ticket of the test client",
        frozenset(
            {
                "list_items", "get_product_charge", "list_product_charges", "create_product_charge", "update_product_charge",
                "delete_product_charge",
            }
        ),
        "product_charges",
    ),
    OptIn(
        "invoice_update",
        "changes the lines of a Draft invoice of the test client and runs a tax probe (a temporary catalog item and a second Draft); "
        "each Draft uses up an invoice number",
        frozenset(
            {
                "list_items", "list_taxes", "create_invoice", "get_invoice", "update_invoice", "delete_invoice", "create_item",
                "delete_item",
            }
        ),
        "invoice_update",
    ),
    OptIn(
        "contracts",
        "creates an MCPTEST Hourly contract on the test client, changes it and archives it; a contract CANNOT be deleted through the "
        "API, so it stays in Gorelo, archived, until removed in the Gorelo app",
        frozenset({"list_items", "get_contract", "create_contract", "update_contract"}),
        "contracts",
    ),
    OptIn(
        "comment_edits",
        "creates an MCPTEST project with a Private project comment and a Private task comment on the test client, edits both and deletes them",
        frozenset({"get_project_comment", "update_project_comment", "update_task_comment"}),
    ),
    OptIn(
        "ticket_source",
        "sets the source of an MCPTEST ticket of the test client and reads it back",
        frozenset(),
    ),
)
OPT_IN_BY_KEY: dict[str, OptIn] = {area.key: area for area in OPT_INS}
# The tools of the approved invoice run when --approve-via-update is given: a Draft of $1 made with create_invoice is approved with
# approve_invoice (a PATCH of StatusId 5) instead of being created Approved with create_approved_invoice, which that run never calls.
APPROVE_VIA_UPDATE_TOOLS = frozenset({"create_invoice", "approve_invoice"})


def tools_for(
    with_items: bool,
    with_invoices: bool = False,
    with_approved_invoice: bool = False,
    with_custom_assets: bool = False,
    with_documents: bool = False,
    opt_in: Iterable[str] = (),
    approve_via_update: bool = False,
) -> frozenset[str]:
    """The tools a run may call: with --with-approved-invoice exactly APPROVED_INVOICE_TOOLS, less create_approved_invoice (and, without
    --approve-via-update, create_invoice and approve_invoice too) or, with --approve-via-update, less create_approved_invoice only
    (that run has no other area); otherwise MATRIX_TOOLS, without the three item tools unless --with-items was given, without the
    six invoice tools unless --with-invoices was given, with the four custom asset tools only when --with-custom-assets was given
    (the move needs no tool of its own: update_custom_asset and list_client_locations do it), with the sixteen document tools only when
    --with-documents was given and with the tools of each opt-in area of 1.5.0 (OPT_INS) that was enabled."""
    if with_approved_invoice:
        if approve_via_update:
            return APPROVED_RUN_COMMON | APPROVE_VIA_UPDATE_TOOLS
        return APPROVED_INVOICE_TOOLS - APPROVE_VIA_UPDATE_TOOLS
    tools = MATRIX_TOOLS
    if not with_items:
        tools = tools - ITEM_TOOLS
    if with_custom_assets:
        tools = tools | CUSTOM_ASSET_TOOLS
    if with_documents:
        tools = tools | DOCUMENT_TOOLS
    if not with_invoices:
        tools = tools - INVOICE_TOOLS
    for key in opt_in:
        tools = tools | {name for name in OPT_IN_BY_KEY[key].tools if not REGISTRY.is_held(name)}  # a held tool is not callable
    return tools


class CheckFailed(Exception):
    """A tool answered, but not with what the matrix expected."""


class Skipped(Exception):
    """The area cannot run (a missing scope, no run ticket); the text says why. It is not a failure."""


class UsageError(ValueError):
    """A bad command line (an unknown area, nothing left to run)."""


class SetupError(RuntimeError):
    """The server this run would build is not what the run needs: it does not offer every tool the matrix calls, or (for
    the approved invoice run) its Gorelo client cannot be reached to switch the 429 retries off around the one create.
    Found before anything is created."""


# --------------------------------------------------------------------------
# The 429 retries of the Gorelo client, switched off around the one approved create and nowhere else
# --------------------------------------------------------------------------


def gorelo_client_behind(server: Any) -> GoreloClient:
    """The GoreloClient the tools of `server` use, which the approved invoice run needs to hold before its first request.

    build_server has no option for the client's retries, so the client its lifespan built is reached the way a tool reaches
    it: through the lifespan state under "gorelo" (FastMCP keeps it on the started server). When that is not a GoreloClient,
    SetupError: the run must not go on without being able to switch the retries off, and nothing has been sent yet."""
    state = getattr(server, "_lifespan_result", None)
    client = state.get("gorelo") if isinstance(state, dict) else None
    if not isinstance(client, GoreloClient):
        raise SetupError(
            "cannot switch off the 429 retries of the Gorelo client (the started server has no GoreloClient in its "
            "lifespan state): the approved invoice must never be sent twice, so the run did not start"
        )
    return client


@contextlib.contextmanager
def no_429_retries(client: GoreloClient) -> Iterator[None]:
    """Inside the block `client` retries no 429 (max_429_retries is 0); after it, also when the block raised, the setting it
    had before is back.

    GoreloClient retries a 429 (Gorelo did not process the request) for every method, and every retry passes the guard
    again. The approved run's guard allows ONE create, so a retried POST would be refused as a second Approved invoice: a
    guard violation that stops the run and settles the intent as "nothing was sent", although a request was sent and
    answered. With no retry a 429 on the create comes back as a plain rejected create ("it did not process this request"),
    which settles its intent truthfully, and nothing that approves an invoice is ever sent twice behind the caller's back.
    Only that create needs it: the reads, the lookup by Number and the DELETE that voids may be retried safely, so the
    block is the one create call and nothing else. The matrix drives the client one request at a time, so nothing else
    can send while the block is open."""
    normal = client.max_429_retries
    client.max_429_retries = 0
    try:
        yield
    finally:
        client.max_429_retries = normal


# --------------------------------------------------------------------------
# Small checks
# --------------------------------------------------------------------------


def dig(record: Any, *path: str) -> Any:
    """record[path[0]][path[1]]..., or None when a step is missing or not an object."""
    for key in path:
        if not isinstance(record, dict):
            return None
        record = record.get(key)
    return record


def check(condition: Any, text: str) -> None:
    if not condition:
        raise CheckFailed(text)


def expect(what: str, actual: Any, wanted: Any) -> None:
    if actual != wanted:
        raise CheckFailed(f"{what}: expected {wanted!r}, got {actual!r}")


def same_guid(left: Any, right: Any) -> bool:
    return isinstance(left, str) and isinstance(right, str) and left.lower() == right.lower()


def digits(text: Any) -> str:
    return re.sub(r"\D", "", text) if isinstance(text, str) else ""


def full(record: dict[str, Any], tool: str) -> dict[str, Any]:
    """The record a write tool read back. {Id, warning} (the read-back failed after the write) is a failed check."""
    if "warning" in record:
        raise CheckFailed(f"{tool}: the write succeeded but reading it back failed: {str(record['warning'])[:160]}")
    return record


_MISSING = object()  # a key the record does not have (None is a value Gorelo can answer with)


def is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def kind_of(value: Any) -> str:
    """What a value is, for a message about a value that is not what was expected (never the value itself)."""
    return "missing" if value is _MISSING else type(value).__name__


def first_int_id(rows: Any) -> int | None:
    for row in rows if isinstance(rows, list) else []:
        value = row.get("Id") if isinstance(row, dict) else None
        if is_id(value):
            return value
    return None


_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def is_guid(value: Any) -> bool:
    return isinstance(value, str) and _GUID.fullmatch(value) is not None


def first_guid_id(rows: Any) -> str | None:
    """The Id (GUID text) of the first row that has one."""
    for row in rows if isinstance(rows, list) else []:
        value = row.get("Id") if isinstance(row, dict) else None
        if is_guid(value):
            return value
    return None


def billable_item(rows: Any) -> str | None:
    """The Id of the first catalog row that belongs to nobody (ClientId null) or to the test client, else None.

    A row with another client's id, without a ClientId or without a GUID Id is not used: the invoice is for the test client,
    and an item that belongs to another client must never end up on it."""
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or "ClientId" not in row or not is_guid(row.get("Id")):
            continue
        owner = row["ClientId"]
        if owner is None or (is_id(owner) and owner == SITE.test_client):
            return row["Id"]
    return None


def invoice_ids(listing: Any) -> set[str]:
    """The lower-cased Ids of the rows of a list_invoices answer."""
    rows = listing.get("items") if isinstance(listing, dict) else None
    return {
        row["Id"].lower()
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and is_guid(row.get("Id"))
    }


def invoice_record(record: dict[str, Any], fallback: int = INVOICE_DRAFT) -> dict[str, Any]:
    """The manifest details of a new invoice, from the create tool's answer (the manifest contract: status_id, number,
    display_number). status_id is the status Gorelo STORED when the answer says one, else `fallback`: 1 for a Draft
    (create_invoice, the default) and 5 for the Approved invoice (create_approved_invoice, never 1: an invoice that may
    be approved must not look like a Draft). The guard deletes an invoice recorded as 1, and voids one recorded as 5
    only with allow_approved_invoice."""
    stored = dig(record, "Status", "Id")
    number, display = record.get("Number"), record.get("DisplayNumber")
    return {
        "status_id": stored if is_id(stored) else fallback,
        "number": number if is_id(number) else None,
        "display_number": display if isinstance(display, str) and display else None,
    }


def date_text(value: Any) -> Any:
    """The calendar date of a date or date-time answer (its first 10 characters); anything else is returned as it is."""
    return value[:10] if isinstance(value, str) else value


def check_invoice_line(invoice: dict[str, Any], item_id: str, label: str, tool: str) -> dict[str, Any]:
    """The one line the matrix asked for: that item, quantity 1, unit price 1.0 and the run label as its text."""
    lines = invoice.get("LineItems")
    if not isinstance(lines, list) or len(lines) != 1:
        found = f"{len(lines)} line items" if isinstance(lines, list) else f"no list of line items ({kind_of(lines)})"
        raise CheckFailed(f"{tool}: the invoice has {found}, expected exactly 1")
    line = lines[0]
    check(isinstance(line, dict), f"{tool}: the invoice line is not an object")
    check(same_guid(line.get("ItemId"), item_id), f"{tool}: the invoice line is not for the catalog item that was asked for")
    expect(f"{tool} line Quantity", line.get("Quantity"), INVOICE_LINE_QUANTITY)
    expect(f"{tool} line UnitPrice", line.get("UnitPrice"), INVOICE_LINE_PRICE)
    expect(f"{tool} line Description", line.get("Description"), label)
    return line


def invoice_name(invoice: dict[str, Any]) -> str:
    """How an invoice is named in a message: its DisplayNumber (INV-1042), else its Number, else its Id."""
    for key in ("DisplayNumber", "Number", "Id"):
        value = invoice.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if is_id(value):
            return str(value)
    return "(no number)"


def rate_limited(failure: ToolFailed) -> bool:
    """Did the tool fail because Gorelo answered 429 to every try (it did not process the request)? Then asking again is safe."""
    return RATE_LIMITED in failure.text and NOT_PROCESSED in failure.text


_TRACE_TAIL = re.compile(r"\s*\[trace [^\]]*\]\s*$")


def without_trace(text: str) -> str:
    """`text` without the ' [trace <id>]' that ends the text of every enveloped Gorelo error.

    The id differs from one request to the next, so two failures that say the same thing are told apart by their words and
    never by the id (a text that is cut at FAILURE_LIMIT may have lost it already; the comparison is made on the whole text)."""
    return _TRACE_TAIL.sub("", text)


def is_amount(value: Any) -> bool:
    """A finite number that is not a boolean (an invoice total as Gorelo writes it)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def email_problem(invoice: dict[str, Any]) -> str | None:
    """What the invoice says about having been emailed, or None when it says it was not (IsEmailSent false, EmailSentOn null).

    The matrix names no recipient, and the approved_invoice area is skipped before its first request until UNEMAILABLE_CONTACT_STATUS_IDS
    names the contact statuses that cannot be emailed (every contact of the test client must be in one), so an invoice that reads as
    emailed is news:
    the text is written to be noticed (it goes into the notes of the area). A value that cannot be read is a problem too."""
    sent, on = invoice.get("IsEmailSent", _MISSING), invoice.get("EmailSentOn", _MISSING)
    if sent is False and on is None:
        return None
    if sent is True or (isinstance(on, str) and on.strip()):
        return f"WARNING: invoice EMAILED (IsEmailSent {sent!r}, EmailSentOn {on!r}) although no recipient was named"
    return (
        "WARNING: whether the invoice was emailed cannot be read "
        f"(IsEmailSent is {field_kind(sent)}, EmailSentOn is {field_kind(on)})"
    )


def amount_text(value: Any) -> str:
    """An invoice amount for a message: the number itself, else what the field held (missing, null or its type)."""
    return repr(value) if is_amount(value) else field_kind(value)


def tax_problem(invoice: dict[str, Any]) -> str | None:
    """What the invoice says against the no-tax $1 that was approved, or None when TotalTax is 0 and Total is 1.0.

    The line carried the tax-exempt rate (its TaxId) and billed 1 x 1.0, so a TotalTax other than 0 or a Total
    other than 1.0 means Gorelo did not apply that rate as "no tax" (or changed the amount). The text is written to be
    noticed and holds amounts and kinds only; a TotalTax or Total that is missing or not a number is a problem too."""
    tax, total = invoice.get("TotalTax", _MISSING), invoice.get("Total", _MISSING)
    wanted = INVOICE_LINE_QUANTITY * INVOICE_LINE_PRICE
    if is_amount(tax) and tax == 0 and is_amount(total) and total == wanted:
        return None
    return (
        f"WARNING: no-tax check FAILED: TotalTax is {amount_text(tax)} (expected 0) and Total is {amount_text(total)} "
        f"(expected {wanted!r}), so Gorelo may not have applied the tax-exempt rate as no tax"
    )


def tax_exempt_rates(taxes: Any) -> list[dict[str, Any]]:
    """The rows of list_taxes that are THE tax-exempt rate: Percentage exactly 0 and a Name or Code that is, as a whole and without
    regard to case, "Tax Exempt". A row without a whole-number Id is no rate the run can use. The caller needs exactly one."""
    rows = taxes if isinstance(taxes, list) else []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and is_id(row.get("Id"))
        and is_amount(row.get("Percentage"))
        and row["Percentage"] == 0
        and any(isinstance(row.get(key), str) and row[key].casefold() == TAX_EXEMPT_NAME for key in ("Name", "Code"))
    ]


async def find_tax_exempt_rate(m: Matrix) -> int:
    """list_taxes, and the Id of the one tax-exempt rate; Skipped, with a note and without guessing, when there is none or several."""
    rates = tax_exempt_rates((await m.tool("list_taxes")).get("items"))
    if len(rates) != 1:
        found = "none" if not rates else f"{len(rates)} (Ids {', '.join(str(row['Id']) for row in rates)})"
        raise Skipped(
            "precondition not met: list_taxes must hold exactly one rate with Percentage 0 whose Name or Code is exactly 'Tax Exempt' "
            f"(case-insensitive) to put on the Approved invoice, and it holds {found}; nothing was written (no rate is guessed)"
        )
    return rates[0]["Id"]


def unemailable_contact(row: Any) -> bool:
    """True when a row of list_contacts is in a state that is PROVEN not to receive an invoice: its Status.Id (a whole number, never a
    bool) is one of UNEMAILABLE_CONTACT_STATUS_IDS. The name of the status is never matched. Anything else, a row that is not an
    object and every row while the tuple is empty (it is, since 1.3.0) is not provably unemailable."""
    status = dig(row, "Status", "Id")
    return isinstance(status, int) and not isinstance(status, bool) and status in UNEMAILABLE_CONTACT_STATUS_IDS


def no_billing_contacts(row: Any) -> bool:
    """True when a row of list_client_locations names no billing contact: BillingContactIds is present and null, an empty
    list, text with nothing in it, or the text of an empty JSON list ("[]": production answers exactly that for a
    location that names nobody, although the spec calls the field comma-separated ids). A row without the key, or with anything else
    (ids, a non-empty list in any form, text that is not JSON or cannot be read as JSON), is not provably free of them.
    It never raises: json.loads of text nested too deeply is a RecursionError, which is not a ValueError."""
    if not isinstance(row, dict) or "BillingContactIds" not in row:
        return False
    ids = row["BillingContactIds"]
    if ids is None or ids == []:
        return True
    if not isinstance(ids, str):
        return False
    text = ids.strip()
    if not text:
        return True
    try:
        return json.loads(text) == []
    except (ValueError, RecursionError):
        return False


def row_ids(rows: list[Any], limit: int = 10) -> str:
    """The ids of some rows for a message: at most `limit`, then how many more; a row without an integer Id shows as '?'."""
    shown = [str(row["Id"]) if isinstance(row, dict) and is_id(row.get("Id")) else "?" for row in rows[:limit]]
    return ", ".join(shown) + (f" and {len(rows) - limit} more" if len(rows) > limit else "")


def pdf_size(result: Any) -> int:
    """The size in bytes of the PDF file export_invoice_pdf answered with (a text and an embedded file); CheckFailed when
    there is no file, it is empty or it is not application/pdf."""
    for block in getattr(result, "content", None) or []:
        resource = getattr(block, "resource", None)
        if getattr(block, "type", None) != "resource" or resource is None:
            continue
        mime = getattr(resource, "mimeType", None)
        check(mime == "application/pdf", f"export_invoice_pdf attached a file of type {mime!r}, expected application/pdf")
        try:
            data = base64.b64decode(getattr(resource, "blob", None) or "", validate=True)
        except (ValueError, TypeError):
            raise CheckFailed("export_invoice_pdf attached a file whose content is not valid base64") from None
        check(data, "export_invoice_pdf attached an empty file")
        return len(data)
    raise CheckFailed("export_invoice_pdf answered without an embedded file")


def approver_problem(approval: dict[str, Any]) -> str | None:
    """Why the approval comment must not be posted, or None when every stored approver is the operator contact.

    The approval read back lists its approvers (Approvers: [{ContactId, Status}]); the comment emails them all, so
    there must be at least one and each must be the operator contact. An approver list that cannot be read is a reason too."""
    approvers = approval.get("Approvers", _MISSING)
    if not isinstance(approvers, list) or not approvers:
        return f"the stored approval has no readable list of approvers (Approvers is {kind_of(approvers)} or empty)"
    found = [row.get("ContactId", _MISSING) if isinstance(row, dict) else _MISSING for row in approvers]
    if not all(is_id(contact) for contact in found):
        return "an approver of the stored approval has no readable ContactId"
    others = sorted({contact for contact in found if contact != SITE.operator_contact})
    if others:
        listed = ", ".join(str(contact) for contact in others)
        return f"the stored approvers include contact(s) {listed}, not only the operator contact {SITE.operator_contact}"
    return None


PUBLISHED_SHAPE, FLAT_SHAPE, BOTH_SHAPES = "published", "flat", "both"  # how a time entry names its user (entry_user_shape)


def field_kind(value: Any) -> str:
    """What a field held, for a message about it: missing, null or the type of the value (never the value itself)."""
    return "null" if value is None else kind_of(value)


def entry_user_shape(entry: dict[str, Any], wanted: int | None = None) -> str:
    """Check whose time entry `entry` (read back) is and say how it names its user: PUBLISHED_SHAPE or FLAT_SHAPE.

    The published TimeEntryModel has a `User` object {Id, Name}, which production answers since 2026-10-03. On
    2026-10-02 it answered a flat `UserId` and no User object instead: that passes as FLAT_SHAPE, so the caller can note
    the drift. Anything else raises CheckFailed with what came back (ids and kinds, never the user's name): a User
    object whose Id is not `wanted` or is missing, a User that is not an object, a flat UserId that is not `wanted`, or
    neither field. When both are there they must name the same user: BOTH_SHAPES, which the caller notes as drift;
    a flat UserId that disagrees with the User object fails."""
    if wanted is None:
        wanted = SITE.operator_user
    user, flat = entry.get("User", _MISSING), entry.get("UserId", _MISSING)
    if user is not _MISSING and user is not None:
        if not isinstance(user, dict):
            raise CheckFailed(f"time entry User: expected an object {{Id, Name}}, got {kind_of(user)}")
        expect("time entry User.Id", user.get("Id"), wanted)
        if flat is not _MISSING and flat is not None:
            expect("time entry UserId (next to the User object)", flat, wanted)
            return BOTH_SHAPES
        return PUBLISHED_SHAPE
    if flat is not _MISSING and flat is not None:
        expect("time entry UserId", flat, wanted)
        return FLAT_SHAPE
    raise CheckFailed(
        f"time entry user: expected the User object with Id {wanted} (or the flat UserId {wanted} of 2026-10-02), "
        f"got neither: User is {field_kind(user)} and UserId is {field_kind(flat)}"
    )


def named_id(rows: Any, name: str, noun: str) -> int:
    """The Id of the one row whose Name equals `name` (ignoring case); CheckFailed listing the names otherwise."""
    found = [
        row["Id"]
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
        and isinstance(row.get("Name"), str)
        and row["Name"].strip().casefold() == name.casefold()
        and isinstance(row.get("Id"), int)
        and not isinstance(row.get("Id"), bool)
    ]
    if len(found) != 1:
        names = ", ".join(sorted(str(row.get("Name")) for row in rows if isinstance(row, dict))) or "none"
        raise CheckFailed(f"cannot resolve the {noun} named {name!r} ({len(found)} matches); the tenant has: {names}")
    return found[0]


@dataclass(frozen=True)
class Lookups:
    """The tenant ids the ticket creates need, resolved by name once per run (they are tenant data, never constants)."""

    statuses: list[dict[str, Any]]
    incident: int
    everyone: int
    api: int
    normal: int
    sources: list[dict[str, Any]] = field(default_factory=list)  # every row of list_ticket_sources (the ticket_source area picks another)

    def status(self, name: str) -> int:
        return named_id(self.statuses, name, "ticket status")


# --------------------------------------------------------------------------
# The run context
# --------------------------------------------------------------------------


class Matrix:
    """What the areas share: the tool session (the only way anything is sent), the manifest, the lookups, the run ticket.
    """

    def __init__(
        self,
        *,
        manifest: Manifest,
        guard: LiveGuard,
        pacer: Pacer,
        session: ToolSession,
        secret: str,
        clock: Callable[[], datetime],
        lookup_wait: float,
        scopes_missing: set[str],
        with_items: bool = False,
        with_invoices: bool = False,
        with_approved_invoice: bool = False,
        sync_interval: float = SYNC_INTERVAL,
        sync_polls: int = SYNC_POLLS,
        gorelo: GoreloClient | None = None,
        with_custom_assets: bool = False,
        with_custom_properties: bool = False,
        with_documents: bool = False,
        with_asset_move: bool = False,
        second_client_active: bool = False,  # fail closed: a move needs the run to say that the second client is Active
        opt_in: Iterable[str] = (),
        approve_via_update: bool = False,
    ) -> None:
        if with_approved_invoice and gorelo is None:  # the one create is sent with 429 retries off: the client must be reachable
            raise SetupError(
                "the approved invoice run needs the Gorelo client behind its server, to switch off the 429 retries of the one "
                "create: the approved invoice must never be sent twice, so the run did not start"
            )
        self.manifest = manifest
        self.guard = guard
        self.pacer = pacer
        self.session = session
        self.secret = secret
        self.clock = clock
        self.lookup_wait = lookup_wait
        self.scopes_missing = scopes_missing
        self.with_items = with_items
        self.with_invoices = with_invoices
        self.with_approved_invoice = with_approved_invoice
        self.with_custom_assets = with_custom_assets
        self.with_custom_properties = with_custom_properties
        self.with_documents = with_documents
        self.with_asset_move = with_asset_move
        self.second_client_active = second_client_active  # the status check of the run: a moved asset's new client must be Active
        self.opt_in = frozenset(opt_in)  # the opt-in areas of 1.5.0 this run enabled (OPT_INS keys)
        self.approve_via_update = approve_via_update  # the approved invoice is approved with approve_invoice, not created Approved
        self.sync_interval = sync_interval
        self.sync_polls = sync_polls
        self.gorelo = gorelo  # only the approved invoice run holds it (see without_429_retries)
        self.notes: list[str] = []
        self._lookups: Lookups | None = None
        self._plain: dict[str, Any] | None = None
        self._plain_error: str | None = None
        self._location: int | None = None
        self._second_location: int | None = None

    # -- plumbing ----------------------------------------------------------

    def note(self, text: str) -> None:
        self.notes.append(scrub(" ".join(text.split()), self.secret))

    async def tool(self, name: str, /, **arguments: Any) -> dict[str, Any]:
        """Call a tool (the name is positional-only: create_client and friends take a `name` argument of their own)."""
        return await self.session.call(name, arguments)

    async def tool_result(self, name: str, /, **arguments: Any) -> Any:
        """Call a tool whose answer is a file and not a structured object (export_invoice_pdf): its raw result."""
        return await self.session.call_result(name, arguments)

    def without_429_retries(self) -> contextlib.AbstractContextManager[None]:
        """The window of the one approved create: `with matrix.without_429_retries():` around that call and nothing else (see
        no_429_retries). The run's own client retries no 429 inside it and has its normal retries again right after it, also
        when the call raised. SetupError when this Matrix was given no client (only the approved invoice run is)."""
        if self.gorelo is None:
            raise SetupError(
                "this run holds no Gorelo client whose 429 retries could be switched off: the approved invoice must never be "
                "sent twice, so it is not created"
            )
        return no_429_retries(self.gorelo)

    async def create(
        self,
        kind: str,
        label: str,
        details: dict[str, Any],
        tool: str,
        arguments: dict[str, Any],
        *,
        ident: Callable[[dict[str, Any]], Any] = lambda record: record["Id"],
        recorded: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """announce, create, record: manifest.intent BEFORE the call, manifest.created right after it.

        `details` go into the intent and, unless `recorded` is given, into the record. `recorded(answer)` builds the
        record's details from the answer itself (an invoice's number is only known after the create), so the record
        is written once, at once, before any check of the answer.

        A create that surely made nothing (the guard refused it, it never left the process, Gorelo answered 4xx)
        settles its intent as failed; anything else (a timeout, a 5xx, an answer without an Id) leaves the intent
        open, so the cleanup report lists it as announced but never recorded."""
        seq = self.manifest.intent(kind, label, details)
        sent = self.pacer.requests
        try:
            record = await self.tool(tool, **arguments)
        except GuardTripped:
            self.manifest.intent_failed(seq, "the guard refused the request; nothing was sent")
            raise
        except ToolFailed as exc:
            if not exc.sent or exc.rejected:
                self.manifest.intent_failed(seq, exc.text[:200] or "refused")
            raise
        except BaseException:
            if self.pacer.requests == sent:  # failed before anything left the process: nothing was created
                self.manifest.intent_failed(seq, "the call failed before a request was sent")
            raise
        try:
            identity = ident(record)
        except (KeyError, TypeError):
            raise CheckFailed(
                f"{tool} answered without an Id: the {kind} may exist in Gorelo, search for {label!r}"
            ) from None
        try:
            self.manifest.created(kind, identity, label, details if recorded is None else recorded(record))
        except ValueError as exc:
            raise CheckFailed(
                f"the new {kind} has an Id the manifest cannot record ({exc}); it exists in Gorelo, search for {label!r}"
            ) from None
        return record

    def mark_cleaned(self, kind: str, identity: Any, how: str) -> None:
        self.manifest.cleaned(kind, identity, how)

    def mark_voided(self, invoice_id: str) -> None:
        """The approved invoice was voided and stays listed as Void: a known residue, not a cleaned record (a voided invoice
        cannot be removed). Its stored status becomes 4 first, so the guard allows no further DELETE of it."""
        self.manifest.update_details("invoice", invoice_id, status_id=INVOICE_VOID)
        self.manifest.cleanup_failed("invoice", invoice_id, VOIDED_BY_MATRIX)

    def stamp(self, ago: timedelta, offset_hours: int = 0) -> str:
        """An ISO 8601 time `ago` before now, written with an explicit offset (Z for UTC)."""
        moment = (self.clock() - ago).astimezone(timezone(timedelta(hours=offset_hours))).replace(microsecond=0)
        return moment.isoformat().replace("+00:00", "Z")

    # -- shared lookups and records ------------------------------------------

    async def lookups(self) -> Lookups:
        if self._lookups is None:
            statuses = (await self.tool("list_ticket_statuses"))["items"]
            types = (await self.tool("list_ticket_types"))["items"]
            groups = (await self.tool("list_org_groups"))["items"]
            sources = (await self.tool("list_ticket_sources"))["items"]
            priorities = (await self.tool("list_ticket_priorities"))["items"]
            self._lookups = Lookups(
                statuses=statuses,
                incident=named_id(types, "Incident", "ticket type"),
                everyone=named_id(groups, "Everyone", "group"),
                api=named_id(sources, "Api", "ticket source"),
                normal=named_id(priorities, "Normal", "ticket priority"),
                sources=sources,
            )
        return self._lookups

    async def new_ticket(
        self,
        text: str,
        *,
        client_id: int | None = None,
        status: str = "New",
        contact_id: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """create_ticket named after the run, then the check of who the STORED ticket would email (stored_audience).

        `contact_id` is None or the operator contact. The ticket is announced and recorded with its client only: its
        contact_id and cc_contact_ids come from the read-back, so until stored_audience has passed the manifest
        leaves the audience unknown and the guard allows no public comment on it. Every run ticket is made here, so
        none of them can be used before the check: it raises CheckFailed and the caller's area ends on the spot."""
        if client_id is None:
            client_id = SITE.test_client
        if contact_id not in (None, SITE.operator_contact):
            raise ValueError(
                f"a run ticket gets no contact or the operator contact {SITE.operator_contact}, not {contact_id!r}"
            )
        lookups = await self.lookups()
        label = self.manifest.label(text)
        arguments: dict[str, Any] = {
            "title": label,
            "description": f"{label}: created by the live write matrix, safe to delete.",
            "client_id": client_id,
            "status_id": lookups.status(status),
            "type_id": lookups.incident,
            "priority_id": lookups.normal,
            "source_id": lookups.api,
            "group_id": lookups.everyone,
            **extra,
        }
        if contact_id is not None:
            arguments["contact_id"] = contact_id
        record = await self.create("ticket", label, {"client_id": client_id}, "create_ticket", arguments)
        self.stored_audience(text, record, contact_id)
        return record

    def stored_audience(self, text: str, record: dict[str, Any], wanted: int | None) -> None:
        """The STORED ticket must email exactly who the matrix asked for (nobody or `wanted`); the manifest must know too.

        `record` is create_ticket's answer, the ticket as Gorelo stored it (a tenant automation or Gorelo itself may
        have set a contact or CCs the matrix never sent). Called before anything else is sent for the ticket: a
        CheckFailed fails the area at once, so no comment, status change or approval follows. The stored ContactId
        and CcContactIds go into the manifest first, whatever they are, so the guard's public-comment rule judges the
        truth; a read-back that failed or values that cannot be read (a missing key, an odd type) leave the manifest
        without them, which the guard treats as an unknown audience, and fail the check as well."""
        full(record, "create_ticket")
        ticket = record["Id"]
        contact, copies = record.get("ContactId", _MISSING), record.get("CcContactIds", _MISSING)
        if (contact is not None and not is_id(contact)) or not isinstance(copies, list) or not all(map(is_id, copies)):
            raise CheckFailed(
                f"create_ticket: ticket {ticket} reads back with a ContactId ({kind_of(contact)}) or CcContactIds "
                f"({kind_of(copies)}) that cannot be read as contact ids, so who it would email is unknown; "
                "nothing more is sent for it"
            )
        self.manifest.update_details("ticket", ticket, contact_id=contact, cc_contact_ids=list(copies))
        asked = "none" if wanted is None else str(wanted)
        problems = []
        if contact != wanted:
            problems.append(f"ContactId is {'none' if contact is None else contact}, the matrix asked for {asked}")
        unexpected = [cc for cc in copies if cc != wanted]
        if unexpected:
            only = "none" if wanted is None else f"only {asked}"
            problems.append(f"CcContactIds holds {unexpected}, the matrix asked for {only}")
        if problems:
            raise CheckFailed(
                f"ticket {ticket} was stored with an audience the matrix did not ask for ({'; '.join(problems)}); "
                "the area stops here, before any comment, status change or approval"
            )
        self.note(f"{text}: stored audience verified (ContactId {contact}, CcContactIds {copies})")

    async def plain_ticket(self) -> dict[str, Any]:
        """The run's plain ticket on the test client (no contact), created on first use; the comments and time areas build on it."""
        if self._plain is not None:
            return self._plain
        if self._plain_error is not None:
            raise Skipped(f"no run ticket: {self._plain_error}")
        try:
            self._plain = await self.new_ticket("plain ticket")
        except (ToolFailed, CheckFailed) as exc:
            self._plain_error = str(exc)[:160]
            raise
        return self._plain

    async def location(self) -> int:
        """the test client's default location (list_client_locations), for the uptime check and the project."""
        if self._location is None:
            rows = (await self.tool("list_client_locations", client_id=SITE.test_client))["items"]
            defaults = [row for row in rows if isinstance(row, dict) and row.get("IsDefault") is True]
            found = first_int_id(defaults) or first_int_id(rows)
            check(found is not None, f"client {SITE.test_client} has no location")
            self._location = found
        return self._location  # type: ignore[return-value]

    async def second_location(self) -> int | None:
        """The second client's default location (list_client_locations), for the move of the custom asset; None when it has none."""
        if self._second_location is None:
            rows = (await self.tool("list_client_locations", client_id=SITE.second_client))["items"]
            defaults = [row for row in rows if isinstance(row, dict) and row.get("IsDefault") is True]
            self._second_location = first_int_id(defaults) or first_int_id(rows)
        return self._second_location

    def mark_archived(self, document_id: str, how: str) -> None:
        """A document of the run was archived and stays in Gorelo: a known residue, not a cleaned record (a document cannot be
        removed). The outcome ends with DOCUMENT_RESIDUE_TAIL, which the cleanup reads, so it sends nothing for the document."""
        self.manifest.cleanup_failed("document", document_id, document_residue(how))

    async def document_listing(self, label: str, wanted: dict[str, bool], *, status_ids: list[int] | None = None) -> None:
        """list_documents(client_ids=[the test client], query=the run id) must list each document of `wanted` (True) or not list it (False).

        A search can lag behind a write (the ticket and invoice searches do), so it is read up to LOOKUP_ATTEMPTS times, LOOKUP_WAIT
        seconds apart, before the check fails. `status_ids` [2] asks for the archived ones (the default list is Active only)."""
        arguments: dict[str, Any] = {"client_ids": [SITE.test_client], "query": self.manifest.run_id, "page_size": 50}
        if status_ids is not None:
            arguments["status_ids"] = status_ids
        for attempt in range(1, LOOKUP_ATTEMPTS + 1):
            listing = await self.tool("list_documents", **arguments)
            rows = listing.get("items") if isinstance(listing, dict) else None
            seen = {row["Id"].lower() for row in (rows if isinstance(rows, list) else []) if isinstance(row, dict) and is_guid(row.get("Id"))}
            if all((ident.lower() in seen) is listed for ident, listed in wanted.items()):
                if attempt > 1:
                    self.note(f"list_documents ({label}) showed the expected documents on attempt {attempt}")
                return
            if attempt < LOOKUP_ATTEMPTS:
                await asyncio.sleep(self.lookup_wait)
        wrong = sum(1 for ident, listed in wanted.items() if (ident.lower() in seen) is not listed)
        raise CheckFailed(f"list_documents ({label}) did not show the expected documents (a mismatch for {wrong} of {len(wanted)})")

    async def comment(
        self,
        ticket: str,
        text: str,
        kind: str = "private",
        *,
        conversation_id: Any = None,
        attachments: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """A comment of the run's ticket, recorded as private or not (a non-private one can only go with its ticket)."""
        label = self.manifest.label(text)
        arguments: dict[str, Any] = {"ticket_id": ticket, "body": f"<p>{label}</p>", "conversation_type": kind}
        if conversation_id is not None:
            arguments["conversation_id"] = conversation_id
        if attachments is not None:
            arguments["attachments"] = attachments
        details = {"ticket_id": ticket, "private": kind == "private"}
        record = await self.create("comment", label, details, "create_ticket_comment", arguments)
        return full(record, "create_ticket_comment")

    async def get_ticket(self, reference: Any, how: str) -> dict[str, Any]:
        """get_ticket; a number or display number is retried a few times (the new ticket may not be searchable yet)."""
        attempts = 1 if how == "guid" else LOOKUP_ATTEMPTS
        for attempt in range(1, attempts + 1):
            try:
                record = await self.tool("get_ticket", ticket_id=reference)
            except ToolFailed as exc:
                if attempt < attempts and "no ticket has the number or display number" in exc.text:
                    await asyncio.sleep(self.lookup_wait)
                    continue
                raise
            if attempt > 1:
                self.note(f"get_ticket by {how} found the new ticket on attempt {attempt}")
            return record
        raise AssertionError("unreachable")

    async def invoice_listing(self, label: str, invoice_id: str, *, listed: bool, voided: bool = False) -> None:
        """list_invoices(client_ids=[the test client], query=label) must list the invoice (listed=True) or no longer list it.

        A search can lag behind a write (the ticket search does), and a failed run burns an invoice number, so it is
        read up to LOOKUP_ATTEMPTS times, LOOKUP_WAIT seconds apart, before the check fails. `voided` only words the
        failure for the approved invoice, which a void leaves listed (as Void)."""
        for attempt in range(1, LOOKUP_ATTEMPTS + 1):
            listing = await self.tool("list_invoices", client_ids=[SITE.test_client], query=label)
            if (invoice_id.lower() in invoice_ids(listing)) is listed:
                if attempt > 1:
                    what = (
                        "listed the voided invoice"
                        if listed and voided
                        else "listed the new invoice"
                        if listed
                        else "no longer listed the invoice"
                    )
                    self.note(f"list_invoices {what} on attempt {attempt}")
                return
            if attempt < LOOKUP_ATTEMPTS:
                await asyncio.sleep(self.lookup_wait)
        raise CheckFailed(
            "list_invoices did not list the voided invoice (a void leaves it listed, as Void)"
            if listed and voided
            else "list_invoices did not list the new invoice"
            if listed
            else "list_invoices still lists the deleted Draft invoice"
        )

    async def delete_draft(self, number: int) -> dict[str, Any]:
        """delete_invoice for a Draft, by its Number (see remove_invoice)."""
        return await self.remove_invoice(number, "Draft")

    async def remove_invoice(self, number: int, expected: str) -> dict[str, Any]:
        """delete_invoice by Number for an invoice of the status `expected` ("Draft": deleted, "Approved": voided).

        It is asked again, up to LOOKUP_ATTEMPTS times in all and LOOKUP_WAIT seconds apart, when the call changed nothing:
        its lookup found no invoice (the list may lag behind the create), or Gorelo answered 429 to its lookup or to its
        DELETE even after the client's own retries (rate_limited: Gorelo did not process the request, so nothing was deleted
        or voided and a second DELETE is safe; the guard has no once-only rule for it). Any other failure ends the call."""
        lagged = limited = 0
        for attempt in range(1, LOOKUP_ATTEMPTS + 1):
            try:
                answer = await self.tool("delete_invoice", invoice_number=number, expected_status=expected, confirm=True)
            except ToolFailed as exc:
                if attempt < LOOKUP_ATTEMPTS and NO_SUCH_NUMBER in exc.text:
                    lagged += 1
                elif attempt < LOOKUP_ATTEMPTS and rate_limited(exc):
                    limited += 1
                else:
                    raise
                await asyncio.sleep(self.lookup_wait)
                continue
            if lagged:
                self.note(f"delete_invoice found the invoice by its Number on attempt {attempt}")
            if limited:
                self.note(
                    f"delete_invoice was rate limited by Gorelo (HTTP 429) {limited} time{'' if limited == 1 else 's'} and "
                    f"went through on attempt {attempt}"
                )
            return answer
        raise AssertionError("unreachable")

    async def probe_scope(self, tool: str, **arguments: Any) -> str | None:
        """Call a read tool; the missing scope's name when Gorelo answers 403 code 080203, None when it answered."""
        try:
            await self.tool(tool, **arguments)
        except ToolFailed as exc:
            if exc.scope is None:
                raise
            self.scopes_missing.add(exc.scope)
            return exc.scope
        return None


# --------------------------------------------------------------------------
# The areas
# --------------------------------------------------------------------------


async def area_clients(m: Matrix) -> None:
    # update_client is exercised on the run's own temporary client only: the configured test client is never read for this, never
    # changed, and the guard refuses a PATCH of it.
    label = m.manifest.label("temporary client")
    made = await m.create(
        "client",
        label,
        {},
        "create_client",
        {
            "name": label,
            "location_name": m.manifest.label("location"),
            "location_phone": CLIENT_PHONE,
            "location_phone_country_code": REGION,
        },
    )
    client = made["Id"]
    expect("create_client Name", made.get("Name"), label)
    rows = (await m.tool("list_client_locations", client_id=client))["items"]
    check(rows, "the temporary client has no location")
    m.note(f"temporary client {client}: location phone region stored as {dig(rows[0], 'PhoneCountryCode')!r}")
    alternate = m.manifest.label("alternate name")
    updated = await m.tool("update_client", client_id=client, alternate_name=alternate)
    expect("update_client AlternateName on the temporary client", updated.get("AlternateName"), alternate)
    back = await m.tool("get_client", client_id=client)
    expect("get_client AlternateName on the temporary client", back.get("AlternateName"), alternate)


async def area_contacts(m: Matrix) -> None:
    run = m.manifest.run_id
    label = m.manifest.label("contact")
    made = await m.create(
        "contact",
        label,
        {"client_id": SITE.test_client},
        "create_contact",
        {
            "client_id": SITE.test_client,
            "first_name": run,
            "last_name": "Contact",
            "primary_email": f"{run.lower()}@example.invalid",
            "mobile_phone": CONTACT_PHONE,
            "mobile_phone_country_code": REGION,
        },
    )
    contact = made["Id"]
    check(
        digits(made.get("MobilePhone")).endswith(CONTACT_PHONE),
        f"create_contact MobilePhone is {made.get('MobilePhone')!r}, expected the number {CONTACT_PHONE}",
    )
    expect("create_contact MobilePhoneCountryCode", made.get("MobilePhoneCountryCode"), REGION)

    title = f"{run} title"
    requests, writes = m.pacer.requests, m.guard.writes_allowed
    updated = await m.tool("update_contact", contact_id=contact, job_title=title)  # no secondary_email: nothing to decide
    used, wrote = m.pacer.requests - requests, m.guard.writes_allowed - writes
    reads = used - wrote  # the guard counts every request that is not a GET as a write
    # What matters is the writes and the reads, not the total: the client sends a read again after a 429, a 502, 503 or 504 or a
    # refused connection (and a write only after a 429), so the number of requests may legitimately be higher than two.
    check(
        wrote == 1,
        f"update_contact without secondary_email let {wrote} write request(s) through to Gorelo, expected exactly one "
        f"({used} request(s) in all)",
    )
    check(
        reads >= 1,
        f"update_contact without secondary_email sent {used} request(s) and none of them was a read of the contact, expected "
        "its read before the write",
    )
    m.note(
        "update_contact without secondary_email went through "
        + (
            "(the read and the write)"
            if used == 2
            else f"(one write and {reads} read request(s): the client sent a read again after a retryable answer)"
        )
        + "; Gorelo keeps the secondary emails that an update leaves out (verified 2026-10-24), which is not read back here: no "
        "read returns them"
    )
    expect("update_contact JobTitle", updated.get("JobTitle"), title)
    back = await m.tool("get_contact", contact_id=contact)
    expect("JobTitle read back", back.get("JobTitle"), title)
    expect("MobilePhone after the update (a PATCH clears a phone it leaves out)", back.get("MobilePhone"), made.get("MobilePhone"))
    expect("MobilePhoneCountryCode after the update", back.get("MobilePhoneCountryCode"), REGION)
    expect("FirstName after the update", back.get("FirstName"), run)
    expect("LastName after the update", back.get("LastName"), "Contact")


async def area_tickets(m: Matrix) -> None:
    run = m.manifest.run_id
    lookups = await m.lookups()
    plain = full(await m.plain_ticket(), "create_ticket")
    ticket = plain["Id"]
    expect("plain ticket Title", plain.get("Title"), m.manifest.label("plain ticket"))
    expect("plain ticket ClientId", plain.get("ClientId"), SITE.test_client)
    expect("plain ticket Status.Id", dig(plain, "Status", "Id"), lookups.status("New"))
    expect("plain ticket Type.Id", dig(plain, "Type", "Id"), lookups.incident)
    expect("plain ticket Priority.Id", dig(plain, "Priority", "Id"), lookups.normal)
    expect("plain ticket Source.Id", dig(plain, "Source", "Id"), lookups.api)
    check(lookups.everyone in (plain.get("GroupIds") or []), "the plain ticket is not in the Everyone group")

    created_on, closed_on = m.stamp(timedelta(days=3), -5), m.stamp(timedelta(days=2), -5)
    old = full(
        await m.new_ticket("backdated ticket", status="Closed", created_on=created_on, closed_on=closed_on),
        "create_ticket",
    )
    check(old.get("ClosedOn"), "the backdated ticket has no ClosedOn")
    try:
        stored = datetime.fromisoformat(str(old.get("CreatedOn")).replace("Z", "+00:00"))
    except ValueError:
        raise CheckFailed(f"the backdated ticket's CreatedOn is not a datetime: {old.get('CreatedOn')!r}") from None
    wanted = datetime.fromisoformat(created_on)
    check(
        abs((stored - wanted).total_seconds()) <= 120,
        f"the backdated ticket's CreatedOn is {old.get('CreatedOn')!r}, expected about {created_on}",
    )

    for how, reference in (("guid", ticket), ("number", plain.get("Number")), ("display number", plain.get("DisplayNumber"))):
        check(reference not in (None, ""), f"the plain ticket has no {how} to look it up with")
        got = await m.get_ticket(reference, how)
        check(same_guid(got.get("Id"), ticket), f"get_ticket by {how} returned another ticket")

    # One technician cannot be lead (or assisting) and watcher at once: Gorelo answers 400 "Technician already exists"
    # (live, 2026-10-02). So the watcher goes on first and is verified, it is cleared, and only then does the same
    # user become the lead, in a later PATCH: the two never share one.
    title = m.manifest.label("plain ticket (updated)")
    updated = full(
        await m.tool("update_ticket", ticket_id=ticket, title=title, watcher_ids=[SITE.operator_user]), "update_ticket"
    )
    expect("update_ticket Title", updated.get("Title"), title)
    expect("update_ticket WatcherIds", updated.get("WatcherIds"), [SITE.operator_user])
    cleared = full(await m.tool("update_ticket", ticket_id=ticket, clear_fields=["watcher_ids"]), "update_ticket")
    expect("WatcherIds after clear_fields=['watcher_ids']", cleared.get("WatcherIds"), [])
    m.note(f"watchers: the operator user {SITE.operator_user} set, then cleared with clear_fields (no tenant tag is touched)")
    led = full(await m.tool("update_ticket", ticket_id=ticket, lead_assignee_id=SITE.operator_user), "update_ticket")
    expect("update_ticket LeadAssigneeId", led.get("LeadAssigneeId"), SITE.operator_user)
    expect("WatcherIds after the lead was set", led.get("WatcherIds"), [])
    m.note(f"lead assignee: the operator user {SITE.operator_user} set in a later update, after the watcher was cleared")

    role = first_int_id((await m.tool("list_billing_roles"))["items"])
    work_type = first_int_id((await m.tool("list_work_types"))["items"])
    if role is None or work_type is None:
        m.note("billing read-fill skipped: the tenant has no billing role or no work type")
    else:
        first = full(
            await m.tool("update_ticket", ticket_id=ticket, billing_role_id=role, billing_work_type_id=work_type),
            "update_ticket",
        )
        expect("BillingOverride.BillingRole.Id", dig(first, "BillingOverride", "BillingRole", "Id"), role)
        expect("BillingOverride.WorkType.Id", dig(first, "BillingOverride", "WorkType", "Id"), work_type)
        second = full(await m.tool("update_ticket", ticket_id=ticket, billable_status_id=NO_CHARGE), "update_ticket")
        expect("BillingOverride.BillableStatus.Id", dig(second, "BillingOverride", "BillableStatus", "Id"), NO_CHARGE)
        expect("BillingRole kept by the read-fill", dig(second, "BillingOverride", "BillingRole", "Id"), role)
        expect("WorkType kept by the read-fill", dig(second, "BillingOverride", "WorkType", "Id"), work_type)

    def ids_of(found: dict[str, Any]) -> set[str]:
        return {
            row["Id"].lower() for row in found["items"] if isinstance(row, dict) and isinstance(row.get("Id"), str)
        }

    wanted_ids = {"plain": ticket, "backdated": old["Id"]}
    found = await m.tool("search_tickets", client_id=SITE.test_client, limit=500)
    seen = ids_of(found)
    missing = [name for name, value in wanted_ids.items() if value.lower() not in seen]
    if missing and found.get("truncated"):
        narrow = await m.tool("search_tickets", client_id=SITE.test_client, query=run, limit=50)
        seen = ids_of(narrow)
        missing = [name for name, value in wanted_ids.items() if value.lower() not in seen]
        m.note("search_tickets was truncated for the whole client: the run id was added as a query")
    check(not missing, f"search_tickets(client_id={SITE.test_client}) did not return the run's {' and '.join(missing)} ticket")
    m.note(f"search_tickets: matched={found.get('matched')} scanned={found.get('scanned')} truncated={found.get('truncated')}")


async def area_comments(m: Matrix) -> None:
    ticket = (await m.plain_ticket())["Id"]
    private = await m.comment(ticket, "private comment")
    comment = private["Id"]
    expect("private comment ConversationType.Id", dig(private, "ConversationType", "Id"), 2)
    listing = await m.tool("list_ticket_comments", ticket_id=ticket, conversation_types=["private"])
    listed = {row["Id"].lower() for row in listing["items"] if isinstance(row, dict) and isinstance(row.get("Id"), str)}
    check(comment.lower() in listed, "list_ticket_comments did not list the new private comment")
    got = await m.tool("get_ticket_comment", ticket_id=ticket, comment_id=comment)
    check(same_guid(got.get("Id"), comment), "get_ticket_comment returned another comment")
    gone = await m.tool("delete_ticket_comment", ticket_id=ticket, comment_id=comment, confirm=True)
    check(same_guid(gone.get("Id"), comment), "delete_ticket_comment answered with another comment's Id")
    m.mark_cleaned("comment", comment, "deleted by the write matrix (delete_ticket_comment)")

    name = f"{m.manifest.run_id}-attachment.txt"
    label = m.manifest.label("attachment")
    upload = await m.create(
        "attachment",
        label,
        {"ticket_id": ticket},
        "upload_attachment",
        {"item_type": "ticket", "item_id": ticket, "filename": name, "content_text": f"{label}: a small text file."},
        ident=lambda record: record["name"],
    )
    attached = await m.comment(
        ticket, "private comment with attachment", attachments=[{"name": upload["name"], "url": upload["url"]}]
    )
    names = [dig(item, "Name") for item in (attached.get("Attachments") or [])]
    check(upload["name"] in names, "the comment does not list the uploaded attachment")


async def area_email(m: Matrix) -> None:
    lookups = await m.lookups()
    target = lookups.status(SITE.updated_status)  # resolved first: a missing status must fail before anything is emailed
    # new_ticket has read the stored ticket back and verified who it emails (ContactId the operator contact, CcContactIds only the operator contact)
    # and written that into the manifest: a ticket stored any other way ended this area right there, before this line.
    record = await m.new_ticket(
        "email ticket", client_id=SITE.second_client, contact_id=SITE.operator_contact, send_created_email=True
    )
    ticket = record["Id"]
    expect("email ticket ClientId", record.get("ClientId"), SITE.second_client)
    expect("email ticket ContactId", record.get("ContactId"), SITE.operator_contact)
    m.note("email ticket created with send_created_email=true (one email, to the operator contact only)")

    public = await m.comment(ticket, "public comment", "public")
    expect("public comment ConversationType.Id", dig(public, "ConversationType", "Id"), 1)
    m.note(f"public comment email status: {dig(public, 'EmailInfo', 'Status', 'Name')!r}")

    moved = full(await m.tool("update_ticket", ticket_id=ticket, status_id=target), "update_ticket")
    expect("update_ticket Status.Id", dig(moved, "Status", "Id"), target)

    label = m.manifest.label("side conversation")
    side = await m.create(
        "side_conversation",
        label,
        {"ticket_id": ticket},
        "create_ticket_side_conversation",
        {"ticket_id": ticket, "name": label, "email": SITE.operator_email},
    )
    await m.comment(ticket, "side conversation comment", "side_conversation", conversation_id=side["Id"])

    label = m.manifest.label("approval")
    try:
        approval = await m.create(
            "approval",
            label,
            {"ticket_id": ticket},
            "create_ticket_approval",
            {"ticket_id": ticket, "name": label, "contact_ids": [SITE.operator_contact]},
        )
    except ToolFailed as exc:
        if not (exc.rejected and (exc.status in APPROVER_STATUSES or APPROVER_HINT.search(exc.detail))):
            raise
        m.note(f"approval: skipped: approver not eligible (Gorelo said: {exc.detail[:160]})")
        return
    full(approval, "create_ticket_approval")
    problem = approver_problem(approval)
    if problem is not None:  # the comment would email these approvers: not unless every one is the operator contact
        m.note(f"approval comment: skipped: {problem}; no approval comment was posted, so nobody was emailed")
        return
    await m.comment(ticket, "approval comment", "approval", conversation_id=approval["Id"])


async def area_time(m: Matrix) -> None:
    ticket = (await m.plain_ticket())["Id"]
    label = m.manifest.label("time entry")
    entry = full(
        await m.create(
            "time_entry",
            label,
            {"ticket_id": ticket},
            "create_time_entry",
            {
                "user_id": SITE.operator_user,
                "ticket_id": ticket,
                "started_on": m.stamp(timedelta(minutes=30)),
                "ended_on": m.stamp(timedelta(minutes=15)),
                "billable_status_id": NO_CHARGE,
                "no_service_line": True,
                "comment": label,
            },
        ),
        "create_time_entry",
    )
    entry_id = entry["Id"]
    expect("time entry BillableStatus.Id", dig(entry, "BillableStatus", "Id"), NO_CHARGE)
    shape = entry_user_shape(entry)
    if shape == FLAT_SHAPE:  # production answered like this on 2026-10-02: pass, but say so
        m.note(
            f"time entry user: live returned the flat UserId {SITE.operator_user} (the 2026-10-02 shape) instead of the "
            "published User object {Id, Name}; drift worth knowing"
        )
    elif shape == BOTH_SHAPES:  # the published object plus a flat id the model does not have: pass, but say so
        m.note(
            f"time entry user: live returned both the published User object and a flat UserId {SITE.operator_user}; "
            "drift worth knowing"
        )
    hours = entry.get("ActualHours")
    check(hours is None or abs(hours - 0.25) < 0.01, f"time entry ActualHours is {hours!r}, expected 0.25")
    m.note(f"time entry service line Id: {dig(entry, 'ServiceLine', 'Id')!r} (no_service_line=true)")

    comment = f"{label} (updated)"
    changed = await m.tool("update_time_entry", time_entry_id=entry_id, comment=comment)
    expect("update_time_entry Comment", changed.get("Comment"), comment)

    calls = 0
    while True:
        calls += 1
        done = await m.tool("delete_time_entry", time_entry_id=entry_id, confirm=True)
        outcome = done.get("Outcome")
        if outcome == "Deleted":
            break
        check(outcome == "Reopened", f"delete_time_entry answered an unexpected Outcome ({outcome!r})")
        check(calls < MAX_REOPEN_ATTEMPTS, f"the time entry was still Reopened after {MAX_REOPEN_ATTEMPTS} delete calls")
    m.mark_cleaned(
        "time_entry",
        entry_id,
        "deleted by the write matrix (delete_time_entry)" + (f" after {calls} calls (Reopened first)" if calls > 1 else ""),
    )
    try:
        await m.tool("get_time_entry", time_entry_id=entry_id)
    except ToolFailed as exc:
        m.note(f"get_time_entry after the delete: HTTP {exc.status}")
    else:
        m.note("get_time_entry after the delete: the entry is still readable (the docs say 404)")


async def area_items(m: Matrix) -> None:
    if not m.with_items:  # nothing is announced or sent: the item tools are not even callable without the flag
        raise Skipped(ITEMS_SKIP_REASON)
    label = m.manifest.label("item")
    item = full(
        await m.create(
            "item",
            label,
            {"client_id": SITE.test_client},
            "create_item",
            {
                "type": "product",
                "name": label,
                "client_id": SITE.test_client,
                "description": f"{label}: created by the live write matrix.",
                "unit_price": 1.0,
            },
        ),
        "create_item",
    )
    item_id = item["Id"]
    expect("create_item Name", item.get("Name"), label)
    expect("create_item ClientId", item.get("ClientId"), SITE.test_client)
    expect("create_item Type.Id", dig(item, "Type", "Id"), 1)
    description = f"{label}: updated"
    changed = full(
        await m.tool("update_item", item_id=item_id, description=description, unit_price=2.0), "update_item"
    )
    expect("update_item Description", changed.get("Description"), description)
    expect("update_item UnitPrice", changed.get("UnitPrice"), 2.0)
    gone = await m.tool("delete_item", item_id=item_id, confirm=True)
    check(same_guid(gone.get("Id"), item_id), "delete_item answered with another item's Id")
    m.mark_cleaned("item", item_id, "deleted by the write matrix (delete_item)")


def read_only_words(sent: int) -> str:
    """What a negative check says about the requests of a call the tool refused locally: the guard let no write through, so they
    were GETs. One is the read; more are that read sent again by the client (a 429, a 502, 503 or 504, a refused connection)."""
    if sent == 1:
        return "nothing was sent but that GET"
    return f"nothing was sent but GETs ({sent} requests: the read and the client's own retries of it)"


async def refuse_move_without_location(m: Matrix, asset_id: str, renamed: str) -> None:
    """The local negative check of a move: update_custom_asset with the SECOND client as client_id and no location_id.

    Since contract a7954b24844b a different ClientId MOVES the asset, and Gorelo needs a LocationId of the new client; the tool reads
    the asset first and refuses locally, before any PATCH. It passes only on that refusal: a ToolFailed with no HTTP status whose text
    says "location_id: required when client_id moves the asset", at least one request, all of them GETs (the read, and the client's own
    retry of it after a 429, a 502, 503 or 504 or a refused connection), and no write let through by the guard.
    Anything else is a CheckFailed saying what happened. A PATCH that carried the second client would be refused by the guard (the move
    option and the announcement are missing), which stops the run. Nothing moves."""
    requests, writes = m.pacer.requests, m.guard.writes_allowed
    try:
        await m.tool("update_custom_asset", custom_asset_id=asset_id, client_id=SITE.second_client, name=renamed)
    except ToolFailed as exc:
        sent, wrote = m.pacer.requests - requests, m.guard.writes_allowed - writes
        if wrote:
            raise CheckFailed(
                f"update_custom_asset with the second client and no location_id let {wrote} write request(s) through to Gorelo; "
                f"look for the asset in the Gorelo app: it may have moved to client {SITE.second_client}"
            ) from None
        if exc.status is None and sent >= 1 and "location_id" in exc.text and "moves the asset" in exc.text:
            m.note(
                "update_custom_asset with another client_id and no location_id: refused locally after its read of the asset, "
                + read_only_words(sent)
            )
            return
        status = f"HTTP {exc.status}" if exc.status is not None else "no HTTP answer"
        raise CheckFailed(
            "update_custom_asset with another client_id and no location_id was not refused locally after one GET and before any PATCH, "
            f"so the negative check did not pass ({sent} request(s), {status}): {exc.text[:160]}"
        ) from None
    raise CheckFailed(
        "update_custom_asset with another client_id and no location_id was ACCEPTED (a PATCH reached Gorelo): the asset may have moved "
        f"to client {SITE.second_client}; look for it in the Gorelo app"
    )


def custom_asset_unanswered_text(client_id: Any) -> str:
    """The residue text of a custom asset while the move's PATCH is unanswered: the asset MAY be on the client the move names."""
    return f"may be on the second client {client_id} (its move is not answered yet), and stays there {CUSTOM_ASSET_RESIDUE_TAIL}"


def record_custom_asset_client(m: Matrix, asset_id: str, client_id: Any, *, answer_pending: bool = False) -> None:
    """Record where a custom asset of the move is: its manifest details (client_id, which the guard and the cleanup read) and the
    residue text of its record, which the cleanup keeps because it ends with CUSTOM_ASSET_RESIDUE_TAIL. While the move's PATCH is
    unanswered (answer_pending) the text says the asset MAY be on the second client; after the answer it names the client."""
    text = custom_asset_unanswered_text(client_id) if answer_pending else custom_asset_residue(client_id)
    m.manifest.cleanup_failed("custom_asset", asset_id, text)  # the text first: a stop between the two saves leaves "MAY be there"
    m.manifest.update_details("custom_asset", asset_id, client_id=client_id)


async def move_custom_asset_out_and_back(m: Matrix, asset_id: str, home_location: int, renamed: str, serial: str) -> None:
    """The opt-in move (--with-asset-move): the asset goes to the second client and comes back. See the module docstring.

    The manifest details of the asset name the second client BEFORE the move is sent (the guard insists on it), and the record's
    residue text says the asset MAY be there until Gorelo answers the move, so a crash then leaves that text true; once the answer
    comes the record names the client. The way back is tried whatever the way out did: from the second client it brings the asset
    home, from the test client the tool sees no move and re-sends the client the asset has. The details go back to the test client
    when Gorelo answers the way back. A way back that is refused or never sent leaves them at the second client (the way out was
    clean); an unknown outcome, a way out that was not clean, or a request the guard refused leaves them at "unknown", so the
    reminder at the end of the run says where to look."""
    if not m.second_client_active:
        m.note(f"asset move skipped: the second client {SITE.second_client} is not Active, and a moved asset needs an active client")
        return
    target = await m.second_location()
    if target is None:
        m.note(f"asset move skipped: the second client {SITE.second_client} has no location to move the asset to")
        return
    record_custom_asset_client(m, asset_id, SITE.second_client, answer_pending=True)  # announced BEFORE the move is sent
    m.note(f"custom asset {asset_id}: moving to client {SITE.second_client} (location {target}) and back; recorded in the manifest first")
    trouble: ToolFailed | CheckFailed | None = None
    try:
        try:
            out = full(
                await m.tool("update_custom_asset", custom_asset_id=asset_id, client_id=SITE.second_client, location_id=target),
                "update_custom_asset (move)",
            )
            record_custom_asset_client(m, asset_id, SITE.second_client)  # answered: the record names the second client
            expect("update_custom_asset (move) Id", str(out.get("Id", "")).lower(), asset_id.lower())
            expect("update_custom_asset (move) ClientId", out.get("ClientId"), SITE.second_client)
            expect("update_custom_asset (move) LocationId", out.get("LocationId"), target)
            expect("update_custom_asset (move) Name (left alone)", out.get("Name"), renamed)
            expect("update_custom_asset (move) SerialNumber (left alone)", out.get("SerialNumber"), serial)
            seen = await m.tool("get_custom_asset", custom_asset_id=asset_id)
            expect("get_custom_asset ClientId after the move", seen.get("ClientId"), SITE.second_client)
        except (ToolFailed, CheckFailed) as exc:
            trouble = exc
        if isinstance(trouble, ToolFailed) and trouble.sent and trouble.rejected:  # Gorelo answered 4xx: the asset never left
            record_custom_asset_client(m, asset_id, SITE.test_client)
            raise trouble
        if trouble is not None:  # moved, or not known to have moved: whatever it is, ask Gorelo to bring it home
            record_custom_asset_client(m, asset_id, CUSTOM_ASSET_CLIENT_UNKNOWN)
        else:  # the way back is sent from the second client: until Gorelo answers it, the asset MAY be there
            record_custom_asset_client(m, asset_id, SITE.second_client, answer_pending=True)
        answered = False  # set once Gorelo answers the way back: the asset is then at the test client, whatever a read after that says
        try:
            home = full(
                await m.tool("update_custom_asset", custom_asset_id=asset_id, client_id=SITE.test_client, location_id=home_location),
                "update_custom_asset (move back)",
            )
            answered = True
            record_custom_asset_client(m, asset_id, SITE.test_client)  # answered: the record names the test client again
            expect("update_custom_asset (move back) ClientId", home.get("ClientId"), SITE.test_client)
            expect("update_custom_asset (move back) LocationId", home.get("LocationId"), home_location)
            seen = await m.tool("get_custom_asset", custom_asset_id=asset_id)
            expect("get_custom_asset ClientId after the way back", seen.get("ClientId"), SITE.test_client)
        except (ToolFailed, CheckFailed) as exc:
            unsure = trouble is not None or isinstance(exc, CheckFailed) or (isinstance(exc, ToolFailed) and exc.sent and not exc.rejected)
            if unsure:
                record_custom_asset_client(m, asset_id, CUSTOM_ASSET_CLIENT_UNKNOWN)
            elif not answered:  # refused or never sent: the way out left the asset at the second client
                record_custom_asset_client(m, asset_id, SITE.second_client)
            raise
    except GuardTripped:  # a request was refused: nothing says where the asset is
        record_custom_asset_client(m, asset_id, CUSTOM_ASSET_CLIENT_UNKNOWN)
        raise
    m.note(f"custom asset {asset_id}: moved to client {SITE.second_client} and back to client {SITE.test_client}, the same Id each time")
    if trouble is not None:
        raise trouble


async def area_custom_assets(m: Matrix) -> None:
    """One custom asset on the test client: created, read, changed, cleared, a move refused locally and, with --with-asset-move, moved
    to the second client and back.

    The asset is not deleted by the harness (see CUSTOM_ASSET_RESIDUE), so the area leaves it behind on purpose: it is recorded in the
    manifest before and after the create, and the cleanup reports it as a known residue. The cleanup never moves a stranded asset
    back: that is a manual step in the Gorelo app."""
    if not m.with_custom_assets:  # nothing is announced or sent: the custom asset tools are not even callable without the flag
        raise Skipped(CUSTOM_ASSETS_SKIP_REASON)
    types = (await m.tool("list_custom_asset_hardware_types")).get("items")
    hardware = first_int_id(types)
    if hardware is None:
        raise Skipped("list_custom_asset_hardware_types answered no hardware type with an Id; nothing was created")
    location = await m.location()
    warranty = (m.clock().astimezone(timezone.utc).date() + timedelta(days=CUSTOM_ASSET_WARRANTY_DAYS)).isoformat()
    label = f"{m.manifest.run_id}-asset"
    serial = f"{m.manifest.run_id}-sn"
    asset = full(
        await m.create(
            "custom_asset",
            label,
            {"client_id": SITE.test_client},
            "create_custom_asset",
            {
                "name": label,
                "client_id": SITE.test_client,
                "location_id": location,
                "hardware_type_id": hardware,
                "serial_number": serial,
                "ip_address": CUSTOM_ASSET_IP,
                "warranty_end_date": warranty,
            },
        ),
        "create_custom_asset",
    )
    asset_id = asset["Id"]
    m.note(f"custom asset {asset_id} on client {SITE.test_client}: {CUSTOM_ASSET_RESIDUE}")
    expect("create_custom_asset Name", asset.get("Name"), label)
    expect("create_custom_asset ClientId", asset.get("ClientId"), SITE.test_client)
    expect("create_custom_asset LocationId", asset.get("LocationId"), location)
    expect("create_custom_asset HardwareTypeId", asset.get("HardwareTypeId"), hardware)
    expect("create_custom_asset SerialNumber", asset.get("SerialNumber"), serial)
    expect("create_custom_asset IpAddress", asset.get("IpAddress"), CUSTOM_ASSET_IP)
    expect("create_custom_asset WarrantyEndDate", date_text(asset.get("WarrantyEndDate")), warranty)

    seen = await m.tool("get_custom_asset", custom_asset_id=asset_id)  # the new tool, read live: the same asset as the create answered
    expect("get_custom_asset Id", str(seen.get("Id", "")).lower(), asset_id.lower())
    expect("get_custom_asset Name", seen.get("Name"), label)
    expect("get_custom_asset ClientId", seen.get("ClientId"), SITE.test_client)

    renamed, description = f"{label} v2", f"{label}: updated"
    changed = full(
        await m.tool(
            "update_custom_asset",
            custom_asset_id=asset_id,
            name=renamed,
            ip_address=CUSTOM_ASSET_IP_CHANGED,
            description=description,
        ),
        "update_custom_asset",
    )
    expect("update_custom_asset Name", changed.get("Name"), renamed)
    expect("update_custom_asset IpAddress", changed.get("IpAddress"), CUSTOM_ASSET_IP_CHANGED)
    expect("update_custom_asset Description", changed.get("Description"), description)
    expect("update_custom_asset SerialNumber (left alone)", changed.get("SerialNumber"), serial)
    expect("update_custom_asset WarrantyEndDate (left alone)", date_text(changed.get("WarrantyEndDate")), warranty)
    expect("update_custom_asset ClientId (left alone)", changed.get("ClientId"), SITE.test_client)

    cleared = full(
        await m.tool(
            "update_custom_asset",
            custom_asset_id=asset_id,
            client_id=SITE.test_client,  # the asset's current client: the tool reads the asset first and sees no move
            clear_fields=["description", "ip_address", "warranty_end_date"],
        ),
        "update_custom_asset",
    )
    for field_name in ("Description", "IpAddress", "WarrantyEndDate"):
        value = cleared.get(field_name)
        check(value is None or value == "", f"update_custom_asset clear_fields: {field_name} still reads back {kind_of(value)}")
    expect("update_custom_asset Name (left alone by clear_fields)", cleared.get("Name"), renamed)
    expect("update_custom_asset SerialNumber (left alone by clear_fields)", cleared.get("SerialNumber"), serial)
    expect("update_custom_asset ClientId (the current client moved nothing)", cleared.get("ClientId"), SITE.test_client)

    await refuse_move_without_location(m, asset_id, renamed)
    if m.with_asset_move:
        await move_custom_asset_out_and_back(m, asset_id, location, renamed, serial)


def client_fields(record: dict[str, Any], tool: str) -> list[dict[str, Any]]:
    """The CustomProperties of the client record that `tool` answered, checked entry by entry with the smoke's shape check
    (custom_properties_of): a record that does not carry them as the contract says is API drift, a failed check."""
    try:
        return custom_properties_of(record)
    except ShapeError as exc:
        raise CheckFailed(f"{tool}: {exc}") from None


def field_of(record: dict[str, Any], field_id: int, tool: str) -> dict[str, Any]:
    """The entry for custom field `field_id` in the CustomProperties of `record`: exactly one, else CheckFailed."""
    found = [entry for entry in client_fields(record, tool) if entry["Id"] == field_id]
    if len(found) != 1:
        raise CheckFailed(f"{tool}: CustomProperties has {len(found)} entries for custom field {field_id}, expected exactly 1")
    return found[0]


def check_field_set(record: dict[str, Any], field_id: int, text: str, tool: str) -> None:
    """The custom field reads as saved with the text the run sent: IsSet true and Value equal to it (a text field shows what was
    sent). A wrong Value is described by its kind only: it may be somebody else's text."""
    entry = field_of(record, field_id, tool)
    expect(f"{tool} custom field {field_id} IsSet", entry["IsSet"], True)
    if entry["Value"] != text:
        raise CheckFailed(f"{tool} custom field {field_id} Value: expected the text the run sent, got {field_kind(entry['Value'])}")


def check_field_cleared(record: dict[str, Any], field_id: int, text: str, tool: str) -> None:
    """The custom field reads as not set: IsSet false, and a Value that is not the run's text any more (it is the field's default,
    or null)."""
    entry = field_of(record, field_id, tool)
    expect(f"{tool} custom field {field_id} IsSet", entry["IsSet"], False)
    if entry["Value"] == text:
        raise CheckFailed(f"{tool} custom field {field_id}: IsSet is false but Value still shows the text the run sent")


def unset_text_field(fields: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The custom field the area writes: among the fields that are not set (IsSet exactly false), whose Type.Name contains
    "text" and that are not hidden (IsHidden true: its Value is always null, so the area could not read back what it wrote and
    check_field_set would fail), the one with the lowest Id whose Value is not null (an unset field shows its default as its
    Value, so a clear can restore it); only when none has a default, the one with the lowest Id. None when there is no such field.
    A hidden field that is SET still counts as a set field for the skip rule of the area (IsSet is reported for hidden fields)."""
    candidates = [entry for entry in fields if entry["IsSet"] is False and is_text_field(entry) and not is_hidden(entry)]
    pool = [entry for entry in candidates if entry.get("Value") is not None] or candidates
    return min(pool, key=lambda entry: entry["Id"]) if pool else None


def pick_reason(chosen: dict[str, Any]) -> str:
    """Which case of unset_text_field's rule applied, for the note (kinds only, never the default itself)."""
    if chosen.get("Value") is not None:
        return "it has a default (its Value is not null), so a clear can restore it: the lowest Id among such fields"
    return (
        "no unset text field has a default, so it is the lowest Id; a required field cannot be cleared back to nothing and "
        "would stay set (a leftover)"
    )


def describe_fields(fields: list[dict[str, Any]], limit: int | None = FIELDS_SHOWN) -> str:
    """Each custom field as "Id (Type.Name, IsSet true|false)", plus ", hidden" for a hidden one, at most `limit` of them (all when None): ids and kinds, never a
    value. The reason of a skip is cut at DETAIL_LIMIT characters, so it names the first FIELDS_SHOWN and counts the rest."""
    shown = [
        f"{entry['Id']} ({entry['Type']['Name']}, IsSet {str(entry['IsSet']).lower()}{', hidden' if is_hidden(entry) else ''})"
        for entry in (fields if limit is None else fields[:limit])
    ]
    more = f" and {len(fields) - limit} more" if limit is not None and len(fields) > limit else ""
    return ", ".join(shown) + more


def describe_ids(ids: list[int], limit: int = FIELDS_SHOWN) -> str:
    """Custom field Ids for a reason: the first `limit` of them and how many more there are."""
    more = f" and {len(ids) - limit} more" if len(ids) > limit else ""
    return ", ".join(str(ident) for ident in ids[:limit]) + more


FieldStates = dict[int, tuple[bool, str | None]]


def field_states(fields: list[dict[str, Any]]) -> FieldStates:
    """{Id: (IsSet, Value)} of a CustomProperties list that client_fields has already checked (every Id once)."""
    return {entry["Id"]: (entry["IsSet"], entry["Value"]) for entry in fields}


def require_fields_as_before(
    first: FieldStates, record: dict[str, Any], chosen: int, state: tuple[bool, str | None], tool: str, client: int
) -> None:
    """Every custom field of `record` reads as at the area's first read, except the chosen one, which reads as `state`.

    A field with another IsSet or Value, one that is no longer listed and one that is new all count. The failure names the
    Ids only, never a name or a value, and tells the operator to check them in the Gorelo app. This is what makes an update
    that Gorelo applied as a replace (or any other write to a field the area did not send) a failure and never a pass."""
    wanted = {**first, chosen: state}
    now = field_states(client_fields(record, tool))
    changed = sorted(ident for ident in wanted.keys() | now.keys() if wanted.get(ident) != now.get(ident))
    if changed:
        raise CheckFailed(
            f"{tool}: custom field(s) {describe_ids(changed)} of client {client} do not read as at the first read of this run "
            "(IsSet, Value or presence changed); check them in the Gorelo app"
        )


async def refuse_unknown_field(m: Matrix, client: int, text: str) -> str:
    """The negative check: update_client with an Id that is not a client custom field must be refused LOCALLY by the tool.

    The tool reads the client first and refuses an Id that client does not list before it sends anything (no PATCH, and no
    second read), so the requests of the call are that GET, sent again by the client after a 429, a 502, 503 or 504 or a refused
    connection when Gorelo asked for it, and the guard lets no write through (guard.writes_allowed does not move). It passes
    only on that refusal: a ToolFailed with no HTTP status that names the Id, at least one request (all of them GETs), no write.
    Anything else is a CheckFailed saying what happened: another error (the read failed, a timeout, an HTTP answer), no request
    at all, a write that was let through, or a success. A PATCH that carries the Id never reaches Gorelo: the guard refuses it (GuardTripped, which stops
    the run), because the Id is not a field the run recorded. Returns the words for the note."""
    unknown = CUSTOM_PROPERTY_UNKNOWN_ID
    requests, writes = m.pacer.requests, m.guard.writes_allowed
    try:
        await m.tool("update_client", client_id=client, custom_properties=[{"id": unknown, "value": text}])
    except ToolFailed as exc:
        sent, wrote = m.pacer.requests - requests, m.guard.writes_allowed - writes
        if wrote:
            raise CheckFailed(
                f"update_client with an Id that is not a client custom field let {wrote} write request(s) through to Gorelo; look at "
                f"client {client} in the Gorelo app for a custom value of {text!r}"
            ) from None
        if exc.status is None and sent >= 1 and str(unknown) in exc.text:
            if sent == 1:
                return "the tool refused it itself after reading the client: its one request was that GET, no PATCH was sent"
            return (
                f"the tool refused it itself after reading the client: its {sent} requests were that GET and the client's own "
                "retries of it, no PATCH was sent"
            )
        status = f"HTTP {exc.status}" if exc.status is not None else "no HTTP answer"
        raise CheckFailed(
            "update_client with an Id that is not a client custom field was not refused locally after one GET and before any "
            f"PATCH, so the negative check did not pass ({sent} request(s), {status}): {without_quotes(exc.text)[:180]}"
        ) from None
    raise CheckFailed(
        f"update_client ACCEPTED custom field {unknown}, which is not a client custom field (a PATCH reached Gorelo): look at "
        f"client {client} in the Gorelo app for a custom value of {text!r}"
    )


async def area_custom_properties(m: Matrix) -> None:
    """One custom field value on the test client: set, read, cleared, read, and a negative check with an Id that is no client field.

    The one area that writes to the test client ITSELF (every other area creates records of its own), so it is opt-in
    (--with-custom-properties), it runs only while no custom field of the test client is set (an update that Gorelo applied as
    a replace would otherwise destroy values), it uses nothing but a text field with no saved value, and
    the field is recorded in the manifest BEFORE the first write: that record is what the guard insists on for every write and
    what the cleanup undoes if the run stops after the set and before the clear. After every answer and every fresh read the
    other custom fields must read exactly as at the first read (require_fields_as_before)."""
    if not m.with_custom_properties:  # nothing is announced or sent, not even a read: the area is not touched without the flag
        raise Skipped(CUSTOM_PROPERTIES_SKIP_REASON)
    client = SITE.test_client
    fields = client_fields(await m.tool("get_client", client_id=client), "get_client")
    if not fields:
        raise Skipped("no client custom fields are defined (get_client of the test client lists none); nothing was written")
    held = sorted(entry["Id"] for entry in fields if entry["IsSet"] is not False)
    if held:
        m.note("custom fields of the test client (Id, Type, IsSet): " + describe_fields(fields, None))  # all of them, never cut
        raise Skipped(
            f"custom field(s) {describe_ids(held)} of the test client are set (IsSet true), so nothing was written: the area runs "
            "only while none is, so that an update applied as a replace would have nothing to destroy"
        )
    chosen = unset_text_field(fields)
    if chosen is None:
        m.note("custom fields of the test client (Id, Type, IsSet): " + describe_fields(fields, None))  # all of them, never cut
        raise Skipped(
            "no custom field of the test client is an unset text field, so nothing was written; its fields (Id, Type, IsSet): "
            + describe_fields(fields)
        )
    field_id, text = chosen["Id"], m.manifest.run_id
    if CUSTOM_PROPERTY_UNKNOWN_ID in {entry["Id"] for entry in fields}:  # the negative check must name an Id no field has
        raise CheckFailed(
            f"custom field {CUSTOM_PROPERTY_UNKNOWN_ID}, the Id the negative check sends, is a client custom field here; "
            "nothing was written"
        )
    first = field_states(fields)  # {Id: (IsSet, Value)} before the run wrote anything: what every later read is compared with
    try:  # BEFORE the first write: the guard refuses any write to a field the manifest does not record as unset
        m.manifest.created(
            "custom_property",
            field_id,
            m.manifest.label(f"custom property {field_id}"),
            {
                "client_id": client,
                "name": chosen["Name"],
                "type": chosen["Type"]["Name"],
                "was_set": False,
                "value": text,
                "value_before": chosen["Value"],
            },
        )
    except ValueError as exc:
        raise CheckFailed(f"custom field {field_id} cannot be recorded in the manifest ({exc}); nothing was written") from None
    m.note(
        f"custom field {field_id} (Type {chosen['Type']['Name']!r}) of client {client}: unset before the run and picked because "
        f"{pick_reason(chosen)}; recorded in the manifest before the first write"
    )

    saved = await m.tool("update_client", client_id=client, custom_properties=[{"id": field_id, "value": text}])
    check_field_set(saved, field_id, text, "update_client")
    require_fields_as_before(first, saved, field_id, (True, text), "update_client", client)
    fresh = await m.tool("get_client", client_id=client)
    check_field_set(fresh, field_id, text, "get_client after the set")
    require_fields_as_before(first, fresh, field_id, (True, text), "get_client after the set", client)

    cleared = await m.tool("update_client", client_id=client, clear_custom_property_ids=[field_id])
    check_field_cleared(cleared, field_id, text, "update_client (clear)")
    require_fields_as_before(first, cleared, field_id, first[field_id], "update_client (clear)", client)
    after = await m.tool("get_client", client_id=client)
    check_field_cleared(after, field_id, text, "get_client after the clear")
    require_fields_as_before(first, after, field_id, first[field_id], "get_client after the clear", client)
    m.mark_cleaned("custom_property", field_id, "cleared by the write matrix (update_client clear_custom_property_ids)")
    m.note(
        f"custom field {field_id}: set to the run id, read, cleared, read; the other {len(first) - 1} custom field(s) read as at the "
        "first read after every step"
    )

    outcome = await refuse_unknown_field(m, client, text)
    last = await m.tool("get_client", client_id=client)
    require_fields_as_before(first, last, field_id, first[field_id], "get_client after the negative check", client)
    m.note(
        f"negative check, update_client with the Id {CUSTOM_PROPERTY_UNKNOWN_ID}, which is not a client custom field: {outcome}; "
        f"the last read shows the same {len(first)} custom fields as the first"
    )


def check_document(
    record: dict[str, Any], tool: str, ident: str, *, title: str | None = None, parent: Any = _MISSING, status: int = DOCUMENT_ACTIVE
) -> None:
    """The document record a tool answered is the document `ident` of the test client with the status (and, when given, the title and
    the parent: None = the top level) that the step asked for. Ids and the run's own title only in the messages."""
    check(same_guid(record.get("Id"), ident), f"{tool}: the answer is for another document")
    if title is not None:
        expect(f"{tool} Title", record.get("Title"), title)
    expect(f"{tool} ClientId", record.get("ClientId"), SITE.test_client)
    expect(f"{tool} Status.Id", dig(record, "Status", "Id"), status)
    if parent is not _MISSING:
        shown = record.get("ParentDocumentId")
        if parent is None:
            expect(f"{tool} ParentDocumentId", shown, None)
        else:
            check(same_guid(shown, parent), f"{tool}: ParentDocumentId is not the parent that was asked for")


def check_not_shared(record: dict[str, Any], tool: str) -> None:
    """The document is not shared: the public link and the client portal are off (a new document, a copy, an unshared one)."""
    for flag in ("PublicLinkEnabled", "PublishToPortalEnabled"):
        expect(f"{tool} {flag}", record.get(flag), False)


def attachment_names(record: dict[str, Any]) -> list[Any]:
    """The Name of every attachment of a document record (the urls carry an access token and are never looked at)."""
    return [dig(item, "Name") for item in (record.get("Attachments") or [])]


def child_ids(record: dict[str, Any]) -> set[str]:
    """The lower-cased Ids of the Children of a document record."""
    return {
        item["Id"].lower() for item in (record.get("Children") or []) if isinstance(item, dict) and is_guid(item.get("Id"))
    }


async def refuse_dropping_the_attachment(m: Matrix, document_id: str, other_name: str) -> None:
    """The negative check of the attachments: update_document with a list that LEAVES OUT the current file must be refused locally.

    Gorelo replaces the whole attachment list, so the tool reads the document first and refuses a list that would drop a current file
    unless remove_attachment_names names it. It passes only on that refusal: a ToolFailed with no HTTP status whose text says the
    attachment "would be removed", at least one request, all of them GETs (the read, and the client's own retry of it after a 429, a
    502, 503 or 504 or a refused connection), and no write let through by the guard. Anything else is a CheckFailed saying what
    happened. The file name is never printed."""
    requests, writes = m.pacer.requests, m.guard.writes_allowed
    try:
        await m.tool(
            "update_document",
            document_id=document_id,
            attachments=[{"name": other_name, "url": "https://files.example.invalid/" + other_name}],
        )
    except ToolFailed as exc:
        sent, wrote = m.pacer.requests - requests, m.guard.writes_allowed - writes
        if wrote:
            raise CheckFailed(
                f"update_document with a list that leaves the current file out let {wrote} write request(s) through to Gorelo; "
                f"look at document {document_id} in the Gorelo app: its attachments may have been replaced"
            ) from None
        if exc.status is None and sent >= 1 and "would be removed" in exc.text:
            m.note(
                "update_document with a list that leaves the current file out: refused locally after its read of the document, "
                + read_only_words(sent)
            )
            return
        status = f"HTTP {exc.status}" if exc.status is not None else "no HTTP answer"
        raise CheckFailed(
            "update_document with a list that leaves the current file out was not refused locally after one GET and before any "
            f"PATCH, so the negative check did not pass ({sent} request(s), {status})"
        ) from None
    raise CheckFailed(
        "update_document ACCEPTED a list that leaves the current file out (a PATCH reached Gorelo): look at document "
        f"{document_id} in the Gorelo app, its attachments may have been replaced"
    )


async def area_documents(m: Matrix) -> None:
    """Two documents of the test client (a document and its copy), run through every document tool but share_document.

    A document cannot be deleted through the API, so both are a known residue from the moment they exist: each is recorded in the
    manifest before and after its create, the area archives both at the end (mark_archived) and the cleanup archives what a stopped
    run left active. Sharing is never turned on: the tool set has no share_document and the guard refuses a true flag. Every answer
    is checked and the notes say what Gorelo did, because these steps are also the probes of the document assumptions nobody has
    tested live (see the module docstring)."""
    if not m.with_documents:  # nothing is announced or sent: the document tools are not even callable without the flag
        raise Skipped(DOCUMENTS_SKIP_REASON)
    missing = await m.probe_scope("list_documents", page_size=1)  # a key without the Documents scope ends the area here, as a skip
    if missing is not None:
        raise Skipped(f"scope missing: {missing}")
    run, client = m.manifest.run_id, SITE.test_client
    label = m.manifest.label("document")
    main = await m.create(
        "document", label, {"client_id": client, "parent_document_id": None}, "create_document",
        {"title": label, "client_id": client, "body": f"<p>{label}</p>"},
    )
    main_id = main["Id"]
    check_document(main, "create_document", main_id, title=label, parent=None)
    check_not_shared(main, "create_document")
    m.note(f"document {main_id} of client {client} created at the top level, not shared; {DOCUMENT_RESIDUE_TAIL}")

    title2, alternate = m.manifest.label("document (updated)"), m.manifest.label("portal name")
    updated = await m.tool(
        "update_document", document_id=main_id, title=title2, body=f"<p>{title2}</p>", alternate_name=alternate, is_starred=True
    )
    check_document(updated, "update_document", main_id, title=title2, parent=None)
    expect("update_document AlternateName", updated.get("AlternateName"), alternate)
    expect("update_document IsStarred", updated.get("IsStarred"), True)
    check(run in str(updated.get("Body")), "update_document: the body in the answer does not hold the text that was sent")
    fresh = await m.tool("get_document", document_id=main_id)
    check_document(fresh, "get_document", main_id, title=title2, parent=None)
    expect("get_document IsStarred", fresh.get("IsStarred"), True)

    copy_label = m.manifest.label("document copy")
    copy = await m.create(
        "document", copy_label, {"client_id": client, "parent_document_id": None, "duplicate_of": main_id}, "duplicate_document",
        {"document_id": main_id, "title": copy_label},
    )
    copy_id = copy["Id"]
    check(not same_guid(copy_id, main_id), "duplicate_document answered the original's Id, not a copy")
    check_document(copy, "duplicate_document", copy_id, title=copy_label, parent=None)
    check_not_shared(copy, "duplicate_document")
    expect("duplicate_document IsStarred (the star starts off)", copy.get("IsStarred"), False)
    m.note("the copy of a starred document starts with the star, the public link and the portal off, as the spec says")
    await m.document_listing("both documents", {main_id: True, copy_id: True})

    file_name = f"{run}-document.txt"
    upload_label = m.manifest.label("document attachment")
    upload = await m.create(
        "attachment", upload_label, {"document_id": main_id}, "upload_attachment",
        {"item_type": "document", "item_id": main_id, "filename": file_name, "content_text": f"{upload_label}: a small text file."},
        ident=lambda record: record["name"],
    )
    attached = await m.tool(
        "update_document", document_id=main_id, attachments=[{"name": upload["name"], "url": upload["url"]}]
    )
    check(upload["name"] in attachment_names(attached), "update_document: the answer does not list the uploaded file")
    await refuse_dropping_the_attachment(m, main_id, f"{run}-other.txt")
    body2 = f"<p>{title2}: second body</p>"
    kept = await m.tool("update_document", document_id=main_id, body=body2)
    check(upload["name"] in attachment_names(kept), "update_document with a new body: the file is no longer attached")
    m.note("a changed body kept the attachment (the document attachments belong to the current version, the spec says)")
    cleared = await m.tool("update_document", document_id=main_id, clear_fields=["attachments"])
    expect("update_document clear_fields=['attachments'] Attachments", attachment_names(cleared), [])

    exported = await m.tool_result("export_document_pdf", document_id=main_id)
    try:
        m.note(f"export_document_pdf: {check_file(exported)}")
    except ShapeError as exc:
        raise CheckFailed(f"export_document_pdf: {exc}") from None

    versions = await m.tool("list_document_versions", document_id=main_id, page_size=50)
    rows = [row for row in (versions.get("items") or []) if isinstance(row, dict)]
    check(len(rows) >= 2, f"list_document_versions lists {len(rows)} version(s) after a create and an update, expected at least 2")
    expect("versions that are current", sum(1 for row in rows if row.get("IsCurrent") is True), 1)
    originals = [row for row in rows if row.get("Title") == label and is_guid(row.get("VersionId"))]
    check(len(originals) == 1, f"list_document_versions: {len(originals)} versions have the title the document was created with, expected 1")
    version_id = originals[0]["VersionId"]
    old = await m.tool("get_document_version", document_id=main_id, version_id=version_id)
    check(same_guid(old.get("VersionId"), version_id), "get_document_version returned another version")
    expect("get_document_version Title", old.get("Title"), label)
    restored = await m.tool("restore_document_version", document_id=main_id, version_id=version_id)
    check_document(restored, "restore_document_version", main_id, title=label, parent=None)
    back = await m.tool("get_document", document_id=main_id)
    expect("get_document Title after the restore of the oldest version", back.get("Title"), label)
    m.note(f"{len(rows)} versions listed; the oldest was made current again and the document reads its title again")

    linked = await m.tool("add_document_related_items", document_id=main_id, related_items=[{"item_type": "document", "item_id": copy_id}])
    check(
        any(dig(row, "ItemType", "Id") == 1 and same_guid(row.get("ItemId"), copy_id) for row in (linked.get("items") or [])),
        "add_document_related_items: the answer does not list the link to the copy",
    )
    related = (await m.tool("list_document_related_items", document_id=main_id)).get("items") or []
    links = [row for row in related if isinstance(row, dict) and same_guid(row.get("ItemId"), copy_id) and is_id(row.get("Id"))]
    check(len(links) == 1, f"list_document_related_items shows {len(links)} links to the copy, expected 1")
    link_id = links[0]["Id"]
    gone = await m.tool("remove_document_related_item", document_id=main_id, related_item_id=link_id, confirm=True)
    expect("remove_document_related_item Id", gone.get("Id"), link_id)
    after = (await m.tool("list_document_related_items", document_id=main_id)).get("items") or []
    check(
        not any(isinstance(row, dict) and same_guid(row.get("ItemId"), copy_id) for row in after),
        "list_document_related_items still shows the link that was removed",
    )

    into = await m.tool("move_document", document_id=main_id, parent_document_id=copy_id)
    check_document(into, "move_document (under the copy)", main_id, parent=copy_id)
    parent = await m.tool("get_document", document_id=copy_id)
    check(main_id.lower() in child_ids(parent), "get_document of the copy does not list the moved document among its children")
    m.note("a document moved under another one took its client (ClientId read back as the test client)")
    top = await m.tool("move_document", document_id=main_id, client_id=client)
    check_document(top, "move_document (back to the top level)", main_id, parent=None)
    parent = await m.tool("get_document", document_id=copy_id)
    check(main_id.lower() not in child_ids(parent), "get_document of the copy still lists the document that was moved out")

    unshared = await m.tool("unshare_document", document_id=main_id, public_link=True, publish_to_portal=True)
    check_document(unshared, "unshare_document", main_id, parent=None)
    check_not_shared(unshared, "unshare_document")

    archived = await m.tool("archive_document", document_id=copy_id)
    check_document(archived, "archive_document (the copy)", copy_id, parent=None, status=DOCUMENT_ARCHIVED)
    back_again = await m.tool("restore_document", document_id=copy_id)
    check_document(back_again, "restore_document (the copy)", copy_id, parent=None)
    m.note("an archived document was restored and reads Active again at the top level")

    for ident, name in ((copy_id, "the copy"), (main_id, "the first document")):
        done = await m.tool("archive_document", document_id=ident)
        check_document(done, f"archive_document ({name})", ident, status=DOCUMENT_ARCHIVED)
        m.mark_archived(ident, "archived by the write matrix (archive_document)")
    await m.document_listing("the active list", {main_id: False, copy_id: False})
    await m.document_listing("status_ids [2]", {main_id: True, copy_id: True}, status_ids=[DOCUMENT_ARCHIVED])


async def area_invoices(m: Matrix) -> None:
    """A Draft invoice for the test client, created, read, listed, exported as a PDF and deleted again.

    Never an Approved one: create_approved_invoice is not a tool the matrix may call (MATRIX_TOOLS), and the guard
    refuses any StatusId but 1, any recipient and any other client."""
    if not m.with_invoices:  # nothing is announced or sent: the invoice tools are not even callable without the flag
        raise Skipped(INVOICES_SKIP_REASON)
    found = await m.tool("list_items", type="product", status="active", page_size=CATALOG_PAGE)
    item_id = billable_item(found.get("items"))
    if item_id is None:
        raise Skipped(
            f"no active product of nobody or of client {SITE.test_client} among the first {CATALOG_PAGE} catalog items "
            "to put on the invoice"
        )
    today = m.clock().astimezone(timezone.utc).date()
    invoice_date, due_date = today.isoformat(), (today + timedelta(days=INVOICE_DUE_DAYS)).isoformat()
    label = m.manifest.label("invoice")
    # The invoice is recorded (status_id, number, display_number) the moment create_invoice answers, before any check,
    # so the cleanup can find it even when a check fails. An answer without an Id leaves the intent open: the cleanup
    # then searches list_invoices (the test client) for the run label.
    made = await m.create(
        "invoice",
        label,
        {"client_id": SITE.test_client, "status_id": INVOICE_DRAFT},
        "create_invoice",
        {
            "client_id": SITE.test_client,
            "line_items": [
                {
                    "item_id": item_id,
                    "quantity": INVOICE_LINE_QUANTITY,
                    "description": label,
                    "unit_price": INVOICE_LINE_PRICE,
                }
            ],
            "invoice_date": invoice_date,
            "due_date": due_date,
            "reference": label,
        },
        recorded=invoice_record,
    )
    invoice_id = made["Id"]
    invoice = full(made, "create_invoice")
    expect("create_invoice Status.Id (Draft)", dig(invoice, "Status", "Id"), INVOICE_DRAFT)
    expect("create_invoice ClientId", invoice.get("ClientId"), SITE.test_client)
    expect("create_invoice Reference", invoice.get("Reference"), label)
    expect("create_invoice InvoiceDate", date_text(invoice.get("InvoiceDate")), invoice_date)
    expect("create_invoice DueDate", date_text(invoice.get("DueDate")), due_date)
    check_invoice_line(invoice, item_id, label, "create_invoice")
    m.note(
        f"invoice number consumed: Number {invoice.get('Number')!r}, DisplayNumber {invoice.get('DisplayNumber')!r} "
        "(a Draft uses up one invoice number)"
    )

    got = await m.tool("get_invoice", invoice_id=invoice_id)
    check(same_guid(got.get("Id"), invoice_id), "get_invoice returned another invoice")
    expect("get_invoice Number", got.get("Number"), invoice.get("Number"))
    check_invoice_line(got, item_id, label, "get_invoice")

    await m.invoice_listing(label, invoice_id, listed=True)

    exported = await m.tool_result("export_invoice_pdf", invoice_id=invoice_id)
    size = pdf_size(exported)
    m.note(
        f"export_invoice_pdf: {size} bytes of application/pdf; the export event is recorded on the test invoice, "
        "which is deleted next"
    )

    # Nothing is deleted unless the invoice is still a Draft. Gorelo's DELETE: Draft: deleted (no longer listed).
    # Approved: voided (status Void, still listed), and this area never voids: only the approved_invoice area does, for
    # the one invoice it approved itself. Whatever else this area finds is left for the user (the cleanup reads it again
    # and does the same: it voids an Approved invoice recorded as 5 only when the user runs it with --void-approved).
    again = await m.tool("get_invoice", invoice_id=invoice_id)
    check(same_guid(again.get("Id"), invoice_id), "get_invoice returned another invoice before the delete")
    status = dig(again, "Status", "Id")
    if status != INVOICE_DRAFT:
        raise CheckFailed(
            f"the test invoice has Status.Id {status!r} before the delete, not {INVOICE_DRAFT} (Draft): nothing was "
            "deleted or voided, it is left for the user"
        )
    number = again.get("Number")
    if not is_id(number):  # delete_invoice finds an invoice by its Number: without one the cleanup's raw DELETE does it
        m.note("the test invoice has no Number, so delete_invoice cannot find it: the cleanup removes it with its raw DELETE")
        return
    gone = await m.delete_draft(number)
    check(same_guid(gone.get("Id"), invoice_id), "delete_invoice answered with another invoice's Id")
    expect("delete_invoice StatusId (Deleted)", gone.get("StatusId"), INVOICE_DELETED)
    await m.invoice_listing(label, invoice_id, listed=False)
    m.mark_cleaned("invoice", invoice_id, "deleted by the write matrix (delete_invoice, Draft)")


async def check_nobody_can_be_emailed(m: Matrix) -> None:
    """Raise Skipped unless nobody could be emailed the Approved invoice: the test client has at least one contact (a list that is
    empty proves nothing: contacts in a status that no StatusIds value lists are not returned) and every contact is in a state of
    UNEMAILABLE_CONTACT_STATUS_IDS, and no location of the test client names a billing contact. Two reads, nothing is written, and a
    failed read fails the area (it cannot be said that nobody could be emailed). The reasons name ids only, never a contact."""
    rows: list[Any] = []
    arguments: dict[str, Any] = {"client_id": SITE.test_client, "page_size": 200, "status_ids": CONTACT_STATUS_IDS}
    for _ in range(CONTACT_PAGES):
        page = await m.tool("list_contacts", **arguments)
        found = page.get("items")
        if not isinstance(found, list):
            raise Skipped(
                f"precondition not met: list_contacts did not answer a list, so it cannot be said that every contact of "
                f"client {SITE.test_client} cannot be emailed; nothing was sent"
            )
        rows.extend(found)
        if page.get("has_more") is not True:
            break
        cursor = page.get("next_cursor")
        if not (isinstance(cursor, str) and cursor):
            raise Skipped(
                f"precondition not met: list_contacts has more contacts of client {SITE.test_client} but no cursor to read "
                "them, so it cannot be said that every one cannot be emailed; nothing was sent"
            )
        arguments["cursor"] = cursor
    else:
        raise Skipped(
            f"precondition not met: client {SITE.test_client} has more than {CONTACT_PAGES * 200} contacts, too many to "
            "check that every one cannot be emailed; nothing was sent"
        )
    if not rows:
        raise Skipped(
            f"precondition not met: list_contacts lists no contact of client {SITE.test_client}, which proves nothing: contacts in a "
            "status that no StatusIds value lists are not returned, so it cannot be said "
            "that nobody could be emailed; nothing was sent"
        )
    awake = [row for row in rows if not unemailable_contact(row)]
    if awake:
        raise Skipped(
            f"precondition not met: {len(awake)} of the {len(rows)} contacts of client {SITE.test_client} are not in a state that "
            f"cannot be emailed (status ids {', '.join(map(str, UNEMAILABLE_CONTACT_STATUS_IDS))}; contacts {row_ids(awake)}), so an "
            "Approved invoice might be emailed to one of them; nothing was sent"
        )
    locations = (await m.tool("list_client_locations", client_id=SITE.test_client)).get("items")
    if not isinstance(locations, list):
        raise Skipped(
            f"precondition not met: list_client_locations did not answer a list, so it cannot be said that no location of "
            f"client {SITE.test_client} names a billing contact; nothing was sent"
        )
    billed = [row for row in locations if not no_billing_contacts(row)]
    if billed:
        raise Skipped(
            f"precondition not met: {len(billed)} of the {len(locations)} locations of client {SITE.test_client} name "
            f"billing contacts or have no readable BillingContactIds (ids {row_ids(billed)}), so an Approved invoice "
            "might be emailed to them; nothing was sent"
        )
    m.note(
        f"nobody could be emailed: all {len(rows)} contacts of client {SITE.test_client} are in a state that cannot be emailed "
        f"(status ids {', '.join(map(str, UNEMAILABLE_CONTACT_STATUS_IDS))}) and none of its {len(locations)} locations names a billing contact"
    )


async def approve_draft_by_update(m: Matrix, label: str, line: dict[str, Any], tax_id: int) -> dict[str, Any]:
    """--approve-via-update: create ONE Draft of $1 (create_invoice, the tax-exempt rate as tax_id, no recipient), check what Gorelo stored, record it for the approval
    and approve it with approve_invoice (a PATCH of StatusId 5). Returns the approved invoice as the tool
    read it back.

    The Draft is created and recorded as any Draft (status_id 1). The guard approves only a Draft whose manifest details hold
    approve_via_update true, a total above 0 and at most APPROVED_INVOICE_LIMIT, and total_tax 0, so those are written from the matrix's own read of
    the Draft (the tax-exempt rate, tax_id and its Percentage 0, is recorded with it) (a total or a tax that is not what was asked for stops the area here, with the Draft left to the cleanup, which deletes it). The
    client's 429 retries are off around the approval alone: the guard allows ONE approval, and a retried PATCH would count as a second. When the
    approval call fails after a request was sent, the invoice is read again and the manifest takes the status Gorelo says, so a Draft that was
    approved after all is never left recorded as a Draft."""
    today = m.clock().astimezone(timezone.utc).date()
    made = await m.create(
        "invoice",
        label,
        {"client_id": SITE.test_client, "status_id": INVOICE_DRAFT, "tax_id": tax_id, "tax_percentage": 0},
        "create_invoice",
        {
            "client_id": SITE.test_client,
            "line_items": [line],
            "invoice_date": today.isoformat(),
            "due_date": (today + timedelta(days=INVOICE_DUE_DAYS)).isoformat(),
            "reference": label,
        },
        recorded=lambda record: {**invoice_record(record), "tax_id": tax_id, "tax_percentage": 0},
    )
    invoice_id = made["Id"]
    draft = full(made, "create_invoice")
    expect("create_invoice Status.Id (Draft, to be approved)", dig(draft, "Status", "Id"), INVOICE_DRAFT)
    problem = tax_problem(draft)
    if problem is not None:
        raise CheckFailed(f"{problem}; the Draft is not approved and is left to the cleanup, which deletes it")
    m.manifest.update_details(
        "invoice", invoice_id, approve_via_update=True, total=draft.get("Total"), total_tax=draft.get("TotalTax")
    )
    sent = m.pacer.requests
    try:
        with m.without_429_retries():
            approved = await m.tool("approve_invoice", invoice_id=invoice_id, confirm=True)
    except ToolFailed as exc:
        if m.pacer.requests == sent or exc.rejected:  # nothing reached Gorelo, or Gorelo said no: the invoice is still the Draft
            raise
        read = await m.tool("get_invoice", invoice_id=invoice_id)  # unconfirmed: take the status Gorelo says it has
        m.manifest.update_details("invoice", invoice_id, **invoice_record(read, INVOICE_DRAFT))
        raise
    m.manifest.update_details("invoice", invoice_id, **invoice_record(approved, INVOICE_APPROVED))
    return approved


async def area_approved_invoice(m: Matrix) -> None:
    """ONE Approved invoice of $1 for the test client: created, read, waited for until Gorelo has pushed it to the connected
    accounting system, then voided at once and checked. The only area that calls create_approved_invoice, and it runs
    alone (--with-approved-invoice, --only approved_invoice), behind a guard built with allow_approved_invoice.

    Nothing is sent without the flag, and nothing is written unless the preconditions hold (nobody could be emailed, an
    item to bill). Since 1.3.0 the first precondition cannot be proven (UNEMAILABLE_CONTACT_STATUS_IDS is empty), so the area is
    skipped before its first request until the operator decides what proves it. The invoice is recorded the moment it is created.
    It is never voided before ExternalId says it reached the accounting system: an invoice that never syncs fails the area and is
    left, Approved, for the user."""
    if not m.with_approved_invoice:  # nothing is announced or sent: the tools are not even callable without the flag
        raise Skipped(APPROVED_INVOICE_SKIP_REASON)
    if not UNEMAILABLE_CONTACT_STATUS_IDS:  # nothing proves that nobody could be emailed: FAIL CLOSED, before the first request
        raise Skipped(APPROVED_INVOICE_GATE_CLOSED)
    await check_nobody_can_be_emailed(m)
    found = await m.tool("list_items", type="product", status="active", page_size=CATALOG_PAGE)
    item_id = billable_item(found.get("items"))
    if item_id is None:
        raise Skipped(
            f"no active product of nobody or of client {SITE.test_client} among the first {CATALOG_PAGE} catalog items "
            "to put on the invoice"
        )
    tax_id = await find_tax_exempt_rate(m)  # before any write: with none or several the area is skipped (no_tax is never used here)
    label = m.manifest.label("approved invoice")
    line = {"item_id": item_id, "quantity": INVOICE_LINE_QUANTITY, "unit_price": INVOICE_LINE_PRICE, "description": label, "tax_id": tax_id}
    if m.approve_via_update:  # a Draft of $1 approved with approve_invoice (a PATCH of StatusId 5); create_approved_invoice is not called
        made = await approve_draft_by_update(m, label, line, tax_id)
        approver = "approve_invoice"
    else:
        # Recorded (status_id, number, display_number) the moment create_approved_invoice answers, before any check; the status
        # is what Gorelo stored, and 5 (never 1) when the answer has none, so a failed read-back cannot make an Approved invoice
        # look like a Draft. An answer without an Id leaves the intent open: the cleanup then searches for the label.
        #
        # The client's 429 retries are off for this call and for nothing else (they are back right after it, also when it raises):
        # the guard allows ONE create, so a retried POST would be refused as a second one. The call includes the tool's read-back
        # GET, which is therefore not retried either: a 429 there is a failed read-back like any other (the invoice exists, is
        # recorded as Approved and is left for the user with the --void-approved command). Every later request, the polls, the
        # lookup by Number and the DELETE that voids, keeps the normal retries.
        with m.without_429_retries():
            made = await m.create(
                "invoice",
                label,
                # tax_id and its Percentage 0 are recorded BEFORE the write: the guard compares the line's TaxId with this id
                {"status_id": INVOICE_APPROVED, "tax_id": tax_id, "tax_percentage": 0},
                "create_approved_invoice",
                {"client_id": SITE.test_client, "line_items": [line], "reference": label, "confirm": True},
                recorded=lambda record: invoice_record(record, INVOICE_APPROVED),
            )
        approver = "create_approved_invoice"
    invoice_id = made["Id"]
    invoice = full(made, approver)
    name = invoice_name(invoice)
    warned: set[str] = set()

    def watch(record: dict[str, Any], when: str) -> None:
        """Note, once per distinct text, that the invoice reads as emailed (or unreadably): it must not have been."""
        problem = email_problem(record)
        if problem is not None and problem not in warned:
            warned.add(problem)
            m.note(f"{problem}; seen {when}; invoice {invoice_name(record)}")

    watch(invoice, "when it was created")  # first, so that a failing check below cannot hide it
    status = dig(invoice, "Status", "Id")
    if status == INVOICE_PAID:
        raise CheckFailed(
            f"{approver} Status.Id: expected {INVOICE_APPROVED} (Approved), got {INVOICE_PAID} (Paid): "
            f"Gorelo created invoice {name} as Paid, and it refuses to delete or void a Paid invoice, so it is left "
            "for the user"
        )
    expect(f"{approver} Status.Id (Approved)", status, INVOICE_APPROVED)
    expect(f"{approver} ClientId", invoice.get("ClientId"), SITE.test_client)
    expect(f"{approver} Reference", invoice.get("Reference"), label)
    check_invoice_line(invoice, item_id, label, approver)
    total = invoice.get("Total")
    check(is_amount(total) and total > 0, f"{approver} Total is {total!r}, expected an amount above 0")
    m.note(
        f"approved invoice number consumed: Number {invoice.get('Number')!r}, DisplayNumber {invoice.get('DisplayNumber')!r} "
        "(an Approved invoice cannot be removed, only voided, and it stays listed as Void)"
    )

    # Wait for the push to the accounting system: ExternalId is the invoice's id there, null "when not yet synced". A read
    # that fails (a 5xx, a timeout, a 429) is a GET: it wrote nothing and says nothing about the invoice, so it only means
    # "not synced yet" and the next poll asks again. The wait ends when ExternalId is set, on a status other than
    # Approved, or when the polls run out. A guard refusal is no ToolFailed and still stops the run at once.
    got = invoice
    external: str | None = None
    failed, last_failure = 0, ""
    said_failed: set[str] = set()
    for attempt in range(m.sync_polls + 1):
        if attempt:
            await asyncio.sleep(m.sync_interval)
        try:
            read = await m.tool("get_invoice", invoice_id=invoice_id)
        except ToolFailed as exc:
            failed, last_failure = failed + 1, exc.text[:FAILURE_LIMIT]
            # Once per distinct text, so an outage does not fill the summary. The trace id that ends an enveloped error is
            # left out when the texts are compared (it differs per request: every failure would look new), and the note keeps
            # the first full text, trace included.
            same = without_trace(exc.text)
            if same not in said_failed:
                said_failed.add(same)
                then = "so it is asked again at the next poll" if attempt < m.sync_polls else "and no poll is left"
                m.note(
                    f"get_invoice failed at read {attempt + 1} while waiting for the accounting system, {then}: "
                    f"{last_failure}"
                )
            continue
        got = read
        check(same_guid(got.get("Id"), invoice_id), "get_invoice returned another invoice")
        watch(got, f"at read {attempt + 1} while waiting for the accounting system")
        now = dig(got, "Status", "Id")
        if now != INVOICE_APPROVED:
            raise CheckFailed(
                f"approved invoice {invoice_name(got)} has Status.Id {now!r} while waiting for the accounting system, not "
                f"{INVOICE_APPROVED} (Approved): nothing was voided, it is left for the user"
            )
        value = got.get("ExternalId")
        if isinstance(value, str) and value.strip():
            external = value
            break
    if external is None:
        waited = f"{m.sync_polls + 1} reads over {m.sync_polls * m.sync_interval:g} s"
        m.note(
            f"approved invoice {invoice_name(got)}: ExternalId was still null after {waited}, so it was not voided"
            if not failed
            else f"approved invoice {invoice_name(got)}: ExternalId was not seen in {waited}, {failed} of them failed "
            f"(the last: {last_failure}), so it was not voided"
        )
        raise CheckFailed(
            f"approved invoice {invoice_name(got)} not yet synced to accounting; check the accounting system, then void "
            f"it with: python -m scripts.live.cleanup {m.manifest.path} --void-approved ({VOID_COMMAND_NOTE})"
        )
    link = got.get("PaymentLink")
    m.note(
        f"approved invoice {invoice_name(got)}: pushed to the accounting system, ExternalId {external}, PaymentLink "
        + ("set" if isinstance(link, str) and link.strip() else "not set")
    )

    # Void it at once. Gorelo's DELETE of an Approved invoice voids it (StatusId 4) and leaves it listed.
    number = got.get("Number")
    if not is_id(number):  # delete_invoice finds an invoice by its Number: only the cleanup's raw DELETE can void this one
        raise CheckFailed(
            f"approved invoice {invoice_name(got)} has no Number, so delete_invoice cannot find it; void it with: "
            f"python -m scripts.live.cleanup {m.manifest.path} --void-approved ({VOID_COMMAND_NOTE})"
        )
    gone = await m.remove_invoice(number, "Approved")
    check(same_guid(gone.get("Id"), invoice_id), "delete_invoice answered with another invoice's Id")
    expect("delete_invoice StatusId (Void)", gone.get("StatusId"), INVOICE_VOID)
    m.note(VOID_REMINDER)  # as soon as Gorelo said Void: a failing read below must not leave it unsaid (the summary repeats it)
    after = await m.tool("get_invoice", invoice_id=invoice_id)
    check(same_guid(after.get("Id"), invoice_id), "get_invoice returned another invoice after the void")
    watch(after, "after the void")
    # The no-tax check (the tax-exempt TaxId, 1 x 1.0) is a note and nothing more: it comes after the void and never raises, so it can
    # neither stop nor skip the void or the bookkeeping below. A loud note is how an invoice that was taxed is noticed.
    problem = tax_problem(after)
    if problem is None:
        m.note("no-tax check passed: TotalTax is 0 and Total is 1.0 on the read after the void")
    else:
        m.note(
            f"{problem}; read after the void, invoice {invoice_name(after)}; at the last read before the void it was "
            f"TotalTax {amount_text(got.get('TotalTax', _MISSING))} and Total {amount_text(got.get('Total', _MISSING))}"
        )
    expect("get_invoice Status.Id after the void", dig(after, "Status", "Id"), INVOICE_VOID)
    m.mark_voided(invoice_id)  # recorded before the listing check: the void is a fact whatever the search says
    await m.invoice_listing(label, invoice_id, listed=True, voided=True)


async def area_uptime(m: Matrix) -> None:
    location = await m.location()
    label = m.manifest.label("uptime check")
    created = full(
        await m.create(
            "uptime",
            label,
            {"client_id": SITE.test_client},
            "create_uptime_check",
            {
                "check_type": "http",
                "frequency_minutes": 60,
                "region": "seattle",
                "url": SITE.probe_url,
                "client_id": SITE.test_client,
                "location_id": location,
                "description": label,
            },
        ),
        "create_uptime_check",
    )
    check_id = created["Id"]
    expect("uptime check Type.Id (http)", dig(created, "Type", "Id"), 2)
    expect("uptime check Target.Url", dig(created, "Target", "Url"), SITE.probe_url)
    expect("uptime check RegionId (seattle)", created.get("RegionId"), 1)
    expect("uptime check Frequency", created.get("Frequency"), 60)
    reason = f"{label}: maintenance during the live test"
    # Gorelo refuses a window without a start ("MaintenanceMode.StartDateTime is required when enabling maintenance
    # mode."), and the tool never reads the clock: the start is now, written with an explicit UTC offset.
    held = full(
        await m.tool(
            "set_uptime_maintenance",
            check_id=check_id,
            enabled=True,
            start=m.stamp(timedelta(0)),
            duration_minutes=60,
            reason=reason,
        ),
        "set_uptime_maintenance",
    )
    expect("MaintenanceMode.Enabled", dig(held, "MaintenanceMode", "Enabled"), True)
    description = f"{label} (updated)"
    changed = full(await m.tool("update_uptime_check", check_id=check_id, description=description), "update_uptime_check")
    expect("update_uptime_check Description", changed.get("Description"), description)
    m.note(
        f"maintenance window after update_uptime_check: Enabled={dig(changed, 'MaintenanceMode', 'Enabled')!r} "
        f"DurationInMinutes={dig(changed, 'MaintenanceMode', 'DurationInMinutes')!r}"
    )
    gone = await m.tool("delete_uptime_check", check_id=check_id, confirm=True)
    check(same_guid(gone.get("Id"), check_id), "delete_uptime_check answered with another check's Id")
    m.mark_cleaned("uptime", check_id, "deleted by the write matrix (delete_uptime_check)")


async def area_projects(m: Matrix) -> None:
    projects = await m.probe_scope("list_projects", page_size=1)
    forms = await m.probe_scope("list_forms", page_size=1)
    if forms is None:
        m.note("forms scope is available; the matrix has no forms write step")
    else:
        m.note(f"forms: scope missing ({forms})")
    if projects is not None:
        raise Skipped(f"scope missing: {projects}")
    location = await m.location()
    # create_project requires a project type and a group (contract e15cb5a18ec2): the first type the tenant has, and
    # the Everyone group the ticket lookups already resolve by name
    lookups = await m.lookups()
    type_id = first_guid_id((await m.tool("list_project_types"))["items"])
    if type_id is None:
        raise Skipped("the tenant has no project type to create a project with")
    label = m.manifest.label("project")
    # What this area can set off, per the create_project and create_project_task texts and the spec: the
    # project-created notification of the app fires on a plain create_project (Gorelo does not say who receives it; the group is
    # the Everyone group), and creating a task fires the same automation triggers as in the app. The only documented way to
    # suppress both is created_on (a backdated import), which this area does not send: it exercises the plain tools.
    m.note(
        "create_project sends no created_on, so the project-created notification of the app fires (Gorelo does not say who "
        "receives it) and creating the task fires the app's automation triggers; the comment is private and emails nobody"
    )
    project = full(
        await m.create(
            "project",
            label,
            {"client_id": SITE.test_client},
            "create_project",
            {
                "title": label,
                "client_id": SITE.test_client,
                "location_id": location,
                "type_id": type_id,
                "group_id": lookups.everyone,
            },
        ),
        "create_project",
    )
    project_id = project["Id"]
    expect("create_project Title", project.get("Title"), label)

    label = m.manifest.label("section")
    section = await m.create(
        "section",
        label,
        {"project_id": project_id},
        "create_project_section",
        {"project_id": project_id, "title": label},
    )
    label = m.manifest.label("task")
    task = full(
        await m.create(
            "task",
            label,
            {"project_id": project_id},
            "create_project_task",
            {"project_id": project_id, "title": label, "section_id": section["Id"]},
        ),
        "create_project_task",
    )
    task_id = task["Id"]
    check(same_guid(task.get("SectionId"), section["Id"]), "the new task is not in the section it was created in")

    label = m.manifest.label("task comment")
    posted = full(
        await m.create(
            "project_comment",
            label,
            {"project_id": project_id, "task_id": task_id},
            "create_project_comment",
            {
                "project_id": project_id,
                "task_id": task_id,
                "body": f"<p>{label}</p>",
                "conversation_type": "private",
            },
        ),
        "create_project_comment",
    )
    comment_id = posted["Id"]
    gone = await m.tool(
        "delete_project_comment", project_id=project_id, task_id=task_id, comment_id=comment_id, confirm=True
    )
    check(same_guid(gone.get("Id"), comment_id), "delete_project_comment answered with another comment's Id")
    m.mark_cleaned("project_comment", comment_id, "deleted by the write matrix (delete_project_comment)")
    gone = await m.tool("delete_project_task", project_id=project_id, task_id=task_id, confirm=True)
    check(same_guid(gone.get("Id"), task_id), "delete_project_task answered with another task's Id")
    m.mark_cleaned("task", task_id, "deleted by the write matrix (delete_project_task)")


# --------------------------------------------------------------------------
# The opt-in areas of release 1.5.0 (contract 6c30770dffc0): off by default, one flag each (OPT_INS)
# --------------------------------------------------------------------------


def opt_in(m: Matrix, key: str) -> OptIn:
    """The definition of an opt-in area, after checking that the run enabled it: Skipped (nothing announced, nothing sent: its tools
    are not even callable) with the reason otherwise. Every one of these areas needs the TEST CLIENT Active, like every write run (the
    check of run_matrix, before a manifest is made); none of them changes the status of a client."""
    area = OPT_IN_BY_KEY[key]
    if area.held_tools:  # while the area's tools are held back it is skipped, whether or not its flag was given
        raise Skipped(area.held_reason)
    if key not in m.opt_in:
        raise Skipped(area.skip_reason)
    return area


def status_id_of(row: Any) -> Any:
    """The status id of a row whose Status is {Id, Name} (or, tolerated, the bare number)."""
    status = row.get("Status") if isinstance(row, dict) else None
    return status.get("Id") if isinstance(status, dict) else status


def ids_of(listing: Any) -> list[Any]:
    rows = listing.get("items") if isinstance(listing, dict) else None
    return [row.get("Id") for row in (rows if isinstance(rows, list) else []) if isinstance(row, dict)]


def has_id(ids: Any, ident: Any) -> bool:
    """Is `ident` among these ids (a GUID compares ignoring case)?"""
    return any((same_guid(found, ident) if isinstance(ident, str) else found == ident) for found in (ids if isinstance(ids, list) else []))


def lists_id(listing: Any, ident: Any) -> bool:
    """Does a list answer hold a row with this Id?"""
    return has_id(ids_of(listing), ident)


ALERT_SUBJECT_KEYS = ("AgentAssetId", "CheckId", "UptimeId", "DomainId", "ContractId", "CustomAssetId")  # what an alert is about
ALERT_DISMISSED_PAGE = 200  # list_alerts page size when every Dismissed alert of the test client is read
ALERT_DISMISSED_PAGES = 50  # pages read at most; more than that and the area is skipped (nothing proves the stack is free)


def alert_stack_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """What Gorelo groups an alert's stack by, as far as this harness can see: the Type and the subject ids (AgentAssetId, CheckId, UptimeId,
    DomainId, ContractId, CustomAssetId), compared without regard to case. The Title is NOT part of it: the stack is the repeats of one alert,
    and two alerts with different titles may still be one stack, so a Title match is not trusted."""
    kind = row.get("Type")
    kind = kind.get("Id") if isinstance(kind, dict) else kind
    return (kind, *[str(row[key]).lower() if row.get(key) is not None else None for key in ALERT_SUBJECT_KEYS])


async def dismissed_alerts_of_the_test_client(m: Matrix) -> list[dict[str, Any]]:
    """Every Dismissed alert of the test client, on EVERY page (a stack member on page two would be restored by the restore too)."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(ALERT_DISMISSED_PAGES):
        arguments: dict[str, Any] = {"client_ids": [SITE.test_client], "status": ["dismissed"], "page_size": ALERT_DISMISSED_PAGE}
        if cursor is not None:
            arguments["cursor"] = cursor
        page = await m.tool("list_alerts", **arguments)
        rows.extend(row for row in (page.get("items") or []) if isinstance(row, dict))
        cursor = page.get("next_cursor")
        if not page.get("has_more") or not isinstance(cursor, str) or not cursor:
            return rows
    raise Skipped(
        f"the test client {SITE.test_client} has more than {ALERT_DISMISSED_PAGES * ALERT_DISMISSED_PAGE} Dismissed alerts, so the area cannot "
        "prove that restoring one to New would not move another back; nothing was changed"
    )


async def area_alert_status(m: Matrix) -> None:
    """Dismiss, then restore to New, ONE existing New alert of the test client (set_alert_status, get_alert).

    The run never creates an alert (post_alert is not a tool of the matrix). Gorelo changes the whole stack of an alert (the repeats grouped
    with it) with it: dismissing moves every New alert of the stack and restoring to New moves every Dismissed one back. So the area reads
    EVERY Dismissed alert of the test client (all pages) and uses an alert only when none of them shares its Type and subject ids
    (alert_stack_key; the Title is not compared): restoring would move such an alert back to New, which the run did not dismiss. After the
    restore, an id in its UpdatedIds that the dismissal did not name FAILS the area, naming the ids as a leftover for the user.
    The alert is recorded in the manifest (kind alert, details client_id and status_before 1, then dismissed_ids: the ids the dismissal
    moved) BEFORE the first change: the guard allows only a recorded alert of the test client and the cleanup restores one a stopped run
    left dismissed, only within the run's window and only for the ids recorded. Skipped, with a note, when no alert of the test client is New."""
    opt_in(m, "alert_status")
    listing = await m.tool("list_alerts", client_ids=[SITE.test_client], page_size=50)
    rows = [row for row in (listing.get("items") or []) if isinstance(row, dict)]
    candidates = [
        row
        for row in rows
        if is_guid(row.get("Id")) and status_id_of(row) == ALERT_NEW and row.get("ClientId") == SITE.test_client
    ]
    if not candidates:
        raise Skipped(f"no alert of the test client {SITE.test_client} is New among the {len(rows)} listed, so there is nothing to dismiss")
    dismissed_already = await dismissed_alerts_of_the_test_client(m)
    taken = {alert_stack_key(other) for other in dismissed_already}
    chosen = next((row for row in candidates if alert_stack_key(row) not in taken), None)
    if chosen is None:
        raise Skipped(
            f"every New alert of the test client {SITE.test_client} shares its Type and subject with a Dismissed alert (the Title is not "
            "compared), which restoring to New would move back too; nothing was changed"
        )
    alert_id = chosen["Id"]
    label = m.manifest.label(f"alert {alert_id}")
    m.manifest.created("alert", alert_id, label, {"client_id": SITE.test_client, "status_before": ALERT_NEW})
    dismissed = await m.tool("set_alert_status", alert_ids=[alert_id], status="dismissed")
    check(has_id(dismissed.get("UpdatedIds"), alert_id), "set_alert_status dismissed: UpdatedIds does not name the alert")
    moved = [str(item) for item in dismissed.get("UpdatedIds", [])]
    m.manifest.update_details("alert", alert_id, dismissed_ids=moved)  # what the cleanup may restore, and the area checks the restore against
    count = len(moved)
    got = await m.tool("get_alert", alert_id=alert_id)
    check(same_guid(got.get("Id"), alert_id), "get_alert returned another alert")
    expect("get_alert Status.Id after the dismissal", status_id_of(got), ALERT_DISMISSED)
    check(got.get("DismissedOn") is not None, "get_alert after the dismissal: DismissedOn is not set")
    restored = await m.tool("set_alert_status", alert_ids=[alert_id], status="new")
    check(has_id(restored.get("UpdatedIds"), alert_id), "set_alert_status new: UpdatedIds does not name the alert")
    back = await m.tool("get_alert", alert_id=alert_id)
    expect("get_alert Status.Id after the restore", status_id_of(back), ALERT_NEW)
    check(back.get("DismissedOn") is None, "get_alert after the restore: DismissedOn is still set")
    m.mark_cleaned("alert", alert_id, "restored to New by the write matrix (set_alert_status)")
    known = {item.lower() for item in moved}
    extra = [str(item) for item in restored.get("UpdatedIds", []) if str(item).lower() not in known]
    if extra:
        raise CheckFailed(
            f"set_alert_status new moved back {len(extra)} alert(s) that the dismissal had not moved: {', '.join(extra)}. They were Dismissed "
            "before the run or were dismissed by somebody meanwhile and are New now: LEFT FOR THE USER, who has to dismiss them again in the "
            "Gorelo app if they should stay dismissed"
        )
    m.note(
        f"alert {alert_id} dismissed ({count} alert(s) of its stack moved) and restored to New ({len(restored.get('UpdatedIds', []))} moved back); "
        "get_alert read Dismissed with DismissedOn set, then New with it cleared"
    )


async def area_asset_types(m: Matrix) -> None:
    """Create, rename and delete an MCPTEST asset type (asset types belong to the whole account, so the name starts with the run id)."""
    opt_in(m, "asset_types")
    label = m.manifest.label("asset type")
    made = full(await m.create("asset_type", label, {}, "create_asset_type", {"name": label}), "create_asset_type")
    type_id = made["Id"]
    expect("create_asset_type Name", made.get("Name"), label)
    renamed = f"{label} renamed"
    changed = full(await m.tool("update_asset_type", asset_type_id=type_id, name=renamed), "update_asset_type")
    expect("update_asset_type Name", changed.get("Name"), renamed)
    listing = await m.tool("list_asset_types")
    shown = [row for row in listing.get("items", []) if isinstance(row, dict) and row.get("Id") == type_id]
    check(len(shown) == 1 and shown[0].get("Name") == renamed, "list_asset_types does not show the renamed type once")
    gone = await m.tool("delete_asset_type", asset_type_id=type_id, confirm=True)
    expect("delete_asset_type Id", gone.get("Id"), type_id)
    check(not lists_id(await m.tool("list_asset_types"), type_id), "list_asset_types still lists the deleted type")
    m.mark_cleaned("asset_type", type_id, "deleted by the write matrix (delete_asset_type)")


async def area_client_locations(m: Matrix) -> None:
    """Create, update and delete one MCPTEST location of the test client. Gorelo marks a deleted location inactive: it leaves the client's list
    (checked) and the single read afterwards is only noted."""
    opt_in(m, "client_locations")
    client = SITE.test_client
    label = m.manifest.label("location")
    made = full(
        await m.create(
            "client_location",
            label,
            {"client_id": client},
            "create_client_location",
            {"client_id": client, "name": label, "address1": "1 Test Street", "city": "Seattle"},
        ),
        "create_client_location",
    )
    location = made["Id"]
    expect("create_client_location Name", made.get("Name"), label)
    expect("create_client_location ClientId", made.get("ClientId"), client)
    check(made.get("IsDefault") is not True, "create_client_location: the new location is the default location of the test client")
    changed = full(
        await m.tool("update_client_location", client_id=client, location_id=location, city="Tacoma", address2="Suite 2"),
        "update_client_location",
    )
    expect("update_client_location City", changed.get("City"), "Tacoma")
    expect("update_client_location Address2", changed.get("Address2"), "Suite 2")
    expect("update_client_location Name (left out of the update)", changed.get("Name"), label)
    got = await m.tool("get_client_location", client_id=client, location_id=location)
    expect("get_client_location City", got.get("City"), "Tacoma")
    check(lists_id(await m.tool("list_client_locations", client_id=client), location), "list_client_locations does not list the new location")
    gone = await m.tool("delete_client_location", client_id=client, location_id=location, confirm=True)
    expect("delete_client_location Id", gone.get("Id"), location)
    check(not lists_id(await m.tool("list_client_locations", client_id=client), location), "list_client_locations still lists the deleted location")
    try:
        after = await m.tool("get_client_location", client_id=client, location_id=location)
        m.note(f"get_client_location after the delete still answers (IsDefault {after.get('IsDefault')!r}): Gorelo keeps an inactive location")
    except ToolFailed as exc:
        m.note(f"get_client_location after the delete answers: {exc.text[:120]}")
    m.mark_cleaned("client_location", location, "deleted by the write matrix (delete_client_location; Gorelo marks it inactive)")


async def area_client_domains(m: Matrix) -> None:
    """Create, update and delete one MCPTEST domain of the test client: mcptest-<run>.example, under the reserved .example TLD (it can route no
    inbound email). The expiration date is sent explicitly, so Gorelo's registry lookup is never asked."""
    opt_in(m, "client_domains")
    client = SITE.test_client
    name = m.manifest.run_id.lower() + ".example"
    label = m.manifest.label("domain")
    today = m.clock().astimezone(timezone.utc).date()
    first, second = (today + timedelta(days=365)).isoformat(), (today + timedelta(days=366)).isoformat()
    made = full(
        await m.create(
            "client_domain",
            label,
            {"client_id": client, "name": name},
            "create_client_domain",
            {"client_id": client, "name": name, "expiration_date": first},
        ),
        "create_client_domain",
    )
    domain = made["Id"]
    expect("create_client_domain Name", made.get("Name"), name)
    expect("create_client_domain ExpirationDate", date_text(made.get("ExpirationDate")), first)
    changed = full(
        await m.tool("update_client_domain", client_id=client, domain_id=domain, expiration_date=second), "update_client_domain"
    )
    expect("update_client_domain ExpirationDate", date_text(changed.get("ExpirationDate")), second)
    expect("update_client_domain Name (left out of the update)", changed.get("Name"), name)
    gone = await m.tool("delete_client_domain", client_id=client, domain_id=domain, confirm=True)
    expect("delete_client_domain Id", gone.get("Id"), domain)
    domains = (await m.tool("get_client", client_id=client)).get("Domains")
    check(
        not any(isinstance(entry, dict) and entry.get("Id") == domain for entry in (domains if isinstance(domains, list) else [])),
        "get_client still lists the deleted domain in its Domains",
    )
    m.mark_cleaned("client_domain", domain, "deleted by the write matrix (delete_client_domain)")


async def area_contact_tags(m: Matrix) -> None:
    """On an MCPTEST contact: set tag_ids, update another field WITHOUT tag_ids, then clear the tags with [].

    update_contact sends TagIds only when the caller gives tag_ids and never re-sends the stored tags (Gorelo's text: "omit to leave it
    unchanged"), so the second step tests Gorelo's own omit rule. Whether Gorelo kept the tags is NOT a failure of the area either way: it is
    the answer the run was made to get, and the note says which it was (the update's answer and a fresh get_contact are both read)."""
    opt_in(m, "contact_tags")
    tags = (await m.tool("list_contact_tags")).get("items")
    tag = first_int_id(tags)
    if tag is None:
        raise Skipped("the tenant has no contact tag to set")
    run = m.manifest.run_id
    label = m.manifest.label("tagged contact")
    made = full(
        await m.create(
            "contact",
            label,
            {"client_id": SITE.test_client},
            "create_contact",
            {
                "client_id": SITE.test_client,
                "first_name": run,
                "last_name": "Tagged",
                "primary_email": f"{run.lower()}-tags@example.invalid",
            },
        ),
        "create_contact",
    )
    contact = made["Id"]
    expect("create_contact TagIds (none asked for)", made.get("TagIds"), [])
    tagged = await m.tool("update_contact", contact_id=contact, tag_ids=[tag])
    expect("update_contact TagIds after setting them", tagged.get("TagIds"), [tag])
    title = f"{run} tagged title"
    omitted = await m.tool("update_contact", contact_id=contact, job_title=title)
    expect("update_contact JobTitle", omitted.get("JobTitle"), title)
    back = await m.tool("get_contact", contact_id=contact)
    answered, stored = omitted.get("TagIds"), back.get("TagIds")
    kept = answered == [tag] and stored == [tag]
    cleared = await m.tool("update_contact", contact_id=contact, tag_ids=[])
    expect("update_contact TagIds after tag_ids=[]", cleared.get("TagIds"), [])
    expect("get_contact TagIds after tag_ids=[]", (await m.tool("get_contact", contact_id=contact)).get("TagIds"), [])
    m.note(
        f"contact tags (tag {tag}) set; an update that named no tag_ids (Gorelo's omit rule, nothing re-sent): "
        + (
            "Gorelo KEPT the tags (omitting TagIds leaves them unchanged)"
            if kept
            else f"Gorelo did NOT keep the tags (the update answered TagIds {answered!r} and get_contact read {stored!r}), so omitting TagIds "
            "does not leave them unchanged"
        )
        + "; then cleared with []"
    )


async def area_product_charges(m: Matrix) -> None:
    """On the run's plain MCPTEST ticket of the test client: create, update and delete a No charge product charge (billable status 2, so it
    can never be invoiced). The product is the first active product of nobody or of the test client; the ticket is the plain ticket the
    comments and time areas use (made here when this area runs alone)."""
    opt_in(m, "product_charges")
    found = await m.tool("list_items", type="product", status="active", page_size=CATALOG_PAGE)
    item_id = billable_item(found.get("items"))
    if item_id is None:
        raise Skipped(
            f"no active product of nobody or of client {SITE.test_client} among the first {CATALOG_PAGE} catalog items to charge"
        )
    ticket = full(await m.plain_ticket(), "create_ticket")["Id"]
    label = m.manifest.label("product charge")
    made = full(
        await m.create(
            "product_charge",
            label,
            {"ticket_id": ticket, "client_id": SITE.test_client},
            "create_product_charge",
            {
                "item_id": item_id,
                "ticket_id": ticket,
                "quantity": 1,
                "unit_price": 1.0,
                "description": label,
                "billable_status_id": NO_CHARGE,
            },
        ),
        "create_product_charge",
    )
    charge = made["Id"]
    check(same_guid(made.get("TicketId"), ticket), "create_product_charge: the charge is not on the ticket that was asked for")
    expect("create_product_charge Quantity", made.get("Quantity"), 1)
    expect("create_product_charge BillableStatus.Id (No charge)", dig(made, "BillableStatus", "Id"), NO_CHARGE)
    expect("create_product_charge ClientId", made.get("ClientId"), SITE.test_client)
    changed = await m.tool("update_product_charge", product_charge_id=charge, quantity=2, unit_price=2.0, description=f"{label} updated")
    expect("update_product_charge Quantity", changed.get("Quantity"), 2)
    expect("update_product_charge UnitPrice", changed.get("UnitPrice"), 2.0)
    expect("update_product_charge BillableStatus.Id (left out of the update)", dig(changed, "BillableStatus", "Id"), NO_CHARGE)
    got = await m.tool("get_product_charge", product_charge_id=charge)
    expect("get_product_charge Description", got.get("Description"), f"{label} updated")
    check(lists_id(await m.tool("list_product_charges", ticket_ids=[ticket]), charge), "list_product_charges does not list the new charge")
    gone = await m.tool("delete_product_charge", product_charge_id=charge, confirm=True)
    expect("delete_product_charge Id", gone.get("Id"), charge)
    check(not lists_id(await m.tool("list_product_charges", ticket_ids=[ticket]), charge), "list_product_charges still lists the deleted charge")
    m.mark_cleaned("product_charge", charge, "deleted by the write matrix (delete_product_charge)")


async def area_ticket_source(m: Matrix) -> None:
    """Set the source of the run's plain MCPTEST ticket to another source (1 to 6; 7 Messenger is read back only, never written) and read it
    back, then put it back to the Api source the ticket was created with."""
    opt_in(m, "ticket_source")
    lookups = await m.lookups()
    plain = full(await m.plain_ticket(), "create_ticket")
    ticket = plain["Id"]
    current = dig(plain, "Source", "Id")
    other = next(
        (
            row
            for row in lookups.sources
            if isinstance(row, dict) and is_id(row.get("Id")) and row["Id"] <= 6 and row["Id"] not in (current, lookups.api)
        ),
        None,
    )
    if other is None:
        raise Skipped("list_ticket_sources has no writable source (1 to 6) other than the one of the ticket")
    changed = await m.tool("update_ticket", ticket_id=ticket, source_id=other["Id"])
    expect("update_ticket Source.Id", dig(changed, "Source", "Id"), other["Id"])
    got = await m.get_ticket(ticket, "guid")
    expect("get_ticket Source.Id after the update", dig(got, "Source", "Id"), other["Id"])
    back = await m.tool("update_ticket", ticket_id=ticket, source_id=lookups.api)
    expect("update_ticket Source.Id (back to Api)", dig(back, "Source", "Id"), lookups.api)
    m.note(f"ticket source set to {other['Id']} ({other.get('Name')!r}) and read back, then put back to {lookups.api} (Api)")


def line_state(line: Any) -> dict[str, Any]:
    """What a line (or contract line item) says about its price: the values that an entry with an Id only must leave alone."""
    line = line if isinstance(line, dict) else {}
    return {
        "Quantity": line.get("Quantity"),
        "UnitPrice": line.get("UnitPrice"),
        "Description": line.get("Description"),
        "DiscountPercent": line.get("DiscountPercent"),
        "Tax": dig(line, "Tax", "Id"),
        "BillableStatus": dig(line, "BillableStatus", "Id"),
    }


def lines_by_text(invoice: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The invoice's lines by their Description (the run label plus a letter): line text -> line."""
    lines = invoice.get("LineItems")
    found = {}
    for line in lines if isinstance(lines, list) else []:
        if isinstance(line, dict) and isinstance(line.get("Description"), str):
            found[line["Description"]] = line
    return found


async def area_invoice_update(m: Matrix) -> None:
    """update_invoice on a Draft of the test client, then a TAX PROBE.

    The Draft has two lines (A and B). Steps: update the reference and both dates; add line C; change line A; remove line C. After every step
    the lines the step did not name must read exactly as before (the tool sends {Id} alone for them: this settles whether Gorelo keeps the
    fields an entry with only an Id leaves out). The Draft is deleted at the end (it uses up an invoice number).

    The TAX PROBE: a temporary MCPTEST catalog item whose tax is the first rate of list_taxes with Percentage above 0 (skipped, with a note, when
    there is none, or when list_taxes does not hold exactly one Tax Exempt rate: the tool would refuse) goes onto a second Draft with
    no_tax=true. The tool looks up the Tax Exempt rate and sends its id as TaxId (a null would give the item's own tax, measured 2026-10-30).
    The rate is recorded in the intent before the write. The note records which TaxId the tool used and which TaxId and tax amount Gorelo
    applied. The area FAILS unless the line's TaxAmount and the invoice's TotalTax read 0. create_invoice adds a `warning` to its answer when
    a no_tax line still shows tax; the note records it. The Draft and the item are deleted first, then the check is made."""
    opt_in(m, "invoice_update")
    found = await m.tool("list_items", type="product", status="active", page_size=CATALOG_PAGE)
    item_id = billable_item(found.get("items"))
    if item_id is None:
        raise Skipped(
            f"no active product of nobody or of client {SITE.test_client} among the first {CATALOG_PAGE} catalog items to put on the invoice"
        )
    today = m.clock().astimezone(timezone.utc).date()
    label = m.manifest.label("update invoice")
    text_a, text_b, text_c = f"{label} A", f"{label} B", f"{label} C"

    def line(text: str, price: float) -> dict[str, Any]:
        return {"item_id": item_id, "quantity": 1, "unit_price": price, "description": text}

    made = await m.create(
        "invoice",
        label,
        {"client_id": SITE.test_client, "status_id": INVOICE_DRAFT},
        "create_invoice",
        {
            "client_id": SITE.test_client,
            "line_items": [line(text_a, 1.0), line(text_b, 2.0)],
            "invoice_date": today.isoformat(),
            "due_date": (today + timedelta(days=INVOICE_DUE_DAYS)).isoformat(),
            "reference": label,
        },
        recorded=invoice_record,
    )
    invoice_id = made["Id"]
    invoice = full(made, "create_invoice")
    expect("create_invoice Status.Id (Draft)", dig(invoice, "Status", "Id"), INVOICE_DRAFT)
    before = lines_by_text(invoice)
    check(set(before) == {text_a, text_b}, "create_invoice: the Draft does not have exactly the two lines that were asked for")
    states = {text: line_state(row) for text, row in before.items()}
    m.note(
        f"invoice number consumed: Number {invoice.get('Number')!r}, DisplayNumber {invoice.get('DisplayNumber')!r} (a Draft uses up one invoice number)"
    )

    def same_as_before(read: dict[str, Any], texts: list[str], step: str) -> None:
        lines = lines_by_text(read)
        for text in texts:
            name = text[len(label):].strip()
            check(text in lines, f"update_invoice ({step}) line {name} (named by its Id only): it no longer reads with its text, so its values were not kept")
            expect(f"update_invoice ({step}) line {name} (named by its Id only)", line_state(lines[text]), states[text])

    reference, new_date, new_due = f"{label} ref", (today + timedelta(days=1)).isoformat(), (today + timedelta(days=30)).isoformat()
    step = await m.tool("update_invoice", invoice_id=invoice_id, reference=reference, invoice_date=new_date, due_date=new_due)
    expect("update_invoice Reference", step.get("Reference"), reference)
    expect("update_invoice InvoiceDate", date_text(step.get("InvoiceDate")), new_date)
    expect("update_invoice DueDate", date_text(step.get("DueDate")), new_due)
    same_as_before(step, [text_a, text_b], "header only")
    step = await m.tool("update_invoice", invoice_id=invoice_id, add_lines=[line(text_c, 3.0)])
    check(len(step.get("LineItems") or []) == 3, "update_invoice (add a line): the Draft does not have three lines")
    same_as_before(step, [text_a, text_b], "a line added")
    states[text_c] = line_state(lines_by_text(step).get(text_c))
    line_a = lines_by_text(step)[text_a]["Id"]
    step = await m.tool("update_invoice", invoice_id=invoice_id, update_lines=[{"line_id": line_a, "quantity": 2, "unit_price": 4.0}])
    changed_a = line_state(lines_by_text(step).get(text_a))
    expect("update_invoice (change a line) Quantity of line A", changed_a["Quantity"], 2)
    expect("update_invoice (change a line) UnitPrice of line A", changed_a["UnitPrice"], 4.0)
    states[text_a] = {**states[text_a], "Quantity": 2, "UnitPrice": 4.0}
    same_as_before(step, [text_a, text_b, text_c], "a line changed")
    line_c = lines_by_text(step)[text_c]["Id"]
    step = await m.tool("update_invoice", invoice_id=invoice_id, remove_line_ids=[line_c])
    check(len(step.get("LineItems") or []) == 2, "update_invoice (remove a line): the Draft does not have two lines")
    same_as_before(step, [text_a, text_b], "a line removed")
    m.note(
        "update_invoice: the lines that a step did not name were sent as {Id} alone and read back with the same Quantity, UnitPrice, "
        "Description, DiscountPercent, Tax and BillableStatus after all four steps: Gorelo keeps the fields an entry with only an Id leaves out"
    )
    after = await m.tool("get_invoice", invoice_id=invoice_id)
    expect("get_invoice Status.Id before the delete", dig(after, "Status", "Id"), INVOICE_DRAFT)
    number = after.get("Number")
    if is_id(number):
        gone = await m.delete_draft(number)
        check(same_guid(gone.get("Id"), invoice_id), "delete_invoice answered with another invoice's Id")
        expect("delete_invoice StatusId (Deleted)", gone.get("StatusId"), INVOICE_DELETED)
        m.mark_cleaned("invoice", invoice_id, "deleted by the write matrix (delete_invoice, Draft)")
    else:
        m.note("the Draft has no Number, so delete_invoice cannot find it: the cleanup removes it with its raw DELETE")
    await tax_probe(m, today)


async def tax_probe(m: Matrix, today: Any) -> None:
    """Does no_tax=true give NO tax on a line of an item taxed above 0? A temporary MCPTEST item taxed at a rate above 0 goes onto a Draft with
    no_tax=true; the tool sends the id of the Tax Exempt rate. The note says which TaxId the tool used and which TaxId and tax amount Gorelo
    applied; the area FAILS (after the Draft and the item are deleted) unless TaxAmount and TotalTax read 0. Skipped, with a note, when
    list_taxes has no rate above 0 or not exactly one Tax Exempt rate."""
    taxes = (await m.tool("list_taxes")).get("items")
    rate = next(
        (
            row
            for row in (taxes if isinstance(taxes, list) else [])
            if isinstance(row, dict) and is_id(row.get("Id")) and is_amount(row.get("Percentage")) and row["Percentage"] > 0
        ),
        None,
    )
    if rate is None:
        m.note("TAX PROBE skipped: list_taxes has no rate with Percentage above 0")
        return
    exempt = tax_exempt_rates(taxes)
    if len(exempt) != 1:
        found = "none" if not exempt else f"{len(exempt)} (Ids {', '.join(str(row['Id']) for row in exempt)})"
        m.note(f"TAX PROBE skipped: list_taxes must hold exactly one Tax Exempt rate for no_tax to resolve (it holds {found}); nothing was written")
        return
    exempt_id = exempt[0]["Id"]
    item_label = m.manifest.label("tax probe item")
    item = full(
        await m.create(
            "item",
            item_label,
            {"client_id": SITE.test_client},
            "create_item",
            {
                "type": "product",
                "name": item_label,
                "client_id": SITE.test_client,
                "description": f"{item_label}: created by the live write matrix, safe to delete.",
                "unit_price": 1.0,
                "tax_id": rate["Id"],
            },
        ),
        "create_item",
    )
    item_id = item["Id"]
    label = m.manifest.label("tax probe invoice")
    made = await m.create(
        "invoice",
        label,
        # the exempt rate and its Percentage 0 are recorded BEFORE the write (the approved invoice's pattern): the guard allows no other TaxId
        {"client_id": SITE.test_client, "status_id": INVOICE_DRAFT, "tax_id": exempt_id, "tax_percentage": 0},
        "create_invoice",
        {
            "client_id": SITE.test_client,
            "line_items": [{"item_id": item_id, "quantity": 1, "unit_price": 1.0, "description": label, "no_tax": True}],
            "invoice_date": today.isoformat(),
            "due_date": (today + timedelta(days=INVOICE_DUE_DAYS)).isoformat(),
            "reference": label,
        },
        recorded=lambda record: {**invoice_record(record), "tax_id": exempt_id, "tax_percentage": 0},
    )
    invoice_id = made["Id"]
    # create_invoice adds a `warning` to the FULL invoice when a no_tax line still shows tax; a warning on an answer without line items is a
    # failed read-back (full() raises for that one). The tax warning is a finding of the probe, so it is taken off and recorded in the note.
    tool_warning = made.get("warning") if isinstance(made.get("LineItems"), list) else None
    tool_note = made.get("note") if isinstance(made.get("note"), str) else None
    dropped = ("warning", "note") if tool_warning else ("note",)
    invoice = full({key: value for key, value in made.items() if key not in dropped}, "create_invoice")
    got = await m.tool("get_invoice", invoice_id=invoice_id)  # a fresh read: what Gorelo stored, not what the create answered
    row = lines_by_text(got).get(label) or {}
    applied, amount, total_tax = dig(row, "Tax", "Id"), row.get("TaxAmount"), got.get("TotalTax", invoice.get("TotalTax"))
    gave_none = is_amount(amount) and amount == 0 and is_amount(total_tax) and total_tax == 0  # by the amounts: how "no tax" is spelled is Gorelo's
    verdict = (
        "no_tax=true gave NO tax"
        if gave_none
        else "no_tax=true did NOT give no tax (a tax was applied although the Tax Exempt rate was sent): the area FAILS"
    )
    m.note(
        f"TAX PROBE: a line of an item taxed at {rate['Percentage']:g}% (TaxId {rate['Id']}) created with no_tax=true (the tool sent TaxId "
        f"{exempt_id}, the Tax Exempt rate): Gorelo applied TaxId {applied!r}, line TaxAmount {amount_text(amount)}, invoice TotalTax "
        f"{amount_text(total_tax)}: {verdict}"
        + (f"; create_invoice warning: {tool_warning}" if tool_warning else "; create_invoice added no warning")
        + (f"; create_invoice note: {tool_note}" if tool_note else "")
    )
    number = got.get("Number")
    if is_id(number) and dig(got, "Status", "Id") == INVOICE_DRAFT:
        gone = await m.delete_draft(number)
        expect("delete_invoice StatusId (Deleted, tax probe)", gone.get("StatusId"), INVOICE_DELETED)
        m.mark_cleaned("invoice", invoice_id, "deleted by the write matrix (delete_invoice, Draft)")
    else:
        m.note("the tax probe Draft was not deleted by the area (no Number or not a Draft): the cleanup handles it")
    gone_item = await m.tool("delete_item", item_id=item_id, confirm=True)
    check(same_guid(gone_item.get("Id"), item_id), "delete_item answered with another item's Id")
    m.mark_cleaned("item", item_id, "deleted by the write matrix (delete_item)")
    check(
        gave_none,
        f"TAX PROBE FAILED: a line created with no_tax=true (TaxId {exempt_id}, the Tax Exempt rate) shows TaxAmount {amount_text(amount)} and the "
        f"invoice TotalTax {amount_text(total_tax)}, expected 0 and 0: no_tax does not give no tax",
    )


async def area_contracts(m: Matrix) -> None:
    """An MCPTEST Hourly contract (labor terms 2) on the test client: one service line with two line items (No charge), update its name and
    one line item, add a second service line then remove it, check that the entries sent with an Id only kept their values, then archive it
    (status 3). A contract CANNOT be deleted through the API (the guard refuses the DELETE): the archived contract is a known residue, like an
    archived document. Never a Block Hours line (it raises an invoice) and never auto_approve_and_send true."""
    opt_in(m, "contracts")
    found = await m.tool("list_items", type="product", status="active", page_size=CATALOG_PAGE)
    item_id = billable_item(found.get("items"))
    if item_id is None:
        raise Skipped(
            f"no active product of nobody or of client {SITE.test_client} among the first {CATALOG_PAGE} catalog items to bill on the contract"
        )
    client = SITE.test_client
    label = m.manifest.label("contract")
    hourly = {"rate_type_id": 1}
    made = full(
        await m.create(
            "contract",
            label,
            {"client_id": client},
            "create_contract",
            {
                "name": label,
                "client_id": client,
                "service_lines": [
                    {
                        "name": f"{label} line",
                        "labor_terms_id": 2,
                        "per_hour_details": hourly,
                        "line_items": [
                            {"item_id": item_id, "quantity": 1, "unit_price": 1.0, "description": f"{label} item A", "billable_status_id": NO_CHARGE},
                            {"item_id": item_id, "quantity": 3, "unit_price": 2.0, "description": f"{label} item B", "billable_status_id": NO_CHARGE},
                        ],
                    }
                ],
                "confirm": True,
            },
        ),
        "create_contract",
    )
    contract = made["Id"]

    def lines_of(record: dict[str, Any]) -> list[dict[str, Any]]:
        lines = record.get("ServiceLines")
        return [row for row in (lines if isinstance(lines, list) else []) if isinstance(row, dict)]

    expect("create_contract Name", made.get("Name"), label)
    expect("create_contract Client.Id", dig(made, "Client", "Id"), client)
    expect("create_contract Status.Id (Active)", dig(made, "Status", "Id"), 1)
    first = lines_of(made)
    check(len(first) == 1, f"create_contract: the contract has {len(first)} service lines, expected 1")
    expect("create_contract line LaborTerms.Id (Hourly)", dig(first[0], "LaborTerms", "Id"), 2)
    items = first[0].get("LineItems") if isinstance(first[0].get("LineItems"), list) else []
    check(len(items) == 2, f"create_contract: the service line has {len(items)} line items, expected 2")
    line_id = first[0]["Id"]
    item_a = next((row for row in items if row.get("Description") == f"{label} item A"), None)
    item_b = next((row for row in items if row.get("Description") == f"{label} item B"), None)
    check(item_a is not None and item_b is not None, "create_contract: the service line does not have the line items A and B that were asked for")
    kept_b = line_state(item_b)  # line item B and the service line's name are only ever named by their Id: they must never change
    line_name = f"{label} line"

    def check_kept(record: dict[str, Any], step: str) -> None:
        lines = lines_of(record)
        current = next((row for row in lines if row.get("Id") == line_id), None)
        check(current is not None, f"update_contract ({step}): the first service line is gone")
        expect(f"update_contract ({step}) service line Name (named by its Id only)", current.get("Name"), line_name)
        row = next((x for x in current.get("LineItems") or [] if x.get("Id") == item_b["Id"]), None)
        check(row is not None, f"update_contract ({step}): line item B is gone")
        expect(f"update_contract ({step}) line item B (named by its Id only)", line_state(row), kept_b)

    renamed = f"{label} renamed"
    changed = await m.tool(
        "update_contract",
        contract_id=contract,
        name=renamed,
        service_lines=[{"id": line_id, "line_items": [{"id": item_a["Id"], "quantity": 2}]}],
        confirm=True,
    )
    expect("update_contract Name", changed.get("Name"), renamed)
    lines = lines_of(changed)
    check(len(lines) == 1 and lines[0].get("Id") == line_id, "update_contract: the service line did not stay")
    now_a = next((row for row in lines[0].get("LineItems") or [] if row.get("Id") == item_a["Id"]), {})
    expect("update_contract line item A Quantity", now_a.get("Quantity"), 2)
    expect("update_contract line item A UnitPrice (left out of the update)", now_a.get("UnitPrice"), 1.0)
    check_kept(changed, "a line item changed")
    kept_a = line_state(now_a)
    added = await m.tool(
        "update_contract",
        contract_id=contract,
        service_lines=[{"name": f"{label} line 2", "labor_terms_id": 2, "per_hour_details": hourly}],
        confirm=True,
    )
    lines = lines_of(added)
    check(len(lines) == 2, f"update_contract (add a line): the contract has {len(lines)} service lines, expected 2")
    second = next((row for row in lines if row.get("Id") != line_id), None)
    check(second is not None and second.get("Name") == f"{label} line 2", "update_contract (add a line): the new service line is not there")
    check_kept(added, "a service line added")
    removed = await m.tool("update_contract", contract_id=contract, remove_service_line_ids=[second["Id"]], confirm=True)
    lines = lines_of(removed)
    check(len(lines) == 1 and lines[0].get("Id") == line_id, "update_contract (remove a line): the contract does not have the first line only")
    check_kept(removed, "a service line removed")
    after_a = next((row for row in lines[0].get("LineItems") or [] if row.get("Id") == item_a["Id"]), {})
    expect("update_contract (remove a line) line item A (named by its Id only)", line_state(after_a), kept_a)
    m.note(
        "update_contract: the service line and the line items that a step sent with an Id only read back with the same Name, Quantity, UnitPrice, "
        "Description, DiscountPercent, Tax and BillableStatus after a line item was changed and a second service line was added and removed"
    )
    archived = await m.tool("update_contract", contract_id=contract, status="archived", confirm=True)
    expect("update_contract Status.Id (Archived)", dig(archived, "Status", "Id"), CONTRACT_ARCHIVED)
    m.manifest.cleanup_failed("contract", contract, contract_residue("archived by the write matrix (update_contract)"))
    m.note(f"contract {contract} archived: it cannot be deleted through the API, so it stays in Gorelo until removed in the Gorelo app")


async def area_comment_edits(m: Matrix) -> None:
    """On an MCPTEST project of the test client: a Private project comment and a Private task comment, edit both bodies (update_project_comment,
    update_task_comment), then delete the comments and the task (the project is soft deleted by the cleanup). Like the projects area it needs
    the Project scope (skipped, with the scope named, when the key lacks it)."""
    opt_in(m, "comment_edits")
    missing = await m.probe_scope("list_projects", page_size=1)
    if missing is not None:
        raise Skipped(f"scope missing: {missing}")
    location = await m.location()
    lookups = await m.lookups()
    type_id = first_guid_id((await m.tool("list_project_types"))["items"])
    if type_id is None:
        raise Skipped("the tenant has no project type to create a project with")
    label = m.manifest.label("edit project")
    m.note(
        "create_project sends no created_on, so the project-created notification of the app fires (Gorelo does not say who receives it) and "
        "creating the task fires the app's automation triggers; the comments are Private and email nobody"
    )
    project = full(
        await m.create(
            "project",
            label,
            {"client_id": SITE.test_client},
            "create_project",
            {"title": label, "client_id": SITE.test_client, "location_id": location, "type_id": type_id, "group_id": lookups.everyone},
        ),
        "create_project",
    )
    project_id = project["Id"]
    label = m.manifest.label("edit section")
    section = await m.create(
        "section", label, {"project_id": project_id}, "create_project_section", {"project_id": project_id, "title": label}
    )
    label = m.manifest.label("edit task")
    task = full(
        await m.create(
            "task",
            label,
            {"project_id": project_id},
            "create_project_task",
            {"project_id": project_id, "title": label, "section_id": section["Id"]},
        ),
        "create_project_task",
    )
    task_id = task["Id"]
    label = m.manifest.label("project comment")
    posted = full(
        await m.create(
            "project_comment",
            label,
            {"project_id": project_id},
            "create_project_comment",
            {"project_id": project_id, "body": f"<p>{label}</p>", "conversation_type": "private"},
        ),
        "create_project_comment",
    )
    comment_id = posted["Id"]
    edited = f"<p>{label} edited</p>"
    changed = full(await m.tool("update_project_comment", project_id=project_id, comment_id=comment_id, body=edited), "update_project_comment")
    expect("update_project_comment BodyHtml", changed.get("BodyHtml"), edited)
    got = await m.tool("get_project_comment", project_id=project_id, comment_id=comment_id)
    expect("get_project_comment BodyHtml after the edit", got.get("BodyHtml"), edited)
    label = m.manifest.label("task comment")
    task_posted = full(
        await m.create(
            "project_comment",
            label,
            {"project_id": project_id, "task_id": task_id},
            "create_project_comment",
            {"project_id": project_id, "task_id": task_id, "body": f"<p>{label}</p>", "conversation_type": "private"},
        ),
        "create_project_comment",
    )
    task_comment = task_posted["Id"]
    task_edited = f"<p>{label} edited</p>"
    changed = full(
        await m.tool("update_task_comment", project_id=project_id, task_id=task_id, comment_id=task_comment, body=task_edited),
        "update_task_comment",
    )
    expect("update_task_comment BodyHtml", changed.get("BodyHtml"), task_edited)
    got = await m.tool("get_project_comment", project_id=project_id, task_id=task_id, comment_id=task_comment)
    expect("get_project_comment (task comment) BodyHtml after the edit", got.get("BodyHtml"), task_edited)
    gone = await m.tool("delete_project_comment", project_id=project_id, task_id=task_id, comment_id=task_comment, confirm=True)
    check(same_guid(gone.get("Id"), task_comment), "delete_project_comment answered with another comment's Id")
    m.mark_cleaned("project_comment", task_comment, "deleted by the write matrix (delete_project_comment)")
    gone = await m.tool("delete_project_comment", project_id=project_id, comment_id=comment_id, confirm=True)
    check(same_guid(gone.get("Id"), comment_id), "delete_project_comment answered with another comment's Id")
    m.mark_cleaned("project_comment", comment_id, "deleted by the write matrix (delete_project_comment)")
    gone = await m.tool("delete_project_task", project_id=project_id, task_id=task_id, confirm=True)
    check(same_guid(gone.get("Id"), task_id), "delete_project_task answered with another task's Id")
    m.mark_cleaned("task", task_id, "deleted by the write matrix (delete_project_task)")


# --------------------------------------------------------------------------
# Selecting and running areas
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Area:
    key: str
    run: Callable[[Matrix], Awaitable[None]]
    sends_email: bool = False


AREAS: tuple[Area, ...] = (
    Area("clients", area_clients),
    Area("contacts", area_contacts),
    Area("tickets", area_tickets),
    Area("comments", area_comments),
    Area("email", area_email, sends_email=True),
    Area("time", area_time),
    Area("items", area_items),
    Area(CUSTOM_ASSETS_AREA, area_custom_assets),
    Area(CUSTOM_PROPERTIES_AREA, area_custom_properties),
    Area(DOCUMENTS_AREA, area_documents),
    Area("invoices", area_invoices),
    Area(APPROVED_INVOICE_AREA, area_approved_invoice),
    Area("alert_status", area_alert_status),
    Area("asset_types", area_asset_types),
    Area("client_locations", area_client_locations),
    Area("client_domains", area_client_domains),
    Area("contact_tags", area_contact_tags),
    Area("product_charges", area_product_charges),
    Area("invoice_update", area_invoice_update),
    Area("contracts", area_contracts),
    Area("comment_edits", area_comment_edits),
    Area("ticket_source", area_ticket_source),
    Area("uptime", area_uptime),
    Area("projects", area_projects),
)
ALIASES = {"forms": "projects"}


def select_areas(only: Sequence[str] | None, skip_email: bool, with_approved_invoice: bool = False) -> list[Area]:
    """The areas to run, always in AREAS order (the comments and time areas build on the tickets area).

    `only` is a list of area names (None: all). Raises UsageError for an unknown name, an empty selection, and for
    with_approved_invoice (--with-approved-invoice) unless the approved_invoice area is the ONLY area selected: that run
    approves a real invoice, so nothing else runs beside it."""
    known = [area.key for area in AREAS]
    wanted: list[str] | None = None
    if only is not None:
        wanted = []
        for raw in only:
            name = raw.strip().lower()
            name = ALIASES.get(name, name)
            if name not in known:
                raise UsageError(f"unknown area {raw!r}; the areas are {', '.join(known)}")
            if name not in wanted:
                wanted.append(name)
    chosen = [area for area in AREAS if wanted is None or area.key in wanted]
    if skip_email:
        chosen = [area for area in chosen if not area.sends_email]
    if not chosen:
        raise UsageError("no area left to run: --skip-email removed the only one selected")
    if with_approved_invoice and [area.key for area in chosen] != [APPROVED_INVOICE_AREA]:
        raise UsageError(
            f"--with-approved-invoice runs the {APPROVED_INVOICE_AREA} area alone: use --only {APPROVED_INVOICE_AREA} "
            "(it approves a real invoice, so no other area runs beside it)"
        )
    return chosen


@dataclass
class AreaResult:
    """status: pass, FAIL, skipped (scope missing, no run ticket, items without --with-items, custom_assets without
    --with-custom-assets, custom_properties without --with-custom-properties, while a custom field of the test client is set or
    with no unset text field to use, invoices without
    --with-invoices, approved_invoice without --with-approved-invoice or with a precondition that does not hold, no
    catalog item or project type to use) or not run (the run stopped after a guard refusal)."""

    area: str
    status: str
    detail: str = ""
    notes: list[str] = field(default_factory=list)
    requests: int = 0
    tripped: bool = False


async def _run_area(matrix: Matrix, area: Area) -> AreaResult:
    matrix.notes = []
    requests = matrix.pacer.requests
    refused = len(matrix.guard.violations)
    status, detail, tripped = "pass", "", False
    try:
        await area.run(matrix)
    except Skipped as exc:
        status, detail = "skipped", str(exc)
    except GuardTripped as exc:
        status, detail, tripped = "FAIL", f"the guard refused a request, the run stops: {exc}", True
    except (ToolFailed, CheckFailed) as exc:
        status, detail = "FAIL", str(exc)
        if isinstance(exc, ToolFailed) and exc.tool == "update_client":  # its alarm for other custom fields quotes names and values
            detail = without_quotes(detail)
    except Exception as exc:
        status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
    if len(matrix.guard.violations) > refused and not tripped:
        status, tripped = "FAIL", True
        detail = f"the guard refused a request, the run stops: {matrix.guard.violations[-1]}"
    return AreaResult(
        area=area.key,
        status=status,
        detail=scrub(" ".join(detail.split()), matrix.secret)[:DETAIL_LIMIT],
        notes=list(matrix.notes),
        requests=matrix.pacer.requests - requests,
        tripped=tripped,
    )


def custom_property_summary(record: Any, run_id: str, manifest_path: str) -> str:
    """The summary line for a custom_property record that is left over, by the kind of leftover (custom_property_leftover_kind):
    other custom fields changed while the cleanup cleared the run's field (their ids, and no re-run as the advice), the cleanup
    sent nothing because another custom field of the test client is set, or the field may still hold the run's text."""
    client, ident = record.details.get("client_id"), record.id
    kind = custom_property_leftover_kind(record)
    if kind == "changed":
        return (
            f"custom field(s) {shown_ids(changed_fields_of(record.details))} of client {client} read differently after the run's "
            f"field {ident} was cleared: check them in the Gorelo app; python -m scripts.live.cleanup {manifest_path} reports "
            f'them again until "{CHANGED_FIELDS}" is deleted from custom property {ident} in that manifest'
        )
    if kind == "others_set":
        return (
            f"a custom field value is left over: custom property {ident} of client {client} still holds {run_id}, and other "
            f"custom fields of that client are set, so the cleanup sends no write: clear custom property {ident} in the Gorelo app, "
            f"then run python -m scripts.live.cleanup {manifest_path} to close the record"
        )
    return (
        f"a custom field value is left over: custom property {ident} of client {client} may still hold {run_id}; run python -m "
        f"scripts.live.cleanup {manifest_path} (it clears the field only while it still holds that text), or clear it in the "
        "Gorelo app"
    )


@dataclass
class MatrixReport:
    """What a run did. `ok` is True when no area FAILED, nothing is left over and the guard refused nothing.

    `summary` counts the manifest's records: created, cleaned, leftovers (what needs a human), undeletable (known
    undeletable records, uploaded attachment files and voided invoices, which nobody can remove: neither cleaned nor
    leftovers, never alone a failure) and unresolved_intents.

    `voided` names the approved invoices of the run that were voided (voided_invoices): the summary says for each that the
    void is in Gorelo only (VOID_REMINDER), whatever else the run did."""

    run_id: str
    manifest_path: str
    results: list[AreaResult] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    cleanup: CleanupReport | None = None
    cleanup_error: str | None = None
    scopes_missing: list[str] = field(default_factory=list)
    requests: int = 0
    refused: int = 0
    interrupted: str | None = None
    voided: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    asset_texts: dict[str, str] = field(default_factory=dict)  # asset id -> the residue text of its record (see residue_asset_texts)
    documents: list[str] = field(default_factory=list)  # the documents of the run: none can be deleted through the API (residue_documents)
    contracts: list[str] = field(default_factory=list)  # the contracts of the run: none can be deleted through the API (residue_contracts)

    @property
    def ok(self) -> bool:
        return (
            self.interrupted is None
            and not any(result.status == "FAIL" for result in self.results)
            and self.refused == 0
            and self.cleanup_error is None
            and self.cleanup is not None
            and self.cleanup.ok
        )

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def render(self) -> list[str]:
        width = max([len("area"), *(len(result.area) for result in self.results)]) + 2
        lines = [f"write matrix {self.run_id}: summary", f"{'area':<{width}}{'result':<9}{'requests':<10}detail"]
        for result in self.results:
            lines.append(f"{result.area:<{width}}{result.status:<9}{result.requests:<10}{result.detail}".rstrip())
        notes = [(result.area, note) for result in self.results for note in result.notes]
        if notes:
            lines.append("notes:")
            lines.extend(f"  {area}: {note}" for area, note in notes)
        lines.append(
            f"records: {self.summary.get('created', 0)} created, {self.summary.get('cleaned', 0)} cleaned, "
            f"{self.summary.get('leftovers', 0)} left over, {self.summary.get('undeletable', 0)} known undeletable, "
            f"{self.summary.get('unresolved_intents', 0)} announced without an id"
        )
        for record in self.cleanup.undeletable if self.cleanup is not None else []:
            lines.append(f"known undeletable: {record.kind} {record.id}: {record.outcome}")
        if self.cleanup_error:
            lines.append(f"cleanup could not finish: {self.cleanup_error}")
        lines.append("scopes missing: " + (", ".join(self.scopes_missing) or "none"))
        lines.append(f"requests: {self.requests} sent by the matrix, {self.refused} refused by the guard")
        if self.interrupted:
            lines.append(f"the run was interrupted ({self.interrupted}); the cleanup above ran anyway")
        if not self.ok:
            lines.append(f"if anything is left over: python -m scripts.live.cleanup {self.manifest_path}")
            leftovers = self.cleanup.leftovers if self.cleanup is not None else []
            if any(record.kind == "invoice" and record.details.get("status_id") == INVOICE_APPROVED for record in leftovers):
                lines.append(
                    "an Approved invoice is left over: check the accounting system, then void it with: "
                    f"python -m scripts.live.cleanup {self.manifest_path} --void-approved ({VOID_COMMAND_NOTE})"
                )
            for record in leftovers:  # a value the run set on the test client itself and could not clear: say where, and how
                if record.kind == "custom_property":
                    lines.append(custom_property_summary(record, self.run_id, self.manifest_path))
        # this harness never deletes a custom asset (the spec's soft DELETE is not used): the operator removes it by hand
        for asset in self.assets:
            lines.append(f"reminder: custom asset {asset} {self.asset_texts.get(asset, CUSTOM_ASSET_RESIDUE)}")
        if self.assets:  # and a stranded one is moved back by hand: the cleanup never sends that PATCH
            lines.append(f"reminder: {CUSTOM_ASSET_NEVER_MOVED_BACK}")
        if self.documents:  # a document cannot be deleted through the API either: they stay in Gorelo, archived
            lines.append(
                f"reminder: {len(self.documents)} document(s) of this run stay in Gorelo {DOCUMENT_RESIDUE_TAIL}; the cleanup lines above "
                f"say which are archived: search the Documents area of Gorelo for {self.run_id}"
            )
        if self.contracts:  # a contract cannot be deleted through the API either: archived, it stays in Gorelo
            lines.append(
                f"reminder: {len(self.contracts)} contract(s) of this run stay in Gorelo {CONTRACT_RESIDUE_TAIL}; the cleanup lines above "
                f"say which are archived: search the Contracts of Gorelo for {self.run_id}"
            )
        for name in self.voided:  # the last thing said before the result: an invoice voided in Gorelo may still be open in Xero
            lines.append(f"reminder: approved invoice {name}: {VOID_REMINDER}")
        lines.append("result: " + ("PASSED" if self.ok else "FAILED"))
        return lines


def voided_invoices(manifest: Manifest) -> list[str]:
    """The names (DisplayNumber, else Number, else Id) of the invoices of this run that the manifest records as voided.

    An invoice is recorded with status_id 4 (INVOICE_VOID) when the area voided it (mark_voided) and when the cleanup found
    it void (a known residue); only an Approved invoice can be void, so each of these was pushed to the accounting system
    and its void is in Gorelo only (VOID_REMINDER). Read from the manifest, so it holds whichever of the two noticed it."""
    names = []
    for record in manifest.all_created():
        stored = record.details.get("status_id")
        if record.kind != "invoice" or isinstance(stored, bool) or stored != INVOICE_VOID:
            continue
        names.append(
            invoice_name(
                {"DisplayNumber": record.details.get("display_number"), "Number": record.details.get("number"), "Id": record.id}
            )
        )
    return names


def residue_assets(manifest: Manifest) -> list[str]:
    """The ids of the custom assets of this run: this harness never deletes a custom asset (the spec's soft DELETE is not used), so
    each stays on the test client until the operator removes it in the Gorelo app (the cleanup reports each as a known residue,
    CUSTOM_ASSET_RESIDUE)."""
    return [str(record.id) for record in manifest.all_created() if record.kind == "custom_asset"]


def residue_documents(manifest: Manifest) -> list[str]:
    """The ids of the documents of this run: none of them can be deleted through the API, so each stays in Gorelo (archived when the
    run or the cleanup could do it), and the summary says so."""
    return [str(record.id) for record in manifest.all_created() if record.kind == "document"]


def residue_contracts(manifest: Manifest) -> list[str]:
    """The ids of the contracts of this run: none of them can be deleted through the API, so each stays in Gorelo (archived when the run or
    the cleanup could do it), and the summary says so."""
    return [str(record.id) for record in manifest.all_created() if record.kind == "contract"]


def residue_asset_texts(manifest: Manifest) -> dict[str, str]:
    """Where each custom asset of this run may be, in the words of its record: its id -> the outcome the matrix or the cleanup wrote
    last. An outcome ends with CUSTOM_ASSET_RESIDUE_TAIL, so the cleanup keeps it; an asset with no outcome yet reads as the residue
    of its client_id (custom_asset_residue)."""
    return {
        str(record.id): record.outcome or custom_asset_residue(record.details.get("client_id"))
        for record in manifest.all_created()
        if record.kind == "custom_asset"
    }


async def run_matrix(
    *,
    areas: Sequence[str] | None = None,
    skip_email: bool = False,
    leftovers: bool = False,
    with_items: bool = False,
    with_invoices: bool = False,
    with_approved_invoice: bool = False,
    with_custom_assets: bool = False,
    with_custom_properties: bool = False,
    with_documents: bool = False,
    with_asset_move: bool = False,
    with_alert_status: bool = False,
    with_asset_types: bool = False,
    with_client_locations: bool = False,
    with_client_domains: bool = False,
    with_contact_tags=False,  # a bool; written without an annotation because a test forbids one old word followed by a colon in this file
    with_product_charges: bool = False,
    with_invoice_update: bool = False,
    with_contracts: bool = False,
    with_comment_edits: bool = False,
    with_ticket_source: bool = False,
    approve_via_update: bool = False,
    settings: Settings | None = None,
    transport: Any = None,
    pace: float = PACE_SECONDS,
    directory: str | os.PathLike[str] | None = None,
    started: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    lookup_wait: float = LOOKUP_WAIT,
    sync_interval: float = SYNC_INTERVAL,
    sync_polls: int = SYNC_POLLS,
    echo: Callable[[str], None] = emit,
) -> MatrixReport:
    """Run the selected areas, then ALWAYS the cleanup, and print the summary through `echo`.

    areas        area names for --only (None: every area); --skip-email is skip_email.
    leftovers    also delete the leftovers listed in site.local.toml [leftovers] (run_cleanup(leftovers=True)).
    with_items   run the items area (--with-items). Without it the area is skipped with ITEMS_SKIP_REASON, also when
                 it was selected with areas=["items"], and the three item tools cannot be called.
    with_custom_assets run the custom_assets area (--with-custom-assets). Without it the area is skipped with
                 CUSTOM_ASSETS_SKIP_REASON, also when it was selected with areas=["custom_assets"], and the three custom asset
                 tools cannot be called. This harness never deletes the asset it creates (the spec's soft DELETE is not used): the
                 cleanup reports it as a known residue and the summary tells the operator to remove it in the Gorelo app. Not allowed beside
                 with_approved_invoice (that run takes no other flag).
    with_custom_properties run the custom_properties area (--with-custom-properties). Without it the area is skipped with
                 CUSTOM_PROPERTIES_SKIP_REASON, also when it was selected with areas=["custom_properties"], and nothing is sent
                 for it (not even a read). It sets one unset text custom field of the TEST CLIENT itself to the run id and clears it
                 again, so it is the one area behind a guard that allows a PATCH of the test client (allow_custom_properties); it
                 runs only while no custom field of the test client is set, and every other field must read as at the first read
                 after each step; the field is recorded in the manifest before the first write and the cleanup clears it if the
                 run stopped in between. Not allowed beside with_approved_invoice (that run takes no other flag).
    with_asset_move move the custom asset of the custom_assets area to the second client and back (--with-asset-move). It needs
                 with_custom_assets (UsageError otherwise, before anything is created) and builds the guard with
                 allow_custom_asset_move; the move is skipped, with a note, when the second client is not Active or has no location.
    with_documents run the documents area (--with-documents). Without it the area is skipped with DOCUMENTS_SKIP_REASON, also when it
                 was selected with areas=["documents"], and the sixteen document tools cannot be called. It creates two documents of
                 the test client that cannot be deleted through the API: the area archives them, the cleanup archives what a
                 stopped run left, and the summary tells the operator that they stay in Gorelo. Not allowed beside
                 with_approved_invoice (that run takes no other flag).
    with_alert_status, with_asset_types, with_client_locations, with_client_domains, with_contact_tags, with_product_charges,
    with_invoice_update, with_contracts, with_comment_edits, with_ticket_source  the opt-in areas of 1.5.0 (OPT_INS; --with-<area>). Each is
                 skipped with its reason without its flag, also under --only <area>, and its tools cannot be called then; each flag opens
                 its tools and the matching guard area (guard.OPT_IN_AREAS). Not allowed beside with_approved_invoice.
    approve_via_update  with with_approved_invoice only (UsageError otherwise): the approved invoice is a Draft of $1 approved with
                 approve_invoice instead of an invoice created Approved; create_approved_invoice is not called then.
    A run needs the TEST CLIENT Active: right after the check of the client names run_matrix raises guard.ClientNotActive (a
    SiteConfigError: "cannot start", exit status 2) when it is not, before a manifest is made or a request is sent.
    with_invoices run the invoices area (--with-invoices). Without it the area is skipped with INVOICES_SKIP_REASON,
                 also when it was selected with areas=["invoices"], and the six invoice tools cannot be called.
    with_approved_invoice run the approved_invoice area (--with-approved-invoice), which approves one $1 invoice on
                 the test client and voids it. It must be the ONLY area selected (areas=["approved_invoice"]) and the only flag
                 (no with_items, with_invoices, leftovers or skip_email), else UsageError before anything is created; the
                 run may call only APPROVED_INVOICE_TOOLS, behind a guard built with allow_approved_invoice. Without it
                 the area is skipped with APPROVED_INVOICE_SKIP_REASON, also when it was selected, and
                 create_approved_invoice cannot be called. The create call alone runs with the 429 retries of the run's
                 Gorelo client switched off (see no_429_retries; the client is looked up before the first request), so a
                 429 on the one create is a plain rejected create. The cleanup that follows never voids (run_cleanup is
                 not given void_approved: that is the user's command, cleanup --void-approved). Whenever an approved
                 invoice of the run was voided, the summary ends with a reminder that the void is in Gorelo only.
    settings     default live_settings() (reads the API key from the app .env); tests pass their own.
    transport    an httpx transport for every client of the run (tests pass httpx.MockTransport).
    pace         seconds between two requests (0 in tests).
    directory    where the manifest goes (default .live-runs in the repository).
    started      the time the run id is made from (default now); clock times backdated records (default now, UTC).
    lookup_wait  seconds between the retries of get_ticket by number or display number.
    sync_interval, sync_polls  how the approved invoice is polled for its ExternalId: every sync_interval seconds (5),
                 sync_polls times after the first read (24, so at most 120 s); tests pass 0 and a small count.
    Raises UsageError (before anything is created) for a bad area selection, EnvError when there is no API key and
    SetupError when the settings leave out a tool the matrix calls (a toolset or the destructive tools).
    """
    site()  # refuse to start, before anything is created, when the site config is missing or incomplete
    selected = select_areas(areas, skip_email, with_approved_invoice)
    opted = {
        key: given
        for key, given in (
            ("alert_status", with_alert_status), ("asset_types", with_asset_types), ("client_locations", with_client_locations),
            ("client_domains", with_client_domains), ("contact_tags", with_contact_tags), ("product_charges", with_product_charges),
            ("invoice_update", with_invoice_update), ("contracts", with_contracts), ("comment_edits", with_comment_edits),
            ("ticket_source", with_ticket_source),
        )
    }
    enabled = [key for key, given in opted.items() if given]
    if approve_via_update and not with_approved_invoice:
        raise UsageError("--approve-via-update needs --with-approved-invoice: it only changes how that run makes its one approved invoice")
    if with_approved_invoice:  # the run that approves a real invoice does that and nothing else, so no other flag opens anything
        alongside = [
            flag
            for flag, given in (
                ("--with-items", with_items), ("--with-invoices", with_invoices),
                ("--with-custom-assets", with_custom_assets), ("--with-custom-properties", with_custom_properties),
                ("--with-documents", with_documents), ("--with-asset-move", with_asset_move),
                ("--leftovers", leftovers),
                ("--skip-email", skip_email),
                *((OPT_IN_BY_KEY[key].flag, True) for key in enabled),
            )
            if given
        ]
        if alongside:
            raise UsageError(f"--with-approved-invoice runs alone: it cannot be combined with {', '.join(alongside)}")
    if with_asset_move and not with_custom_assets:
        raise UsageError(
            "--with-asset-move needs --with-custom-assets: only the custom_assets area creates the asset that moves to the second client and back"
        )
    chosen = settings if settings is not None else live_settings()
    needed = tools_for(
        with_items, with_invoices, with_approved_invoice, with_custom_assets, with_documents, opt_in=enabled, approve_via_update=approve_via_update
    )
    offered = {spec.name for spec in REGISTRY.select(chosen.toolsets, chosen.destructive, chosen.destructive_tools)}
    if needed - offered:  # a renamed or gated tool would otherwise show up half way through the run
        raise SetupError("the server does not offer the tools the matrix calls: " + ", ".join(sorted(needed - offered)))
    # the ids must be the named clients, before any write; the same reads give the status of both: a write run needs the TEST client Active
    facts = await live_guard.verify_site_clients(chosen.api_key, transport=transport, base_url=chosen.base_url)
    # ClientNotActive (cannot start, exit 2): nothing but the two client reads was sent and no manifest is made
    live_guard.require_active_test_client(facts)
    second = facts.get(SITE.second_client) if isinstance(facts, dict) else None
    manifest = Manifest.start(started, directory=directory, api_host=chosen.api_host)
    echo(f"api host: {chosen.api_host}")
    echo(f"write matrix {manifest.run_id}: manifest {manifest.path}")
    echo("areas: " + ", ".join(area.key for area in selected))
    guard = LiveGuard(
        "write", manifest, require_intents=True, allow_approved_invoice=with_approved_invoice,
        allow_custom_assets=with_custom_assets, allow_custom_properties=with_custom_properties,
        allow_custom_asset_move=with_asset_move, allow_documents=with_documents, base_url=chosen.base_url,
        allow_areas=[OPT_IN_BY_KEY[key].guard_area for key in enabled if OPT_IN_BY_KEY[key].guard_area],
    )
    pacer = Pacer(pace)
    hooks = {"request": [guard, pacer]}
    results: list[AreaResult] = []
    scopes: set[str] = set()
    interrupted: str | None = None
    cleanup_report: CleanupReport | None = None
    cleanup_error: str | None = None
    try:
        with quiet_logs():
            server = build_server(chosen, transport=transport, event_hooks=hooks)
            async with Client(server) as tools:
                # Before the first request: the one create must never be sent twice, so the client whose retries the area
                # switches off around that call has to be reachable, or the run does not start.
                gorelo = gorelo_client_behind(server) if with_approved_invoice else None
                matrix = Matrix(
                    manifest=manifest,
                    guard=guard,
                    pacer=pacer,
                    session=ToolSession(tools, guard, pacer, chosen.api_key, allowed=needed),
                    secret=chosen.api_key,
                    clock=clock if clock is not None else (lambda: datetime.now(timezone.utc)),
                    lookup_wait=lookup_wait,
                    scopes_missing=scopes,
                    with_items=with_items,
                    with_invoices=with_invoices,
                    with_approved_invoice=with_approved_invoice,
                    with_custom_assets=with_custom_assets,
                    with_custom_properties=with_custom_properties,
                    with_documents=with_documents,
                    with_asset_move=with_asset_move,
                    second_client_active=second is not None and second.active,
                    opt_in=enabled,
                    approve_via_update=approve_via_update,
                    sync_interval=sync_interval,
                    sync_polls=sync_polls,
                    gorelo=gorelo,
                )
                stopped = False
                for area in selected:
                    if stopped:
                        results.append(AreaResult(area.key, "not run", "the run stopped after a guard refusal"))
                        continue
                    result = await _run_area(matrix, area)
                    results.append(result)
                    stopped = result.tripped
                    progress = f"area {result.area}: {result.status}, {result.requests} requests"
                    echo(scrub(progress + (f" ({result.detail})" if result.detail else ""), chosen.api_key))
    except BaseException as exc:
        interrupted = type(exc).__name__
        raise
    finally:
        try:
            cleanup_report = await run_cleanup(
                manifest, leftovers=leftovers, settings=chosen, transport=transport, pace=pace, echo=echo,
                verify_clients=False,  # checked above, before the first write
            )
        except Exception as exc:
            cleanup_error = scrub(" ".join(f"{type(exc).__name__}: {exc}".split()), chosen.api_key)[:DETAIL_LIMIT]
        counts = manifest.summary()
        undeletable = len(cleanup_report.undeletable) if cleanup_report is not None else 0
        counts["leftovers"] -= undeletable  # known undeletable files are not records anybody can still clean up
        counts["undeletable"] = undeletable
        report = MatrixReport(
            run_id=manifest.run_id,
            manifest_path=str(manifest.path),
            results=results,
            summary=counts,
            cleanup=cleanup_report,
            cleanup_error=cleanup_error,
            scopes_missing=sorted(scopes),
            requests=pacer.requests,
            refused=len(guard.violations),
            interrupted=interrupted,
            voided=voided_invoices(manifest),  # after the cleanup: it may have found an invoice void too
            assets=residue_assets(manifest),
            asset_texts=residue_asset_texts(manifest),
            documents=residue_documents(manifest),
            contracts=residue_contracts(manifest),
        )
        for line in report.render():
            echo(scrub(line, chosen.api_key))
    return report


def parse_only(text: str | None) -> list[str] | None:
    return None if text is None else text.split(",")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.live.write_matrix",
        description="Run the live write matrix against the test client and the second client, then clean up what it created.",
    )
    parser.add_argument("--skip-email", action="store_true", help="leave out the email area (the second client, the operator contact)")
    parser.add_argument(
        "--only",
        metavar="AREA,...",
        help="run only these areas, in the matrix order: " + ", ".join(area.key for area in AREAS)
        + ". 'forms' is another name for 'projects': the Forms scope is only probed there, and the area WRITES a project, a "
        "section, a task and a private task comment on the test client (the project-created notification of the app may fire)",
    )
    parser.add_argument(
        "--leftovers",
        action="store_true",
        help="also delete the leftovers listed in site.local.toml [leftovers] (the leftover clients and contact) after checking their names",
    )
    parser.add_argument(
        "--with-items",
        action="store_true",
        help="also run the items area (creates, updates and deletes a catalog product on the test client); without it the "
        "area is skipped, also under --only items",
    )
    parser.add_argument(
        "--with-custom-assets",
        action="store_true",
        help="also run the custom_assets area (creates and updates ONE custom asset on the test client; this harness never deletes "
        "a custom asset (the spec's soft DELETE is not used), so it stays there until removed in the Gorelo app); without it the area "
        "is skipped, also under --only custom_assets",
    )
    parser.add_argument(
        "--with-asset-move",
        action="store_true",
        help="also MOVE the custom asset of the custom_assets area to the second client and back (only together with the custom "
        "assets flag; the second client must be Active and have a location; the manifest names the second client before the move "
        "is sent)",
    )
    parser.add_argument(
        "--with-documents",
        action="store_true",
        help="also run the documents area (creates two documents on the test client, which CANNOT be deleted through the API: the "
        "run archives them and they stay in Gorelo until removed in the Gorelo app; sharing is never turned on); without it the "
        "area is skipped, also under --only documents",
    )
    parser.add_argument(
        "--with-custom-properties",
        action="store_true",
        help="also run the custom_properties area (sets one unset text custom field of the test client ITSELF to the run id and "
        "clears it again: the one write the live guard allows on that client; it runs only while no custom field of that client "
        "is set; the field is recorded before the first write and the cleanup clears it if the run stops in between); without "
        "it the area is skipped, also under --only custom_properties",
    )
    parser.add_argument(
        "--with-invoices",
        action="store_true",
        help="also run the invoices area (creates and deletes a Draft invoice on the test client, which uses up one invoice "
        "number); without it the area is skipped, also under --only invoices",
    )
    parser.add_argument(
        "--with-approved-invoice",
        action="store_true",
        help="run the approved_invoice area, ALONE (needs --only approved_invoice and no other flag): it approves a "
        "$1 invoice on the test client, which pushes it to the connected accounting system, then voids it (a voided invoice "
        "stays listed as Void); the area is skipped before its first request until UNEMAILABLE_CONTACT_STATUS_IDS names the "
        "contact statuses that cannot be emailed (empty since 1.3.0, so the gate is closed); without the flag the area is skipped, "
        "also under --only approved_invoice",
    )
    parser.add_argument(
        "--approve-via-update",
        action="store_true",
        help="with --with-approved-invoice only: make the one approved invoice by creating a Draft of $1 and approving it with "
        "approve_invoice (a PATCH of StatusId 5) instead of creating it Approved. It pushes the "
        "invoice to the connected accounting system like the create does; the void is in Gorelo only, so void the accounting system's copy by hand",
    )
    for area in OPT_INS:
        parser.add_argument(
            area.flag,
            action="store_true",
            help=f"also run the {area.key} area ({area.what}); without it the area is skipped, also under --only {area.key}. "
            "It needs the test client Active, like every write run",
        )
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(
            run_matrix(
                areas=parse_only(args.only),
                skip_email=args.skip_email,
                leftovers=args.leftovers,
                with_items=args.with_items,
                with_invoices=args.with_invoices,
                with_approved_invoice=args.with_approved_invoice,
                with_custom_assets=args.with_custom_assets,
                with_custom_properties=args.with_custom_properties,
                with_documents=args.with_documents,
                with_asset_move=args.with_asset_move,
                approve_via_update=args.approve_via_update,
                **{f"with_{area.key}": getattr(args, f"with_{area.key}") for area in OPT_INS},
            )
        )
    except UsageError as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return 2
    except (EnvError, SetupError, SiteConfigError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # the cleanup and the summary already ran, in run_matrix's finally
        print("interrupted", file=sys.stderr)
        return 130
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
