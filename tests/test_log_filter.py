"""server.LogValueFilter: argument values never reach FastMCP's log records (offline, no sockets).

FastMCP logs a rejected tool call with logger.exception, and pydantic's ValidationError prints every
offending argument (`input_value=...`). Those are client names, emails and phone numbers. An error that
holds values the journal must not keep (update_client's alarm lists the previous values of custom fields,
possibly a password or a licence key) says so with a log_summary attribute and is logged as that summary
alone. main() installs the filter; these tests drive it with synthetic records, with a real rejected call
and with a real alarm of update_client through the in-process fastmcp Client at INFO and at DEBUG.
"""

import copy
import logging
import pickle
import traceback
from contextlib import contextmanager

import httpx
import pytest
from conftest import call_tool_raw, envelope, error_envelope, in_order, quiet_loggers
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import TypeAdapter, ValidationError

import tools.clients as clients_module
from gorelo_client import GoreloAPIError
from server import (
    FILTERED_LOGGERS,
    LOG_VALUE_FILTER,
    PRIVATE_LOG_ATTRIBUTE,
    LogValueFilter,
    install_log_value_filter,
    redact_input_values,
    remove_log_value_filter,
    validation_summary,
)

pytestmark = pytest.mark.anyio

SECRET = "SECRET-VALUE-123"


@pytest.fixture(autouse=True)
def _leave_global_logging_as_found():
    yield
    remove_log_value_filter()


def validation_error(value=SECRET):
    """A real pydantic ValidationError, as FastMCP raises for a tool call with a mistyped argument."""
    try:
        TypeAdapter(dict[str, int]).validate_python({"name": value, "tags": [1, {"deep": value}]})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def exc_info_of(exc):
    return (type(exc), exc, exc.__traceback__)


def record_for(msg, exc=None, args=None, name="fastmcp.server.server"):
    return logging.LogRecord(name, logging.ERROR, __file__, 10, msg, args, exc_info_of(exc) if exc is not None else None)


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


# --------------------------------------------------------------------------
# The pieces
# --------------------------------------------------------------------------


def test_the_control_case_pydantic_prints_the_value():
    assert SECRET in str(validation_error())  # which is why the filter exists


@pytest.mark.parametrize(
    "text, expected",
    [
        ("x [type=string_type, input_value='abc', input_type=str]", "x [type=string_type, input_value=<omitted>, input_type=str]"),
        ("[type=t, input_value={'a': [1, 2], 'b': ']'}, input_type=dict]", "[type=t, input_value=<omitted>, input_type=dict]"),
        ("input_value=['a, b', 'c'], input_type=list", "input_value=<omitted>, input_type=list"),
        ("two: input_value=1, input_type=int] and input_value='x', input_type=str]",
         "two: input_value=<omitted>, input_type=int] and input_value=<omitted>, input_type=str]"),
        ("a\ninput_value='multi\nline', input_type=str]", "a\ninput_value=<omitted>, input_type=str]"),
        ("input_value=truncated with no type] tail", "input_value=<omitted>] tail"),
        ("nothing to hide here", "nothing to hide here"),
        ("", ""),
    ],
)
def test_input_value_fragments_are_redacted(text, expected):
    once = redact_input_values(text)
    assert once == expected
    assert redact_input_values(once) == once  # idempotent


def test_the_real_pydantic_message_is_fully_redacted():
    redacted = redact_input_values(str(validation_error()))
    assert SECRET not in redacted and "input_value=<omitted>" in redacted and "input_type=" in redacted


def test_the_summary_lists_locations_and_types_only():
    summary = validation_summary(validation_error())
    assert summary == "2 validation error(s), argument values omitted: name: int_parsing; tags: int_type"
    assert SECRET not in summary


def test_the_summary_prints_list_indexes_and_hides_anything_that_could_be_a_users_key():
    try:
        TypeAdapter(dict[str, list[int]]).validate_python({"ids": [1, "x"], f"{SECRET} key": ["y"]})
    except ValidationError as exc:
        summary = validation_summary(exc)
    assert "ids.1: int_parsing" in summary and "<key>.0: int_parsing" in summary and SECRET not in summary


