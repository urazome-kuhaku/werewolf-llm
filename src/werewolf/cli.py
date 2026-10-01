"""Command-line entry point for Werewolf Arena."""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated

import typer

from werewolf.cli_support.diagnostics import (
    DiagnosticFailure,
    json_report,
    pi_doctor,
    validate_config_file,
    validate_rules_file,
    verify_archive_path,
)
from werewolf.cli_support.play_setup import PlaySetupError, build_play_setup
from werewolf.knowledge.preview import experimental_preview

app = typer.Typer(
    name="werewolf",
    help="Werewolf Arena V1 multi-model game framework.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Configuration diagnostics.", no_args_is_help=True)
rules_app = typer.Typer(help="Published rules diagnostics.", no_args_is_help=True)
pi_app = typer.Typer(help="Pi runtime diagnostics.", no_args_is_help=True)
archive_app = typer.Typer(help="Game archive diagnostics.", no_args_is_help=True)
play_app = typer.Typer(help="Experimental playable game setup.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(rules_app, name="rules")
app.add_typer(pi_app, name="pi")
app.add_typer(archive_app, name="archive")
app.add_typer(play_app, name="play")


def _report_or_exit(
    operation: str,
    callback: Callable[[Path], dict[str, object]],
    path: Path,
) -> None:
    """Run a diagnostic callback and keep failure output concise and safe."""

    try:
        report = callback(path)
    except (DiagnosticFailure, OSError, ValueError) as exc:
        typer.echo(f"{operation} failed: {str(exc)[:500]}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json_report(report))


@app.command()
def version() -> None:
    """Print the installed package version."""

    from werewolf import __version__

    typer.echo(__version__)


@config_app.command("validate")
def config_validate(
    config_file: Annotated[Path, typer.Argument(help="Configuration YAML file.")],
) -> None:
    """Validate the V1 configuration envelope without printing credentials."""

    _report_or_exit("configuration validation", validate_config_file, config_file)


@rules_app.command("validate")
def rules_validate(
    board_file: Annotated[Path, typer.Argument(help="Board Markdown document.")],
) -> None:
    """Validate one board and its published dependency closure when present."""

    _report_or_exit("rules validation", validate_rules_file, board_file)


@pi_app.command("doctor")
def pi_doctor_command(
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="Optional game configuration YAML file."),
    ] = None,
) -> None:
    """Run safe local Pi checks and emit a machine-readable capabilities report."""

    try:
        report = pi_doctor(config_file)
    except (DiagnosticFailure, OSError, ValueError) as exc:
        typer.echo(f"pi doctor failed: {str(exc)[:500]}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json_report(report))
    if report.get("status") != "valid":
        raise typer.Exit(code=1)


@archive_app.command("verify")
def archive_verify(
    archive_path: Annotated[
        Path, typer.Argument(help="Immutable archive or compatible snapshot directory.")
    ],
) -> None:
    """Verify an archive or compatible snapshot directory and its hashes."""

    _report_or_exit("archive verification", verify_archive_path, archive_path)


@play_app.command("init")
def play_init(
    output: Annotated[
        Path, typer.Option("--output", help="New directory for the generated playable setup.")
    ],
    pi_seats: Annotated[
        str,
        typer.Option(
            "--pi-seats",
            help=(
                "Comma-separated Pi seats (1-12), for example 1,2,3. Use --all-scripted for no Pi."
            ),
        ),
    ] = "",
    provider: Annotated[str, typer.Option(help="Pi provider identifier.")] = "github-copilot",
    model: Annotated[str, typer.Option(help="Pi model identifier.")] = "gpt-6-luna",
    reasoning: Annotated[str, typer.Option(help="Pi reasoning level.")] = "medium",
    game_id: Annotated[
        str, typer.Option("--game-id", help="Lowercase game identifier.")
    ] = "classic-play-001",
    seed: Annotated[int, typer.Option(help="Deterministic game seed.")] = 20260930,
    all_scripted: Annotated[
        bool,
        typer.Option(
            "--all-scripted",
            help="Generate a script-only setup; useful for offline runner checks.",
        ),
    ] = False,
) -> None:
    """Compile the classic candidate into an isolated experimental setup."""

    try:
        values = tuple(int(item.strip()) for item in pi_seats.split(",") if item.strip())
    except ValueError:
        typer.echo("play init failed: --pi-seats must be a comma-separated integer list", err=True)
        raise typer.Exit(code=2) from None
    try:
        report = build_play_setup(
            output,
            pi_seats=values,
            provider=provider,
            model=model,
            reasoning=reasoning,  # type: ignore[arg-type]
            game_id=game_id,
            seed=seed,
            all_scripted=all_scripted,
        )
    except (PlaySetupError, OSError, ValueError) as exc:
        typer.echo(f"play init failed: {str(exc)[:500]}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json_report(report))


@play_app.command("run")
def play_run(
    config_file: Annotated[
        Path, typer.Option("--config", help="Playable game.yaml generated by play init.")
    ],
    experimental_preview_enabled: Annotated[
        bool,
        typer.Option(
            "--experimental-preview",
            help="Allow a setup whose candidate rules are pending human review.",
        ),
    ] = False,
    max_rounds: Annotated[
        int, typer.Option("--max-rounds", min=1, max=10_000, help="Stop after this many rounds.")
    ] = 20,
    sheriff_candidates: Annotated[
        str,
        typer.Option(
            "--sheriff-candidates",
            help="Optional comma-separated first-day sheriff candidate seats.",
        ),
    ] = "",
) -> None:
    """Run a complete playable game through the moderator command boundary."""

    try:
        candidates = (
            tuple(int(item.strip()) for item in sheriff_candidates.split(",") if item.strip())
            if sheriff_candidates.strip()
            else None
        )
    except ValueError:
        typer.echo(
            "play run failed: --sheriff-candidates must be comma-separated integers",
            err=True,
        )
        raise typer.Exit(code=2) from None

    from werewolf.moderator.play_runner import PlayRunnerError, run_game

    def emit(event: Mapping[str, object]) -> None:
        typer.echo(json_report(event))

    try:
        import asyncio

        summary = asyncio.run(
            run_game(
                config_file,
                max_rounds=max_rounds,
                experimental_preview=experimental_preview_enabled,
                output=emit,
                sheriff_candidates=candidates,
            )
        )
    except (PlayRunnerError, OSError, ValueError) as exc:
        typer.echo(f"play run failed: {str(exc)[:500]}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json_report(summary))


@app.command()
def moderator(
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="Game configuration YAML file."),
    ] = None,
    experimental_preview_enabled: Annotated[
        bool,
        typer.Option(
            "--experimental-preview",
            help="Allow an explicitly prepared pending-review candidate setup.",
        ),
    ] = False,
) -> None:
    """Run one long-lived moderator command loop."""

    if config_file is None:
        typer.echo("moderator requires --config <game.yaml>", err=True)
        raise typer.Exit(code=2)
    from werewolf.moderator import ModeratorShell

    shell = ModeratorShell(config_file)
    if experimental_preview_enabled:
        with experimental_preview():
            shell.run()
    else:
        shell.run()


if __name__ == "__main__":
    app()
