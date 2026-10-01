"""Run one bounded, real Pi ruleset-authoring smoke test.

This script deliberately sends one public synthetic sentence only.  The
returned value is an untrusted draft and is summarized for the terminal; it
is never assigned a claim status or written to the Vault.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from werewolf.ruleset_workbench.claim_extractor import (
    ClaimExtractionError,
    ClaimExtractor,
)
from werewolf.ruleset_workbench.claims import ClaimStatus
from werewolf.ruleset_workbench.pi_synthesis import (
    EvidenceExcerpt,
    PiRuleSynthesisProvider,
    SynthesisProviderError,
    SynthesisProviderResponseError,
)

PUBLIC_SYNTHETIC_EVIDENCE = "女巫不可自救。"
RULESET_CANDIDATE_ID = "classic_12_smoke"


def _evidence() -> EvidenceExcerpt:
    digest = hashlib.sha256(PUBLIC_SYNTHETIC_EVIDENCE.encode("utf-8")).hexdigest()
    return EvidenceExcerpt(
        source_id="synthetic_public_rule",
        content_sha256=digest,
        excerpt=PUBLIC_SYNTHETIC_EVIDENCE,
    )


def _summary(result: Any) -> dict[str, Any]:
    return {
        "status": "ok",
        "provider": "github-copilot",
        "model": "gpt-6-luna",
        "mode": "json",
        "tools": "disabled",
        "batches": len(result.batches),
        "claims": [
            {
                "claim_id": claim.claim_id,
                "key": claim.key,
                "value": claim.value,
                "scope": claim.scope,
                "evidence": [item.source_id for item in claim.evidence],
            }
            for claim in result.claims
        ],
        "draft_count": len(result.drafts),
        "draft_keys": [sorted(draft) for draft in result.drafts],
    }


def _validate_result(result: Any, evidence: EvidenceExcerpt) -> Any:
    """Require the Pi draft to survive the real claim extraction boundary.

    The extractor receives the same one-item evidence batch that was sent to
    Pi.  This keeps the smoke test honest: a syntactically valid draft is not
    enough unless it produces a source-closed, unverified ``RuleClaim``.
    """

    try:
        extraction = ClaimExtractor().extract(
            ((evidence,),),
            result,
            ruleset_candidate_id=RULESET_CANDIDATE_ID,
        )
    except (ClaimExtractionError, ValueError, TypeError) as exc:
        raise SynthesisProviderResponseError(
            "Pi draft did not produce a source-closed RuleClaim"
        ) from exc

    valid_claims = []
    for claim in extraction.claims:
        if claim.ruleset_candidate_id != RULESET_CANDIDATE_ID:
            continue
        if claim.status is not ClaimStatus.UNVERIFIED:
            continue
        anchors = [anchor for anchor in extraction.anchors if anchor.claim_id == claim.claim_id]
        if any(
            anchor.source_id == evidence.source_id
            and anchor.content_sha256 == evidence.content_sha256
            and anchor.quote in evidence.excerpt
            for anchor in anchors
        ):
            valid_claims.append(claim)

    if not valid_claims:
        raise SynthesisProviderResponseError(
            "Pi draft did not produce at least one UNVERIFIED RuleClaim with "
            "the correct candidate ID and an exact evidence anchor"
        )
    return extraction


async def _run() -> int:
    evidence = _evidence()
    provider = PiRuleSynthesisProvider(
        executable="pi.cmd",
        provider="github-copilot",
        model="gpt-6-luna",
        thinking="minimal",
    )
    result = await provider.synthesize(
        "12人标准场",
        RULESET_CANDIDATE_ID,
        ((evidence,),),
    )
    extraction = _validate_result(result, evidence)
    summary = _summary(result)
    summary["claim_validation"] = {
        "status": "ok",
        "candidate_id": extraction.ruleset_candidate_id,
        "unverified_rule_claims": [
            {"claim_id": claim.claim_id, "key": claim.key}
            for claim in extraction.claims
            if claim.status is ClaimStatus.UNVERIFIED
        ],
        "exact_anchor_count": len(extraction.anchors),
    }
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except SynthesisProviderError as exc:
        print(
            json.dumps(
                {"status": "error", "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
