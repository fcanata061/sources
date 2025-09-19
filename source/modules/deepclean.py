# source/modules/deepclean.py
"""
deepclean.py - limpeza profunda e manutenção do repositório / sistema.

Uso (exemplos):
  python deepclean.py --dry-run --report report.json
  python deepclean.py --execute --purge-orphans --backup-before --yes

Funcionalidades:
 - limpeza de caches (distfiles/binpkgs/metadata) via CacheManager
 - remoção de pacotes órfãos (consulta installed_db e recipes)
 - purge-orphans: usa Remover para desinstalar órfãos (force opcional)
 - limpar sandboxes e temp directories
 - rebuild-db: tentativa de reparar installed_db com base em reports
 - snapshots/backup antes de remoções
 - dry-run/execute modes, relatório JSON, desktop notify (notify-send)
 - hooks: pre_deepclean / post_deepclean
"""
from __future__ import annotations
import os
import sys
import json
import shutil
import tempfile
import tarfile
import time
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional

# Tentativa de importar módulos do projeto; tolerante a ausências
try:
    from modules import logger as _logger
except Exception:
    _logger = None
try:
    from modules import cache as _cache
except Exception:
    _cache = None
try:
    from modules import remove as _remove
except Exception:
    _remove = None
try:
    from modules import sandbox as _sandbox
except Exception:
    _sandbox = None
try:
    from modules import hooks as _hooks
except Exception:
    _hooks = None
try:
    from modules import search as _search
except Exception:
    _search = None
try:
    from modules import recipe as _recipe
except Exception:
    _recipe = None

# Simple fallback logger if modules.logger is not available
class _FallbackLog:
    def info(self, *a, **k): print("[INFO]", *a)
    def error(self, *a, **k): print("[ERROR]", *a, file=sys.stderr)
    def debug(self, *a, **k): print("[DEBUG]", *a)

LOG = _logger.Logger("deepclean.log") if _logger and hasattr(_logger, "Logger") else _FallbackLog()

def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def has_notify_send() -> bool:
    return shutil.which("notify-send") is not None

def notify(title: str, message: str, dry_run: bool):
    if dry_run:
        LOG.info(f"[DRY-RUN] notify: {title} - {message}")
        return
    if has_notify_send():
        try:
            import subprocess
            subprocess.run(["notify-send", title, message], check=False)
        except Exception as e:
            LOG.error("notify-send failed: " + str(e))
    else:
        LOG.info(f"{title}: {message}")

