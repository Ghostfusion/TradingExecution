"""Contract-schema conformance: the published JSON is the contract the code enforces.

The three files under ``contracts/`` are the published interface; the code that
enforces them lives in ``signald.contracts`` (envelope) and ``signald.schema``
(body). There is no ``jsonschema`` dependency (stdlib only), so these tests
prove conformance by driving the real validators rather than by re-implementing
schema evaluation. The independence scan at the bottom mirrors the ``independence``
job in ``.github/workflows/ci.yml`` so it also runs locally.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime
from pathlib import Path

import pytest

from signald.contracts import (
    GATE_PRECEDENCE,
    SLEEVES,
    TRADE_PERMISSIONS,
    EnvelopeError,
    validate_envelope,
)
from signald.samples import build_sample, build_sample_v11
from signald.schema import (
    VALID_DATA_QUALITY,
    VALID_DIRECTIONS,
    ContractError,
    SignalContract,
    parse_research_decision,
    sha256_of,
)

pytestmark = pytest.mark.timeout(120)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = REPO_ROOT / "contracts"
SCHEMA_FILES = {
    "research_decision": "research_decision.v1.schema.json",
    "signal": "signal.v2.schema.json",
    "order_intent": "order_intent.v1.schema.json",
}
NOW = datetime(2026, 9, 12, 14, 0)

#: SignalContract's Phase-A field names (everything above the ``v2`` block in
#: ``signald/schema.py``); the v2 additions are the complement of this set.
PHASE_A_SIGNAL_FIELDS = frozenset(
    {
        "symbol",
        "action",
        "target_pct",
        "expiry",
        "decision_hash",
        "timestamp",
        "target_notional_usd",
        "score",
        "confidence",
        "target_price",
        "stop_price",
        "max_position_pct",
        "strategy",
        "reason",
    }
)


# --- helpers ---------------------------------------------------------------
def load(name: str) -> dict:
    return json.loads((CONTRACTS / SCHEMA_FILES[name]).read_text(encoding="utf-8"))


def enum_of(schema: dict, key: str) -> list:
    """Enum members of a property, dropping the ``null`` of a nullable enum."""
    return [m for m in schema["properties"][key]["enum"] if m is not None]


def v11(**overrides) -> dict:
    return build_sample_v11(effective_date=NOW.date(), **overrides)


def rehash(doc: dict) -> dict:
    """Recompute the hashes a producer would, after a mutation.

    ``artifact_sha256`` is only recomputed when it is already present, so a test
    that deletes it keeps testing its requiredness rather than re-creating it.
    """
    if "artifact_sha256" in doc:
        doc["artifact_sha256"] = sha256_of(
            {k: v for k, v in doc.items() if k not in ("artifact_sha256", "decision_hash")}
        )
    doc["decision_hash"] = "sha256:" + sha256_of(
        {k: v for k, v in doc.items() if k != "decision_hash"}
    )
    return doc


def boundary_rejects(doc: dict) -> bool:
    """True when the ingest path rejects the artifact (envelope then body).

    The boundary is two validators: ``validate_envelope`` owns the envelope
    (ticker, closed enums, v1.1 provenance/expiry) and ``parse_research_decision``
    owns the body (effective_date, data_quality, action resolution).
    """
    try:
        validate_envelope(doc, now=NOW)
    except EnvelopeError:
        return True
    try:
        parse_research_decision(doc)
    except ContractError:
        return True
    return False


# --- the files themselves --------------------------------------------------
@pytest.mark.parametrize("name", sorted(SCHEMA_FILES))
def test_every_schema_is_json_and_declares_draft_2020_12(name):
    schema = load(name)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith(SCHEMA_FILES[name])
    assert isinstance(schema["properties"], dict) and schema["properties"]


# --- enums equal the code's constants --------------------------------------
def test_research_decision_closed_enums_match_the_code():
    schema = load("research_decision")
    assert sorted(enum_of(schema, "direction")) == sorted(VALID_DIRECTIONS)
    assert sorted(enum_of(schema, "data_quality")) == sorted(VALID_DATA_QUALITY)


def test_signal_v2_closed_enums_match_the_code():
    schema = load("signal")
    assert sorted(enum_of(schema, "sleeve")) == sorted(SLEEVES)
    assert sorted(enum_of(schema, "trade_permission")) == sorted(TRADE_PERMISSIONS)
    assert sorted(enum_of(schema, "binding_gate")) == sorted(GATE_PRECEDENCE)
    assert sorted(enum_of(schema, "stop_kind")) == ["native", "synthetic_stop_limit"]


def test_order_intent_sleeve_enum_matches_the_code():
    assert sorted(enum_of(load("order_intent"), "sleeve")) == sorted(SLEEVES)


# --- required keys are enforced at the boundary ----------------------------
@pytest.mark.parametrize("key", load("research_decision")["required"])
def test_each_required_research_decision_key_is_enforced(key):
    doc = v11()
    del doc[key]
    rehash(doc)  # isolate requiredness from the hash check
    assert boundary_rejects(doc), f"{key!r} is required by the schema but not enforced"


@pytest.mark.parametrize(
    "key", load("research_decision")["x-required-when"]["schema_version>=1.1.0"]
)
def test_v11_required_keys_are_enforced_by_the_envelope(key):
    doc = v11()
    del doc[key]
    rehash(doc)
    with pytest.raises(EnvelopeError):
        validate_envelope(doc, now=NOW)


@pytest.mark.parametrize(
    "key", load("research_decision")["x-required-when"]["schema_version>=1.1.0"]
)
def test_v11_required_keys_are_version_gated(key):
    """A legacy 1.0 artifact keeps working without the strict 1.1 fields."""
    doc = v11(schema_version="1.0.0")
    del doc[key]
    rehash(doc)
    validate_envelope(doc, now=NOW)  # no raise: the rule is >=1.1.0 only


def test_the_legacy_phase_a_sample_still_validates():
    """Back-compat claim in the docs: a Phase-A artifact (no schema_version) ingests."""
    doc = build_sample(effective_date=NOW.date())
    assert "schema_version" not in doc
    validate_envelope(doc, now=NOW)
    parse_research_decision(doc)


# --- the v2 additions are exactly SignalContract's new fields ---------------
def test_signal_v2_additions_match_the_signalcontract_v2_fields():
    schema = load("signal")
    declared_v2 = set(schema["properties"]) - set(schema["required"])
    code_v2 = set(SignalContract.__dataclass_fields__) - PHASE_A_SIGNAL_FIELDS
    assert declared_v2 == code_v2
    assert declared_v2  # the comparison is meaningful only if there are additions


# --- never a market order ---------------------------------------------------
def test_order_intent_never_uses_a_market_order():
    """plan 4.4: entries are limit/marketable-limit, never market."""
    schema = load("order_intent")
    assert "market" not in enum_of(schema, "order_type")
    assert schema["additionalProperties"] is False


# --- independence: no sibling-repo coupling in signald/ ---------------------
FORBIDDEN_ROOTS = ("tradingagents", "trading_web")
FORBIDDEN_ENV_PREFIX = "TRADINGAGENTS_"


def _sibling_couplings(path: Path) -> list[str]:
    """Names in ``path`` that would couple execution to a sibling repo.

    Comments/docstrings are ignored by construction (the AST drops comments,
    and a docstring is never an identifier or an env-prefix literal), so the
    deliberate notes in ``config.py``/``watch.py`` and the ``producer.service``
    label in ``samples.py`` do not trip the scan.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] in FORBIDDEN_ROOTS]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in FORBIDDEN_ROOTS:
                found.append(node.module)
        elif isinstance(node, ast.Name) and node.id.split(".")[0] in FORBIDDEN_ROOTS:
            found.append(node.id)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith(FORBIDDEN_ENV_PREFIX)
        ):
            found.append(node.value)
    return found


def test_signald_carries_no_sibling_repo_coupling():
    offenders = {
        str(path.relative_to(REPO_ROOT)): _sibling_couplings(path)
        for path in sorted((REPO_ROOT / "signald").rglob("*.py"))
        if _sibling_couplings(path)
    }
    assert not offenders, offenders
