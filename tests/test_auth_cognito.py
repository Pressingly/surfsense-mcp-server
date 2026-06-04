"""Unit tests for ``surfsense_mcp.auth.cognito.SurfSenseCognitoProvider``.

The provider's only override is ``_extract_upstream_claims``: decode the
Cognito id_token from the token response and surface ``email`` /
``cognito:username`` / the raw id_token so the relay layer
(:mod:`surfsense_mcp.auth.http`) can identify the *same* user the browser
login resolves. We instantiate via ``__new__`` to skip the network-touching
``__init__`` (OIDC discovery) — the method under test uses no instance state.
"""

from __future__ import annotations

import jwt
import pytest

from surfsense_mcp.auth.cognito import SurfSenseCognitoProvider


def _provider() -> SurfSenseCognitoProvider:
    return SurfSenseCognitoProvider.__new__(SurfSenseCognitoProvider)


def _id_token(claims: dict) -> str:
    # Unsigned-ish HS256 token; the provider decodes without verifying signature.
    return jwt.encode(claims, "test-secret", algorithm="HS256")


async def test_extracts_email_username_and_raw_id_token() -> None:
    id_tok = _id_token(
        {
            "sub": "09daf50c-c0a1-70ec-41e2-443a7270561c",
            "cognito:username": "1020010000020127",
            "email": "1020010000020127@askii.ai",
            "token_use": "id",
            "aud": "client123",
        }
    )
    out = await _provider()._extract_upstream_claims({"access_token": "AT", "id_token": id_tok})
    assert out == {
        "id_token": id_tok,
        "email": "1020010000020127@askii.ai",
        "cognito:username": "1020010000020127",
    }


async def test_returns_none_when_no_id_token() -> None:
    """Cognito always returns an id_token with openid scope; if it's missing we
    return None so the relay falls back rather than embedding junk."""
    assert await _provider()._extract_upstream_claims({"access_token": "AT"}) is None


async def test_returns_none_when_id_token_not_a_string() -> None:
    assert await _provider()._extract_upstream_claims({"id_token": 12345}) is None


async def test_returns_none_when_id_token_undecodable() -> None:
    out = await _provider()._extract_upstream_claims({"id_token": "not-a-jwt"})
    assert out is None


async def test_omits_missing_optional_claims_but_keeps_id_token() -> None:
    """An id_token with neither email nor cognito:username still yields the raw
    token (useful for the HTTPS relay) without inventing identity claims."""
    id_tok = _id_token({"sub": "uuid-only", "token_use": "id"})
    out = await _provider()._extract_upstream_claims({"id_token": id_tok})
    assert out == {"id_token": id_tok}


async def test_ignores_non_string_claim_values() -> None:
    id_tok = _id_token({"email": ["a@b.ai"], "cognito:username": None, "sub": "x"})
    out = await _provider()._extract_upstream_claims({"id_token": id_tok})
    assert out == {"id_token": id_tok}


@pytest.mark.parametrize("blank", ["", None])
async def test_treats_blank_email_as_missing(blank) -> None:
    id_tok = _id_token({"email": blank, "cognito:username": "u", "sub": "x"})
    out = await _provider()._extract_upstream_claims({"id_token": id_tok})
    assert "email" not in out
    assert out["cognito:username"] == "u"