class DeepClean:
    def __init__(self,
                 installed_db_path: str = "/var/lib/sources/installed_db.json",
                 recipes_dir: str = "/usr/sources",
                 report_dir: str = "/var/log/sources",
                 backups_dir: str = "/var/backups/sources",
                 dry_run: bool = True):
        self.installed_db_path = os.path.abspath(installed_db_path)
        self.recipes_dir = os.path.abspath(recipes_dir)
        self.report_dir = os.path.abspath(report_dir)
        self.backups_dir = os.path.abspath(backups_dir)
        self.dry_run = dry_run

        os.makedirs(self.report_dir, exist_ok=True)
        os.makedirs(self.backups_dir, exist_ok=True)

        self.log = LOG

        # instantiate helpers if available
        self.cache_mgr = _cache.CacheManager(dry_run=dry_run) if _cache else None
        # Remover expects (installed_db, dry_run), try to construct
        self.remover = None
        if _remove:
            try:
                self.remover = _remove.Remover(installed_db=self.installed_db_path, dry_run=self.dry_run)
            except Exception:
                try:
                    self.remover = _remove.Remover(installed_db=self.installed_db_path)
                    self.remover.dry_run = self.dry_run
                except Exception:
                    self.remover = None

        self.sandbox_mgr = _sandbox.Sandbox if _sandbox else None
        self.hooks = (_hooks.HookManager(dry_run=dry_run) if _hooks else None)

        # load installed_db (if exists)
        self.installed_db = {}
        if os.path.exists(self.installed_db_path):
            try:
                with open(self.installed_db_path, "r", encoding="utf-8") as fh:
                    self.installed_db = json.load(fh)
            except Exception as e:
                self.log.error(f"Failed to read installed_db: {e}")
                self.installed_db = {}

        # For search of recipes
        self.search = _search.PackageSearch(repo_path=self.recipes_dir, installed_db=self.installed_db_path) if _search else None

    # ------------------
    # Helpers
    # ------------------
    def _save_installed_db(self):
        if self.dry_run:
            self.log.info("[DRY-RUN] would save installed_db")
            return
        dirpath = os.path.dirname(self.installed_db_path)
        if dirpath and not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)
        with open(self.installed_db_path, "w", encoding="utf-8") as fh:
            json.dump(self.installed_db, fh, indent=2)
        self.log.info(f"installed_db saved to {self.installed_db_path}")

    def _write_report(self, report: Dict[str, Any], name: Optional[str] = None) -> str:
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        fname = name or f"deepclean-report-{ts}.json"
        out = os.path.join(self.report_dir, fname)
        if self.dry_run:
            self.log.info(f"[DRY-RUN] would write report to {out}")
            return out
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        self.log.info(f"Report written to {out}")
        return out

    def _confirm(self, prompt: str, assume_yes: bool) -> bool:
        if assume_yes:
            return True
        try:
            ans = input(prompt + " [y/N]: ").strip().lower()
            return ans in ("y", "yes")
        except Exception:
            return False

    # ------------------
    # Scans
    # ------------------
    def scan_caches(self) -> Dict[str, Any]:
        """
        Returna resumo dos caches e arquivos que seriam removidos.
        """
        summary = {"caches": {}}
        if not self.cache_mgr:
            self.log.info("CacheManager not available; skipping cache scan")
            return summary
        for ctype, conf in (self.cache_mgr.cache_types.items() if hasattr(self.cache_mgr, "cache_types") else []):
            path = conf.get("path")
            files = []
            if os.path.isdir(path):
                for root, _, fnames in os.walk(path):
                    for fn in fnames:
                        fp = os.path.join(root, fn)
                        try:
                            st = os.stat(fp)
                            files.append({"path": fp, "size": st.st_size, "mtime": st.st_mtime})
                        except Exception:
                            continue
            summary["caches"][ctype] = {"path": path, "count": len(files), "files": files}
        return summary

    def find_orphans(self) -> List[str]:
        """
        Detecta pacotes instalados que não tem recipe correspondente (órfãos).
        Retorna lista de package names.
        """
        orphans = []
        if not self.installed_db:
            self.log.info("installed_db empty or missing")
            return orphans
        # If search (index) available use it
        all_recipes = set(self.search.list_all_packages()) if self.search else set()
        for pkg in list(self.installed_db.keys()):
            if pkg not in all_recipes:
                orphans.append(pkg)
        self.log.info(f"Found {len(orphans)} orphan packages")
        return orphans

    def scan_sandboxes(self, base_dirs: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Lista sandboxes e build dirs que podem ser removed.
        base_dirs: lista de diretórios onde sandboxes are created (defaults: ./sandbox, /tmp/sources-*)
        """
        candidates = []
        if base_dirs is None:
            base_dirs = [os.path.abspath("sandbox"), tempfile.gettempdir()]
        for bd in base_dirs:
            if not os.path.isdir(bd):
                continue
            for name in os.listdir(bd):
                path = os.path.join(bd, name)
                # Heurística: folder names like sandbox-*, sources-*, build-*
                if os.path.isdir(path) and ("sandbox" in name or "build" in name or "sources" in name):
                    try:
                        st = os.stat(path)
                        candidates.append({"path": path, "mtime": st.st_mtime})
                    except Exception:
                        continue
        self.log.info(f"Found {len(candidates)} sandbox/build candidates")
        return {"candidates": candidates}

    def scan_tmp(self, patterns: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Lista arquivos temporários relacionados ao projeto.
        patterns: lista de globs to match under /tmp
        """
        if patterns is None:
            patterns = ["sources-*", "tmp.*sources*", "binpkg-install-*", "build-*"]
        tmp = tempfile.gettempdir()
        found = []
        for fn in os.listdir(tmp):
            for pat in patterns:
                if fn.startswith(pat.replace("*", "")) or fn == pat:
                    found.append(os.path.join(tmp, fn))
        self.log.info(f"Found {len(found)} tmp candidates")
        return {"tmp": found}

    # ------------------
    # Actions
    # ------------------
    def backup_paths(self, paths: List[str], name_prefix: Optional[str] = None) -> Optional[str]:
        """
        Cria um tar.gz com os paths (somente arquivos existentes). Retorna path do backup ou None se dry-run.
        """
        if not paths:
            return None
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        prefix = name_prefix or "deepclean-backup"
        outname = f"{prefix}-{ts}.tar.gz"
        outpath = os.path.join(self.backups_dir, outname)
        if self.dry_run:
            self.log.info(f"[DRY-RUN] Would create backup {outpath} for {len(paths)} items")
            return None
        with tarfile.open(outpath, "w:gz") as tf:
            for p in paths:
                if os.path.exists(p):
                    try:
                        tf.add(p, arcname=os.path.relpath(p, "/"))
                    except Exception as e:
                        self.log.error(f"Failed to add {p} to backup: {e}")
        self.log.info(f"Backup created at {outpath}")
        return outpath

    def clean_caches(self, execute: bool = False) -> Dict[str, Any]:
        """
        Limpa caches usando CacheManager policies.
        execute=True fará ações (se dry_run=False). Se cache_mgr missing, tenta apagar standard cache dirs.
        Retorna relatório.
        """
        rep = {"caches": {}, "executed": execute and (not self.dry_run)}
        if self.cache_mgr:
            # iterate config and call clean_type with keep_recent=False to perform aggressive cleanup
            for ctype in getattr(self.cache_mgr, "cache_types", {}).keys():
                try:
                    if self.dry_run or not execute:
                        self.log.info(f"[DRY-RUN] Would clean cache type: {ctype}")
                        rep["caches"][ctype] = {"action": "dry-run"}
                    else:
                        self.cache_mgr.clean_type(ctype, keep_recent=False)
                        rep["caches"][ctype] = {"action": "cleaned"}
                except Exception as e:
                    rep["caches"][ctype] = {"error": str(e)}
                    self.log.error(f"Error cleaning cache {ctype}: {e}")
        else:
            # fallback: common paths
            default_paths = ["/var/cache/source/distfiles", "/var/cache/source/binpkgs", "/var/cache/source/metadata"]
            for p in default_paths:
                if os.path.exists(p):
                    if self.dry_run or not execute:
                        self.log.info(f"[DRY-RUN] Would remove contents of {p}")
                        rep["caches"][p] = {"action": "dry-run"}
                    else:
                        try:
                            for root, dirs, files in os.walk(p):
                                for fn in files:
                                    fp = os.path.join(root, fn)
                                    try:
                                        os.remove(fp)
                                    except Exception:
                                        pass
                            rep["caches"][p] = {"action": "cleaned"}
                            self.log.info(f"Cleaned {p}")
                        except Exception as e:
                            rep["caches"][p] = {"error": str(e)}
        return rep

    def purge_orphans(self, orphans: List[str], execute: bool = False, force: bool = False, backup_before: bool = True) -> Dict[str, Any]:
        """
        Remove pacotes órfãos.
        If remover present, uses remover.remove_package for each orphan (preferred).
        Otherwise tries to remove listed files from installed_db entries.
        """
        rep = {"orphans": {}, "executed": execute and (not self.dry_run)}
        if not orphans:
            return rep

        if backup_before and execute and not self.dry_run:
            # build backup of all orphan files
            paths = []
            for pkg in orphans:
                entry = self.installed_db.get(pkg, {})
                paths.extend(entry.get("files", []))
            bkp = self.backup_paths(paths, name_prefix="deepclean-orphans")
            rep["backup"] = bkp

        for pkg in orphans:
            if self.remover:
                try:
                    if self.dry_run or not execute:
                        self.log.info(f"[DRY-RUN] Would call remover.remove_package({pkg}, force={force})")
                        rep["orphans"][pkg] = {"action": "dry-run"}
                    else:
                        res = self.remover.remove_package(pkg, force=force, backup=backup_before)
                        rep["orphans"][pkg] = {"removed": True, "result": res}
                except Exception as e:
                    rep["orphans"][pkg] = {"error": str(e)}
                    self.log.error(f"Failed to remove orphan {pkg}: {e}")
            else:
                # fallback: remove files listed in installed_db entry
                entry = self.installed_db.get(pkg, {})
                files = entry.get("files", [])
                removed = []
                errs = []
                for f in files:
                    try:
                        if self.dry_run or not execute:
                            self.log.info(f"[DRY-RUN] Would remove file {f}")
                        else:
                            if os.path.exists(f):
                                os.remove(f)
                            removed.append(f)
                    except Exception as e:
                        errs.append(str(e))
                # remove DB entry
                if not (self.dry_run or not execute):
                    try:
                        if pkg in self.installed_db:
                            del self.installed_db[pkg]
                            self._save_installed_db()
                    except Exception as e:
                        errs.append(str(e))
                rep["orphans"][pkg] = {"removed_files": removed, "errors": errs}
        return rep

    def clean_sandboxes(self, execute: bool = False, dirs: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Remove sandboxes/build directories candidates.
        """
        rep = {"sandboxes": [], "executed": execute and (not self.dry_run)}
        scan = self.scan_sandboxes(dirs)
        for c in scan.get("candidates", []):
            p = c["path"]
            if self.dry_run or not execute:
                self.log.info(f"[DRY-RUN] Would remove sandbox {p}")
                rep["sandboxes"].append({"path": p, "action": "dry-run"})
            else:
                try:
                    shutil.rmtree(p)
                    self.log.info(f"Removed sandbox {p}")
                    rep["sandboxes"].append({"path": p, "action": "removed"})
                except Exception as e:
                    self.log.error(f"Failed to remove sandbox {p}: {e}")
                    rep["sandboxes"].append({"path": p, "error": str(e)})
        return rep

    def clean_tmp(self, execute: bool = False, patterns: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Remove /tmp items matched by patterns returned in scan_tmp.
        """
        rep = {"tmp": [], "executed": execute and (not self.dry_run)}
        scan = self.scan_tmp(patterns.get("patterns") if isinstance(patterns, dict) else None) if patterns else self.scan_tmp()
        for p in scan.get("tmp", []):
            if self.dry_run or not execute:
                self.log.info(f"[DRY-RUN] Would remove tmp {p}")
                rep["tmp"].append({"path": p, "action": "dry-run"})
            else:
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                    rep["tmp"].append({"path": p, "action": "removed"})
                except Exception as e:
                    rep["tmp"].append({"path": p, "error": str(e)})
        return rep

    def rebuild_db(self, execute: bool = False) -> Dict[str, Any]:
        """
        Tenta reparar o installed_db: verifica entradas existentes, remove entradas com arquivos inexistentes,
        e opcionalmente reconstrói baseando-se em manifests das recipes (se manifests apontarem para files present).
        """
        rep = {"checked": 0, "removed_entries": [], "fixed_entries": []}
        for pkg, entry in list(self.installed_db.items()):
            rep["checked"] += 1
            files = entry.get("files", [])
            exists_any = any(os.path.exists(f) for f in files)
            if not exists_any:
                # entry seems invalid; mark for removal
                rep["removed_entries"].append(pkg)
                if execute and not self.dry_run:
                    try:
                        del self.installed_db[pkg]
                    except Exception as e:
                        self.log.error(f"Failed removing DB entry {pkg}: {e}")
            else:
                # attempt to fix file list by scanning files that match recipe manifest if available
                if self.search:
                    rec = self.search.info(pkg)
                    if rec:
                        manifest = rec.get("manifest_files", []) or []
                        fixed = []
                        for rel in manifest:
                            cand = os.path.join(rec.get("path", ""), rel)
                            if os.path.exists(cand):
                                fixed.append(cand)
                        if fixed and execute and not self.dry_run:
                            self.installed_db[pkg]["files"] = fixed
                            rep["fixed_entries"].append(pkg)
        if execute and not self.dry_run:
            self._save_installed_db()
        return rep

    # ------------------
    # Top-level orchestration
    # ------------------
    def run(self,
            execute: bool = False,
            purge_orphans_flag: bool = False,
            purge_force: bool = False,
            backup_before: bool = True,
            clean_caches_flag: bool = True,
            clean_sandboxes_flag: bool = True,
            clean_tmp_flag: bool = True,
            rebuild_db_flag: bool = False,
            assume_yes: bool = False) -> Dict[str, Any]:
        """
        Orquestra deepclean. Retorna relatório dict.
        """
        report = {
            "started_at": now_iso(),
            "dry_run": self.dry_run,
            "actions": {}
        }

        # hooks pre_deepclean
        if self.hooks:
            try:
                self.hooks.run_hooks("pre_deepclean", {}, None)
            except Exception as e:
                self.log.error("pre_deepclean hook failed: " + str(e))

        # scan caches
        report["actions"]["scan_caches"] = self.scan_caches()

        # identify orphans
        orphans = self.find_orphans()
        report["actions"]["orphans"] = {"count": len(orphans), "packages": orphans}

        # confirm if executing and orphans present
        if execute and orphans and not assume_yes:
            ok = self._confirm(f"About to purge {len(orphans)} orphan packages. Continue?", assume_yes)
            if not ok:
                self.log.info("User aborted orphan purge")
                purge_orphans_flag = False

        # backup before destructive actions
        backup_path = None
        if backup_before and execute and (not self.dry_run):
            # gather candidate paths: cache files & orphan installed files & sandbox tmp
            cand_paths = []
            # caches: add top-level cache paths
            if self.cache_mgr:
                for ctype, conf in getattr(self.cache_mgr, "cache_types", {}).items():
                    cand_paths.append(conf.get("path"))
            # orphan files
            for pkg in orphans:
                entry = self.installed_db.get(pkg, {})
                cand_paths.extend(entry.get("files", []))
            # sandboxes
            sand = self.scan_sandboxes().get("candidates", [])
            cand_paths.extend([c["path"] for c in sand])
            # tmp
            cand_paths.extend(self.scan_tmp().get("tmp", []))
            self.log.info(f"Creating backup for {len(cand_paths)} paths before deepclean")
            backup_path = self.backup_paths(cand_paths, name_prefix="deepclean-full")
            report["actions"]["backup"] = backup_path

        # caches cleanup
        if clean_caches_flag:
            report["actions"]["clean_caches"] = self.clean_caches(execute=execute)

        # purge orphans
        if purge_orphans_flag:
            report["actions"]["purge_orphans"] = self.purge_orphans(orphans, execute=execute, force=purge_force, backup_before=backup_before)

        # clean sandboxes
        if clean_sandboxes_flag:
            report["actions"]["clean_sandboxes"] = self.clean_sandboxes(execute=execute)

        # clean tmp
        if clean_tmp_flag:
            report["actions"]["clean_tmp"] = self.clean_tmp(execute=execute)

        # rebuild db if requested
        if rebuild_db_flag:
            report["actions"]["rebuild_db"] = self.rebuild_db(execute=execute)

        # hooks post_deepclean
        if self.hooks:
            try:
                self.hooks.run_hooks("post_deepclean", report, None)
            except Exception as e:
                self.log.error("post_deepclean hook failed: " + str(e))

        report["finished_at"] = now_iso()
        # write report and notify
        report_path = self._write_report(report)
        notify("DeepClean finished", f"Report: {os.path.basename(report_path)}", dry_run=self.dry_run)
        return report

# ------------------
# CLI
# ------------------
def main(argv=None):
    import argparse
    argv = argv or sys.argv[1:]
    ap = argparse.ArgumentParser(prog="deepclean", description="Deep cleaning and maintenance for sources")
    ap.add_argument("--db", default="/var/lib/sources/installed_db.json", help="Path to installed_db.json")
    ap.add_argument("--recipes", default="/usr/sources", help="Recipes base dir")
    ap.add_argument("--report-dir", default="/var/log/sources", help="Where to write reports")
    ap.add_argument("--backups-dir", default="/var/backups/sources", help="Where to write backups")
    ap.add_argument("--dry-run", action="store_true", default=True, help="Do not execute destructive actions (default)")
    ap.add_argument("--execute", action="store_true", help="Actually perform destructive actions")
    ap.add_argument("--purge-orphans", action="store_true", help="Remove orphan packages")
    ap.add_argument("--force", action="store_true", help="Force removal of orphans (pass to remover)")
    ap.add_argument("--no-backup", action="store_true", help="Skip automatic backup before operations")
    ap.add_argument("--no-caches", action="store_true", help="Do not clean caches")
    ap.add_argument("--no-sandboxes", action="store_true", help="Do not clean sandboxes")
    ap.add_argument("--no-tmp", action="store_true", help="Do not clean temporary files")
    ap.add_argument("--rebuild-db", action="store_true", help="Attempt to rebuild installed_db entries")
    ap.add_argument("--yes", action="store_true", help="Assume yes for confirmations")
    ap.add_argument("--report", help="Write report to specific filename")
    args = ap.parse_args(argv)

    dry_run = args.dry_run and (not args.execute)
    cleaner = DeepClean(installed_db_path=args.db, recipes_dir=args.recipes,
                        report_dir=args.report_dir, backups_dir=args.backups_dir,
                        dry_run=dry_run)

    report = cleaner.run(
        execute=args.execute,
        purge_orphans_flag=args.purge_orphans,
        purge_force=args.force,
        backup_before=(not args.no_backup),
        clean_caches_flag=(not args.no_caches),
        clean_sandboxes_flag=(not args.no_sandboxes),
        clean_tmp_flag=(not args.no_tmp),
        rebuild_db_flag=args.rebuild_db,
        assume_yes=args.yes
    )

    # optionally write to requested report filename
    if args.report:
        cleaner._write_report(report, name=args.report)

    print(json.dumps(report, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
