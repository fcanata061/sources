# source/modules/remove.py
"""
Remover: remoção segura de pacotes instalados.

Formato esperado do installed_db (JSON):
{
  "<pkgname>": {
      "name": "<pkgname>",
      "version": "1.0",
      "files": ["/usr/bin/foo", "/usr/lib/libfoo.so", ...],
      "depends": ["dep1", "dep2"],    # dependências do pacote
      "recipe": { ... },              # opcional: recipe metadata
      "installed_at": "2025-09-19T..."
  },
  ...
}

Este módulo não força um formato estrito, mas trabalha melhor se o DB seguir esta estrutura.
"""

from __future__ import annotations
import os
import json
import time
import tarfile
from datetime import datetime
from typing import Dict, List, Optional, Any

# importa os módulos do projeto (assume que existem conforme discutido)
from modules import logger, sandbox, fakeroot, hooks, recipe


class RemoveError(Exception):
    pass


class Remover:
    def __init__(self, installed_db: str = "installed_db.json", dry_run: bool = False, backups_dir: str = "removals"):
        """
        installed_db: caminho para JSON com estado de pacotes instalados, ou um dict já carregado.
        dry_run: se True -> apenas loga ações, não executa.
        backups_dir: onde salvar backups antes de remover.
        """
        self.log = logger.Logger("remover.log")
        self.dry_run = dry_run
        self.backups_dir = os.path.abspath(backups_dir)
        os.makedirs(self.backups_dir, exist_ok=True)

        # carregar DB
        if isinstance(installed_db, dict):
            self.db_path = None
            self._db = installed_db
        else:
            self.db_path = os.path.abspath(installed_db)
            if os.path.exists(self.db_path):
                with open(self.db_path, "r", encoding="utf-8") as fh:
                    try:
                        self._db = json.load(fh)
                    except Exception:
                        self._db = {}
            else:
                self._db = {}

        # instanciar fakeroot (tenta suporte à classe com nomes diferentes)
        try:
            self.fakeroot = fakeroot.Fakeroot(dry_run=self.dry_run)
        except Exception:
            # fallback: busca outro nome
            try:
                self.fakeroot = fakeroot.FakeRoot(dry_run=self.dry_run)
            except Exception:
                # se não existir, cria um executor minimal (exec sem fakeroot)
                self.log.info("Nenhum Fakeroot custom encontrado — fallback para execução direta (pode exigir root).")
                class _LocalExec:
                    def __init__(self, dry_run=False):
                        self.dry_run = dry_run
                        self.log = logger.Logger("fakeroot-fallback.log")
                    def run(self, cmd, cwd=None, env=None, timeout=None, retries=1, check=True, shell=False):
                        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
                        self.log.info(f"[fallback-run] {cmd_str} (cwd={cwd})")
                        if self.dry_run:
                            return None
                        import subprocess
                        if shell:
                            subprocess.run(cmd, shell=True, check=check, cwd=cwd, env=env)
                            return None
                        else:
                            subprocess.run(cmd, check=check, cwd=cwd, env=env)
                            return None
                self.fakeroot = _LocalExec(dry_run=self.dry_run)

        # hooks manager (adaptador para várias assinaturas possíveis)
        try:
            # HookManager(source_dir, dry_run=...)
            self._hooks = hooks.HookManager(dry_run=self.dry_run)
        except TypeError:
            # fallback: instantiate without args
            try:
                self._hooks = hooks.HookManager()
                self._hooks.dry_run = self.dry_run
            except Exception:
                self._hooks = None

        # recipe manager (opcional)
        try:
            self._recipe_mgr = recipe.RecipeManager()
        except Exception:
            self._recipe_mgr = None

    # -------------------------
    # DB helpers
    # -------------------------
    def _save_db(self):
        if self.db_path:
            if self.dry_run:
                self.log.info(f"[DRY-RUN] Não salvarei DB em {self.db_path}")
                return
            with open(self.db_path, "w", encoding="utf-8") as fh:
                json.dump(self._db, fh, indent=2)
            self.log.debug(f"DB salvo em {self.db_path}")

    def list_installed(self) -> List[str]:
        return sorted(list(self._db.keys()))

    def package_exists(self, name: str) -> bool:
        return name in self._db

    def installed_files(self, name: str) -> List[str]:
        entry = self._db.get(name, {})
        return entry.get("files", [])

    # -------------------------
    # Reverse dependency check
    # -------------------------
    def check_reverse_dependencies(self, package: str) -> List[str]:
        """
        Retorna lista de pacotes que dependem de `package`.
        """
        broken = []
        for other, meta in self._db.items():
            if other == package:
                continue
            deps = meta.get("depends", []) or []
            if package in deps:
                broken.append(other)
        self.log.debug(f"Reverse deps for {package}: {broken}")
        return broken

    # -------------------------
    # Hooks execution
    # -------------------------
    def _execute_recipe_hooks(self, entry: Dict[str, Any], stage: str):
        """
        Executa hooks definidos na recipe armazenada no entry['recipe'] (ou por caminho).
        Hooks em formato string serão executados via fakeroot (para garantir permissões).
        Funções Python serão chamadas diretamente.
        """
        recipe_data = entry.get("recipe") or {}
        # try to load recipe from recipe manager if recipe_data has a path
        if isinstance(recipe_data, str) and self._recipe_mgr:
            try:
                recipe_data = self._recipe_mgr.load(recipe_data)
            except Exception:
                recipe_data = {}

        hooks_list = {}
        # prefer HookManager.load_from_recipe if available
        if self._hooks:
            try:
                # some HookManager implementations expose load_from_recipe(recipe, stage)
                if hasattr(self._hooks, "load_from_recipe"):
                    hooks_list = self._hooks.load_from_recipe(stage)
                    # If load_from_recipe returns list already, wrap
                    if isinstance(hooks_list, list):
                        hooks_list = hooks_list
                else:
                    # try to read from recipe_data directly
                    hooks_list = recipe_data.get("hooks", {}).get(stage, [])
            except Exception:
                hooks_list = recipe_data.get("hooks", {}).get(stage, [])
        else:
            hooks_list = recipe_data.get("hooks", {}).get(stage, [])

        # normalize: ensure it's a list
        if not isinstance(hooks_list, list):
            hooks_list = list(hooks_list) if hooks_list else []

        for hk in hooks_list:
            if callable(hk):
                if self.dry_run:
                    self.log.info(f"[DRY-RUN] Executaria hook python {hk} para stage={stage}")
                    continue
                try:
                    hk(recipe_data, None)
                except Exception as e:
                    self.log.error(f"Erro no hook python {hk}: {e}")
                    raise
            else:
                # treat as command string; run via fakeroot to ensure permission
                cmd = hk
                try:
                    if self.dry_run:
                        self.log.info(f"[DRY-RUN] Executaria via fakeroot: {cmd}")
                    else:
                        # run under fakeroot, shell=True to allow complex commands
                        self.fakeroot.run(cmd, shell=True, check=True)
                except Exception as e:
                    self.log.error(f"Erro ao executar hook cmd '{cmd}': {e}")
                    raise

    def _run_global_hooks(self, stage: str):
        """
        Tenta executar hooks que estejam em source/hooks/global/<stage> (se existirem),
        e também quaisquer hooks registrados via HookManager.register_global.
        """
        # 1) run HookManager global hooks if available
        if self._hooks and hasattr(self._hooks, "run_hooks"):
            try:
                # some HookManager.run_hooks expect (stage, recipe, sandbox)
                # others expect (stage, recipe, sandbox_path)
                # For global hooks we pass an empty recipe.
                self._hooks.run_hooks(stage, {}, None)
                return
            except Exception:
                # fallback to other interface
                pass

        # 2) try to find files under "source/hooks/global/<stage>" and execute them via fakeroot
        base = os.path.join("source", "hooks", "global")
        hook_dir = os.path.join(base, stage)
        if os.path.isdir(hook_dir):
            for fname in sorted(os.listdir(hook_dir)):
                path = os.path.join(hook_dir, fname)
                if os.access(path, os.X_OK):
                    cmd = path
                else:
                    # shell-run non-executable (e.g., .sh without +x)
                    cmd = f"sh {path}"
                try:
                    if self.dry_run:
                        self.log.info(f"[DRY-RUN] Executaria global hook: {cmd}")
                    else:
                        self.fakeroot.run(cmd, shell=True, check=True)
                except Exception as e:
                    self.log.error(f"Erro ao executar global hook {path}: {e}")
                    raise

    # -------------------------
    # Backup
    # -------------------------
    def _create_backup(self, name: str, files: List[str]) -> Optional[str]:
        """
        Cria um tar.gz com os arquivos listados (usando fakeroot para preservar permissões).
        Retorna o caminho do backup criado, ou None em dry_run.
        """
        if not files:
            self.log.debug("Nenhum arquivo para backup.")
            return None

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_name = f"{name}-preremove-{ts}.tar.gz"
        backup_path = os.path.join(self.backups_dir, backup_name)

        # construir comando tar -czf <backup_path> <file1> <file2> ...
        # usamos shell=True para permitir caminhos com espaços - vamos escapar os filenames
        files_escaped = " ".join([shlex_quote(p) for p in files])
        cmd = f"tar -czf {shlex_quote(backup_path)} {files_escaped}"
        try:
            if self.dry_run:
                self.log.info(f"[DRY-RUN] Criaria backup: {backup_path} com arquivos: {files_escaped}")
                return None
            # executar tar via fakeroot para preservar metadados
            self.fakeroot.run(cmd, shell=True, check=True)
            self.log.info(f"Backup criado: {backup_path}")
            return backup_path
        except Exception as e:
            self.log.error(f"Falha ao criar backup via tar: {e}")
            raise

    # -------------------------
    # Remoção de arquivos
    # -------------------------
    def remove_files(self, package: str, files: Optional[List[str]] = None):
        """
        Remove os arquivos listados. Se files não informado, usa a lista do installed_db.
        Executa remoção via fakeroot (rm -rf).
        """
        files = files if files is not None else self.installed_files(package)
        if not files:
            self.log.info(f"Nenhum arquivo listado para {package} — nada a remover.")
            return

        for f in files:
            if not f:
                continue
            # garantir tipo string
            path = str(f)
            # cuidado: não remova raiz acidentalmente
            if path in ("/", ""):
                self.log.error(f"Caminho perigoso detectado, pulando: {path}")
                continue
            cmd = f"rm -rf {shlex_quote(path)}"
            try:
                if self.dry_run:
                    self.log.info(f"[DRY-RUN] Iriamos executar: {cmd}")
                else:
                    self.fakeroot.run(cmd, shell=True, check=True)
                    self.log.info(f"Removido: {path}")
            except Exception as e:
                self.log.error(f"Falha ao remover {path}: {e}")
                raise

    # -------------------------
    # Pós remoção: cleanup DB
    # -------------------------
    def _finalize_removal(self, package: str):
        """
        Remove a entrada do DB e persiste alterações.
        """
        if package in self._db:
            self.log.info(f"Removendo entrada DB para {package}")
            if not self.dry_run:
                del self._db[package]
                self._save_db()
        else:
            self.log.debug(f"Nenhuma entrada DB encontrada para {package} ao finalizar remoção.")

    # -------------------------
    # Public main method
    # -------------------------
    def remove_package(self, package: str, force: bool = False, backup: bool = True):
        """
        Fluxo completo de remoção:
         - verifica dependências reversas (se não force -> erro)
         - executa global & recipe pre_remove hooks
         - cria backup (opcional)
         - remove arquivos via fakeroot
         - executa post_remove hooks
         - atualiza DB (remove entrada)
         - retorna dicionário com resultado (backup, removed_files)
        """
        if not self.package_exists(package):
            raise RemoveError(f"Pacote não encontrado: {package}")

        # 1) reverse deps
        rev = self.check_reverse_dependencies(package)
        if rev and not force:
            raise RemoveError(f"Pacote {package} é dependência de: {rev}. Use force=True para forçar.")

        entry = self._db.get(package, {})
        files = entry.get("files", [])

        # 2) pre-remove hooks (global + recipe)
        try:
            self._run_global_hooks("pre_remove")
            self._execute_recipe_hooks(entry, "pre_remove")
        except Exception as e:
            raise RemoveError(f"Falha em pre-remove hooks: {e}")

        # 3) backup
        backup_path = None
        try:
            if backup:
                backup_path = self._create_backup(package, files)
        except Exception as e:
            # se backup falhar, abortamos (safety)
            raise RemoveError(f"Falha ao criar backup para {package}: {e}")

        # 4) remove files
        try:
            self.remove_files(package, files)
        except Exception as e:
            # tenta rollback do backup se possível
            if backup_path and os.path.exists(backup_path):
                try:
                    self.log.info("Tentando rollback a partir do backup...")
                    self._restore_from_backup(backup_path)
                    self.log.info("Rollback aplicado.")
                except Exception as re:
                    self.log.error(f"Rollback falhou: {re}")
            raise RemoveError(f"Falha ao remover arquivos de {package}: {e}")

        # 5) post-remove hooks
        try:
            self._execute_recipe_hooks(entry, "post_remove")
            self._run_global_hooks("post_remove")
        except Exception as e:
            # log e segue — hook pós falhou mas arquivos já removidos
            self.log.error(f"Erro em post-remove hooks para {package}: {e}")

        # 6) atualizar DB
        try:
            self._finalize_removal(package)
        except Exception as e:
            self.log.error(f"Falha ao atualizar DB após remoção: {e}")
            raise RemoveError(f"Removido, mas não foi possível atualizar DB: {e}")

        return {"package": package, "backup": backup_path, "removed_files_count": len(files)}

    # -------------------------
    # Helper: restore backup
    # -------------------------
    def _restore_from_backup(self, backup_path: str):
        """
        Restaura um backup tar.gz usando fakeroot (extrai para /).
        ATENÇÃO: deve ser usado apenas em rollback de erro.
        """
        if self.dry_run:
            self.log.info(f"[DRY-RUN] Não restauraria backup {backup_path}")
            return

        if not os.path.exists(backup_path):
            raise RemoveError(f"Backup não encontrado: {backup_path}")

        cmd = f"tar -xzf {shlex_quote(backup_path)} -C /"
        try:
            self.fakeroot.run(cmd, shell=True, check=True)
            self.log.info(f"Backup restaurado a partir de {backup_path}")
        except Exception as e:
            raise RemoveError(f"Falha ao restaurar backup {backup_path}: {e}")


# -------------------------
# Util: shell-quote (simples)
# -------------------------
def shlex_quote(s: str) -> str:
    """
    Pequena versão de shlex.quote para compatibilidade (não importamos shlex diretamente
    para evitar comportamentos de plataforma).
    """
    import shlex
    return shlex.quote(s)


# -------------------------
# CLI (opcional, rápido)
# -------------------------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="remove.py", description="Remover pacotes instalados")
    ap.add_argument("package", help="Nome do pacote a remover")
    ap.add_argument("--db", default="installed_db.json", help="Caminho do DB de instalados (JSON)")
    ap.add_argument("--force", action="store_true", help="Forçar remoção mesmo se houver reverse-deps")
    ap.add_argument("--no-backup", action="store_true", help="Não criar backup antes de remover")
    ap.add_argument("--dry-run", action="store_true", help="Simular sem executar")
    args = ap.parse_args()

    r = Remover(installed_db=args.db, dry_run=args.dry_run)
    try:
        res = r.remove_package(args.package, force=args.force, backup=not args.no_backup)
        print("Remoção concluída:", res)
    except Exception as e:
        print("Falha:", e)
        raise
