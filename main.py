#!/usr/bin/env python3
"""Command-line entry point for Langdock action tasks."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from bundle_actions import (
    bundle_integration_options,
    export_bundle,
    import_bundle,
)
from config import load_integrations, load_secrets
from import_actions import read_json
from langdock_api import LangdockClient
from sync_actions import (
    UserAborted,
    download_integration,
    sync_integration,
    upload_integration,
)

DEFAULT_BUNDLE_FILE = "actions.zip"


def add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--integration-id",
        help="UUID of one Langdock integration. Overrides the integration map.",
    )
    selector.add_argument(
        "--integration",
        action="append",
        metavar="NAME",
        help=(
            "Use one named integration from the map. Repeat the option "
            "to select several named integrations."
        ),
    )
    parser.add_argument(
        "--integrations-map",
        default="integrations.json",
        help="JSON name-to-ID map used when no integration ID is supplied.",
    )
    parser.add_argument(
        "--output",
        default="./integrations",
        help="Output root directory. Defaults to ./integrations.",
    )
    parser.add_argument(
        "--on-mismatch",
        choices=("ask", "fail", "skip", "local", "remote"),
        default="ask",
        help="Policy for actions that exist locally and remotely but differ.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Compatibility shortcut: use the source side automatically "
            "instead of asking about mismatches."
        ),
    )


def add_bundle_arguments(commands: argparse._SubParsersAction) -> None:
    bundle_parser = commands.add_parser(
        "bundle",
        help="Create or import one-file ZIP bundles for AI-assisted editing.",
    )
    bundle_commands = bundle_parser.add_subparsers(
        dest="bundle_command",
        required=True,
    )

    bundle_export = bundle_commands.add_parser(
        "export",
        help="Create a ZIP bundle from local integration folders.",
    )
    bundle_export.add_argument(
        "--source",
        default="./integrations",
        help="Local integration root. Defaults to ./integrations.",
    )
    bundle_export.add_argument(
        "--output",
        default=None,
        help=f"ZIP output path. Defaults to {DEFAULT_BUNDLE_FILE}.",
    )
    bundle_export.add_argument(
        "--integration",
        action="append",
        metavar="NAME",
        help=(
            "Export one named local integration. If omitted, show local "
            "integrations as numbered choices. Repeat to select several."
        ),
    )

    bundle_import = bundle_commands.add_parser(
        "import",
        help="Replace local integration folders with a ZIP bundle.",
    )
    bundle_import.add_argument(
        "--input",
        default=None,
        help=f"ZIP bundle to import. Defaults to {DEFAULT_BUNDLE_FILE}.",
    )
    bundle_import.add_argument(
        "--destination",
        default="./integrations",
        help="Local integration root. Defaults to ./integrations.",
    )
    bundle_import.add_argument(
        "--integration",
        action="append",
        metavar="NAME",
        help=(
            "Import one named integration from the bundle. If omitted, show "
            "the ZIP integration folders as numbered choices. Repeat to select several."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download, upload, synchronize, and bundle Langdock actions."
    )
    parser.add_argument(
        "--secrets",
        default="secrets.json",
        help="Path to the local secrets JSON file.",
    )

    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("download", "Download actions from Langdock into local folders."),
        ("upload", "Upload local actions to Langdock."),
        ("sync", "Synchronize local and remote actions without deleting either side."),
    ):
        command_parser = commands.add_parser(name, help=help_text)
        add_selection_arguments(command_parser)
    add_bundle_arguments(commands)

    return parser


def output_name(integration_name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", integration_name)
    value = value.strip("-").lower()
    return value or "integration"


def resolve_map_names(
    requested_names: list[str],
    integrations: dict[str, str],
) -> list[str]:
    names: list[str] = []
    casefolded = {name.casefold(): name for name in integrations}

    for requested_name in requested_names:
        resolved_name = requested_name if requested_name in integrations else None
        if resolved_name is None:
            resolved_name = casefolded.get(requested_name.casefold())
        if resolved_name is None:
            available = ", ".join(sorted(integrations))
            raise RuntimeError(
                f"Unknown integration {requested_name!r}. "
                f"Available names: {available}"
            )
        names.append(resolved_name)

    return names


def resolve_targets(
    args: argparse.Namespace,
) -> list[tuple[str | None, str, Path]]:
    output_root = Path(args.output)
    if args.integration_id:
        return [(None, args.integration_id, output_root)]

    integrations = load_integrations(Path(args.integrations_map))
    names = (
        list(integrations)
        if args.integration is None
        else resolve_map_names(args.integration, integrations)
    )
    targets: list[tuple[str | None, str, Path]] = []
    destinations: dict[str, str] = {}
    for name in names:
        directory = output_name(name)
        previous_name = destinations.get(directory)
        if previous_name and previous_name != name:
            raise RuntimeError(
                "Integration names produce the same output directory: "
                f"{previous_name!r} and {name!r}"
            )
        destinations[directory] = name
        targets.append((name, integrations[name], output_root / directory))
    return targets


def discover_local_integrations(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    integrations: list[dict[str, Any]] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            continue
        name = directory.name
        manifest_path = directory / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = read_json(manifest_path)
            except RuntimeError:
                manifest = {}
            if isinstance(manifest, dict) and isinstance(manifest.get("name"), str):
                name = manifest["name"]
        integrations.append(
            {
                "name": name,
                "directory": directory.name,
                "path": directory,
            }
        )
    return integrations


def select_names(
    choices: list[str],
    requested: list[str] | None,
    prompt: str,
) -> list[str]:
    by_casefold = {choice.casefold(): choice for choice in choices}
    if requested:
        selected: list[str] = []
        for value in requested:
            match = by_casefold.get(value.casefold())
            if match is None:
                raise RuntimeError(
                    f"Unknown integration {value!r}. Available names: "
                    + ", ".join(choices)
                )
            if match not in selected:
                selected.append(match)
        return selected

    if not choices:
        raise RuntimeError(
            "No local integrations were found. Use --integration NAME "
            "or provide a bundle containing integration metadata."
        )
    if not sys.stdin.isatty():
        raise RuntimeError(
            "No integration was specified and no interactive terminal is available. "
            "Use --integration NAME."
        )

    print(prompt)
    for index, choice in enumerate(choices, start=1):
        print(f"  {index}. {choice}")
    while True:
        answer = input("Select number(s), or 'all': ").strip().lower()
        if answer == "all":
            return list(choices)
        try:
            indexes = [int(value.strip()) for value in answer.split(",")]
        except ValueError:
            indexes = []
        if indexes and all(1 <= index <= len(choices) for index in indexes):
            selected = []
            for index in indexes:
                choice = choices[index - 1]
                if choice not in selected:
                    selected.append(choice)
            return selected
        print("Please enter valid numbered choices, for example 1 or 1,3.")


def resolve_bundle_export_targets(
    args: argparse.Namespace,
) -> list[tuple[str, Path]]:
    local = discover_local_integrations(Path(args.source))
    if args.integration:
        selected: list[dict[str, Any]] = []
        for requested in args.integration:
            matches = [
                item
                for item in local
                if item["name"].casefold() == requested.casefold()
                or item["directory"].casefold() == requested.casefold()
            ]
            if not matches:
                raise RuntimeError(
                    f"Unknown local integration {requested!r}. Available names: "
                    + ", ".join(item["name"] for item in local)
                )
            if matches[0] not in selected:
                selected.append(matches[0])
        return [
            (item["name"], item["path"])
            for item in selected
        ]

    selected_names = select_names(
        [item["name"] for item in local],
        None,
        "Available local integrations:",
    )
    selected = {name.casefold() for name in selected_names}
    return [
        (item["name"], item["path"])
        for item in local
        if item["name"].casefold() in selected
    ]


def default_bundle_path(names: list[str]) -> Path:
    return Path(DEFAULT_BUNDLE_FILE)


def resolve_bundle_import_selection(
    args: argparse.Namespace,
    bundle_path: Path,
) -> list[str]:
    if not bundle_path.is_file():
        raise RuntimeError(f"Bundle file not found: {bundle_path}")
    options = bundle_integration_options(bundle_path)
    return select_bundle_names(options, args.integration)


def select_bundle_names(
    options: list[dict[str, str]],
    requested: list[str] | None,
) -> list[str]:
    if requested:
        selected: list[str] = []
        for value in requested:
            matches = [
                option
                for option in options
                if option["name"].casefold() == value.casefold()
                or option["directory"].casefold() == value.casefold()
            ]
            if not matches:
                available = ", ".join(
                    f"{option['name']} ({option['directory']})"
                    for option in options
                )
                raise RuntimeError(
                    f"Unknown bundle integration {value!r}. Available: {available}"
                )
            name = matches[0]["name"]
            if name not in selected:
                selected.append(name)
        return selected

    if not options:
        raise RuntimeError("The bundle contains no integration folders.")
    if not sys.stdin.isatty():
        raise RuntimeError(
            "No integration was specified and no interactive terminal is available. "
            "Use --integration NAME."
        )

    print("Available bundle integrations:")
    for index, option in enumerate(options, start=1):
        print(f"  {index}. {option['name']} ({option['directory']})")
    while True:
        answer = input("Select number(s), or 'all': ").strip().lower()
        if answer == "all":
            return [option["name"] for option in options]
        try:
            indexes = [int(value.strip()) for value in answer.split(",")]
        except ValueError:
            indexes = []
        if indexes and all(1 <= index <= len(options) for index in indexes):
            selected = []
            for index in indexes:
                name = options[index - 1]["name"]
                if name not in selected:
                    selected.append(name)
            return selected
        print("Please enter valid numbered choices, for example 1 or 1,3.")


def effective_policy(command: str, args: argparse.Namespace) -> str:
    if not args.overwrite:
        return args.on_mismatch
    if command == "download":
        return "remote"
    return "local"


def run_bundle(args: argparse.Namespace) -> int:
    if args.bundle_command == "export":
        targets = resolve_bundle_export_targets(args)
        bundle_path = Path(args.output) if args.output else default_bundle_path(
            [name for name, _ in targets]
        )
        export_bundle(
            source_root=Path(args.source),
            bundle_path=bundle_path,
            targets=targets,
            complete=False,
        )
        return 0
    if args.bundle_command == "import":
        bundle_path = Path(args.input) if args.input else Path(DEFAULT_BUNDLE_FILE)
        selected_names = resolve_bundle_import_selection(args, bundle_path)
        import_bundle(
            bundle_path=bundle_path,
            destination_root=Path(args.destination),
            selected_names=selected_names,
        )
        return 0
    raise RuntimeError(f"Unsupported bundle command: {args.bundle_command}")


def run() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "bundle":
            return run_bundle(args)

        secrets = load_secrets(Path(args.secrets))
        client = LangdockClient(
            api_key=secrets["api_key"],
            base_url=secrets.get("base_url", "https://api.langdock.com"),
        )
        policy = effective_policy(args.command, args)
        failures: list[str] = []

        for name, integration_id, output_dir in resolve_targets(args):
            label = name or integration_id
            print(f"Processing {args.command}: {label}")
            try:
                if args.command == "download":
                    download_integration(client, integration_id, output_dir, policy)
                elif args.command == "upload":
                    upload_integration(client, integration_id, output_dir, policy)
                elif args.command == "sync":
                    sync_integration(client, integration_id, output_dir, policy)
                else:
                    parser.error(f"Unsupported command: {args.command}")
            except UserAborted:
                raise
            except RuntimeError as exc:
                print(f"ERROR processing {label}: {exc}", file=sys.stderr)
                failures.append(str(label))

        if failures:
            failed_names = ", ".join(failures)
            print(
                f"ERROR: Failed to process {len(failures)} integration(s): "
                f"{failed_names}",
                file=sys.stderr,
            )
            return 1
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
