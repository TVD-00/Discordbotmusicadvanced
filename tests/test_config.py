from __future__ import annotations

import pytest

import bot.config as config_module
from bot.config import load_config

_CONFIG_ENV_KEYS = [
    "DISCORD_TOKEN",
    "DEV_GUILD_ID",
    "LAVALINK_HOST",
    "LAVALINK_PORT",
    "LAVALINK_PASSWORD",
    "LAVALINK_SECURE",
    "LAVALINK_IDENTIFIER",
    "LAVALINK_NODES_JSON",
    "WAVELINK_CACHE_CAPACITY",
    "LAVALINK_NODE_RETRIES",
    "LAVALINK_PRIMARY_HEALTH_INTERVAL",
    "DEFAULT_VOLUME",
    "IDLE_TIMEOUT_SECONDS",
    "ANNOUNCE_NOWPLAYING",
    "DB_PATH",
    "LOG_LEVEL",
    "LOG_DIR",
    "LOG_FILE",
    "LOG_MAX_BYTES",
    "LOG_BACKUP_COUNT",
    "SUPPORT_INVITE_URL",
    "VOTE_URL",
]


@pytest.fixture(autouse=True)
def clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Test cần độc lập với file .env cục bộ để tránh flake.
    monkeypatch.setattr(config_module, "load_dotenv", lambda override=False: None)
    for key in _CONFIG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_load_config_with_primary_node_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("LAVALINK_HOST", "localhost")
    monkeypatch.setenv("LAVALINK_PASSWORD", "password")

    config = load_config()

    assert config.primary_lavalink_node is not None
    assert config.primary_lavalink_node.identifier == "primary"
    assert config.fallback_lavalink_nodes == ()
    assert len(config.lavalink_nodes) == 1
    assert config.lavalink_nodes[0].identifier == "primary"


def test_load_config_with_primary_and_fallback_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("LAVALINK_HOST", "primary.example.com")
    monkeypatch.setenv("LAVALINK_PASSWORD", "primary-pass")
    monkeypatch.setenv(
        "LAVALINK_NODES_JSON",
        '[{"identifier":"backup1","uri":"https://backup.example.com:443","password":"backup-pass"}]',
    )

    config = load_config()

    assert config.primary_lavalink_node is not None
    assert config.primary_lavalink_node.identifier == "primary"
    assert len(config.fallback_lavalink_nodes) == 1
    assert config.fallback_lavalink_nodes[0].identifier == "backup1"
    assert len(config.lavalink_nodes) == 2
    assert config.lavalink_nodes[0].identifier == "primary"
    assert config.lavalink_nodes[1].identifier == "backup1"


def test_load_config_reject_duplicate_identifier_between_primary_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("LAVALINK_HOST", "primary.example.com")
    monkeypatch.setenv("LAVALINK_PASSWORD", "primary-pass")
    monkeypatch.setenv(
        "LAVALINK_NODES_JSON",
        '[{"identifier":"primary","uri":"https://backup.example.com:443","password":"backup-pass"}]',
    )

    with pytest.raises(ValueError, match="Trùng identifier"):
        load_config()


def test_load_config_reject_invalid_secure_value_in_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv(
        "LAVALINK_NODES_JSON",
        '[{"identifier":"backup1","host":"backup.example.com","port":2333,"password":"backup-pass","secure":"sometimes"}]',
    )

    with pytest.raises(ValueError, match="boolean không hợp lệ"):
        load_config()


def test_load_config_requires_at_least_one_lavalink_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")

    with pytest.raises(ValueError, match="Thiếu cấu hình Lavalink"):
        load_config()
