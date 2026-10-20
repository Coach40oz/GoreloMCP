"""Live test harness for the Gorelo MCP server (the test client and the second client only).

Safety infrastructure, built first and reviewed before anything runs live:

    _env.py      the API key (read from the app .env at run time, never printed), live Settings, request pacer
    manifest.py  the run manifest: every record a run creates, written before and after the create call
    guard.py     an httpx request hook that refuses, BEFORE anything is sent, every request outside the allowlist
    cleanup.py   deletes what a manifest says a run created (also the leftovers listed in site.local.toml [leftovers], with --leftovers;
                 with --void-approved it also voids an Approved invoice the run recorded as Approved: without that
                 flag a cleanup never voids, and only the write matrix's approved_invoice area voids the one invoice it
                 approved itself); it also clears the custom field value that the custom_properties area (--with-custom-properties)
                 set on the test client, but only while the field still shows the run's text and no other custom field of
                 that client is set; it also archives a document that a stopped documents area (--with-documents) left Active
                 (a document cannot be deleted through the API, so it is a known residue)

How a harness wires them together (the same guard goes on every client of the run):

    settings = live_settings()                                    # GORELO_BASE_URL decides the host the guard allows
    facts = await verify_site_clients(settings.api_key, base_url=settings.base_url)    # the name and the status of each client
    require_active_test_client(facts)                             # a write run starts only while the test client is Active
    manifest = Manifest.start(api_host=settings.api_host)         # .live-runs/MCPTEST-<time>.json
    guard = LiveGuard("write", manifest, require_intents=True, base_url=settings.base_url)    # "read" and no manifest for a read-only run
    server = build_server(settings, event_hooks={"request": [guard, Pacer(1.0)]})
    try:
        async with Client(server) as tools:
            label = manifest.label("plain ticket")
            manifest.intent("ticket", label, {"client_id": TEST_CLIENT})            # BEFORE the create
            made = (await tools.call_tool("create_ticket", {...})).structured_content   # the ticket as stored
            manifest.created("ticket", made["Id"], label, {"client_id": TEST_CLIENT})
            # who the STORED ticket emails (what it read back with, not what was asked for): the guard's
            # public-comment rule reads it, and a ticket without it gets no public comment
            manifest.update_details("ticket", made["Id"], contact_id=made["ContactId"], cc_contact_ids=made["CcContactIds"])
            guard.assert_clean()                                             # after every step
    finally:
        await run_cleanup(manifest)                       # or: python -m scripts.live.cleanup <file> [--void-approved]

See docs/why/07-live-test-harness.txt for the design and each module's docstring for the rules.
"""