def test_a_long_summary_is_cut_and_says_how_many_more():
    try:
        TypeAdapter(list[int]).validate_python(["x"] * 30)
    except ValidationError as exc:
        summary = validation_summary(exc)
    assert summary.startswith("30 validation error(s)") and summary.endswith("; and 10 more")
    assert summary.count("int_parsing") == 20


def test_a_summary_never_fails_even_for_a_strange_error():
    class Strange(Exception):
        def errors(self, **kwargs):
            raise RuntimeError("boom")

    assert validation_summary(Strange()) == "validation failed, argument values omitted"


# --------------------------------------------------------------------------
# The filter on a synthetic record
# --------------------------------------------------------------------------


def test_a_validation_error_record_loses_its_values_and_its_traceback():
    exc = validation_error()
    record = record_for("Error validating tool 'create_client'", exc)
    assert LogValueFilter().filter(record) is True  # it never drops a record
    assert record.exc_info is None and record.exc_text is None and record.args is None
    message = record.getMessage()
    assert message == (
        "Error validating tool 'create_client' "
        "[2 validation error(s), argument values omitted: name: int_parsing; tags: int_type]"
    )
    formatted = logging.Formatter("%(levelname)s %(name)s %(message)s").format(record)
    assert SECRET not in formatted and "Traceback" not in formatted and "input_value" not in formatted
    assert (record.name, record.levelno) == ("fastmcp.server.server", logging.ERROR)


def test_a_cached_traceback_text_is_dropped_too():
    record = record_for("Error validating tool 'x'", validation_error())
    logging.Formatter().format(record)  # a handler that ran first caches exc_text on the record
    assert SECRET in record.exc_text
    LogValueFilter().filter(record)
    assert record.exc_text is None
    assert SECRET not in logging.Formatter().format(record)


def test_the_validation_error_may_be_a_cause_a_context_or_inside_a_group():
    wrapped_cause = None
    try:
        try:
            raise validation_error()
        except ValidationError as inner:
            raise ToolError("Error calling tool 'x'") from inner
    except ToolError as outer:
        wrapped_cause = outer
    try:
        try:
            raise validation_error()
        except ValidationError:
            raise RuntimeError("while handling it")  # noqa: B904 (the implicit __context__ is the point)
    except RuntimeError as outer:
        wrapped_context = outer
    grouped = ExceptionGroup("several", [KeyError("k"), ExceptionGroup("deeper", [validation_error()])])
    for exc in (wrapped_cause, wrapped_context, grouped):
        record = record_for("Error calling tool 'x'", exc)
        LogValueFilter().filter(record)
        assert record.exc_info is None, type(exc).__name__
        assert SECRET not in record.getMessage() and "argument values omitted" in record.getMessage()


def test_another_exception_keeps_its_traceback_and_its_message():
    exc = ValueError("name: must not be empty")
    record = record_for("Error calling tool 'x'", exc)
    LogValueFilter().filter(record)
    assert record.exc_info == exc_info_of(exc) and record.getMessage() == "Error calling tool 'x'"


def test_a_message_that_embeds_a_validation_error_is_redacted_even_without_exc_info():
    record = record_for(f"retrying after: {validation_error()}")
    LogValueFilter().filter(record)
    assert SECRET not in record.getMessage() and "input_value=<omitted>" in record.getMessage()
    assert record.exc_info is None


def test_percent_arguments_are_merged_before_redaction():
    exc = validation_error()
    record = record_for("tool %s failed: %s", exc, args=("create_client", str(exc)))
    LogValueFilter().filter(record)
    message = record.getMessage()
    assert message.startswith("tool create_client failed: ") and SECRET not in message and record.args is None


def test_a_percent_sign_in_a_redacted_message_is_not_a_format_directive():
    record = record_for("100% of input_value='x', input_type=str] failed", None)
    LogValueFilter().filter(record)
    assert record.getMessage() == "100% of input_value=<omitted>, input_type=str] failed"


