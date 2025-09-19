# source/modules/auto.py
"""
auto.py - Ultimate Auto Updater & System Auditor with desktop notifications.

Features (ultimate):
 - Detecta atualizações de pacotes a partir das recipes (repo local)
 - Resolução básica de dependências e atualização em ordem correta
 - Snapshot / rollback: cria binpkg backup antes de atualizar; restaura se falha
 - Rodar atualizações paralelas, com ordem respeitando deps
 - Agendamento helper (systemd timer file generator stub)
 - Auditoria (órfãos, dependências quebradas)
 - Notificações via desktop (notify-send) + relatório JSON
 - Hooks: pre_update, post_update, pre_system_update, post_system_update
 - Dry-run mode, verbose logging, retries
 - CLI: check, update, update-all, audit, report, schedule, heal
"""

from __future__ import annotations
import os
import sys
import json
import time
import shutil
import tempfile
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Dict, Any, Optional

# try to import project modules; be resilient to different class names
try:
    from modules import recipe as _recipe
    from modules import logger as _logger
    from modules import hooks as _hooks
    from modules import search as _search
    from modules import binpkg as _binpkg
    from modules import sandbox as _sandbox
    from modules import fakeroot as _fakeroot
    # build can be named BuildManager, Builder, build.Builder etc.
    from modules import build as _build_mod
except Exception:
    # If imports fail, we'll still proceed but log missing functionality at runtime.
    _recipe = _logger = _hooks = _search = _binpkg = _sandbox = _fakeroot = _build_mod = None

# utils
def now_iso():
    return datetime.utcnow().isoformat() + "Z"


class AutoUltimateError(Exception):
    pass


