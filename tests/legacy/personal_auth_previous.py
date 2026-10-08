"""
FastMCP Personal Auth Provider

A drop-in OAuth 2.1 auth provider for FastMCP that works with Claude.ai,
Claude mobile, Claude Desktop, and Claude Code - no external identity
provider required.

Usage:
    from fastmcp import FastMCP
    from personal_auth import PersonalAuthProvider

    auth = PersonalAuthProvider(
        base_url="https://your-domain.com",
        password="your-secret-password",
        allowed_redirect_domains=["claude.ai", "claude.com", "localhost"],
    )

    mcp = FastMCP(name="my-server", auth=auth)

    @mcp.tool
    def hello() -> str:
        return "Hello, world!"

    mcp.run(transport="streamable-http", host="0.0.0.0", port=8050)
"""

import html as html_lib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.handlers.authorize import (
    AuthorizationHandler as _SDKAuthorizationHandler,
)
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

logger = logging.getLogger("personal-auth")

DEFAULT_ACCESS_TOKEN_EXPIRY = 30 * 24 * 60 * 60  # 30 days
DEFAULT_STATE_DIR = ".oauth-state"

AUTHORIZATION_PATH = "/authorize"
FAILED_ATTEMPT_WINDOW_SECONDS = 15 * 60  # 15 minutes
MAX_FAILED_ATTEMPTS = 5

# OAuth fields we must preserve across the consent form round-trip.
_OAUTH_PASSTHROUGH_FIELDS = (
    "client_id",
    "redirect_uri",
    "response_type",
    "code_challenge",
    "code_challenge_method",
    "state",
    "scope",
    "resource",
)


