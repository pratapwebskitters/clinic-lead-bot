import importlib

import dotenv
import pytest
import yaml
from fastapi.testclient import TestClient

from app import clinics, config
from app.main import app


@pytest.fixture
def clinic_dir(tmp_path, monkeypatch):
    path = tmp_path / "clinics"
    path.mkdir()
    monkeypatch.setenv("CLINICS_DIR", str(path))
    return path


@pytest.fixture
def clinic_data():
    return {
        "clinic_name": "Example Dental", "booking_link": "https://example.com/book",
        "phone": "+911234567890", "location": "10 Main Street", "timings": "Mon-Fri 9-5",
        "prices": {"consultation": "₹300"}, "mode": "shadow",
    }


def write_clinic(directory, data, name="example.yaml"):
    path = directory / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")


def test_load_validated_clinic_with_optional_fields(clinic_dir, clinic_data):
    clinic_data.update(maps_link="https://example.com/maps", doctors=["Dr Example"],
                       services=["Cleaning"], owner_telegram_chat_id=-123456,
                       languages=["English"], payment_options="Cash")
    write_clinic(clinic_dir, clinic_data)
    loaded = clinics.load_clinic("EXAMPLE")
    assert loaded["clinic_id"] == "example"
    assert loaded["booking_link"] == clinic_data["booking_link"]
    assert loaded["maps_link"] == clinic_data["maps_link"]
    assert loaded["prices"] == {"consultation": "₹300"}
    assert loaded["owner_telegram_chat_id"] == -123456


@pytest.mark.parametrize("data", [[], ["clinic"], "scalar", 42, None, {}])
def test_invalid_yaml_shape_has_clear_filename(clinic_dir, data):
    write_clinic(clinic_dir, data)
    with pytest.raises(clinics.ClinicConfigError, match="example.yaml") as exc:
        clinics.load_clinic("example")
    assert "mapping" in str(exc.value) or "Field required" in str(exc.value)


@pytest.mark.parametrize("field", [
    "clinic_name", "booking_link", "phone", "location", "timings", "prices", "mode",
])
def test_required_clinic_fields(clinic_dir, clinic_data, field):
    del clinic_data[field]
    write_clinic(clinic_dir, clinic_data)
    with pytest.raises(clinics.ClinicConfigError, match=field):
        clinics.load_clinic("example")


@pytest.mark.parametrize("field,value", [
    ("booking_link", "ftp://example.com/book"), ("booking_link", "not a URL"),
    ("prices", {"consultation": 300}), ("prices", ["₹300"]),
    ("mode", "automatic"), ("clinic_name", ""),
    ("maps_link", "javascript:alert(1)"),
])
def test_invalid_clinic_field_types(clinic_dir, clinic_data, field, value):
    clinic_data[field] = value
    write_clinic(clinic_dir, clinic_data)
    with pytest.raises(clinics.ClinicConfigError, match=field):
        clinics.load_clinic("example")


def test_yaml_syntax_error_names_file(clinic_dir):
    (clinic_dir / "broken.yaml").write_text("clinic_name: [broken", encoding="utf-8")
    with pytest.raises(clinics.ClinicConfigError, match="broken.yaml"):
        clinics.load_clinic("broken")


@pytest.mark.parametrize("app_env,mode,rejected", [
    ("development", "shadow", False), ("development", "auto", True),
    ("production", "shadow", True), ("production", "auto", True),
])
def test_placeholder_rules(clinic_dir, clinic_data, monkeypatch, app_env, mode, rejected):
    monkeypatch.setenv("APP_ENV", app_env)
    clinic_data.update(mode=mode, owner_telegram_chat_id="REPLACE_WITH_CHAT_ID")
    write_clinic(clinic_dir, clinic_data)
    if rejected:
        with pytest.raises(clinics.ClinicConfigError, match="owner_telegram_chat_id"):
            clinics.load_clinic("example")
    else:
        assert clinics.load_clinic("example")["mode"] == "shadow"


def test_nested_placeholder_rejected_in_auto_mode(clinic_dir, clinic_data):
    clinic_data.update(mode="auto", prices={"consultation": "REPLACE_WITH_APPROVED_PRICE"})
    write_clinic(clinic_dir, clinic_data)
    with pytest.raises(clinics.ClinicConfigError, match="prices.consultation"):
        clinics.load_clinic("example")


def test_development_validates_every_file_and_warns(clinic_dir, clinic_data, monkeypatch, caplog):
    monkeypatch.setenv("APP_ENV", "development")
    write_clinic(clinic_dir, clinic_data)
    write_clinic(clinic_dir, [], "bad_list.yaml")
    write_clinic(clinic_dir, {"mode": "shadow"}, "missing.yaml")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert "bad_list.yaml" in caplog.text and "mapping" in caplog.text
    assert "missing.yaml" in caplog.text and "clinic_name" in caplog.text


def test_production_rejects_invalid_clinic_at_startup(clinic_dir, clinic_data, monkeypatch):
    production(monkeypatch)
    write_clinic(clinic_dir, clinic_data)
    write_clinic(clinic_dir, [], "bad_list.yaml")
    write_clinic(clinic_dir, {}, "missing.yaml")
    with pytest.raises(clinics.ClinicConfigError) as exc:
        with TestClient(app):
            pass
    assert "bad_list.yaml" in str(exc.value)
    assert "missing.yaml" in str(exc.value)


@pytest.mark.parametrize("host,sender,missing", [
    ("", "", ["SMTP_HOST", "SMTP_FROM"]),
    ("smtp.example.com", "", ["SMTP_FROM"]),
    ("", "clinic@example.com", ["SMTP_HOST"]),
])
def test_production_auto_clinic_requires_smtp(clinic_dir, clinic_data, monkeypatch, host, sender, missing):
    production(monkeypatch)
    write_clinic(clinic_dir, clinic_data, "shadow.yaml")
    write_clinic(clinic_dir, dict(clinic_data, mode="auto"), "auto.yaml")
    monkeypatch.setenv("SMTP_HOST", host)
    monkeypatch.setenv("SMTP_FROM", sender)
    with pytest.raises(RuntimeError) as exc:
        with TestClient(app):
            pass
    assert all(name in str(exc.value) for name in missing)


@pytest.mark.parametrize("app_env,mode,smtp", [
    ("production", "auto", True), ("production", "shadow", False),
    ("development", "auto", False),
])
def test_startup_accepts_valid_configuration(clinic_dir, clinic_data, monkeypatch, app_env, mode, smtp):
    production(monkeypatch)
    monkeypatch.setenv("APP_ENV", app_env)
    write_clinic(clinic_dir, dict(clinic_data, mode=mode))
    if smtp:
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_FROM", "clinic@example.com")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_dotenv_loads_without_overriding_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("WEBHOOK_SECRET=from-file\nLLM_MODEL=model-from-file\n", encoding="utf-8")
    monkeypatch.setenv("WEBHOOK_SECRET", "from-environment")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    original_load = dotenv.load_dotenv
    calls = []

    def load_test_env(path, **kwargs):
        calls.append((path, kwargs))
        return original_load(env_file, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(dotenv, "load_dotenv", load_test_env)
        importlib.reload(config)
    try:
        settings = config.get_settings()
        assert settings.webhook_secret == "from-environment"
        assert settings.llm_model == "model-from-file"
        assert calls == [(config.BASE_DIR / ".env", {"override": False})]
    finally:
        config.load_dotenv = original_load
