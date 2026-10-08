"""Tool modules. Importing this package imports every module below, so their @gorelo_tool
decorators register the tools in tools._common.REGISTRY.

server.build_server() does that import (unless it is given a custom Registry) and then registers the
tools selected by GORELO_TOOLSETS and GORELO_ENABLE_DESTRUCTIVE. Shared helpers live in tools/_common.py only.

Two rules every tool module follows (details in the tools/_common.py docstring):
* Never call a decorated tool from another tool. Use client_of(ctx) for HTTP, and
  reread_after_write() to read a record back after a write. Only the outermost tool translates errors.
* A running tool can only send the operations it declared in ops=[...]: declare every GET it makes
  too, including the re-read after a write.
"""

from tools import (  # noqa: F401
    alerts,
    assets,
    attachments,
    catalog,
    clients,
    contacts,
    contracts,
    conversations,
    forms,
    invoices,
    meta,
    org,
    project_tasks,
    projects,
    tickets,
    time_entries,
    uptime,
)
from tools._common import REGISTRY, Registry, ToolSpec, gorelo_tool  # noqa: F401
