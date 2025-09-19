# source/modules/cli.py
"""
Central CLI for 'sources' manager.
- Uses rich for colored output, tables, spinners and progress bars.
- Integrates available modules in source/modules: build, remove, search, info,
  upgrade, update, auto, cache, sync, deepclean, history, hooks, logger, etc.
- Dry-run by default (use --execute to apply), supports --no-color and --quiet.
- Provides many aliases / abbreviations for commands.

Usage examples:
  python -m source.modules.cli sync run --execute
  python -m source.modules.cli build gcc --execute
  python -m source.modules.cli upgrade --execute
  python -m source.modules.cli update --execute
  python -m source.modules.cli rsys --execute    # rebuild-system
  python -m source.modules.cli rb gcc --execute  # rebuild single package
"""

from __future__ import annotations
import argparse
import importlib
import json
import os
import sys
import traceback
from typing import Any, Dict, List, Optional

# rich UI
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

# Try to import modules; gracefully degrade if not present
def try_import(modname: str):
    try:
        return importlib.import_module(modname)
    except Exception:
        return None

modules = {}
for m in (
    "modules.build",
    "modules.remove",
    "modules.search",
    "modules.info",
    "modules.upgrade",
    "modules.update",
    "modules.auto",
    "modules.cache",
    "modules.sync",
    "modules.deepclean",
    "modules.history",
    "modules.hooks",
    "modules.logger",
):
    modules[m.split(".")[-1]] = try_import(m)

# Fallback simple logger if modules.logger missing
if modules.get("logger") and hasattr(modules["logger"], "Logger"):
    try:
        LOG = modules["logger"].Logger("cli.log")
    except Exception:
        class SimpleLog:
            def info(self, *a, **k): print("[INFO]", *a)
            def error(self, *a, **k): print("[ERROR]", *a, file=sys.stderr)
            def debug(self, *a, **k): print("[DEBUG]", *a)
        LOG = SimpleLog()
else:
    class SimpleLog:
        def info(self, *a, **k): print("[INFO]", *a)
        def error(self, *a, **k): print("[ERROR]", *a, file=sys.stderr)
        def debug(self, *a, **k): print("[DEBUG]", *a)
    LOG = SimpleLog()

# Hooks manager if available
HookManager = None
if modules.get("hooks") and hasattr(modules["hooks"], "HookManager"):
    try:
        HookManager = modules["hooks"].HookManager
    except Exception:
        HookManager = None

# helpers
def safe_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:
        LOG.error(f"Error calling {fn}: {e}")
        LOG.debug(traceback.format_exc())
        return None

def print_panel(console: Console, title: str, text: str, style: str = "green"):
    console.print(Panel(text, title=title, style=style))

# Create console with color toggles
def make_console(no_color: bool, quiet: bool) -> Console:
    if no_color:
        return Console(color_system=None, force_terminal=False, markup=False, quiet=quiet)
    return Console()

