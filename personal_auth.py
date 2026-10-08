"""
FastMCP Personal Auth Provider

A drop-in OAuth 2.1 auth provider for FastMCP that works with Claude.ai,
Claude mobile, Claude Desktop, and Claude Code. No external identity
provider is required.

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
import logging
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import (
    InvalidRedirectUriError,
    InvalidScopeError,
    OAuthClientInformationFull,
    OAuthToken,
)
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route, request_response

import oauth_guard
import oauth_store
from oauth_guard import GateError, VersionGuardError  # noqa: F401  (re-exported: main.py refuses to start on them)

logger = logging.getLogger("personal-auth")

DEFAULT_ACCESS_TOKEN_EXPIRY = 30 * 24 * 60 * 60  # 30 days: the access token of a sign-in
DEFAULT_REFRESH_ACCESS_TOKEN_EXPIRY = 60 * 60  # 1 hour: what a refresh issues (the framework's default, kept)
DEFAULT_STATE_DIR = ".oauth-state"

# What happens when a refresh token that was already rotated away is presented again. Within the grace window it is
# taken for a retry or a race and only logged at INFO. Later, "log" logs a WARNING and changes nothing else (the
# default); "revoke" also revokes the live tokens of its family. Either way the request itself is refused.
REUSE_POLICIES = ("log", "revoke")
DEFAULT_REUSE_POLICY = "log"
DEFAULT_REUSE_GRACE_SECONDS = 5 * 60
# How long a starting server waits for another process to let go of the state lock.
DEFAULT_STATE_LOCK_WAIT_SECONDS = 5.0

AUTHORIZATION_PATH = "/authorize"
# What the consent page shows of a client's name: printable characters only, at most this many.
CLIENT_NAME_DISPLAY_CHARS = 100


def _id8(value: object) -> str:
    """The first 8 characters of an id, for a log line: letters, digits, "-" and "_" as they are, anything else as "?",
    so that no id, whatever it holds, can put a newline or markup into the journal."""
    text = "" if value is None else str(value)[:8]
    return "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "?" for ch in text) or "-"


@dataclass(frozen=True)
class _Refusal:
    """A consent request that cannot be served. `reason` is a word from a fixed list (it goes to the journal, never to the
    caller: every caller gets the same page). `outcome` is the log tag: blocked_redirect when the client is registered but
    the redirect address it would be sent to is not acceptable, invalid_request otherwise."""

    reason: str
    outcome: str = "invalid_request"
    client: Optional[str] = None
    redirect: Optional[str] = None


@dataclass(frozen=True)
class _ValidRequest:
    """A consent request that passed every check: the registered client, the redirect address it would be sent to (one
    of the client's registered addresses, and acceptable), and the parameters as the consent form carries them."""

    client: OAuthClientInformationFull
    redirect: AnyUrl
    redirect_explicit: bool
    scopes: Optional[list[str]]
    fields: dict[str, str]  # the OAuth fields that were present and not empty, as sent


def _usable_name(name: Optional[str]) -> str:
    """A client name for the consent page: printable characters only (no control, bidirectional or zero-width characters)
    and not too long. Escaping for HTML is done where it is printed."""
    shown = "".join(ch for ch in (name or "") if ch.isprintable())[:CLIENT_NAME_DISPLAY_CHARS].strip()
    return shown or "(unnamed client)"


class PersonalAuthProvider(InMemoryOAuthProvider):
    """OAuth 2.1 provider for personal/small-team MCP servers.

    Fills the gap between FastMCP's InMemoryOAuthProvider (test-only, no
    persistence, no security) and OAuthProxy (requires Google/GitHub/Auth0).

    Features:
    - Dynamic Client Registration (DCR) for Claude.ai compatibility, open but limited: redirect
      addresses must pass oauth_guard.redirect_problem, the metadata must be keepable (no lone
      surrogate, not nested deeply), 10 registrations per address per hour, 50 per day, 50 stored
      clients (the oldest idle client over a day old makes room); a body that cannot be parsed or
      checked is a clean 400 and stores nothing
    - PKCE support (handled by FastMCP framework)
    - Restrict /authorize to approved redirect addresses only (the same validator, re-applied to
      what is stored)
    - Interactive browser consent page with password gate: the whole request is checked before the
      password is looked at, every invalid request gets the same page, and only a valid one with
      the right password is redirected
    - Constant-time password comparison on NFC UTF-8 bytes, failures counted per client address
      (IPv6 per /64): 5 per address in 15 minutes, 30 overall in an hour
    - Bodies of requests to /authorize, /token, /register and /revoke are capped at 16 KiB,
      whatever the HTTP method; an OPTIONS with a body is refused (a preflight has none)
    - Fails closed: refuses to start without a password or on an unchecked fastmcp or mcp, and
      authorize() works only inside a request the consent endpoint approved
    - Token persistence to a JSON file (survives restarts), read, checked, pruned and
      written atomically by oauth_store.py
    - One issuer of pat_ and prt_ tokens for sign-ins and refreshes; refresh tokens rotate
      within a family, a rotated token leaves only a sha256 tombstone, and /revoke is on
    - An expired access token is refused and nothing else changes: its refresh token stays
      valid, so the client refreshes instead of signing in again
    - Configurable token expiry (default 30 days for a sign-in, 1 hour for a refresh)
    """

    def __init__(
        self,
        base_url: str,
        password: Optional[str] = None,
        allowed_redirect_domains: Optional[list[str]] = None,
        access_token_expiry_seconds: int = DEFAULT_ACCESS_TOKEN_EXPIRY,
        state_dir: Optional[str] = None,
        *,
        refresh_access_token_expiry_seconds: int = DEFAULT_REFRESH_ACCESS_TOKEN_EXPIRY,
        reuse_policy: str = DEFAULT_REUSE_POLICY,
        reuse_grace_seconds: int = DEFAULT_REUSE_GRACE_SECONDS,
        state_lock_wait_seconds: float = DEFAULT_STATE_LOCK_WAIT_SECONDS,
        clock: Optional[Callable[[], float]] = None,
    ):
        """
        Args:
            base_url: Public URL of this server (e.g. "https://my-server.example.com")
            password: The password the consent page asks for. Required: there is no mode without
                one (None or a blank password raises oauth_guard.GateError), because a sign-in
                that nobody has to prove anything for would be open to whoever registers a client.
                A password shorter than 16 characters starts, with a warning.
            allowed_redirect_domains: List of domains allowed in OAuth redirect URIs (a domain and
                its subdomains). None, the default, means ["claude.ai", "claude.com", "localhost"]:
                there is no constructor setting that allows every domain. Whatever the list says,
                an address is accepted only as https, or as http to localhost or a loopback address
                that the list names (see oauth_guard.redirect_problem).
            access_token_expiry_seconds: How long the access token of a sign-in lasts. Default 30 days.
            state_dir: Directory for persisting OAuth state. Default ".oauth-state".
            refresh_access_token_expiry_seconds: How long the access token a refresh issues lasts.
                Default 1 hour. Refresh tokens never expire in this release.
            reuse_policy: "log" (the default) or "revoke": what to do when a refresh token that was
                already rotated away is presented after the grace window (see REUSE_POLICIES).
            reuse_grace_seconds: The grace window for that, default 300 seconds.
            state_lock_wait_seconds: How long to wait for another process to release the state lock
                before refusing to start (oauth_store.StateLockedError).
            clock: Seconds since the epoch, time.time by default. Tests pass a fake one. Only this
                module's own decisions use it; the framework's checks read the real clock.

        Raises:
            oauth_guard.VersionGuardError: fastmcp is not 3.2.x or mcp is not 1.27.x.
            oauth_guard.GateError: no usable password.
            oauth_store.StateFileError: the state file or directory cannot be used (not JSON, wrong
                shape, unknown version, not readable, locked by another process). The file is left
                untouched and the service must not start.
        All of them are raised before anything is changed, and each means the service must not start.
        """
        if reuse_policy not in REUSE_POLICIES:
            raise ValueError(f"reuse_policy must be one of {', '.join(REUSE_POLICIES)}")
        oauth_guard.check_framework_versions()
        password = oauth_guard.require_password(password)
        super().__init__(
            base_url=base_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        )

        self.password = password
        self.allowed_redirect_domains = allowed_redirect_domains if allowed_redirect_domains is not None else [
            "claude.ai", "claude.com", "localhost"
        ]
        self.access_token_expiry_seconds = access_token_expiry_seconds
        self.refresh_access_token_expiry_seconds = refresh_access_token_expiry_seconds
        self.reuse_policy = reuse_policy
        self.reuse_grace_seconds = reuse_grace_seconds
        self._clock: Callable[[], float] = clock or time.time
        self._state_dir = Path(state_dir or DEFAULT_STATE_DIR)

        # Refresh-token families and the tombstones of rotated tokens (see oauth_store.py).
        self._refresh_meta: dict[str, dict[str, Any]] = {}
        self._retired: dict[str, dict[str, Any]] = {}

        # In-memory limiters, keyed on the client address (see oauth_guard): wrong passwords, and registrations.
        self._failure_limiter = oauth_guard.FailureLimiter()
        self._registration_limiter = oauth_guard.RegistrationLimiter()

        # The one writer of the state file: this process holds the lock for as long as it lives, so the operator
        # script (which asks for it before it writes) refuses to run while the service is up.
        self._state_lock = oauth_store.StateLock(self._state_dir, create_dir=True)
        self._state_lock.acquire(timeout=state_lock_wait_seconds)
        try:
            self._load_state()
            # Only once the file has loaded: when it has not, everything is left as it was found, for the operator to look at.
            oauth_store.remove_stale_temp_files(self._state_dir)
        except BaseException:
            self._state_lock.release()
            raise
        if oauth_guard.password_is_short(password):
            # The length itself is not logged.
            logger.warning(
                "the login password is shorter than %d characters; a longer one is much harder to guess",
                oauth_guard.MIN_PASSWORD_CHARS,
            )

    def close(self) -> None:
        """Let go of the state lock. The service never calls this (the lock lives as long as the process); tests
        and scripts that build a second provider on the same directory do."""
        self._state_lock.release()

    # --- State persistence (the file format, the lock and the atomic write are in oauth_store.py) ---

    def _state_file(self) -> Path:
        return self._state_dir / oauth_store.STATE_FILE_NAME

    def _now(self) -> float:
        return self._clock()

    def _state_view(self) -> oauth_store.OAuthState:
        """The live dictionaries as one OAuthState: shared, not copied, so oauth_store's functions change them in place."""
        return oauth_store.OAuthState(
            clients=self.clients,
            access_tokens=self.access_tokens,
            refresh_tokens=self.refresh_tokens,
            access_to_refresh=self._access_to_refresh_map,
            refresh_to_access=self._refresh_to_access_map,
            refresh_meta=self._refresh_meta,
            retired=self._retired,
        )

    def _load_state(self) -> None:
        """Read the state file (a version 1 file is migrated in memory; the file itself is not written here).
        Raises oauth_store.StateFileError for a file that cannot be trusted, which stops the service from starting."""
        result = oauth_store.load_state(self._state_dir, now=self._now())
        state = result.state
        self.clients = state.clients
        self.access_tokens = state.access_tokens
        self.refresh_tokens = state.refresh_tokens
        self._access_to_refresh_map = state.access_to_refresh
        self._refresh_to_access_map = state.refresh_to_access
        self._refresh_meta = state.refresh_meta
        self._retired = state.retired
        # The counts are what the file held; "pruned" is what the prune that follows a load dropped from memory.
        logger.info(
            "oauth state loaded format=%s clients=%d access=%d refresh=%d pruned=%d",
            result.format,
            result.clients,
            result.access_tokens,
            result.refresh_tokens,
            result.pruned.total,
        )

    def _save_state(self) -> None:
        """Prune, then write the whole state atomically. Raises oauth_store.StateFileError if it cannot be written; the
        old file is then untouched."""
        state = self._state_view()
        oauth_store.prune(state, self._now())
        oauth_store.write_state(self._state_dir, state)

    def _snapshot(self) -> tuple[dict[Any, Any], ...]:
        return tuple(
            dict(table)
            for table in (
                self.access_tokens,
                self.refresh_tokens,
                self._access_to_refresh_map,
                self._refresh_to_access_map,
                self._refresh_meta,
                self._retired,
                self.auth_codes,
            )
        )

    def _restore(self, snapshot: tuple[dict[Any, Any], ...]) -> None:
        tables = (
            self.access_tokens,
            self.refresh_tokens,
            self._access_to_refresh_map,
            self._refresh_to_access_map,
            self._refresh_meta,
            self._retired,
            self.auth_codes,
        )
        for table, saved in zip(tables, snapshot):
            table.clear()
            table.update(saved)

    @contextmanager
    def _persisted(self) -> Iterator[None]:
        """Run a change to the token state, then save it. If the change or the save fails, the state in memory goes back
        to what it was, so what a client was told and what the file holds cannot drift apart. Nothing inside may await."""
        snapshot = self._snapshot()
        try:
            yield
            self._save_state()
        except BaseException:
            self._restore(snapshot)
            raise

    # --- Authorization gate ---

    def _is_redirect_allowed(self, redirect_uri: str) -> bool:
        """True if oauth_guard.redirect_problem finds nothing wrong with the address: https on the allowlist, or http to
        loopback; no userinfo, fragment, backslash, whitespace or control character; judged on the normalized form."""
        return oauth_guard.redirect_problem(redirect_uri, self.allowed_redirect_domains) is None

    def _require_safe_to_serve(self) -> None:
        """Checked before any route is built: the framework is a version the gate was checked against, and there is a
        password. Raises oauth_guard.GateError (VersionGuardError for the first)."""
        oauth_guard.check_framework_versions()
        oauth_guard.require_password(self.password)

    # --- Registration: open, but limited ---

    def _registration_refused(self, host: str, reason: str) -> None:
        """Written by oauth_guard.RegistrationGate when it refuses a registration request itself (over a rate limit, a body
        that is not JSON, metadata that cannot be kept); the framework's handler never saw it."""
        logger.warning("register outcome=refused reason=%s ip=%s", oauth_guard.safe(reason), oauth_guard.safe(host))

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Register a client (the framework's registration endpoint calls this) if every redirect address passes
        oauth_guard.redirect_problem, the client can be kept and answered (oauth_guard.client_metadata_problem: no lone
        surrogate in any text, not nested too deeply, serializable) and there is room: at most oauth_guard.MAX_CLIENTS
        clients are kept, and when the table is full the oldest client that holds no token and is over a day old makes
        room. Raises RegistrationError (HTTP 400) otherwise, and nothing is stored or changed then, or if the state cannot
        be saved. The checks come before anything is touched: the framework writes its 201 answer after this returns, and a
        client whose answer cannot be written must not be left in the table and the file."""
        ip = oauth_guard.safe(oauth_guard.REQUEST_IP.get())
        found = oauth_guard.registration_problem(client_info.redirect_uris, self.allowed_redirect_domains)
        if found is not None:
            reason, host = found
            logger.warning("register outcome=refused reason=redirect_%s redirect=%s ip=%s", reason, host, ip)
            raise RegistrationError(
                "invalid_redirect_uri",
                "redirect_uris must be https addresses on an allowed domain, or http addresses on localhost",
            )
        unkeepable = oauth_guard.client_metadata_problem(client_info)
        if unkeepable is not None:
            logger.warning("register outcome=refused reason=metadata_%s ip=%s", oauth_guard.safe(unkeepable), ip)
            raise RegistrationError("invalid_client_metadata", oauth_guard.METADATA_REFUSED_DESCRIPTION)
        before = dict(self.clients)
        evicted: list[str] = []
        try:
            # Until there is room for one more (more than one goes if the table was loaded over the cap).
            while client_info.client_id not in self.clients and len(self.clients) >= oauth_guard.MAX_CLIENTS:
                state = self._state_view()
                victim = oauth_guard.idle_client_to_evict(self.clients, oauth_store.clients_with_tokens(state), self._now())
                removed = oauth_store.remove_tokenless_clients(state, [victim]) if victim is not None else []
                if not removed:
                    logger.warning("register outcome=refused reason=client_cap ip=%s", ip)
                    raise RegistrationError("invalid_client_metadata", "Too many clients are registered. Try again later.")
                evicted += removed
            await super().register_client(client_info)
            self._save_state()
        except BaseException:
            self.clients.clear()
            self.clients.update(before)
            raise
        logger.info(
            'register outcome=registered client=%s name="%s" ip=%s evicted=%d',
            _id8(client_info.client_id), oauth_guard.safe(client_info.client_name), ip, len(evicted),
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Issue an authorization code and return the redirect address that carries it.

        Works only inside a request that the consent endpoint approved for exactly this client and redirect address, after
        the password matched (oauth_guard.approved / take_approval). Called from anywhere else, which can only happen if
        the framework's own /authorize handler ever got into the path, it raises access_denied: the password page is not
        the only thing standing between a client and a code. The redirect address is checked once more."""
        redirect = str(params.redirect_uri) if params.redirect_uri else ""
        if not oauth_guard.take_approval(str(client.client_id), redirect):
            logger.error("authorize outcome=invalid_request reason=no_consent client=%s", _id8(client.client_id))
            raise AuthorizeError(
                error="access_denied",
                error_description="Authorization requires the consent page.",
            )
        problem = oauth_guard.redirect_problem(redirect, self.allowed_redirect_domains)
        if problem is not None:
            logger.warning(
                "authorize outcome=blocked_redirect reason=%s client=%s redirect=%s",
                oauth_guard.safe(problem), _id8(client.client_id), oauth_guard.safe_host(redirect),
            )
            raise AuthorizeError(
                error="access_denied",
                error_description="Redirect URI not allowed.",
            )

        # The code and the save go together: if the state cannot be saved, the code is taken back. (The framework's authorize
        # never suspends, so nothing else runs in between, as _persisted() requires.) Not written with `with
        # self._persisted()`: that is a contextmanager, which cannot let the framework's own AuthorizeError (a frozen
        # dataclass) pass through.
        snapshot = self._snapshot()
        try:
            result = await super().authorize(client, params)
            self._save_state()
        except BaseException:
            self._restore(snapshot)
            raise
        return result

    # --- Tokens: one issuer for sign-ins and refreshes, rotation within a family, revocation ---

    def _issue_pair(
        self,
        client_id: str,
        scopes: list[str],
        *,
        family: Optional[str],
        origin: str,
        access_lifetime: int,
    ) -> tuple[OAuthToken, str]:
        """Mint an access token (pat_) and a refresh token (prt_), store and pair them, and record the refresh token's
        family. The one place either kind is created. Does not save: callers do that inside _persisted()."""
        now = self._now()
        access_value = f"pat_{secrets.token_hex(32)}"
        refresh_value = f"prt_{secrets.token_hex(32)}"
        family = family or oauth_store.new_family_id()
        self.access_tokens[access_value] = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=list(scopes),
            expires_at=int(now + access_lifetime),
        )
        self.refresh_tokens[refresh_value] = RefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=list(scopes),
            expires_at=None,
        )
        self._access_to_refresh_map[access_value] = refresh_value
        self._refresh_to_access_map[refresh_value] = access_value
        self._refresh_meta[refresh_value] = {"family": family, "issued_at": int(now), "origin": origin}
        token = OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=access_lifetime,
            refresh_token=refresh_value,
            scope=" ".join(scopes),
        )
        return token, family

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")
        if authorization_code.code not in self.auth_codes:
            raise TokenError("invalid_grant", "Authorization code not found or already used.")
        with self._persisted():
            del self.auth_codes[authorization_code.code]
            token, family = self._issue_pair(
                client.client_id,
                authorization_code.scopes,
                family=None,
                origin=oauth_store.ORIGIN_CODE,
                access_lifetime=self.access_token_expiry_seconds,
            )
        logger.info("token outcome=issued client=%s family=%s", _id8(client.client_id), _id8(family))
        return token

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        """The access token if it is known and has not expired. An expired one is refused and NOTHING else changes: its
        refresh token stays valid, so the client refreshes instead of signing in again. (The framework's version deleted
        the access token together with its refresh token, which forced a new sign-in.) The
        prune that runs at every save removes it once it has been expired for a week. The refusal is logged ("access
        outcome=expired"), except inside a request to /revoke, where the framework only looks the token up."""
        record = self.access_tokens.get(token)
        if record is None:
            return None
        if record.expires_at is not None and record.expires_at < self._now():
            if not oauth_guard.revoking():  # /revoke only asks whose the token is: that is not a session presenting it
                logger.info("access outcome=expired client=%s", _id8(record.client_id))
            return None
        return record

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        """The refresh token if it is live, belongs to this client and has not expired (refresh tokens do not expire in
        this release). A string that is not live but is a rotated-away token is a reuse: see _note_reuse (which does
        nothing inside a request to /revoke). Either way nothing is deleted here."""
        record = self.refresh_tokens.get(refresh_token)
        if record is None:
            self._note_reuse(client, refresh_token)
            return None
        if record.client_id != client.client_id:
            return None
        if record.expires_at is not None and record.expires_at < self._now():
            return None
        return record

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate: the refresh token and its access token are retired (a sha256 tombstone stays) and a new pair of the
        same family is issued, with an access token that lasts refresh_access_token_expiry_seconds."""
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")
        # What counts is the token as stored, not the object the caller holds: the token handler loads it just before,
        # but a direct caller (a test, a script) could hold a stale copy.
        live = self.refresh_tokens.get(refresh_token.token)
        if live is None:
            self._note_reuse(client, refresh_token.token)
            raise TokenError("invalid_grant", "refresh token does not exist")
        if live.client_id != client.client_id:
            raise TokenError("invalid_grant", "refresh token does not exist")
        if live.expires_at is not None and live.expires_at < self._now():
            raise TokenError("invalid_grant", "refresh token has expired")
        if not set(scopes).issubset(live.scopes):
            raise TokenError("invalid_scope", "Requested scopes exceed those authorized by the refresh token.")
        with self._persisted():
            family = oauth_store.retire_refresh_token(self._state_view(), live.token, self._now())
            token, _ = self._issue_pair(
                client.client_id,
                scopes,
                family=family,
                origin=oauth_store.ORIGIN_REFRESH,
                access_lifetime=self.refresh_access_token_expiry_seconds,
            )
        logger.info("token outcome=refreshed client=%s family=%s", _id8(client.client_id), _id8(family))
        return token

    def _note_reuse(self, client: OAuthClientInformationFull, presented: str) -> None:
        """Called when a refresh token string that is not live is presented. If its sha256 is a tombstone it was rotated
        away and is being used again: within the grace window that is logged at INFO (a retry or a race), later at
        WARNING, and under the "revoke" policy the live tokens of its family are revoked too. The caller refuses the
        request in every case. A string that is not a tombstone (never issued, or revoked, or pruned) is just unknown.

        Never inside a request to /revoke (oauth_guard.revoking()): the framework's revocation handler loads the token it
        was given as a refresh token to find out whose it is, and a client that revokes a token it has already rotated
        away is cleaning up, not replaying it. Taking that for a reuse would log a theft signal that is not one and,
        under the "revoke" policy, end the session of the very client that asked."""
        if oauth_guard.revoking():
            return
        entry = self._retired.get(oauth_store.token_digest(presented))
        if entry is None:
            return
        family = str(entry["family"])
        age = int(self._now() - entry["retired_at"])
        if age <= self.reuse_grace_seconds:
            logger.info(
                "token outcome=refresh_reuse_in_grace client=%s family=%s age=%ds",
                _id8(client.client_id), _id8(family), age,
            )
            return
        logger.warning(
            "token outcome=refresh_reuse_detected client=%s family=%s age=%ds policy=%s",
            _id8(client.client_id), _id8(family), age, self.reuse_policy,
        )
        if self.reuse_policy == "revoke":
            self._revoke_family(family, client_id=client.client_id, reason="reuse")

    def _revoke_family(self, family: str, *, client_id: Optional[str], reason: str) -> None:
        """Remove the live tokens of a family. The removal in memory stands even if the save fails (revoking is the safe
        direction); the failure is logged, and the next save writes it."""
        access, refresh = oauth_store.revoke_family(self._state_view(), family)
        if not (access or refresh):
            return
        logger.warning(
            "revoke outcome=revoked client=%s family=%s reason=%s access=%d refresh=%d",
            _id8(client_id), _id8(family), reason, access, refresh,
        )
        try:
            self._save_state()
        except oauth_store.StateFileError as exc:
            logger.error("oauth state: the revocation could not be saved (%s)", exc)

    async def revoke_token(self, token: Any) -> None:
        """Revoke a token and its counterpart: the live tokens of its family (the SDK's /revoke handler calls this for a
        token it loaded for an authenticated client). The removal in memory stands even if the save then fails, and the
        failure is raised, so /revoke does not claim a success that the file does not hold."""
        state = self._state_view()
        now = self._now()
        if isinstance(token, RefreshToken):
            refresh_values = [token.token] if token.token in self.refresh_tokens else []
        elif isinstance(token, AccessToken):
            refresh_values = oauth_store.paired_refresh_tokens(state, token.token)
        else:
            return
        families = sorted({oauth_store.ensure_family(state, value, now) for value in refresh_values})
        access = refresh = 0
        for family in families:
            gone_access, gone_refresh = oauth_store.revoke_family(state, family)
            access += gone_access
            refresh += gone_refresh
        if isinstance(token, AccessToken):
            access += oauth_store.remove_access_token(state, token.token)
        if not (access or refresh):
            return
        logger.info(
            "revoke outcome=revoked client=%s family=%s access=%d refresh=%d",
            _id8(token.client_id), _id8(families[0] if families else None), access, refresh,
        )
        self._save_state()

    # --- HTTP routes: /authorize replaced by the consent endpoint, bodies capped, registrations limited ---

    def get_routes(self, mcp_path: Optional[str] = None) -> list[Route]:
        """The framework's OAuth routes with the gate built in.

        /authorize is replaced by the consent endpoint. /token, /register and /revoke keep the framework's handlers but
        get the 16 KiB body cap, for every HTTP method (the routes answer OPTIONS as well as POST, and an OPTIONS that
        carries a body is refused); /register also gets the registration limiter and the check of its body, /token and
        /revoke the check that a multipart body can be read, and /revoke the mark that tells the token loaders they are only
        looking a token up. /mcp is not one of these routes and is never
        capped. Raises oauth_guard.GateError unless the framework produced exactly one route for each of the four paths,
        and VersionGuardError or GateError if the framework version or the password is not what the gate needs: a route
        that went missing or changed shape would otherwise leave the password page, or a cap, out of the request path
        without a single error."""
        self._require_safe_to_serve()
        found = super().get_routes(mcp_path)
        for path in oauth_guard.BODY_LIMITED_PATHS:
            count = sum(1 for route in found if isinstance(route, Route) and route.path == path)
            if count != 1:
                raise GateError(
                    f"expected exactly one {path} route from the OAuth framework and found {count}; "
                    "refusing to serve a gate that cannot be placed in front of it"
                )
        routes = [
            self._guarded(route) if isinstance(route, Route) and route.path in oauth_guard.BODY_LIMITED_PATHS else route
            for route in found
        ]
        ours = [
            route for route in routes
            if getattr(route, "path", None) == AUTHORIZATION_PATH and isinstance(getattr(route, "endpoint", None), oauth_guard.BodyLimit)
        ]
        if len(ours) != 1 or sum(1 for route in routes if getattr(route, "path", None) == AUTHORIZATION_PATH) != 1:
            raise GateError("the consent endpoint is not the one /authorize route; refusing to serve")
        return routes

    def _guarded(self, route: Route) -> Route:
        """The route with its body cap on (for /register also the gate with the limiter; for /token and /revoke the form
        guard, and for /revoke also the revocation mark); /authorize is replaced altogether. The cap is the outermost
        wrapper, so that nothing, the gate included, ever reads a body that is over it, and it holds for every method the
        route answers."""
        path = route.path
        if path == AUTHORIZATION_PATH:
            consent = oauth_guard.BodyLimit(
                request_response(self._make_authorize_endpoint()), path=path, refuse=oauth_guard.consent_refusal
            )
            return Route(path, endpoint=consent, methods=["GET", "POST"])
        app = route.endpoint
        if path == "/register":
            app = oauth_guard.RegistrationGate(
                app, limiter=self._registration_limiter, clock=self._now, on_refused=self._registration_refused
            )
        elif path in ("/token", "/revoke"):
            app = oauth_guard.FormGuard(app, path=path)  # these handlers read a form: a multipart body that does not parse is a 400
            if path == "/revoke":
                app = oauth_guard.RevocationMark(app)
        capped = oauth_guard.BodyLimit(app, path=path, refuse=oauth_guard.json_refusal)
        return Route(path, endpoint=capped, methods=sorted(route.methods or ()), name=route.name)

    def _make_authorize_endpoint(self) -> Callable[[Request], Any]:
        """Build the Starlette endpoint of /authorize.

        GET shows the consent page for a valid request. POST takes the form in this order: content type (a form, else
        415), the fields (all text, none too long, else 400), the whole request (registered client, registered and
        acceptable redirect address, response_type code, a PKCE challenge, a scope the client has, else 400), the deny
        button (a redirect to the registered address with access_denied), the failure limiter (else 429), and only then the
        password. Every invalid request gets the same 400 page whatever the password was and the password is not looked at
        for it, so the page cannot be used to test a guess and an invalid request does not count against the limiter.
        Only a valid request with the right password is redirected, to the address the client registered, by authorize()."""

        async def endpoint(request: Request) -> Response:
            ip = request.client.host if request.client else "?"
            try:
                if request.method == "POST":
                    return await self._consent_post(request, ip)
                return self._consent_get(request, ip)
            except Exception as exc:  # last resort: no traceback and no detail for the caller, a line for the journal
                logger.error("authorize outcome=error ip=%s error=%s", oauth_guard.safe(ip), type(exc).__name__)
                return oauth_guard.server_error_response()

        return endpoint

    def _refuse(self, refusal: _Refusal, ip: str) -> Response:
        """Log a request that cannot be served and answer it with the one 400 page."""
        level = logging.WARNING if refusal.outcome == "blocked_redirect" else logging.INFO
        if refusal.redirect is not None:
            logger.log(
                level, "authorize outcome=%s reason=%s client=%s redirect=%s ip=%s",
                refusal.outcome, oauth_guard.safe(refusal.reason), _id8(refusal.client),
                oauth_guard.safe_host(refusal.redirect), oauth_guard.safe(ip),
            )
        else:
            logger.log(
                level, "authorize outcome=%s reason=%s client=%s ip=%s",
                refusal.outcome, oauth_guard.safe(refusal.reason), _id8(refusal.client), oauth_guard.safe(ip),
            )
        return oauth_guard.invalid_request_response()

    def _check_request(self, fields: dict[str, str]) -> "_ValidRequest | _Refusal":
        """Everything about an authorization request that can be checked without the password. A field that is empty counts
        as absent, which is how the consent form has always carried the optional ones."""
        sent = {key: fields[key] for key in oauth_guard.OAUTH_FIELDS if fields.get(key)}
        client_id = sent.get("client_id")
        if client_id is None:
            return _Refusal("no_client_id")
        client = self.clients.get(client_id)
        if client is None:
            return _Refusal("unknown_client", client=client_id)
        if sent.get("response_type") != "code":
            return _Refusal("response_type", client=client_id)
        challenge = sent.get("code_challenge")
        if challenge is None or not oauth_guard.PKCE_CHALLENGE.fullmatch(challenge):
            return _Refusal("code_challenge", client=client_id)
        # The framework treats a missing method as S256, which is the only one it checks at /token; "plain" is refused.
        if sent.get("code_challenge_method", "S256") != "S256":
            return _Refusal("code_challenge_method", client=client_id)
        requested: Optional[AnyUrl] = None
        if "redirect_uri" in sent:
            raw = sent["redirect_uri"]
            if oauth_guard.text_problem(raw) is not None:
                return _Refusal("redirect_text", client=client_id, redirect=raw)
            try:
                requested = AnyUrl(raw)
            except ValueError:  # pydantic's ValidationError is a ValueError
                return _Refusal("redirect_unparseable", client=client_id)
        try:
            redirect = client.validate_redirect_uri(requested)
        except InvalidRedirectUriError:
            return _Refusal("redirect_not_registered", client=client_id)
        # The address is registered. It must also be acceptable now: a client registered before the validator existed can
        # hold any address, and this is the only thing between it and a redirect to it.
        problem = oauth_guard.redirect_problem(str(redirect), self.allowed_redirect_domains)
        if problem is not None:
            return _Refusal(problem, outcome="blocked_redirect", client=client_id, redirect=str(redirect))
        try:
            scopes = client.validate_scope(sent.get("scope"))
        except InvalidScopeError:
            return _Refusal("scope", client=client_id)
        return _ValidRequest(client=client, redirect=redirect, redirect_explicit=requested is not None, scopes=scopes, fields=sent)

    def _consent_get(self, request: Request, ip: str) -> Response:
        fields, problem = oauth_guard.read_fields(request.query_params.multi_items())
        if problem is not None:
            return self._refuse(_Refusal(problem), ip)
        checked = self._check_request(fields)
        if isinstance(checked, _Refusal):
            return self._refuse(checked, ip)
        logger.info(
            'authorize outcome=consent_shown client=%s name="%s" ip=%s',
            _id8(checked.client.client_id), oauth_guard.safe(checked.client.client_name), oauth_guard.safe(ip),
        )
        return self._render_consent_page(checked)

    async def _consent_post(self, request: Request, ip: str) -> Response:
        if not oauth_guard.is_form_content_type(request.headers.get("content-type")):
            logger.info("authorize outcome=invalid_request reason=content_type client=- ip=%s", oauth_guard.safe(ip))
            return oauth_guard.unsupported_media_response()
        try:
            form = await request.form(max_files=1, max_fields=64, max_part_size=oauth_guard.MAX_BODY_BYTES)
        except Exception:  # a multipart body that does not parse (the framework raises its own errors for it)
            return self._refuse(_Refusal("form"), ip)
        try:
            return await self._decide(form, ip)
        finally:
            await form.close()  # a file part, if there was one, is closed

    async def _decide(self, form: Any, ip: str) -> Response:
        fields, problem = oauth_guard.read_fields(form.multi_items())
        if problem is not None:
            return self._refuse(_Refusal(problem), ip)
        checked = self._check_request(fields)
        if isinstance(checked, _Refusal):
            return self._refuse(checked, ip)
        client_id, name = _id8(checked.client.client_id), oauth_guard.safe(checked.client.client_name)

        if fields.get("decision", "").strip() == "deny":
            logger.info('authorize outcome=denied_by_user client=%s name="%s" ip=%s', client_id, name, oauth_guard.safe(ip))
            return self._deny_response(checked)

        now = self._now()
        blocked = self._failure_limiter.block(ip, now)
        if blocked is not None:
            # scope=ip: this address had its five wrong passwords. scope=global: thirty were made from all addresses in the
            # hour, so everybody, the operator included, is refused until the oldest of them is an hour old (retry_in).
            logger.warning(
                'authorize outcome=rate_limited scope=%s retry_in=%ds client=%s name="%s" ip=%s',
                blocked.scope, blocked.retry_in, client_id, name, oauth_guard.safe(ip),
            )
            return oauth_guard.too_many_attempts_response()

        if not oauth_guard.password_matches(fields.get("password", ""), self.password):
            self._failure_limiter.record(ip, now)
            logger.warning('authorize outcome=wrong_password client=%s name="%s" ip=%s', client_id, name, oauth_guard.safe(ip))
            return self._render_consent_page(checked, error="Incorrect password. Please try again.")

        params = AuthorizationParams(
            state=checked.fields.get("state"),
            scopes=checked.scopes,
            code_challenge=checked.fields["code_challenge"],
            redirect_uri=checked.redirect,
            redirect_uri_provided_explicitly=checked.redirect_explicit,
            resource=checked.fields.get("resource"),
        )
        try:
            with oauth_guard.approved(str(checked.client.client_id), str(checked.redirect)):
                url = await self.authorize(checked.client, params)
        except AuthorizeError as exc:
            logger.warning(
                "authorize outcome=invalid_request reason=authorize_%s client=%s ip=%s",
                oauth_guard.safe(exc.error), client_id, oauth_guard.safe(ip),
            )
            return oauth_guard.invalid_request_response()
        self._failure_limiter.reset(ip)
        logger.info('authorize outcome=approved client=%s name="%s" ip=%s', client_id, name, oauth_guard.safe(ip))
        return RedirectResponse(url, status_code=302, headers=oauth_guard.consent_headers())

    def _render_consent_page(self, valid: _ValidRequest, error: Optional[str] = None) -> HTMLResponse:
        """The consent page for a request that passed _check_request, from the checked values (never from the raw request).
        Every value that is printed is escaped; the page carries no script."""

        def h(value: object) -> str:
            return html_lib.escape("" if value is None else str(value), quote=True)

        hidden_html = "\n".join(
            f'<input type="hidden" name="{h(field)}" value="{h(valid.fields[field])}">'
            for field in oauth_guard.OAUTH_FIELDS
            if field in valid.fields
        )
        error_html = f'<div class="err">{h(error)}</div>' if error else ""
        client_name = _usable_name(valid.client.client_name)
        scopes_display = " ".join(valid.scopes) if valid.scopes else "(none)"

        body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize {h(client_name)}</title>
<style>
{oauth_guard.PAGE_STYLE}
</style>
</head>
<body>
<div class="card">
<h1>Authorize MCP Access</h1>
{error_html}
<dl>
<dt>Client</dt><dd>{h(client_name)}</dd>
<dt>Client ID</dt><dd>{h(valid.client.client_id)}</dd>
<dt>Redirect URI</dt><dd>{h(valid.redirect)}</dd>
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
        return HTMLResponse(body, headers=oauth_guard.consent_headers())

    def _deny_response(self, valid: _ValidRequest) -> Response:
        """The OAuth error redirect for a user who pressed Deny. The request has passed _check_request, so the address is
        one the client registered and one that is acceptable: the deny button cannot be turned into an open redirect."""
        url = construct_redirect_uri(
            str(valid.redirect),
            error="access_denied",
            error_description="User denied authorization",
            state=valid.fields.get("state"),
        )
        return RedirectResponse(url, status_code=302, headers=oauth_guard.consent_headers())
