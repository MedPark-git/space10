from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_required_files_exist():
    required = ["app.py", "requirements.txt", "runtime.txt", "Procfile", ".gitignore", ".env.example",
                "README.md", "RESTORE_GUIDE_KO.md", "BACKUP_METADATA.json", "templates", "static", "migrations", "tests", "user_data"]
    assert all((ROOT / path).exists() for path in required)


def test_runtime_contract():
    app = (ROOT / "app.py").read_text()
    procfile = (ROOT / "Procfile").read_text()
    assert "DB_USER" in app and "DB_USERNAME" not in app
    assert "sqlite" not in app.lower()
    assert "postgresql+psycopg" in app
    assert "generate_password_hash" in app and "check_password_hash" in app
    assert "--workers 2 --threads 4 --timeout 120" in procfile


def test_no_bootstrap_secret_committed():
    forbidden = "medpark" + "1!"
    for path in ROOT.rglob("*"):
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts and path.name != "test_security_contract.py":
            assert forbidden not in path.read_text(errors="ignore")


def test_gitignore_protects_runtime_data():
    content = (ROOT / ".gitignore").read_text()
    for pattern in [".env", "user_data/*", "*.db", "*.sqlite", "*.sql", "*.dump", "*.log"]:
        assert pattern in content
