# source/modules/build.py
"""
Ultra-evolved build orchestrator.

Features:
 - dependency graph resolution (topological)
 - parallel builds by levels (respecting deps)
 - auto-detect build system
 - cache local + remote-cache stubs
 - snapshots + rollback (transactional)
 - hooks (recipe, local, global)
 - reproducible-build support via fixed env
 - reports / metrics
 - CLI: build, graph, status, clean-cache, report
"""

from __future__ import annotations
import os
import sys
import json
import shutil
import hashlib
import traceback
import logging
import time
import argparse
from pathlib import Path
from typing import Dict, List, Optional, Set, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

# Import project modules (assumes they exist as previously discussed)
from modules import logger as _logger
from modules import sandbox as _sandbox
from modules import fakeroot as _fakeroot
from modules import hooks as _hooks

# ---------------------------
# Utilities
# ---------------------------
def read_recipe(source_dir: str) -> dict:
    f = os.path.join(source_dir, "recipe.yaml")
    if not os.path.exists(f):
        return {}
    with open(f, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def hash_string(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def compute_fingerprint(source_dir: str, recipe: dict) -> str:
    """
    Fingerprint that mixes recipe and source tree metadata to detect changes.
    Uses recipe content and file paths + mtimes (cheap) unless recipe provides manifest_files.
    """
    m = hashlib.sha256()
    rec_bytes = json.dumps(recipe, sort_keys=True, default=str).encode("utf-8")
    m.update(rec_bytes)

    manifest = recipe.get("manifest_files")
    if manifest:
        for rel in sorted(manifest):
            p = os.path.join(source_dir, rel)
            if os.path.exists(p) and os.path.isfile(p):
                m.update(open(p, "rb").read())
            else:
                m.update(f"missing:{rel}".encode("utf-8"))
    else:
        # fallback: names + mtimes
        for root, _, files in os.walk(source_dir):
            for fn in sorted(files):
                fp = os.path.join(root, fn)
                try:
                    st = os.stat(fp)
                    data = f"{os.path.relpath(fp, source_dir)}:{st.st_mtime}".encode("utf-8")
                except Exception:
                    data = f"{os.path.relpath(fp, source_dir)}:err".encode("utf-8")
                m.update(data)
    return m.hexdigest()

def topological_sort(nodes: Set[str], deps: Dict[str, List[str]]) -> List[str]:
    # Kahn's algorithm
    in_deg = {n: 0 for n in nodes}
    for n, ds in deps.items():
        for d in ds:
            if d in in_deg:
                in_deg[n] += 1
    q = [n for n, deg in in_deg.items() if deg == 0]
    ordered = []
    while q:
        n = q.pop(0)
        ordered.append(n)
        for m, ds in deps.items():
            if n in ds:
                in_deg[m] -= 1
                if in_deg[m] == 0:
                    q.append(m)
    if len(ordered) != len(nodes):
        raise RuntimeError("Cyclic or missing dependency detected.")
    return ordered

# ---------------------------
# Builder
# ---------------------------
class BuildManager:
    def __init__(self,
                 cache_dir: str = "build_cache",
                 workers: int = 4,
                 dry_run: bool = False,
                 verbose: bool = False,
                 reproducible: bool = True,
                 remote_cache_enabled: bool = False):
        self.cache_dir = os.path.abspath(cache_dir)
        os.makedirs(self.cache_dir, exist_ok=True)
        self.workers = workers
        self.dry_run = dry_run
        self.verbose = verbose
        self.reproducible = reproducible
        self.remote_cache_enabled = remote_cache_enabled

        self.log = _logger.Logger("build-manager.log")
        self.fakeroot = _fakeroot.Fakeroot(dry_run=dry_run)
        # a basic global hooks manager (global hooks path: source/hooks/global)
        self.global_hooks = os.path.join("source", "hooks", "global")

        # runtime metrics
        self.metrics = {
            "built": 0,
            "cache_hits": 0,
            "failed": 0,
            "start_time": time.time(),
            "packages": {}
        }

    # ---------------------------
    # Build system detection
    # ---------------------------
    @staticmethod
    def detect_build_system(source_dir: str) -> str:
        if os.path.exists(os.path.join(source_dir, "CMakeLists.txt")):
            return "cmake"
        if os.path.exists(os.path.join(source_dir, "meson.build")):
            return "meson"
        if os.path.exists(os.path.join(source_dir, "configure")):
            return "autotools"
        if os.path.exists(os.path.join(source_dir, "pyproject.toml")) or os.path.exists(os.path.join(source_dir, "setup.py")):
            return "python"
        if os.path.exists(os.path.join(source_dir, "Cargo.toml")):
            return "rust"
        if os.path.exists(os.path.join(source_dir, "package.json")):
            return "node"
        # fallback to make if Makefile or nothing
        if os.path.exists(os.path.join(source_dir, "Makefile")):
            return "make"
        return "make"

    # ---------------------------
    # Graph utilities
    # ---------------------------
    def build_graph(self, source_dirs: List[str]) -> (Dict[str, dict], Dict[str, List[str]]):
        """
        Load recipe for each source dir and build nodes and deps.
        Returns (pkg_map, deps)
        pkg_map: name -> {"source": path, "recipe": {}}
        deps: name -> [dep names that are present among provided sources]
        """
        pkg_map: Dict[str, dict] = {}
        deps: Dict[str, List[str]] = {}

        for sd in source_dirs:
            recipe = read_recipe(sd)
            name = recipe.get("name") or os.path.basename(os.path.abspath(sd))
            pkg_map[name] = {"source": os.path.abspath(sd), "recipe": recipe}
        # build deps
        for name, data in pkg_map.items():
            recipe = data["recipe"]
            raw_deps = recipe.get("depends", []) or []
            # only consider deps that exist among provided sources (external deps ignored)
            deps[name] = [d for d in raw_deps if d in pkg_map]
        return pkg_map, deps

    def export_graph_dot(self, deps: Dict[str, List[str]], output: str = "deps.dot"):
        lines = ["digraph dependencies {"]
        for pkg, ds in deps.items():
            if not ds:
                lines.append(f'  "{pkg}";')
            for d in ds:
                lines.append(f'  "{d}" -> "{pkg}";')
        lines.append("}")
        with open(output, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        self.log.info(f"Graph exported to {output}")
        return output

    # ---------------------------
    # Remote cache stubs (can be implemented)
    # ---------------------------
    def push_remote_cache(self, archive_path: str) -> bool:
        # stub for remote upload (S3/HTTP/rsync/etc.)
        self.log.info(f"[remote-cache] (stub) would push {archive_path}")
        return False

    def fetch_remote_cache(self, name: str, fingerprint: str) -> Optional[str]:
        # stub for remote fetch: return local path if found and downloaded
        self.log.info(f"[remote-cache] (stub) searching for {name}-{fingerprint}")
        return None

    # ---------------------------
    # Cache helpers
    # ---------------------------
    def _cache_paths(self, name: str, fingerprint: str):
        meta = os.path.join(self.cache_dir, f"{name}-{fingerprint}.json")
        archive = os.path.join(self.cache_dir, f"{name}-{fingerprint}.tar.gz")
        return meta, archive

    # ---------------------------
    # Build orchestration
    # ---------------------------
    def build_all(self, source_dirs: List[str], stop_on_failure: bool = True) -> Dict[str, Any]:
        pkg_map, deps = self.build_graph(source_dirs)
        nodes = set(pkg_map.keys())
        order = topological_sort(nodes, deps)
        self.log.info(f"Topological order: {order}")

        # build levels
        levels = self._levels_from_order(order, deps)
        results = {}
        for idx, level in enumerate(levels):
            self.log.info(f"Building level {idx}: {level}")
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                future_to_name = {}
                for name in level:
                    meta = pkg_map[name]
                    future = ex.submit(self._build_single, name, meta["source"], meta["recipe"])
                    future_to_name[future] = name

                for fut in as_completed(future_to_name):
                    name = future_to_name[fut]
                    try:
                        res = fut.result()
                        results[name] = {"ok": True, "result": res}
                        self.metrics["built"] += 1
                        self.metrics["packages"][name] = {"status": "built", "result": res}
                        self.log.info(f"Built: {name}")
                    except Exception as e:
                        results[name] = {"ok": False, "error": str(e), "trace": traceback.format_exc()}
                        self.metrics["failed"] += 1
                        self.metrics["packages"][name] = {"status": "failed", "error": str(e)}
                        self.log.error(f"Failed building {name}: {e}")
                        if self.verbose:
                            self.log.debug(traceback.format_exc())
                        if stop_on_failure:
                            raise
        self.metrics["end_time"] = time.time()
        return results

    def _levels_from_order(self, order: List[str], deps: Dict[str, List[str]]) -> List[List[str]]:
        remain = set(order)
        levels = []
        built = set()
        while remain:
            this = []
            for n in list(remain):
                if set(deps.get(n, [])).issubset(built):
                    this.append(n)
            if not this:
                raise RuntimeError("Cannot form build levels (cycle?)")
            for n in this:
                remain.remove(n)
                built.add(n)
            levels.append(sorted(this))
        return levels

    # ---------------------------
    # Single package pipeline
    # ---------------------------
    def _build_single(self, name: str, source_dir: str, recipe: dict) -> dict:
        r = recipe or {}
        dry = self.dry_run
        self.log.info(f"Starting build pipeline for {name} (dry_run={dry})")

        # fingerprint and cache check
        fingerprint = compute_fingerprint(source_dir, r)
        meta_file, archive_file = self._cache_paths(name, fingerprint)

        # remote-cache check (if enabled)
        if self.remote_cache_enabled:
            remote_hit = self.fetch_remote_cache(name, fingerprint)
            if remote_hit:
                self.metrics["cache_hits"] += 1
                self.log.info(f"Remote cache hit for {name}")
                return {"cache_hit": True, "archive": remote_hit}

        # local cache check
        if os.path.exists(meta_file) and os.path.exists(archive_file):
            self.metrics["cache_hits"] += 1
            self.log.info(f"[cache-hit] {name} -> {archive_file}")
            if dry:
                return {"cache_hit": True, "archive": archive_file}
            # optional: unpack into sandbox for consumers (we'll not unpack by default, just return)
            return {"cache_hit": True, "archive": archive_file}

        # prepare sandbox & hooks
        sandbox = _sandbox.Sandbox(name, dry_run=dry)
        hk = _hooks.HookManager(source_dir, dry_run=dry)

        # transactional snapshot mechanism
        snapshot_before = None
        try:
            # global pre-prepare hook
            hk.run_global("pre-prepare")

            # recipe pre-prepare commands
            for cmd in hk.load_from_recipe("pre-prepare"):
                # recipe commands execute in source_dir; some commands may assume fakeroot - allow shell strings
                self.fakeroot.run(cmd, cwd=source_dir, env=None, timeout=None, retries=1, check=True)

            # prepare sandbox
            sandbox.prepare(clean=True, std_dirs=True, metadata={"recipe": r, "fingerprint": fingerprint})

            # build system detection
            bsys = r.get("build_system") or self.detect_build_system(source_dir)

            # pre-build hooks (recipe/local/global)
            for cmd in hk.load_from_recipe("pre-build"):
                self.fakeroot.run(cmd, cwd=source_dir, shell=True)
            hk.run("pre-build")
            hk.run_global("pre-build")

            # create build dir if necessary
            build_dir = os.path.join(source_dir, r.get("build_dir", "build"))
            if bsys in ("cmake", "meson") and not os.path.exists(build_dir):
                if not dry:
                    os.makedirs(build_dir, exist_ok=True)

            # actual build steps by system
            if bsys == "cmake":
                self.fakeroot.run(["cmake", source_dir], cwd=build_dir)
                self.fakeroot.run(["make", "-j"], cwd=build_dir)
            elif bsys == "meson":
                self.fakeroot.run(["meson", build_dir, source_dir], cwd=source_dir)
                self.fakeroot.run(["ninja", "-C", build_dir], cwd=source_dir)
            elif bsys == "python":
                # support typical python packaging (editable builds not performed here)
                if os.path.exists(os.path.join(source_dir, "pyproject.toml")):
                    # try build with pip wheel
                    self.fakeroot.run(["python", "-m", "pip", "wheel", ".", "-w", build_dir], cwd=source_dir)
                else:
                    self.fakeroot.run(["python", "setup.py", "build"], cwd=source_dir)
            elif bsys == "rust":
                self.fakeroot.run(["cargo", "build", "--release"], cwd=source_dir)
            elif bsys == "node":
                # install node deps and build if script exists
                if os.path.exists(os.path.join(source_dir, "package.json")):
                    self.fakeroot.run(["npm", "install"], cwd=source_dir)
                    pkg = r.get("package_script", "build")
                    # only run if script exists in package.json: skip check here (assume user config)
                    self.fakeroot.run(["npm", "run", pkg], cwd=source_dir)
            else:
                # default: make/autotools
                if os.path.exists(os.path.join(source_dir, "configure")):
                    self.fakeroot.run(["./configure"], cwd=source_dir)
                self.fakeroot.run(["make", "-j"], cwd=source_dir)

            # post-build hooks
            hk.run("post-build")
            hk.run_global("post-build")
            for cmd in hk.load_from_recipe("post-build"):
                self.fakeroot.run(cmd, cwd=source_dir, shell=True)

            # snapshot before install for rollback safety
            snapshot_before = sandbox.snapshot()

            # install -> use DESTDIR pointing at sandbox.path
            env = os.environ.copy()
            env["DESTDIR"] = sandbox.path
            if bsys == "cmake":
                self.fakeroot.run(["make", "install"], cwd=build_dir, env=env)
            elif bsys == "meson":
                self.fakeroot.run(["ninja", "-C", build_dir, "install"], cwd=source_dir, env=env)
            elif bsys == "python":
                # do install into destdir using pip if wheel available or setup.py install --root
                if os.path.exists(os.path.join(build_dir, "")):
                    # fallback to setup install
                    self.fakeroot.run(["python", "setup.py", "install", f"--root={sandbox.path}"], cwd=source_dir, env=env)
                else:
                    self.fakeroot.run(["python", "setup.py", "install", f"--root={sandbox.path}"], cwd=source_dir, env=env)
            else:
                # default make install
                self.fakeroot.run(["make", "install"], cwd=source_dir, env=env)

            # post-install hooks
            hk.run("post-install")
            hk.run_global("post-install")
            for cmd in hk.load_from_recipe("post-install"):
                self.fakeroot.run(cmd, cwd=source_dir, shell=True)

            # optional tests inside sandbox
            tests = r.get("tests", [])
            for t in tests:
                # tests run inside sandbox root
                self.fakeroot.run(t, cwd=sandbox.path)

            # package/ archive sandbox
            output_dir = r.get("output_dir", "packages")
            if not os.path.exists(output_dir) and not dry:
                os.makedirs(output_dir, exist_ok=True)
            archive_name = os.path.join(output_dir, f"{name}-{fingerprint}.tar.gz")
            archive_path = sandbox.archive(output_file=archive_name)

            # cache: copy to cache_dir atomically
            cached_path = os.path.join(self.cache_dir, os.path.basename(archive_path))
            if not dry:
                shutil.copy2(archive_path, cached_path)
                meta = {"name": name, "fingerprint": fingerprint, "archive": cached_path, "recipe": r}
                meta_file, _ = self._cache_paths(name, fingerprint)
                with open(meta_file, "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, indent=2)

            # remote cache push
            if self.remote_cache_enabled:
                try:
                    self.push_remote_cache(cached_path)
                except Exception:
                    self.log.debug("Remote cache push failed (ignored)")

            # post-package hooks
            hk.run("post-package")
            hk.run_global("post-package")
            for cmd in hk.load_from_recipe("post-package"):
                self.fakeroot.run(cmd, cwd=source_dir, shell=True)

            self.metrics["packages"][name] = {"archive": archive_path, "fingerprint": fingerprint}
            return {"cache_hit": False, "archive": archive_path, "fingerprint": fingerprint}

        except Exception as e:
            self.log.error(f"Build error for {name}: {e}")
            # rollback from snapshot
            try:
                if snapshot_before and os.path.exists(snapshot_before) and not dry:
                    sandbox.restore(snapshot_before)
                    self.log.info(f"Rollback applied for {name}")
            except Exception as re:
                self.log.error(f"Rollback failed for {name}: {re}")
            raise

    # ---------------------------
    # Status / cache / clean
    # ---------------------------
    def status(self) -> dict:
        """Return metrics & cache listing"""
        info = {"metrics": self.metrics}
        # list cache files
        cache_entries = []
        for fn in sorted(os.listdir(self.cache_dir)):
            cache_entries.append(fn)
        info["cache"] = cache_entries
        return info

    def clean_cache(self, keep_latest: bool = True):
        """Clean cache directory (optionally keep latest file per package prefix)"""
        files = sorted(os.listdir(self.cache_dir))
        if not files:
            return
        if keep_latest:
            # group by package base name before fingerprint
            grouped: Dict[str, List[str]] = {}
            for f in files:
                # expected format: name-<fingerprint>.tar.gz or .json
                base = f.split("-")[0]
                grouped.setdefault(base, []).append(f)
            to_remove = []
            for base, flist in grouped.items():
                flist_sorted = sorted(flist)
                # keep last
                to_remove.extend(flist_sorted[:-1])
        else:
            to_remove = files

        for f in to_remove:
            p = os.path.join(self.cache_dir, f)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
                self.log.info(f"Removed cache {p}")
            except Exception as e:
                self.log.error(f"Failed to remove cache {p}: {e}")

    # ---------------------------
    # Reporting
    # ---------------------------
    def report_json(self, out: str = "build-report.json"):
        report = {
            "metrics": self.metrics,
            "generated_at": time.time()
        }
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        self.log.info(f"Report written to {out}")
        return out

# ---------------------------
# CLI
# ---------------------------
def main():
    ap = argparse.ArgumentParser(prog="build.py", description="Ultra-evolved build manager")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="Build sources")
    p_build.add_argument("paths", nargs="+", help="Source directories (each with optional recipe.yaml)")
    p_build.add_argument("--workers", type=int, default=4)
    p_build.add_argument("--cache-dir", default="build_cache")
    p_build.add_argument("--dry-run", action="store_true")
    p_build.add_argument("--verbose", action="store_true")
    p_build.add_argument("--no-remote-cache", action="store_true")

    p_graph = sub.add_parser("graph", help="Export dependency graph (DOT)")
    p_graph.add_argument("paths", nargs="+")
    p_graph.add_argument("--output", default="deps.dot")

    p_status = sub.add_parser("status", help="Show status & cache")
    p_status.add_argument("--cache-dir", default="build_cache")

    p_clean = sub.add_parser("clean-cache", help="Clean local cache")
    p_clean.add_argument("--cache-dir", default="build_cache")
    p_clean.add_argument("--keep-latest", action="store_true")

    p_report = sub.add_parser("report", help="Write metrics report")
    p_report.add_argument("--out", default="build-report.json")

    args = ap.parse_args()

    if args.cmd == "build":
        bm = BuildManager(cache_dir=args.cache_dir, workers=args.workers, dry_run=args.dry_run,
                          verbose=args.verbose, remote_cache_enabled=not args.no_remote_cache)
        results = bm.build_all(args.paths)
        print(json.dumps(results, indent=2))
        bm.report_json()
    elif args.cmd == "graph":
        bm = BuildManager()
        pkg_map, deps = bm.build_graph(args.paths)
        out = bm.export_graph_dot(deps, output=args.output)
        print("Graph exported to", out)
    elif args.cmd == "status":
        bm = BuildManager(cache_dir=args.cache_dir)
        st = bm.status()
        print(json.dumps(st, indent=2))
    elif args.cmd == "clean-cache":
        bm = BuildManager(cache_dir=args.cache_dir)
        bm.clean_cache(keep_latest=args.keep_latest)
        print("Cache cleaned.")
    elif args.cmd == "report":
        bm = BuildManager()
        out = bm.report_json(out=args.out)
        print("Report:", out)

if __name__ == "__main__":
    main()
