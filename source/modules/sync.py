# source/modules/sync.py
"""
sync.py - sincronização de recipes a partir de repositórios Git.

- Lê configuração de source.conf (em /etc/sources/ ou local).
- Suporta múltiplos remotos (mas principal é "origin").
- Clona ou atualiza o repositório para /usr/sources (ou caminho configurado).
- Recipes ficam disponíveis em subpastas do repositório.
- Logs são salvos via Logger.
- Integra com hooks globais (pre_sync, post_sync).
- Modo dry-run para testar sem alterar nada.
"""

import os
import sys
import subprocess
import configparser
import shutil
import tempfile
from datetime import datetime
from typing import Optional, Dict, Any

from modules import logger as _logger
from modules import hooks as _hooks


class SyncError(Exception):
    pass


class SyncManager:
    def __init__(self,
                 config_file: str = "/etc/sources/source.conf",
                 dry_run: bool = False):
        self.config_file = config_file
        self.dry_run = dry_run
        self.log = _logger.Logger("sync.log")
        try:
            self.hooks = _hooks.HookManager(dry_run=dry_run)
        except Exception:
            self.hooks = None

        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        cfg = configparser.ConfigParser()
        if not os.path.exists(self.config_file):
            raise SyncError(f"Config file not found: {self.config_file}")

        cfg.read(self.config_file)
        repo_url = cfg.get("sync", "repo_url", fallback="https://github.com/fcanata061/sources.git")
        branch = cfg.get("sync", "branch", fallback="main")
        dest_dir = cfg.get("sync", "dest_dir", fallback="/usr/sources")

        return {
            "repo_url": repo_url,
            "branch": branch,
            "dest_dir": os.path.abspath(dest_dir)
        }

    def _run(self, cmd, cwd=None):
        if self.dry_run:
            self.log.info(f"[DRY-RUN] Would run: {cmd}")
            return 0
        self.log.debug(f"Running: {cmd} (cwd={cwd})")
        res = subprocess.run(cmd, shell=True, cwd=cwd,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise SyncError(f"Command failed: {cmd}\nstdout: {res.stdout}\nstderr: {res.stderr}")
        return res.stdout.strip()

    def sync(self, force_reset: bool = False) -> str:
        """
        Clona ou atualiza o repositório configurado em source.conf para o diretório dest_dir.
        Executa hooks globais pre_sync e post_sync.
        Retorna caminho do diretório sincronizado.
        """
        repo_url = self.config["repo_url"]
        branch = self.config["branch"]
        dest_dir = self.config["dest_dir"]

        # pre-sync hooks
        if self.hooks:
            try:
                self.hooks.run_hooks("pre_sync", {"repo_url": repo_url}, None)
            except Exception as e:
                self.log.error(f"pre_sync hook failed: {e}")

        if not os.path.exists(dest_dir):
            self.log.info(f"Destination {dest_dir} does not exist, cloning repository...")
            if not self.dry_run:
                os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
            cmd = f"git clone --branch {branch} {repo_url} {dest_dir}"
            self._run(cmd)
        else:
            if force_reset:
                self.log.info("Force reset enabled, cleaning repository...")
                self._run("git fetch --all", cwd=dest_dir)
                self._run(f"git reset --hard origin/{branch}", cwd=dest_dir)
            else:
                self.log.info("Updating existing repository...")
                self._run("git fetch --all", cwd=dest_dir)
                self._run(f"git checkout {branch}", cwd=dest_dir)
                self._run(f"git pull origin {branch}", cwd=dest_dir)

        # save timestamp
        ts_file = os.path.join(dest_dir, ".last_sync")
        if not self.dry_run:
            with open(ts_file, "w", encoding="utf-8") as f:
                f.write(datetime.utcnow().isoformat() + "Z")
        else:
            self.log.info(f"[DRY-RUN] Would update timestamp file {ts_file}")

        # post-sync hooks
        if self.hooks:
            try:
                self.hooks.run_hooks("post_sync", {"repo_url": repo_url}, dest_dir)
            except Exception as e:
                self.log.error(f"post_sync hook failed: {e}")

        self.log.info(f"Repository synced to {dest_dir}")
        return dest_dir

    def list_recipes(self) -> Dict[str, str]:
        """
        Lista as recipes disponíveis no diretório sincronizado.
        Retorna dict {recipe_name: path}
        """
        dest_dir = self.config["dest_dir"]
        if not os.path.exists(dest_dir):
            raise SyncError("Destination directory not found, run sync first.")

        recipes = {}
        for root, dirs, files in os.walk(dest_dir):
            for fn in files:
                if fn.endswith(".recipe") or fn == "recipe.json":
                    path = os.path.join(root, fn)
                    name = os.path.relpath(root, dest_dir)
                    recipes[name] = path
        return recipes


def main_cli(argv=None):
    import argparse
    argv = argv or sys.argv[1:]
    mgr = SyncManager()

    ap = argparse.ArgumentParser(prog="sync", description="Sync recipes from Git repository")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("run", help="Perform sync from configured repo")
    p_sync.add_argument("--force", action="store_true", help="Force reset local repo")

    p_list = sub.add_parser("list", help="List available recipes after sync")

    args = ap.parse_args(argv)

    if args.cmd == "run":
        path = mgr.sync(force_reset=args.force)
        print("Synced to:", path)
        return 0

    if args.cmd == "list":
        recs = mgr.list_recipes()
        for name, path in recs.items():
            print(f"{name}: {path}")
        return 0


if __name__ == "__main__":
    main_cli()
