"""Tests for the self-hosted domain-mailbox-vps adapter.

The VPS server stores raw MIME documents and returns them under a ``results``
array, so the reader must decode quoted-printable and base64 parts before the
OpenAI verification code can be located.
"""

import json
from datetime import datetime, timezone

import pytest

from sunny_core.mailbox import (
    MailboxAccessError,
    VpsDomainReader,
    account_from_row,
    create_mailbox_reader,
)

# A trimmed but structurally faithful copy of a real OpenAI notification:
# the body is text/html with quoted-printable encoding.
REAL_OTP_EMAIL = (
    "Content-Transfer-Encoding: quoted-printable\r\n"
    "Content-Type: text/html; charset=us-ascii\r\n"
    "Date: Mon, 14 Sep 2026 05:29:04 +0000 (UTC)\r\n"
    "From: OpenAI <noreply@tm.openai.com>\r\n"
    "Message-ID: <Jr20NPxeRJiKvqh57_JbjQ@geopod-ismtpd-14>\r\n"
    "Subject: =?UTF-8?B?5L2g55qEIE9wZW5BSSDpqozor4HnoIHkuLo=?= 096480\r\n"
    "To: zpai1h1x9lv1@cyxz.online\r\n"
    "Mime-Version: 1.0\r\n"
    "\r\n"
    "<html><body><div>New sign-in to your OpenAI account</div>\r\n"
    "<div>=E4=BD=A0=E7=9A=84 OpenAI =E9=AA=8C=E8=AF=81=E7=A0=81=E4=B8=BA 096480</div>\r\n"
    "</body></html>\r\n"
)


def _credential(email="user@example.com", token="vps-token-1"):
    return json.dumps({
        "provider": "vps",
        "base_url": "https://manage.example.com",
        "auth_token": token,
        "email": email,
    })


def _account(email="user@example.com", token="vps-token-1"):
    return account_from_row({
        "email": email,
        "mailbox_type": "domain",
        "mailbox_channel": "domain_api",
        "access_key": _credential(email, token),
    })


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


def test_account_from_row_accepts_vps_credential():
    account = _account()
    assert account.mailbox_type == "domain"
    assert account.mailbox_channel == "domain_api"
    metadata = json.loads(account.access_key)
    assert metadata["provider"] == "vps"
    assert metadata["base_url"] == "https://manage.example.com"


def test_account_from_row_rejects_vps_credential_for_other_mailbox():
    with pytest.raises(ValueError):
        account_from_row({
            "email": "someone-else@example.com",
            "mailbox_type": "domain",
            "mailbox_channel": "domain_api",
            "access_key": _credential("user@example.com"),
        })


def test_create_mailbox_reader_dispatches_on_provider():
    reader = create_mailbox_reader(_account(), None)
    assert isinstance(reader, VpsDomainReader)


def test_create_mailbox_reader_keeps_cloudmail_dialect(monkeypatch):
    legacy = account_from_row({
        "email": "user@example.com",
        "mailbox_type": "domain",
        "mailbox_channel": "domain_api",
        "access_key": json.dumps({"base_url": "https://mail.example", "auth_token": "t"}),
    })
    from sunny_core import mailbox as mailbox_module

    assert not isinstance(create_mailbox_reader(legacy, None), VpsDomainReader)


def test_reader_decodes_quoted_printable_html_and_extracts_code(monkeypatch):
    logs = []
    reader = VpsDomainReader(_account(), logs.append)
    payload = {"results": [
        {"id": 12, "address": "user@example.com", "created_at": "2026-09-14T05:29:04.776Z",
         "raw": REAL_OTP_EMAIL},
    ]}
    monkeypatch.setattr("sunny_core.mailbox.requests.get", lambda *a, **k: _Response(payload))

    current = reader._latest()

    assert current["code"] == "096480"
    assert current["id"] == 12
    # The Chinese body text must survive quoted-printable + UTF-8 decoding.
    assert "验证码为 096480" in current["body"]
    assert "<" not in current["body"]
    assert current["subject"] == "你的 OpenAI 验证码为 096480"
    assert "noreply@tm.openai.com" in current["sender"]


