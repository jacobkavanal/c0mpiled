from purchasing_agent.supabase_repository import SupabaseRepository


def test_loads_only_non_null_fingerprints_from_hard_denials(monkeypatch) -> None:
    repository = SupabaseRepository("https://example.supabase.co", "service-role-key")
    captured = {}

    def fake_get(table, params):
        captured["table"] = table
        captured["params"] = params
        return [
            {"action_fingerprint": "a" * 64},
            {"action_fingerprint": None},
            {"action_fingerprint": "b" * 64},
        ]

    monkeypatch.setattr(repository, "_get", fake_get)

    assert repository.denied_action_fingerprints() == {"a" * 64, "b" * 64}
    assert captured == {
        "table": "purchase_attempts",
        "params": {
            "select": "action_fingerprint",
            "action_fingerprint": "not.is.null",
            "decision": "eq.block",
        },
    }
