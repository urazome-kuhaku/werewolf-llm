"""Integration tests for the workbench publication gate."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from test_knowledge_compiler import _write_compilable_package

from werewolf.knowledge.frontmatter import parse_markdown
from werewolf.knowledge.publisher import (
    PublishedPackageConflictError,
    PublishGateError,
    ReviewRecord,
    RulesetPublisher,
)

BOARD = "test-board@1.0.0"


def _candidate_manifest_digest(job: Path) -> str:
    manifest = json.loads((job / "publish-manifest.json").read_text(encoding="utf-8"))
    documents = sorted(
        ({"path": item["path"], "sha256": item["sha256"]} for item in manifest["files"]),
        key=lambda item: item["path"],
    )
    logical = {
        "board_ref": manifest["package_id"],
        "documents": documents,
        "package_id": manifest["package_id"],
        "schema_version": 1,
    }
    canonical = json.dumps(
        logical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _review(job: Path, *, reviewer: str = "test-reviewer") -> ReviewRecord:
    return ReviewRecord(
        reviewed_by=reviewer,
        reviewed_at=date(2026, 9, 27),
        decision="APPROVED",
        approved_manifest_sha256=_candidate_manifest_digest(job),
    )


def _source(source_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_id": source_id,
        "url": f"https://example.test/{source_id}",
        "title": f"虚构来源 {source_id}",
        "publisher": "虚构规则组",
        "source_class": "PLATFORM_RULES",
        "published_at": "2026-09-01T00:00:00Z",
        "fetched_at": "2026-09-27T00:00:00Z",
        "content_sha256": hashlib.sha256(source_id.encode()).hexdigest(),
        "excerpt": "这是用于发布门禁测试的虚构证据摘录。",
        "retrieval_method": "fixture",
    }


def _claim(
    claim_id: str,
    source_id: str,
    *,
    status: str = "SUPPORTED",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "claim_id": claim_id,
        "ruleset_candidate_id": "test-board",
        "key": f"fixture.{claim_id.replace('-', '_')}",
        "value": True,
        "scope": "BOARD",
        "conditions": {},
        "evidence_ids": [source_id],
        "confidence": 1.0,
        "status": status,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _prepare_job(workbench_root: Path, name: str = "job") -> Path:
    job = workbench_root / name
    draft = job / "draft"
    _write_compilable_package(draft)
    for document in draft.rglob("*.md"):
        document.write_text(
            document.read_text(encoding="utf-8").replace(
                "reviewed_by: reviewer", "reviewed_by: test-reviewer"
            ),
            encoding="utf-8",
        )

    sources = [
        _source("source-board"),
        _source("source-role"),
        _source("source-mechanic"),
        _source("source-interaction"),
    ]
    claims = [
        _claim("claim-board", "source-board"),
        _claim("claim-role", "source-role"),
        _claim("claim-mechanic", "source-mechanic"),
        _claim("claim-interaction", "source-interaction"),
    ]
    job.mkdir(parents=True, exist_ok=True)
    (job / "sources.yaml").write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (job / "claims.yaml").write_text(
        yaml.safe_dump(claims, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    _write_json(
        job / "conflicts.json",
        {"conflicts": [], "unresolved_conflicts": [], "needs_human_decision": False},
    )
    _write_json(
        job / "coverage.json",
        {
            "required_count": 1,
            "satisfied_count": 1,
            "coverage_percentage": 100.0,
            "blocking_requirement_ids": [],
            "passed": True,
            "items": [
                {
                    "requirement_id": "fixture-required",
                    "required": True,
                    "status": "SATISFIED",
                    "blocking": False,
                }
            ],
        },
    )
    documents = []
    for path in sorted(draft.rglob("*.md")):
        relative = path.relative_to(draft).as_posix()
        documents.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    _write_json(
        job / "publish-manifest.json",
        {"schema_version": 1, "package_id": BOARD, "files": documents},
    )
    return job


def _active_ability(*, action_code: int, resource_id: str | None = None) -> dict[str, object]:
    ability: dict[str, object] = {
        "ability_id": "fixture_action",
        "name": "测试动作",
        "action_code": action_code,
        "timing": "NIGHT_ACTION",
        "allowed_phases": ["NIGHT_ACTION"],
        "trigger_type": "ACTIVE",
        "target_rule": {
            "kind": "PLAYER",
            "min_targets": 1,
            "max_targets": 1,
            "allow_self": False,
            "allow_dead": False,
        },
        "input_information": [],
        "request_effect": {
            "effect_code": "fixture_request",
            "description": "测试请求。",
            "visibility": "PRIVATE",
        },
        "resolution_effect": {
            "effect_code": "fixture_resolution",
            "description": "测试结算。",
            "visibility": "PRIVATE",
        },
        "result_visibility": ["PRIVATE"],
        "failure_rules": [],
    }
    if resource_id is not None:
        ability["resource"] = {
            "resource_id": resource_id,
            "initial_amount": 1,
            "cost_per_use": 1,
        }
    return ability


def _install_role_ability(job: Path, ability: dict[str, object]) -> None:
    role_path = job / "draft" / "roles" / "wolf" / "1.0.0" / "role.md"
    parsed = parse_markdown(role_path.read_bytes())
    values = dict(parsed.frontmatter)
    values["abilities"] = [ability]
    role_path.write_text(
        "---\n"
        + yaml.safe_dump(values, allow_unicode=True, sort_keys=False)
        + "---\n"
        + parsed.body,
        encoding="utf-8",
    )
    manifest_path = job / "publish-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    files = manifest["files"]
    assert isinstance(files, list)
    for entry in files:
        assert isinstance(entry, dict)
        if entry["path"] == "roles/wolf/1.0.0/role.md":
            entry["sha256"] = hashlib.sha256(role_path.read_bytes()).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_failed_gate_does_not_touch_published_or_compiled(tmp_path: Path) -> None:
    job = _prepare_job(tmp_path / "vault" / "_workbench")
    _write_json(
        job / "conflicts.json",
        {
            "conflicts": [{"resolution": "NEEDS_DECISION"}],
            "unresolved_conflicts": [{"resolution": "NEEDS_DECISION"}],
        },
    )

    publisher = RulesetPublisher(tmp_path / "vault")
    with pytest.raises(PublishGateError, match="unresolved blockers"):
        await publisher.publish("job", _review(job))

    assert not (tmp_path / "vault" / "published").exists()
    assert not (tmp_path / "vault" / "compiled").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ability", "message"),
    (
        (_active_ability(action_code=999), "unknown action_code"),
        (_active_ability(action_code=104, resource_id="wrong_resource"), "resource_id"),
    ),
)
async def test_role_action_contract_gate_rejects_mismatch_without_output(
    tmp_path: Path,
    ability: dict[str, object],
    message: str,
) -> None:
    job = _prepare_job(tmp_path / "vault" / "_workbench")
    _install_role_ability(job, ability)

    publisher = RulesetPublisher(tmp_path / "vault")
    with pytest.raises(PublishGateError, match=message):
        await publisher.publish("job", _review(job))

    assert not (tmp_path / "vault" / "published").exists()
    assert not (tmp_path / "vault" / "compiled").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["REJECTED", "CONFLICTING"])
async def test_rejected_or_conflicting_claim_cannot_be_referenced(
    tmp_path: Path,
    status: str,
) -> None:
    job = _prepare_job(tmp_path / "vault" / "_workbench")
    claims_path = job / "claims.yaml"
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))
    assert isinstance(claims, list)
    for claim in claims:
        assert isinstance(claim, dict)
        if claim["claim_id"] == "claim-role":
            claim["status"] = status
    claims_path.write_text(
        yaml.safe_dump(claims, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    publisher = RulesetPublisher(tmp_path / "vault")
    with pytest.raises(PublishGateError, match="claims_not_supported=claim-role"):
        await publisher.publish("job", _review(job))

    assert not (tmp_path / "vault" / "published").exists()
    assert not (tmp_path / "vault" / "compiled").exists()


@pytest.mark.asyncio
async def test_unreferenced_rejected_claim_is_retained_for_audit(tmp_path: Path) -> None:
    job = _prepare_job(tmp_path / "vault" / "_workbench")
    sources_path = job / "sources.yaml"
    claims_path = job / "claims.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))
    assert isinstance(sources, list)
    assert isinstance(claims, list)
    sources.append(_source("source-unused"))
    claims.append(_claim("claim-unused", "source-unused", status="REJECTED"))
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    claims_path.write_text(
        yaml.safe_dump(claims, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    publisher = RulesetPublisher(tmp_path / "vault")
    result = await publisher.publish("job", _review(job))

    assert result.idempotent is False


@pytest.mark.asyncio
async def test_compiled_publish_failure_leaves_published_vault_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _prepare_job(tmp_path / "vault" / "_workbench")
    publisher = RulesetPublisher(tmp_path / "vault")

    async def fail_compiled_publish(_package: object) -> Path:
        raise RuntimeError("injected compiled store failure")

    monkeypatch.setattr(publisher._compiled_store, "publish", fail_compiled_publish)
    with pytest.raises(RuntimeError, match="injected compiled store failure"):
        await publisher.publish(job, _review(job))

    assert not (tmp_path / "vault" / "published").exists()
    assert not (tmp_path / "vault" / "compiled").exists()


@pytest.mark.asyncio
async def test_publication_is_idempotent_for_same_content(tmp_path: Path) -> None:
    job = _prepare_job(tmp_path / "vault" / "_workbench")
    publisher = RulesetPublisher(tmp_path / "vault")

    first = await publisher.publish(job, _review(job))
    first_bytes = {
        path.relative_to(first.published_root).as_posix(): path.read_bytes()
        for path in first.published_root.rglob("*")
        if path.is_file()
    }
    second = await publisher.publish("job", _review(job))

    assert first.idempotent is False
    assert second.idempotent is True
    assert second.package_sha256 == first.package_sha256
    assert {
        path.relative_to(second.published_root).as_posix(): path.read_bytes()
        for path in second.published_root.rglob("*")
        if path.is_file()
    } == first_bytes


@pytest.mark.asyncio
async def test_same_version_with_different_content_is_rejected(tmp_path: Path) -> None:
    workbench = tmp_path / "vault" / "_workbench"
    first_job = _prepare_job(workbench, "job-one")
    second_job = _prepare_job(workbench, "job-two")
    role = second_job / "draft" / "roles" / "wolf" / "1.0.0" / "role.md"
    role.write_text(role.read_text(encoding="utf-8") + "\n新增虚构规则。\n", encoding="utf-8")
    manifest_path = second_job / "publish-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["files"]:
        path = second_job / "draft" / item["path"]
        item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    _write_json(manifest_path, manifest)

    publisher = RulesetPublisher(tmp_path / "vault")
    await publisher.publish(first_job, _review(first_job))
    with pytest.raises(PublishedPackageConflictError, match="different content"):
        await publisher.publish("job-two", _review(second_job))

    published_role = tmp_path / "vault" / "published" / "roles" / "wolf" / "1.0.0" / "role.md"
    assert "新增虚构规则" not in published_role.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_review_record_is_required_and_cannot_be_invented(tmp_path: Path) -> None:
    _prepare_job(tmp_path / "vault" / "_workbench")
    publisher = RulesetPublisher(tmp_path / "vault")

    with pytest.raises(PublishGateError, match="human review"):
        await publisher.publish("job")

    with pytest.raises(PublishGateError, match="APPROVED"):
        await publisher.publish(
            "job",
            {"reviewed_by": "reviewer", "reviewed_at": "2026-09-27", "decision": "REJECTED"},
        )


@pytest.mark.asyncio
async def test_review_record_must_bind_candidate_manifest_digest(tmp_path: Path) -> None:
    _prepare_job(tmp_path / "vault" / "_workbench")
    publisher = RulesetPublisher(tmp_path / "vault")

    with pytest.raises(PublishGateError, match="does not approve"):
        await publisher.publish(
            "job",
            ReviewRecord(
                reviewed_by="test-reviewer",
                reviewed_at=date(2026, 9, 27),
                decision="APPROVED",
                approved_manifest_sha256="0" * 64,
            ),
        )

    assert not (tmp_path / "vault" / "published").exists()
    assert not (tmp_path / "vault" / "compiled").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewer", ["pending-human-review", "pending", "unknown"])
async def test_placeholder_reviewer_cannot_approve_candidate(
    tmp_path: Path,
    reviewer: str,
) -> None:
    job = _prepare_job(tmp_path / "vault" / "_workbench")
    publisher = RulesetPublisher(tmp_path / "vault")

    with pytest.raises(PublishGateError, match="explicit APPROVED"):
        await publisher.publish(
            "job",
            {
                "reviewed_by": reviewer,
                "reviewed_at": "2026-09-27",
                "decision": "APPROVED",
                "approved_manifest_sha256": _candidate_manifest_digest(job),
            },
        )

    assert not (tmp_path / "vault" / "published").exists()


@pytest.mark.asyncio
async def test_review_metadata_is_materialized_in_staging_only(tmp_path: Path) -> None:
    job = _prepare_job(tmp_path / "vault" / "_workbench")
    draft_files = sorted((job / "draft").rglob("*.md"))
    for path in draft_files:
        path.write_text(
            path.read_text(encoding="utf-8")
            .replace("reviewed_by: test-reviewer", "reviewed_by: pending-human-review")
            .replace("reviewed_at: 2026-09-27", "reviewed_at: 2026-09-28"),
            encoding="utf-8",
        )
    original = {path: path.read_bytes() for path in draft_files}
    manifest_path = job / "publish-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["files"]:
        item["sha256"] = hashlib.sha256((job / "draft" / item["path"]).read_bytes()).hexdigest()
    _write_json(manifest_path, manifest)

    review = ReviewRecord(
        reviewed_by="real-human-reviewer",
        reviewed_at=date(2026, 9, 29),
        decision="APPROVED",
        approved_manifest_sha256=_candidate_manifest_digest(job),
    )
    result = await RulesetPublisher(tmp_path / "vault").publish(job, review)

    assert {path: path.read_bytes() for path in draft_files} == original
    published_manifest = json.loads(
        (result.published_root / "manifests" / f"{BOARD}.json").read_text(encoding="utf-8")
    )
    assert published_manifest["candidate_manifest_sha256"] == review.approved_manifest_sha256
    assert published_manifest["review"] == {
        "approved_manifest_sha256": review.approved_manifest_sha256,
        "reviewed_at": "2026-09-29",
        "reviewed_by": "real-human-reviewer",
        "decision": "APPROVED",
    }
    final_entries = []
    for item in published_manifest["documents"]:
        path = result.published_root / item["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        parsed = parse_markdown(path.read_bytes())
        assert parsed.frontmatter["reviewed_by"] == "real-human-reviewer"
        assert parsed.frontmatter["reviewed_at"] == "2026-09-29"
        final_entries.append({"path": item["path"], "sha256": item["sha256"]})
    assert published_manifest["documents"] == final_entries
    assert (
        published_manifest["documents_sha256"]
        == hashlib.sha256(
            json.dumps(
                final_entries,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )


@pytest.mark.asyncio
async def test_same_candidate_and_review_is_idempotent_but_different_review_conflicts(
    tmp_path: Path,
) -> None:
    job = _prepare_job(tmp_path / "vault" / "_workbench")
    candidate_digest = _candidate_manifest_digest(job)
    first_review = ReviewRecord(
        reviewed_by="reviewer-one",
        reviewed_at=date(2026, 9, 29),
        decision="APPROVED",
        approved_manifest_sha256=candidate_digest,
    )
    publisher = RulesetPublisher(tmp_path / "vault")
    first = await publisher.publish(job, first_review)
    second = await publisher.publish("job", first_review)
    assert first.idempotent is False
    assert second.idempotent is True

    different_review = ReviewRecord(
        reviewed_by="reviewer-two",
        reviewed_at=first_review.reviewed_at,
        decision="APPROVED",
        approved_manifest_sha256=candidate_digest,
    )
    with pytest.raises(PublishedPackageConflictError, match="different content|different review"):
        await publisher.publish("job", different_review)
