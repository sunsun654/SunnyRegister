package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestFetchOutlookMailSubjectsUsesGraphSubjectAndPreview(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/token":
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"access_token":"graph-token"}`)
		case "/messages":
			if got := r.Header.Get("Authorization"); got != "Bearer graph-token" {
				t.Fatalf("unexpected authorization header: %s", got)
			}
			if got := r.URL.Query().Get("$select"); got != "subject,bodyPreview" {
				t.Fatalf("health query must request subject and body preview, got %q", got)
			}
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"value":[{"subject":"Welcome","bodyPreview":"Weekly update"},{"subject":"Account notice [C-ABC123]","bodyPreview":"Access deactivated"}]}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	originalGraphEndpoints := hotmailGraphTokenEndpoints
	originalIMAPEndpoints := hotmailTokenEndpoints
	originalMessagesURL := outlookGraphMessagesURL
	hotmailGraphTokenEndpoints = []hotmailTokenEndpoint{{Name: "GRAPH-TEST", URL: server.URL + "/token"}}
	hotmailTokenEndpoints = nil
	outlookGraphMessagesURL = server.URL + "/messages"
	t.Cleanup(func() {
		hotmailGraphTokenEndpoints = originalGraphEndpoints
		hotmailTokenEndpoints = originalIMAPEndpoints
		outlookGraphMessagesURL = originalMessagesURL
	})

	subjects, err := fetchOutlookMailSubjects("user@outlook.com", "client-id", "refresh-token", 5, "")
	if err != nil {
		t.Fatalf("Graph subject query failed: %v", err)
	}
	if got := strings.Join(subjects, "|"); got != "Welcome\nWeekly update|Account notice [C-ABC123]\nAccess deactivated" {
		t.Fatalf("unexpected subjects: %s", got)
	}
}

func TestFetchMailSubjectsViaGraphRespectsProxyValidation(t *testing.T) {
	_, err := fetchMailSubjectsViaGraph("token", 5, "://bad-proxy")
	if err == nil || !strings.Contains(err.Error(), "invalid Graph proxy URL") {
		t.Fatalf("expected proxy validation error, got %v", err)
	}
	if _, parseErr := url.Parse("://bad-proxy"); parseErr == nil {
		t.Fatal("test proxy must remain invalid")
	}
}

func TestFetchOutlookMailSubjectsFallsBackToIMAP(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"access_token":"mail-token"}`)
	}))
	defer server.Close()

	originalGraphEndpoints := hotmailGraphTokenEndpoints
	originalIMAPEndpoints := hotmailTokenEndpoints
	originalGraphFetch := sunnyFetchMailSubjectsViaGraph
	originalIMAPFetch := sunnyFetchMailHeadersViaIMAP
	hotmailGraphTokenEndpoints = []hotmailTokenEndpoint{{Name: "GRAPH-TEST", URL: server.URL}}
	hotmailTokenEndpoints = []hotmailTokenEndpoint{{Name: "IMAP-TEST", URL: server.URL}}
	sunnyFetchMailSubjectsViaGraph = func(string, int, string) ([]string, error) {
		return nil, fmt.Errorf("Graph permission unavailable")
	}
	sunnyFetchMailHeadersViaIMAP = func(email, token string, limit int, proxy string) ([]string, error) {
		if email != "user@outlook.com" || token != "mail-token" || limit != 5 {
			t.Fatalf("unexpected IMAP fallback arguments: %s %s %d", email, token, limit)
		}
		return []string{"IMAP subject"}, nil
	}
	t.Cleanup(func() {
		hotmailGraphTokenEndpoints = originalGraphEndpoints
		hotmailTokenEndpoints = originalIMAPEndpoints
		sunnyFetchMailSubjectsViaGraph = originalGraphFetch
		sunnyFetchMailHeadersViaIMAP = originalIMAPFetch
	})

	subjects, err := fetchOutlookMailSubjects("user@outlook.com", "client-id", "refresh-token", 5, "")
	if err != nil {
		t.Fatalf("IMAP fallback failed: %v", err)
	}
	if len(subjects) != 1 || subjects[0] != "IMAP subject" {
		t.Fatalf("unexpected IMAP subjects: %#v", subjects)
	}
}

func TestSunnyHealthCheckConcurrencyDefaultsAndBounds(t *testing.T) {
	server := &Server{maintenance: map[string]any{"health_concurrency": 4}}
	if got := server.sunnyHealthCheckConcurrency(); got != 4 {
		t.Fatalf("configured concurrency = %d, want 4", got)
	}
	server.maintenance["health_concurrency"] = 99
	if got := server.sunnyHealthCheckConcurrency(); got != 16 {
		t.Fatalf("max concurrency = %d, want 16", got)
	}
}

