package handlers


import (
	"testing"

	"github.com/flowie/backend/internal/domain"
)

// chỉ là unit test.
func TestDevelopmentWorkspaceNameUsesDisplayName(t *testing.T) {
	user := &domain.User{DisplayName: "Alice", Email: "alice@example.com"}

	got := developmentWorkspaceName(user)
	if got != "Alice Workspace" {
		t.Fatalf("expected development workspace name to use display name, got %q", got)
	}
}

func TestDevelopmentWorkspaceNameFallsBackToEmail(t *testing.T) {
	user := &domain.User{Email: "alice@example.com"}

	got := developmentWorkspaceName(user)
	if got != "alice@example.com Workspace" {
		t.Fatalf("expected development workspace name to fall back to email, got %q", got)
	}
}
