from werewolf.knowledge.preview import experimental_preview, experimental_preview_enabled


def test_preview_scope_is_explicit_and_restored() -> None:
    assert experimental_preview_enabled() is False
    with experimental_preview():
        assert experimental_preview_enabled() is True
    assert experimental_preview_enabled() is False