// TestSunnyHealthTaskDomainMailboxUsesDomainApi is a regression test for a
// self-hosted domain mailbox being routed into the Outlook OAuth fallback,
// which failed with "The provided request must include a 'client_id' input
// parameter" because domain mailboxes carry no Microsoft client_id.
func TestSunnyHealthTaskDomainMailboxUsesDomainApi(t *testing.T) {
	s := newSunnySessionTestServer(t)

	var mailbox SunnyMailbox
	if err := s.db.Where("email = ?", "session@example.com").First(&mailbox).Error; err != nil {
		t.Fatalf("load mailbox: %v", err)
	}
	// Turn the seeded Outlook-style mailbox into a domain mailbox.
	if err := s.db.Model(&SunnyMailbox{}).Where("id = ?", mailbox.ID).Updates(map[string]any{
		"mailbox_type":    "domain",
		"mailbox_channel": "domain_api",
		"access_key":      `{"provider":"vps","base_url":"https://manage.example.com","auth_token":"t","email":"session@example.com"}`,
		"client_id":       "",
		"refresh_token":   "",
	}).Error; err != nil {
		t.Fatalf("convert mailbox to domain: %v", err)
	}

	// The Outlook fallbacks must never be consulted for a domain mailbox.
	previousFetch := sunnyFetchOutlookMailSubjects
	previousGraph := sunnyFetchMailSubjectsViaGraph
	previousIMAP := sunnyFetchMailHeadersViaIMAP
	sunnyFetchOutlookMailSubjects = func(string, string, string, int, string) ([]string, error) {
		return nil, fmt.Errorf("Outlook IMAP path must not be used for a domain mailbox")
	}
	sunnyFetchMailSubjectsViaGraph = func(string, int, string) ([]string, error) {
		return nil, fmt.Errorf("Outlook Graph path must not be used for a domain mailbox")
	}
	sunnyFetchMailHeadersViaIMAP = func(string, string, int, string) ([]string, error) {
		return nil, fmt.Errorf("Outlook IMAP headers path must not be used for a domain mailbox")
	}
	t.Cleanup(func() {
		sunnyFetchOutlookMailSubjects = previousFetch
		sunnyFetchMailSubjectsViaGraph = previousGraph
		sunnyFetchMailHeadersViaIMAP = previousIMAP
	})

	// The domain provider API is the expected route; returning a normal
	// notification proves the dispatch reached it. Asserting the path also
	// pins the vps dialect: the legacy CloudMail fallback would request
	// POST /api/public/emailList and fail with HTTP 404.
	var requestedPath string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestedPath = r.URL.Path
		if r.URL.Path != "/v1/messages" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"results":[{"id":"m1","created_at":"2026-09-21T00:00:00Z","raw":"Content-Type: text/plain; charset=utf-8\r\nFrom: ChatGPT <noreply@tm.openai.com>\r\nSubject: Weekly update\r\n\r\nAll good\r\n"}]}`)
	}))
	defer upstream.Close()
	if err := s.db.Model(&SunnyMailbox{}).Where("id = ?", mailbox.ID).Update("access_key",
		fmt.Sprintf(`{"provider":"vps","base_url":%q,"auth_token":"t","email":"session@example.com"}`, upstream.URL),
	).Error; err != nil {
		t.Fatalf("point mailbox at test upstream: %v", err)
	}

	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	task := s.createTask(sunnyHealthTaskType, "sunny", map[string]any{"session_ids": []uint{session.ID}}, 1)
	s.executeSunnyAccountHealthCheckTask(&task, map[string]any{"session_ids": []uint{session.ID}})

	if err := s.db.First(&task, "id = ?", task.ID).Error; err != nil {
		t.Fatalf("reload task: %v", err)
	}
	result := jsonMap(task.ResultJSON)
	if requestedPath != "/v1/messages" {
		t.Fatalf("domain mailbox must be read through the vps API, got path %q", requestedPath)
	}
	if intValue(result["failed"], 0) != 0 {
		items, _ := result["items"].([]any)
		t.Fatalf("domain mailbox health check must not fail: %#v", items)
	}
	if intValue(result["alive"], 0) != 1 {
		t.Fatalf("expected the domain mailbox to be reported alive, got result=%#v", result)
	}

	// The stored failure must be cleared now that the check succeeded.
	if err := s.db.Where("email = ?", session.Email).First(&session).Error; err != nil {
		t.Fatalf("reload session: %v", err)
	}
	if session.HealthCheckStatus != "alive" || session.HealthCheckError != "" {
		t.Fatalf("unexpected session health state: status=%q error=%q", session.HealthCheckStatus, session.HealthCheckError)
	}
}
