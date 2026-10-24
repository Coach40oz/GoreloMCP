"""gorelo_client.py: the token bucket, the back-off every request shares after a 429, and the one retry of a read.

The bucket reads a clock and waits through an injected sleep, so these tests run on VIRTUAL time. VirtualTime is a clock
and a sleep that move together, plus a driver (`run`) that lets every task go as far as it can and only then moves the
clock to the next wake-up. Nothing sleeps for real, no socket is opened, and tasks run in the same order every time.
Gateway is the fake Gorelo: it notes the virtual time each request reached it and answers from a script.

Facts measured live on 2026-10-24 that the scenarios copy: Gorelo limits the whole API key (a burst of about 6, then about
1 request per second), a 429 is gateway JSON with the content type application/octet-stream and a `Retry-After: 1`
header, and reads were seen to fail with a gateway answer that is not an envelope (503 text/plain of 114 bytes,
502 text/html).
"""

import asyncio
import heapq
import inspect
import itertools
import logging
import time

import httpx
import pytest
from conftest import (
    ORIGINAL_CLIENT_SLEEP,
    TEST_API_KEY,
    TEST_TRACE_ID,
    envelope,
    error_envelope,
    list_tools,
    paged_envelope,
    uid,
)
from fastmcp import Client

import gorelo_client
from gorelo_client import EXPORT_NOTE, GATEWAY_RETRY_STATUSES, GATEWAY_RETRY_WAIT, GoreloAPIError, GoreloClient

pytestmark = pytest.mark.anyio

CLIENT_OP = "GET /v1/clients/{clientId}"
CREATE_CLIENT = "POST /v1/clients"
PDF_OP = "GET /v1/invoices/{invoiceId}/pdf"
DOCUMENT_PDF_OP = "GET /v1/documents/{documentId}/pdf"
TIME_ENTRY_PATCH = "PATCH /v1/time-entries/{timeEntryId}"
TIME_ENTRY_DELETE = "DELETE /v1/time-entries/{timeEntryId}"
SECRET = "SECRET-VALUE-456"


# --------------------------------------------------------------------------
# Virtual time and a fake Gorelo
# --------------------------------------------------------------------------


class VirtualTime:
    """A clock and a sleep that move together, and the driver that runs a scenario on them.

    `sleep` is what the client under test is given: it notes the delay it was asked for in `delays` and waits until the
    clock has moved on by that much. `elapse` is the same wait for the fake Gorelo (work that takes time) and is not
    noted. `run` starts the coroutines as tasks and, whenever none of them can go on, moves the clock to the earliest
    wake-up, so a scenario takes no real time. Sleepers with the same wake-up time are woken in the order they slept.
    """

    def __init__(self, start=1000.0):
        self.start = float(start)
        self.now = float(start)
        self.delays = []
        self._sleepers = []
        self._order = itertools.count()

    def clock(self):
        return self.now

    async def sleep(self, delay):
        self.delays.append(delay)
        await self.elapse(delay)

    async def elapse(self, seconds):
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.now + max(0.0, seconds), next(self._order), future))
        await future

    async def _settle(self):
        """Yield to the loop until no other task has anything left to run right now."""
        loop = asyncio.get_running_loop()
        ready = getattr(loop, "_ready", None)  # the callbacks that are due; empty means every task waits for something
        for _ in range(500):
            await asyncio.sleep(0)
            if ready is not None and not ready:
                return

    def _wake_next(self):
        while self._sleepers:
            wake, _, future = heapq.heappop(self._sleepers)
            if future.done():  # its task was cancelled: time does not move for it
                continue
            self.now = max(self.now, wake)
            future.set_result(None)
            return True
        return False

    async def run(self, *awaitables):
        """Run the coroutines or tasks to the end on virtual time; returns the tasks. A failure is raised."""
        tasks = [asyncio.ensure_future(item) for item in awaitables]
        try:
            while not all(task.done() for task in tasks):
                await self._settle()
                if all(task.done() for task in tasks):
                    break
                if not self._wake_next():
                    raise AssertionError("deadlock: no task can go on and none is waiting for time to pass")
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for task in tasks:
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()
        return tasks


class Gateway:
    """The fake Gorelo of these tests. It notes the virtual time each request arrived and answers from a script.

    on(method, path, *answers, work=0.0): the answers are served one per request and the last one for every request after
    that. An answer is an httpx.Response (a template, copied for each request) or a function that returns a fresh
    response or exception. `work` is how long Gorelo takes to answer, in virtual seconds. A request for a (method, path)
    with no script gets a successful envelope with `{"Id": 1}`.
    """

    MAX_ARRIVALS = 200  # a client that never stops asking fails the test instead of looping for ever

    def __init__(self, vt):
        self.vt = vt
        self.arrivals = []  # (virtual time, method, path)
        self.in_flight = 0
        self.peak = 0
        self._script = {}
        self._work = {}

    def on(self, method, path, *answers, work=0.0):
        self._script[(method, path)] = list(answers)
        self._work[(method, path)] = work

    async def __call__(self, request):
        key = (request.method, request.url.path)
        self.arrivals.append((self.vt.now, *key))
        assert len(self.arrivals) <= self.MAX_ARRIVALS, "the client keeps sending the same request"
        answers = self._script.get(key) or [ok({"Id": 1})]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self._work.get(key):
                await self.vt.elapse(self._work[key])
        finally:
            self.in_flight -= 1
        if callable(answer):
            answer = answer()
        if isinstance(answer, BaseException):
            if isinstance(answer, httpx.RequestError):
                answer.request = request
            raise answer
        return httpx.Response(answer.status_code, headers=dict(answer.headers), content=answer.content)

    def offsets(self):
        """When each request arrived, in seconds after the clock started, in arrival order."""
        return [when - self.vt.start for when, _, _ in self.arrivals]

    def paths(self):
        return [path for _, _, path in self.arrivals]


def ok(data=None):
    return httpx.Response(200, json=envelope(data))


def too_many(retry_after=1):
    """Gorelo's 429 as the gateway sends it: JSON that is not an envelope, with the content type octet-stream."""
    body = b'{"error":"rate_limited","message":"Too many requests","retry_after":1,"timestamp":"2026-10-24T00:00:00Z"}'
    return httpx.Response(
        429, content=body, headers={"content-type": "application/octet-stream", "Retry-After": str(retry_after)}
    )


def gateway_page(status):
    """The gateway failures seen on reads: no envelope, a text or an HTML page."""
    if status == 502:
        return httpx.Response(502, content=b"<html>Bad Gateway</html>", headers={"content-type": "text/html"})
    return httpx.Response(status, content=(b"upstream request timeout " * 5)[:114], headers={"content-type": "text/plain"})


def get_client_record(client, number):
    return client.get_one(CLIENT_OP, path_params={"clientId": number}, tool="get_client")


def client_path(number):
    return f"/v1/clients/{number}"


def info_lines(caplog):
    """The per-request INFO lines of the client."""
    return [r.getMessage() for r in caplog.records if r.name == "gorelo_client" and r.levelno == logging.INFO]


