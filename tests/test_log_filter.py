"""server.LogValueFilter: argument values never reach FastMCP's log records (offline, no sockets).

FastMCP logs a rejected tool call with logger.exception, and pydantic's ValidationError prints every
offending argument (`input_value=...`). Those are client names, emails and phone numbers. main() installs
the filter; these tests drive it with synthetic records and with a real rejected call.
"""

import logging

import pytest
from conftest import call_tool_raw
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import TypeAdapter, ValidationError

from server import (
    FILTERED_LOGGERS,
    LOG_VALUE_FILTER,
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
