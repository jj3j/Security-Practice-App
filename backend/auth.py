from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from learning_db import (
    LearnerSession,
    LearningAuthorizationError,
    LearningRepository,
)


GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_ID_TOKEN_CLOCK_SKEW_SECONDS = 60
SESSION_COOKIE_NAME = "__Host-gdsa_session"
CSRF_COOKIE_NAME = "__Host-gdsa_csrf"
LOGIN_COOKIE_NAME = "__Host-gdsa_login"


class AuthenticationFailureCategory(str, Enum):
    CONFIGURATION_INVALID = "configuration_invalid"
    REQUEST_INVALID = "request_invalid"
    STATE_INVALID = "state_invalid"
    GOOGLE_AUTHORIZATION_DENIED = "google_authorization_denied"
    GOOGLE_TOKEN_EXCHANGE_REJECTED = "google_token_exchange_rejected"
    GOOGLE_TOKEN_EXCHANGE_UNAVAILABLE = "google_token_exchange_unavailable"
    GOOGLE_TOKEN_RESPONSE_INVALID = "google_token_response_invalid"
    GOOGLE_DEPENDENCY_UNAVAILABLE = "google_dependency_unavailable"
    GOOGLE_ID_TOKEN_INVALID = "google_id_token_invalid"
    GOOGLE_ID_TOKEN_TRANSPORT_FAILED = "google_id_token_transport_failed"
    GOOGLE_ID_TOKEN_MALFORMED = "google_id_token_malformed"
    GOOGLE_ID_TOKEN_VALUE_INVALID = "google_id_token_value_invalid"
    GOOGLE_ID_TOKEN_AUDIENCE_INVALID = "google_id_token_audience_invalid"
    GOOGLE_ID_TOKEN_TIME_INVALID = "google_id_token_time_invalid"
    GOOGLE_ID_TOKEN_ALGORITHM_UNSUPPORTED = "google_id_token_algorithm_unsupported"
    GOOGLE_ISSUER_INVALID = "google_issuer_invalid"
    GOOGLE_SUBJECT_INVALID = "google_subject_invalid"
    GOOGLE_NONCE_INVALID = "google_nonce_invalid"


class AuthenticationError(RuntimeError):
    """Raised when an authentication operation cannot be completed safely."""

    def __init__(
        self,
        message: str,
        *,
        category: AuthenticationFailureCategory = AuthenticationFailureCategory.REQUEST_INVALID,
    ) -> None:
        super().__init__(message)
        self.category = category


class AuthenticationDenied(PermissionError):
    """Raised when a validated identity has not been approved."""


def _id_token_value_failure_category(error: ValueError) -> AuthenticationFailureCategory:
    message = str(error)
    if message.startswith("Token has wrong audience"):
        return AuthenticationFailureCategory.GOOGLE_ID_TOKEN_AUDIENCE_INVALID
    if message.startswith(("Token used too early", "Token expired")):
        return AuthenticationFailureCategory.GOOGLE_ID_TOKEN_TIME_INVALID
    if message.startswith(("Unsupported signature algorithm", "The key algorithm")):
        return AuthenticationFailureCategory.GOOGLE_ID_TOKEN_ALGORITHM_UNSUPPORTED
    if message.startswith("Wrong issuer"):
        return AuthenticationFailureCategory.GOOGLE_ISSUER_INVALID
    return AuthenticationFailureCategory.GOOGLE_ID_TOKEN_VALUE_INVALID


@dataclass(frozen=True)
class OidcIdentity:
    issuer: str
    subject: str
    email: str | None
    display_name: str | None


@dataclass(frozen=True)
class AuthenticationConfig:
    client_id: str
    client_secret: str
    public_base_url: str
    redirect_uri: str
    session_idle_seconds: int
    session_lifetime_seconds: int
    http_timeout_seconds: float

    @property
    def origin(self) -> str:
        parsed = urllib.parse.urlsplit(self.public_base_url)
        return f"{parsed.scheme}://{parsed.netloc}"


class OidcClient(Protocol):
    def authorization_url(self, *, state: str, nonce: str, code_verifier: str) -> str: ...

    def exchange_code(self, *, code: str, nonce: str, code_verifier: str) -> OidcIdentity: ...


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AuthenticationError(
            f"{name} is required.",
            category=AuthenticationFailureCategory.CONFIGURATION_INVALID,
        )
    return value


