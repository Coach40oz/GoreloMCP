"""The client-name check of the live harness: ids of the site config must be the clients it names, before any write.

Offline: an httpx.MockTransport plays Gorelo. Every entry point that can write (write_matrix, cleanup, probes) and the
read-only smoke run refuse with exit 2 ("cannot start: client <id> is named X, site config says Y") on a mismatch,
and an unedited example config is refused before a single request is made.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import TEST_API_KEY
from site_helper import REAL_VERIFY, site_config_env  # noqa: F401  (autouse: invented ids; the name check is a no-op)

from scripts import site_config
from scripts.live import _env, cleanup, probes_20261001, smoke, write_matrix
from scripts.live import guard as guard_module
from scripts.live.manifest import Manifest
from scripts.site_config import SiteConfigError
from test_live_write_matrix import FakeGorelo

pytestmark = pytest.mark.anyio

NAMES = {9501: "Sandbox Alpha", 9502: "Sandbox Beta"}


def gorelo(names, requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        client_id = int(request.url.path.rsplit("/", 1)[1])
        if client_id not in names:
            return httpx.Response(404, json={"IsSuccess": False})
        return httpx.Response(200, json={"IsSuccess": True, "Data": {"Id": client_id, "Name": names[client_id]}})

    return httpx.MockTransport(handler)


async def test_matching_names_pass_after_one_read_per_client():
    seen = []
    await REAL_VERIFY(TEST_API_KEY, transport=gorelo(NAMES, seen))
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/clients/9501"), ("GET", "/v1/clients/9502")]
    assert all(r.headers["X-API-Key"] == TEST_API_KEY for r in seen)


@pytest.mark.parametrize("wrong", [9501, 9502])
async def test_a_differing_name_stops_with_both_names(wrong):
    names = {**NAMES, wrong: "Somebody Else Ltd"}
    with pytest.raises(SiteConfigError, match=rf"client {wrong} is named Somebody Else Ltd, site config says {NAMES[wrong]}"):
        await REAL_VERIFY(TEST_API_KEY, transport=gorelo(names, []))


async def test_the_name_must_match_exactly():
    for name in ("sandbox alpha", "Sandbox Alpha ", "Sandbox Alpha 2"):
        with pytest.raises(SiteConfigError, match="site config says Sandbox Alpha"):
            await REAL_VERIFY(TEST_API_KEY, transport=gorelo({**NAMES, 9501: name}, []))


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_a_client_that_cannot_be_read_stops_the_run(status):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={}))
    with pytest.raises(SiteConfigError, match=rf"cannot check client 9501: Gorelo answered HTTP {status}"):
        await REAL_VERIFY(TEST_API_KEY, transport=transport)


async def test_a_body_without_a_name_stops_the_run():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"Data": {"Id": 9501}}))
    with pytest.raises(SiteConfigError, match="client 9501 is named \\(no name\\)"):
        await REAL_VERIFY(TEST_API_KEY, transport=transport)


def test_the_example_config_is_refused_before_any_request(monkeypatch):
    monkeypatch.setenv(site_config.ENV_VAR, str(site_config.REPO_ROOT / "site.example.toml"))
    seen = []
    import asyncio

    with pytest.raises(SiteConfigError, match="example file"):
        asyncio.run(REAL_VERIFY(TEST_API_KEY, transport=gorelo(NAMES, seen)))
    assert seen == []


# --- the entry points ------------------------------------------------------------------------------


async def test_the_write_matrix_stops_on_a_mismatch_with_no_write_and_no_manifest(make_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(write_matrix.live_guard, "verify_site_clients", REAL_VERIFY)
    seen = []
    with pytest.raises(SiteConfigError, match="client 9502 is named Elsewhere, site config says Sandbox Beta"):
        await write_matrix.run_matrix(
            settings=make_settings(destructive=True), transport=gorelo({**NAMES, 9502: "Elsewhere"}, seen),
            pace=0, directory=tmp_path / "runs", echo=lambda line: None,
        )
    assert {r.method for r in seen} == {"GET"} and not (tmp_path / "runs").exists()


def test_the_write_matrix_main_exits_2_on_a_refusal(monkeypatch, capsys):
    async def refuse(**options):
        raise SiteConfigError("client 9501 is named Elsewhere, site config says Sandbox Alpha")

    monkeypatch.setattr(write_matrix, "run_matrix", refuse)
    assert write_matrix.main([]) == 2
    assert "cannot start: client 9501 is named Elsewhere, site config says Sandbox Alpha" in capsys.readouterr().err


async def test_cleanup_stops_on_a_mismatch_before_any_other_request(make_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup.live_guard, "verify_site_clients", REAL_VERIFY)
    manifest = Manifest("MCPTEST-20990101000000", tmp_path / "run.json")
    seen = []
    with pytest.raises(SiteConfigError, match="client 9501 is named Elsewhere"):
        await cleanup.run_cleanup(
            manifest, settings=make_settings(destructive=True), transport=gorelo({**NAMES, 9501: "Elsewhere"}, seen),
            pace=0, echo=lambda line: None,
        )
    assert [r.url.path for r in seen] == ["/v1/clients/9501"]


async def test_cleanup_with_matching_names_goes_on(make_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup.live_guard, "verify_site_clients", REAL_VERIFY)
    manifest = Manifest("MCPTEST-20990101000000", tmp_path / "run.json")
    seen = []
    report = await cleanup.run_cleanup(
        manifest, settings=make_settings(destructive=True), transport=gorelo(NAMES, seen), pace=0, echo=lambda line: None
    )
    assert report.ok and [r.url.path for r in seen] == ["/v1/clients/9501", "/v1/clients/9502"]


async def test_smoke_stops_on_a_mismatch_with_only_the_name_read_sent(make_settings, monkeypatch):
    monkeypatch.setattr(smoke.live_guard, "verify_site_clients", REAL_VERIFY)
    seen = []
    with pytest.raises(SiteConfigError, match="client 9502 is named Elsewhere, site config says Sandbox Beta"):
        await smoke.run_smoke(
            settings=make_settings(), transport=gorelo({**NAMES, 9502: "Elsewhere"}, seen), pace=0, echo=lambda line: None
        )
    assert [r.url.path for r in seen] == ["/v1/clients/9501", "/v1/clients/9502"]


def missing_config(monkeypatch, tmp_path):
    monkeypatch.setenv(site_config.ENV_VAR, str(tmp_path / "missing.toml"))

    def never(*args, **kwargs):
        raise AssertionError("the API key was read although the site config is missing")

    monkeypatch.setattr(_env, "ENV_FILE", tmp_path / "no-such-dir" / ".env")
    monkeypatch.setattr(smoke, "live_settings", never)
    monkeypatch.setattr(cleanup, "live_settings", never)

    def no_network(request):
        raise AssertionError("a request was sent although the site config is missing")

    real = httpx.AsyncClient.__init__
    monkeypatch.setattr(
        httpx.AsyncClient, "__init__", lambda self, *a, **kw: real(self, *a, **{**kw, "transport": httpx.MockTransport(no_network)})
    )


def test_smoke_main_without_a_site_config_exits_2_and_sends_nothing(monkeypatch, tmp_path, capsys):
    missing_config(monkeypatch, tmp_path)
    assert smoke.main([]) == 2
    err = capsys.readouterr().err
    assert "cannot start" in err and "site.example.toml" in err


def test_cleanup_main_without_a_site_config_exits_2_and_sends_nothing(monkeypatch, tmp_path, capsys):
    path = tmp_path / "runs" / "MCPTEST-20990101000000.json"
    run = Manifest("MCPTEST-20990101000000", path)
    run.created("contact", 9990, run.label("k"))
    missing_config(monkeypatch, tmp_path)
    assert cleanup.main([str(path)]) == 2
    err = capsys.readouterr().err
    assert "cannot start" in err and "site.example.toml" in err


# --- probes_20261001 -------------------------------------------------------------------------------


def test_probes_without_a_site_config_print_to_stderr_and_exit_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(site_config.ENV_VAR, str(tmp_path / "missing.toml"))
    assert probes_20261001.start() == 2
    out = capsys.readouterr()
    assert "cannot start" in out.err and "site.example.toml" in out.err and out.out == ""


def test_probes_stop_on_a_name_mismatch(monkeypatch, tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("GORELO_API_KEY=k\n", encoding="utf-8")
    monkeypatch.setattr(probes_20261001, "ENV_FILE", env)
    seen = []
    real = httpx.Client.__init__
    monkeypatch.setattr(
        httpx.Client, "__init__", lambda self, *a, **kw: real(self, *a, **{**kw, "transport": gorelo({**NAMES, 9502: "Elsewhere"}, seen)})
    )
    assert probes_20261001.start() == 2
    assert "cannot start: client 9502 is named Elsewhere, site config says Sandbox Beta" in capsys.readouterr().err
    assert {r.method for r in seen} == {"GET"}


def test_probes_write_only_to_the_exact_test_client_or_records_they_created(monkeypatch):
    monkeypatch.setattr(probes_20261001, "TEST_CLIENT", 9501)
    monkeypatch.setattr(probes_20261001, "CREATED", {"contacts": [77], "tickets": [88]})
    allowed = probes_20261001.write_allowed
    assert allowed("GET", "/clients/9502", None)
    assert allowed("POST", "/tickets", {"ClientId": 9501}) and allowed("POST", "/alerts", {"ClientId": 9501})
    assert allowed("PATCH", "/clients", {"Id": 9501}) and allowed("DELETE", "/tickets/88", None)
    assert allowed("PATCH", "/contacts", {"ContactId": 77, "ClientId": 9501}) and allowed("DELETE", "/contacts/77", None)
    assert not allowed("POST", "/tickets", {"ClientId": 95010}) and not allowed("POST", "/tickets", {})
    assert not allowed("POST", "/tickets", {"ClientId": 9502}) and not allowed("PATCH", "/clients", {"Id": 9502})
    assert not allowed("DELETE", "/tickets/89", None) and not allowed("DELETE", "/clients/9501", None)
    assert not allowed("PATCH", "/contacts", {"ContactId": 78, "ClientId": 9501})


# --- the status of the clients: a write run needs the test client Active (contract a7954b24844b: 1 Active, 3 Credit hold, 4 Archived) ---


def gorelo_with_status(statuses, requests):
    """Like gorelo(): every configured client exists under its name; `statuses` maps a client id to its Status object (or DROP)."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        client_id = int(request.url.path.rsplit("/", 1)[1])
        data = {"Id": client_id, "Name": NAMES[client_id]}
        status = statuses.get(client_id, {"Id": 1, "Name": "Active"})
        if status is not DROP:
            data["Status"] = status
        return httpx.Response(200, json={"IsSuccess": True, "Data": data})

    return httpx.MockTransport(handler)


