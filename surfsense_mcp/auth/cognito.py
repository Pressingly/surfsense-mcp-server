"""Cognito provider that surfaces id-token identity to the SurfSense relay.

Why this exists
---------------
FastMCP's stock ``AWSCognitoProvider`` validates the inbound Cognito **access
token** and filters its claims down to ``{sub, username, cognito:groups}``
(see ``fastmcp.server.auth.providers.aws.AWSCognitoTokenVerifier``). Two facts
about Cognito access tokens make that insufficient for identifying the upstream
SurfSense user:

* they carry **no ``email`` claim** at all, and
* for users federated from an external IdP (e.g. askii.ai), ``username`` is an
  opaque Cognito UUID (``09daf50c-…``), not the human identifier the web login
  keys on (``1020010000020127``).

The browser/cookie login resolves the right user because oauth2-proxy reads the
**id_token**, whose ``email`` claim (``1020010000020127@askii.ai``) SurfSense's
``ProxyAuthMiddleware`` prefers. To make the MCP path land on that *same* user,
we need the id_token's claims — but the access-token verifier never sees them.

``OAuthProxy`` exposes the right seam: :meth:`_extract_upstream_claims` is
called with the full Cognito token response (access_token + id_token + …) at
exchange *and* refresh time, and whatever it returns is sealed inside the
FastMCP-signed reference JWT and handed back at request time as
``AccessToken.claims["upstream_claims"]`` (consumed by
:mod:`surfsense_mcp.auth.http`). We decode the id_token there and stash
``email``, ``cognito:username``, and the raw id_token string.

Trust note
----------
The id_token is decoded **without signature verification**, which is safe here:

* it arrives directly from Cognito's token endpoint over the provider's own
  server-to-server TLS call — never from the MCP client, and
* the extracted values are re-sealed inside the FastMCP JWT whose signature
  *is* verified on every request before ``upstream_claims`` is read.

The inbound access token is still independently JWKS-verified on every request
by the stock verifier, so this adds no auth-bypass surface — it only enriches
the identity forwarded downstream.
"""

from __future__ import annotations

from typing import Any

import jwt
from fastmcp.server.auth.providers.aws import AWSCognitoProvider
from fastmcp.utilities.logging import get_logger

from surfsense_mcp.auth.http import (
    COGNITO_USERNAME_CLAIM,
    EMAIL_CLAIM,
    ID_TOKEN_KEY,
)

logger = get_logger(__name__)


class SurfSenseCognitoProvider(AWSCognitoProvider):
    """``AWSCognitoProvider`` that propagates id-token identity downstream.

    Identical construction/behavior to the base provider — it only overrides
    :meth:`_extract_upstream_claims` to capture the id_token's identity claims.
    """

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        id_token = idp_tokens.get(ID_TOKEN_KEY)
        if not isinstance(id_token, str) or not id_token:
            logger.warning(
                "Cognito token response has no id_token — SurfSense identity will "
                "fall back to the access token and may resolve the wrong user."
            )
            return None

        try:
            # Unverified by design (see module docstring): fresh from Cognito's
            # token endpoint, and re-sealed in the FastMCP-signed JWT.
            claims = jwt.decode(id_token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            logger.warning("Failed to decode Cognito id_token: %s", exc)
            return None

        extracted: dict[str, Any] = {ID_TOKEN_KEY: id_token}
        email = claims.get(EMAIL_CLAIM)
        if isinstance(email, str) and email:
            extracted[EMAIL_CLAIM] = email
        cognito_username = claims.get(COGNITO_USERNAME_CLAIM)
        if isinstance(cognito_username, str) and cognito_username:
            extracted[COGNITO_USERNAME_CLAIM] = cognito_username
        return extracted