def test_a_record_that_is_clean_is_left_exactly_as_it_was():
    record = record_for("tool=%s status=%s", None, args=("t", 200))
    before = (record.msg, record.args, record.exc_info, record.exc_text)
    LogValueFilter().filter(record)
    assert (record.msg, record.args, record.exc_info, record.exc_text) == before


def test_the_filter_never_breaks_logging():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("cannot print")

    record = record_for(Unprintable(), validation_error())
    assert LogValueFilter().filter(record) is True
    assert record.exc_info is None and "withheld" in record.getMessage() and SECRET not in record.getMessage()
    broken_args = record_for("%s and %s", None, args=("only one",))  # getMessage() raises TypeError
    assert LogValueFilter().filter(broken_args) is True


# --------------------------------------------------------------------------
# The filter on an error that holds values the journal must not keep
# --------------------------------------------------------------------------


class PrivateError(Exception):
    """An error whose text holds a value (SECRET) and that says what may be logged instead, as the alarm of
    tools/clients.py does."""

    def __init__(self, summary="op X on client 7: fields 1, 2 changed; values withheld from the log"):
        super().__init__(f"the previous value was {SECRET}")
        self.log_summary = summary


SUMMARY = "op X on client 7: fields 1, 2 changed; values withheld from the log"


def test_the_attribute_is_the_one_the_alarm_of_the_clients_module_carries():
    changes = [
        (42, clients_module._CustomField("Licence", True, SECRET), "now reads unset"),
        (43, clients_module._CustomField(None, False, None), "now reads set"),
    ]
    alarm = clients_module._collateral_error(changes, ["a problem"], 9101)
    assert PRIVATE_LOG_ATTRIBUTE == "log_summary"
    assert isinstance(getattr(alarm, PRIVATE_LOG_ATTRIBUTE), str)  # the name the filter looks for is the one it sets
    assert SECRET in str(alarm)  # the model needs the previous value to put it back ...
    assert SECRET not in alarm.log_summary  # ... the journal does not
    assert alarm.log_summary == (
        "PATCH /v1/clients/{clientId} on client 9101: custom field(s) 42, 43 changed without being sent; "
        "previous values withheld from the log"
    )
    record = record_for("Error calling tool 'update_client'", ToolError(str(alarm)))
    LogValueFilter().filter(record)
    assert record.exc_info is not None  # a ToolError that only carries the text is not marked: the mark is on the cause
    try:
        raise ToolError("Error calling tool 'update_client'") from alarm
    except ToolError as error:
        record = record_for("Error calling tool 'update_client'", error)
    LogValueFilter().filter(record)
    assert record.exc_info is None and SECRET not in logging.Formatter().format(record)
    assert record.getMessage() == f"Error calling tool 'update_client' [{alarm.log_summary}]"


def test_a_record_with_a_private_error_loses_its_traceback_and_gets_the_summary_instead():
    record = record_for("Error calling tool 'x'", PrivateError())
    assert LogValueFilter().filter(record) is True  # it never drops a record
    assert record.exc_info is None and record.exc_text is None and record.args is None
    assert record.getMessage() == f"Error calling tool 'x' [{SUMMARY}]"
    formatted = logging.Formatter("%(levelname)s %(name)s %(message)s").format(record)
    assert SECRET not in formatted and "Traceback" not in formatted
    assert (record.name, record.levelno) == ("fastmcp.server.server", logging.ERROR)


def test_a_cached_traceback_text_of_a_private_error_is_dropped_too():
    record = record_for("Error calling tool 'x'", PrivateError())
    logging.Formatter().format(record)  # a handler that ran first caches exc_text on the record
    assert SECRET in record.exc_text
    LogValueFilter().filter(record)
    assert record.exc_text is None
    assert SECRET not in logging.Formatter().format(record)


