WHY WE DID IT THIS WAY

These notes explain the main decisions behind this Gorelo MCP server, in plain language, for MSP
technicians and developers. Each note has four parts: WHAT we did, WHY, WHAT IT COSTS, and WHERE IN
THE CODE. File and function names are given instead of line numbers, so they stay true as the code
moves. The notes describe the code at the time of writing; if they disagree with the code, the code
wins and the note needs fixing.

Index

01-self-hosted-not-cloud.txt
    Why you run this server yourself instead of using a hosted service.
02-spec-index-and-request-validation.txt
    Why every request is checked against an index of the live Gorelo OpenAPI spec before it is sent.
03-forbidden-and-gated-operations.txt
    Which operations can never be called, and which are off until you switch them on and confirm.
04-private-comments-by-default.txt
    Why a ticket comment is Private unless you say otherwise, and who gets emailed when it is not.
05-built-in-oauth-one-password.txt
    Why the server has its own OAuth sign-in with one password, and the outage that shaped it.
06-api-watcher.txt
    Why a daily job compares Gorelo's changelog and API spec with what this server was built on.
07-live-test-harness.txt
    How live tests against a real Gorelo tenant are fenced in so they cannot touch real client data.
08-server-hardening.txt
    The server and service settings that limit the damage if something goes wrong.
09-tool-design-for-ai.txt
    How the tools are shaped so an AI model uses them correctly: paging, priority, dates, errors.
10-toolsets.txt
    Why projects and forms are off by default.

Related documents

../API-OBSERVED-BEHAVIOR.md   Where production Gorelo behavior differed from the published spec, with dates.
../../SECURITY.md             How to report a vulnerability and what the threat model is.
../../CONTRIBUTING.md         How to run the tests and propose a change.
