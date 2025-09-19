# source/modules/upgrade.py
"""
upgrade.py - atualizador local que usa apenas o repo em /usr/sources (ou configurado)
Respeita dependências entre recipes; tenta instalar via binpkg ou build; suporta dry-run.

Principais behaviors:
 - Lê installed_db.json para saber pacotes instalados.
 - Varre /usr/sources/<pkg>/recipe.yaml (ou recipe.json) para obter versão e depends.
 - Compara versões; marca pacotes com recipe.version > installed.version para upgrade.
 - Resolve dependências: garante que dependências sejam atualizadas/instaladas antes.
 - Para cada pacote a atualizar: executa hooks, tenta instalar binpkg se disponível, senão build -> install.
 - Modo dry-run (default) apenas reporta ações sem tocar o sistema.
 - Gera relatório JSON e envia notify-send ao final se --execute.
"""

from __future__ import annotations
import os
import sys
import json
import re
import shutil
import subprocess
import configparser
import tempfile
import time
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional, Set
from concurrent.futures import ThreadPoolExecutor, as_completed

# optional project modules
try:
    from modules import recipe as _recipe
except Exception:
    _recipe = None
try:
    from modules import binpkg as _binpkg
except Exception:
    _binpkg = None
try:
    from modules import build as _build
except Exception:
    _build = None
try:
    from modules import sandbox as _sandbox
except Exception:
    _sandbox = None
try:
    from modules import fakeroot as _fakeroot
except Exception:
    _fakeroot = None
try:
    from modules import hooks as _hooks
except Exception:
    _hooks = None
try:
    from modules import logger as _logger
except Exception:
    _logger = None
try:
    from modules import search as _search
except Exception:
    _search = None

# fallback logger
class _SimpleLogger:
    def info(self, *a, **k): print("[INFO]", *a)
    def error(self, *a, **k): print("[ERROR]", *a, file=sys.stderr)
    def debug(self, *a, **k): print("[DEBUG]", *a)

LOG = _logger.Logger("upgrade.log") if (_logger and hasattr(_logger, "Logger")) else _SimpleLogger()

DEFAULT_CONF_PATHS = [
    "/etc/sources/source.conf",
    os.path.expanduser("~/.config/sources/source.conf"),
    os.path.join(os.getcwd(), "source.conf"),
]

def load_conf(conf_path: Optional[str] = None) -> Dict[str, Any]:
    cfg = configparser.ConfigParser()
    found = None
    if conf_path and os.path.exists(conf_path):
        cfg.read(conf_path); found = conf_path
    else:
        for p in DEFAULT_CONF_PATHS:
            if os.path.exists(p):
                cfg.read(p); found = p
                break
    conf = {}
    section = "sources"
    defaults = {
        "recipes_dir": "/usr/sources",
        "installed_db": "/var/lib/sources/installed_db.json",
        "report_dir": "/var/log/sources",
        "concurrency": "4",
        "binpkg_cache": "binpkg_cache",
        "git_prefer": "tags"
    }
    if found and cfg.has_section(section):
        for k, v in cfg.items(section):
            conf[k] = v
    for k, v in defaults.items():
        conf.setdefault(k, v)
    conf["concurrency"] = int(conf.get("concurrency"))
    return conf

def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

def has_notify_send() -> bool:
    return shutil.which("notify-send") is not None

def notify(title: str, message: str, dry_run: bool):
    if dry_run:
        LOG.info(f"[DRY-RUN] notify: {title} - {message}")
        return
    if has_notify_send():
        try:
            subprocess.run(["notify-send", title, message], check=False)
        except Exception as e:
            LOG.error("notify-send failed: " + str(e))
            LOG.info(f"{title}: {message}")
    else:
        LOG.info(f"{title}: {message}")

# version utilities (robust, same as other modules)
def version_key(v: str):
    if not isinstance(v, str):
        return [v]
    s = v.strip()
    if s.startswith("v") and re.match(r"v\d", s):
        s = s[1:]
    parts = re.split(r'[.\-_\+]', s)
    key = []
    for p in parts:
        if p.isdigit():
            key.append(int(p))
        else:
            m = re.match(r'([a-zA-Z]+)(\d+)$', p)
            if m:
                key.append(m.group(1))
                key.append(int(m.group(2)))
            else:
                key.append(p.lower())
    return key

