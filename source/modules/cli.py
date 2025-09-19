import argparse
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
import subprocess
import sys

from modules.config import config
from modules import build, remove, search, info, upgrade, update, auto, cache, sync, history, hooks

console = Console()

class SourceCLI:
    def __init__(self):
        self.parser = argparse.ArgumentParser(
            prog="source",
            description="Source Package Manager",
        )
        self.subparsers = self.parser.add_subparsers(dest="command")

        # Aliases
        self.commands = {
            "install": ("i", self.cmd_install),
            "remove": ("r", self.cmd_remove),
            "search": ("s", self.cmd_search),
            "info": ("in", self.cmd_info),
            "upgrade": ("ug", self.cmd_upgrade),
            "update": ("up", self.cmd_update),
            "build": ("b", self.cmd_build),
            "sync": ("sy", self.cmd_sync),
            "cache-clean": ("cc", self.cmd_cache_clean),
            "cache-deepclean": ("dc", self.cmd_cache_deepclean),
            "history": ("h", self.cmd_history),
            "auto": ("a", self.cmd_auto),
            "rebuild-system": ("rsys", self.cmd_rebuild_system),
            "rebuild": ("rb", self.cmd_rebuild),
        }

        for cmd, (alias, func) in self.commands.items():
            sp = self.subparsers.add_parser(cmd, aliases=[alias])
            sp.set_defaults(func=func)
            sp.add_argument("args", nargs="*")

        # Flags globais
        self.parser.add_argument("--dry-run", action="store_true", help="Executar em modo simulado")
        self.parser.add_argument("--no-color", action="store_true", help="Desativar cores")
        self.parser.add_argument("--no-animations", action="store_true", help="Desativar animações")

    def run(self):
        args = self.parser.parse_args()

        # Config global herdada do source.conf
        self.dry_run = args.dry_run or config.dry_run
        self.use_colors = not args.no_color and config.use_colors
        self.use_animations = not args.no_animations and config.use_animations

        if not args.command:
            self.parser.print_help()
            return

        func = getattr(args, "func", None)
        if func:
            self._run_with_hooks(func, args)

    # =====================
    # Hooks globais
    # =====================
    def _run_with_hooks(self, func, args):
        if config.pre_hooks and not self.dry_run:
            subprocess.call([config.pre_hooks, args.command])

        func(args)

        if config.post_hooks and not self.dry_run:
            subprocess.call([config.post_hooks, args.command])

    # =====================
    # Comandos
    # =====================
    def cmd_install(self, args):
        for pkg in args.args:
            self._simulate_or_run(lambda: build.install(pkg), f"📦 Instalando {pkg}")

    def cmd_remove(self, args):
        for pkg in args.args:
            self._simulate_or_run(lambda: remove.remove(pkg), f"🗑️ Removendo {pkg}")

    def cmd_search(self, args):
        term = args.args[0] if args.args else ""
        results = search.search(term)
        table = Table(title=f"🔍 Resultados para '{term}'")
        table.add_column("Pacote", style="cyan")
        table.add_column("Versão", style="green")
        table.add_column("Descrição", style="yellow")
        for r in results:
            table.add_row(r["name"], r["version"], r["desc"])
        console.print(table)

    def cmd_info(self, args):
        for pkg in args.args:
            data = info.get_info(pkg)
            table = Table(title=f"ℹ️ Info: {pkg}")
            for k, v in data.items():
                table.add_row(str(k), str(v))
            console.print(table)

    def cmd_upgrade(self, args):
        pkgs = args.args or []
        self._simulate_or_run(lambda: upgrade.upgrade(pkgs), "⬆️ Atualizando pacotes")

    def cmd_update(self, args):
        self._simulate_or_run(update.update_all, "🔄 Procurando novas versões")

    def cmd_build(self, args):
        for pkg in args.args:
            self._simulate_or_run(lambda: build.build(pkg), f"⚙️ Build {pkg}")

    def cmd_sync(self, args):
        self._simulate_or_run(sync.sync_repo, "🔄 Sincronizando repositório")

    def cmd_cache_clean(self, args):
        self._simulate_or_run(cache.clean, "🧹 Limpando cache")

    def cmd_cache_deepclean(self, args):
        self._simulate_or_run(cache.deepclean, "🔥 Deepclean cache")

    def cmd_history(self, args):
        history.show_history()

    def cmd_auto(self, args):
        self._simulate_or_run(auto.auto_manage, "🤖 Gerenciando pacotes automaticamente")

    def cmd_rebuild_system(self, args):
        self._simulate_or_run(build.rebuild_system, "♻️ Recompilando todo o sistema")

    def cmd_rebuild(self, args):
        for pkg in args.args:
            self._simulate_or_run(lambda: build.rebuild(pkg), f"♻️ Recompilando {pkg}")

    # =====================
    # Helpers
    # =====================
    def _simulate_or_run(self, func, message):
        if self.dry_run:
            console.print(f"[yellow][DRY-RUN][/yellow] {message}")
            return

        if self.use_animations:
            with Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                transient=True,
            ) as progress:
                task = progress.add_task(message, total=None)
                try:
                    func()
                    progress.update(task, completed=1)
                    console.print(f"[green]✅ {message} concluído[/green]")
                except Exception as e:
                    console.print(f"[red]❌ Erro: {e}[/red]")
        else:
            console.print(f"[blue]{message}...[/blue]")
            try:
                func()
                console.print(f"[green]✅ {message} concluído[/green]")
            except Exception as e:
                console.print(f"[red]❌ Erro: {e}[/red]")