def warning_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "gorelo_client" and r.levelno == logging.WARNING]


@pytest.fixture
def vt():
    return VirtualTime()


@pytest.fixture
def gateway(vt):
    return Gateway(vt)


@pytest.fixture
def new_client(vt, gateway, spec_index):
    """new_client(**GoreloClient kwargs): a client on the fake Gorelo, with the virtual clock and sleep unless given."""

    def factory(**kwargs):
        kwargs.setdefault("sleep", vt.sleep)
        kwargs.setdefault("clock", vt.clock)
        return GoreloClient(TEST_API_KEY, spec=spec_index, transport=httpx.MockTransport(gateway), **kwargs)

    return factory


# --------------------------------------------------------------------------
# The bucket
# --------------------------------------------------------------------------


async def test_the_first_five_requests_go_out_with_no_wait_and_the_sixth_waits_one_second(vt, gateway, new_client):
    async with new_client() as client:

        async def one_after_another():
            for number in range(1, 9):
                await get_client_record(client, number)

        await vt.run(one_after_another())
    assert gateway.offsets() == [0, 0, 0, 0, 0, 1.0, 2.0, 3.0]
    assert vt.delays == [1.0, 1.0, 1.0]


async def test_the_bucket_refills_while_the_client_is_idle_but_never_holds_more_than_the_burst(vt, gateway, new_client):
    async with new_client() as client:

        async def scenario():
            for number in range(1, 6):  # five at once empty the bucket
                await get_client_record(client, number)
            await vt.elapse(2.0)  # two idle seconds: two tokens back
            for number in range(6, 9):
                await get_client_record(client, number)
            await vt.elapse(100.0)  # a long pause fills the bucket again, to five and not to a hundred
            for number in range(9, 16):
                await get_client_record(client, number)

        await vt.run(scenario())
    assert gateway.offsets() == [0] * 5 + [2.0, 2.0, 3.0] + [103.0] * 5 + [104.0, 105.0]


async def test_the_rate_and_the_burst_are_the_constructor_arguments(vt, gateway, new_client):
    async with new_client(rate_per_second=2.0, burst=2) as client:

        async def scenario():
            for number in range(1, 6):
                await get_client_record(client, number)

        await vt.run(scenario())
    assert gateway.offsets() == [0, 0, 0.5, 1.0, 1.5]
    assert vt.delays == [0.5, 0.5, 0.5]


async def test_a_slow_rate_waits_for_the_fraction_of_a_token(vt, gateway, new_client):
    async with new_client(rate_per_second=0.25, burst=1) as client:  # one request every four seconds

        async def scenario():
            await get_client_record(client, 1)
            await vt.elapse(1.0)
            await get_client_record(client, 2)
            await get_client_record(client, 3)

        await vt.run(scenario())
    assert gateway.offsets() == [0, 4.0, 8.0]
    assert vt.delays == [3.0, 4.0]


async def test_the_bucket_is_the_whole_key_not_one_per_endpoint(vt, gateway, new_client):
    gateway.on("GET", "/v1/tickets", httpx.Response(200, json=paged_envelope([])))
    gateway.on("PATCH", "/v1/time-entries/55", ok({"Id": 55}))
    gateway.on("DELETE", "/v1/time-entries/55", ok(True))
    async with new_client() as client:

        async def scenario():
            await get_client_record(client, 1)
            await client.get_page("GET /v1/tickets", tool="list_tickets")
            await client.post(CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "y"}}, tool="create_client")
            await client.patch(TIME_ENTRY_PATCH, path_params={"timeEntryId": 55}, json_body={"Comment": "x"}, tool="t")
            await client.delete(TIME_ENTRY_DELETE, path_params={"timeEntryId": 55}, tool="t")
            await get_client_record(client, 3)  # any endpoint, but the sixth request of the key

        await vt.run(scenario())
    assert gateway.offsets() == [0, 0, 0, 0, 0, 1.0]
    assert [(method, path) for _, method, path in gateway.arrivals] == [
        ("GET", client_path(1)), ("GET", "/v1/tickets"), ("POST", "/v1/clients"),
        ("PATCH", "/v1/time-entries/55"), ("DELETE", "/v1/time-entries/55"), ("GET", client_path(3)),
    ]


async def test_the_pages_of_get_all_are_paced_by_the_bucket(vt, gateway, new_client):
    pages = [
        httpx.Response(200, json=paged_envelope([{"Id": n}], next_cursor=None if n == 8 else f"c{n}", total_count=8))
        for n in range(1, 9)
    ]
    gateway.on("GET", "/v1/clients", *pages)
    async with new_client() as client:
        tasks = await vt.run(client.get_all("GET /v1/clients", page_size=1, tool="list_clients"))
    result = tasks[0].result()
    assert result.pages == 8 and [row["Id"] for row in result.items] == list(range(1, 9)) and result.complete
    assert gateway.offsets() == [0] * 5 + [1.0, 2.0, 3.0]


async def test_a_closed_client_fails_at_once_instead_of_waiting_for_a_token(vt, gateway, new_client):
    client = new_client(burst=1)
    async with client:
        await vt.run(get_client_record(client, 1))  # the only token is gone
    with pytest.raises(RuntimeError, match="already closed"):
        await vt.run(get_client_record(client, 2))
    assert vt.delays == [] and len(gateway.arrivals) == 1


async def test_waiters_are_served_in_the_order_they_arrive(vt, gateway, new_client):
    async with new_client(burst=1) as client:  # one token at a time: requests 2 to 5 queue up behind request 1
        await vt.run(*(get_client_record(client, number) for number in range(1, 6)))
    assert gateway.paths() == [client_path(n) for n in (1, 2, 3, 4, 5)]
    assert gateway.offsets() == [0, 1.0, 2.0, 3.0, 4.0]


async def test_a_request_that_arrives_later_never_overtakes_one_that_waits(vt, gateway, new_client):
    async with new_client(burst=1) as client:

        async def latecomer():
            await vt.elapse(0.5)  # request 2 is the one asleep at the head of the line, 3 and 4 wait behind it
            await get_client_record(client, 9)

        await vt.run(*(get_client_record(client, number) for number in range(1, 5)), latecomer())
    assert gateway.paths() == [client_path(n) for n in (1, 2, 3, 4, 9)]
    assert gateway.offsets() == [0, 1.0, 2.0, 3.0, 4.0]


async def test_a_waiter_that_is_cancelled_takes_no_token_and_leaves_the_line(vt, gateway, new_client):
    async with new_client(burst=1) as client:
        tasks = [asyncio.ensure_future(get_client_record(client, number)) for number in (1, 2, 3, 4)]

        async def cancel_the_third():
            await vt.elapse(0.5)
            tasks[2].cancel()

        await vt.run(*tasks[:2], tasks[3], cancel_the_third())
        assert tasks[2].cancelled()
    # 3 left the line while 2 was asleep: 4 follows 2 one second later and the cancelled one cost nobody a second
    assert gateway.paths() == [client_path(n) for n in (1, 2, 4)]
    assert gateway.offsets() == [0, 1.0, 2.0]


