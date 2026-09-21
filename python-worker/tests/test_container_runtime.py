from __future__ import annotations

import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from sunny_core.browser_backend import camoufox_runtime_error
from sunny_core import mailbox as mailbox_module
from sunny_core.mailbox import HotmailReader, MailAccount, MailboxAccessError, URLAPIICloudReader, XbovoICloudReader, _request_outlook_access_token, account_from_row, create_mailbox_reader, parse_account_line


def account(email: str = "user@example.com") -> MailAccount:
    return MailAccount(
        email=email,
        password="password",
        client_id="client-id",
        refresh_token="refresh-token",
        raw=f"{email}----password----client-id----refresh-token",
    )


class CamoufoxRuntimeTests(unittest.TestCase):
    def test_container_reports_missing_linux_library(self) -> None:
        def load_library(name: str):
            if name == "libgtk-3.so.0":
                raise OSError("not found")
            return object()

        with (
            patch("sunny_core.browser_backend.sys.platform", "linux"),
            patch.dict(os.environ, {"SUNNY_CONTAINERIZED": "true"}),
            patch("sunny_core.browser_backend.ctypes.CDLL", side_effect=load_library),
        ):
            error = camoufox_runtime_error()

        self.assertIn("libgtk-3.so.0", error)

    def test_capital_x11_xcb_soname_is_accepted(self) -> None:
        """Regression: libx11-xcb1 ships libX11-xcb.so.1 with a capital X.

        Probing only the lowercase spelling made every headless Camoufox launch
        fail with a bogus "missing dependencies" error, which surfaced as
        Access Token renewal failures even though the library was present.
        """
        loaded: list[str] = []

        def load_library(name: str):
            loaded.append(name)
            # Only the real (capitalised) soname resolves, as in the image.
            if name == "libX11-xcb.so.1":
                return object()
            if name == "libx11-xcb.so.1":
                raise OSError("cannot open shared object file")
            return object()

        with (
            patch("sunny_core.browser_backend.sys.platform", "linux"),
            patch.dict(os.environ, {"SUNNY_CONTAINERIZED": "true"}),
            patch("sunny_core.browser_backend.ctypes.CDLL", side_effect=load_library),
        ):
            error = camoufox_runtime_error()

        self.assertEqual(error, "")
        self.assertIn("libX11-xcb.so.1", loaded)

    def test_lowercase_x11_xcb_soname_still_accepted(self) -> None:
        """Images that only expose the lowercase soname must keep working."""

        def load_library(name: str):
            if name == "libx11-xcb.so.1":
                return object()
            if name == "libX11-xcb.so.1":
                raise OSError("cannot open shared object file")
            return object()

        with (
            patch("sunny_core.browser_backend.sys.platform", "linux"),
            patch.dict(os.environ, {"SUNNY_CONTAINERIZED": "true"}),
            patch("sunny_core.browser_backend.ctypes.CDLL", side_effect=load_library),
        ):
            error = camoufox_runtime_error()

        self.assertEqual(error, "")


class OutlookImapRouteTests(unittest.TestCase):
    def test_direct_then_dedicated_proxy(self) -> None:
        reader = HotmailReader(account(), None, "http://task-proxy.example:8080")
        with patch.dict(
            os.environ,
            {
                "OUTLOOK_IMAP_DIRECT_FIRST": "true",
                "OUTLOOK_IMAP_PROXY": "socks5://imap-proxy.example:1080",
            },
        ):
            self.assertEqual(
                reader._imap_proxy_candidates(),
                ["", "socks5://imap-proxy.example:1080"],
            )

    def test_task_proxy_is_used_when_dedicated_proxy_is_empty(self) -> None:
        reader = HotmailReader(account(), None, "http://task-proxy.example:8080")
        with patch.dict(
            os.environ,
            {"OUTLOOK_IMAP_DIRECT_FIRST": "false", "OUTLOOK_IMAP_PROXY": ""},
        ):
            self.assertEqual(
                reader._imap_proxy_candidates(),
                ["http://task-proxy.example:8080", ""],
            )


