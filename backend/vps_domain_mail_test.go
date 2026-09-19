package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// A structurally faithful copy of a real OpenAI notification: a text/html body
// with quoted-printable encoding and a UTF-8 base64 encoded subject.
const vpsRealOTPEmail = "Content-Transfer-Encoding: quoted-printable\r\n" +
	"Content-Type: text/html; charset=us-ascii\r\n" +
	"Date: Mon, 14 Sep 2026 05:29:04 +0000 (UTC)\r\n" +
	"From: OpenAI <noreply@tm.openai.com>\r\n" +
	"Message-ID: <Jr20NPxeRJiKvqh57_JbjQ@geopod-ismtpd-14>\r\n" +
	"Subject: =?UTF-8?B?5L2g55qEIE9wZW5BSSDpqozor4HnoIHkuLo=?= 096480\r\n" +
	"To: zpai1h1x9lv1@cyxz.online\r\n" +
	"Mime-Version: 1.0\r\n" +
	"\r\n" +
	"<html><body><div>New sign-in to your OpenAI account</div>\r\n" +
	"<div>=E4=BD=A0=E7=9A=84 OpenAI =E9=AA=8C=E8=AF=81=E7=A0=81=E4=B8=BA 096480</div>\r\n" +
	"</body></html>\r\n"

func TestDomainMailProviderDetection(t *testing.T) {
	for _, value := range []string{"vps", "VPS", "domain_mailbox_vps", "domain-mailbox-vps"} {
		if got := domainMailProvider(map[string]any{"provider": value}); got != "vps" {
			t.Fatalf("provider %q normalized to %q, want vps", value, got)
		}
	}
	if got := domainMailProvider(map[string]any{}); got != "cloudmail" {
		t.Fatalf("empty provider normalized to %q, want cloudmail", got)
	}
	if got := domainMailProvider(map[string]any{"provider": "cloudmail"}); got != "cloudmail" {
		t.Fatalf("cloudmail provider normalized to %q", got)
	}
}

func TestDomainMailDecodeMIMEExtractsQuotedPrintableHTML(t *testing.T) {
	body, subject, sender := domainMailDecodeMIME(vpsRealOTPEmail)
	if subject != "你的 OpenAI 验证码为 096480" {
		t.Fatalf("unexpected decoded subject: %q", subject)
	}
	if !strings.Contains(sender, "noreply@tm.openai.com") {
		t.Fatalf("unexpected decoded sender: %q", sender)
	}
	if strings.Contains(body, "<") || strings.Contains(body, "=E4") {
		t.Fatalf("body was not fully decoded: %q", body)
	}
	if !strings.Contains(body, "验证码为 096480") {
		t.Fatalf("utf-8 body text was mangled: %q", body)
	}
}

func TestDomainMailVPSMessageItemsExtractCode(t *testing.T) {
	items := domainMailVPSMessageItems([]map[string]any{{
		"id":         12,
		"address":    "zpai1h1x9lv1@cyxz.online",
		"created_at": "2026-09-14T05:29:04.776Z",
		"raw":        vpsRealOTPEmail,
	}}, "zpai1h1x9lv1@cyxz.online")
	if len(items) != 1 {
		t.Fatalf("expected one item, got %d", len(items))
	}
	item := items[0]
	if item["otp"] != "096480" {
		t.Fatalf("unexpected otp: %#v", item["otp"])
	}
	if item["source"] != "domain_api" {
		t.Fatalf("unexpected source: %#v", item["source"])
	}
	if item["subject"] != "你的 OpenAI 验证码为 096480" {
		t.Fatalf("unexpected subject: %#v", item["subject"])
	}
	// Item ids are normalized to strings by firstText().
	if item["id"] != "12" {
		t.Fatalf("unexpected id: %#v", item["id"])
	}
}

func TestDomainMailVPSMessageListReadsResultsArray(t *testing.T) {
	list := domainMailVPSMessageList(map[string]any{
		"results": []any{map[string]any{"id": 1.0, "raw": vpsRealOTPEmail}},
		"count":   1.0,
	})
	if len(list) != 1 {
		t.Fatalf("expected one message, got %d", len(list))
	}
	// The CloudMail shape must not accidentally match a VPS payload.
	if got := domainMailMessageList(map[string]any{"results": []any{map[string]any{"id": 1.0}}}); len(got) != 0 {
		t.Fatalf("legacy parser should ignore the results key, got %d", len(got))
	}
}