DROP = object()
ARCHIVED = {"Id": 4, "Name": "Archived"}


async def test_the_check_returns_the_status_of_each_client_from_the_same_reads():
    seen = []
    facts = await REAL_VERIFY(TEST_API_KEY, transport=gorelo_with_status({9501: ARCHIVED}, seen))
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/clients/9501"), ("GET", "/v1/clients/9502")]  # no extra request
    assert facts[9501] == guard_module.ClientFacts(4, "Archived") and not facts[9501].active
    assert facts[9502] == guard_module.ClientFacts(1, "Active") and facts[9502].active


@pytest.mark.parametrize(
    "status, expected",
    [
        (DROP, guard_module.ClientFacts(None, None)),
        (None, guard_module.ClientFacts(None, None)),
        ("Active", guard_module.ClientFacts(None, None)),
        ({"Id": "1", "Name": "Active"}, guard_module.ClientFacts(None, "Active")),
        ({"Id": True, "Name": "Active"}, guard_module.ClientFacts(None, "Active")),
        ({"Id": 3, "Name": 5}, guard_module.ClientFacts(3, None)),
        ({"Id": 3, "Name": "Credit hold"}, guard_module.ClientFacts(3, "Credit hold")),
    ],
)
async def test_a_status_that_cannot_be_read_is_unknown_and_never_active(status, expected):
    facts = await REAL_VERIFY(TEST_API_KEY, transport=gorelo_with_status({9501: status}, []))
    assert facts[9501] == expected and not facts[9501].active