def _integer_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.environ.get(name, str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise AuthenticationError(
            f"{name} must be an integer.",
            category=AuthenticationFailureCategory.CONFIGURATION_INVALID,
        ) from exc
    if parsed < minimum or parsed > maximum:
        raise AuthenticationError(
            f"{name} must be between {minimum} and {maximum}.",
            category=AuthenticationFailureCategory.CONFIGURATION_INVALID,
        )
    return parsed


def load_authentication_config() -> AuthenticationConfig:
    public_base_url = _required_env("GDSA_PUBLIC_BASE_URL").rstrip("/")
    parsed = urllib.parse.urlsplit(public_base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        raise AuthenticationError(
            "GDSA_PUBLIC_BASE_URL must be an HTTPS origin without a path.",
            category=AuthenticationFailureCategory.CONFIGURATION_INVALID,
        )
    idle_seconds = _integer_env(
        "GDSA_SESSION_IDLE_SECONDS", 3600, minimum=300, maximum=86400
    )
    lifetime_seconds = _integer_env(
        "GDSA_SESSION_LIFETIME_SECONDS", 43200, minimum=idle_seconds, maximum=604800
    )
    timeout_seconds = _integer_env(
        "GDSA_OIDC_HTTP_TIMEOUT_SECONDS", 10, minimum=2, maximum=30
    )
    return AuthenticationConfig(
        client_id=_required_env("GDSA_GOOGLE_CLIENT_ID"),
        client_secret=_required_env("GDSA_GOOGLE_CLIENT_SECRET"),
        public_base_url=public_base_url,
        redirect_uri=f"{public_base_url}/auth/callback",
        session_idle_seconds=idle_seconds,
        session_lifetime_seconds=lifetime_seconds,
        http_timeout_seconds=float(timeout_seconds),
    )


class GoogleOidcClient:
    def __init__(self, config: AuthenticationConfig) -> None:
        self.config = config

    @staticmethod
    def _code_challenge(code_verifier: str) -> str:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def authorization_url(self, *, state: str, nonce: str, code_verifier: str) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": self.config.client_id,
                "response_type": "code",
                "scope": "openid email profile",
                "redirect_uri": self.config.redirect_uri,
                "state": state,
                "nonce": nonce,
                "code_challenge": self._code_challenge(code_verifier),
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"

    def exchange_code(self, *, code: str, nonce: str, code_verifier: str) -> OidcIdentity:
        body = urllib.parse.urlencode(
            {
                "code": code,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "redirect_uri": self.config.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            }
        ).encode("ascii")
        request = urllib.request.Request(
            GOOGLE_TOKEN_ENDPOINT,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.http_timeout_seconds) as response:
                if response.status != 200:
                    raise AuthenticationError(
                        "Google token exchange was rejected.",
                        category=AuthenticationFailureCategory.GOOGLE_TOKEN_EXCHANGE_REJECTED,
                    )
                response_body = response.read(64 * 1024)
        except AuthenticationError:
            raise
        except urllib.error.HTTPError as exc:
            raise AuthenticationError(
                "Google token exchange was rejected.",
                category=AuthenticationFailureCategory.GOOGLE_TOKEN_EXCHANGE_REJECTED,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AuthenticationError(
                "Google token exchange was unavailable.",
                category=AuthenticationFailureCategory.GOOGLE_TOKEN_EXCHANGE_UNAVAILABLE,
            ) from exc
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthenticationError(
                "Google token response was invalid.",
                category=AuthenticationFailureCategory.GOOGLE_TOKEN_RESPONSE_INVALID,
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("id_token"), str):
            raise AuthenticationError(
                "Google token response did not include a valid ID token.",
                category=AuthenticationFailureCategory.GOOGLE_TOKEN_RESPONSE_INVALID,
            )

        try:
            from google.auth.exceptions import (
                GoogleAuthError,
                InvalidValue,
                MalformedError,
                TransportError,
            )
            from google.auth.transport.requests import Request
            from google.oauth2 import id_token
            import requests
            from requests.exceptions import RequestException
        except ImportError as exc:
            raise AuthenticationError(
                "Google authentication dependencies are unavailable.",
                category=AuthenticationFailureCategory.GOOGLE_DEPENDENCY_UNAVAILABLE,
            ) from exc
        try:
            self_timeout = self.config.http_timeout_seconds

            class TimeoutSession(requests.Session):
                def request(self, method: str, url: str, **kwargs: Any) -> Any:
                    kwargs.setdefault("timeout", self_timeout)
                    return super().request(method, url, **kwargs)

            claims = id_token.verify_oauth2_token(
                payload["id_token"],
                Request(session=TimeoutSession()),
                self.config.client_id,
                clock_skew_in_seconds=GOOGLE_ID_TOKEN_CLOCK_SKEW_SECONDS,
            )
        except (TransportError, RequestException) as exc:
            raise AuthenticationError(
                "Google ID token verification transport failed.",
                category=AuthenticationFailureCategory.GOOGLE_ID_TOKEN_TRANSPORT_FAILED,
            ) from exc
        except MalformedError as exc:
            raise AuthenticationError(
                "Google ID token was malformed.",
                category=AuthenticationFailureCategory.GOOGLE_ID_TOKEN_MALFORMED,
            ) from exc
        except (InvalidValue, ValueError) as exc:
            raise AuthenticationError(
                "Google ID token value validation failed.",
                category=_id_token_value_failure_category(exc),
            ) from exc
        except GoogleAuthError as exc:
            raise AuthenticationError(
                "Google ID token validation failed.",
                category=AuthenticationFailureCategory.GOOGLE_ID_TOKEN_INVALID,
            ) from exc

        if not isinstance(claims, dict):
            raise AuthenticationError(
                "Google ID token validation failed.",
                category=AuthenticationFailureCategory.GOOGLE_ID_TOKEN_INVALID,
            )

        issuer = claims.get("iss")
        subject = claims.get("sub")
        token_nonce = claims.get("nonce")
        if issuer != GOOGLE_ISSUER:
            raise AuthenticationError(
                "Google ID token issuer is invalid.",
                category=AuthenticationFailureCategory.GOOGLE_ISSUER_INVALID,
            )
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise AuthenticationError(
                "Google ID token subject is invalid.",
                category=AuthenticationFailureCategory.GOOGLE_SUBJECT_INVALID,
            )
        if not isinstance(token_nonce, str) or not secrets.compare_digest(token_nonce, nonce):
            raise AuthenticationError(
                "Google ID token nonce is invalid.",
                category=AuthenticationFailureCategory.GOOGLE_NONCE_INVALID,
            )

        email_value = claims.get("email")
        email = email_value.strip() if isinstance(email_value, str) and email_value.strip() else None
        if email is not None and claims.get("email_verified") is not True:
            email = None
        name_value = claims.get("name")
        display_name = (
            name_value.strip() if isinstance(name_value, str) and name_value.strip() else None
        )
        return OidcIdentity(
            issuer=issuer,
            subject=subject,
            email=email,
            display_name=display_name,
        )


class AuthenticationService:
    def __init__(
        self,
        repository: LearningRepository,
        config: AuthenticationConfig,
        oidc_client: OidcClient | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.oidc_client = oidc_client or GoogleOidcClient(config)

    @classmethod
    def from_env(cls) -> "AuthenticationService":
        database_value = _required_env("GDSA_LEARNER_DATABASE_PATH")
        repository = LearningRepository(Path(database_value))
        repository.initialize()
        return cls(repository, load_authentication_config())

    def start_login(self, return_path: str = "/") -> tuple[str, str]:
        state, nonce, code_verifier = self.repository.create_login_transaction(
            return_path=return_path
        )
        return (
            self.oidc_client.authorization_url(
                state=state,
                nonce=nonce,
                code_verifier=code_verifier,
            ),
            state,
        )

    def complete_login(
        self,
        *,
        state: str,
        browser_state: str,
        code: str,
    ) -> tuple[str, str, str]:
        if not browser_state or not secrets.compare_digest(state, browser_state):
            raise AuthenticationError(
                "Login state does not match the initiating browser.",
                category=AuthenticationFailureCategory.STATE_INVALID,
            )
        transaction = self.repository.consume_login_transaction(state)
        if transaction is None:
            raise AuthenticationError(
                "Login state is invalid, expired, or already used.",
                category=AuthenticationFailureCategory.STATE_INVALID,
            )
        identity = self.oidc_client.exchange_code(
            code=code,
            nonce=transaction.nonce,
            code_verifier=transaction.code_verifier,
        )
        try:
            learner = self.repository.authenticate_approved_identity(
                identity.issuer,
                identity.subject,
                email=identity.email,
                display_name=identity.display_name,
            )
        except LearningAuthorizationError as exc:
            self.repository.record_pending_identity(
                identity.issuer,
                identity.subject,
                email=identity.email,
                display_name=identity.display_name,
            )
            raise AuthenticationDenied(
                "This Google identity is awaiting administrator approval."
            ) from exc
        session_token, csrf_token = self.repository.create_session(
            learner.user_id,
            idle_seconds=self.config.session_idle_seconds,
            lifetime_seconds=self.config.session_lifetime_seconds,
        )
        return session_token, csrf_token, transaction.return_path

    def session(self, session_token: str, *, csrf_token: str | None = None) -> LearnerSession | None:
        return self.repository.get_session(
            session_token,
            idle_seconds=self.config.session_idle_seconds,
            csrf_token=csrf_token,
        )

    def logout(self, session_token: str) -> None:
        self.repository.revoke_session(session_token)


def parse_cookie_header(value: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for item in value.split(";"):
        name, separator, raw_value = item.strip().partition("=")
        if separator and name and name not in cookies:
            cookies[name] = raw_value
    return cookies


def session_cookie(token: str, *, max_age: int) -> str:
    return (
        f"{SESSION_COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; "
        "Secure; HttpOnly; SameSite=Lax"
    )


def csrf_cookie(token: str, *, max_age: int) -> str:
    return f"{CSRF_COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; Secure; SameSite=Strict"


def login_cookie(state: str) -> str:
    return (
        f"{LOGIN_COOKIE_NAME}={state}; Path=/; Max-Age=600; "
        "Secure; HttpOnly; SameSite=Lax"
    )


def clear_login_cookie() -> str:
    return (
        f"{LOGIN_COOKIE_NAME}=; Path=/; Max-Age=0; "
        "Secure; HttpOnly; SameSite=Lax"
    )


def clear_auth_cookies() -> list[str]:
    return [
        f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax",
        f"{CSRF_COOKIE_NAME}=; Path=/; Max-Age=0; Secure; SameSite=Strict",
    ]