def test_reader_sends_mailbox_scoped_bearer_token(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        captured["headers"] = kwargs.get("headers")
        return _Response({"results": []})

    monkeypatch.setattr("sunny_core.mailbox.requests.get", fake_get)
    reader = VpsDomainReader(_account(email="scoped@example.com", token="only-this-mailbox"), None)
    reader._latest()

    assert captured["url"] == "https://manage.example.com/v1/messages"
    assert captured["params"] == {"address": "scoped@example.com", "limit": 10}
    # A single mailbox token must never be sent as a shared admin credential.
    assert captured["headers"]["Authorization"] == "Bearer only-this-mailbox"


def test_reader_picks_newest_message(monkeypatch):
    older = REAL_OTP_EMAIL.replace("096480", "111111")
    newer = REAL_OTP_EMAIL.replace("096480", "978744")
    reader = VpsDomainReader(_account(), None)
    payload = {"results": [
        {"id": 1, "created_at": "2020-01-01T00:00:00Z", "raw": older},
        {"id": 2, "created_at": "2099-01-01T00:00:00Z", "raw": newer},
    ]}
    monkeypatch.setattr("sunny_core.mailbox.requests.get", lambda *a, **k: _Response(payload))

    current = reader._latest()
    assert current["code"] == "978744"
    assert current["id"] == 2
    assert current["timestamp"] == datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()


def test_reader_rejects_unauthorized_credential(monkeypatch):
    monkeypatch.setattr("sunny_core.mailbox.requests.get", lambda *a, **k: _Response({}, status_code=401))
    reader = VpsDomainReader(_account(), None)
    with pytest.raises(MailboxAccessError) as excinfo:
        reader._latest()
    assert excinfo.value.terminal is True


def test_reader_rejects_unrecognized_payload(monkeypatch):
    monkeypatch.setattr("sunny_core.mailbox.requests.get", lambda *a, **k: _Response({"unexpected": []}))
    reader = VpsDomainReader(_account(), None)
    with pytest.raises(MailboxAccessError):
        reader._latest()


def test_reader_rejects_non_json_credential():
    account = account_from_row({
        "email": "user@example.com",
        "mailbox_type": "domain",
        "mailbox_channel": "domain_api",
        "access_key": json.dumps({"base_url": "https://manage.example.com", "auth_token": "t"}),
    })
    # The legacy CloudMail dialect carries no provider marker, so it must never
    # be silently routed through the VPS reader.
    assert not isinstance(create_mailbox_reader(account, None), VpsDomainReader)


def test_reader_rejects_credential_without_base_url():
    account = account_from_row({
        "email": "user@example.com",
        "mailbox_type": "domain",
        "mailbox_channel": "domain_api",
        "access_key": json.dumps({"provider": "vps", "base_url": "not-a-url", "auth_token": "t"}),
    })
    with pytest.raises(MailboxAccessError):
        VpsDomainReader(account, None)


def test_wait_for_code_ignores_message_already_seen_at_connect(monkeypatch):
    """The connect() baseline, not a wall-clock floor, defines what is "old"."""
    reader = VpsDomainReader(_account(), None)
    payload = {"results": [
        {"id": 1, "created_at": "2020-01-01T00:00:00Z", "raw": REAL_OTP_EMAIL},
    ]}
    monkeypatch.setattr("sunny_core.mailbox.requests.get", lambda *a, **k: _Response(payload))

    reader.connect()
    with pytest.raises(TimeoutError):
        reader.wait_for_code(2000000000, timeout=0.05)


def test_wait_for_code_accepts_otp_whose_provider_timestamp_lags_the_request(monkeypatch):
    """Regression for the production registration timeout.

    The VPS stamps ``created_at`` with its own ingestion time, which trails the
    OpenAI send by minutes. The old timestamp floor compared that stamp against
    ``time.time()`` and therefore rejected the freshly delivered OTP, so the
    task failed with "重新发送协议验证码后等待 60 秒仍未收到验证码" while the code
    was already present in the inbox.
    """
    reader = VpsDomainReader(_account(), None)

    # No mail yet when the reader establishes its baseline.
    monkeypatch.setattr("sunny_core.mailbox.requests.get", lambda *a, **k: _Response({"results": []}))
    reader.connect()

    # The OTP then arrives, stamped far earlier than min_timestamp.
    payload = {"results": [
        {"id": 7, "created_at": "2020-01-01T00:00:00Z", "raw": REAL_OTP_EMAIL},
    ]}
    monkeypatch.setattr("sunny_core.mailbox.requests.get", lambda *a, **k: _Response(payload))

    assert reader.wait_for_code(2000000000, timeout=1) == "096480"


def test_wait_for_code_returns_new_message(monkeypatch):
    reader = VpsDomainReader(_account(), None)
    payload = {"results": [
        {"id": 5, "created_at": "2099-01-01T00:00:00Z", "raw": REAL_OTP_EMAIL},
    ]}
    monkeypatch.setattr("sunny_core.mailbox.requests.get", lambda *a, **k: _Response(payload))

    assert reader.wait_for_code(0, timeout=5) == "096480"


def test_latest_message_surfaces_domain_api_contract(monkeypatch):
    reader = VpsDomainReader(_account(), None)
    payload = {"results": [
        {"id": "m-1", "source": "noreply@tm.openai.com", "address": "user@example.com",
         "created_at": "2026-09-14T05:29:04.776Z", "raw": REAL_OTP_EMAIL},
    ]}
    monkeypatch.setattr("sunny_core.mailbox.requests.get", lambda *a, **k: _Response(payload))

    message = reader.latest_message()
    assert message["otp"] == "096480"
    assert message["source"] == "domain_api"
    assert message["email"] == "user@example.com"
    assert message["to"] == "user@example.com"
    assert message["id"] == "m-1"


def test_reader_falls_back_to_subject_when_body_has_no_code(monkeypatch):
    raw = (
        "Content-Type: text/plain; charset=utf-8\r\n"
        "From: OpenAI <noreply@tm.openai.com>\r\n"
        "Subject: Your code 424242\r\n"
        "\r\n"
        "Sign in to your account.\r\n"
    )
    reader = VpsDomainReader(_account(), None)
    monkeypatch.setattr("sunny_core.mailbox.requests.get", lambda *a, **k: _Response(
        {"results": [{"id": 1, "created_at": "2099-01-01T00:00:00Z", "raw": raw}]}
    ))
    assert reader._latest()["code"] == "424242"