async def test_a_request_that_waits_for_a_token_holds_no_in_flight_slot(vt, gateway, new_client):
    gateway.on("GET", client_path(1), ok({"Id": 1}), work=5.0)  # request 1 is in flight for five seconds
    slot_taken_while_waiting = []
    holder = {}

    async def sleep(delay):
        slot_taken_while_waiting.append(holder["client"]._semaphore.locked())
        await vt.sleep(delay)

    async with new_client(burst=1, max_concurrency=2, sleep=sleep) as client:
        holder["client"] = client
        await vt.run(get_client_record(client, 1), get_client_record(client, 2))
    # request 2 waited a second for its token while request 1 held one of the two slots; with the token taken
    # inside the slot, request 2 would have held the other one and the semaphore would have been full
    assert slot_taken_while_waiting == [False]
    assert gateway.offsets() == [0, 1.0]


async def test_max_concurrency_still_limits_the_requests_in_flight(vt, gateway, new_client):
    for number in range(1, 13):
        gateway.on("GET", client_path(number), ok({"Id": number}), work=1.0)
    async with new_client(burst=20) as client:  # a bucket that does not get in the way
        await vt.run(*(get_client_record(client, number) for number in range(1, 13)))
    assert gateway.peak == 4
    assert gateway.offsets() == [0] * 4 + [1.0] * 4 + [2.0] * 4


class SleepThatReturnsAtOnce:
    """Like conftest's fake sleep: it moves no clock. It also fails the test when the same waiter keeps coming back."""

    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)
        assert len(self.delays) < 50, "a waiter keeps asking for a wait that its sleep has already served"


@pytest.mark.parametrize("clock", [lambda: 1000.0, time.monotonic], ids=["frozen-clock", "real-clock"])
async def test_a_sleep_that_returns_at_once_cannot_make_a_waiter_spin(spec_index, mock_gorelo, clock):
    # The bucket knows that the time it slept has passed, so after the sleep the waiter takes its token instead of
    # reading a clock that has not moved and asking for the same wait again.
    mock_gorelo.on("GET", "/v1/clients/{clientId}", envelope({"Id": 1}))
    sleep = SleepThatReturnsAtOnce()
    client = GoreloClient(TEST_API_KEY, spec=spec_index, transport=mock_gorelo.transport, sleep=sleep, clock=clock)
    async with client:
        for number in range(1, 13):
            await get_client_record(client, number)
    assert len(sleep.delays) == 7  # requests 6 to 12, one wait each
    assert all(abs(delay - 1.0) < 0.01 for delay in sleep.delays)


async def test_the_default_clock_and_the_real_sleep_really_wait(spec_index, mock_gorelo):
    # the default clock (time.monotonic) and asyncio.sleep, given explicitly so that a suite-wide fixture that makes the
    # default sleep instant cannot hide what this test is for
    mock_gorelo.on("GET", "/v1/clients/{clientId}", envelope({"Id": 1}))
    async with GoreloClient(
        TEST_API_KEY, spec=spec_index, transport=mock_gorelo.transport, rate_per_second=100.0, burst=2,
        sleep=asyncio.sleep,
    ) as client:
        started = time.monotonic()
        for number in range(1, 5):
            await get_client_record(client, number)
        elapsed = time.monotonic() - started
    assert 0.015 <= elapsed < 5.0  # the third request waits 10 ms, the fourth 20 ms: not less than 30 ms in all


# --------------------------------------------------------------------------
# 429: the back-off is shared
# --------------------------------------------------------------------------


async def test_a_429_is_retried_after_the_retry_after_and_not_before(vt, gateway, new_client, caplog):
    caplog.set_level(logging.INFO, logger="gorelo_client")
    gateway.on("GET", client_path(1), too_many(2), ok({"Id": 1}))
    async with new_client() as client:
        results = await vt.run(get_client_record(client, 1))
    assert results[0].result() == {"Id": 1}
    assert gateway.offsets() == [0, 2.0] and vt.delays == [2.0]
    warnings = warning_lines(caplog)
    assert len(warnings) == 1 and "HTTP 429" in warnings[0] and "waiting 2.0s" in warnings[0] and "retry 1 of 5" in warnings[0]


@pytest.mark.parametrize("retry_after, wait", [(0, 1.0), (0.5, 1.0), (1, 1.0), (2.5, 2.5), (7, 7.0)])
async def test_the_retry_waits_for_the_longer_of_the_retry_after_and_the_next_token(
    vt, gateway, new_client, retry_after, wait
):
    # a 429 empties the bucket, so even "retry now" waits for the next token, which is one second away at 1 per second
    gateway.on("GET", client_path(1), too_many(retry_after), ok({"Id": 1}))
    async with new_client() as client:
        await vt.run(get_client_record(client, 1))
    assert vt.delays == [wait] and gateway.offsets() == [0, wait]


async def test_a_429_makes_every_queued_request_wait_for_the_retry_after_too(vt, gateway, new_client):
    # A POST that Gorelo answers 429 after half a second, and six reads. Five requests take the five tokens at once;
    # reads 6 and 7 queue for the sixth token, which the empty bucket would give them after one and two seconds.
    gateway.on("POST", "/v1/clients", too_many(4), ok({"Id": 5}), work=0.5)
    async with new_client() as client:

        async def create():
            return await client.post(
                CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "y"}}, tool="create_client"
            )

        tasks = await vt.run(create(), *(get_client_record(client, number) for number in range(2, 8)))
    assert tasks[0].result() == {"Id": 5}
    sends = [(round(when - vt.start, 3), method, path) for when, method, path in gateway.arrivals]
    assert sends[:5] == [(0, "POST", "/v1/clients")] + [(0, "GET", client_path(n)) for n in (2, 3, 4, 5)]
    # the 429 reaches the client at 0.5 s and holds every token back until 4.5 s: reads 6 and 7 do not go out at 1 and 2
    # s, and the POST goes out once more, after the two reads that were already waiting, not before the 4.5 s
    assert sends[5:] == [(4.5, "GET", client_path(6)), (4.5, "GET", client_path(7)), (4.5, "POST", "/v1/clients")]


async def test_a_429_leaves_the_requests_in_flight_alone_and_holds_back_the_ones_that_follow(vt, gateway, new_client):
    # read 1 gets a 429 while read 2 is still being answered: read 2 finishes, the requests that follow wait
    gateway.on("GET", client_path(1), too_many(3), ok({"Id": 1}), work=0.25)
    gateway.on("GET", client_path(2), ok({"Id": 2}), work=2.0)
    async with new_client() as client:

        async def later():
            await vt.elapse(1.0)  # the bucket still has three of its tokens before the 429 empties it
            await get_client_record(client, 3)

        tasks = await vt.run(get_client_record(client, 1), get_client_record(client, 2), later())
    assert tasks[0].result() == {"Id": 1} and tasks[1].result() == {"Id": 2}  # read 2 was in flight and was answered
    sends = [(round(when - vt.start, 3), path) for when, _, path in gateway.arrivals]
    # 1 and 2 at once; the 429 comes back at 0.25 s and holds every token until 3.25 s; read 1 asked again at 0.25 s and
    # read 3 asked at 1.0 s, so read 1 is served first
    assert sends == [(0, client_path(1)), (0, client_path(2)), (3.25, client_path(1)), (3.25, client_path(3))]


