"""Entry point of the Gorelo MCP server (the systemd unit runs `/opt/gorelo-mcp/app/.venv/bin/python main.py`, see docs/INSTALL.md and docs/OPERATIONS.md).

Thin on purpose: keep argument values out of FastMCP's log records, read settings, build the OAuth
provider, build the server, serve HTTP on 127.0.0.1:8765. Importing this module has no side effects
(no .env read, no logging setup, no directories); everything happens inside main().

The service refuses to start (one error line, exit status 1, nothing served) when its settings are
wrong, when the OAuth state file cannot be trusted (oauth_store.StateFileError: the file is left as it
is, for the operator to look at) and when the login gate cannot be made safe (oauth_guard.GateError:
an unchecked framework version, no password, a route that was not replaced).
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from oauth_guard import GateError
from oauth_store import StateFileError
from personal_auth import PersonalAuthProvider
from server import build_server, install_log_value_filter
from settings import Settings, SettingsError

# The directory this file lives in. In production that is /opt/gorelo-mcp/app, which is also the
# systemd WorkingDirectory, so the OAuth state stays in the same ./.oauth-state it always used
# (PersonalAuthProvider's default was the relative path ".oauth-state").
APP_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765

logger = logging.getLogger("gorelo-mcp")


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    install_log_value_filter()  # FastMCP's rejected-call logging prints argument values otherwise
    try:
        settings = Settings.from_env(os.environ)
    except SettingsError as exc:
        logger.error("%s; refusing to start", exc)
        sys.exit(1)
    try:
        auth = PersonalAuthProvider(
            base_url=settings.public_base_url,
            password=settings.mcp_auth_password,
            state_dir=str(APP_DIR / ".oauth-state"),
        )
        # The gate also checks its routes when the HTTP app is built, which happens inside run().
        build_server(settings, auth=auth).run(transport="http", host=HOST, port=PORT)
    except (StateFileError, GateError) as exc:
        logger.error("%s; refusing to start", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
