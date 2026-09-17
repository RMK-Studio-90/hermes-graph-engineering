"""PUBLIC_SANITIZER_HITS = 0, with negative controls proving each rule fires.

Planted strings are assembled at runtime so this file stays clean itself.
"""
import boundary_check

BRAND = "ACME" + "CORP"
DRIVE_PATH = "E" + ":" + "\\" + "private\\dir"
PRIVATE_MAIL = "someone" + "@" + "private-mail.dev"
GITHUB_TOKEN = "gh" + "p_" + "A" * 36
CREDENTIAL = "api_key" + ' = "' + "q" * 16 + '"'


def _plant(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rules(hits):
    return sorted((h.path, h.rule) for h in hits)


def test_public_tree_has_zero_unauthorized_hits(repo_root, policy):
    hits = boundary_check.sanitize(repo_root, policy)
    assert hits == [], "\n".join("%s:%d [%s] %s" % (h.path, h.line, h.rule, h.excerpt) for h in hits)


def test_policy_file_is_the_only_exemption(policy):
    assert policy["exempt_files"] == ["scripts/sanitizer_policy.json"]


def test_negative_controls_strict_zone(tmp_path, test_policy):
    _plant(tmp_path, "src/pkg/a.py", "NAME = '%s Studio'\n" % BRAND)
    _plant(tmp_path, "docs/b.md", "see %s\n" % DRIVE_PATH)
    _plant(tmp_path, "docs/c.md", "mail %s\n" % PRIVATE_MAIL)
    _plant(tmp_path, "examples/d.yaml", "token: %s\n" % GITHUB_TOKEN)
    _plant(tmp_path, "src/pkg/e.py", CREDENTIAL + "\n")
    _plant(tmp_path, "docs/%s-notes.md" % BRAND.lower(), "clean\n")
    assert _rules(boundary_check.sanitize(tmp_path, test_policy)) == sorted([
        ("src/pkg/a.py", "private-brand"),
        ("docs/b.md", "windows-drive-path"),
        ("docs/c.md", "email-address"),
        ("examples/d.yaml", "github-token"),
        ("src/pkg/e.py", "credential-assignment"),
        ("docs/%s-notes.md" % BRAND.lower(), "private-brand"),
    ])


def test_relaxed_zone_allows_integration_name_but_not_secrets_or_paths(tmp_path, test_policy):
    zone = test_policy["relaxed_zones"][0]
    _plant(tmp_path, zone + "adapter.py", "NAME = '%s'\n" % BRAND)
    _plant(tmp_path, zone + "config.md", "root: %s\n" % DRIVE_PATH)
    _plant(tmp_path, zone + "secret.py", "token = '%s'\n" % GITHUB_TOKEN)
    assert _rules(boundary_check.sanitize(tmp_path, test_policy)) == sorted([
        (zone + "config.md", "windows-drive-path"),
        (zone + "secret.py", "credential-assignment"),
        (zone + "secret.py", "github-token"),
    ])


def test_allowed_placeholders_are_not_hits(tmp_path, test_policy):
    _plant(tmp_path, "docs/ok.md", "\n".join([
        "contact: maintainer" + "@" + "example.com",
        "https://github.com/example/project",
        "workspace: ${GE_WORKSPACE}",
        "api_key_ref: env:EXAMPLE_API_KEY",
    ]))
    assert boundary_check.sanitize(tmp_path, test_policy) == []


def test_excluded_dirs_are_skipped(tmp_path, test_policy):
    _plant(tmp_path, ".venv/lib/x.py", BRAND + "\n")
    _plant(tmp_path, ".private/notes.md", DRIVE_PATH + "\n")
    assert boundary_check.sanitize(tmp_path, test_policy) == []


def test_public_policy_names_no_private_markers(public_policy):
    """The published policy must stay generic; private markers live only in the local overlay."""
    text = (boundary_check.POLICY_FILE).read_text(encoding="utf-8").lower()
    assert public_policy["relaxed_zones"] == []
    for marker in ("acme", "berichts", "videograf", "k0"):
        assert marker not in text


def test_local_overlay_only_tightens(tmp_path, public_policy):
    import json
    import shutil

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(boundary_check.POLICY_FILE, scripts / "sanitizer_policy.json")
    (tmp_path / ".private").mkdir()
    (tmp_path / ".private" / "sanitizer_policy.local.json").write_text(json.dumps({
        "strict_rules": [{"id": "local-brand", "regex": "(?i)" + BRAND}],
        "import_policy": {"forbidden_prefixes": ["acme"], "allowed_third_party": ["requests"]},
    }), encoding="utf-8")
    merged = boundary_check.load_policy(scripts / "sanitizer_policy.json")
    assert len(merged["strict_rules"]) == len(public_policy["strict_rules"]) + 1
    assert "acme" in merged["import_policy"]["forbidden_prefixes"]
    assert merged["import_policy"]["allowed_third_party"] == public_policy["import_policy"]["allowed_third_party"]
    _plant(tmp_path, "docs/x.md", BRAND + "\n")
    assert [h.rule for h in boundary_check.sanitize(tmp_path, merged)] == ["local-brand"]


def test_secret_rules_fire_on_fake_credentials(tmp_path, public_policy):
    fake = {
        "a.md": "bot " + "123456789" + ":" + "A" * 35,
        "b.md": "Authorization: " + "Bearer " + "x" * 20,
        "c.md": "hook https://" + "discord.com/api/webhooks/1/abc",
        "d.md": "host " + "192.168" + ".1.20",
        "e.md": "path /home/" + "someone/project",
    }
    for rel, text in fake.items():
        _plant(tmp_path, rel, text + "\n")
    assert _rules(boundary_check.sanitize(tmp_path, public_policy)) == sorted([
        ("a.md", "telegram-bot-token"), ("b.md", "authorization-header"), ("c.md", "webhook-url"),
        ("d.md", "private-ipv4"), ("e.md", "home-directory-path"),
    ])