async def test_the_429_budget_is_five_retries_and_ends_in_a_rate_limit_error(vt, gateway, new_client):
    gateway.on("GET", client_path(1), too_many(1))
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(get_client_record(client, 1))
    err = info.value
    assert err.kind == "rate_limit" and err.status == 429 and err.write_unconfirmed is False
    assert str(err) == (
        "GET /v1/clients/{clientId}: Gorelo is rate limiting (HTTP 429); gave up after 6 attempt(s) and 5.0s of "
        "Retry-After waits (time queued behind other requests not counted)"
    )
    assert len(gateway.arrivals) == 6 and vt.delays == [1.0] * 5


async def test_the_429_budget_is_thirty_seconds_of_retry_after(vt, gateway, new_client):
    gateway.on("GET", client_path(1), too_many(10))
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(get_client_record(client, 1))
    err = info.value
    assert err.kind == "rate_limit"
    assert str(err).endswith("gave up after 4 attempt(s) and 30.0s of Retry-After waits (time queued behind other requests not counted)")
    assert vt.delays == [10.0, 10.0, 10.0] and gateway.offsets() == [0, 10.0, 20.0, 30.0]


async def test_a_429_error_keeps_its_kind_for_a_write_and_never_says_unconfirmed(vt, gateway, new_client):
    gateway.on("POST", "/v1/clients", too_many(1))
    async with new_client(max_429_retries=1) as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(client.post(CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "y"}}, tool="create_client"))
    assert info.value.kind == "rate_limit" and info.value.write_unconfirmed is False
    assert len(gateway.arrivals) == 2 and "gave up after 2 attempt(s) and 1.0s of Retry-After waits" in str(info.value)


async def test_the_429_budget_can_be_changed_while_the_client_runs(vt, gateway, new_client):
    # scripts/live/write_matrix.py sets client.max_429_retries = 0 around one write and puts the old value back
    gateway.on("GET", client_path(1), too_many(1), ok({"Id": 1}))
    gateway.on("GET", client_path(2), too_many(1), ok({"Id": 2}))
    gateway.on("GET", client_path(3), too_many(100), ok({"Id": 3}))
    async with new_client() as client:
        normal = client.max_429_retries
        assert normal == 5 and client.max_429_wait == 30.0
        client.max_429_retries = 0
        with pytest.raises(GoreloAPIError, match=r"gave up after 1 attempt\(s\) and 0.0s of Retry-After waits") as info:
            await vt.run(get_client_record(client, 1))
        assert info.value.kind == "rate_limit"
        client.max_429_retries = normal
        assert (await vt.run(get_client_record(client, 2)))[0].result() == {"Id": 2}  # retried now
        client.max_429_wait = 200.0
        assert (await vt.run(get_client_record(client, 3)))[0].result() == {"Id": 3}  # a longer wait is accepted now
        client.max_429_wait = 0.0
        gateway.on("GET", client_path(4), too_many(1), ok({"Id": 4}))
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(get_client_record(client, 4))
        assert info.value.kind == "rate_limit"


async def test_no_429_retry_still_empties_the_bucket_for_the_other_requests(vt, gateway, new_client):
    gateway.on("GET", client_path(1), too_many(3))
    async with new_client(max_429_retries=0) as client:

        async def scenario():
            with pytest.raises(GoreloAPIError):
                await get_client_record(client, 1)
            await get_client_record(client, 2)  # a different request on the same key, right after the 429

        await vt.run(scenario())
    assert gateway.offsets() == [0, 3.0]
    assert vt.delays == [3.0]


async def test_a_silly_retry_after_holds_the_others_back_for_one_budget_at_most(vt, gateway, new_client):
    gateway.on("GET", client_path(1), too_many(3600))
    async with new_client() as client:

        async def scenario():
            with pytest.raises(GoreloAPIError) as info:
                await get_client_record(client, 1)
            assert info.value.kind == "rate_limit"
            await get_client_record(client, 2)

        await vt.run(scenario())
    assert vt.delays == [30.0]  # the first request did not wait at all; the next one waits for max_429_wait, not an hour
    assert gateway.offsets() == [0, 30.0]


async def test_a_later_429_never_shortens_a_back_off_that_is_already_running(vt, gateway, new_client):
    # Read 1 is told at 0.5 s to wait ten seconds, read 2 is told at 2.0 s to wait one. When read 1 gives up at 4.0 s,
    # read 2 is next in line: it must still wait for the ten seconds, which end at 10.5 s.
    gateway.on("GET", client_path(1), too_many(10), ok({"Id": 1}), work=0.5)
    gateway.on("GET", client_path(2), too_many(1), ok({"Id": 2}), work=2.0)
    async with new_client() as client:
        first = asyncio.ensure_future(get_client_record(client, 1))
        second = asyncio.ensure_future(get_client_record(client, 2))

        async def give_up_on_the_first():
            await vt.elapse(4.0)
            first.cancel()

        await vt.run(first, second, give_up_on_the_first())
    assert first.cancelled() and second.result() == {"Id": 2}
    assert gateway.paths() == [client_path(1), client_path(2), client_path(2)]
    assert gateway.offsets() == [0, 0, 10.5]


async def test_a_request_that_holds_a_token_and_waits_for_a_slot_waits_out_a_429_that_comes_meanwhile(
    vt, gateway, new_client
):
    # Reads 1 to 4 are in flight for 3 s and hold the four slots. Reads 5, 6 and 7 took their tokens (the bucket starts
    # with five and gets one more each second, so at 0 s, 1 s and 2 s) and wait for a slot. Read 1 is answered 429 with
    # Retry-After 5 at 3 s. The four slots free up at 3 s, but the back-off runs until 8 s: no request may leave in between.
    for number in range(1, 5):
        gateway.on("GET", client_path(number), ok({"Id": number}), work=3.0)
    gateway.on("GET", client_path(1), too_many(5), ok({"Id": 1}), work=3.0)
    for number in (5, 6, 7):
        gateway.on("GET", client_path(number), ok({"Id": number}))
    async with new_client(max_concurrency=4) as client:
        tasks = await vt.run(*(get_client_record(client, number) for number in range(1, 8)))
    assert [task.result() for task in tasks] == [{"Id": number} for number in range(1, 8)]
    offsets = gateway.offsets()
    assert offsets[:4] == [0, 0, 0, 0]
    assert not [offset for offset in offsets if 3.0 <= offset < 8.0], offsets  # nothing leaves inside the back-off
    assert offsets[4:] == [8.0] * 4  # read 1 once more, and reads 5, 6 and 7 once the back-off has passed
    assert sorted(gateway.paths()[4:]) == sorted(client_path(number) for number in (1, 5, 6, 7))
    assert gateway.peak <= 4