def test_the_private_error_may_be_a_cause_a_context_or_inside_a_group():
    try:
        try:
            raise PrivateError()
        except PrivateError as inner:
            raise ToolError("Error calling tool 'x'") from inner
    except ToolError as outer:
        wrapped_cause = outer
    try:
        try:
            raise PrivateError()
        except PrivateError:
            raise RuntimeError("while handling it")  # noqa: B904 (the implicit __context__ is the point)
    except RuntimeError as outer:
        wrapped_context = outer
    grouped = ExceptionGroup("several", [KeyError("k"), ExceptionGroup("deeper", [PrivateError()])])
    for exc in (PrivateError(), wrapped_cause, wrapped_context, grouped):
        record = record_for("Error calling tool 'x'", exc)
        LogValueFilter().filter(record)
        assert record.exc_info is None, type(exc).__name__
        assert record.getMessage() == f"Error calling tool 'x' [{SUMMARY}]", type(exc).__name__
        assert SECRET not in logging.Formatter().format(record)


def test_percent_arguments_are_merged_and_a_percent_sign_in_the_summary_is_not_a_directive():
    record = record_for("tool %s failed", PrivateError("100% of the fields changed"), args=("update_client",))
    LogValueFilter().filter(record)
    assert record.getMessage() == "tool update_client failed [100% of the fields changed]" and record.args is None


@pytest.mark.parametrize("summary", ["", None, 5, b"bytes", ["a list"]])
def test_only_a_text_summary_marks_an_error_and_an_empty_one_still_cuts_the_traceback(summary):
    exc = PrivateError(summary)
    record = record_for("Error calling tool 'x'", exc)
    LogValueFilter().filter(record)
    if summary == "":
        assert record.exc_info is None
        assert record.getMessage() == "Error calling tool 'x' [details withheld from the log]"
    else:
        assert record.exc_info == exc_info_of(exc)  # not a text: not a mark, the record is left alone
        assert record.getMessage() == "Error calling tool 'x'"


def test_a_private_error_wins_over_a_validation_error_in_the_same_chain():
    grouped = ExceptionGroup("both", [validation_error(), PrivateError()])
    record = record_for("Error calling tool 'x'", grouped)
    LogValueFilter().filter(record)
    assert record.exc_info is None
    assert record.getMessage() == f"Error calling tool 'x' [{SUMMARY}]"  # one summary, and no value of either
    assert SECRET not in logging.Formatter().format(record)


def test_a_property_that_raises_cannot_break_logging_and_still_cuts_the_traceback():
    class Hostile(Exception):
        @property
        def log_summary(self):
            raise RuntimeError("cannot read")

    record = record_for("Error calling tool 'x'", Hostile(SECRET))
    assert LogValueFilter().filter(record) is True
    assert record.exc_info is None and "withheld" in record.getMessage() and SECRET not in record.getMessage()


def test_a_record_with_no_exception_to_read_is_left_alone():
    record = record_for("Error calling tool 'x'", None)
    record.exc_info = (None, None, None)  # what logging.exception stores outside an except block
    LogValueFilter().filter(record)
    assert record.exc_info == (None, None, None) and record.getMessage() == "Error calling tool 'x'"


def duplicates():
    """Copies and unpickled copies of the same alarm, with the alarm itself: the ways an exception is duplicated."""
    held = clients_module._CustomField("Licence", True, SECRET)
    alarm = clients_module._collateral_error([(42, held, "now reads unset")], [], 9101)
    why = "the read of the client after it timed out"
    unchecked = clients_module._unchecked_error({42: held}, why, [41], None, 9101)
    makers = {"copy": copy.copy, "deepcopy": copy.deepcopy}
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        makers[f"pickle-{protocol}"] = lambda error, p=protocol: pickle.loads(pickle.dumps(error, protocol=p))
    errors = (("changed", alarm), ("unchecked", unchecked))
    return [(f"{kind}-{name}", error, make(error)) for kind, error in errors for name, make in makers.items()]


@pytest.mark.parametrize("name, original, duplicate", duplicates(), ids=[name for name, _, _ in duplicates()])
def test_the_filter_still_redacts_a_copy_or_an_unpickled_copy_of_the_alarm(name, original, duplicate):
    assert SECRET in str(duplicate)  # the copy still carries the text for the model
    try:
        raise ToolError("Error calling tool 'update_client'") from duplicate
    except ToolError as wrapped:
        wrapped_copy = wrapped
    for exc in (duplicate, wrapped_copy, ExceptionGroup("group", [duplicate])):
        record = record_for("Error calling tool 'update_client'", exc)
        LogValueFilter().filter(record)
        assert record.exc_info is None, type(exc).__name__
        assert record.getMessage() == f"Error calling tool 'update_client' [{original.log_summary}]"
        assert SECRET not in logging.Formatter().format(record)


