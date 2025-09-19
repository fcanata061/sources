#!/usr/bin/env python3
import argparse
import sys
import subprocess
from rich.console import Console
from rich.table import Table
from rich.progress import Progress
from rich.spinner import Spinner
from rich.panel import Panel

# módulos internos
from source.modules import (
    build,
    remove,
    search,
    info,
    upgrade,
    update,
    sync,
    cache,
    history,
    auto,
    hooks,
    binpkg,
    config,
)

console = Console()

def run_hooks(stage: str):
    """Executa hooks globais definidos no source.conf"""
    hook_path = None
    if stage == "pre":
        hook_path = config.get("hooks", "pre_hooks", fallback=None)
    elif stage == "post":
        hook_path = config.get("hooks", "post_hooks", fallback=None)

    if hook_path:
        try:
            subprocess.run(hook_path, shell=True, check=True)
            console.print(f"[green]✓ Hook {stage} executado[/green]")
        except subprocess.CalledProcessError:
            console.print(f"[red]✗ Hook {stage} falhou[/red]")


def notify(title, message):
    """Notifica no desktop se estiver habilitado"""
    if config.getboolean("notifications", "enabled", fallback=False):
        subprocess.run(
            ["notify-send", title, message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main():
    parser = argparse.ArgumentParser(
        prog="source",
        description="Source Package Manager CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # --- build ---
    build_parser = subparsers.add_parser("build", aliases=["b"], help="Build a package")
    build_parser.add_argument("package", help="Package to build")
    build_parser.add_argument(
        "--with-install",
        action="store_true",
        help="Also install after build",
    )

    # --- install ---
    install_parser = subparsers.add_parser("install", aliases=["i"], help="Install a package")
    install_parser.add_argument("package", help="Package to install")

    # --- remove ---
    remove_parser = subparsers.add_parser("remove", aliases=["r"], help="Remove a package")
    remove_parser.add_argument("package", help="Package to remove")

    # --- search ---
    search_parser = subparsers.add_parser("search", aliases=["s"], help="Search packages")
    search_parser.add_argument("query", help="Search term")

    # --- info ---
    info_parser = subparsers.add_parser("info", aliases=["in"], help="Show package info")
    info_parser.add_argument("package", help="Package to show info")

    # --- upgrade ---
    subparsers.add_parser("upgrade", aliases=["ug"], help="Upgrade installed packages")

    # --- update ---
    subparsers.add_parser("update", aliases=["up"], help="Check for new versions")

    # --- sync ---
    subparsers.add_parser("sync", aliases=["sy"], help="Sync recipes from git repo")

    # --- cache ---
    cache_parser = subparsers.add_parser("cache", help="Cache operations")
    cache_parser.add_argument("action", choices=["clean", "deepclean"], help="Cache action")

    # --- history ---
    subparsers.add_parser("history", aliases=["h"], help="Show build/install history")

    # --- auto ---
    subparsers.add_parser("auto", aliases=["a"], help="Auto build/install pending packages")

    # --- rebuild-system ---
    subparsers.add_parser("rebuild-system", aliases=["rsys"], help="Rebuild the whole system")

    # --- rebuild (single pkg) ---
    rebuild_parser = subparsers.add_parser("rebuild", aliases=["rb"], help="Rebuild a package")
    rebuild_parser.add_argument("package", help="Package to rebuild")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Executa hooks globais
    run_hooks("pre")

    try:
        if args.command in ["build", "b"]:
            builder = build.Builder()
            builder.build(args.package, with_install=args.with_install)
            notify("Source", f"Build finalizado: {args.package}")

        elif args.command in ["install", "i"]:
            binpkg.install(args.package)
            notify("Source", f"Instalado: {args.package}")

        elif args.command in ["remove", "r"]:
            remove.remove(args.package)
            notify("Source", f"Removido: {args.package}")

        elif args.command in ["search", "s"]:
            results = search.search(args.query)
            table = Table(title=f"Resultados para {args.query}")
            table.add_column("Nome", style="cyan")
            table.add_column("Versão", style="green")
            table.add_column("Descrição", style="white")
            for r in results:
                table.add_row(r["name"], r["version"], r["summary"])
            console.print(table)

        elif args.command in ["info", "in"]:
            pkginfo = info.get_info(args.package)
            console.print(Panel(str(pkginfo), title=f"Info: {args.package}", style="bold green"))

        elif args.command in ["upgrade", "ug"]:
            upgrader = upgrade.Upgrader()
            upgrader.upgrade_world()

        elif args.command in ["update", "up"]:
            updater = update.Updater()
            updater.check_all()

        elif args.command in ["sync", "sy"]:
            sync.sync()

        elif args.command == "cache":
            if args.action == "clean":
                cache.clean()
            elif args.action == "deepclean":
                cache.deepclean()

        elif args.command in ["history", "h"]:
            history.show()

        elif args.command in ["auto", "a"]:
            auto.run()

        elif args.command in ["rebuild-system", "rsys"]:
            history.rebuild_system()

        elif args.command in ["rebuild", "rb"]:
            history.rebuild_package(args.package)

    except Exception as e:
        console.print(f"[red]Erro: {e}[/red]")
        sys.exit(1)

    run_hooks("post")


if __name__ == "__main__":
    main()
