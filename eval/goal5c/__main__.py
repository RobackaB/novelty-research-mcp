"""Synthetic-only offline CLI; no acquisition, labelling or metric execution."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .contracts import (
    ContractError, canonical_json_bytes, validate_measurement_contract, validate_truth_catalog,
    validate_measurement_inputs,
)
from .snapshot import import_snapshot, load_json, validate_manifest
from .preparation import prepare_snapshot
from .contracts import sha256_json
from .freeze import freeze_registry


def write_bundle(path: Path, value: dict) -> None:
    """Write a new artifact outside Git worktrees, never overwrite an input."""
    path = path.resolve()
    if not path.parent.is_dir() or any((parent / ".git").exists() for parent in path.parents):
        raise ContractError("output must be in an existing directory outside Git worktrees")
    content = canonical_json_bytes(value)
    try:
        with path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise ContractError("cannot create output; existing artifacts are never overwritten") from exc


def write_preparation(directory: Path, worksheet: dict, analyst: dict) -> None:
    """Create a new external artifact directory, sealed only after both writes."""
    directory = directory.resolve()
    if not directory.parent.is_dir() or any((parent / ".git").exists() for parent in directory.parents):
        raise ContractError("output must be outside Git worktrees")
    try:
        directory.mkdir()
    except OSError as exc:
        raise ContractError("preparation output directory must be new") from exc
    write_bundle(directory / "analyst-only.json", analyst)
    write_bundle(directory / "worksheet.json", worksheet)
    write_bundle(directory / "COMPLETE.json", {
        "worksheet_sha256": sha256_json(worksheet), "analyst_sha256": sha256_json(analyst),
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a synthetic versioned contract")
    validate.add_argument("kind", choices=("manifest", "truth", "measurement"))
    validate.add_argument("input", type=Path)
    capture = commands.add_parser("import-snapshot", help="import a finalized synthetic capture snapshot")
    capture.add_argument("--manifest", required=True, type=Path)
    capture.add_argument("--snapshot", required=True, type=Path)
    capture.add_argument("--output", required=True, type=Path)
    inputs = commands.add_parser("validate-inputs", help="bind a synthetic measurement to an imported bundle and truth")
    inputs.add_argument("--measurement", required=True, type=Path)
    inputs.add_argument("--bundle", required=True, type=Path)
    inputs.add_argument("--truth", required=True, type=Path)
    prepare = commands.add_parser("prepare-synthetic", help="prepare reviewed synthetic candidates and blank worksheets")
    prepare.add_argument("--manifest", required=True, type=Path)
    prepare.add_argument("--snapshot", required=True, type=Path)
    prepare.add_argument("--plan", required=True, type=Path)
    prepare.add_argument("--output-dir", required=True, type=Path)
    freeze = commands.add_parser("freeze-synthetic", help="rehearse a preregistered original-query roster")
    freeze.add_argument("--registry", required=True, type=Path)
    freeze.add_argument("--plan", required=True, type=Path)
    freeze.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            validators = {"manifest": validate_manifest, "truth": validate_truth_catalog,
                          "measurement": validate_measurement_contract}
            validators[args.kind](load_json(args.input))
            print("Synthetic contract valid; no real-study authorization is implied.")
        elif args.command == "validate-inputs":
            validate_measurement_inputs(load_json(args.measurement), load_json(args.bundle), load_json(args.truth))
            print("Synthetic input references valid; no metrics or workflow selection verified.")
        elif args.command == "prepare-synthetic":
            worksheet, analyst = prepare_snapshot(args.snapshot, load_json(args.manifest), load_json(args.plan))
            write_preparation(args.output_dir, worksheet, analyst)
            print(f"Synthetic preparation created: {len(worksheet['items'])} blank items. "
                  "Only worksheet.json is for annotators; no labels or metrics computed.")
        elif args.command == "freeze-synthetic":
            frozen = freeze_registry(load_json(args.registry), load_json(args.plan))
            write_bundle(args.output, frozen)
            print(f"Synthetic roster created: {len(frozen['pilot_queries'])} pilot originals, "
                  f"{len(frozen['discovery_queries'])} discovery originals, "
                  f"{len(frozen['ordered_reserves'])} reserves. Real execution remains disabled.")
        else:
            bundle = import_snapshot(args.snapshot, load_json(args.manifest))
            write_bundle(args.output, bundle)
            print(f"Synthetic bundle created: {len(bundle['raw_events'])} events, "
                  f"{len(bundle['examples'])} unreconciled examples. No metrics computed.")
    except (ContractError, TypeError, ValueError, KeyError, UnicodeError):
        # Do not echo source text, SQL values, private paths, or raw parser errors.
        print("Goal 5C input rejected; verify schema, provenance, scope and output location.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
