"""A development identity provider: ``python -m notes_api.dev_issuer``.

The compose stack and the container smoke test have no identity provider, so this module plays one. ``env``
generates an RSA key pair and prints the lines a ``.env`` file (or a shell ``eval``) needs: the issuer and
audience, the public key as an inline JWKS for the server, and the private key for minting. ``token`` mints
an RS256 access token with that key. The test suite's local issuer is built on the same class.

Development only. A deployment configures ``OIDC_ISSUER``, ``OIDC_AUDIENCE``, and ``OIDC_JWKS_URL`` for a
real identity provider and never sets ``NOTES_DEV_ISSUER_KEY``. This module never reads ``Settings``, so it
runs from the image without ``DATABASE_URL``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

DEFAULT_ISSUER = "https://issuer.example"
DEFAULT_AUDIENCE = "notes-api"
DEFAULT_KEY_ID = "dev-key-1"
KEY_VARIABLE = "NOTES_DEV_ISSUER_KEY"
ACCESS_TOKEN_TYPE = "at+jwt"


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class DevIssuer:
    """An RSA key pair, its JWKS, and RS256 access tokens for one issuer and audience."""

    def __init__(
        self,
        *,
        issuer: str = DEFAULT_ISSUER,
        audience: str = DEFAULT_AUDIENCE,
        key_id: str = DEFAULT_KEY_ID,
        private_key: RSAPrivateKey | None = None,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.key_id = key_id
        self._private_key = private_key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key: RSAPublicKey = self._private_key.public_key()
        numbers = self.public_key.public_numbers()
        self.jwks: dict[str, Any] = {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": key_id,
                    "n": _b64url_uint(numbers.n),
                    "e": _b64url_uint(numbers.e),
                }
            ]
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DevIssuer:
        """The issuer whose private key is in ``NOTES_DEV_ISSUER_KEY`` (base64 of a PKCS#8 DER key)."""
        env = os.environ if env is None else env
        encoded = env.get(KEY_VARIABLE)
        if not encoded:
            raise LookupError(f"{KEY_VARIABLE} is not set; run `python -m notes_api.dev_issuer env` first")
        key = serialization.load_der_private_key(base64.b64decode(encoded), password=None)
        if not isinstance(key, RSAPrivateKey):
            raise ValueError(f"{KEY_VARIABLE} is not an RSA private key")
        return cls(
            issuer=env.get("OIDC_ISSUER") or DEFAULT_ISSUER,
            audience=env.get("OIDC_AUDIENCE") or DEFAULT_AUDIENCE,
            private_key=key,
        )

    def token(
        self,
        subject: str,
        name: str | None = None,
        *,
        audience: str | None = None,
        expires_in: timedelta = timedelta(hours=1),
        issued_at: datetime | None = None,
        extra_claims: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
    ) -> str:
        """An RS256 access token.

        ``extra_claims`` and ``headers`` override the defaults entry by entry; a value of None removes the
        claim or header, so tests can mint tokens that lack one.
        """
        now = issued_at or datetime.now(UTC)
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": subject,
            "aud": audience or self.audience,
            "iat": int(now.timestamp()),
            "exp": int((now + expires_in).timestamp()),
        }
        if name is not None:
            claims["name"] = name
        claims.update(extra_claims or {})
        token_headers: dict[str, Any] = {"kid": self.key_id, "typ": ACCESS_TOKEN_TYPE}
        token_headers.update(headers or {})
        claims = {name: value for name, value in claims.items() if value is not None}
        token_headers = {name: value for name, value in token_headers.items() if value is not None}
        return jwt.encode(claims, self._private_key, algorithm="RS256", headers=token_headers)

    def private_key_b64(self) -> str:
        der = self._private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return base64.b64encode(der).decode("ascii")

    def env_lines(self) -> str:
        """Single-quoted ``NAME='value'`` lines, valid both in a compose ``.env`` file and for ``eval``."""
        values = {
            "OIDC_ISSUER": self.issuer,
            "OIDC_AUDIENCE": self.audience,
            "OIDC_JWKS": json.dumps(self.jwks, separators=(",", ":")),
            KEY_VARIABLE: self.private_key_b64(),
            "CURSOR_SIGNING_KEY": secrets.token_hex(32),
        }
        lines = []
        for name, value in values.items():
            if "'" in value or "\n" in value:
                raise ValueError(f"{name} cannot be written as a single-quoted line")
            lines.append(f"{name}='{value}'")
        return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m notes_api.dev_issuer",
        description="A development identity provider for the compose stack and the smoke test.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    env_parser = commands.add_parser("env", help="generate a key pair and print .env lines for the stack")
    env_parser.add_argument("--issuer", default=DEFAULT_ISSUER)
    env_parser.add_argument("--audience", default=DEFAULT_AUDIENCE)
    token_parser = commands.add_parser("token", help=f"mint an access token with the key in {KEY_VARIABLE}")
    token_parser.add_argument("--sub", required=True, help="subject claim; one subject is one user")
    token_parser.add_argument("--name", help="name claim, shown as the user's displayName")
    token_parser.add_argument("--audience", help="aud claim (default: OIDC_AUDIENCE or notes-api)")
    token_parser.add_argument("--expires-in-hours", type=float, default=8.0)
    args = parser.parse_args(argv)

    if args.command == "env":
        sys.stdout.write(DevIssuer(issuer=args.issuer, audience=args.audience).env_lines())
        return 0
    try:
        issuer = DevIssuer.from_env()
    except (LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    token = issuer.token(
        args.sub, args.name, audience=args.audience, expires_in=timedelta(hours=args.expires_in_hours)
    )
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