async def test_a_request_that_waits_for_a_slot_leaves_at_once_when_no_429_came(vt, gateway, new_client):
    # the same line-up without the 429: the slots free up at 3 s and reads 5 to 7 leave at 3 s, as before
    for number in range(1, 8):
        gateway.on("GET", client_path(number), ok({"Id": number}), work=3.0)
    async with new_client(max_concurrency=4) as client:
        await vt.run(*(get_client_record(client, number) for number in range(1, 8)))
    assert gateway.offsets() == [0, 0, 0, 0, 3.0, 3.0, 3.0]
    assert vt.delays == [1.0, 1.0]  # only reads 6 and 7 waited, for their tokens


async def test_a_request_that_asks_during_the_back_off_still_queues_behind_the_retry_that_asked_first(
    vt, gateway, new_client
):
    # After the 429 a new request takes no token before 8 s and holds no slot while it waits: the order stays first come,
    # first served (read 1's retry, which asked at 3 s, before read 8, which asked at 4 s)
    for number in range(1, 5):
        gateway.on("GET", client_path(number), ok({"Id": number}), work=3.0)
    gateway.on("GET", client_path(1), too_many(5), ok({"Id": 1}), work=3.0)
    async with new_client(max_concurrency=4) as client:

        async def latecomer():
            await vt.elapse(4.0)
            return await get_client_record(client, 8)

        await vt.run(*(get_client_record(client, number) for number in range(1, 5)), latecomer())
    assert gateway.offsets() == [0, 0, 0, 0, 8.0, 8.0]
    assert gateway.paths()[4:] == [client_path(1), client_path(8)]


async def test_wait_unblocked_takes_no_token_and_returns_at_once_without_a_back_off(vt):
    bucket = gorelo_client._TokenBucket(1.0, 5, vt.clock, vt.sleep)
    await vt.run(bucket.wait_unblocked())
    assert vt.delays == [] and vt.now == vt.start
    for _ in range(5):  # all five tokens are still there
        await vt.run(bucket.take())
    assert vt.delays == [] and vt.now == vt.start


async def test_wait_unblocked_waits_for_the_end_of_the_back_off_and_again_when_a_later_429_extends_it(vt):
    bucket = gorelo_client._TokenBucket(1.0, 5, vt.clock, vt.sleep)
    bucket.back_off(5.0)

    async def a_later_429():
        await vt.elapse(2.0)
        bucket.back_off(10.0)  # ends at 12 s

    await vt.run(bucket.wait_unblocked(), a_later_429())
    assert vt.now - vt.start == 12.0
    assert vt.delays == [5.0, 7.0]


async def test_wait_unblocked_cannot_spin_on_a_sleep_that_returns_at_once():
    bucket = gorelo_client._TokenBucket(1.0, 5, lambda: 1000.0, SleepThatReturnsAtOnce())
    bucket.back_off(5.0)
    sleep = bucket._sleep
    await bucket.wait_unblocked()
    assert sleep.delays == [5.0]  # one wait, then the bucket knows that the time has passed


# --------------------------------------------------------------------------
# A read is sent once more after a gateway failure
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_a_read_answered_with_a_gateway_failure_is_sent_once_more_after_two_seconds(
    vt, gateway, new_client, caplog, status
):
    caplog.set_level(logging.INFO, logger="gorelo_client")
    gateway.on("GET", client_path(1), gateway_page(status), ok({"Id": 1}))
    async with new_client() as client:
        tasks = await vt.run(get_client_record(client, 1))
    assert tasks[0].result() == {"Id": 1}
    assert gateway.offsets() == [0, 2.0] and vt.delays == [2.0] and GATEWAY_RETRY_WAIT == 2.0
    warnings = warning_lines(caplog)
    assert warnings == [
        f"tool=get_client op=GET /v1/clients/{{clientId}} got HTTP {status}; retrying once in 2.0s"
    ]
    (info_line,) = info_lines(caplog)
    assert "status=200" in info_line and "attempts=2" in info_line


def test_the_statuses_that_are_retried_are_exactly_502_503_and_504():
    assert GATEWAY_RETRY_STATUSES == frozenset({502, 503, 504})


async def test_a_retry_after_header_on_a_gateway_failure_does_not_change_the_two_seconds(vt, gateway, new_client):
    # the wait before the one retry of a read is fixed; only a 429 is told how long to wait
    busy = httpx.Response(503, content=b"busy", headers={"content-type": "text/plain", "Retry-After": "30"})
    gateway.on("GET", client_path(1), busy, ok({"Id": 1}))
    async with new_client() as client:
        await vt.run(get_client_record(client, 1))
    assert gateway.offsets() == [0, 2.0] and vt.delays == [2.0]


# What a read raised for a gateway failure before the retry existed, and still raises when the second answer is the same
# one (the status stays in err.status, the body is never quoted).
SAME_FAILURE_TWICE = [
    pytest.param(
        gateway_page(503),
        "shape",
        "GET /v1/clients/{clientId}: unexpected response shape; refusing to guess "
        "(HTTP 503, a body that is not JSON, content-type text/plain, 114 bytes)",
        id="503-text-plain",
    ),
    pytest.param(
        gateway_page(502),
        "shape",
        "GET /v1/clients/{clientId}: unexpected response shape; refusing to guess "
        "(HTTP 502, a body that is not JSON, content-type text/html, 24 bytes)",
        id="502-html",
    ),
    pytest.param(
        httpx.Response(504, json=error_envelope(504, [("070001", "Gateway timeout")])),
        "http",
        "GET /v1/clients/{clientId}: HTTP 504: Gateway timeout",
        id="504-envelope",
    ),
]


@pytest.mark.parametrize("failure, kind, message", SAME_FAILURE_TWICE)
async def test_a_read_that_fails_again_raises_what_it_always_raised(vt, gateway, new_client, failure, kind, message):
    gateway.on("GET", client_path(1), failure)
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(get_client_record(client, 1))
    err = info.value
    assert (err.kind, err.status, err.write_unconfirmed, str(err)) == (kind, failure.status_code, False, message)
    assert len(gateway.arrivals) == 2 and vt.delays == [2.0]  # once more, never a third time


async def test_the_failure_a_read_raises_is_the_one_of_its_last_attempt(vt, gateway, new_client):
    gateway.on("GET", client_path(1), gateway_page(503), httpx.Response(404, json=error_envelope(404, [("070401", "gone")])))
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(get_client_record(client, 1))
    assert (info.value.kind, info.value.status, info.value.trace_id) == ("http", 404, TEST_TRACE_ID)
    assert len(gateway.arrivals) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 500, 501, 505, 599, 302, 304])
