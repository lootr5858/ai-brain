#!/usr/bin/env python3
"""Print or verify the local brain-mcp STDIO connection."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.brain_mcp_install import (
    InstallError,
    discover_context,
    stdio_connection,
    validate_context,
    verify_stdio,
)
from lib.config import CONFIG_ENV, config_path, load_config


def stdio_command(args) -> int:
    context = discover_context(args.config)
    validate_context(context)
    print(json.dumps(stdio_connection(context), sort_keys=True))
    return 0


def verify_stdio_command(args) -> int:
    for check in verify_stdio(discover_context(args.config)):
        print(f"VERIFY {check}: PASS")
    return 0


def _systemd_exec_arg(path: Path) -> str:
    value = str(path.absolute())
    if any(character in value for character in "\0\n\r"):
        raise InstallError("systemd service paths cannot contain control characters")
    value = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
    )
    return f'"{value}"'


def remote_install_service_command(args) -> int:
    engine = Path(__file__).resolve().parents[1]
    python = engine / ".venv/bin/python"
    entrypoint = engine / "servers/brain-mcp/http.py"
    template = engine / "servers/brain-mcp/brain-mcp-remote.service.in"
    if not python.is_file():
        raise InstallError(f"missing virtual-environment Python: {python}")
    if not entrypoint.is_file():
        raise InstallError(f"missing remote brain-MCP entrypoint: {entrypoint}")
    if not template.is_file():
        raise InstallError(f"missing systemd service template: {template}")

    load_config(args.config)
    explicit = args.config is not None or CONFIG_ENV in os.environ
    config_argument = (
        " --config " + _systemd_exec_arg(config_path(args.config))
        if explicit
        else ""
    )

    rendered = template.read_text().replace(
        "@BRAIN_MCP_PYTHON@",
        _systemd_exec_arg(python),
    ).replace(
        "@BRAIN_MCP_HTTP@",
        _systemd_exec_arg(entrypoint),
    ).replace(
        "@BRAIN_MCP_CONFIG@",
        config_argument,
    )
    if "@BRAIN_MCP_" in rendered:
        raise InstallError("systemd service template contains an unknown placeholder")

    config_home = os.environ.get("XDG_CONFIG_HOME")
    destination = (
        Path(config_home) if config_home else Path.home() / ".config"
    ) / "systemd/user/brain-mcp-remote.service"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered)
    destination.chmod(0o644)
    print(f"Installed brain-mcp-remote.service: {destination}")
    return 0


def remote_init_command(_args) -> int:
    from lib.brain_mcp_remote import normalize_public_url
    from lib.brain_mcp_remote_store import RemoteStore

    resource = normalize_public_url(input("Public brain-MCP HTTPS URL: "))
    RemoteStore().set_canonical_resource(resource)
    print(f"Remote brain-MCP initialized: {resource}")
    return 0


def _select_profile() -> str:
    print("Select client type:")
    print("1. ChatGPT")
    print("2. Claude")
    print("3. Hermes")
    choice = input("Selection: ").strip()
    try:
        return {"1": "chatgpt", "2": "claude", "3": "hermes"}[choice]
    except KeyError:
        from lib.brain_mcp_remote import RemoteAccessError

        raise RemoteAccessError("client selection must be 1, 2, or 3") from None


def remote_add_client_command(_args) -> int:
    from lib.brain_mcp_remote import client_redirect_uri
    from lib.brain_mcp_remote_store import RemoteStore, StoreError

    store = RemoteStore()
    resource = store.get_canonical_resource()
    if resource is None:
        raise StoreError("run remote init before adding a client")
    profile = _select_profile()
    unique_name = input("Unique name: ")
    if profile == "chatgpt":
        print(f"Brain-MCP URL: {resource}")
        supplied = input("Exact ChatGPT HTTPS callback: ").strip()
    else:
        supplied = None
    redirect_uri = client_redirect_uri(profile, supplied)
    created = store.create_client(
        unique_name=unique_name,
        profile=profile,
        redirect_uri=redirect_uri,
    )
    if profile != "chatgpt":
        print(f"Brain-MCP URL: {resource}")
    print(f"OAuth client ID: {created.client.client_id}")
    print(f"OAuth client secret: {created.client_secret}")
    print()
    print("Save the client secret now. It cannot be displayed again.")
    print("If it is lost, remove this client and create it again.")
    return 0


def remote_list_clients_command(_args) -> int:
    from lib.brain_mcp_remote_store import RemoteStore

    clients = RemoteStore().list_clients()
    if not clients:
        print("No remote clients.")
        return 0
    print("#  NAME  TYPE  CREATED (UTC)")
    for index, client in enumerate(clients, 1):
        created = datetime.fromtimestamp(
            client.created_at,
            tz=timezone.utc,
        ).strftime("%Y-%m-%d")
        print(f"{index}  {client.unique_name}  {client.profile}  {created}")
    return 0


def remote_remove_client_command(_args) -> int:
    from lib.brain_mcp_remote_store import RemoteStore, StoreError

    store = RemoteStore()
    clients = store.list_clients()
    if not clients:
        print("No remote clients.")
        return 0
    for index, client in enumerate(clients, 1):
        print(f"{index}. {client.unique_name} ({client.profile})")
    print("all. Remove all clients")
    selection = input("Selection: ").strip()
    if selection.casefold() == "all":
        confirmation = input('Type "REMOVE ALL" to confirm: ')
        if confirmation != "REMOVE ALL":
            print("Cancelled.")
            return 0
        removed = store.remove_all_clients()
        print("Removed remote clients: " + ", ".join(removed))
        return 0
    try:
        index = int(selection)
    except ValueError:
        raise StoreError("client selection must be a number or all") from None
    if not 1 <= index <= len(clients):
        raise StoreError("client selection is out of range")
    name = store.remove_client(clients[index - 1].client_id)
    print(f"Removed remote client: {name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stdio-command").set_defaults(handler=stdio_command)
    commands.add_parser("verify-stdio").set_defaults(handler=verify_stdio_command)
    remote = commands.add_parser("remote")
    remote_commands = remote.add_subparsers(dest="remote_command", required=True)
    remote_commands.add_parser("install-service").set_defaults(
        handler=remote_install_service_command
    )
    remote_commands.add_parser("init").set_defaults(handler=remote_init_command)
    remote_commands.add_parser("add-client").set_defaults(
        handler=remote_add_client_command
    )
    remote_commands.add_parser("list-clients").set_defaults(
        handler=remote_list_clients_command
    )
    remote_commands.add_parser("remove-client").set_defaults(
        handler=remote_remove_client_command
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (EOFError, InstallError, OSError) as exc:
        print(f"REFUSE {exc}")
        return 1
    except Exception as exc:
        if args.command != "remote":
            raise
        from lib.brain_mcp_remote import RemoteAccessError
        from lib.brain_mcp_remote_store import StoreError

        if not isinstance(exc, (RemoteAccessError, StoreError)):
            raise
        print(f"REFUSE {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