def compare_versions(a: Optional[str], b: Optional[str]) -> int:
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    ka = version_key(a)
    kb = version_key(b)
    for x, y in zip(ka, kb):
        if type(x) == type(y):
            if x < y: return -1
            if x > y: return 1
        else:
            if isinstance(x, int) and isinstance(y, str):
                return 1
            if isinstance(x, str) and isinstance(y, int):
                return -1
            if str(x) < str(y): return -1
            if str(x) > str(y): return 1
    if len(ka) < len(kb):
        rest = kb[len(ka):]
        for r in rest:
            if r == 0 or r == "":
                continue
            return -1
        return 0
    elif len(ka) > len(kb):
        rest = ka[len(kb):]
        for r in rest:
            if r == 0 or r == "":
                continue
            return 1
        return 0
    return 0

# -----------------------
# main UpgradeManager
# -----------------------
class UpgradeError(Exception):
    pass

class UpgradeManager:
    def __init__(self, conf_path: Optional[str] = None, dry_run: bool = True):
        self.conf = load_conf(conf_path)
        self.recipes_dir = os.path.abspath(self.conf["recipes_dir"])
        self.installed_db_path = os.path.abspath(self.conf["installed_db"])
        self.report_dir = os.path.abspath(self.conf["report_dir"])
        self.concurrency = int(self.conf.get("concurrency", 4))
        self.binpkg_cache = os.path.abspath(self.conf.get("binpkg_cache", "binpkg_cache"))
        os.makedirs(self.report_dir, exist_ok=True)
        os.makedirs(self.binpkg_cache, exist_ok=True)

        self.dry_run = dry_run

        # helpers
        self.recipe_mgr = _recipe.RecipeManager() if _recipe else None
        self.search = (_search.PackageSearch(repo_path=self.recipes_dir, installed_db=self.installed_db_path)
                       if _search else None)
        # binpkg/build/sandbox/fakeroot/hook managers
        self.binpkg_mgr = _binpkg.BinPkgManager(cache_dir=self.binpkg_cache, installed_db=self.installed_db_path, dry_run=self.dry_run) if _binpkg else None
        self.build_mgr = None
        if _build:
            # try common class names
            for name in ("Builder", "BuildManager"):
                cls = getattr(_build, name, None)
                if callable(cls):
                    try:
                        self.build_mgr = cls(dry_run=self.dry_run)
                        break
                    except Exception:
                        try:
                            self.build_mgr = cls()
                            break
                        except Exception:
                            continue
        self.sandbox_cls = getattr(_sandbox, "Sandbox", None) if _sandbox else None
        # Hook manager
        self.hooks = None
        if _hooks:
            try:
                self.hooks = _hooks.HookManager(dry_run=self.dry_run)
            except Exception:
                try:
                    self.hooks = _hooks.HookManager()
                    self.hooks.dry_run = self.dry_run
                except Exception:
                    self.hooks = None

        # logger
        self.log = LOG

        # load installed_db
        self.installed_db: Dict[str, Any] = {}
        if os.path.exists(self.installed_db_path):
            try:
                with open(self.installed_db_path, "r", encoding="utf-8") as fh:
                    self.installed_db = json.load(fh)
            except Exception as e:
                self.log.error("Failed to read installed_db: " + str(e))
                self.installed_db = {}

    # -----------------------
    # Helpers: recipe loading
    # -----------------------
    def _load_recipe_for(self, pkg: str) -> Optional[Dict[str,Any]]:
        """
        Busca recipe no recipes_dir/<pkg>/recipe.yaml ou recipe.json ou recipe.yaml
        Retorna dicionário da recipe ou None.
        """
        base = os.path.join(self.recipes_dir, pkg)
        if not os.path.isdir(base):
            return None
        # try recipe.json, recipe.yaml
        for fn in ("recipe.json", "recipe.yaml", "recipe.yml"):
            path = os.path.join(base, fn)
            if os.path.exists(path):
                try:
                    if fn.endswith(".json"):
                        with open(path, "r", encoding="utf-8") as fh:
                            return json.load(fh)
                    else:
                        # lazy import yaml only if needed
                        try:
                            import yaml
                            with open(path, "r", encoding="utf-8") as fh:
                                return yaml.safe_load(fh)
                        except Exception:
                            # attempt simple parsing fallback for YAML-ish (not ideal)
                            with open(path, "r", encoding="utf-8") as fh:
                                txt = fh.read()
                                # very naive json-like extraction - prefer having pyyaml installed
                                try:
                                    return json.loads(txt)
                                except Exception:
                                    return {"name": pkg}
                except Exception as e:
                    self.log.error(f"Failed to parse recipe {path}: {e}")
                    return None
        return None

    # -----------------------
    # Determine upgrade candidates (local recipes only)
    # -----------------------
    def find_local_upgrade_candidates(self, force: bool = False) -> Dict[str, Dict[str,Any]]:
        """
        Varre installed_db e compara com recipes locais em recipes_dir.
        Retorna dict {pkg: {"installed": v_inst, "available": v_avail, "recipe": recipe_dict}}
        """
        candidates = {}
        for pkg, meta in self.installed_db.items():
            installed_v = meta.get("version")
            recipe = self._load_recipe_for(pkg)
            if not recipe:
                self.log.debug(f"No local recipe for {pkg}")
                continue
            avail_v = recipe.get("version")
            if avail_v is None:
                continue
            cmp = compare_versions(installed_v, avail_v)
            if cmp < 0 or force:
                candidates[pkg] = {"installed": installed_v, "available": avail_v, "recipe": recipe}
        return candidates

    # -----------------------
    # Dependency graph & ordering
    # -----------------------
    def build_dep_graph(self, pkgs: List[str]) -> Dict[str, List[str]]:
        """
        Build dependency graph restricted to pkgs set. Only considers 'depends' or 'rdepends' fields in recipe.
        Returns {pkg: [dep1, dep2, ...]} where deps are in pkgs set (we only resolve upgrades among selected).
        """
        graph: Dict[str, List[str]] = {}
        for p in pkgs:
            recipe = self._load_recipe_for(p)
            if not recipe:
                graph[p] = []
                continue
            deps = recipe.get("depends") or recipe.get("dependencies") or []
            # normalize to names; only keep those in pkgs (we don't auto-add external packages here)
            graph[p] = [d for d in deps if d in pkgs]
        return graph

    def topo_levels(self, nodes: List[str], deps: Dict[str, List[str]]) -> List[List[str]]:
        """
        Levelize graph: each level contains nodes whose deps are already built.
        In cycles, a remaining set will be returned as one level.
        """
        nodes_set = set(nodes)
        built: Set[str] = set()
        remain = set(nodes)
        levels: List[List[str]] = []
        while remain:
            ready = []
            for n in sorted(remain):
                if set(deps.get(n, [])).issubset(built):
                    ready.append(n)
            if not ready:
                # cycle or impossible deps: include all remain as one level
                ready = sorted(list(remain))
            for r in ready:
                remain.remove(r)
                built.add(r)
            levels.append(ready)
        return levels

    # -----------------------
    # Per-package upgrade
    # -----------------------
    def _run_recipe_hooks(self, recipe: Dict[str,Any], stage: str):
        """
        Execute recipe-level hooks if present: recipe.get('hooks', {}).get(stage)
        Supports both command strings and callables stored in recipe (callable unlikely).
        """
        hooks_list = []
        if recipe:
            hooks_list = (recipe.get("hooks") or {}).get(stage, []) or []
        # global hooks manager may also provide run_hooks(stage, recipe, sandbox)
        if self.hooks and hasattr(self.hooks, "run_hooks"):
            try:
                self.hooks.run_hooks(stage, recipe, None)
            except Exception as e:
                self.log.error(f"Global hook manager error for stage {stage}: {e}")
        # run recipe-level
        for hk in hooks_list:
            if callable(hk):
                try:
                    if self.dry_run:
                        self.log.info(f"[DRY-RUN] would call python hook {hk} for stage {stage}")
                    else:
                        hk(recipe)
                except Exception as e:
                    self.log.error(f"Error executing recipe hook {hk}: {e}")
            else:
                # treat as shell command (string)
                cmd = hk
                try:
                    if self.dry_run:
                        self.log.info(f"[DRY-RUN] would run hook cmd: {cmd}")
                    else:
                        # use fakeroot if available for commands that need privileges
                        if hasattr(self.binpkg_mgr, "fakeroot") and self.binpkg_mgr.fakeroot:
                            self.binpkg_mgr.fakeroot.run(cmd, shell=True, check=True)
                        else:
                            subprocess.run(cmd, shell=True, check=True)
                except Exception as e:
                    self.log.error(f"Error executing hook cmd '{cmd}': {e}")
                    raise

    def upgrade_package(self, pkg: str, candidate: Dict[str,Any], force: bool = False) -> Dict[str,Any]:
        """
        Performs the upgrade for a single package:
         - runs pre hooks
         - tries binpkg in cache (/var/cache/... / self.binpkg_cache)
         - else tries build using build_mgr and sandbox
         - runs post hooks
        Returns result dict.
        """
        res = {"package": pkg, "installed": candidate.get("installed"), "available": candidate.get("available"),
               "status": "pending", "method": None, "error": None, "timestamp": now_iso()}
        recipe = candidate.get("recipe") or {}
        try:
            # pre-package hooks
            self._run_recipe_hooks(recipe, "pre_package_upgrade")
            if self.hooks and hasattr(self.hooks, "run_hooks"):
                try:
                    self.hooks.run_hooks("pre_upgrade", recipe, None)
                except Exception as e:
                    self.log.error(f"pre_upgrade global hook error: {e}")

            # try binpkg cache first: expected name pkg-version.tar.gz
            archive_path = os.path.join(self.binpkg_cache, f"{pkg}-{candidate.get('available')}.tar.gz")
            if os.path.exists(archive_path) and self.binpkg_mgr:
                res["method"] = "binpkg-cache"
                if self.dry_run:
                    self.log.info(f"[DRY-RUN] would install binpkg {archive_path} for {pkg}")
                    res["status"] = "dry-run"
                else:
                    self.log.info(f"Installing binpkg {archive_path} for {pkg}")
                    inst = self.binpkg_mgr.install_binpkg(archive_path, force=force, backup=True)
                    res["status"] = "ok" if inst.get("installed", False) else "failed"
                    res["install_result"] = inst
                # post hooks
                self._run_recipe_hooks(recipe, "post_package_upgrade")
                if self.hooks and hasattr(self.hooks, "run_hooks"):
                    try:
                        self.hooks.run_hooks("post_upgrade", recipe, None)
                    except Exception as e:
                        self.log.error(f"post_upgrade global hook error: {e}")
                return res

            # else try building from local recipe via build manager
            if not self.build_mgr:
                res["status"] = "no-builder"
                res["error"] = "No build manager available to build from source"
                self.log.error(res["error"])
                return res

            # prepare sandbox if sandbox class present
            sandbox_path = None
            sb = None
            if self.sandbox_cls:
                try:
                    sb = self.sandbox_cls(pkg, base_dir=os.path.join(tempfile.gettempdir(),"sandbox"), dry_run=self.dry_run)
                    # try to prepare with recipe metadata if possible
                    try:
                        sb.prepare()
                    except TypeError:
                        # some implementations require arguments
                        try:
                            sb.prepare(clean=True)
                        except Exception:
                            pass
                    sandbox_path = getattr(sb, "path", None) or None
                except Exception as e:
                    self.log.debug(f"Failed to instantiate sandbox for {pkg}: {e}")
                    sb = None

            # call build manager - try several common method names
            build_fn = None
            for name in ("build_single_pkg", "build_pkg", "build", "build_package"):
                if hasattr(self.build_mgr, name):
                    build_fn = getattr(self.build_mgr, name)
                    break
            if not build_fn:
                # try module-level function
                if hasattr(_build, "build_single_pkg"):
                    build_fn = getattr(_build, "build_single_pkg")
            if not build_fn:
                res["status"] = "no-build-fn"
                res["error"] = "Build manager present but no known build function found"
                self.log.error(res["error"])
                return res

            # call build function; signatures vary, try common patterns
            self.log.info(f"Building {pkg} from source using build manager")
            if self.dry_run:
                self.log.info(f"[DRY-RUN] would call build function for {pkg}")
                res["status"] = "dry-run"
                res["method"] = "build"
                # run post hooks
                try:
                    self._run_recipe_hooks(recipe, "post_package_upgrade")
                except Exception:
                    pass
                return res

            try:
                # Try calling build_fn in multiple plausible ways
                build_result = None
                try:
                    build_result = build_fn(pkg, os.path.join(self.recipes_dir, pkg), recipe)
                except TypeError:
                    try:
                        build_result = build_fn(os.path.join(self.recipes_dir, pkg))
                    except TypeError:
                        build_result = build_fn(pkg)
                res["method"] = "build"
                res["build_result"] = build_result
                # if build_result contains archive, try to install via binpkg_mgr
                archive = None
                if isinstance(build_result, dict):
                    archive = build_result.get("archive") or build_result.get("artifact")
                if archive and os.path.exists(archive) and self.binpkg_mgr:
                    self.log.info(f"Installing archive produced by build for {pkg}: {archive}")
                    inst = self.binpkg_mgr.install_binpkg(archive, force=force, backup=True)
                    res["install_result"] = inst
                    res["status"] = "ok" if inst.get("installed", False) else "failed"
                else:
                    # build finished but no archive - maybe build function installed in-place
                    res["status"] = "ok"
            except Exception as e:
                res["status"] = "failed"
                res["error"] = str(e)
                self.log.error(f"Build/install failed for {pkg}: {e}")

            # post hooks
            try:
                self._run_recipe_hooks(recipe, "post_package_upgrade")
                if self.hooks and hasattr(self.hooks, "run_hooks"):
                    try:
                        self.hooks.run_hooks("post_upgrade", recipe, None)
                    except Exception as e:
                        self.log.error(f"post_upgrade global hook error: {e}")
            except Exception as e:
                self.log.error(f"post-package hooks failed for {pkg}: {e}")

            # update installed_db entry if success
            if res.get("status") == "ok" and not self.dry_run:
                entry = self.installed_db.get(pkg, {})
                entry["version"] = candidate.get("available")
                entry["updated_at"] = now_iso()
                self.installed_db[pkg] = entry
                # save immediately to persist partial progress
                try:
                    self._save_installed_db()
                except Exception as e:
                    self.log.error(f"Failed saving installed_db after upgrading {pkg}: {e}")

            return res
        except Exception as e:
            res["status"] = "error"
            res["error"] = str(e)
            self.log.error(f"Exception upgrading {pkg}: {e}")
            return res

    def _save_installed_db(self):
        if self.dry_run:
            self.log.info("[DRY-RUN] would save installed_db")
            return
        dirp = os.path.dirname(self.installed_db_path)
        if dirp and not os.path.exists(dirp):
            os.makedirs(dirp, exist_ok=True)
        with open(self.installed_db_path, "w", encoding="utf-8") as fh:
            json.dump(self.installed_db, fh, indent=2)
        self.log.info(f"installed_db saved to {self.installed_db_path}")

    # -----------------------
    # Upgrade orchestration
    # -----------------------
    def upgrade(self,
                packages: Optional[List[str]] = None,
                execute: bool = False,
                force: bool = False,
                concurrency: Optional[int] = None) -> Dict[str,Any]:
        """
        Orquestra upgrade.
        - packages: list of package names to upgrade; if None -> upgrade all local candidates
        - execute: if False -> dry-run mode (no changes)
        - force: force upgrade even if versions are same
        - concurrency: number of workers per level (parallel)
        """
        report: Dict[str,Any] = {
            "started_at": now_iso(),
            "recipes_dir": self.recipes_dir,
            "execute": execute and (not self.dry_run),
            "dry_run": self.dry_run,
            "candidates": {},
            "levels": [],
            "results": {}
        }
        # reload installed_db in case changed
        if os.path.exists(self.installed_db_path):
            try:
                with open(self.installed_db_path, "r", encoding="utf-8") as fh:
                    self.installed_db = json.load(fh)
            except Exception:
                pass

        # find candidates
        all_candidates = self.find_local_upgrade_candidates(force=force)
        if packages:
            # filter to intersection
            selected = {p: all_candidates[p] for p in packages if p in all_candidates}
        else:
            selected = all_candidates

        report["candidates"] = {k: {"installed":v["installed"], "available":v["available"]} for k,v in selected.items()}

        if not selected:
            report["message"] = "No upgrade candidates found"
            report["finished_at"] = now_iso()
            return report

        # compute dependency graph among selected packages
        pkgs = list(selected.keys())
        dep_graph = self.build_dep_graph(pkgs)
        levels = self.topo_levels(pkgs, dep_graph)
        report["levels"] = levels

        # per-level parallelism
        concurrency = concurrency or self.concurrency
        for level_idx, level in enumerate(levels):
            report["results"].setdefault(level_idx, {})
            if not level:
                continue
            # for each pkg in level, upgrade (parallel)
            self.log.info(f"Upgrading level {level_idx}: {level}")
            futures = {}
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                for pkg in level:
                    cand = selected.get(pkg)
                    if not cand:
                        report["results"][level_idx][pkg] = {"status":"skipped","reason":"not selected"}
                        continue
                    # dry-run check
                    if self.dry_run or not execute:
                        self.log.info(f"[DRY-RUN] Would upgrade {pkg} {cand['installed']} -> {cand['available']}")
                        report["results"][level_idx][pkg] = {"status":"dry-run","installed":cand["installed"], "available":cand["available"]}
                        continue
                    futures[ex.submit(self.upgrade_package, pkg, cand, force)]=pkg

                # gather done
                for fut in as_completed(futures):
                    pkg = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as e:
                        res = {"status":"error","error":str(e)}
                    report["results"][level_idx][pkg] = res

        # final save of DB if executing and not dry-run
        if execute and (not self.dry_run):
            try:
                self._save_installed_db()
            except Exception as e:
                self.log.error("Failed saving installed_db at end: " + str(e))

        report["finished_at"] = now_iso()

        # write report
        basename = f"upgrade-report-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        jsonp = os.path.join(self.report_dir, basename + ".json")
        try:
            if self.dry_run or not execute:
                self.log.info(f"[DRY-RUN] Would write report to {jsonp}")
            else:
                with open(jsonp, "w", encoding="utf-8") as fh:
                    json.dump(report, fh, indent=2)
                self.log.info(f"Wrote report to {jsonp}")
        except Exception as e:
            self.log.error("Failed writing report: " + str(e))

        # notification summarizing status
        outdated_count = len(pkgs)
        ok_count = 0
        fail_count = 0
        # count results
        for lvl in report.get("results", {}).values():
            for pkg, r in lvl.items():
                st = r.get("status")
                if st == "ok":
                    ok_count += 1
                elif st in ("failed","error"):
                    fail_count += 1
        title = f"Upgrade: {ok_count} ok, {fail_count} failed ({outdated_count} candidates)"
        lines = []
        # produce short lines of failed or updated packages
        for lvl in report.get("results", {}).values():
            for pkg, r in lvl.items():
                if r.get("status") == "ok":
                    lines.append(f"{pkg}: updated to {r.get('available')}")
                elif r.get("status") in ("failed","error"):
                    lines.append(f"{pkg}: FAIL ({r.get('error')})")
        message = "\n".join(lines[:6]) + (f"\n...+{max(0,len(lines)-6)} others" if len(lines)>6 else "")
        notify(title, message or "No detailed lines", self.dry_run or (not execute))

        return report