async def test_other_statuses_of_a_read_are_not_retried(vt, gateway, new_client, status):
    gateway.on("GET", client_path(1), httpx.Response(status, content=b"<html>x</html>", headers={"content-type": "text/html"}))
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(get_client_record(client, 1))
    assert info.value.status == status
    assert len(gateway.arrivals) == 1 and vt.delays == []


async def test_a_connection_that_is_never_established_is_retried_once(vt, gateway, new_client, caplog):
    caplog.set_level(logging.INFO, logger="gorelo_client")
    gateway.on("GET", client_path(1), lambda: httpx.ConnectError("refused"), ok({"Id": 1}))
    async with new_client() as client:
        tasks = await vt.run(get_client_record(client, 1))
    assert tasks[0].result() == {"Id": 1}
    assert gateway.offsets() == [0, 2.0] and vt.delays == [2.0]
    assert warning_lines(caplog) == ["tool=get_client op=GET /v1/clients/{clientId} connect error; retrying once in 2.0s"]
    (info_line,) = info_lines(caplog)
    assert "status=200" in info_line and "attempts=2" in info_line


async def test_a_connection_that_fails_twice_raises_the_transport_error_it_always_raised(vt, gateway, new_client, caplog):
    caplog.set_level(logging.INFO, logger="gorelo_client")
    gateway.on("GET", client_path(1), lambda: httpx.ConnectError("refused"))
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(get_client_record(client, 1))
    err = info.value
    assert (err.kind, err.status, err.write_unconfirmed) == ("transport", None, False)
    assert str(err) == "GET /v1/clients/{clientId}: the connection failed (ConnectError); this was a read, nothing was changed"
    assert len(gateway.arrivals) == 2 and vt.delays == [2.0]
    (info_line,) = info_lines(caplog)
    assert "status=transport" in info_line and "attempts=2" in info_line


async def test_the_log_line_of_a_request_tells_how_its_last_attempt_ended(vt, gateway, new_client, caplog):
    caplog.set_level(logging.INFO, logger="gorelo_client")
    gateway.on("GET", client_path(1), gateway_page(503), lambda: httpx.ConnectError("refused"))
    async with new_client() as client:
        with pytest.raises(GoreloAPIError):
            await vt.run(get_client_record(client, 1))
    (info_line,) = info_lines(caplog)
    assert "status=transport" in info_line and "attempts=2" in info_line  # not the 503 of the first attempt


@pytest.mark.parametrize(
    "failure, kind",
    [
        pytest.param(lambda: httpx.ReadTimeout("slow"), "timeout", id="read-timeout"),
        pytest.param(lambda: httpx.WriteTimeout("slow"), "timeout", id="write-timeout"),
        pytest.param(lambda: httpx.PoolTimeout("slow"), "timeout", id="pool-timeout"),
        pytest.param(lambda: httpx.ConnectTimeout("slow"), "timeout", id="connect-timeout"),
        pytest.param(lambda: httpx.ReadError("reset"), "transport", id="read-error"),
        pytest.param(lambda: httpx.WriteError("reset"), "transport", id="write-error"),
        pytest.param(lambda: httpx.RemoteProtocolError("bad"), "transport", id="remote-protocol-error"),
        pytest.param(lambda: httpx.LocalProtocolError("bad"), "transport", id="local-protocol-error"),
        pytest.param(lambda: httpx.UnsupportedProtocol("bad"), "transport", id="unsupported-protocol"),
    ],
)
async def test_a_timeout_and_a_failure_after_the_connection_was_made_are_not_retried(
    vt, gateway, new_client, failure, kind
):
    gateway.on("GET", client_path(1), failure)
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(get_client_record(client, 1))
    assert info.value.kind == kind and info.value.write_unconfirmed is False
    assert len(gateway.arrivals) == 1 and vt.delays == []


WRITES = {
    "POST": lambda c: c.post(CREATE_CLIENT, json_body={"Name": "x", "Location": {"Name": "y"}}, tool="create_client"),
    "PATCH": lambda c: c.patch(TIME_ENTRY_PATCH, path_params={"timeEntryId": 55}, json_body={"Comment": "x"}, tool="t"),
    "DELETE": lambda c: c.delete(TIME_ENTRY_DELETE, path_params={"timeEntryId": 55}, tool="t"),
    "multipart": lambda c: c.post_multipart(
        "POST /v1/attachments", files={"file": ("a.txt", b"x")}, form={"itemType": "Ticket"}, tool="t"
    ),
}
WRITE_PATHS = {
    "POST": "/v1/clients", "PATCH": "/v1/time-entries/55", "DELETE": "/v1/time-entries/55", "multipart": "/v1/attachments",
}


@pytest.mark.parametrize("write", sorted(WRITES))
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda: gateway_page(502), id="502"),
        pytest.param(lambda: gateway_page(503), id="503"),
        pytest.param(lambda: gateway_page(504), id="504"),
        pytest.param(lambda: httpx.ConnectError("refused"), id="connect-error"),
    ],
)
async def test_a_write_is_never_sent_twice_after_a_gateway_failure(vt, gateway, new_client, write, failure):
    method = "POST" if write == "multipart" else write
    gateway.on(method, WRITE_PATHS[write], failure, ok({"Id": 1}))
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(WRITES[write](client))
    assert info.value.write_unconfirmed is True
    assert len(gateway.arrivals) == 1 and vt.delays == []


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda: gateway_page(502), id="502"),
        pytest.param(lambda: gateway_page(503), id="503"),
        pytest.param(lambda: gateway_page(504), id="504"),
        pytest.param(lambda: httpx.ConnectError("refused"), id="connect-error"),
    ],
)
async def test_the_invoice_pdf_export_is_never_sent_twice_after_a_gateway_failure(vt, gateway, new_client, failure):
    # a GET that Gorelo records as an export event: a retry records another one
    pdf = httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    gateway.on("GET", f"/v1/invoices/{uid(1)}/pdf", failure, pdf)
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(client.get_binary(PDF_OP, path_params={"invoiceId": uid(1)}, max_bytes=1000, tool="export_invoice_pdf"))
    assert info.value.write_unconfirmed is True and EXPORT_NOTE in str(info.value)
    assert len(gateway.arrivals) == 1 and vt.delays == []


async def test_the_invoice_pdf_export_is_retried_on_a_429_because_gorelo_did_not_process_it(vt, gateway, new_client):
    # a 429 is the one failure that is retried for every method, a side-effect GET included: nothing was recorded
    pdf = httpx.Response(200, content=b"%PDF-1.7 export", headers={"content-type": "application/pdf"})
    gateway.on("GET", f"/v1/invoices/{uid(1)}/pdf", too_many(2), pdf)
    async with new_client() as client:
        tasks = await vt.run(
            client.get_binary(PDF_OP, path_params={"invoiceId": uid(1)}, max_bytes=1000, tool="export_invoice_pdf")
        )
    assert tasks[0].result().content == b"%PDF-1.7 export"
    assert gateway.offsets() == [0, 2.0] and vt.delays == [2.0]


