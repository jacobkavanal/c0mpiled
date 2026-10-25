import pytest

from gateway import simulate

VOICE_ENV = (
    "VOICE_PROVIDER",
    "VOICE_MODEL",
    "VOICE_EFFORT",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in VOICE_ENV:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_reads_gemini_key_and_settings_from_dotenv(workdir):
    (workdir / ".env").write_text("GEMINI_API_KEY=gm-test\nVOICE_EFFORT=medium\n")

    settings = simulate.SimulatorSettings()

    assert settings.voice_provider == "gemini"
    assert settings.gemini_api_key.get_secret_value() == "gm-test"
    assert settings.resolved_voice_model == "gemini-3.5-flash-lite"
    assert settings.resolved_voice_effort == "medium"


def test_environment_variable_overrides_dotenv(workdir, monkeypatch):
    (workdir / ".env").write_text("GEMINI_API_KEY=from-file\n")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")

    assert simulate.SimulatorSettings().gemini_api_key.get_secret_value() == "from-env"


def test_missing_gemini_key_exits_with_instructions(workdir, capsys):
    with pytest.raises(SystemExit) as excinfo:
        simulate.require_credentials(simulate.SimulatorSettings())

    assert excinfo.value.code == 1
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_missing_anthropic_key_exits_with_instructions(workdir, capsys):
    (workdir / ".env").write_text("VOICE_PROVIDER=anthropic\n")

    with pytest.raises(SystemExit):
        simulate.require_credentials(simulate.SimulatorSettings())

    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_credentials_present_passes(workdir):
    (workdir / ".env").write_text("GEMINI_API_KEY=gm-test\n")

    simulate.require_credentials(simulate.SimulatorSettings())
