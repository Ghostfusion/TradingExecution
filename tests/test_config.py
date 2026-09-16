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


def test_the_daemon_clock_is_naive_utc_not_host_local():
    """2026-09-15: `now_utc()` returned `datetime.now()` - host-local, mislabelled.

    Every age computation reads a naive stamp as UTC, so on the operator's
    US-Central box the daemon clock ran 5 h off: a five-hour-old quote looked
    fresh, which disabled the quote-age staleness check outright.
    """
    from datetime import UTC, datetime

    from signald.config import now_utc

    stamp = now_utc()
    assert stamp.tzinfo is None, "the convention is naive (tz-free)"
    assert abs((datetime.now(UTC).replace(tzinfo=None) - stamp).total_seconds()) < 2

    # ...and it must be UTC, not whatever the host happens to be set to.
    offset = abs(datetime.now().astimezone().utcoffset().total_seconds())
    observed = abs((datetime.now() - stamp).total_seconds())
    assert observed == pytest.approx(offset, abs=2)


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


def test_the_control_api_key_id_is_read_as_its_own_field(tmp_path):
    """`TRADINGEXEC_API_KEY_ID` is the control API's key, not `ALPACA_API_KEY_ID`.

    The alias table exists for Alpaca's names, so it must not rewrite our own
    (the control API silently came up keyless before this).
    """
    cfg = load_config(
        env_file=tmp_path / "absent.env",
        environ={"TRADINGEXEC_API_KEY_ID": "op-1", "TRADINGEXEC_API_SIGNING_SECRET": "s1"},
    )

    assert cfg.api_key_id == "op-1" and cfg.api_signing_secret == "s1"
    assert cfg.alpaca_key is None


def test_signal_mode_has_no_order_path(tmp_path):
    cfg = load_config(env_file=tmp_path / "absent.env", environ={"TRADINGEXEC_MODE": "signal"})
    assert cfg.mode == "signal" and not cfg.order_path_enabled and not cfg.live


def test_every_built_switch_defaults_on(tmp_path):
    """Owner decision 2026-09-13: the four switches ship ON (.env sets them too).

    Reachable is not sent: the order path still needs the caller's execute=True,
    `live` needs its second opt-in, and the intraday router needs the caller's
    `enabled` on top of the config flag.
    """
    cfg = load_config(env_file=tmp_path / "absent.env", environ={})
    assert cfg.mode == "paper" and cfg.order_path_enabled and not cfg.live
    assert cfg.intraday_enabled is True
    assert cfg.api_enabled is True
    assert cfg.mcp_enabled is True


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