async def test_the_invoice_pdf_export_gives_up_on_429_within_the_429_budget_and_is_not_unconfirmed(vt, gateway, new_client):
    gateway.on("GET", f"/v1/invoices/{uid(1)}/pdf", too_many(1))
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(
                client.get_binary(PDF_OP, path_params={"invoiceId": uid(1)}, max_bytes=1000, tool="export_invoice_pdf")
            )
    err = info.value
    assert (err.kind, err.status, err.write_unconfirmed) == ("rate_limit", 429, False)
    assert len(gateway.arrivals) == 6 and vt.delays == [1.0] * 5  # the same budget as any request: 5 retries


async def test_a_503_on_the_invoice_pdf_export_after_a_429_is_not_retried(vt, gateway, new_client):
    # the 429 is retried; the 503 that follows is not (the export may have been recorded) and says so
    gateway.on("GET", f"/v1/invoices/{uid(1)}/pdf", too_many(1), gateway_page(503), httpx.Response(200, content=b"%PDF"))
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(
                client.get_binary(PDF_OP, path_params={"invoiceId": uid(1)}, max_bytes=1000, tool="export_invoice_pdf")
            )
    assert info.value.write_unconfirmed is True and EXPORT_NOTE in str(info.value) and info.value.status == 503
    assert len(gateway.arrivals) == 2  # the 429 and the 503, never a third request


async def test_a_pdf_that_changes_nothing_is_retried_like_any_read(vt, gateway, new_client):
    # the document PDF is a binary GET but not a side-effect GET
    path = f"/v1/documents/{uid(5)}/pdf"
    pdf = httpx.Response(200, content=b"%PDF-1.7 ok", headers={"content-type": "application/pdf"})
    gateway.on("GET", path, gateway_page(503), pdf)
    async with new_client() as client:
        download = client.get_binary(
            DOCUMENT_PDF_OP, path_params={"documentId": uid(5)}, max_bytes=1000, tool="get_document_pdf"
        )
        tasks = await vt.run(download)
    assert tasks[0].result().content == b"%PDF-1.7 ok"
    assert gateway.offsets() == [0, 2.0] and vt.delays == [2.0]


async def test_a_pdf_that_changes_nothing_raises_what_it_raised_when_the_second_answer_fails_too(vt, gateway, new_client):
    path = f"/v1/documents/{uid(5)}/pdf"
    gateway.on("GET", path, gateway_page(502))
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            download = client.get_binary(
                DOCUMENT_PDF_OP, path_params={"documentId": uid(5)}, max_bytes=1000, tool="get_document_pdf"
            )
            await vt.run(download)
    assert info.value.kind == "shape" and info.value.status == 502 and info.value.write_unconfirmed is False
    assert "HTTP 502, a body that is not JSON, content-type text/html, 24 bytes" in str(info.value)
    assert len(gateway.arrivals) == 2


async def test_the_retry_of_a_read_takes_a_token_like_every_attempt(vt, gateway, new_client):
    # one token, refilled every ten seconds: the first attempt takes it, the retry (two seconds later) waits for the next
    gateway.on("GET", client_path(1), gateway_page(503), ok({"Id": 1}))
    async with new_client(burst=1, rate_per_second=0.1) as client:
        await vt.run(get_client_record(client, 1))
    assert gateway.offsets() == [0, 10.0]
    assert vt.delays == [2.0, 8.0]


async def test_a_read_that_is_waiting_to_be_retried_holds_no_token_and_no_slot(vt, gateway, new_client):
    # request 1 sleeps its two seconds; request 2 is served in the meantime, from the bucket and the slot
    gateway.on("GET", client_path(1), gateway_page(503), ok({"Id": 1}))
    async with new_client(max_concurrency=1) as client:
        await vt.run(get_client_record(client, 1), get_client_record(client, 2))
    assert gateway.paths() == [client_path(1), client_path(2), client_path(1)]
    assert gateway.offsets() == [0, 0, 2.0]


async def test_the_gateway_retry_and_the_429_budget_are_separate(vt, gateway, new_client):
    # 503, then a 429, then success: one retry of each kind, so max_429_retries=1 is enough
    gateway.on("GET", client_path(1), gateway_page(503), too_many(1), ok({"Id": 1}))
    async with new_client(max_429_retries=1) as client:
        tasks = await vt.run(get_client_record(client, 1))
    assert tasks[0].result() == {"Id": 1}
    assert len(gateway.arrivals) == 3


async def test_a_second_gateway_failure_after_a_429_is_not_retried_again(vt, gateway, new_client):
    # one gateway retry per request in all, wherever it is spent
    gateway.on("GET", client_path(1), too_many(1), gateway_page(503), gateway_page(503), ok({"Id": 1}))
    async with new_client() as client:
        with pytest.raises(GoreloAPIError) as info:
            await vt.run(get_client_record(client, 1))
    assert info.value.status == 503 and len(gateway.arrivals) == 3  # 429, 503 (retried), 503 (raised)


async def test_the_warning_of_a_retry_carries_no_value_and_no_query(vt, gateway, new_client, caplog):
    caplog.set_level(logging.DEBUG)
    gateway.on("GET", "/v1/tickets", gateway_page(503), httpx.Response(200, json=paged_envelope([])))
    async with new_client() as client:
        await vt.run(client.get_page("GET /v1/tickets", query={"Query": SECRET}, tool="list_tickets"))
    retry_lines = [r.getMessage() for r in caplog.records if r.name == "gorelo_client" and "retrying once" in r.getMessage()]
    assert retry_lines == ["tool=list_tickets op=GET /v1/tickets got HTTP 503; retrying once in 2.0s"]
    for record in caplog.records:
        assert SECRET not in record.getMessage() and TEST_API_KEY not in record.getMessage()
    assert not [r for r in caplog.records if r.name.split(".")[0] in ("httpx", "httpcore")]


# --------------------------------------------------------------------------
# Against a Gorelo that limits the whole key
# --------------------------------------------------------------------------


class StrictGorelo(Gateway):
    """Gorelo as it was measured on 2026-10-24: one bucket for the whole key, a burst of 6 and one token a second back.
    A request that finds the bucket empty gets a 429 with `Retry-After: 1`, whatever endpoint it asked."""

    def __init__(self, vt, latency=0.2):
        super().__init__(vt)
        self.latency = latency
        self.tokens = 6.0
        self.stamp = vt.now
        self.served = 0
        self.limited = 0

    async def __call__(self, request):
        self.arrivals.append((self.vt.now, request.method, request.url.path))
        self.tokens = min(6.0, self.tokens + (self.vt.now - self.stamp))
        self.stamp = self.vt.now
        allowed = self.tokens >= 1.0
        if allowed:
            self.tokens -= 1.0
            self.served += 1
        else:
            self.limited += 1
        await self.vt.elapse(self.latency)
        answer = ok({"Id": 1}) if allowed else too_many(1)
        return httpx.Response(answer.status_code, headers=dict(answer.headers), content=answer.content)