# -----------------------
# CLI
# -----------------------
def main(argv=None):
    import argparse
    argv = argv or sys.argv[1:]
    ap = argparse.ArgumentParser(prog="upgrade", description="Upgrade packages using local /usr/sources recipes (respects deps)")
    ap.add_argument("--conf", help="Path to source.conf")
    ap.add_argument("--pkg", nargs="+", help="Specific package(s) to upgrade (default: all candidates)")
    ap.add_argument("--execute", action="store_true", help="Perform changes (default is dry-run)")
    ap.add_argument("--force", action="store_true", help="Force upgrade even if versions equal")
    ap.add_argument("--concurrency", type=int, help="Parallel workers per level")
    ap.add_argument("--report-dir", help="Override report dir")
    ap.add_argument("--dry-run", action="store_true", help="Explicit dry-run (default)")
    args = ap.parse_args(argv)

    conf_path = args.conf
    mgr = UpgradeManager(conf_path=conf_path, dry_run=(not args.execute or args.dry_run))
    if args.report_dir:
        mgr.report_dir = args.report_dir
    try:
        report = mgr.upgrade(packages=args.pkg, execute=args.execute, force=args.force, concurrency=args.concurrency)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except Exception as e:
        LOG.error("Upgrade failed: " + str(e))
        return 2

if __name__ == "__main__":
    sys.exit(main())