func TestDomainMailVPSClientCreatesMailboxWithScopedToken(t *testing.T) {
	var createAuth string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/mailboxes":
			createAuth = r.Header.Get("Authorization")
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			// The VPS server generates the address from local_part + domain and
			// rejects an explicit "address" field.
			if _, hasAddress := body["address"]; hasAddress {
				t.Errorf("create body must not carry address: %#v", body)
			}
			if text(body["local_part"]) == "" || text(body["domain"]) != "example.com" {
				t.Errorf("unexpected create body: %#v", body)
			}
			writeJSON(w, http.StatusCreated, map[string]any{
				"address": "generated@example.com", "domain": "example.com", "token": "mailbox-scoped-token",
			})
		case "/v1/messages":
			// The Go backend's own message reads use the admin credential; the
			// per-mailbox token is what the Python worker uses to poll an inbox.
			if got := r.Header.Get("Authorization"); got != "Bearer admin-token" {
				t.Errorf("admin message read must use the admin token, got %q", got)
			}
			if r.URL.Query().Get("address") != "generated@example.com" {
				t.Errorf("unexpected address query: %q", r.URL.Query().Get("address"))
			}
			writeJSON(w, http.StatusOK, map[string]any{"results": []any{map[string]any{"id": 7.0, "raw": vpsRealOTPEmail}}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := newDomainMailClient(map[string]any{
		"provider":   "vps",
		"base_url":   server.URL,
		"auth_token": "admin-token",
		"domains":    []string{"example.com"},
	})
	if err != nil {
		t.Fatalf("build vps client: %v", err)
	}
	if client.provider != "vps" {
		t.Fatalf("unexpected provider %q", client.provider)
	}
	ctx := context.Background()
	address, token, err := client.createVPSMailbox(ctx, "abcdef123456", "example.com")
	if err != nil {
		t.Fatalf("create mailbox: %v", err)
	}
	if createAuth != "Bearer admin-token" {
		t.Fatalf("create must authenticate with the admin bearer, got %q", createAuth)
	}
	if address != "generated@example.com" || token != "mailbox-scoped-token" {
		t.Fatalf("unexpected create result: %q %q", address, token)
	}
	credential, err := vpsMailboxCredential(client.baseURL, address, token)
	if err != nil {
		t.Fatalf("build credential: %v", err)
	}
	var metadata map[string]string
	if err := json.Unmarshal([]byte(credential), &metadata); err != nil {
		t.Fatalf("credential must be JSON metadata: %v (%s)", err, credential)
	}
	if metadata["provider"] != "vps" || metadata["auth_token"] != "mailbox-scoped-token" || metadata["email"] != "generated@example.com" {
		t.Fatalf("unexpected credential metadata: %#v", metadata)
	}
	// The issued credential is scoped to the mailbox, never the admin token.
	if strings.Contains(credential, "admin-token") {
		t.Fatal("per-mailbox credential must not embed the admin token")
	}
	if err := validateDomainMailboxAccessKey(credential, "generated@example.com"); err != nil {
		t.Fatalf("issued credential must validate for its own mailbox: %v", err)
	}
	if err := validateDomainMailboxAccessKey(credential, "other@example.com"); err == nil {
		t.Fatal("credential must be rejected for a different mailbox")
	}
	messages, err := client.listMessages(ctx, address)
	if err != nil {
		t.Fatalf("list messages: %v", err)
	}
	items := domainMailVPSMessageItems(messages, address)
	if len(items) != 1 || items[0]["otp"] != "096480" {
		t.Fatalf("unexpected vps messages: %#v", items)
	}
}

func TestDomainMailVPSClientRejectsCreateWithoutToken(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// A response without a mailbox-scoped token must never be accepted.
		writeJSON(w, http.StatusCreated, map[string]any{"address": "generated@example.com", "domain": "example.com"})
	}))
	defer server.Close()
	client, err := newDomainMailClient(map[string]any{
		"provider":   "vps",
		"base_url":   server.URL,
		"auth_token": "admin-token",
		"domains":    []string{"example.com"},
	})
	if err != nil {
		t.Fatalf("build vps client: %v", err)
	}
	if _, _, err := client.createVPSMailbox(context.Background(), "abcdef123456", "example.com"); err == nil {
		t.Fatal("create must fail when the server omits the mailbox token")
	}
}

func TestRandomDomainLocalPartIsAlphanumeric(t *testing.T) {
	for i := 0; i < 20; i++ {
		value := randomDomainLocalPart(12)
		if len(value) != 12 {
			t.Fatalf("unexpected length %d for %q", len(value), value)
		}
		for _, r := range value {
			if !((r >= 'a' && r <= 'z') || (r >= '0' && r <= '9')) {
				t.Fatalf("local part %q contains an invalid character %q", value, r)
			}
		}
	}
	if got := len(randomDomainLocalPart(2)); got != 6 {
		t.Fatalf("short length must clamp to 6, got %d", got)
	}
	if got := len(randomDomainLocalPart(99)); got != 32 {
		t.Fatalf("long length must clamp to 32, got %d", got)
	}
}

func TestNewDomainMailClientAllowsVPSWithoutSitePassword(t *testing.T) {
	// The VPS flow authenticates with a single bearer token and has no site password.
	if _, err := newDomainMailClient(map[string]any{
		"provider":   "vps",
		"base_url":   "https://manage.example.com",
		"auth_token": "admin-token",
		"domains":    []string{"example.com"},
	}); err != nil {
		t.Fatalf("vps client must not require a site password: %v", err)
	}
	// The legacy CloudMail flow still requires it.
	if _, err := newDomainMailClient(map[string]any{
		"base_url":   "https://manage.example.com",
		"auth_token": "admin-token",
		"domains":    []string{"example.com"},
	}); err == nil {
		t.Fatal("cloudmail client must still require a site password")
	}
}

func TestDomainMailVPSCredentialImportLine(t *testing.T) {
	credential, err := json.Marshal(map[string]string{
		"provider":   "vps",
		"base_url":   "https://manage.example.com",
		"auth_token": "mailbox-scoped-token",
		"email":      "user@example.com",
	})
	if err != nil {
		t.Fatalf("marshal credential: %v", err)
	}
	parsed, err := parseSunnyMailboxLineForProvider("user@example.com----"+string(credential), "自建域名邮箱", "domain_api")
	if err != nil {
		t.Fatalf("import vps credential line: %v", err)
	}
	if parsed["email"] != "user@example.com" || parsed["access_key"] != string(credential) {
		t.Fatalf("unexpected parsed line: %#v", parsed)
	}
	if _, err := parseSunnyMailboxLineForProvider("other@example.com----"+string(credential), "自建域名邮箱", "domain_api"); err == nil {
		t.Fatal("import must reject a credential bound to a different mailbox")
	}
}
