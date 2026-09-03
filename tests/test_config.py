"""Config load/override tests (env-file + alias mapping)."""

from __future__ import annotations

from signald.config import load_config

pytestmark = __import__("pytest").mark.timeout(120)


def test_env_file_alias_mapping(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "ALPACA_API_KEY=key123\nALPACA_SECRET_KEY=sec456\nALPACA_PAPER=true\n",
        encoding="utf-8",
    )
    cfg = load_config(env_file=env, watch_dir=tmp_path / "w", data_dir=tmp_path / "s")
    assert cfg.alpaca_key == "key123"
    assert cfg.alpaca_secret == "sec456"
    assert cfg.alpaca_paper is True


def test_none_overrides_skipped(tmp_path):
    cfg = load_config(env_file=None, watch_dir=None, data_dir=None)
    # no crash; defaults remain
    assert cfg.watch_dir is not None


def test_config_hash_stable_excludes_secrets(tmp_path):
    cfg1 = load_config(env_file=None, alpaca_key="a", alpaca_secret="b")
    cfg2 = load_config(env_file=None, alpaca_key="c", alpaca_secret="d")
    assert cfg1.config_hash() == cfg2.config_hash()  # secrets excluded
    assert cfg1.config_hash() == cfg1.config_hash()  # deterministic