def test_control_a_copy_that_lost_the_mark_would_be_logged_with_its_values():
    # what GoreloAPIError.__reduce__ alone would give a copy of the alarm: the same text, no log_summary
    held = clients_module._CustomField("Licence", True, SECRET)
    alarm = clients_module._collateral_error([(42, held, "now reads unset")], [], 9101)
    plain = GoreloAPIError(str(alarm), status=200, op_key=alarm.op_key, kind="shape", write_unconfirmed=True)
    record = record_for("Error calling tool 'update_client'", plain)
    LogValueFilter().filter(record)
    assert record.exc_info is not None and SECRET in logging.Formatter().format(record)


# --------------------------------------------------------------------------
# Installing it
# --------------------------------------------------------------------------


def _snapshot():
    state = {}
    names = sorted({*FILTERED_LOGGERS, *(n for n in logging.root.manager.loggerDict if n.split(".")[0] in FILTERED_LOGGERS)})
    for name in names:
        logger = logging.getLogger(name)
        state[name] = (list(logger.filters), [(h, list(h.filters)) for h in logger.handlers])
    return state


def test_install_attaches_the_filter_to_the_loggers_and_their_handlers_and_remove_undoes_it():
    before = _snapshot()
    assert install_log_value_filter() is LOG_VALUE_FILTER
    fastmcp = logging.getLogger("fastmcp")
    assert LOG_VALUE_FILTER in fastmcp.filters and LOG_VALUE_FILTER in logging.getLogger("mcp").filters
    assert fastmcp.handlers and all(LOG_VALUE_FILTER in handler.filters for handler in fastmcp.handlers)
    assert LOG_VALUE_FILTER in logging.getLogger("fastmcp.server.server").filters  # a child that already exists
    install_log_value_filter()  # twice changes nothing
    assert fastmcp.filters.count(LOG_VALUE_FILTER) == 1
    assert all(handler.filters.count(LOG_VALUE_FILTER) == 1 for handler in fastmcp.handlers)
    remove_log_value_filter()
    assert _snapshot() == before


def test_the_filter_is_installed_on_the_handlers_so_children_created_later_are_covered():
    # A logger's own filters only see records logged directly on it; FastMCP's records come from child
    # loggers and are handled by the handlers of the "fastmcp" logger (it does not propagate to root).
    parent = logging.getLogger("gtest_tree")
    handler = ListHandler()
    parent.addHandler(handler)
    parent.propagate = False
    try:
        install_log_value_filter(["gtest_tree"])
        late_child = logging.getLogger("gtest_tree.created.after.install")
        late_child.error("Error validating tool 'x'", exc_info=exc_info_of(validation_error()))
        (record,) = handler.records
        assert record.exc_info is None and SECRET not in record.getMessage()
    finally:
        parent.removeHandler(handler)
        remove_log_value_filter(["gtest_tree"])


def test_the_default_loggers_are_fastmcp_and_mcp():
    assert FILTERED_LOGGERS == ("fastmcp", "mcp")


# --------------------------------------------------------------------------
# A real rejected call
# --------------------------------------------------------------------------


def _server():
    server = FastMCP("t")

    @server.tool
    async def make_client(name: str, phone: str = "1") -> dict:
        """Make a client."""
        return {}

    return server


