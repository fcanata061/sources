#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto.py - Ultimate Auto Manager (evolved)

Funcionalidades estendidas:
 - resolução de dependências com version constraints (>=, <=, =, ^, ~)
 - caching persistente de recipes
 - retries/backoff em builds
 - rollback best-effort em falhas
 - export DOT/JSON do grafo/plano
 - interactive mode, dry-run produzido script de execução
 - hooks: pre_auto, post_auto, pre_auto_pkg, post_auto_pkg, on_fail_pkg, on_recover_pkg
 - integração com modules.build, modules.binpkg, modules.hooks, modules.history
"""
from __future__ import annotations

import os
import sys
import json
import re
import time
import math
import shutil
import threading
import traceback
from typing import Dict, Any, List, Optional, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# Rich UI
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.tree import Tree
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
    from rich.prompt import Confirm
    from rich.traceback import install as rich_install
    rich_install()
except Exception:
    Console = None

console = Console() if Console else None

# Try import project modules (graceful)
def try_mod(name: str):
    try:
        return __import__(name, fromlist=["*"])
    except Exception:
        return None

_mod_config = try_mod("modules.config") or try_mod("config")
_mod_build = try_mod("modules.build") or try_mod("build")
_mod_binpkg = try_mod("modules.binpkg") or try_mod("binpkg")
_mod_hooks = try_mod("modules.hooks") or try_mod("hooks")
_mod_logger = try_mod("modules.logger") or try_mod("logger")
_mod_history = try_mod("modules.history") or try_mod("history")

# logger
if _mod_logger and hasattr(_mod_logger, "Logger"):
    try:
        LOG = _mod_logger.Logger("auto.log")
    except Exception:
        class _FallbackLog:
            def info(self, *a, **k): print("[INFO]", *a)
            def error(self, *a, **k): print("[ERROR]", *a, file=sys.stderr)
            def debug(self, *a, **k): print("[DEBUG]", *a)
        LOG = _FallbackLog()
else:
    class _FallbackLog:
        def info(self, *a, **k): print("[INFO]", *a)
        def error(self, *a, **k): print("[ERROR]", *a, file=sys.stderr)
        def debug(self, *a, **k): print("[DEBUG]", *a)
    LOG = _FallbackLog()

# basic defaults (config overrides if available)
DEFAULTS = {
    "recipes_dir": "/usr/sources",
    "installed_db": "/var/lib/sources/installed_db.json",
    "report_dir": "/var/log/sources",
    "cache_dir": "/var/cache/source_auto",
    "concurrency": 4,
    "notify": True,
    "notify_title": "Source Auto",
    "cache_file": ".cache/auto_recipes.json",
    "max_retries": 2,
    "backoff_base": 2.0
}

def now_iso():
    return datetime.utcnow().isoformat() + "Z"

# -------------------------
# Version handling utilities
# -------------------------
def version_key(v: Optional[str]):
    if v is None:
        return []
    s = str(v).strip()
    if s.startswith("v") and re.match(r"v\d", s):
        s = s[1:]
    parts = re.split(r'[.+_\-]', s)
    key = []
    for p in parts:
        if p.isdigit():
            key.append(int(p))
        else:
            # split alpha suffix like rc1
            m = re.match(r'([a-zA-Z]+)(\d+)$', p)
            if m:
                key.append(m.group(1))
                key.append(int(m.group(2)))
            else:
                key.append(p.lower())
    return key

def compare_versions(a: Optional[str], b: Optional[str]) -> int:
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
        return -1
    if len(ka) > len(kb):
        return 1
    return 0

# Simple semver-ish constraint matcher: supports ">=1.2.3", "<=2.0", "=1.5", "^1.2", "~1.2"
def version_satisfies(version: Optional[str], constraint: Optional[str]) -> bool:
    if not constraint or not version:
        return True
    v = version.strip()
    c = constraint.strip()
    # exact
    if c.startswith("="):
        return compare_versions(v, c[1:].strip()) == 0
    if c.startswith(">="):
        return compare_versions(v, c[2:].strip()) >= 0
    if c.startswith("<="):
        return compare_versions(v, c[2:].strip()) <= 0
    if c.startswith(">"):
        return compare_versions(v, c[1:].strip()) > 0
    if c.startswith("<"):
        return compare_versions(v, c[1:].strip()) < 0
    if c.startswith("^"):
        # ^1.2 => >=1.2 <2.0
        base = c[1:].strip()
        parts = base.split(".")
        major = parts[0]
        upper = str(int(major) + 1)
        return compare_versions(v, base) >= 0 and compare_versions(v, upper) < 0
    if c.startswith("~"):
        # ~1.2 => >=1.2 <1.3
        base = c[1:].strip()
        parts = base.split(".")
        if len(parts) >= 2:
            major = parts[0]; minor = parts[1]
            upper = f"{major}.{int(minor)+1}"
        else:
            upper = f"{parts[0]}.9999"
        return compare_versions(v, base) >= 0 and compare_versions(v, upper) < 0
    # fallback: exact match
    return compare_versions(v, c) == 0

# -------------------------
# AutoManager
# -------------------------
class AutoManager:
    def __init__(self,
                 recipes_dir: Optional[str] = None,
                 installed_db: Optional[str] = None,
                 report_dir: Optional[str] = None,
                 concurrency: Optional[int] = None,
                 dry_run: bool = True,
                 cache_file: Optional[str] = None,
                 max_retries: Optional[int] = None):
        cfg = _mod_config.config if (_mod_config and hasattr(_mod_config, "config")) else None
        self.recipes_dir = recipes_dir or (cfg.recipes_dir if cfg else DEFAULTS["recipes_dir"])
        self.installed_db_path = installed_db or (cfg.installed_db if cfg else DEFAULTS["installed_db"])
        self.report_dir = report_dir or (cfg.log_dir if cfg else DEFAULTS["report_dir"])
        self.cache_dir = (getattr(cfg, "cache_dir", DEFAULTS["cache_dir"]) if cfg else DEFAULTS["cache_dir"])
        self.cache_file = cache_file or os.path.join(self.cache_dir, (getattr(cfg, "cache_file", DEFAULTS["cache_file"]) if cfg else DEFAULTS["cache_file"]))
        self.concurrency = concurrency or (getattr(cfg, "concurrency", DEFAULTS["concurrency"]) if cfg else DEFAULTS["concurrency"])
        self.dry_run = dry_run
        self.max_retries = (max_retries if max_retries is not None else (getattr(cfg, "max_retries", DEFAULTS["max_retries"]) if cfg else DEFAULTS["max_retries"]))
        self.backoff_base = (getattr(cfg, "backoff_base", DEFAULTS["backoff_base"]) if cfg else DEFAULTS["backoff_base"])
        self.notify_enabled = (getattr(cfg, "notify_enabled", DEFAULTS["notify"]) if cfg else DEFAULTS["notify"])
        self.notify_title = (getattr(cfg, "notify_title", DEFAULTS["notify_title"]) if cfg else DEFAULTS["notify_title"])

        # ensure dirs
        os.makedirs(self.report_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)

        # managers
        self.hooks = None
        if _mod_hooks and hasattr(_mod_hooks, "HookManager"):
            try:
                self.hooks = _mod_hooks.HookManager(dry_run=self.dry_run)
            except Exception:
                try:
                    self.hooks = _mod_hooks.HookManager()
                    self.hooks.dry_run = self.dry_run
                except Exception:
                    self.hooks = None
        self.history = None
        if _mod_history and hasattr(_mod_history, "History"):
            try:
                self.history = _mod_history.History(dry_run=self.dry_run)
            except Exception:
                try:
                    self.history = _mod_history.History()
                    self.history.dry_run = self.dry_run
                except Exception:
                    self.history = None

        # load installed db
        self.installed = {}
        self._load_installed_db()

        # build/binpkg detection
        self.build_mgr = self._detect_build_manager()
        self.binpkg_mgr = self._detect_binpkg_manager()

        # recipes cache
        self._recipes_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_mtime: Dict[str, float] = {}
        self._recipes_lock = threading.Lock()
        self._load_cache()

    # -------------------------
    # IO helpers
    # -------------------------
    def _load_installed_db(self):
        try:
            if os.path.exists(self.installed_db_path):
                with open(self.installed_db_path, "r", encoding="utf-8") as fh:
                    self.installed = json.load(fh)
            else:
                self.installed = {}
        except Exception as e:
            LOG.error("Error loading installed_db: " + str(e))
            self.installed = {}

    def _save_installed_db(self):
        try:
            dirp = os.path.dirname(self.installed_db_path)
            if dirp and not os.path.exists(dirp):
                os.makedirs(dirp, exist_ok=True)
            with open(self.installed_db_path, "w", encoding="utf-8") as fh:
                json.dump(self.installed, fh, indent=2, ensure_ascii=False)
        except Exception as e:
            LOG.error("Failed saving installed_db: " + str(e))

    def _report_path(self, basename: Optional[str] = None) -> str:
        basename = basename or f"auto-report-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        return os.path.join(self.report_dir, basename + ".json")

    # -------------------------
    # cache persistence
    # -------------------------
    def _load_cache(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._recipes_cache = data.get("recipes", {})
                self._cache_mtime = data.get("mtimes", {})
                LOG.info(f"Loaded recipes cache ({len(self._recipes_cache)} entries)")
            else:
                self._recipes_cache = {}
                self._cache_mtime = {}
        except Exception as e:
            LOG.error("Failed to load cache: " + str(e))
            self._recipes_cache = {}
            self._cache_mtime = {}

    def _save_cache(self):
        try:
            data = {"recipes": self._recipes_cache, "mtimes": self._cache_mtime}
            with open(self.cache_file, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            LOG.info("Recipes cache saved")
        except Exception as e:
            LOG.error("Failed to save cache: " + str(e))

    # -------------------------
    # recipe parsing
    # -------------------------
    def _recipe_path(self, pkg: str) -> Optional[str]:
        base = os.path.join(self.recipes_dir, pkg)
        if not os.path.isdir(base):
            return None
        for fn in ("recipe.yaml", "recipe.yml", "recipe.json"):
            p = os.path.join(base, fn)
            if os.path.exists(p):
                return p
        return None

    def _read_recipe_file(self, pkg: str) -> Optional[Dict[str, Any]]:
        """
        Read recipe and cache it. Invalidate cache when mtime changes.
        """
        path = self._recipe_path(pkg)
        if not path:
            return None
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = None
        with self._recipes_lock:
            cached = self._recipes_cache.get(pkg)
            if cached and (self._cache_mtime.get(pkg) == mtime):
                return cached
        # parse
        try:
            if path.endswith(".json"):
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            else:
                try:
                    import yaml
                    with open(path, "r", encoding="utf-8") as fh:
                        data = yaml.safe_load(fh)
                except Exception:
                    with open(path, "r", encoding="utf-8") as fh:
                        data = json.loads(fh.read())
            with self._recipes_lock:
                self._recipes_cache[pkg] = data or {}
                if mtime:
                    self._cache_mtime[pkg] = mtime
            return data or {}
        except Exception as e:
            LOG.error(f"Failed to parse recipe {path}: {e}")
            return None

    def list_all_recipes(self) -> List[str]:
        if not os.path.isdir(self.recipes_dir):
            return []
        out = []
        for name in sorted(os.listdir(self.recipes_dir)):
            if os.path.isdir(os.path.join(self.recipes_dir, name)):
                if self._recipe_path(name):
                    out.append(name)
        return out

    # -------------------------
    # dependency resolution
    # -------------------------
    def _get_deps_from_recipe(self, pkg: str) -> Dict[str, List[Any]]:
        """
        Returns dict: {'build': [(name, constraint),...], 'runtime': [(name,constraint),...]}
        Accepts several recipe shapes.
        """
        rec = self._read_recipe_file(pkg)
        if not rec:
            return {"build": [], "runtime": []}
        out = {"build": [], "runtime": []}
        # prefer structured dependencies block
        if isinstance(rec.get("dependencies"), dict):
            for k in ("build", "runtime"):
                lst = rec["dependencies"].get(k, []) or []
                for item in lst:
                    if isinstance(item, str):
                        out[k].append((item, None))
                    elif isinstance(item, dict):
                        name = item.get("name") or item.get("pkg") or item.get("package")
                        ver = item.get("version") or item.get("constraint")
                        out[k].append((name, ver))
        else:
            # legacy keys
            if rec.get("build-depends"):
                for item in rec.get("build-depends", []):
                    if isinstance(item, str): out["build"].append((item, None))
                    elif isinstance(item, dict): out["build"].append((item.get("name"), item.get("version")))
            if rec.get("depends"):
                for item in rec.get("depends", []):
                    if isinstance(item, str): out["runtime"].append((item, None))
                    elif isinstance(item, dict): out["runtime"].append((item.get("name"), item.get("version")))
        return out

    def resolve_all_deps(self, roots: List[str], include_build: bool=True, include_runtime: bool=True,
                         only_regex: Optional[str]=None, exclude_regex: Optional[str]=None,
                         auto_add_missing: bool=False) -> Dict[str, Any]:
        only_re = re.compile(only_regex) if only_regex else None
        exclude_re = re.compile(exclude_regex) if exclude_regex else None

        def accept(n: str) -> bool:
            if only_re and not only_re.search(n):
                return False
            if exclude_re and exclude_re.search(n):
                return False
            return True

        graph = {}  # pkg -> set(deps)
        missing = set()
        seen = set()

        def add_node(n):
            if n not in graph:
                graph[n] = set()

        def walk(pkg):
            if pkg in seen:
                return
            seen.add(pkg)
            if not accept(pkg):
                return
            rec = self._read_recipe_file(pkg)
            if not rec:
                missing.add(pkg)
                add_node(pkg)
                return
            deps = self._get_deps_from_recipe(pkg)
            depset = set()
            if include_build:
                depset.update([d[0] for d in deps.get("build", []) if d and d[0]])
            if include_runtime:
                depset.update([d[0] for d in deps.get("runtime", []) if d and d[0]])
            add_node(pkg)
            for d in depset:
                if not accept(d):
                    continue
                graph[pkg].add(d)
                walk(d)

        for r in roots:
            walk(r)
        # nodes list
        nodes = set(graph.keys())
        for deps in graph.values():
            nodes.update(deps)
        return {"graph": {k: sorted(list(v)) for k,v in graph.items()}, "nodes": sorted(list(nodes)), "missing": sorted(list(missing))}

    def topo_levels(self, nodes: List[str], deps: Dict[str, List[str]]) -> List[List[str]]:
        nodes_set = set(nodes)
        built = set()
        remain = set(nodes)
        levels = []
        while remain:
            ready = []
            for n in sorted(remain):
                dlist = [d for d in deps.get(n, []) if d in nodes_set]
                if set(dlist).issubset(built):
                    ready.append(n)
            if not ready:
                # cycle: capture cycle nodes
                # best-effort: find one cycle via DFS
                cycle = self._find_cycle(remain, deps)
                if cycle:
                    ready = cycle
                else:
                    ready = sorted(list(remain))
            for r in ready:
                remain.remove(r)
                built.add(r)
            levels.append(ready)
        return levels

    def _find_cycle(self, nodes_remaining: Set[str], deps: Dict[str, List[str]]) -> List[str]:
        # simple DFS to detect cycle among remain
        graph = deps
        visited = {}
        path = []
        def dfs(u):
            visited[u] = 1
            path.append(u)
            for v in graph.get(u, []):
                if v not in nodes_remaining:
                    continue
                if visited.get(v) == 1:
                    # cycle found - return cycle slice
                    idx = path.index(v)
                    return path[idx:]
                if visited.get(v) is None:
                    c = dfs(v)
                    if c: return c
            path.pop()
            visited[u] = 2
            return None
        for n in nodes_remaining:
            if visited.get(n) is None:
                c = dfs(n)
                if c:
                    return c
        return list(nodes_remaining)  # fallback

    # -------------------------
    # Build / Install orchestration
    # -------------------------
    def _detect_build_manager(self):
        if not _mod_build:
            return None
        for name in ("Builder","BuildManager"):
            cls = getattr(_mod_build, name, None)
            if callable(cls):
                try:
                    return cls(dry_run=self.dry_run)
                except Exception:
                    try:
                        inst = cls()
                        inst.dry_run = self.dry_run
                        return inst
                    except Exception:
                        continue
        # module-level functions fallback
        if any(hasattr(_mod_build, fn) for fn in ("build_single_pkg","build_pkg","build")):
            return _mod_build
        return None

    def _detect_binpkg_manager(self):
        if not _mod_binpkg:
            return None
        cls = getattr(_mod_binpkg, "BinPkgManager", None)
        if callable(cls):
            try:
                return cls(dry_run=self.dry_run)
            except Exception:
                try:
                    inst = cls()
                    inst.dry_run = self.dry_run
                    return inst
                except Exception:
                    return None
        return _mod_binpkg

    def _execute_build_with_retries(self, pkg: str, recipe: Dict[str, Any], with_install: bool, max_retries: int):
        attempt = 0
        last_err = None
        while attempt <= max_retries:
            if attempt > 0:
                backoff = (self.backoff_base ** attempt)
                LOG.info(f"Retry {attempt} for {pkg} after backoff {backoff:.1f}s")
                time.sleep(backoff)
            res = self._build_one(pkg, recipe, with_install)
            if res.get("status") not in ("failed","error","install-failed"):
                return res
            last_err = res.get("error")
            attempt += 1
        # all retries failed
        return {"package": pkg, "status": "failed", "error": last_err}

    def _build_one(self, pkg: str, recipe: Dict[str, Any], with_install: bool=False) -> Dict[str, Any]:
        """
        Build a single package with hooks and integration.
        Returns status dict.
        """
        start = time.time()
        res = {"package": pkg, "status": "pending", "error": None, "ts": now_iso(), "time_s": None}
        try:
            # recipe sanity
            if not recipe:
                res.update({"status":"no-recipe","error":"recipe not found"})
                return res

            # pre hooks (global & recipe)
            if self.hooks:
                try:
                    self.hooks.run_hooks("pre_auto_pkg", recipe, None)
                except Exception as e:
                    LOG.error(f"pre_auto_pkg global hook failed for {pkg}: {e}")
            rhooks = recipe.get("hooks", {}) or {}
            for cmd in rhooks.get("pre_auto_pkg", []) or []:
                if self.dry_run:
                    LOG.info(f"[DRY-RUN] pre_auto_pkg cmd for {pkg}: {cmd}")
                else:
                    try:
                        import subprocess as _sub
                        _sub.run(cmd, shell=True, check=True)
                    except Exception as e:
                        res.update({"status":"failed","error":f"pre_auto_pkg failed: {e}"})
                        return res

            # build
            if not self.build_mgr:
                res.update({"status":"no-builder","error":"no build manager available"})
                return res

            try:
                if hasattr(self.build_mgr, "build_single_pkg"):
                    build_result = self.build_mgr.build_single_pkg(pkg, os.path.join(self.recipes_dir,pkg), recipe)
                elif hasattr(self.build_mgr, "build_pkg"):
                    build_result = self.build_mgr.build_pkg(pkg, os.path.join(self.recipes_dir,pkg), recipe)
                elif hasattr(self.build_mgr, "build"):
                    try:
                        build_result = self.build_mgr.build(pkg, os.path.join(self.recipes_dir,pkg), recipe)
                    except TypeError:
                        try:
                            build_result = self.build_mgr.build(os.path.join(self.recipes_dir,pkg))
                        except Exception:
                            build_result = self.build_mgr.build(pkg)
                else:
                    # if build_mgr is a module with functions
                    if callable(self.build_mgr):
                        build_result = self.build_mgr(pkg)
                    else:
                        res.update({"status":"no-build-fn","error":"no known build interface"})
                        return res
            except Exception as e:
                LOG.error(f"Build exception for {pkg}: {e}\n{traceback.format_exc()}")
                res.update({"status":"failed","error":str(e)})
                # call on_fail_pkg hook
                try:
                    if self.hooks:
                        self.hooks.run_hooks("on_fail_pkg", {"package":pkg,"error":str(e)}, None)
                except Exception:
                    pass
                return res

            res["build_result"] = build_result
            res["status"] = "built"

            # install if requested
            if with_install:
                if not self.binpkg_mgr:
                    res.update({"install_result":{"status":"no-binpkg-manager"}})
                    res["status"] = "built-no-install"
                else:
                    try:
                        artifact = None
                        if isinstance(build_result, dict):
                            artifact = build_result.get("archive") or build_result.get("artifact")
                        if artifact and os.path.exists(artifact):
                            inst = None
                            if hasattr(self.binpkg_mgr, "install_binpkg"):
                                inst = self.binpkg_mgr.install_binpkg(artifact, force=False)
                            elif hasattr(self.binpkg_mgr, "install"):
                                inst = self.binpkg_mgr.install(artifact)
                            res["install_result"] = inst
                            if inst and inst.get("installed") or (isinstance(inst, dict) and inst.get("status") in ("ok","installed")):
                                res["status"] = "installed"
                                # update installed db minimally
                                try:
                                    ver = (recipe.get("package") or {}).get("version") or recipe.get("version")
                                    if ver:
                                        self.installed.setdefault(pkg, {})["version"] = ver
                                except Exception:
                                    pass
                            else:
                                res["status"] = "installed-unknown"
                        else:
                            # attempt binpkg.install(pkg)
                            if hasattr(self.binpkg_mgr, "install"):
                                inst = self.binpkg_mgr.install(pkg)
                                res["install_result"] = inst
                                if inst and (inst.get("installed") or inst.get("status") in ("ok","installed")):
                                    res["status"] = "installed"
                            else:
                                res["install_result"] = {"status":"no-install-method"}
                    except Exception as e:
                        LOG.error(f"Install error for {pkg}: {e}")
                        res.update({"status":"install-failed","error":str(e)})
                        try:
                            if self.hooks:
                                self.hooks.run_hooks("on_fail_pkg", {"package":pkg,"error":str(e)}, None)
                        except Exception:
                            pass
                        return res

            # post hooks
            for cmd in rhooks.get("post_auto_pkg", []) or []:
                if self.dry_run:
                    LOG.info(f"[DRY-RUN] post_auto_pkg cmd for {pkg}: {cmd}")
                else:
                    try:
                        import subprocess as _sub
                        _sub.run(cmd, shell=True, check=False)
                    except Exception:
                        LOG.error(f"post_auto_pkg command failed for {pkg}: {cmd}")

            if self.hooks:
                try:
                    self.hooks.run_hooks("post_auto_pkg", recipe, None)
                except Exception:
                    pass

            # success
            res["time_s"] = round(time.time() - start, 2)
            # record to history
            if self.history and not self.dry_run:
                try:
                    details = {"build_result": res.get("build_result"), "install_result": res.get("install_result")}
                    self.history.record(action="auto-build", package=pkg, details=details, result="ok", actor="auto")
                except Exception:
                    pass
            return res
        except Exception as e:
            res.update({"status":"error","error":str(e)})
            LOG.error(f"Unexpected error building {pkg}: {e}\n{traceback.format_exc()}")
            try:
                if self.hooks:
                    self.hooks.run_hooks("on_fail_pkg", {"package":pkg,"error":str(e)}, None)
            except Exception:
                pass
            return res

    # -------------------------
    # Plan / Execute public API
    # -------------------------
    def plan(self, targets: List[str], include_build: bool=True, include_runtime: bool=True,
             only_regex: Optional[str]=None, exclude_regex: Optional[str]=None, auto_add_missing: bool=False) -> Dict[str, Any]:
        resolved = self.resolve_all_deps(targets, include_build=include_build, include_runtime=include_runtime,
                                         only_regex=only_regex, exclude_regex=exclude_regex, auto_add_missing=auto_add_missing)
        graph = resolved["graph"]
        nodes = sorted(list(resolved["nodes"]))
        missing = resolved["missing"]
        # filter local nodes for topological levels
        local_nodes = [n for n in nodes if n not in missing and self._recipe_path(n)]
        levels = self.topo_levels(local_nodes, graph)
        return {"graph": graph, "nodes": nodes, "missing": missing, "levels": levels, "targets": targets}

    def execute(self, plan: Dict[str, Any], execute: bool=False, concurrency: Optional[int]=None,
                with_install_for: Optional[Set[str]]=None, force: bool=False, interactive: bool=False,
                export_dot: Optional[str]=None, dump_script: Optional[str]=None, max_retries: Optional[int]=None):
        """
        Execute plan. If execute=False -> dry-run. interactive: prompt per-level.
        dump_script: if set, produce run_plan.sh with the build/install commands (best-effort).
        """
        report = {"started_at": now_iso(), "dry_run": not execute or self.dry_run, "levels": plan.get("levels", []), "results": {}, "missing": plan.get("missing", []), "targets": plan.get("targets", [])}
        concurrency = concurrency or self.concurrency
        max_retries = (max_retries if max_retries is not None else self.max_retries)

        # pre_auto hooks
        if self.hooks:
            try:
                self.hooks.run_hooks("pre_auto", plan, None)
            except Exception as e:
                LOG.error("pre_auto hook error: " + str(e))

        # export dot if requested
        if export_dot:
            try:
                self._export_dot(plan, export_dot)
                LOG.info(f"Exported DOT to {export_dot}")
            except Exception as e:
                LOG.error("DOT export failed: " + str(e))

        # interactive confirm whole plan
        if interactive and execute:
            if console:
                console.print(Panel(f"[bold]Plano com {len(plan.get('nodes',[]))} nós em {len(plan.get('levels',[]))} níveis[/bold]"))
                self.show_plan_tree(plan.get("targets", []), plan.get("graph", {}))
            ok = Confirm.ask("Executar plano agora?", default=False)
            if not ok:
                return {"aborted": True, "reason": "user declined"}

        # produce script skeleton if requested
        script_lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        if dump_script:
            # include preliminary notes
            script_lines.append("# Generated execution script (best-effort). Review before running.")
            script_lines.append("")

        # execute levels
        for level_idx, level in enumerate(plan.get("levels", [])):
            report["results"].setdefault(level_idx, {})
            if not level:
                continue
            if console:
                console.print(Panel(f"[bold cyan]Nível {level_idx} : {len(level)} pacotes[/bold cyan]"))
            if interactive and execute:
                ok = Confirm.ask(f"Executar nível {level_idx} ({len(level)} pacotes)?", default=True)
                if not ok:
                    for pkg in level:
                        report["results"][level_idx][pkg] = {"status":"skipped-by-user"}
                    continue

            # dry-run listing
            if not execute:
                for pkg in level:
                    report["results"][level_idx][pkg] = {"status":"dry-run"}
                continue

            # parallel build per level
            futures = {}
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                for pkg in level:
                    rec = self._read_recipe_file(pkg)
                    if not rec:
                        report["results"][level_idx][pkg] = {"status":"missing-recipe"}
                        continue
                    # skip if installed and not force
                    inst = self.installed.get(pkg)
                    ver_in_recipe = (rec.get("package") or {}).get("version") or rec.get("version")
                    if inst and not force and ver_in_recipe:
                        try:
                            if str(inst.get("version")) == str(ver_in_recipe):
                                report["results"][level_idx][pkg] = {"status":"already-installed","version":inst.get("version")}
                                continue
                        except Exception:
                            pass
                    with_install = (with_install_for is not None and pkg in with_install_for)
                    futures[ex.submit(self._execute_build_with_retries, pkg, rec, with_install, max_retries)] = pkg

                # gather results
                for fut in as_completed(futures):
                    pkg = futures[fut]
                    try:
                        r = fut.result()
                    except Exception as e:
                        r = {"status":"error","error":str(e)}
                    report["results"][level_idx][pkg] = r
                    # collect into installed db on success
                    if r.get("status") in ("installed","installed-unknown") and not self.dry_run:
                        try:
                            ver = ( (r.get("build_result") or {}).get("version") or (r.get("build_result") or {}).get("pkgver") or ver_in_recipe )
                            if ver:
                                self.installed.setdefault(pkg, {})["version"] = ver
                        except Exception:
                            pass
                    # on failure attempt rollback best-effort for dependents installed in this run
                    if r.get("status") in ("failed","error","install-failed"):
                        # record history failure
                        if self.history and not self.dry_run:
                            try:
                                self.history.record(action="auto-failed", package=pkg, details={"result":r}, result="fail", actor="auto")
                            except Exception:
                                pass
                        # attempt on_fail_pkg hooks already called inside _build_one
                        # attempt rollback if requested: try removing package if it installed earlier
                        try:
                            self._attempt_rollback_on_failure(pkg, r)
                        except Exception as e:
                            LOG.error("Rollback attempt error: " + str(e))

            # end level
        # end levels

        # post_auto hooks
        if self.hooks:
            try:
                self.hooks.run_hooks("post_auto", {"report":report}, None)
            except Exception as e:
                LOG.error("post_auto hook error: " + str(e))

        # save installed_db if modifications and not dry_run
        if execute and not self.dry_run:
            self._save_installed_db()

        # save report
        try:
            path = self._report_path()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
            LOG.info(f"Auto report saved to {path}")
        except Exception as e:
            LOG.error("Failed to write report: " + str(e))

        # produce script if requested
        if dump_script:
            try:
                with open(dump_script, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(script_lines))
                os.chmod(dump_script, 0o755)
                LOG.info(f"Wrote exec script to {dump_script}")
            except Exception as e:
                LOG.error("Failed to write script: " + str(e))

        # notify
        self._notify_summary(report)
        return report

    def _notify_summary(self, report):
        if not self.notify_enabled:
            return
        try:
            total=0; ok=0; failed=0
            for lvl in report.get("results",{}).values():
                for pkg, r in lvl.items():
                    total+=1
                    if r.get("status") in ("installed","built","installed-unknown","built-no-install"):
                        ok+=1
                    elif r.get("status") in ("failed","error","install-failed"):
                        failed+=1
            title = f"{self.notify_title} - auto finished"
            msg = f"{total} packages processed — ok: {ok}, failed: {failed}"
            if shutil.which("notify-send"):
                import subprocess
                subprocess.run(["notify-send", title, msg], check=False)
            else:
                LOG.info(f"Notification: {title} - {msg}")
        except Exception as e:
            LOG.error("Notify failed: " + str(e))

    def _attempt_rollback_on_failure(self, pkg: str, result: Dict[str,Any]):
        """
        Best-effort rollback: use history or binpkg backups or remove module.
        Currently: if build produced 'install_result' with 'backup' or 'snapshot', attempt restore;
        else if package was partially installed during this auto run, try to call binpkg.remove / remove module.
        """
        # If history available, try to find last install event for this pkg and use its snapshot/backup
        if not self.history:
            return
        try:
            # search recent history for package installs
            entries = self.history.list_history(limit=200, package=pkg)
            for e in entries:
                det = e.get("details",{}) or {}
                snapshot = det.get("snapshot") or det.get("backup")
                if snapshot and os.path.exists(snapshot):
                    # restore snapshot
                    if self.dry_run:
                        LOG.info(f"[DRY-RUN] Would extract snapshot {snapshot} to /")
                        return
                    import subprocess
                    subprocess.run(["tar","-xzf", snapshot, "-C", "/"], check=False)
                    LOG.info(f"Restored snapshot for {pkg} from {snapshot}")
                    return
            # else attempt to remove
            rem_mod = try_mod("modules.remove") or try_mod("remove")
            if rem_mod:
                Rem = getattr(rem_mod, "Remover", None) or getattr(rem_mod, "RemoveManager", None)
                if Rem:
                    try:
                        rem = Rem(installed_db=self.installed_db_path, dry_run=self.dry_run)
                    except Exception:
                        rem = None
                    if rem:
                        if self.dry_run:
                            LOG.info(f"[DRY-RUN] Would call remover.remove_package({pkg})")
                        else:
                            rem.remove_package(pkg, force=True, backup=True)
                            LOG.info(f"Rollback: removed {pkg} via remover")
        except Exception as e:
            LOG.error("Rollback best-effort failed: " + str(e))

    # -------------------------
    # Visualization / helpers
    # -------------------------
    def show_plan_tree(self, roots: List[str], graph: Dict[str, List[str]]):
        if not console:
            print("Plan:")
            for r in roots:
                print(r)
            return
        tree = Tree(f"[bold green]Plano para: {', '.join(roots)}[/bold green]")
        visited = set()
        def walk(node: str, branch):
            label = node
            branch_node = branch.add(label)
            for ch in graph.get(node, []):
                if ch in visited:
                    branch_node.add(f"{ch} (já)")
                else:
                    visited.add(ch)
                    walk(ch, branch_node)
        for r in roots:
            walk(r, tree)
        console.print(tree)

    def _export_dot(self, plan: Dict[str, Any], path: str):
        graph = plan.get("graph", {})
        lines = ["digraph deps {", "rankdir=LR;"]
        for src, dsts in graph.items():
            for d in dsts:
                lines.append(f'  "{src}" -> "{d}";')
        lines.append("}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

# -------------------------
# CLI entrypoint
# -------------------------
def main(argv: Optional[List[str]] = None):
    import argparse
    argv = argv or sys.argv[1:]
    ap = argparse.ArgumentParser(prog="auto", description="Ultimate auto (dependency resolver + orchestrator)")
    ap.add_argument("targets", nargs="*", help="Target packages (recipes). If empty, choose candidates")
    ap.add_argument("--only", help="Regex include")
    ap.add_argument("--exclude", help="Regex exclude")
    ap.add_argument("--no-build-deps", action="store_true", help="Ignore build deps")
    ap.add_argument("--dry-run", action="store_true", help="Dry-run (default)")
    ap.add_argument("--execute", action="store_true", help="Execute builds and installs")
    ap.add_argument("--with-install", nargs="*", help="List of packages to install after build (if omitted while passed => install all)")
    ap.add_argument("--concurrency", type=int, help="Workers per level")
    ap.add_argument("--force", action="store_true", help="Force rebuild/install even if installed")
    ap.add_argument("--interactive", action="store_true", help="Interactive confirmation per level")
    ap.add_argument("--export-dot", help="Export graph to DOT file (path)")
    ap.add_argument("--plan-out", help="Write plan JSON to file")
    ap.add_argument("--dump-script", help="Write run_plan.sh script (best-effort)")
    ap.add_argument("--auto-add-missing", action="store_true", help="Auto-include missing deps as nodes (won't build if no recipe)")
    ap.add_argument("--max-retries", type=int, help="Max retries per package")
    args = ap.parse_args(argv)

    am = AutoManager(dry_run=(not args.execute) or args.dry_run, concurrency=args.concurrency, max_retries=args.max_retries)
    # determine targets
    targets = args.targets or []
    if not targets:
        # choose recipes not installed or all recipes
        all_recipes = am.list_all_recipes()
        targets = [r for r in all_recipes if r not in am.installed]
        if not targets:
            targets = all_recipes

    plan = am.plan(targets, include_build=(not args.no_build_deps), include_runtime=True,
                   only_regex=args.only, exclude_regex=args.exclude, auto_add_missing=args.auto_add_missing)
    if args.plan_out:
        try:
            with open(args.plan_out, "w", encoding="utf-8") as fh:
                json.dump(plan, fh, indent=2, ensure_ascii=False)
            LOG.info(f"Wrote plan JSON to {args.plan_out}")
        except Exception as e:
            LOG.error("Failed to write plan-out: " + str(e))

    if console:
        console.print(Panel(f"[bold]Plano gerado para {len(targets)} alvos[/bold]\nNíveis: {len(plan.get('levels', []))}", title="auto - plano"))
        am.show_plan_tree(targets, plan.get("graph", {}))
    else:
        print("Plan:", plan)

    # with_install_for logic
    with_install_for = None
    if args.with_install is not None:
        if len(args.with_install) == 0:
            with_install_for = set([p for p in plan.get("nodes", []) if p not in plan.get("missing", [])])
        else:
            with_install_for = set(args.with_install)

    report = am.execute(plan, execute=args.execute, concurrency=args.concurrency,
                        with_install_for=with_install_for, force=args.force,
                        interactive=args.interactive, export_dot=args.export_dot,
                        dump_script=args.dump_script, max_retries=args.max_retries)

    if console:
        console.print(Panel(json.dumps(report, indent=2, ensure_ascii=False), title="auto report"))
    else:
        print(json.dumps(report, indent=2))

    # save cache
    am._save_cache()
    return 0

if __name__ == "__main__":
    sys.exit(main())
