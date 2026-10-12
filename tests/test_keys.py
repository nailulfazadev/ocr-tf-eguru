import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import pytest
import app.sheets as sh

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "APP_DIR", tmp_path / "d")
    monkeypatch.setattr(sh, "SERVICE_ACCOUNT_PATH", tmp_path / "d" / "service_account.json")
    monkeypatch.setattr(sh, "CLIENT_SECRET_PATH", tmp_path / "d" / "client_secret.json")
    monkeypatch.chdir(tmp_path)
    return tmp_path

def test_service_account_gets_correct_name_even_if_file_named_otherwise(home):
    src = home / "n8n-integration-123-abcdef.json"
    src.write_text(json.dumps({"type": "service_account", "private_key": "k", "client_email": "a@b.iam.gserviceaccount.com"}))
    kind, info = sh.install_key(str(src))
    assert kind == "service_account" and info == "a@b.iam.gserviceaccount.com"
    assert (home / "d" / "service_account.json").exists() and not (home / "d" / "client_secret.json").exists()
    assert "a@b.iam.gserviceaccount.com" in sh.key_status()

def test_oauth_client_goes_to_client_secret(home):
    src = home / "x.json"; src.write_text(json.dumps({"installed": {"client_id": "1"}}))
    assert sh.install_key(str(src))[0] == "oauth"
    assert (home / "d" / "client_secret.json").exists()

@pytest.mark.parametrize("content", ["bukan json", "[1,2]", json.dumps({"foo": "bar"})])
def test_rejects_non_key_files(home, content):
    src = home / "z.json"; src.write_text(content)
    with pytest.raises(ValueError):
        sh.install_key(str(src))
