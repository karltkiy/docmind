"""Tests for the one-off transcript recorder.

The recorder performs the only paid provider call in the demo tooling, so these
tests never touch the network. They cover argument handling, credential lookup
and the guarantee that no credential value or credential-adjacent variable name
reaches stdout/stderr (CodeQL ``py/clear-text-logging-sensitive-data``).
"""

from __future__ import annotations

import pytest

from demo import record_transcript as recorder

# A value that looks nothing like a real provider key on purpose: the security
# scanner must not treat this test fixture as a leaked credential.
FAKE_CREDENTIAL = "unit-test-secret-value-123"


def test_read_api_key_returns_none_for_blank_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCMIND_TEST_KEY", "   ")
    assert recorder._read_api_key("DOCMIND_TEST_KEY") is None

    monkeypatch.delenv("DOCMIND_TEST_KEY", raising=False)
    assert recorder._read_api_key("DOCMIND_TEST_KEY") is None


def test_read_api_key_strips_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCMIND_TEST_KEY", f"  {FAKE_CREDENTIAL}  ")
    assert recorder._read_api_key("DOCMIND_TEST_KEY") == FAKE_CREDENTIAL


def test_missing_key_message_is_a_constant_without_interpolation() -> None:
    """The diagnostic must be a fixed string, never an f-string."""
    assert "{" not in recorder.MISSING_KEY_MESSAGE
    assert "}" not in recorder.MISSING_KEY_MESSAGE
    assert "--api-key-env" in recorder.MISSING_KEY_MESSAGE


def test_missing_credential_exits_without_printing_the_variable_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DOCMIND_TEST_KEY", raising=False)

    code = recorder.main(
        [
            "--document",
            "refund-policy.md",
            "--question",
            "How long does a refund take?",
            "--id",
            "refund-window",
            "--api-key-env",
            "DOCMIND_TEST_KEY",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert captured.err.strip() == recorder.MISSING_KEY_MESSAGE
    assert "DOCMIND_TEST_KEY" not in captured.err
    assert "DOCMIND_TEST_KEY" not in captured.out


def test_failure_path_never_echoes_the_credential(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DOCMIND_TEST_KEY", FAKE_CREDENTIAL)

    def boom(**_kwargs: object) -> tuple[list[dict[str, object]], dict[str, float]]:
        raise recorder.RecordTranscriptError("the API answered HTTP 500")

    monkeypatch.setattr(recorder, "capture", boom)

    code = recorder.main(
        [
            "--document",
            "refund-policy.md",
            "--question",
            "How long does a refund take?",
            "--id",
            "refund-window",
            "--api-key-env",
            "DOCMIND_TEST_KEY",
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert "HTTP 500" in captured.err
    assert FAKE_CREDENTIAL not in captured.err
    assert FAKE_CREDENTIAL not in captured.out


def test_credential_reaches_the_auth_header_only() -> None:
    headers = recorder._auth_headers(FAKE_CREDENTIAL)
    assert headers == {"X-API-Key": FAKE_CREDENTIAL}
    assert recorder._auth_headers("") == {}
