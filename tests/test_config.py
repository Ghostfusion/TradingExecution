"""Config load/override tests (env-file + alias mapping)."""

from __future__ import annotations

import pytest

from signald.config import load_config

pytestmark = pytest.mark.timeout(120)


def test_env_file_alias_mapping(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_ALPACA_API_SECRET", raising=False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "ALPACA_API_KEY=key123\nALPACA_SECRET_KEY=sec456\nALPACA_PAPER=true\n",
        encoding="utf-8",
    )
    cfg = load_config(env_file=env, watch_dir=tmp_path / "w", data_dir=tmp_path / "s")
    assert cfg.alpaca_key == "key123"
    assert cfg.alpaca_secret == "sec456"
    assert cfg.alpaca_paper is True


def test_the_research_repo_env_alias_is_rejected(tmp_path, monkeypatch):
    """Boundary rule (design §2.2 rule 3): execution owns its keys.

    The research repo's TRADINGAGENTS_ALPACA_* names are deliberately not a
    supported input - accepting them would make the sibling repo's .env part of
    this process. This inverts the Phase-A behaviour on purpose.
    """
    monkeypatch.setenv("TRADINGAGENTS_ALPACA_API_KEY_ID", "not-a-real-key")
    monkeypatch.setenv("TRADINGAGENTS_ALPACA_API_SECRET", "not-a-real-secret")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    cfg = load_config(
        env_file=tmp_path / "absent.env",  # never read the developer's .env
        watch_dir=tmp_path / "w",
        data_dir=tmp_path / "s",
    )

    assert cfg.alpaca_key is None
    assert cfg.alpaca_secret is None


def test_execution_owned_alpaca_keys_are_accepted(tmp_path, monkeypatch):
    """The supported path: keys copied into this repo's own .env."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("ALPACA_API_KEY=own-key\nALPACA_SECRET_KEY=own-secret\n", encoding="utf-8")

    cfg = load_config(env_file=env, watch_dir=tmp_path / "w", data_dir=tmp_path / "s")

    assert cfg.alpaca_key == "own-key"
    assert cfg.alpaca_secret == "own-secret"


def test_v2_keys_load_from_the_environment(tmp_path):
    environ = {
        "TRADINGEXEC_MODE": "paper",
        "TRADINGEXEC_SLEEVE_SWING_CAPITAL_PCT": "0.6",
        "TRADINGEXEC_MAX_POSITIONS_INTRADAY": "3",
        "TRADINGEXEC_INTRADAY_ENABLED": "true",
        "TRADINGEXEC_INTRADAY_FLAT_BY": "15:45",
        "TRADINGEXEC_INTRADAY_ENTRY_AFTER": "09:40",
        "TRADINGEXEC_API_BIND": "127.0.0.1:9000",
        "TRADINGEXEC_DATA_FEED": "iex",
    }

    cfg = load_config(env_file=tmp_path / "absent.env", environ=environ)

    assert cfg.mode == "paper" and cfg.order_path_enabled
    assert cfg.sleeve_swing_capital_pct == 0.6
    assert cfg.max_positions_intraday == 3
    assert cfg.intraday_enabled is True
    assert cfg.intraday_flat_by == "15:45"
    assert cfg.api_bind == "127.0.0.1:9000"
    assert cfg.data_feed == "iex"


def test_signal_mode_has_no_order_path(tmp_path):
    cfg = load_config(env_file=tmp_path / "absent.env", environ={})
    assert cfg.mode == "signal" and not cfg.order_path_enabled and not cfg.live


@pytest.mark.parametrize(
    "environ",
    [
        {"TRADINGEXEC_MODE": "turbo"},
        {"TRADINGEXEC_SLEEVE_SWING_CAPITAL_PCT": "0.9"},  # 0.9 + 0.3 > 1.0
        {"TRADINGEXEC_INTRADAY_FLAT_BY": "15h50"},
        {"TRADINGEXEC_STRESS_CORRELATION": "1.0"},
    ],
)
def test_a_misconfigured_process_refuses_to_start(tmp_path, environ):
    with pytest.raises(ValueError):
        load_config(env_file=tmp_path / "absent.env", environ=environ)


def test_api_signing_secret_is_never_hashed_or_serialised(tmp_path):
    base = {"TRADINGEXEC_API_KEY_ID": "key-1"}
    one = load_config(
        env_file=tmp_path / "absent.env",
        environ={**base, "TRADINGEXEC_API_SIGNING_SECRET": "s1"},
    )
    two = load_config(
        env_file=tmp_path / "absent.env",
        environ={**base, "TRADINGEXEC_API_SIGNING_SECRET": "s2"},
    )
    assert one.config_hash() == two.config_hash()


def test_none_overrides_skipped(tmp_path):
    cfg = load_config(env_file=None, watch_dir=None, data_dir=None)
    # no crash; defaults remain
    assert cfg.watch_dir is not None


def test_config_hash_stable_excludes_secrets(tmp_path):
    cfg1 = load_config(env_file=None, alpaca_key="a", alpaca_secret="b")
    cfg2 = load_config(env_file=None, alpaca_key="c", alpaca_secret="d")
    assert cfg1.config_hash() == cfg2.config_hash()  # secrets excluded
    assert cfg1.config_hash() == cfg1.config_hash()  # deterministic