# CLI action implementations (wrappers that call modules when present)
class CLI:
    def __init__(self, console: Console, dry_run: bool = True, hooks_enabled: bool = True):
        self.console = console
        self.dry_run = dry_run
        self.quiet = False
        self.hooks = None
        if HookManager:
            try:
                self.hooks = HookManager(dry_run=self.dry_run)
            except Exception:
                try:
                    self.hooks = HookManager()
                    self.hooks.dry_run = self.dry_run
                except Exception:
                    self.hooks = None

        # module shortcuts
        self.build_mod = modules.get("build")
        self.remove_mod = modules.get("remove")
        self.search_mod = modules.get("search")
        self.info_mod = modules.get("info")
        self.upgrade_mod = modules.get("upgrade")
        self.update_mod = modules.get("update")
        self.auto_mod = modules.get("auto")
        self.cache_mod = modules.get("cache")
        self.sync_mod = modules.get("sync")
        self.deepclean_mod = modules.get("deepclean")
        self.history_mod = modules.get("history")

    def _run_hook(self, name: str, payload: Dict[str, Any]):
        if self.hooks and hasattr(self.hooks, "run_hooks"):
            try:
                self.hooks.run_hooks(name, payload, None)
            except Exception as e:
                LOG.error(f"Hook {name} failed: {e}")

    # -----------------------
    # sync
    # -----------------------
    def cmd_sync(self, args: argparse.Namespace):
        console = self.console
        self._run_hook("pre_cli_command", {"cmd": "sync", "args": vars(args)})
        if not self.sync_mod:
            console.print("[red]sync module not available[/red]")
            return 1
        mgr_cls = getattr(self.sync_mod, "SyncManager", None)
        mgr = mgr_cls() if mgr_cls else None
        if args.sub == "run":
            console.print("[blue]Synchronizing repository...[/blue]")
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as p:
                p.add_task("sync", total=None)
                try:
                    res = mgr.sync(force_reset=args.force) if mgr else None
                    console.print(Panel(f"Synced to: {res}", title="sync", style="green"))
                except Exception as e:
                    console.print(f"[red]Sync failed: {e}[/red]")
                    LOG.error(str(e))
                    return 2
        elif args.sub == "status":
            # show last sync timestamp
            if mgr:
                d = mgr.config.get("dest_dir")
                ts_file = os.path.join(d, ".last_sync")
                if os.path.exists(ts_file):
                    with open(ts_file, "r", encoding="utf-8") as fh:
                        ts = fh.read().strip()
                    console.print(Panel(f"Last sync: {ts}\nPath: {d}", title="sync status", style="cyan"))
                else:
                    console.print("[yellow]No .last_sync timestamp found[/yellow]")
            else:
                console.print("[red]No SyncManager available[/red]")
        else:
            console.print("[red]Unknown sync subcommand[/red]")
        self._run_hook("post_cli_command", {"cmd": "sync", "args": vars(args)})
        return 0

    # -----------------------
    # search
    # -----------------------
    def cmd_search(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd": "search", "args": vars(args)})
        console = self.console
        if not self.search_mod or not hasattr(self.search_mod, "PackageSearch"):
            console.print("[red]search module not available[/red]")
            return 2
        searcher = self.search_mod.PackageSearch(repo_path=args.recipes or "/usr/sources")
        matches = []
        # Some search modules may implement .search or .find
        if hasattr(searcher, "search"):
            matches = safe_call(searcher.search, args.term) or []
        elif hasattr(searcher, "find"):
            matches = safe_call(searcher.find, args.term) or []
        else:
            # fallback: simple filesystem scan under recipes dir
            for root, _, files in os.walk(args.recipes or "/usr/sources"):
                for fn in files:
                    if fn.lower().startswith("recipe"):
                        path = os.path.join(root, fn)
                        if args.term.lower() in root.lower():
                            matches.append(os.path.relpath(root, args.recipes or "/usr/sources"))
        # display results
        table = Table(title=f"Search results for '{args.term}'", show_lines=False)
        table.add_column("Package", style="bold")
        table.add_column("Recipe path")
        table.add_column("Summary", overflow="fold")
        for m in matches:
            # match may be string or dict
            if isinstance(m, dict):
                name = m.get("name") or m.get("pkg") or "?"
                path = m.get("path") or "?"
                summary = m.get("summary", "")
            else:
                name = os.path.basename(m)
                path = m
                summary = ""
            table.add_row(name, path, summary)
        console.print(table)
        self._run_hook("post_cli_command", {"cmd": "search", "args": vars(args), "result_count": len(matches)})
        return 0

    # -----------------------
    # info
    # -----------------------
    def cmd_info(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd": "info", "args": vars(args)})
        console = self.console
        if not self.info_mod or not hasattr(self.info_mod, "PackageInfo"):
            console.print("[red]info module not available[/red]")
            return 2
        pi = self.info_mod.PackageInfo(recipe_dir=args.recipes or "/usr/sources")
        try:
            console.print(f"[blue]Package:[/blue] {args.package}")
            console.print(f"[blue]State:[/blue] {pi.status(args.package)}")
            details = pi.details(args.package, verbose=args.verbose)
            tbl = Table(title=f"Info: {args.package}")
            tbl.add_column("Key", style="bold")
            tbl.add_column("Value", overflow="fold")
            for k in ("name", "version", "summary", "description", "build_system"):
                tbl.add_row(k, str(details.get(k)))
            deps = details.get("dependencies", [])
            tbl.add_row("dependencies", ", ".join(deps) if deps else "-")
            console.print(tbl)
            if args.verbose:
                if details.get("hooks"):
                    console.print(Panel(json.dumps(details.get("hooks"), indent=2, ensure_ascii=False), title="hooks"))
                if details.get("files"):
                    ftable = Table(title="Installed files")
                    ftable.add_column("Path")
                    for f in details.get("files"):
                        ftable.add_row(f)
                    console.print(ftable)
        except Exception as e:
            console.print(f"[red]Info error: {e}[/red]")
            LOG.error(str(e))
            return 2
        self._run_hook("post_cli_command", {"cmd":"info", "args": vars(args)})
        return 0

    # -----------------------
    # build / install / rebuild
    # -----------------------
    def _call_build(self, pkg: str, execute: bool, rebuild: bool = False):
        console = self.console
        if not self.build_mod:
            console.print("[red]build module not available[/red]")
            return {"status":"no-build"}
        # Attempt to find Builder class or build function
        Builder = getattr(self.build_mod, "Builder", None) or getattr(self.build_mod, "BuildManager", None)
        build_fn = None
        if Builder:
            try:
                b = Builder(dry_run=(not execute))
            except Exception:
                try:
                    b = Builder()
                    b.dry_run = not execute
                except Exception:
                    b = None
            if b:
                # prefer method build_single_pkg etc.
                for name in ("build_single_pkg","build","build_pkg"):
                    if hasattr(b, name):
                        build_fn = getattr(b, name)
                        break
        # fallback: module-level function
        if not build_fn:
            for name in ("build_single_pkg","build","build_pkg"):
                if hasattr(self.build_mod, name):
                    build_fn = getattr(self.build_mod, name)
                    break
        if not build_fn:
            console.print("[red]No build function found in build module[/red]")
            return {"status":"no-build-fn"}

        console.print(f"[blue]Building {pkg} (execute={execute})[/blue]")
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as p:
            p.add_task("building", total=None)
            try:
                if not execute:
                    # dry-run: just call with dry flag if supported or skip call
                    try:
                        res = build_fn(pkg, os.path.join(args.recipes or "/usr/sources", pkg), None)
                    except Exception:
                        res = {"dry-run": True}
                    console.print("[green]DRY-RUN: build simulated[/green]")
                    return {"status":"dry-run", "result": res}
                else:
                    # call build_fn with common signatures: (pkg, recipe_dir, recipe_data) or (pkg)
                    try:
                        res = build_fn(pkg, os.path.join(args.recipes or "/usr/sources", pkg), None)
                    except TypeError:
                        try:
                            res = build_fn(pkg)
                        except Exception:
                            res = build_fn(os.path.join(args.recipes or "/usr/sources", pkg))
                    console.print("[green]Build call finished[/green]")
                    return {"status":"ok", "result": res}
            except Exception as e:
                console.print(f"[red]Build failed: {e}[/red]")
                LOG.error(traceback.format_exc())
                return {"status":"failed", "error": str(e)}

    def cmd_build(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd": "build", "args": vars(args)})
        pkg = args.package
        res = self._call_build(pkg, execute=args.execute, rebuild=False)
        self._run_hook("post_cli_command", {"cmd":"build", "args": vars(args), "result": res})
        return 0 if res.get("status") in ("ok","dry-run") else 2

    # alias install
    def cmd_install(self, args: argparse.Namespace):
        return self.cmd_build(args)

    # rebuild single package (force rebuild)
    def cmd_rebuild(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd":"rebuild", "args": vars(args)})
        # force build (rebuild) -- similar to build but we can add cleaning
        pkg = args.package
        # try to call build manager with a 'clean' or 'rebuild' option if available
        res = self._call_build(pkg, execute=args.execute, rebuild=True)
        self._run_hook("post_cli_command", {"cmd":"rebuild", "args": vars(args), "result": res})
        return 0

    # rebuild entire system (recompile all installed packages)
    def cmd_rebuild_system(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd":"rebuild-system", "args": vars(args)})
        console = self.console
        # try to use UpgradeManager or UpgradeModule to orchestrate deps and builds
        if self.upgrade_mod and hasattr(self.upgrade_mod, "UpgradeManager"):
            mgr = self.upgrade_mod.UpgradeManager(conf_path=args.conf, dry_run=(not args.execute))
            console.print("[blue]Preparing to rebuild entire system...[/blue]")
            # find all installed packages and attempt upgrade with force True to rebuild
            report = mgr.upgrade(packages=None, execute=args.execute, force=True, concurrency=args.concurrency)
            console.print(Panel(json.dumps(report, indent=2, ensure_ascii=False), title="rebuild-system report"))
            self._run_hook("post_cli_command", {"cmd":"rebuild-system", "args": vars(args), "report": report})
            return 0
        # fallback: iterate installed_db and call build for each
        installed_db = args.db or "/var/lib/sources/installed_db.json"
        try:
            with open(installed_db, "r", encoding="utf-8") as fh:
                installed = json.load(fh)
        except Exception:
            console.print("[red]Could not read installed_db.json[/red]")
            return 2
        pkgs = list(installed.keys())
        console.print(f"[blue]Will rebuild {len(pkgs)} packages (dry-run={not args.execute})[/blue]")
        with Progress("[progress.description]{task.description}", SpinnerColumn(), BarColumn(), TimeElapsedColumn()) as progress:
            task = progress.add_task("rebuilding", total=len(pkgs))
            results = {}
            for p in pkgs:
                if args.only and p != args.only:
                    progress.advance(task)
                    continue
                # call build
                res = self._call_build(p, execute=args.execute)
                results[p] = res
                progress.advance(task)
        self._run_hook("post_cli_command", {"cmd":"rebuild-system", "args": vars(args), "results": results})
        console.print(Panel("Rebuild-system completed", title="rebuild-system", style="green"))
        return 0

    # -----------------------
    # remove
    # -----------------------
    def cmd_remove(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd":"remove", "args": vars(args)})
        console = self.console
        if not self.remove_mod:
            console.print("[red]remove module not available[/red]")
            return 2
        Rem = getattr(self.remove_mod, "Remover", None) or getattr(self.remove_mod, "RemoveManager", None)
        if Rem:
            try:
                rem = Rem(installed_db=args.db, dry_run=(not args.execute))
            except Exception:
                try:
                    rem = Rem(installed_db=args.db)
                    rem.dry_run = (not args.execute)
                except Exception:
                    rem = None
        else:
            rem = None
        pkg = args.package
        console.print(f"[blue]Removing {pkg} (execute={args.execute})[/blue]")
        if rem and hasattr(rem, "remove_package"):
            try:
                res = rem.remove_package(pkg, force=args.force, backup=args.backup)
                console.print(f"[green]Remove result:[/green] {res}")
            except Exception as e:
                console.print(f"[red]Remove failed: {e}[/red]")
                LOG.error(traceback.format_exc())
                return 2
        else:
            # fallback: pretend remove by editing installed_db
            if args.execute:
                try:
                    with open(args.db, "r", encoding="utf-8") as fh:
                        db = json.load(fh)
                except Exception:
                    db = {}
                if pkg in db:
                    del db[pkg]
                    if args.execute:
                        with open(args.db, "w", encoding="utf-8") as fh:
                            json.dump(db, fh, indent=2)
                    console.print(f"[green]Removed {pkg} from installed_db (fallback)[/green]")
                else:
                    console.print(f"[yellow]Package {pkg} not present in installed_db[/yellow]")
        self._run_hook("post_cli_command", {"cmd":"remove", "args": vars(args)})
        return 0

    # -----------------------
    # upgrade (local repo based)
    # -----------------------
    def cmd_upgrade(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd":"upgrade", "args": vars(args)})
        console = self.console
        if not self.upgrade_mod or not hasattr(self.upgrade_mod, "UpgradeManager"):
            console.print("[red]upgrade module not available[/red]")
            return 2
        mgr = self.upgrade_mod.UpgradeManager(conf_path=args.conf, dry_run=(not args.execute))
        pkgs = None
        if args.pkg:
            pkgs = args.pkg
        console.print(f"[blue]Finding upgrade candidates (dry-run={not args.execute})[/blue]")
        report = mgr.upgrade(packages=pkgs, execute=args.execute, force=args.force, concurrency=args.concurrency)
        console.print(Panel(json.dumps(report, indent=2, ensure_ascii=False), title="upgrade report", style="green"))
        self._run_hook("post_cli_command", {"cmd":"upgrade", "args": vars(args), "report": report})
        return 0

    # -----------------------
    # update (check only)
    # -----------------------
    def cmd_update(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd":"update", "args": vars(args)})
        console = self.console
        if not self.update_mod:
            console.print("[red]update module not available[/red]")
            return 2
        # call main function or UpdateChecker
        if hasattr(self.update_mod, "UpdateChecker"):
            uc = self.update_mod.UpdateChecker(conf_path=args.conf, dry_run=(not args.execute))
            report = uc.check_all(only=args.only, exclude=args.exclude, concurrency=args.concurrency)
            # write reports if execute True
            json_path, txt_path = uc._write_reports(report, execute=args.execute, basename=args.basename)
            uc.notify_summary(report, execute=args.execute)
            console.print(Panel(json.dumps(report, indent=2, ensure_ascii=False), title="update report"))
        else:
            # try module-level main or run
            if hasattr(self.update_mod, "main"):
                try:
                    self.update_mod.main([])
                except Exception as e:
                    console.print(f"[red]update.main failed: {e}[/red]")
            else:
                console.print("[red]update functionality not available[/red]")
        self._run_hook("post_cli_command", {"cmd":"update", "args": vars(args)})
        return 0

    # -----------------------
    # auto (automation)
    # -----------------------
    def cmd_auto(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd":"auto", "args": vars(args)})
        console = self.console
        if not self.auto_mod:
            console.print("[red]auto module not available[/red]")
            return 2
        # try AutoUltimate/AutoUpdater class
        clazz = getattr(self.auto_mod, "AutoUltimate", None) or getattr(self.auto_mod, "AutoUpdater", None)
        if clazz:
            au = clazz(dry_run=(not args.execute))
            if args.sub == "check":
                res = au.check_for_updates()
                console.print(Panel(json.dumps(res, indent=2, ensure_ascii=False), title="auto check"))
            elif args.sub == "update-all":
                res = au.auto_update_all(force=args.force) if hasattr(au, "auto_update_all") else au.auto_update_all()
                console.print(Panel(json.dumps(res, indent=2, ensure_ascii=False), title="auto update-all"))
            else:
                # default run
                res = au.auto_update_all(force=args.force) if hasattr(au, "auto_update_all") else {"note":"auto ran"}
                console.print(Panel(json.dumps(res, indent=2, ensure_ascii=False), title="auto"))
        else:
            console.print("[red]auto manager class not found[/red]")
        self._run_hook("post_cli_command", {"cmd":"auto", "args": vars(args)})
        return 0

    # -----------------------
    # cache
    # -----------------------
    def cmd_cache(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd":"cache", "args": vars(args)})
        console = self.console
        if not self.cache_mod:
            console.print("[red]cache module not available[/red]")
            return 2
        CacheMgr = getattr(self.cache_mod, "CacheManager", None)
        cm = CacheMgr(dry_run=(not args.execute)) if CacheMgr else None
        if args.sub == "clean":
            rep = cm.clean_all(keep_recent=not args.force) if cm else {"note":"no cache mgr"}
            console.print(Panel(json.dumps(rep, indent=2, ensure_ascii=False), title="cache clean"))
        elif args.sub == "deepclean":
            # use deepclean module instead if present
            if self.deepclean_mod and hasattr(self.deepclean_mod, "DeepClean"):
                dc_cls = getattr(self.deepclean_mod, "DeepClean")
                dc = dc_cls(dry_run=(not args.execute))
                rep = dc.run(execute=args.execute, purge_orphans_flag=args.purge_orphans, purge_force=args.force,
                             backup_before=(not args.no_backup), clean_caches_flag=True,
                             clean_sandboxes_flag=True, clean_tmp_flag=True, rebuild_db_flag=False,
                             assume_yes=args.yes)
                console.print(Panel(json.dumps(rep, indent=2, ensure_ascii=False), title="deepclean report"))
            else:
                console.print("[red]deepclean module not available[/red]")
        else:
            console.print("[red]cache subcommand unknown[/red]")
        self._run_hook("post_cli_command", {"cmd":"cache", "args": vars(args)})
        return 0

    # -----------------------
    # deepclean
    # -----------------------
    def cmd_deepclean(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd":"deepclean", "args": vars(args)})
        console = self.console
        if not self.deepclean_mod:
            console.print("[red]deepclean module not available[/red]")
            return 2
        DC = getattr(self.deepclean_mod, "DeepClean", None)
        if not DC:
            console.print("[red]DeepClean class not found[/red]")
            return 2
        dc = DC(dry_run=(not args.execute))
        rep = dc.run(execute=args.execute, purge_orphans_flag=args.purge_orphans, purge_force=args.force,
                     backup_before=(not args.no_backup), clean_caches_flag=(not args.no_caches),
                     clean_sandboxes_flag=(not args.no_sandboxes), clean_tmp_flag=(not args.no_tmp),
                     rebuild_db_flag=args.rebuild_db, assume_yes=args.yes)
        console.print(Panel(json.dumps(rep, indent=2, ensure_ascii=False), title="deepclean report"))
        self._run_hook("post_cli_command", {"cmd":"deepclean", "args": vars(args), "report": rep})
        return 0

    # -----------------------
    # history
    # -----------------------
    def cmd_history(self, args: argparse.Namespace):
        self._run_hook("pre_cli_command", {"cmd":"history", "args": vars(args)})
        console = self.console
        if not self.history_mod:
            console.print("[red]history module not available[/red]")
            return 2
        Hist = getattr(self.history_mod, "History", None)
        if not Hist:
            console.print("[red]History class not found[/red]")
            return 2
        h = Hist(history_file=args.file, dry_run=args.dry_run)
        if args.sub == "list":
            items = h.list_history(limit=args.limit, action=args.action, package=args.package, since=args.since, text=args.text)
            console.print(Panel(json.dumps(items, indent=2, ensure_ascii=False), title="history list"))
        elif args.sub == "show":
            ent = h.show(args.id)
            if not ent:
                console.print("[yellow]Entry not found[/yellow]")
            else:
                console.print(Panel(json.dumps(ent, indent=2, ensure_ascii=False), title=f"history {args.id}"))
        elif args.sub == "rollback":
            res = h.rollback(args.id, assume_yes=args.yes)
            console.print(Panel(json.dumps(res, indent=2, ensure_ascii=False), title=f"rollback {args.id}"))
        elif args.sub == "export":
            out = h.export(args.out, fmt=args.fmt)
            console.print(Panel(f"Exported: {out}", title="history export"))
        else:
            console.print("[red]Unknown history subcommand[/red]")
        self._run_hook("post_cli_command", {"cmd":"history", "args": vars(args)})
        return 0

# -----------------------
# CLI wiring and argparse setup
# -----------------------
def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sources", description="sources manager CLI (rich-enabled)")
    ap.add_argument("--no-color", action="store_true", help="Disable color output")
    ap.add_argument("--quiet", action="store_true", help="Quiet mode; less output")
    ap.add_argument("--conf", help="Path to source.conf (passed to subcommands if supported)")
    sub = ap.add_subparsers(dest="command", required=True)

    # sync
    p_sync = sub.add_parser("sync", aliases=["sy"], help="Sync repository (git)")
    p_sync_sub = p_sync.add_subparsers(dest="sub", required=False)
    p_sync_run = p_sync_sub.add_parser("run", help="Run sync")
    p_sync_run.add_argument("--force", action="store_true")
    p_sync_status = p_sync_sub.add_parser("status", help="Show last sync status")

    # search
    p_search = sub.add_parser("search", aliases=["s"], help="Search recipes")
    p_search.add_argument("term")
    p_search.add_argument("--recipes", help="Recipes directory", default="/usr/sources")

    # info
    p_info = sub.add_parser("info", aliases=["in"], help="Show package info")
    p_info.add_argument("package")
    p_info.add_argument("--recipes", default="/usr/sources")
    p_info.add_argument("-v", "--verbose", action="store_true")

    # build / install / rebuild
    p_build = sub.add_parser("build", aliases=["b"], help="Build a package from recipe")
    p_build.add_argument("package")
    p_build.add_argument("--execute", action="store_true")
    p_build.add_argument("--recipes", default="/usr/sources")

    p_install = sub.add_parser("install", aliases=["i"], help="Alias to build")
    p_install.add_argument("package")
    p_install.add_argument("--execute", action="store_true")
    p_install.add_argument("--recipes", default="/usr/sources")

    p_rebuild = sub.add_parser("rebuild", aliases=["rb"], help="Rebuild single package")
    p_rebuild.add_argument("package")
    p_rebuild.add_argument("--execute", action="store_true")

    p_rebuild_system = sub.add_parser("rebuild-system", aliases=["rsys"], help="Rebuild whole system (all installed)")
    p_rebuild_system.add_argument("--execute", action="store_true")
    p_rebuild_system.add_argument("--db", help="Installed DB", default="/var/lib/sources/installed_db.json")
    p_rebuild_system.add_argument("--concurrency", type=int, default=4)
    p_rebuild_system.add_argument("--only", help="If provided, rebuild only this package")

    # remove
    p_remove = sub.add_parser("remove", aliases=["r"], help="Remove package")
    p_remove.add_argument("package")
    p_remove.add_argument("--execute", action="store_true")
    p_remove.add_argument("--force", action="store_true")
    p_remove.add_argument("--backup", action="store_true")
    p_remove.add_argument("--db", default="/var/lib/sources/installed_db.json")

    # upgrade
    p_upgrade = sub.add_parser("upgrade", aliases=["ug"], help="Upgrade package(s) from local recipes")
    p_upgrade.add_argument("pkg", nargs="*", help="Package(s) to upgrade (omit to upgrade all candidates)")
    p_upgrade.add_argument("--execute", action="store_true")
    p_upgrade.add_argument("--force", action="store_true")
    p_upgrade.add_argument("--concurrency", type=int, default=4)
    p_upgrade.add_argument("--conf", help="Path to source.conf")

    # update (check only)
    p_update = sub.add_parser("update", aliases=["up"], help="Check upstream versions (notify only)")
    p_update.add_argument("--execute", action="store_true")
    p_update.add_argument("--only", help="Regex include")
    p_update.add_argument("--exclude", help="Regex exclude")
    p_update.add_argument("--concurrency", type=int, default=6)
    p_update.add_argument("--timeout", type=int, default=15)
    p_update.add_argument("--basename", help="Basename for report files")
    p_update.add_argument("--conf", help="Path to source.conf")

    # auto
    p_auto = sub.add_parser("auto", aliases=["a"], help="Automated updater")
    p_auto_sub = p_auto.add_subparsers(dest="sub", required=False)
    p_auto_sub.add_parser("check", help="Check updates")
    p_auto_sub.add_parser("update-all", help="Auto update all (attempt)")

    # cache
    p_cache = sub.add_parser("cache", aliases=["cc"], help="Cache management")
    p_cache_sub = p_cache.add_subparsers(dest="sub", required=True)
    p_cache_sub.add_parser("clean", help="Clean caches")
    p_cache_sub.add_parser("deepclean", help="Full deepclean (delegates to deepclean module)")

    # deepclean
    p_dc = sub.add_parser("deepclean", aliases=["dc"], help="Deepclean orchestration")
    p_dc.add_argument("--execute", action="store_true")
    p_dc.add_argument("--purge-orphans", action="store_true")
    p_dc.add_argument("--force", action="store_true")
    p_dc.add_argument("--no-backup", action="store_true")
    p_dc.add_argument("--no-caches", action="store_true")
    p_dc.add_argument("--no-sandboxes", action="store_true")
    p_dc.add_argument("--no-tmp", action="store_true")
    p_dc.add_argument("--rebuild-db", action="store_true")
    p_dc.add_argument("--yes", action="store_true")

    # history
    p_hist = sub.add_parser("history", aliases=["h"], help="History commands")
    p_hist_sub = p_hist.add_subparsers(dest="sub", required=True)
    ph_list = p_hist_sub.add_parser("list")
    ph_list.add_argument("--limit", type=int, default=50)
    ph_list.add_argument("--action")
    ph_list.add_argument("--package")
    ph_list.add_argument("--since")
    ph_list.add_argument("--text")
    ph_show = p_hist_sub.add_parser("show")
    ph_show.add_argument("id")
    ph_export = p_hist_sub.add_parser("export")
    ph_export.add_argument("out")
    ph_export.add_argument("--fmt", choices=("json","csv"), default="json")
    ph_rollback = p_hist_sub.add_parser("rollback")
    ph_rollback.add_argument("id")
    ph_rollback.add_argument("--yes", action="store_true")

    # misc: update modules
    p_sync2 = sub.add_parser("sync-run", help="Short sync run alias (single arg)", aliases=["sy-run"])
    p_sync2.add_argument("--force", action="store_true")

    # provide top-level shortcuts via aliases mapping in help
    return ap

def main(argv: Optional[List[str]] = None):
    argv = argv or sys.argv[1:]
    # pre-parse color / quiet
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--no-color", action="store_true")
    pre.add_argument("--quiet", action="store_true")
    known, rest = pre.parse_known_args(argv)
    console = make_console(known.no_color, known.quiet)
    cli = CLI(console=console, dry_run=True, hooks_enabled=True)
    parser = build_argparser()
    args = parser.parse_args(argv)

    # set quiet on cli
    cli.quiet = args.quiet if hasattr(args, "quiet") else False

    # default behavior: dry-run unless --execute provided at subcommand level
    # pass parsed args into handlers
    cmd = args.command
    try:
        if cmd in ("sync", "sy"):
            # normalize
            return cli.cmd_sync(args)
        if cmd in ("search", "s"):
            return cli.cmd_search(args)
        if cmd in ("info", "in"):
            return cli.cmd_info(args)
        if cmd in ("build", "b"):
            return cli.cmd_build(args)
        if cmd in ("install", "i"):
            return cli.cmd_install(args)
        if cmd in ("rebuild", "rb"):
            return cli.cmd_rebuild(args)
        if cmd in ("rebuild-system", "rsys"):
            return cli.cmd_rebuild_system(args)
        if cmd in ("remove", "r"):
            return cli.cmd_remove(args)
        if cmd in ("upgrade", "ug"):
            # adapt args.pkg list into mgr API expecting list or None
            if hasattr(args, "pkg"):
                args_pkg = args.pkg if args.pkg else None
                args.pkg = args_pkg
            return cli.cmd_upgrade(args)
        if cmd in ("update", "up"):
            return cli.cmd_update(args)
        if cmd in ("auto", "a"):
            return cli.cmd_auto(args)
        if cmd in ("cache", "cc"):
            return cli.cmd_cache(args)
        if cmd in ("deepclean", "dc"):
            return cli.cmd_deepclean(args)
        if cmd in ("history", "h"):
            return cli.cmd_history(args)
        if cmd in ("sync-run", "sy-run"):
            # quick sync-run alias
            sync_args = argparse.Namespace(sub="run", force=args.force)
            return cli.cmd_sync(sync_args)
        # fallback
        console.print("[red]Unknown command[/red]")
        return 2
    except Exception as e:
        console.print(f"[red]Unhandled CLI error: {e}[/red]")
        LOG.error(traceback.format_exc())
        return 3

if __name__ == "__main__":
    raise SystemExit(main())
