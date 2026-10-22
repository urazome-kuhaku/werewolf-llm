"""Closed, bounded data model for declarative victory programs."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Literal, NoReturn, Protocol, TypeAlias, TypeVar, get_origin, overload

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
VictoryCount = Annotated[int, Field(ge=0, le=64, strict=True)]
VictoryPriority = Annotated[int, Field(ge=-1_000_000, le=1_000_000, strict=True)]
_T = TypeVar("_T")
_KT = TypeVar("_KT")
_VT_co = TypeVar("_VT_co", covariant=True)


class _SupportsKeysAndGetItem(Protocol[_KT, _VT_co]):
    def keys(self) -> Iterable[_KT]: ...

    def __getitem__(self, key: _KT, /) -> _VT_co: ...


class _FrozenStringMap(dict[str, str]):
    """A JSON-serializable role map that rejects in-place mutation."""

    @staticmethod
    def _immutable() -> NoReturn:
        raise TypeError("victory program mappings are immutable")

    def __setitem__(self, key: str, value: str) -> None:
        del key, value
        self._immutable()

    def __delitem__(self, key: str) -> None:
        del key
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    @overload
    def pop(self, key: str, /) -> str: ...

    @overload
    def pop(self, key: str, default: str, /) -> str: ...

    @overload
    def pop(self, key: str, default: _T, /) -> str | _T: ...

    def pop(self, key: str, default: object = None, /) -> object:
        del key, default
        self._immutable()

    def popitem(self) -> NoReturn:
        self._immutable()

    def setdefault(self, key: str, default: str = "") -> NoReturn:
        del key, default
        self._immutable()

    def update(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self._immutable()

    @overload  # type: ignore[override,misc]
    def __ior__(self, value: _SupportsKeysAndGetItem[str, str], /) -> _FrozenStringMap: ...

    @overload
    def __ior__(self, value: Iterable[tuple[str, str]], /) -> _FrozenStringMap: ...

    def __ior__(self, value: object, /) -> _FrozenStringMap:  # type: ignore[misc]
        del value
        self._immutable()


class VictoryRuleModel(BaseModel):
    """Common immutable and closed configuration for victory programs."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    @model_validator(mode="before")
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for name, field in cls.model_fields.items():
            if get_origin(field.annotation) is tuple and isinstance(normalized.get(name), list):
                normalized[name] = tuple(normalized[name])
        return normalized


class VictoryLiteral(VictoryRuleModel):
    """A boolean or bounded integer literal used by a victory expression."""

    op: Literal["literal"] = "literal"
    value: StrictBool | VictoryCount


class VictoryAliveCount(VictoryRuleModel):
    """Count alive players by one current, explicitly named identity field."""

    op: Literal["count_alive"] = "count_alive"
    field: Literal["faction_id", "victory_group_id"]
    value: Identifier


class VictoryGameState(VictoryRuleModel):
    """Read a strictly boolean, declared GAME-scope state key."""

    op: Literal["game_state"] = "game_state"
    key: Identifier


class VictoryCompare(VictoryRuleModel):
    """Compare two bounded values using the finite comparison vocabulary."""

    op: Literal["eq", "ne", "lt", "le", "gt", "ge"]
    left: VictoryExpr
    right: VictoryExpr


class VictoryBoolean(VictoryRuleModel):
    """Combine boolean expressions without dynamic code or unbounded loops."""

    op: Literal["and", "or", "not"]
    values: tuple[VictoryExpr, ...]

    @model_validator(mode="after")
    def validate_arity(self) -> VictoryBoolean:
        if self.op == "not" and len(self.values) != 1:
            raise ValueError("victory not requires exactly one expression")
        if self.op in {"and", "or"} and not 2 <= len(self.values) <= 16:
            raise ValueError("victory and/or requires between 2 and 16 expressions")
        return self


VictoryExpr: TypeAlias = (
    VictoryLiteral | VictoryAliveCount | VictoryGameState | VictoryCompare | VictoryBoolean
)
for _victory_expr_model in (VictoryCompare, VictoryBoolean):
    _victory_expr_model.model_rebuild(_types_namespace={"VictoryExpr": VictoryExpr})


class VictoryCondition(VictoryRuleModel):
    """One explicitly prioritized, board-local win condition."""

    condition_id: Identifier
    side_id: Identifier
    priority: VictoryPriority
    when: VictoryExpr


class VictoryProgram(VictoryRuleModel):
    """The complete data-only victory semantics frozen for one board."""

    player_groups: dict[Identifier, Identifier]
    conditions: tuple[VictoryCondition, ...] = Field(min_length=1, max_length=64)
    conflict_policy: Literal["DRAW", "PENDING_MODERATOR"]

    @model_validator(mode="after")
    def validate_program(self) -> VictoryProgram:
        if not self.player_groups:
            raise ValueError("victory player_groups must not be empty")
        condition_ids = [condition.condition_id for condition in self.conditions]
        if len(condition_ids) != len(set(condition_ids)):
            raise ValueError("victory condition IDs must be unique")
        expression_count = 0

        def visit(expression: VictoryExpr, depth: int) -> None:
            nonlocal expression_count
            expression_count += 1
            if expression_count > 128:
                raise ValueError("victory program exceeds the 128-node expression bound")
            if depth > 8:
                raise ValueError("victory expression depth exceeds 8")
            if isinstance(expression, VictoryCompare):
                visit(expression.left, depth + 1)
                visit(expression.right, depth + 1)
            elif isinstance(expression, VictoryBoolean):
                for child in expression.values:
                    visit(child, depth + 1)

        for condition in self.conditions:
            visit(condition.when, 1)
        return self

    def model_post_init(self, __context: object) -> None:
        object.__setattr__(
            self,
            "player_groups",
            _FrozenStringMap(sorted(self.player_groups.items())),
        )
        object.__setattr__(
            self,
            "conditions",
            tuple(
                sorted(
                    self.conditions,
                    key=lambda item: (-item.priority, item.condition_id),
                )
            ),
        )


__all__ = [
    "VictoryAliveCount",
    "VictoryBoolean",
    "VictoryCompare",
    "VictoryCondition",
    "VictoryExpr",
    "VictoryGameState",
    "VictoryLiteral",
    "VictoryProgram",
]
