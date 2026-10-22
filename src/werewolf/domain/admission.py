"""Strict durable identity for a game admitted by an experimental catalog."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.ASCII)
_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)


class CatalogAdmission(BaseModel):
    """Exactly eight fields identifying one catalog lease at game start."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    catalog_id: Annotated[str, StringConstraints(min_length=1, max_length=64, strict=True)]
    board_id: Annotated[str, StringConstraints(min_length=1, max_length=64, strict=True)]
    board_version: Annotated[str, StringConstraints(min_length=5, max_length=64, strict=True)]
    package_identity: Annotated[str, StringConstraints(min_length=64, max_length=64, strict=True)]
    gate_sha256: Annotated[str, StringConstraints(min_length=64, max_length=64, strict=True)]
    lease_generation: Annotated[int, Field(ge=1, le=2**31 - 1, strict=True)]
    lease_id: Annotated[str, StringConstraints(min_length=64, max_length=64, strict=True)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> CatalogAdmission:
        for name, value in (("catalog_id", self.catalog_id), ("board_id", self.board_id)):
            if _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"{name} must be a lowercase ASCII identifier")
        if _VERSION.fullmatch(self.board_version) is None:
            raise ValueError("board_version must be X.Y.Z")
        if _SHA256.fullmatch(self.package_identity) is None:
            raise ValueError("package_identity must be a lowercase SHA-256 digest")
        if _SHA256.fullmatch(self.gate_sha256) is None:
            raise ValueError("gate_sha256 must be a lowercase SHA-256 digest")
        if _SHA256.fullmatch(self.lease_id) is None:
            raise ValueError("lease_id must be a lowercase SHA-256 digest")
        return self

    def validate_for_game(self, game_id: str) -> None:
        """Verify the game-bound digest that cannot fit in the eight fields."""

        if not isinstance(game_id, str) or _IDENTIFIER.fullmatch(game_id) is None:
            raise ValueError("game_id must be a lowercase ASCII identifier")
        expected = self.expected_lease_id(
            catalog_id=self.catalog_id,
            game_id=game_id,
            board_ref=f"{self.board_id}@{self.board_version}",
            package_identity=self.package_identity,
            gate_sha256=self.gate_sha256,
            lease_generation=self.lease_generation,
        )
        if self.lease_id != expected:
            raise ValueError("lease_id does not match the admission identity")

    @staticmethod
    def expected_lease_id(
        *,
        catalog_id: str,
        game_id: str,
        board_ref: str,
        package_identity: str,
        gate_sha256: str,
        lease_generation: int,
    ) -> str:
        value = "\0".join(
            (
                catalog_id,
                game_id,
                board_ref,
                package_identity,
                gate_sha256,
                str(lease_generation),
            )
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def build(
        cls,
        *,
        catalog_id: str,
        game_id: str,
        board_id: str,
        board_version: str,
        package_identity: str,
        gate_sha256: str,
        lease_generation: int,
    ) -> CatalogAdmission:
        if not isinstance(game_id, str) or _IDENTIFIER.fullmatch(game_id) is None:
            raise ValueError("game_id must be a lowercase ASCII identifier")
        lease_id = cls.expected_lease_id(
            catalog_id=catalog_id,
            game_id=game_id,
            board_ref=f"{board_id}@{board_version}",
            package_identity=package_identity,
            gate_sha256=gate_sha256,
            lease_generation=lease_generation,
        )
        return cls(
            schema_version=1,
            catalog_id=catalog_id,
            board_id=board_id,
            board_version=board_version,
            package_identity=package_identity,
            gate_sha256=gate_sha256,
            lease_generation=lease_generation,
            lease_id=lease_id,
        )


__all__ = ["CatalogAdmission"]