class XbovoICloudReaderTests(unittest.TestCase):
    @staticmethod
    def _account() -> MailAccount:
        return account_from_row(
            {
                "email": "alias@icloud.com",
                "mailbox_type": "apple",
                "mailbox_channel": "xbovo",
                "access_key": "alias_key",
            }
        )

    def test_account_from_row_parses_apple_xbovo_credential(self) -> None:
        parsed = account_from_row(
            {
                "email": "alias@icloud.com",
                "mailbox_type": "apple",
                "mailbox_channel": "xbovo",
                "access_key": "alias_key",
                "raw": "alias@icloud.com----alias_key",
            }
        )
        self.assertEqual(parsed.mailbox_type, "apple")
        self.assertEqual(parsed.mailbox_channel, "xbovo")
        self.assertEqual(parsed.access_key, "alias_key")
        self.assertIsInstance(create_mailbox_reader(parsed, None), XbovoICloudReader)

    def test_wait_for_code_uses_xbovo_long_poll(self) -> None:
        parsed = account_from_row(
            {
                "email": "alias@icloud.com",
                "mailbox_type": "apple",
                "mailbox_channel": "xbovo",
                "access_key": "alias_key",
            }
        )
        response = Mock()
        response.ok = True
        response.text = '{"ok":true,"code":"123456"}'
        response.json.return_value = {"ok": True, "code": "123456", "timeout": False}
        reader = XbovoICloudReader(parsed, None, "http://proxy.example:8080")
        with patch("sunny_core.mailbox.requests.get", return_value=response) as request_get:
            code = reader.wait_for_code(1_700_000_000, timeout=5)
        self.assertEqual(code, "123456")
        kwargs = request_get.call_args.kwargs
        self.assertEqual(kwargs["params"]["email"], "alias@icloud.com")
        self.assertNotIn("key", kwargs["params"])
        self.assertEqual(kwargs["headers"]["X-API-Key"], "alias_key")
        self.assertEqual(kwargs["proxies"]["https"], "http://proxy.example:8080")

    def test_connect_defers_transient_network_failure_to_otp_polling(self) -> None:
        logs: list[str] = []
        reader = XbovoICloudReader(self._account(), logs.append)
        transient = MailboxAccessError("mailbox_network_error", "temporary network failure")

        with patch.object(reader, "_request", side_effect=transient) as request:
            reader.connect()

        self.assertEqual(request.call_args.kwargs["timeout"], 10)
        self.assertTrue(any("验证码阶段继续重试" in message for message in logs))

    def test_wait_for_code_retries_transient_provider_failure(self) -> None:
        reader = XbovoICloudReader(self._account(), None)
        transient = MailboxAccessError("mailbox_network_error", "temporary network failure")
        success = {"ok": True, "code": "123456"}

        with (
            patch.object(reader, "_request", side_effect=[transient, success]) as request,
            patch("sunny_core.mailbox.time.sleep"),
        ):
            code = reader.wait_for_code(1_700_000_000, timeout=5)

        self.assertEqual(code, "123456")
        self.assertEqual(request.call_count, 2)

    def test_pool_exhaustion_is_retried_and_responses_are_closed(self) -> None:
        busy = Mock(ok=False, status_code=503, text='{"ok":false,"error":"PoolError: connection pool exhausted"}')
        busy.json.return_value = {"ok": False, "error": "PoolError: connection pool exhausted"}
        success = Mock(ok=True, status_code=200, text='{"ok":true,"messages":[]}')
        success.json.return_value = {"ok": True, "messages": []}
        logs: list[str] = []
        reader = XbovoICloudReader(self._account(), logs.append)

        with (
            patch("sunny_core.mailbox.requests.get", side_effect=[busy, success]) as request_get,
            patch("sunny_core.mailbox.random.uniform", return_value=0),
            patch("sunny_core.mailbox.time.sleep"),
        ):
            payload = reader._request("/api/v1/messages", {"email": "alias@icloud.com"})

        self.assertTrue(payload["ok"])
        self.assertEqual(request_get.call_count, 2)
        busy.close.assert_called_once()
        success.close.assert_called_once()
        self.assertTrue(any("连接池繁忙" in message for message in logs))

    def test_pool_exhaustion_after_retries_has_busy_error_code(self) -> None:
        busy = Mock(ok=False, status_code=503, text="<html>PoolError: connection pool exhausted</html>")
        busy.json.side_effect = ValueError("not json")
        reader = XbovoICloudReader(self._account(), None)

        with (
            patch("sunny_core.mailbox.requests.get", return_value=busy),
            patch("sunny_core.mailbox.XBOVO_POOL_RETRIES", 1),
            patch("sunny_core.mailbox.random.uniform", return_value=0),
            patch("sunny_core.mailbox.time.sleep"),
        ):
            with self.assertRaises(MailboxAccessError) as raised:
                reader._request("/api/v1/messages", {"email": "alias@icloud.com"})

        self.assertEqual(raised.exception.code, "mailbox_provider_busy")
        self.assertEqual(busy.close.call_count, 2)

    def test_request_gate_limits_concurrent_provider_calls(self) -> None:
        gate = threading.BoundedSemaphore(2)
        active = 0
        peak = 0
        lock = threading.Lock()

        def request(*_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            response = Mock(ok=True, status_code=200, text='{"ok":true,"messages":[]}')
            response.json.return_value = {"ok": True, "messages": []}
            return response

        with (
            patch.object(mailbox_module, "_XBOVO_REQUEST_GATE", gate),
            patch("sunny_core.mailbox.requests.get", side_effect=request),
        ):
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(
                    executor.map(
                        lambda _: XbovoICloudReader(self._account(), None)._request(
                            "/api/v1/messages", {"email": "alias@icloud.com"}
                        ),
                        range(8),
                    )
                )

        self.assertEqual(len(results), 8)
        self.assertLessEqual(peak, 2)


class URLAPIICloudReaderTests(unittest.TestCase):
    def test_account_from_row_routes_url_api_channel(self) -> None:
        parsed = account_from_row({
            "email": "alias@icloud.com",
            "mailbox_type": "apple",
            "mailbox_channel": "url_api",
            "access_key": "https://mail.example.test/latest/alias@icloud.com",
        })
        self.assertEqual(parsed.mailbox_channel, "url_api")
        self.assertIsInstance(create_mailbox_reader(parsed, None), URLAPIICloudReader)

    def test_connect_baselines_old_code_and_wait_returns_new_code(self) -> None:
        parsed = account_from_row({
            "email": "alias@icloud.com",
            "mailbox_type": "apple",
            "mailbox_channel": "url_api",
            "access_key": "https://mail.example.test/latest/alias@icloud.com",
        })
        old_response = Mock(ok=True, status_code=200, url=parsed.access_key, text="<html><h2>ChatGPT</h2><p>Code 111111</p></html>")
        new_response = Mock(ok=True, status_code=200, url=parsed.access_key, text="<html><h2>ChatGPT</h2><p>Code 222222</p></html>")
        reader = URLAPIICloudReader(parsed, None)
        with patch("sunny_core.mailbox.requests.get", side_effect=[old_response, new_response]) as request_get:
            reader.connect()
            code = reader.wait_for_code(1_700_000_000, timeout=5)
        self.assertEqual(code, "222222")
        self.assertGreaterEqual(request_get.call_args.kwargs["timeout"], 35)

    def test_latest_message_normalizes_html(self) -> None:
        parsed = account_from_row({
            "email": "alias@icloud.com",
            "mailbox_type": "apple",
            "mailbox_channel": "url_api",
            "access_key": "https://mail.example.test/latest/alias@icloud.com",
        })
        response = Mock(ok=True, status_code=200, url=parsed.access_key, text="<html><h2>ChatGPT</h2><p>验证码 <b>654321</b></p></html>")
        with patch("sunny_core.mailbox.requests.get", return_value=response):
            message = URLAPIICloudReader(parsed, None).latest_message()
        self.assertEqual(message["otp"], "654321")
        self.assertEqual(message["source"], "url_api")
        self.assertIn("验证码 654321", message["body"])

    def test_redirect_target_is_revalidated(self) -> None:
        parsed = account_from_row({
            "email": "alias@icloud.com",
            "mailbox_type": "apple",
            "mailbox_channel": "url_api",
            "access_key": "https://mail.example.test/latest",
        })
        redirect = Mock(status_code=302, headers={"Location": "http://127.0.0.1/private"})
        with patch("sunny_core.mailbox.requests.get", return_value=redirect):
            with self.assertRaisesRegex(MailboxAccessError, "私有网络"):
                URLAPIICloudReader(parsed, None).latest_message()

    def test_response_size_is_limited(self) -> None:
        parsed = account_from_row({
            "email": "alias@icloud.com",
            "mailbox_type": "apple",
            "mailbox_channel": "url_api",
            "access_key": "https://mail.example.test/latest",
        })
        response = Mock(ok=True, status_code=200, headers={"Content-Length": str((1 << 20) + 1)})
        with patch("sunny_core.mailbox.requests.get", return_value=response):
            with self.assertRaisesRegex(MailboxAccessError, "内容过大"):
                URLAPIICloudReader(parsed, None).latest_message()


class OutlookGraphCredentialTests(unittest.TestCase):
    def test_expired_refresh_token_is_classified_and_stops_routing(self) -> None:
        response = Mock()
        response.ok = False
        response.status_code = 400
        response.text = '{"error":"invalid_grant"}'
        response.json.return_value = {
            "error": "invalid_grant",
            "error_description": "The user could not be authenticated as the grant is expired. The user must sign in again.",
        }
        with patch("sunny_core.mailbox.requests.post", return_value=response):
            with self.assertRaises(MailboxAccessError) as raised:
                _request_outlook_access_token(account(), {"name": "TEST", "url": "https://example.test/token", "scope": ""}, None)

        self.assertEqual(raised.exception.code, "mailbox_credential_expired")
        self.assertTrue(raised.exception.terminal)

    def test_graph_credential_is_detected_and_reads_messages(self) -> None:
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "value": [{
                "id": "message-id",
                "subject": "Your ChatGPT code is 123456",
                "from": {"emailAddress": {"name": "OpenAI", "address": "noreply@openai.com"}},
                "toRecipients": [{"emailAddress": {"address": "user@example.com"}}],
                "receivedDateTime": "2026-07-22T08:00:00Z",
                "bodyPreview": "Use 123456 to continue",
                "body": {"contentType": "html", "content": "<p>Use <b>123456</b> to continue</p>"},
            }],
        }
        reader = HotmailReader(account(), None)
        with (
            patch("sunny_core.mailbox._request_outlook_access_token", return_value="graph-access-token"),
            patch("sunny_core.mailbox.requests.get", return_value=response) as graph_get,
        ):
            reader.connect()
            message = reader.latest_message()

        self.assertEqual(reader.graph_access_token, "graph-access-token")
        self.assertEqual(message["source"], "graph")
        self.assertEqual(message["otp"], "123456")
        self.assertIn("noreply@openai.com", message["from"])
        self.assertEqual(graph_get.call_args.kwargs["headers"]["Authorization"], "Bearer graph-access-token")

    def test_graph_scope_failure_falls_back_to_imap(self) -> None:
        reader = HotmailReader(account(), None)
        with (
            patch.object(reader, "_connect_graph_routes", return_value=False),
            patch("sunny_core.mailbox._request_outlook_access_token", return_value="imap-access-token"),
            patch.object(reader, "_connect_with_access_token_routes") as connect_imap,
        ):
            reader.connect()

        connect_imap.assert_called_once_with("imap-access-token", "LIVE token-direct")