async def test_fastmcps_own_log_of_a_rejected_call_carries_no_argument_values():
    fastmcp_logger = logging.getLogger("fastmcp")
    handler = ListHandler()
    fastmcp_logger.addHandler(handler)
    arguments = {"name": {"first": SECRET}, "phone": SECRET}
    try:
        server = _server()
        # control: as FastMCP ships, the record carries the value in its exception
        result = await call_tool_raw(server, "make_client", arguments)
        assert result.is_error
        raw = [r for r in handler.records if "Error validating tool 'make_client'" in r.getMessage()]
        assert raw and raw[0].exc_info and SECRET in str(raw[0].exc_info[1])

        handler.records.clear()
        install_log_value_filter()
        result = await call_tool_raw(server, "make_client", arguments)
        assert result.is_error
        assert handler.records, "FastMCP logged nothing about the rejected call: re-check what it logs"
        for record in handler.records:
            assert SECRET not in record.getMessage(), record.getMessage()
            assert record.exc_info is None or SECRET not in str(record.exc_info[1])
            assert record.exc_text is None or SECRET not in record.exc_text
        sanitized = [r for r in handler.records if "Error validating tool 'make_client'" in r.getMessage()]
        assert sanitized and sanitized[0].getMessage() == (
            "Error validating tool 'make_client' [1 validation error(s), argument values omitted: name: string_type]"
        )
    finally:
        fastmcp_logger.removeHandler(handler)


# --------------------------------------------------------------------------
# A real alarm of update_client, through the in-process fastmcp Client
# --------------------------------------------------------------------------
#
# update_client reads the client, writes, and reads it again. When a custom field that was not in the call has changed, it
# raises an error whose text for the model lists the previous values (they are needed to put the fields back, and may be a
# password or a licence key). FastMCP logs a failed tool with logger.exception, which prints the whole chain with its
# messages, so the error is marked (CollateralChangeError.log_summary) and the filter logs it as that summary alone.
# FastMCP also logs the arguments of every call in a DEBUG line (fastmcp.server.mixins.mcp_operations): those are the
# values the caller sent, at DEBUG only, a line this server leaves as FastMCP writes it.

PREVIOUS = "LICENCE-KEY-7f3a-PRIVATE"
SENT = "SENT-VALUE-XYZ"
FASTMCP_ARGUMENT_LINE = "fastmcp.server.mixins.mcp_operations"
CLIENT_9101 = "/v1/clients/9101"
ALARM_LOG_TEXT = (
    "Error calling tool 'update_client' [PATCH /v1/clients/{clientId} on client 9101: custom field(s) 42 changed "
    "without being sent; previous values withheld from the log]"
)


def custom_field(identifier, name, value, is_set=True):
    return {"Id": identifier, "Variable": f"v{identifier}", "Name": name, "Type": {"Id": 1, "Name": "Text"}, "Value": value, "IsSet": is_set}


def client_with(*fields):
    return {"Id": 9101, "Name": "Example Co", "CustomProperties": list(fields)}


READ_BEFORE = client_with(custom_field(41, "Region", None, is_set=False), custom_field(42, "Licence", PREVIOUS))
READ_AFTER = client_with(custom_field(41, "Region", SENT), custom_field(42, "Licence", None, is_set=False))  # 42 was wiped
CALL = {"client_id": 9101, "custom_properties": [{"id": 41, "value": SENT}]}


def everything_in(record):
    """All the text a handler could print for a record: the message, the arguments, the traceback."""
    parts = [str(record.msg), repr(record.args), record.exc_text or ""]
    try:
        parts.append(record.getMessage())
    except Exception:
        pass
    if record.exc_info and record.exc_info[1] is not None:
        parts.append("".join(traceback.format_exception(*record.exc_info)))
    return "\n".join(parts)


@contextmanager
def logs_at(level, *, filtered):
    """Every record of the root, "fastmcp" and "mcp" loggers at `level`, with the filter installed as main() does it (or not).
    The handler is added before the filter is installed, because the filter goes on the handlers that exist then."""
    handler = ListHandler()
    names = ("", "fastmcp", "mcp")
    loggers = [logging.getLogger(name) for name in names]
    with quiet_loggers(names, level):
        for logger_ in loggers:
            logger_.addHandler(handler)
        if filtered:
            install_log_value_filter()
        try:
            yield handler.records
        finally:
            remove_log_value_filter()
            for logger_ in loggers:
                logger_.removeHandler(handler)


