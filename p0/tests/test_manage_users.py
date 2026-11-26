from scripts import manage_users


def test_add_accepts_only_internal_project_roles(monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr(manage_users.getpass, "getpass", lambda prompt: prompts.append(prompt) or "TestPass!2026")
    created = []
    monkeypatch.setattr(manage_users.auth, "create_user", lambda *args, **kwargs: created.append((args, kwargs)))

    result = manage_users.main(["manage_users.py", "add", "tech-one", "technical", "质量部", "P-A"])

    assert result == 0
    assert created == [(("tech-one", "TestPass!2026"), {
        "role": "technical", "department": "质量部", "projects": ["P-A"]
    })]
    assert len(prompts) == 2
    assert "已创建 tech-one（technical）" in capsys.readouterr().out


def test_add_rejects_legacy_and_external_customer_roles_before_password_prompt(monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr(manage_users.getpass, "getpass", lambda prompt: prompts.append(prompt) or "unused")

    for role in ("reviewer", "sales", "viewer", "customer"):
        assert manage_users.main(["manage_users.py", "add", "person", role]) == 2

    assert prompts == []
    error = capsys.readouterr().err
    assert "business_manager" in error
    assert "小程序身份服务" in error


def test_add_usage_lists_confirmed_four_role_names(capsys):
    assert manage_users.main(["manage_users.py", "add", "person"]) == 2
    error = capsys.readouterr().err
    assert "admin|business_manager|technical" in error
    assert "reviewer" not in error