def test_the_test_client_must_be_active_by_its_status_id():
    cfg = site_config.load()
    active, archived = guard_module.ClientFacts(1, "Active"), guard_module.ClientFacts(4, "Archived")
    guard_module.require_active_test_client({9501: active, 9502: archived}, cfg)  # only the TEST client counts here
    for facts, said in (
        ({9501: archived}, "the test client 9501 is Archived (status 4), so a write run did not start and nothing but the two client reads was sent"),
        ({9501: guard_module.ClientFacts(3, "Credit hold")}, "is Credit hold (status 3)"),
        ({9501: guard_module.ClientFacts(None, None)}, "is of a status that could not be read"),
        ({9502: active}, "the status of the test client 9501 was not read"),
        ({}, "the status of the test client 9501 was not read"),
        (None, "the status of the test client 9501 was not read"),
        ({9501: guard_module.ClientFacts(1, "Archived")}, None),  # the id decides, never the name
        ({9501: guard_module.ClientFacts(4, "Active")}, "is Active (status 4)"),
    ):
        if said is None:
            guard_module.require_active_test_client(facts, cfg)
            continue
        with pytest.raises(guard_module.ClientNotActive) as refused:
            guard_module.require_active_test_client(facts, cfg)
        assert said in str(refused.value)
        assert isinstance(refused.value, SiteConfigError)  # every entry point that prints "cannot start" and exits 2 handles it