class HotmailCredentialCompatibilityTests(unittest.TestCase):
    def test_hotmail_four_field_credential_is_accepted(self) -> None:
        parsed = parse_account_line("reader@hotmail.com----password----client-id----refresh-token")

        self.assertEqual(parsed.email, "reader@hotmail.com")
        self.assertEqual(parsed.client_id, "client-id")
        self.assertEqual(parsed.refresh_token, "refresh-token")

    def test_hotmail_swapped_oauth_fields_are_normalized(self) -> None:
        client_id = "11111111-2222-4333-8444-555555555555"
        refresh_token = "M.C502_BL2.0.U.MsaArtifacts.-example-refresh-token-with-enough-length-for-field-detection!0123456789$"

        parsed = parse_account_line(f"reader@hotmail.com----password----{refresh_token}----{client_id}")

        self.assertEqual(parsed.password, "password")
        self.assertEqual(parsed.client_id, client_id)
        self.assertEqual(parsed.refresh_token, refresh_token)
        self.assertEqual(parsed.raw, f"reader@hotmail.com----password----{client_id}----{refresh_token}")

    def test_hotmail_dual_token_prefers_graph(self) -> None:
        response = Mock()
        response.ok = True
        response.json.return_value = {"value": []}
        reader = HotmailReader(account("reader@hotmail.com"), None)
        with (
            patch("sunny_core.mailbox._request_outlook_access_token", return_value="dual-access-token"),
            patch("sunny_core.mailbox.requests.get", return_value=response),
            patch.object(reader, "_connect_with_access_token_routes") as connect_imap,
        ):
            reader.connect()

        self.assertEqual(reader.graph_access_token, "dual-access-token")
        connect_imap.assert_not_called()

    def test_hotmail_imap_pop3_credential_falls_back_to_imap(self) -> None:
        reader = HotmailReader(account("reader@hotmail.com"), None)
        with (
            patch.object(reader, "_connect_graph_routes", return_value=False),
            patch("sunny_core.mailbox._request_outlook_access_token", return_value="outlook-scope-token"),
            patch.object(reader, "_connect_with_access_token_routes") as connect_imap,
        ):
            reader.connect()

        connect_imap.assert_called_once_with("outlook-scope-token", "LIVE token-direct")


if __name__ == "__main__":
    unittest.main()