UNCHECKED_LOG_TEXT = (
    "Error calling tool 'update_client' [PATCH /v1/clients/{clientId} on client 9101: the custom fields could not be "
    "checked after the update; previous states of custom field(s) 41, 42 withheld from the log]"
)
BOOM = error_envelope(500, [("070101", "Boom.")])
FLOWS = [
    # (what the second read answers, what the PATCH answers, the text FastMCP's record carries instead of the exception)
    pytest.param(envelope(READ_AFTER), envelope(READ_AFTER), ALARM_LOG_TEXT, id="another-field-changed"),
    pytest.param(envelope(READ_AFTER), envelope(None), ALARM_LOG_TEXT, id="the-answer-is-unusable-and-a-field-changed"),
    pytest.param(BOOM, envelope(READ_AFTER), UNCHECKED_LOG_TEXT, id="the-second-read-fails"),
    pytest.param(BOOM, envelope(None), UNCHECKED_LOG_TEXT, id="the-answer-is-unusable-and-the-second-read-fails"),
    pytest.param(
        envelope({"Id": 9101}), httpx.Response(200, content=b""), UNCHECKED_LOG_TEXT, id="neither-can-be-used"
    ),
]


NOT_CONFIRMED_LOG_TEXT = (
    "Error calling tool 'update_client' [PATCH /v1/clients/{{clientId}} on client 9101: the update was not confirmed "
    "({how}); previous states of custom field(s) 41, 42 withheld from the log]"
)
BAD_GATEWAY_PAGE = httpx.Response(502, content=b"<html>bad gateway</html>", headers={"content-type": "text/html"})


def not_confirmed_flow(patch_answer, how, name):
    """A flow whose PATCH ends without an answer Gorelo settled: no second read follows."""
    return pytest.param(None, patch_answer, NOT_CONFIRMED_LOG_TEXT.format(how=how), id=name)


NOT_CONFIRMED_FLOWS = [
    not_confirmed_flow(httpx.ReadTimeout("slow"), "timeout", "the-patch-times-out"),
    not_confirmed_flow(httpx.ConnectError("down"), "connection error", "the-connection-fails"),
    not_confirmed_flow(error_envelope(500, [("070101", "Boom.")]), "HTTP 500", "the-patch-answers-500"),
    not_confirmed_flow(BAD_GATEWAY_PAGE, "HTTP 502", "the-patch-answers-a-502-page"),
]


def route_flow(mock_gorelo, second_read, patch_answer):
    reads = [envelope(READ_BEFORE)] + ([] if second_read is None else [second_read])
    mock_gorelo.on("GET", CLIENT_9101, in_order(*reads))
    mock_gorelo.on("PATCH", CLIENT_9101, patch_answer)


ALL_FLOWS = [*FLOWS, *NOT_CONFIRMED_FLOWS]


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG], ids=["info", "debug"])
@pytest.mark.parametrize("second_read, patch_answer, logged", ALL_FLOWS)
async def test_the_previous_values_in_the_error_reach_the_model_but_no_log_record_when_the_filter_is_installed(
    server_factory, mock_gorelo, second_read, patch_answer, logged, level
):
    route_flow(mock_gorelo, second_read, patch_answer)
    server = server_factory(toolsets={"core"})
    with logs_at(level, filtered=True) as records:
        result = await call_tool_raw(server, "update_client", CALL)
    assert result.is_error
    shown_to_the_model = "\n".join(block.text for block in result.content)
    assert PREVIOUS in shown_to_the_model  # the model must have it to put the field back
    assert SENT not in shown_to_the_model  # a value the caller sent is never echoed
    assert records, "nothing was logged: check what FastMCP logs for a failed tool"
    for record in records:
        assert PREVIOUS not in everything_in(record), (record.name, record.getMessage())
    # the one record FastMCP writes for the failed tool: the summary instead of the exception
    (failure,) = [r for r in records if r.name == "fastmcp.server.server" and r.levelno >= logging.ERROR]
    assert failure.exc_info is None and failure.exc_text is None and failure.args is None
    assert failure.getMessage() == logged
    # the values the caller sent are in no record at INFO; at DEBUG FastMCP's own line with the arguments of a call has them
    carrying_the_sent_value = {r.name for r in records if SENT in everything_in(r)}
    if level == logging.INFO:
        assert carrying_the_sent_value == set()
    else:
        assert carrying_the_sent_value <= {FASTMCP_ARGUMENT_LINE}, carrying_the_sent_value


