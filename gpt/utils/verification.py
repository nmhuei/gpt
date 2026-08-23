from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class ManualVerificationStatus(str, Enum):
    PASS = "MANUAL_PASS"
    FAIL = "MANUAL_FAIL"
    BLOCKED = "BLOCKED_MANUAL_VERIFY"


@dataclass(frozen=True)
class ManualVerificationRecord:
    feature_id: str
    status: ManualVerificationStatus
    expected: str
    observed: str
    verifier: str = "operator"
    environment: str = "local"
    evidence: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass(frozen=True)
class ManualVerificationRequirement:
    feature_id: str
    description: str
    matrix: str


DEFAULT_MANUAL_REQUIREMENTS: tuple[ManualVerificationRequirement, ...] = (
    ManualVerificationRequirement(
        "MV-FREE-ANON-AUTH-GUARD",
        "An authenticated browser is rejected and cannot be used for live certification.",
        "free_anonymous",
    ),
    ManualVerificationRequirement(
        "MV-FREE-ANON-DOCTOR",
        "Free/anonymous browser doctor detects composer/auth state without sending a prompt.",
        "free_anonymous",
    ),
    ManualVerificationRequirement(
        "MV-FREE-ANON-SMOKE",
        "Free/anonymous bounded chat smoke returns expected text or typed rate-limit failure.",
        "free_anonymous",
    ),
    ManualVerificationRequirement(
        "MV-MODEL-EFFORT-READBACK",
        "Model and reasoning effort are selected/read back only when exposed to the anonymous session.",
        "free_anonymous",
    ),
    ManualVerificationRequirement(
        "MV-COMMIT-UNKNOWN-RECONCILE",
        "A post-submit uncertainty reconciles history before any resend.",
        "offline_or_live_controlled",
    ),
    ManualVerificationRequirement(
        "MV-WORKER-FACTORY",
        "Worker factory respects max workers, queue timeout, warm workers and releases leases.",
        "offline_or_live_controlled",
    ),
    ManualVerificationRequirement(
        "MV-OPENAI-CHAT",
        "OpenAI chat completion works through a normal client with no custom parser.",
        "free_anonymous",
    ),
    ManualVerificationRequirement(
        "MV-OPENAI-STREAM",
        "OpenAI SSE stream emits role/content/final finish/[DONE] exactly once.",
        "free_anonymous",
    ),
    ManualVerificationRequirement(
        "MV-TOOL-LOOP",
        "Tool call/result continuation preserves call IDs and conversation state.",
        "offline_first_then_live_bounded",
    ),
    ManualVerificationRequirement(
        "MV-ANTHROPIC-CLAUDE-CODE",
        "Claude Code consumes a local /v1/messages response end-to-end.",
        "free_anonymous_external_client",
    ),
    ManualVerificationRequirement(
        "MV-TRACE-ARTIFACTS",
        "Trace and manual-verification artifacts are created with safe permissions.",
        "local",
    ),
    ManualVerificationRequirement(
        "MV-FULL-REGRESSION",
        "After automated gates, at least one human-observed end-to-end check still passes.",
        "required_before_done",
    ),
)


def append_manual_verification(
    path: Path | str,
    record: ManualVerificationRecord,
) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    payload = asdict(record)
    payload["status"] = record.status.value
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
    os.chmod(destination, 0o600)


def load_manual_verifications(path: Path | str) -> list[dict[str, Any]]:
    source = Path(path).expanduser()
    if not source.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            records.append(payload)
    return records


def manual_verification_summary(
    records: list[dict[str, Any]],
    requirements: tuple[ManualVerificationRequirement, ...] = DEFAULT_MANUAL_REQUIREMENTS,
) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        feature_id = record.get("feature_id")
        if not isinstance(feature_id, str):
            continue
        latest[feature_id] = record

    items: list[dict[str, Any]] = []
    all_passed = True
    for requirement in requirements:
        record_payload = latest.get(requirement.feature_id)
        status = record_payload.get("status") if record_payload else "MISSING"
        passed = status == ManualVerificationStatus.PASS.value
        if not passed:
            all_passed = False
        items.append(
            {
                "feature_id": requirement.feature_id,
                "description": requirement.description,
                "matrix": requirement.matrix,
                "status": status,
                "passed": passed,
                "latest": record_payload,
            }
        )
    return {
        "ok": all_passed,
        "required": len(requirements),
        "passed": sum(1 for item in items if item["passed"]),
        "items": items,
    }


__all__ = [
    "DEFAULT_MANUAL_REQUIREMENTS",
    "ManualVerificationRecord",
    "ManualVerificationRequirement",
    "ManualVerificationStatus",
    "append_manual_verification",
    "load_manual_verifications",
    "manual_verification_summary",
]
