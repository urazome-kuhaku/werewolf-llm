"""Support code for the small, read-only top-level CLI diagnostics."""

from .diagnostics import (
    DiagnosticFailure,
    json_report,
    pi_doctor,
    resolve_pi_executable,
    validate_config_file,
    validate_rules_file,
    verify_archive_path,
)

__all__ = [
    "DiagnosticFailure",
    "json_report",
    "pi_doctor",
    "resolve_pi_executable",
    "validate_config_file",
    "validate_rules_file",
    "verify_archive_path",
]