async def parallel_tools(client, vt, tools=8, reads_each=3):
    """claude.ai running `tools` tools at once, each of which reads `reads_each` records one after the other."""

    async def one_tool(tool):
        for read in range(reads_each):
            await get_client_record(client, tool * 10 + read + 1)

    return await vt.run(*(one_tool(tool) for tool in range(tools)))


async def test_parallel_tools_never_meet_a_429_through_the_bucket(vt, spec_index):
    strict = StrictGorelo(vt)
    client = GoreloClient(
        TEST_API_KEY, spec=spec_index, transport=httpx.MockTransport(strict), sleep=vt.sleep, clock=vt.clock
    )
    async with client:
        await parallel_tools(client, vt)
    assert strict.limited == 0 and strict.served == 24
    assert vt.now - vt.start < 21.0  # five at once, then one a second: the 24th goes out at 19 s and is answered at 19.2 s


async def test_the_same_tools_do_meet_429s_without_a_bucket(vt, spec_index):
    # the control of the test above: a bucket so large that it never holds anything back is a client without one
    strict = StrictGorelo(vt)
    client = GoreloClient(
        TEST_API_KEY, spec=spec_index, transport=httpx.MockTransport(strict), sleep=vt.sleep, clock=vt.clock,
        rate_per_second=1000.0, burst=1000,
    )
    async with client:
        try:
            await parallel_tools(client, vt)
        except GoreloAPIError as err:  # some tool may give up after its 5 retries: that is the problem being shown
            assert err.kind == "rate_limit"
    assert strict.limited >= 10


# --------------------------------------------------------------------------
# The constructor
# --------------------------------------------------------------------------


def test_the_default_sleep_of_production_is_the_real_asyncio_sleep():
    # conftest's autouse fixture gives every test client an instant default sleep, so the suite cannot see the default
    # that production runs with. server.build_server builds its client with the defaults: a bucket that waits through an
    # instant sleep would send 24 requests in a burst and draw 429s. ORIGINAL_CLIENT_SLEEP is the default as the code
    # defines it, read before the fixture replaced it.
    assert ORIGINAL_CLIENT_SLEEP is asyncio.sleep
    assert GoreloClient.__init__.__kwdefaults__["sleep"] is not ORIGINAL_CLIENT_SLEEP  # and the fixture is in force here


async def test_the_client_the_server_builds_waits_through_the_real_sleep_and_the_real_clock(server_factory, monkeypatch):
    monkeypatch.setitem(GoreloClient.__init__.__kwdefaults__, "sleep", ORIGINAL_CLIENT_SLEEP)  # the fixture undone
    seen = []
    original = GoreloClient.__aenter__

    async def spy(self):
        seen.append(self)
        return await original(self)

    monkeypatch.setattr(GoreloClient, "__aenter__", spy)
    async with Client(server_factory()) as session:  # the lifespan builds the client
        await list_tools(session)
    assert len(seen) == 1
    built = seen[0]
    assert built._sleep is asyncio.sleep and built._bucket._sleep is asyncio.sleep
    assert built._bucket._clock is time.monotonic
    assert (built._bucket._burst, built._bucket._interval) == (5, 1.0)


def test_the_defaults_of_the_bucket_and_of_the_429_budget():
    parameters = inspect.signature(GoreloClient.__init__).parameters
    names = ("rate_per_second", "burst", "max_429_retries", "max_429_wait", "max_concurrency")
    defaults = {name: parameters[name].default for name in names}
    assert defaults == {"rate_per_second": 1.0, "burst": 5, "max_429_retries": 5, "max_429_wait": 30.0, "max_concurrency": 4}
    assert parameters["clock"].default is time.monotonic
    for name in ("rate_per_second", "burst", "clock", "sleep", "max_429_retries", "max_429_wait", "max_concurrency"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name


def test_the_module_docstring_quotes_the_defaults_and_the_constants_of_the_code():
    parameters = inspect.signature(GoreloClient.__init__).parameters
    doc = " ".join(gorelo_client.__doc__.split())
    statuses = sorted(GATEWAY_RETRY_STATUSES)
    quoted = [
        f"`burst` tokens (default {parameters['burst'].default})",
        f"(default {parameters['rate_per_second'].default:g} request per second)",
        f"`max_429_retries` retries (default {parameters['max_429_retries'].default})",
        f"`max_429_wait` seconds (default {int(parameters['max_429_wait'].default)})",
        f"in flight at once (default {parameters['max_concurrency'].default})",
        f"after {GATEWAY_RETRY_WAIT} s, when the gateway answers HTTP {statuses[0]}, {statuses[1]} or {statuses[2]}",
    ]
    for words in quoted:
        assert words in doc, f"the docstring of gorelo_client.py no longer says {words!r}"


@pytest.mark.parametrize(
    "bad",
    [0, 0.0, -1, -0.5, float("nan"), float("inf"), float("-inf"), True, False, "1", None, [1], 10**400, 5e-324],
)
def test_rate_per_second_must_be_a_finite_number_above_zero(spec_index, bad):
    with pytest.raises(ValueError, match="rate_per_second"):
        GoreloClient(TEST_API_KEY, spec=spec_index, rate_per_second=bad)


@pytest.mark.parametrize("good", [1, 0.5, 100, 1e-3, 2.5])
def test_a_usable_rate_is_accepted(spec_index, good):
    GoreloClient(TEST_API_KEY, spec=spec_index, rate_per_second=good)


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, 5.0, "5", None, [5]])
def test_burst_must_be_an_integer_of_at_least_one(spec_index, bad):
    with pytest.raises(ValueError, match="burst"):
        GoreloClient(TEST_API_KEY, spec=spec_index, burst=bad)


@pytest.mark.parametrize("good", [1, 5, 50])
def test_a_usable_burst_is_accepted(spec_index, good):
    GoreloClient(TEST_API_KEY, spec=spec_index, burst=good)


@pytest.mark.parametrize(
    "clock",
    [
        pytest.param(None, id="none"),
        pytest.param("now", id="text"),
        pytest.param(5, id="number"),
        pytest.param(lambda: "noon", id="returns-text"),
        pytest.param(lambda: None, id="returns-none"),
        pytest.param(lambda: True, id="returns-a-boolean"),
        pytest.param(lambda: float("nan"), id="returns-nan"),
        pytest.param(lambda: float("inf"), id="returns-infinity"),
    ],
)
def test_the_clock_must_be_a_callable_that_returns_a_finite_number(spec_index, clock):
    with pytest.raises(ValueError, match="clock"):
        GoreloClient(TEST_API_KEY, spec=spec_index, clock=clock)


def test_the_existing_arguments_are_still_checked(spec_index):
    with pytest.raises(ValueError, match="max_concurrency"):
        GoreloClient(TEST_API_KEY, spec=spec_index, max_concurrency=0)
    with pytest.raises(ValueError, match="max_429"):
        GoreloClient(TEST_API_KEY, spec=spec_index, max_429_wait=-1)