@pytest.mark.parametrize("second_read, patch_answer, logged", ALL_FLOWS)
async def test_control_without_the_filter_the_exception_carries_the_previous_value_into_the_log(
    server_factory, mock_gorelo, second_read, patch_answer, logged
):
    # proves the checks above can fail: the same call, the same records, but nothing installed
    route_flow(mock_gorelo, second_read, patch_answer)
    server = server_factory(toolsets={"core"})
    with logs_at(logging.INFO, filtered=False) as records:
        result = await call_tool_raw(server, "update_client", CALL)
    assert result.is_error
    (failure,) = [r for r in records if r.name == "fastmcp.server.server" and r.levelno >= logging.ERROR]
    assert failure.exc_info is not None and PREVIOUS in everything_in(failure)


async def test_an_error_that_holds_no_previous_value_is_logged_with_its_traceback_as_before(server_factory, mock_gorelo):
    # only the marked alarm is cut: a plain refusal by Gorelo keeps the record FastMCP wrote
    mock_gorelo.on("GET", CLIENT_9101, envelope(READ_BEFORE))
    mock_gorelo.on("PATCH", CLIENT_9101, error_envelope(400, [("070101", "Custom field 'Region' is required.")]))
    server = server_factory(toolsets={"core"})
    with logs_at(logging.INFO, filtered=True) as records:
        result = await call_tool_raw(server, "update_client", CALL)
    assert result.is_error
    (failure,) = [r for r in records if r.name == "fastmcp.server.server" and r.levelno >= logging.ERROR]
    assert failure.exc_info is not None and failure.getMessage() == "Error calling tool 'update_client'"


READ_SENT_SHOWS = client_with(custom_field(41, "Region", SENT), custom_field(42, "Licence", PREVIOUS))
READ_SENT_MISSING = client_with(custom_field(41, "Region", None, is_set=False), custom_field(42, "Licence", PREVIOUS))
REFUSED = error_envelope(400, [("070101", "Custom field 'Region' is required.")])
OUTCOMES = [
    # name: (what the second read answers, or None for no second read; what the PATCH answers)
    pytest.param(envelope(READ_SENT_SHOWS), envelope(READ_SENT_SHOWS), id="written-and-confirmed"),
    pytest.param(envelope(READ_AFTER), envelope(READ_AFTER), id="another-field-changed"),
    pytest.param(envelope(READ_SENT_MISSING), envelope(READ_SENT_MISSING), id="the-sent-field-is-not-confirmed"),
    pytest.param(error_envelope(500, [("070101", "Boom.")]), envelope(READ_SENT_SHOWS), id="the-second-read-fails"),
    pytest.param(envelope(READ_SENT_SHOWS), envelope(None), id="the-answer-is-unusable"),
    pytest.param(None, REFUSED, id="gorelo-refuses-the-write"),
    pytest.param(None, httpx.ReadTimeout("slow"), id="the-patch-times-out"),
    pytest.param(None, error_envelope(500, [("070101", "Boom.")]), id="the-patch-answers-500"),
]


@pytest.mark.parametrize("second_read, patch_answer", OUTCOMES)
async def test_the_values_the_caller_sent_and_the_previous_ones_are_in_no_record_at_info_whatever_the_outcome(
    server_factory, mock_gorelo, second_read, patch_answer
):
    reads = [envelope(READ_BEFORE)] + ([] if second_read is None else [second_read])
    mock_gorelo.on("GET", CLIENT_9101, in_order(*reads))
    mock_gorelo.on("PATCH", CLIENT_9101, patch_answer)
    server = server_factory(toolsets={"core"})
    with logs_at(logging.INFO, filtered=True) as records:
        await call_tool_raw(server, "update_client", CALL)
    assert records
    for record in records:
        text = everything_in(record)
        assert SENT not in text and PREVIOUS not in text, (record.name, record.getMessage())