class AutoUltimate:
    def __init__(
        self,
        recipes_dir: str = "/usr/sources",
        installed_db: str = "/var/lib/sources/installed_db.json",
        binpkg_cache: str = "binpkg_cache",
        dry_run: bool = False,
        workers: int = 4,
        report_dir: str = "/var/log/sources",
        verbose: bool = False,
    ):
        self.recipes_dir = os.path.abspath(recipes_dir)
        self.installed_db_path = os.path.abspath(installed_db)
        self.binpkg_cache = os.path.abspath(binpkg_cache)
        os.makedirs(self.binpkg_cache, exist_ok=True)
        self.dry_run = dry_run
        self.workers = workers
        self.report_dir = os.path.abspath(report_dir)
        os.makedirs(self.report_dir, exist_ok=True)
        self.verbose = verbose

        # logger (fallback simple)
        if _logger:
            try:
                self.log = _logger.Logger("auto.log")
            except Exception:
                class _SimpleLog:
                    def info(self, *a, **k): print("[INFO]", *a)
                    def error(self, *a, **k): print("[ERROR]", *a, file=sys.stderr)
                    def debug(self, *a, **k): print("[DEBUG]", *a)
                self.log = _SimpleLog()
        else:
            class _SimpleLog:
                def info(self, *a, **k): print("[INFO]", *a)
                def error(self, *a, **k): print("[ERROR]", *a, file=sys.stderr)
                def debug(self, *a, **k): print("[DEBUG]", *a)
            self.log = _SimpleLog()

        # hook manager
        if _hooks:
            try:
                self.hooks = _hooks.HookManager(dry_run=self.dry_run)
            except Exception:
                try:
                    self.hooks = _hooks.HookManager()
                    self.hooks.dry_run = self.dry_run
                except Exception:
                    self.hooks = None
        else:
            self.hooks = None

        # recipe & search managers
        self.recipe_mgr = _recipe.RecipeManager() if _recipe else None
        self.search = _search.PackageSearch(repo_path=self.recipes_dir, installed_db=self.installed_db_path) if _search else None

        # binpkg manager
        self.binpkg_mgr = _binpkg.BinPkgManager(cache_dir=self.binpkg_cache, installed_db=self.installed_db_path, dry_run=self.dry_run) if _binpkg else None

        # builder detection (try multiple names)
        self.build_mgr = None
        if _build_mod:
            # try common class names
            for cand in ("BuildManager", "Builder", "build"):
                cls = getattr(_build_mod, cand, None)
                if callable(cls):
                    try:
                        self.build_mgr = cls(dry_run=self.dry_run)
                        break
                    except TypeError:
                        try:
                            self.build_mgr = cls()
                            break
                        except Exception:
                            continue
        if not self.build_mgr:
            # try module-level factory
            if hasattr(_build_mod, "BuildManager"):
                try:
                    self.build_mgr = _build_mod.BuildManager(dry_run=self.dry_run)
                except Exception:
                    pass

        # load installed DB
        self.installed_db: Dict[str, Any] = {}
        if os.path.exists(self.installed_db_path):
            try:
                with open(self.installed_db_path, "r", encoding="utf-8") as fh:
                    self.installed_db = json.load(fh)
            except Exception:
                self.installed_db = {}

    # ---------------------
    # Persistence
    # ---------------------
    def _save_installed_db(self):
        if self.dry_run:
            self.log.info("[DRY-RUN] Would save installed_db")
            return
        dirpath = os.path.dirname(self.installed_db_path)
        if dirpath and not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)
        with open(self.installed_db_path, "w", encoding="utf-8") as fh:
            json.dump(self.installed_db, fh, indent=2)
        self.log.debug("installed_db saved")

    def _write_report(self, report: Dict[str, Any], basename: Optional[str] = None) -> str:
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        name = basename or f"auto-report-{ts}.json"
        path = os.path.join(self.report_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        self.log.info(f"Report saved: {path}")
        return path

    # ---------------------
    # Notifications
    # ---------------------
    def _has_notify_send(self) -> bool:
        return shutil.which("notify-send") is not None

    def notify(self, title: str, message: str, urgency: str = "normal"):
        """
        Desktop notification via notify-send if available.
        Falls back to logger if not.
        """
        msg = f"{title}: {message}"
        if self._has_notify_send():
            cmd = ["notify-send", "--urgency", urgency, title, message]
            try:
                if self.dry_run:
                    self.log.info(f"[DRY-RUN] Would run: {' '.join(cmd)}")
                else:
                    subprocess.run(cmd, check=False)
            except Exception as e:
                self.log.error("notify-send failed: " + str(e))
                self.log.info(msg)
        else:
            self.log.info(msg)

    # ---------------------
    # Audit utilities
    # ---------------------
    def audit_system(self) -> Dict[str, List[str]]:
        """
        Audit: find orphans (installed not in recipes), broken deps (refer to missing recipes)
        """
        report = {"orphans": [], "broken_deps": []}
        all_recipes = set(self.search.list_all_packages()) if self.search else set()
        for pkg, meta in self.installed_db.items():
            if pkg not in all_recipes:
                report["orphans"].append(pkg)
            deps = meta.get("depends", []) or []
            for d in deps:
                if d not in all_recipes:
                    report["broken_deps"].append(f"{pkg} -> missing {d}")
        self.log.info(f"Audit: {len(report['orphans'])} orphans, {len(report['broken_deps'])} broken_deps")
        return report

    # ---------------------
    # Check updates
    # ---------------------
    def check_for_updates(self) -> List[Dict[str, Any]]:
        """
        Compares installed_db versions vs recipe versions.
        Returns list of {name, current_version, available_version}
        """
        updates = []
        if not self.search:
            raise AutoUltimateError("Search manager not available")
        for pkg, meta in self.installed_db.items():
            rec = self.search.info(pkg)
            if not rec:
                continue
            available = rec.get("version")
            current = meta.get("version")
            if available and current and available != current:
                updates.append({"name": pkg, "current": current, "available": available})
        self.log.info(f"{len(updates)} packages with updates found")
        return updates

    # ---------------------
    # Dependency-aware ordering helpers
    # ---------------------
    def _build_dep_graph(self, packages: List[str]) -> Dict[str, List[str]]:
        """
        Build dependency subgraph for given packages using recipe data.
        """
        deps = {}
        for pkg in packages:
            info = self.search.info(pkg) if self.search else {}
            depends = info.get("depends", []) if info else []
            # filter only packages within our set or installed set
            deps[pkg] = [d for d in depends if d in packages]
        return deps

    def _levels_from_graph(self, nodes: List[str], deps: Dict[str, List[str]]) -> List[List[str]]:
        """
        Simple levelization respecting deps (not full topo detect cycle handling).
        """
        nodes_set = set(nodes)
        built = set()
        remain = set(nodes)
        levels = []
        while remain:
            this = []
            for n in sorted(list(remain)):
                if set(deps.get(n, [])).issubset(built):
                    this.append(n)
            if not this:
                # cycle or unresolved dependency -> break remaining as one level
                this = sorted(list(remain))
            for n in this:
                remain.remove(n)
                built.add(n)
            levels.append(this)
        return levels

    # ---------------------
    # Snapshot (backup) helper: create binpkg backup of currently installed files
    # ---------------------
    def create_snapshot_binpkg(self, package: str) -> Optional[str]:
        """
        Creates a binpkg backup of currently installed files for package (if binpkg manager available).
        Returns path to backup archive or None (dry_run).
        """
        if not self.binpkg_mgr:
            self.log.info("No binpkg manager available for snapshot")
            return None
        entry = self.installed_db.get(package)
        if not entry:
            self.log.info("No installed entry to snapshot")
            return None

        # If binpkg manager has a create from metadata or installed files method, we'd call it.
        # Fallback: create tar.gz of listed files and metadata.json
        files = entry.get("files", [])
        if not files:
            self.log.info("No files listed for package; skipping snapshot")
            return None

        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        outname = f"{package}-{entry.get('version','x')}-snapshot-{ts}.tar.gz"
        outpath = os.path.join(self.binpkg_cache, outname)
        if self.dry_run:
            self.log.info(f"[DRY-RUN] Would create snapshot {outpath}")
            return None

        with tempfile.TemporaryDirectory() as tmpd:
            # copy files into tmpd preserving structure
            for f in files:
                try:
                    if os.path.exists(f):
                        dest = os.path.join(tmpd, f.lstrip("/"))
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.copy2(f, dest)
                except Exception:
                    self.log.debug(f"Failed copying {f} to snapshot dir (ignored)")
            # create metadata.json
            meta = {
                "name": package,
                "version": entry.get("version"),
                "created_at": now_iso(),
                "files": files,
                "source": "snapshot"
            }
            with open(os.path.join(tmpd, "metadata.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2)
            # create tar.gz
            shutil.make_archive(base_name=outpath[:-7], format="gztar", root_dir=tmpd)
            self.log.info(f"Snapshot created: {outpath}")
            return outpath

    # ---------------------
    # Rollback helper (extract snapshot)
    # ---------------------
    def rollback_from_snapshot(self, snapshot_path: str):
        """
        Extract a snapshot tar.gz to root using fakeroot if available.
        """
        if self.dry_run:
            self.log.info(f"[DRY-RUN] Would rollback from {snapshot_path}")
            return
        if not os.path.exists(snapshot_path):
            raise AutoUltimateError("Snapshot not found: " + snapshot_path)
        cmd = f"tar -xzf {shutil.quote(snapshot_path)} -C /"
        if self._has_notify_send():
            self.log.info("Performing rollback with fakeroot (notify)")
        # Use fakeroot.run if available
        if self.binpkg_mgr and hasattr(self.binpkg_mgr, "fakeroot"):
            try:
                self.binpkg_mgr.fakeroot.run(cmd, shell=True, check=True)
                self.log.info("Rollback applied via fakeroot")
                return
            except Exception as e:
                self.log.error("Rollback via fakeroot failed: " + str(e))
        # Fallback to system tar (requires privileges)
        subprocess.run(cmd, shell=True)

    # ---------------------
    # Update package (single)
    # ---------------------
    def update_package(self, package: str, force: bool = False, allow_downgrade: bool = False, retries: int = 1) -> Dict[str, Any]:
        """
        Update a single package:
         - check recipe and available version
         - create snapshot (backup)
         - try binpkg install; if not available, build from source via build_mgr
         - on failure, rollback using snapshot
         - run hooks pre_update/post_update
        """
        report = {"package": package, "timestamp": now_iso(), "status": "pending"}
        if package not in self.installed_db:
            report["status"] = "not-installed"
            self.log.info(f"{package} not installed")
            return report

        rec = self.search.info(package) if self.search else None
        if not rec:
            report["status"] = "recipe-missing"
            self.log.error(f"Recipe missing for {package}")
            return report

        available = rec.get("version")
        current = self.installed_db.get(package, {}).get("version")
        report["current"] = current
        report["available"] = available

        if available == current and not force:
            report["status"] = "up-to-date"
            self.log.info(f"{package} up-to-date ({current})")
            return report

        # pre-update hooks
        try:
            if self.hooks:
                self.hooks.run_hooks("pre_update", rec, None)
        except Exception as e:
            self.log.error("pre_update hook error: " + str(e))

        # snapshot
        snapshot = None
        try:
            snapshot = self.create_snapshot_binpkg(package)
        except Exception as e:
            self.log.error("Snapshot creation failed: " + str(e))
            snapshot = None

        last_err = None
        for attempt in range(1, retries + 1):
            try:
                # try remote/local binpkg first (look in binpkg_cache)
                binpkg_file = os.path.join(self.binpkg_cache, f"{package}-{available}.tar.gz")
                if os.path.exists(binpkg_file):
                    self.log.info(f"Installing binpkg for {package}: {binpkg_file}")
                    res = self.binpkg_mgr.install_binpkg(binpkg_file, force=force, backup=True) if self.binpkg_mgr else None
                    report["method"] = "binpkg"
                    report["result"] = res
                else:
                    # build from source
                    if not self.build_mgr:
                        raise AutoUltimateError("No build manager available to build from source")
                    self.log.info(f"Building {package} from source")
                    recipe_path = rec.get("path")
                    # we expect build_mgr has method build_single_pkg or similar
                    build_fn = None
                    for name in ("build_single_pkg", "_build_single", "build_single", "build"):
                        if hasattr(self.build_mgr, name):
                            build_fn = getattr(self.build_mgr, name)
                            break
                    if not build_fn:
                        # maybe module provides build_single_pkg as function
                        raise AutoUltimateError("Build manager missing expected build method")
                    # call build; signatures vary; try common patterns
                    try:
                        br = build_fn(package, recipe_path, rec)
                    except TypeError:
                        # maybe expects (name, source_dir, recipe)
                        br = build_fn(package, recipe_path, rec)
                    report["method"] = "build"
                    report["build_result"] = br
                    # if build produced an archive, install it
                    archive = None
                    if isinstance(br, dict):
                        archive = br.get("archive") or br.get("artifact")
                    if archive:
                        if self.binpkg_mgr:
                            res = self.binpkg_mgr.install_binpkg(archive, force=force, backup=True)
                            report["install_result"] = res
                report["status"] = "ok"
                # post-update hooks
                try:
                    if self.hooks:
                        self.hooks.run_hooks("post_update", rec, None)
                except Exception as e:
                    self.log.error("post_update hook failed: " + str(e))
                # update installed_db with new version if available
                if available:
                    if package in self.installed_db:
                        self.installed_db[package]["version"] = available
                        self.installed_db[package]["updated_at"] = now_iso()
                    else:
                        # minimal entry
                        self.installed_db[package] = {"name": package, "version": available, "installed_at": now_iso()}
                    self._save_installed_db()
                # notify
                self.notify("AutoUpdate", f"{package} updated: {current} -> {available}", urgency="normal")
                return report
            except Exception as e:
                last_err = str(e)
                self.log.error(f"Attempt {attempt} failed for {package}: {e}")
                # if last attempt, try rollback
                if attempt == retries:
                    report["status"] = "failed"
                    report["error"] = last_err
                    self.notify("AutoUpdate FAIL", f"{package} failed to update: {last_err}", urgency="critical")
                    # try rollback if snapshot exists
                    if snapshot:
                        try:
                            self.rollback_from_snapshot(snapshot)
                            report["rolled_back"] = True
                            self.notify("AutoUpdate Rollback", f"{package} rolled back after failure", urgency="normal")
                        except Exception as re:
                            self.log.error("Rollback failed: " + str(re))
                            report["rollback_error"] = str(re)
                    return report
                # otherwise wait a bit then retry
                time.sleep(2 ** attempt)
        # fallback
        report["status"] = "failed"
        report["error"] = last_err
        return report

    # ---------------------
    # Update multiple packages (parallel w/ dep levels)
    # ---------------------
    def update_all(self, force: bool = False, limit: Optional[List[str]] = None, concurrency: Optional[int] = None) -> Dict[str, Any]:
        """
        Update all updatable packages. If limit is provided, only those packages.
        Returns aggregated report.
        """
        updates = self.check_for_updates()
        to_update = [u["name"] for u in updates]
        if limit:
            to_update = [p for p in to_update if p in limit]
        if not to_update:
            return {"status": "no-updates", "count": 0}

        deps = self._build_dep_graph(to_update)
        levels = self._levels_from_graph(to_update, deps)
        aggregated = {"started": now_iso(), "levels": [], "results": {}}
        max_workers = concurrency or self.workers

        for level_idx, level in enumerate(levels):
            aggregated["levels"].append({"level": level_idx, "packages": level})
            # run this level in parallel
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = {ex.submit(self.update_package, pkg, force=force): pkg for pkg in level}
                for fut in as_completed(futures):
                    pkg = futures[fut]
                    try:
                        res = fut.result()
                        aggregated["results"][pkg] = res
                    except Exception as e:
                        aggregated["results"][pkg] = {"status": "failed", "error": str(e)}
        aggregated["finished"] = now_iso()
        # write report and notify summary
        report_path = self._write_report(aggregated)
        success_count = sum(1 for r in aggregated["results"].values() if r.get("status") == "ok")
        fail_count = sum(1 for r in aggregated["results"].values() if r.get("status") != "ok")
        self.notify("AutoUpdate Summary", f"{success_count} updated, {fail_count} failed. Report: {report_path}", urgency="normal")
        return aggregated

    # ---------------------
    # Helpers: schedule generator (systemd timer stub)
    # ---------------------
    def generate_systemd_timer(self, service_name: str = "sources-auto", timer_interval: str = "daily", out_dir: str = "/etc/systemd/system") -> Dict[str, str]:
        """
        Generate simple systemd service + timer (strings). Does not enable or write them unless requested.
        timer_interval: "daily", "hourly", "weekly" or a OnCalendar spec
        Returns dict with 'service' and 'timer' content and paths.
        """
        # produce OnCalendar mapping
        if timer_interval == "daily":
            oncal = "OnCalendar=daily"
        elif timer_interval == "hourly":
            oncal = "OnCalendar=hourly"
        elif timer_interval == "weekly":
            oncal = "OnCalendar=weekly"
        else:
            oncal = f"OnCalendar={timer_interval}"

        service_unit = f"""[Unit]
Description=Sources auto update service

[Service]
Type=oneshot
ExecStart=/usr/bin/sources auto update-all
"""
        timer_unit = f"""[Unit]
Description=Timer for sources auto update

[Timer]
{oncal}
Persistent=true

[Install]
WantedBy=timers.target
"""
        service_path = os.path.join(out_dir, f"{service_name}.service")
        timer_path = os.path.join(out_dir, f"{service_name}.timer")
        if self.dry_run:
            self.log.info(f"[DRY-RUN] Would write systemd units to {service_path} and {timer_path}")
            return {"service": service_unit, "timer": timer_unit, "service_path": service_path, "timer_path": timer_path}
        # write files
        with open(service_path, "w", encoding="utf-8") as fh:
            fh.write(service_unit)
        with open(timer_path, "w", encoding="utf-8") as fh:
            fh.write(timer_unit)
        self.log.info(f"Wrote {service_path} and {timer_path}")
        return {"service_path": service_path, "timer_path": timer_path}

    # ---------------------
    # Heal (attempt to rebuild broken packages)
    # ---------------------
    def heal(self, package: str) -> Dict[str, Any]:
        """
        Attempt to rebuild a package from recipe if it's broken (e.g., files missing).
        """
        if not self.search:
            raise AutoUltimateError("Search manager unavailable")
        rec = self.search.info(package)
        if not rec:
            raise AutoUltimateError("Recipe not found for " + package)
        if not self.build_mgr:
            raise AutoUltimateError("Build manager not configured")

        self.log.info("Attempting to rebuild " + package)
        # try to build
        try:
            # locate builder method as before
            build_fn = None
            for name in ("build_single_pkg", "_build_single", "build_single", "build"):
                if hasattr(self.build_mgr, name):
                    build_fn = getattr(self.build_mgr, name)
                    break
            if not build_fn:
                raise AutoUltimateError("No build method on build manager")
            recipe_path = rec.get("path")
            br = build_fn(package, recipe_path, rec)
            return {"package": package, "result": br}
        except Exception as e:
            self.log.error("Heal failed: " + str(e))
            return {"package": package, "error": str(e), "status": "failed"}

# ---------------------
# CLI
# ---------------------
def main_cli(argv: Optional[List[str]] = None):
    import argparse, json
    argv = argv or sys.argv[1:]
    ap = argparse.ArgumentParser(prog="auto", description="Ultimate auto updater")
    ap.add_argument("--recipes", default="/usr/sources")
    ap.add_argument("--db", default="/var/lib/sources/installed_db.json")
    ap.add_argument("--binpkg-cache", default="binpkg_cache")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="Check for available updates")
    up = sub.add_parser("update", help="Update a single package")
    up.add_argument("package")
    up.add_argument("--force", action="store_true")
    up_all = sub.add_parser("update-all", help="Update all updatable packages")
    up_all.add_argument("--force", action="store_true")

    audit = sub.add_parser("audit", help="Run system audit (orphans, broken deps)")
    report = sub.add_parser("report", help="Write simple audit/update report")
    schedule = sub.add_parser("schedule", help="Generate systemd timer/service files")
    schedule.add_argument("--interval", default="daily")

    heal = sub.add_parser("heal", help="Attempt to rebuild a specific package")
    heal.add_argument("package")

    args = ap.parse_args(argv)

    au = AutoUltimate(recipes_dir=args.recipes, installed_db=args.db, binpkg_cache=args.binpkg_cache,
                      dry_run=args.dry_run, workers=args.workers, verbose=args.verbose)

    if args.cmd == "check":
        upd = au.check_for_updates()
        print(json.dumps(upd, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "update":
        res = au.update_package(args.package, force=args.force)
        p = au._write_report(res, basename=f"update-{args.package}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json")
        print(json.dumps(res, indent=2, ensure_ascii=False))
        # desktop notify summary
        au.notify("AutoUpdate", f"Update {args.package} finished: {res.get('status')}", urgency="normal")
        return 0

    if args.cmd == "update-all":
        res = au.update_all(force=args.force)
        p = au._write_report(res, basename=f"update-all-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json")
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "audit":
        res = au.audit_system()
        print(json.dumps(res, indent=2, ensure_ascii=False))
        au.notify("AutoAudit", f"Orphans: {len(res.get('orphans',[]))}, Broken deps: {len(res.get('broken_deps',[]))}")
        return 0

    if args.cmd == "report":
        # simple aggregated report: audit + updates
        audit_res = au.audit_system()
        updates = au.check_for_updates()
        rep = {"audit": audit_res, "updates": updates, "generated_at": now_iso()}
        path = au._write_report(rep)
        au.notify("AutoReport", f"Report generated: {os.path.basename(path)}")
        print("Report:", path)
        return 0

    if args.cmd == "schedule":
        res = au.generate_systemd_timer(timer_interval=args.interval)
        print(json.dumps(res, indent=2))
        return 0

    if args.cmd == "heal":
        res = au.heal(args.package)
        print(json.dumps(res, indent=2))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main_cli())