class PersonalAuthProvider(InMemoryOAuthProvider):
    """OAuth 2.1 provider for personal/small-team MCP servers.

    Fills the gap between FastMCP's InMemoryOAuthProvider (test-only, no
    persistence, no security) and OAuthProxy (requires Google/GitHub/Auth0).

    Features:
    - Dynamic Client Registration (DCR) for Claude.ai compatibility
    - PKCE support (handled by FastMCP framework)
    - Restrict /authorize to approved redirect domains only
    - Interactive browser consent page with password gate
    - Constant-time password comparison + per-client rate limiting
    - Token persistence to a JSON file (survives restarts)
    - Configurable token expiry (default 30 days)
    """

    def __init__(
        self,
        base_url: str,
        password: Optional[str] = None,
        allowed_redirect_domains: Optional[list[str]] = None,
        access_token_expiry_seconds: int = DEFAULT_ACCESS_TOKEN_EXPIRY,
        state_dir: Optional[str] = None,
    ):
        """
        Args:
            base_url: Public URL of this server (e.g. "https://my-server.example.com")
            password: Optional password required to authorize. If set, every
                OAuth authorization request triggers an interactive consent
                page that requires this password. If None, no consent page is
                shown and authorization proceeds under the domain allowlist
                only.
            allowed_redirect_domains: List of domains allowed in OAuth redirect URIs.
                Defaults to ["claude.ai", "claude.com", "localhost"]. Set to None
                to allow all domains (not recommended for public servers).
            access_token_expiry_seconds: How long access tokens last. Default 30 days.
            state_dir: Directory for persisting OAuth state. Default ".oauth-state".
        """
        super().__init__(
            base_url=base_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )

        self.password = password
        self.allowed_redirect_domains = allowed_redirect_domains if allowed_redirect_domains is not None else [
            "claude.ai", "claude.com", "localhost"
        ]
        self.access_token_expiry_seconds = access_token_expiry_seconds
        self._state_dir = Path(state_dir or DEFAULT_STATE_DIR)
        self._state_dir.mkdir(parents=True, exist_ok=True)

        # In-memory rate-limit tracker: client_id -> list of failed-attempt timestamps.
        self._failed_attempts: dict[str, list[float]] = {}

        self._load_state()

    # --- State persistence ---

    def _state_file(self) -> Path:
        return self._state_dir / "oauth_tokens.json"

    def _load_state(self):
        f = self._state_file()
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text())
            for k, v in data.get("clients", {}).items():
                self.clients[k] = OAuthClientInformationFull(**v)
            for k, v in data.get("access_tokens", {}).items():
                self.access_tokens[k] = AccessToken(**v)
            for k, v in data.get("refresh_tokens", {}).items():
                self.refresh_tokens[k] = RefreshToken(**v)
            self._access_to_refresh_map = data.get("a2r", {})
            self._refresh_to_access_map = data.get("r2a", {})
            logger.info(
                f"Loaded OAuth state: {len(self.clients)} clients, "
                f"{len(self.access_tokens)} access tokens"
            )
        except Exception as e:
            logger.warning(f"Failed to load OAuth state from {f}: {e}")

    def _save_state(self):
        def serialize(obj):
            if hasattr(obj, "model_dump"):
                return obj.model_dump(mode="json")
            return {
                "token": obj.token, "client_id": obj.client_id,
                "scopes": obj.scopes, "expires_at": obj.expires_at,
            }

        data = {
            "clients": {k: v.model_dump(mode="json") for k, v in self.clients.items()},
            "access_tokens": {k: serialize(v) for k, v in self.access_tokens.items()},
            "refresh_tokens": {k: serialize(v) for k, v in self.refresh_tokens.items()},
            "a2r": self._access_to_refresh_map,
            "r2a": self._refresh_to_access_map,
        }
        self._state_file().write_text(json.dumps(data, indent=2))

    # --- Authorization gate ---

    def _is_redirect_allowed(self, redirect_uri: str) -> bool:
        if self.allowed_redirect_domains is None:
            return True
        try:
            host = urlparse(redirect_uri).hostname or ""
            return any(
                host == domain or host.endswith(f".{domain}")
                for domain in self.allowed_redirect_domains
            )
        except Exception:
            return False

    # --- Rate limiting (per client_id, sliding 15-minute window) ---

    def _prune_failed_attempts(self, client_id: str) -> list[float]:
        cutoff = time.time() - FAILED_ATTEMPT_WINDOW_SECONDS
        attempts = [t for t in self._failed_attempts.get(client_id, []) if t > cutoff]
        if attempts:
            self._failed_attempts[client_id] = attempts
        else:
            self._failed_attempts.pop(client_id, None)
        return attempts

    def _is_rate_limited(self, client_id: str) -> bool:
        if not client_id:
            return False
        return len(self._prune_failed_attempts(client_id)) >= MAX_FAILED_ATTEMPTS

    def _record_failed_attempt(self, client_id: str) -> None:
        if not client_id:
            return
        self._prune_failed_attempts(client_id)
        self._failed_attempts.setdefault(client_id, []).append(time.time())

    def _clear_failed_attempts(self, client_id: str) -> None:
        if client_id:
            self._failed_attempts.pop(client_id, None)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await super().register_client(client_info)
        self._save_state()

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        redirect = str(params.redirect_uri) if params.redirect_uri else ""

        # Defense-in-depth: domain allowlist is re-checked here in case the
        # HTTP layer is ever reached via a path that bypasses the consent
        # route. Password gating lives entirely in the HTTP /authorize route
        # (interactive consent page) - by the time this method runs, the
        # user has already been through that flow, or no password was
        # configured.
        if not self._is_redirect_allowed(redirect):
            raise AuthorizeError(
                error="access_denied",
                error_description="Redirect URI domain not allowed.",
            )

        result = await super().authorize(client, params)
        self._save_state()
        return result

    # --- Token exchange with configurable expiry ---

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if authorization_code.code not in self.auth_codes:
            raise TokenError("invalid_grant", "Authorization code not found or already used.")

        del self.auth_codes[authorization_code.code]

        access_token_value = f"pat_{secrets.token_hex(32)}"
        refresh_token_value = f"prt_{secrets.token_hex(32)}"
        access_token_expires_at = int(time.time() + self.access_token_expiry_seconds)

        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")

        self.access_tokens[access_token_value] = AccessToken(
            token=access_token_value,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=access_token_expires_at,
        )
        self.refresh_tokens[refresh_token_value] = RefreshToken(
            token=refresh_token_value,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=None,
        )

        self._access_to_refresh_map[access_token_value] = refresh_token_value
        self._refresh_to_access_map[refresh_token_value] = access_token_value
        self._save_state()

        return OAuthToken(
            access_token=access_token_value,
            token_type="Bearer",
            expires_in=self.access_token_expiry_seconds,
            refresh_token=refresh_token_value,
            scope=" ".join(authorization_code.scopes),
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        result = await super().exchange_refresh_token(client, refresh_token, scopes)
        self._save_state()
        return result

    async def revoke_token(self, token):
        await super().revoke_token(token)
        self._save_state()

    # --- HTTP routes: replace /authorize with interactive consent ---

    def get_routes(self, mcp_path: Optional[str] = None) -> list[Route]:
        routes = super().get_routes(mcp_path)

        # If no password is configured, preserve the original behavior
        # (redirect-domain check only, no consent page). Return routes
        # unchanged.
        if self.password is None:
            return routes

        sdk_handler = _SDKAuthorizationHandler(provider=self)
        consent_endpoint = self._make_authorize_endpoint(sdk_handler)

        patched: list[Route] = []
        for route in routes:
            if isinstance(route, Route) and route.path == AUTHORIZATION_PATH:
                patched.append(
                    Route(
                        path=AUTHORIZATION_PATH,
                        endpoint=consent_endpoint,
                        methods=["GET", "POST"],
                    )
                )
            else:
                patched.append(route)
        return patched

    def _make_authorize_endpoint(self, sdk_handler: _SDKAuthorizationHandler):
        """Build the Starlette endpoint that serves the consent page."""

        async def endpoint(request: Request) -> Response:
            source_ip = request.client.host if request.client else "?"

            if request.method == "GET":
                params = request.query_params
            else:
                params = await request.form()

            client_id = (params.get("client_id") or "").strip()
            redirect_uri_str = params.get("redirect_uri") or ""

            # Requirement #5: domain allowlist is a first-layer filter. If
            # the redirect URI isn't allowed, never render the consent page.
            # Delegating to the SDK handler produces the same AuthorizeError
            # (raised by our authorize() defense-in-depth check) rendered as
            # a proper OAuth error response.
            if redirect_uri_str and not self._is_redirect_allowed(redirect_uri_str):
                logger.warning(
                    "authorize outcome=blocked_domain client_id=%s redirect=%s ip=%s",
                    client_id,
                    redirect_uri_str,
                    source_ip,
                )
                return await sdk_handler.handle(request)

            client_info = self.clients.get(client_id) if client_id else None
            client_name = (
                client_info.client_name
                if client_info and client_info.client_name
                else "(unregistered client)"
            )

            if request.method == "GET":
                logger.info(
                    "authorize outcome=consent_shown client_id=%s client_name=%s ip=%s",
                    client_id,
                    client_name,
                    source_ip,
                )
                return self._render_consent_page(params)

            # POST - form submission from the consent page.
            form = params  # starlette FormData
            decision = (form.get("decision") or "").strip()

            if decision == "deny":
                logger.info(
                    "authorize outcome=denied_by_user client_id=%s client_name=%s ip=%s",
                    client_id,
                    client_name,
                    source_ip,
                )
                return self._build_deny_redirect(form)

            # Rate limit check before any password comparison.
            if self._is_rate_limited(client_id):
                logger.warning(
                    "authorize outcome=rate_limited client_id=%s client_name=%s ip=%s",
                    client_id,
                    client_name,
                    source_ip,
                )
                return PlainTextResponse(
                    "Too many failed attempts. Try again in 15 minutes.",
                    status_code=429,
                )

            submitted_password = form.get("password") or ""
            if not secrets.compare_digest(submitted_password, self.password or ""):
                self._record_failed_attempt(client_id)
                logger.warning(
                    "authorize outcome=wrong_password client_id=%s client_name=%s ip=%s",
                    client_id,
                    client_name,
                    source_ip,
                )
                # If this attempt pushed us over the limit, surface 429 now.
                if self._is_rate_limited(client_id):
                    return PlainTextResponse(
                        "Too many failed attempts. Try again in 15 minutes.",
                        status_code=429,
                    )
                return self._render_consent_page(
                    form,
                    error="Incorrect password. Please try again.",
                )

            # Approved and password valid - clear counter and let the SDK
            # handler do the heavy lifting (re-validates params, calls
            # provider.authorize(), builds the 302 redirect with the code).
            # Starlette caches request.form(), so the SDK handler sees the
            # same fields we just parsed.
            self._clear_failed_attempts(client_id)
            logger.info(
                "authorize outcome=approved client_id=%s client_name=%s ip=%s",
                client_id,
                client_name,
                source_ip,
            )
            return await sdk_handler.handle(request)

        return endpoint

    def _render_consent_page(
        self,
        params,
        error: Optional[str] = None,
    ) -> HTMLResponse:
        def h(s: Optional[str]) -> str:
            return html_lib.escape(s or "", quote=True)

        client_id = params.get("client_id") or ""
        client_info = self.clients.get(client_id) if client_id else None
        client_name = (
            client_info.client_name
            if client_info and client_info.client_name
            else "(unregistered client)"
        )
        redirect_uri = params.get("redirect_uri") or ""
        scope = params.get("scope") or ""
        scopes_display = scope if scope else "(none)"

        hidden_inputs: list[str] = []
        for field in _OAUTH_PASSTHROUGH_FIELDS:
            value = params.get(field)
            if value is None or value == "":
                continue
            hidden_inputs.append(
                f'<input type="hidden" name="{h(field)}" value="{h(value)}">'
            )
        hidden_html = "\n".join(hidden_inputs)

        error_html = f'<div class="err">{h(error)}</div>' if error else ""

        body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize {h(client_name)}</title>
<style>
body {{ background:#111; color:#eee; font-family:system-ui,-apple-system,sans-serif; margin:0; padding:2rem; display:flex; justify-content:center; }}
.card {{ background:#1b1b1b; border:1px solid #333; border-radius:8px; padding:1.5rem 2rem; max-width:520px; width:100%; }}
h1 {{ font-size:1.25rem; margin:0 0 1rem; }}
dl {{ margin:0 0 1rem; }}
dt {{ font-size:0.75rem; color:#888; text-transform:uppercase; letter-spacing:0.05em; margin-top:0.75rem; }}
dd {{ margin:0.25rem 0 0; word-break:break-all; }}
label {{ display:block; margin-bottom:0.4rem; font-size:0.9rem; color:#bbb; }}
input[type=password] {{ width:100%; padding:0.6rem; background:#000; color:#eee; border:1px solid #444; border-radius:4px; font-size:1rem; box-sizing:border-box; }}
.buttons {{ display:flex; gap:0.5rem; margin-top:1rem; }}
button {{ flex:1; padding:0.7rem; border-radius:4px; border:1px solid #444; font-size:1rem; cursor:pointer; font-family:inherit; }}
button.approve {{ background:#2b6; color:#000; border-color:#2b6; font-weight:600; }}
button.deny {{ background:#222; color:#eee; }}
.err {{ background:#3a1414; color:#f99; border:1px solid #622; padding:0.5rem 0.75rem; border-radius:4px; margin-bottom:1rem; }}
</style>
</head>
<body>
<div class="card">
<h1>Authorize MCP Access</h1>
{error_html}
<dl>
<dt>Client</dt><dd>{h(client_name)}</dd>
<dt>Client ID</dt><dd>{h(client_id)}</dd>
<dt>Redirect URI</dt><dd>{h(redirect_uri)}</dd>
<dt>Scopes</dt><dd>{h(scopes_display)}</dd>
</dl>
<form method="POST" action="{AUTHORIZATION_PATH}" autocomplete="off">
{hidden_html}
<label for="password">Password</label>
<input id="password" type="password" name="password" autofocus required>
<div class="buttons">
<button class="approve" type="submit" name="decision" value="approve">Approve</button>
<button class="deny" type="submit" name="decision" value="deny">Deny</button>
</div>
</form>
</div>
</body>
</html>
"""
        return HTMLResponse(body)

    def _build_deny_redirect(self, form) -> Response:
        """Build the OAuth error redirect for a user-denied request.

        Validates the redirect_uri against both the registered client AND
        the domain allowlist to prevent the deny path from being abused as
        an open redirect.
        """
        client_id = (form.get("client_id") or "").strip()
        redirect_uri_str = form.get("redirect_uri") or ""
        state = form.get("state")

        client_info = self.clients.get(client_id) if client_id else None
        if not client_info:
            return PlainTextResponse("invalid_client", status_code=400)

        try:
            raw = AnyUrl(redirect_uri_str) if redirect_uri_str else None
            validated = client_info.validate_redirect_uri(raw)
        except Exception:
            return PlainTextResponse("invalid_redirect_uri", status_code=400)

        if not self._is_redirect_allowed(str(validated)):
            return PlainTextResponse(
                "redirect_uri domain not allowed",
                status_code=400,
            )

        error_params = {
            "error": "access_denied",
            "error_description": "User denied authorization",
        }
        if state:
            error_params["state"] = state

        return RedirectResponse(
            url=construct_redirect_uri(str(validated), **error_params),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