def test_the_refusal_tells_the_operator_what_to_do_and_that_the_harness_never_changes_a_client():
    with pytest.raises(guard_module.ClientNotActive) as refused:
        guard_module.require_active_test_client({9501: guard_module.ClientFacts(4, "Archived")}, site_config.load())
    text = str(refused.value)
    assert "The operator has to make the test client Active again in the Gorelo app" in text
    assert "this harness never changes the status of a client itself" in text and "needs the test client Active (status 1)" in text
    assert "Sandbox" not in text  # the name of the client is not repeated


async def test_the_write_matrix_stops_on_an_archived_test_client_with_nothing_sent_and_no_manifest(make_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(write_matrix.live_guard, "verify_site_clients", REAL_VERIFY)
    seen = []
    with pytest.raises(guard_module.ClientNotActive, match="the test client 9501 is Archived"):
        await write_matrix.run_matrix(
            settings=make_settings(destructive=True), transport=gorelo_with_status({9501: ARCHIVED}, seen),
            pace=0, directory=tmp_path / "runs", echo=lambda line: None,
        )
    assert [r.method for r in seen] == ["GET", "GET"] and not (tmp_path / "runs").exists()  # only the two reads of the check


async def test_every_area_selection_needs_the_active_test_client_not_only_the_ones_that_name_it(make_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(write_matrix.live_guard, "verify_site_clients", REAL_VERIFY)
    for only in (["clients"], ["email"], ["documents"], ["custom_assets"]):
        with pytest.raises(guard_module.ClientNotActive):
            await write_matrix.run_matrix(
                settings=make_settings(destructive=True), transport=gorelo_with_status({9501: ARCHIVED}, []), pace=0,
                directory=tmp_path / "runs", echo=lambda line: None, areas=only, with_documents=True, with_custom_assets=True,
            )


def test_the_write_matrix_main_exits_2_with_cannot_start_for_an_archived_test_client(monkeypatch, capsys):
    async def refuse(**options):
        raise guard_module.ClientNotActive("the test client 9501 is Archived (status 4), so a write run did not start")

    monkeypatch.setattr(write_matrix, "run_matrix", refuse)
    assert write_matrix.main([]) == 2
    assert "cannot start: the test client 9501 is Archived (status 4)" in capsys.readouterr().err


async def test_the_cleanup_does_not_refuse_an_archived_test_client_it_has_to_clean_up_what_a_run_left(make_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup.live_guard, "verify_site_clients", REAL_VERIFY)
    manifest = Manifest("MCPTEST-20990101000000", tmp_path / "run.json")
    seen = []
    report = await cleanup.run_cleanup(
        manifest, settings=make_settings(destructive=True), transport=gorelo_with_status({9501: ARCHIVED, 9502: ARCHIVED}, seen),
        pace=0, echo=lambda line: None,
    )
    assert report.ok and [r.url.path for r in seen] == ["/v1/clients/9501", "/v1/clients/9502"]


async def test_the_matrix_reads_a_second_client_that_is_not_active_as_no_move_not_as_a_refusal(make_settings, tmp_path, monkeypatch):
    import site_helper

    site_helper.CLIENT_STATUS[9502] = (4, "Archived")  # the stub of the check reports the second client archived
    fake = FakeGorelo()
    report = await write_matrix.run_matrix(
        settings=make_settings(destructive=True), transport=fake.mock.transport, pace=0, directory=tmp_path / "runs",
        echo=lambda line: None, areas=["clients"], lookup_wait=0,
    )
    assert report.ok  # a second client that is not Active stops nothing by itself: only the asset move needs it (see test_live_custom_assets)
