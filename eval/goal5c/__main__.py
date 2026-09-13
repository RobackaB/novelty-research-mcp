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
