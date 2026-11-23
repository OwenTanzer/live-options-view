"""Pre-merge credential gate for the bot catalog.

Issue #41's failure mode is not subtle: a catalog entry becomes active, its
`CRASSUS_PW_*` variable was never created in Railway, and the runner either
crashes on startup (now, since the aggregated preflight) or -- worse, before
that check existed -- registers the account as an ordinary user. Startup
diagnostics catch it late, at deploy time. This module catches it at review
time, by comparing the catalog against its base revision.

Rules, in order of how much they matter:

1. **A newly active `password_env` entry needs recorded provisioning
   evidence.** "Newly active" is concrete: the entry is absent from the base,
   or flips `active: false` to active, or keeps an active entry while changing
   its `password_env` name. In every one of those cases the operator is asking
   Railway for a variable that did not have to exist before.
2. **`active: false` remains a valid way to land code.** An entry that is
   explicitly inactive needs no evidence and cannot abort startup; that is the
   release/activation split the catalog already documents.
3. **Evidence is redacted by construction.** The evidence file may name a
   variable and say who provisioned it and when. It may not contain the value.
   The loader rejects unknown fields and value-like strings rather than trusting
   a reviewer to notice.
4. **The catalog may never carry a `password` literal.** That rule is true
   today and this gate makes it explicit, so a future convenience commit is
   caught rather than reviewed.

The gate is a *review* control, not a replacement for the runtime preflight.
It proves a human asserted the variable exists; `preflight_accounts.py --probe`
against the deployed Worker, and the runner's own startup check, prove the
assertion is true. Both are meant to run -- this one before merge, those after
deploy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "provisioning_evidence.v1"

#: The catalog's variable-name convention. A gate that accepted any name would
#: happily bless `FOO` and leave the operator hunting for a variable that the
#: deploy configuration never had a slot for.
ENV_NAME = re.compile(r"^CRASSUS_PW_[A-Z0-9_]+$")

ALLOWED_EVIDENCE_FIELDS = frozenset({"provisioned_at", "by", "ticket", "note"})
MAX_NOTE_CHARS = 240


@dataclass
class Finding:
    """One gate finding. Names and reasons only -- never a value."""

    kind: str  # "missing_evidence" | "invalid_evidence" | "missing_value" | "secret_in_catalog"
    alias: str = ""
    username: str = ""
    env_name: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        payload = {"kind": self.kind, "detail": self.detail}
        for key in ("alias", "username", "env_name"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        return payload

    def message(self) -> str:
        subject = self.alias or self.username or self.env_name or "(catalog)"
        if self.env_name:
            return f"{subject}: ${self.env_name} -- {self.detail}"
        return f"{subject}: {self.detail}"


@dataclass
class GateResult:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    newly_active: list[dict[str, str]] = field(default_factory=list)
    base_available: bool = True
    checked_entries: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "base_available": self.base_available,
            "checked_entries": self.checked_entries,
            "newly_active": self.newly_active,
            "findings": [f.as_dict() for f in self.findings],
        }

    def summary_lines(self) -> list[str]:
        lines = [
            f"catalog entries checked: {self.checked_entries}",
            f"newly active credential entries: {len(self.newly_active)}",
        ]
        for item in self.newly_active:
            lines.append(
                f"  NEW  {item['alias']} ({item['username']}) -> ${item['env_name']}"
            )
        if not self.findings:
            lines.append("provisioning gate: pass")
        for finding in self.findings:
            lines.append(f"  FAIL {finding.message()}")
        return lines


def load_catalog(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text())
    entries = raw.get("accounts")
    if not isinstance(entries, list):
        raise ValueError(f"{path}: top-level 'accounts' must be a list")
    return entries


def _active(entry: dict[str, Any]) -> bool:
    return entry.get("active", True) is not False


def _identity(entry: dict[str, Any]) -> str:
    username = entry.get("username")
    return username if isinstance(username, str) else ""


def _by_username(entries: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        username = _identity(entry)
        if username:
            result[username] = entry
    return result


def newly_active_entries(
    base_entries: Iterable[dict[str, Any]] | None,
    head_entries: Iterable[dict[str, Any]],
) -> list[dict[str, str]]:
    """Active `password_env` entries that ask Railway for a variable for the
    first time, relative to `base_entries`.

    `base_entries=None` means "no base revision available": every active
    credential entry counts as new, which is the conservative reading and only
    happens for a first commit or an unresolvable base.
    """
    head = list(head_entries)
    base = _by_username(base_entries or [])
    result: list[dict[str, str]] = []
    for entry in head:
        if not _active(entry):
            continue
        env_name = entry.get("password_env")
        if not env_name or not isinstance(env_name, str):
            continue
        username = _identity(entry)
        prior = base.get(username) if base_entries is not None else None
        if base_entries is not None:
            if prior is not None and _active(prior) and prior.get("password_env") == env_name:
                continue  # already active on this exact variable
        result.append({
            "alias": str(entry.get("alias", username)),
            "username": username,
            "env_name": env_name,
        })
    return result


def load_evidence(path: Path) -> dict[str, dict[str, Any]]:
    """Load the redacted evidence file, rejecting anything value-shaped.

    A file that fails here is a gate failure, not a warning: silently ignoring
    malformed evidence would let a typo pass for provisioned.
    """
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError("evidence file must be a JSON object")
    schema = raw.get("schema")
    if schema != SCHEMA:
        raise ValueError(f"evidence schema must be {SCHEMA!r}, got {schema!r}")
    entries = raw.get("entries", {})
    if not isinstance(entries, dict):
        raise ValueError("evidence 'entries' must be an object keyed by variable name")
    for env_name, record in entries.items():
        if not ENV_NAME.match(env_name):
            raise ValueError(f"evidence key {env_name!r} is not a CRASSUS_PW_* variable name")
        if not isinstance(record, dict):
            raise ValueError(f"evidence for {env_name} must be an object")
        unknown = set(record) - ALLOWED_EVIDENCE_FIELDS
        if unknown:
            raise ValueError(
                f"evidence for {env_name} carries unsupported field(s) {sorted(unknown)}; "
                f"allowed: {sorted(ALLOWED_EVIDENCE_FIELDS)} (never a secret value)"
            )
        for field_name, value in record.items():
            if not isinstance(value, str):
                raise ValueError(f"evidence field {env_name}.{field_name} must be a string")
            if len(value) > MAX_NOTE_CHARS:
                raise ValueError(
                    f"evidence field {env_name}.{field_name} is longer than "
                    f"{MAX_NOTE_CHARS} chars; record a name and a reference, not a value"
                )
        if not record.get("by"):
            raise ValueError(f"evidence for {env_name} needs a 'by' (who provisioned it)")
        try:
            date.fromisoformat(record.get("provisioned_at", ""))
        except ValueError as exc:
            raise ValueError(
                f"evidence for {env_name} needs provisioned_at as YYYY-MM-DD"
            ) from exc
    return entries


def check_catalog_hygiene(head_entries: Iterable[dict[str, Any]]) -> list[Finding]:
    """Catalog invariants that must hold regardless of the base revision."""
    findings: list[Finding] = []
    for entry in head_entries:
        username = _identity(entry)
        alias = str(entry.get("alias", username))
        if entry.get("password"):
            findings.append(Finding(
                kind="secret_in_catalog",
                alias=alias,
                username=username,
                detail="catalog entry carries a literal password; use password_env",
            ))
        if not _active(entry):
            continue
        env_name = entry.get("password_env")
        if env_name is not None and not ENV_NAME.match(str(env_name)):
            findings.append(Finding(
                kind="invalid_evidence",
                alias=alias,
                username=username,
                env_name=str(env_name),
                detail=f"password_env must match {ENV_NAME.pattern}",
            ))
    return findings


def evaluate(
    head_entries: list[dict[str, Any]],
    *,
    base_entries: list[dict[str, Any]] | None,
    evidence: dict[str, dict[str, Any]] | None,
    environ: dict[str, str] | None = None,
    require_environment: bool = False,
) -> GateResult:
    """Run the gate.

    `environ` + `require_environment` add the deploy-time half: every newly
    active variable must also have a value in the process environment, which is
    what the release checklist asks the operator to verify with the actual
    Railway variables exported.
    """
    head = list(head_entries)
    findings = check_catalog_hygiene(head)
    new_entries = newly_active_entries(base_entries, head)
    known = evidence or {}
    for item in new_entries:
        record = known.get(item["env_name"])
        if record is None:
            findings.append(Finding(
                kind="missing_evidence",
                alias=item["alias"],
                username=item["username"],
                env_name=item["env_name"],
                detail=(
                    "newly active credential entry has no recorded provisioning "
                    "evidence; provision the Railway variable and add a redacted "
                    "entry to the evidence file, or land the account as active:false"
                ),
            ))
            continue
        if require_environment and environ is not None:
            if not environ.get(item["env_name"]):
                findings.append(Finding(
                    kind="missing_value",
                    alias=item["alias"],
                    username=item["username"],
                    env_name=item["env_name"],
                    detail="evidence exists but the variable has no value in this environment",
                ))
    return GateResult(
        ok=not findings,
        findings=findings,
        newly_active=new_entries,
        base_available=base_entries is not None,
        checked_entries=len(head),
    )
