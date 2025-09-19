# source/modules/search.py
"""
Módulo de pesquisa / descoberta de pacotes no repositório de receitas e no DB de instalados.

Funcionalidades:
 - indexar receitas (cache em JSON)
 - buscar por nome (exato), fuzzy (aproximado), keywords (summary/description)
 - buscar por arquivo instalado
 - listar arquivos instalados por pacote (usando installed_db.json ou recipe.manifest_files)
 - listar dependências e reverse-deps
 - CLI: search, list, info, files, deps, rdeps, refresh-index
"""

from __future__ import annotations
import os
import json
import time
import re
import fnmatch
from typing import List, Dict, Optional, Tuple
from difflib import get_close_matches

# integra com logger do projeto (assumimos modules.logger conforme discutido)
from modules import logger as _logger
from modules import recipe as _recipe

DEFAULT_REPO_DIR = "recipes"          # onde ficam os diretórios com recipe.yaml
DEFAULT_INSTALLED_DB = "installed_db.json"
DEFAULT_INDEX_FILE = ".search_index.json"

class PackageSearch:
    def __init__(self,
                 repo_path: str = DEFAULT_REPO_DIR,
                 installed_db: str = DEFAULT_INSTALLED_DB,
                 index_file: Optional[str] = None,
                 logger: Optional[_logger.Logger] = None):
        self.repo_path = os.path.abspath(repo_path)
        self.installed_db_path = os.path.abspath(installed_db)
        self.index_file = index_file or os.path.join(self.repo_path, DEFAULT_INDEX_FILE)
        self.log = logger or _logger.Logger("search.log")
        self._index: Dict[str, Dict] = {}
        self.recipe_mgr = _recipe.RecipeManager()
        # tenta carregar index se existir, senão constrói na primeira busca
        if os.path.exists(self.index_file):
            try:
                with open(self.index_file, "r", encoding="utf-8") as fh:
                    self._index = json.load(fh)
                self.log.debug(f"Índice carregado de {self.index_file}")
            except Exception as e:
                self.log.error(f"Falha ao carregar índice ({self.index_file}): {e}")
                self._index = {}

    # -----------------------
    # Indexação
    # -----------------------
    def refresh_index(self, force: bool = False) -> Dict[str, Dict]:
        """
        Varre repo_path procurando recipe.yaml e gera um índice:
        {
          "pkgname": {
              "name": ...,
              "version": ...,
              "path": "/abs/path/to/recipe/dir",
              "summary": "...",
              "keywords": [...],
              "manifest_files": [...],
              "provides": [...],
              "depends": [...]
          }, ...
        }
        O índice é salvo em self.index_file.
        """
        if self._index and not force:
            # check minimal sanity: if index non-empty and not forced, retornamos ele
            self.log.debug("Índice já presente — use force=True para rebuild.")
            return self._index

        idx: Dict[str, Dict] = {}
        self.log.info(f"Indexando receitas em {self.repo_path} ...")
        for root, dirs, files in os.walk(self.repo_path):
            if "recipe.yaml" in files:
                recipe_path = os.path.join(root, "recipe.yaml")
                try:
                    rec = self.recipe_mgr.load(recipe_path)
                    name = rec.get("name") or os.path.basename(root)
                    idx[name] = {
                        "name": name,
                        "version": rec.get("version"),
                        "path": root,
                        "summary": rec.get("summary", "") or "",
                        "description": rec.get("description", "") or "",
                        "keywords": rec.get("keywords", []),
                        "manifest_files": rec.get("manifest_files", []),
                        "provides": rec.get("provides", []),
                        "depends": rec.get("depends", []),
                        "recipe": rec
                    }
                except Exception as e:
                    self.log.error(f"Erro ao carregar recipe {recipe_path}: {e}")
                    continue

        # save to disk
        try:
            with open(self.index_file, "w", encoding="utf-8") as fh:
                json.dump(idx, fh, indent=2)
            self.log.info(f"Índice salvo em {self.index_file} ({len(idx)} pacotes)")
        except Exception as e:
            self.log.error(f"Falha ao salvar índice: {e}")

        self._index = idx
        return idx

    def _ensure_index(self):
        if not self._index:
            self.refresh_index(force=True)

    # -----------------------
    # Utilities: installed DB
    # -----------------------
    def _load_installed_db(self) -> Dict[str, Dict]:
        if not os.path.exists(self.installed_db_path):
            self.log.debug("installed_db não existe; retornando vazio")
            return {}
        try:
            with open(self.installed_db_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as e:
            self.log.error(f"Erro ao ler installed_db {self.installed_db_path}: {e}")
            return {}

    # -----------------------
    # Queries
    # -----------------------
    def list_all_packages(self) -> List[str]:
        """Lista todos os nomes de pacotes indexados (rebuild se necessário)."""
        self._ensure_index()
        return sorted(list(self._index.keys()))

    def find_package(self, package_name: str) -> Optional[str]:
        """Retorna caminho absoluto da receita se encontrada por nome exato."""
        self._ensure_index()
        entry = self._index.get(package_name)
        if entry:
            return entry["path"]
        return None

    def search(self, term: str, max_results: int = 15, fuzzy: bool = True) -> List[Tuple[str, float]]:
        """
        Busca por nome/summary/description/keywords.
        Retorna lista de (name, score) ordenada por score desc.
        Se fuzzy=True usa get_close_matches para nomes semelhantes.
        """
        self._ensure_index()
        term_l = term.lower().strip()

        results: Dict[str, float] = {}
        # direct name matches (high score)
        for name, meta in self._index.items():
            if term_l == name.lower():
                results[name] = max(results.get(name, 0), 1.0)
                continue
            # exact token in name
            if term_l in name.lower():
                results[name] = max(results.get(name, 0), 0.8)

            # keywords, summary, description
            if term_l in (meta.get("summary") or "").lower():
                results[name] = max(results.get(name, 0), 0.7)
            if term_l in (meta.get("description") or "").lower():
                results[name] = max(results.get(name, 0), 0.6)
            for kw in meta.get("keywords", []) or []:
                if term_l in str(kw).lower():
                    results[name] = max(results.get(name, 0), 0.75)

        # fuzzy by name if enabled
        if fuzzy:
            names = list(self._index.keys())
            close = get_close_matches(term, names, n=max_results, cutoff=0.6)
            for c in close:
                results[c] = max(results.get(c, 0), 0.65)

        # sort results by score
        sorted_results = sorted(results.items(), key=lambda kv: kv[1], reverse=True)
        return sorted_results[:max_results]

    def search_regex(self, pattern: str, field: str = "name") -> List[str]:
        """Busca via regex no campo especificado (name/summary/description)."""
        self._ensure_index()
        prog = re.compile(pattern)
        matches = []
        for name, meta in self._index.items():
            target = meta.get(field, "") if field in meta else ""
            if prog.search(target):
                matches.append(name)
        return matches

    def search_files(self, filename_pattern: str) -> List[Tuple[str, str]]:
        """
        Busca pacotes que listam um arquivo em manifest_files OR instalado (installed_db).
        Retorna lista de tuples (package, matched_path).
        """
        self._ensure_index()
        out = []
        # check manifest_files in recipes
        for name, meta in self._index.items():
            for mf in meta.get("manifest_files", []) or []:
                if fnmatch.fnmatch(mf, filename_pattern) or filename_pattern in mf:
                    out.append((name, os.path.join(meta["path"], mf)))
        # check installed_db
        inst = self._load_installed_db()
        for pkg, meta in inst.items():
            for f in meta.get("files", []) or []:
                if fnmatch.fnmatch(f, filename_pattern) or filename_pattern in f:
                    out.append((pkg, f))
        return out

    def list_files(self, package_name: str) -> List[str]:
        """
        Retorna lista de arquivos instalados por um pacote.
        Prioriza installed_db; se não existir, usa recipe.manifest_files (expansão relativa).
        """
        inst = self._load_installed_db()
        if package_name in inst:
            return inst[package_name].get("files", [])

        # fallback to recipe manifest
        self._ensure_index()
        meta = self._index.get(package_name)
        if not meta:
            return []
        files = []
        for rel in meta.get("manifest_files", []) or []:
            files.append(os.path.join(meta["path"], rel))
        return files

    def list_dependencies(self, package_name: str) -> List[str]:
        """Retorna lista de dependências declaradas na receita (depends)."""
        self._ensure_index()
        meta = self._index.get(package_name)
        if not meta:
            return []
        return meta.get("depends", []) or []

    def reverse_dependencies(self, package_name: str) -> List[str]:
        """Retorna lista de pacotes no índice que dependem de package_name."""
        self._ensure_index()
        rdeps = []
        for name, meta in self._index.items():
            for d in meta.get("depends", []) or []:
                if d == package_name:
                    rdeps.append(name)
        return rdeps

    def list_provides(self, package_name: str) -> List[str]:
        self._ensure_index()
        return self._index.get(package_name, {}).get("provides", []) or []

    # -----------------------
    # Helpers / CLI friendly
    # -----------------------
    def info(self, package_name: str) -> Dict:
        """Retorna metadados completos do pacote (do índice)."""
        self._ensure_index()
        return self._index.get(package_name, {})

# -----------------------
# CLI
# -----------------------
def main_cli(argv: Optional[List[str]] = None):
    import argparse
    argv = argv or []
    ps = PackageSearch()
    ap = argparse.ArgumentParser(prog="search.py", description="Search packages and recipes")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List all packages")
    p_list.add_argument("--repo", default=ps.repo_path)

    p_search = sub.add_parser("search", help="Search by term (name/summary/keywords)")
    p_search.add_argument("term")
    p_search.add_argument("--fuzzy", action="store_true")

    p_info = sub.add_parser("info", help="Show recipe metadata")
    p_info.add_argument("pkg")

    p_files = sub.add_parser("files", help="List files for package or search file pattern")
    p_files.add_argument("pkg_or_pattern", help="package name or filename pattern (contains or glob)")

    p_deps = sub.add_parser("deps", help="List dependencies declared in recipe")
    p_deps.add_argument("pkg")

    p_rdeps = sub.add_parser("rdeps", help="List reverse dependencies (who depends on pkg)")
    p_rdeps.add_argument("pkg")

    p_refresh = sub.add_parser("refresh-index", help="Rebuild index from recipes dir")
    p_refresh.add_argument("--force", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        for p in ps.list_all_packages():
            print(p)
        return

    if args.cmd == "search":
        res = ps.search(args.term, fuzzy=args.fuzzy)
        for name, score in res:
            print(f"{name}\t{score:.2f}")
        return

    if args.cmd == "info":
        info = ps.info(args.pkg)
        if not info:
            print("Pacote não encontrado")
            return
        for k, v in info.items():
            if k == "recipe":
                print(f"{k}: <recipe dict...>")
            else:
                print(f"{k}: {v}")
        return

    if args.cmd == "files":
        # if pkg exists, list files; else treat as pattern
        if args.pkg_or_pattern in ps.list_all_packages():
            for f in ps.list_files(args.pkg_or_pattern):
                print(f)
        else:
            for pkg, path in ps.search_files(args.pkg_or_pattern):
                print(f"{pkg}\t{path}")
        return

    if args.cmd == "deps":
        for d in ps.list_dependencies(args.pkg):
            print(d)
        return

    if args.cmd == "rdeps":
        for d in ps.reverse_dependencies(args.pkg):
            print(d)
        return

    if args.cmd == "refresh-index":
        ps.refresh_index(force=args.force)
        print("Índice atualizado.")
        return

if __name__ == "__main__":
    import sys
    main_cli(sys.argv[1:])
