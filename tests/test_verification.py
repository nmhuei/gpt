import json
import stat

from gpt.verification import (
    DEFAULT_MANUAL_REQUIREMENTS,
    ManualVerificationRecord,
    ManualVerificationStatus,
    append_manual_verification,
    load_manual_verifications,
    manual_verification_summary,
)


def test_manual_verification_record_round_trip_and_permissions(tmp_path):
    target = tmp_path / "evidence" / "manual.jsonl"
    record = ManualVerificationRecord(
        feature_id="MV-UNIT",
        status=ManualVerificationStatus.PASS,
        expected="one direct behavior check",
        observed="behavior matched",
        verifier="test-operator",
        environment="offline",
        evidence=["trace://unit"],
        metadata={"automated_gate": "pass"},
    )

    append_manual_verification(target, record)

    loaded = load_manual_verifications(target)
    assert len(loaded) == 1
    assert loaded[0]["feature_id"] == "MV-UNIT"
    assert loaded[0]["status"] == "MANUAL_PASS"
    assert loaded[0]["evidence"] == ["trace://unit"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    json.dumps(loaded[0])


def test_manual_verification_loader_tolerates_missing_file(tmp_path):
    assert load_manual_verifications(tmp_path / "missing.jsonl") == []


def test_manual_verification_summary_requires_all_default_records():
    summary = manual_verification_summary([])
    assert summary["ok"] is False
    assert summary["required"] == len(DEFAULT_MANUAL_REQUIREMENTS)
    assert summary["passed"] == 0
    assert {item["status"] for item in summary["items"]} == {"MISSING"}


def test_manual_verification_summary_uses_latest_record():
    feature = DEFAULT_MANUAL_REQUIREMENTS[0].feature_id
    records = [
        {
            "feature_id": feature,
            "status": "MANUAL_FAIL",
            "expected": "works",
            "observed": "broken",
        },
        {
            "feature_id": feature,
            "status": "MANUAL_PASS",
            "expected": "works",
            "observed": "works now",
        },
    ]
    summary = manual_verification_summary(records, DEFAULT_MANUAL_REQUIREMENTS[:1])
    assert summary["ok"] is True
    assert summary["passed"] == 1
    assert summary["items"][0]["latest"]["observed"] == "works now"
