# source/modules/recipe.py
"""
Recipe manager - criar, validar, editar e calcular fingerprint de recipe.yaml

Uso:
  - Programaticamente: from modules.recipe import RecipeManager
  - CLI: python -m source.modules.recipe create ... / validate / add-dep / add-hook / fingerprint
"""

from __future__ import annotations
import os
import sys
import yaml
import json
import hashlib
from typing import List, Dict, Optional, Any
from datetime import datetime

# Ajuste conforme sua implementação de logger (assumimos modules.logger conforme discutido)
from modules import logger as _logger


class RecipeError(Exception):
    pass


class RecipeManager:
    REQUIRED_FIELDS = ["name", "version"]

    def __init__(self, logger: Optional[_logger.Logger] = None):
        self.log = logger or _logger.Logger("recipe-manager.log")

    # -------------------------
    # I/O
    # -------------------------
    def load(self, path: str) -> Dict[str, Any]:
        """Carrega recipe.yaml de um diretório ou de um arquivo específico"""
        path = os.path.abspath(path)
        if os.path.isdir(path):
            candidate = os.path.join(path, "recipe.yaml")
        else:
            candidate = path

        if not os.path.exists(candidate):
            raise RecipeError(f"Recipe file not found: {candidate}")

        with open(candidate, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        self.log.info(f"Recipe carregada: {candidate}")
        return data

    def save(self, recipe: Dict[str, Any], dest_dir: str):
        """Salva recipe dict como recipe.yaml no diretório destino"""
        dest_dir = os.path.abspath(dest_dir)
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir, exist_ok=True)
        dest_file = os.path.join(dest_dir, "recipe.yaml")
        with open(dest_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(recipe, f, sort_keys=False, allow_unicode=True)
        self.log.info(f"Recipe salva em: {dest_file}")
        return dest_file

    # -------------------------
    # Criação / template
    # -------------------------
    def create(self,
               dest_dir: str,
               name: str,
               version: str,
               build_system: str = "make",
               summary: str = "",
               description: str = "",
               depends: Optional[List[str]] = None,
               manifest_files: Optional[List[str]] = None,
               hooks: Optional[Dict[str, List[Any]]] = None,
               metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Cria uma recipe.yaml básica e salva em dest_dir.
        Retorna o caminho do arquivo salvo.
        """
        recipe = {
            "name": name,
            "version": version,
            "build_system": build_system,
            "summary": summary,
            "description": description,
            "depends": depends or [],
            "manifest_files": manifest_files or [],
            "hooks": hooks or {},
            "metadata": metadata or {},
            "created_at": datetime.utcnow().isoformat() + "Z",
        }

        self.validate(recipe)  # garante que campos obrigatórios existem
        return self.save(recipe, dest_dir)

    # -------------------------
    # Validação
    # -------------------------
    def validate(self, recipe: Dict[str, Any]) -> bool:
        """Valida que campos essenciais existam e tenham formato razoável"""
        missing = [f for f in self.REQUIRED_FIELDS if f not in recipe or not recipe[f]]
        if missing:
            raise RecipeError(f"Campos obrigatórios faltando: {missing}")

        if "depends" in recipe and not isinstance(recipe["depends"], list):
            raise RecipeError("Campo 'depends' deve ser uma lista")

        if "manifest_files" in recipe and not isinstance(recipe["manifest_files"], list):
            raise RecipeError("Campo 'manifest_files' deve ser uma lista")

        if "hooks" in recipe and not isinstance(recipe["hooks"], dict):
            raise RecipeError("Campo 'hooks' deve ser um dicionário com listas por stage")

        # checks básicos adicionais
        if "version" in recipe:
            if not isinstance(recipe["version"], (str, int)):
                raise RecipeError("Campo 'version' deve ser string ou número")

        self.log.info(f"Recipe {recipe.get('name')} validada")
        return True

    # -------------------------
    # Edição helpers
    # -------------------------
    def add_dependency(self, recipe: Dict[str, Any], dep: str) -> Dict[str, Any]:
        deps = recipe.setdefault("depends", [])
        if dep not in deps:
            deps.append(dep)
            self.log.info(f"Dependência adicionada: {dep}")
        else:
            self.log.debug(f"Dependência já existe: {dep}")
        return recipe

    def remove_dependency(self, recipe: Dict[str, Any], dep: str) -> Dict[str, Any]:
        deps = recipe.get("depends", [])
        if dep in deps:
            deps.remove(dep)
            self.log.info(f"Dependência removida: {dep}")
        return recipe

    def add_hook(self, recipe: Dict[str, Any], stage: str, hook: Any) -> Dict[str, Any]:
        """
        Adiciona hook a recipe.
        hook pode ser:
         - string (comando ou script)
         - lista de strings (multicomando)
         - função serializável não suportado via YAML; para isso, use registro dinâmico via hooks module
        """
        hooks = recipe.setdefault("hooks", {})
        arr = hooks.setdefault(stage, [])
        # evita duplicatas exatas
        if hook not in arr:
            arr.append(hook)
            self.log.info(f"Hook adicionado no stage {stage}: {hook}")
        return recipe

    def remove_hook(self, recipe: Dict[str, Any], stage: str, hook: Any) -> Dict[str, Any]:
        hooks = recipe.get("hooks", {})
        arr = hooks.get(stage, [])
        if hook in arr:
            arr.remove(hook)
            self.log.info(f"Hook removido do stage {stage}: {hook}")
        return recipe

    def update_field(self, recipe: Dict[str, Any], field: str, value: Any) -> Dict[str, Any]:
        recipe[field] = value
        self.log.info(f"Campo atualizado: {field} = {value}")
        return recipe

    # -------------------------
    # Fingerprint / manifest helpers
    # -------------------------
    def compute_fingerprint(self, source_dir: str, recipe: Dict[str, Any]) -> str:
        """
        Computa um fingerprint SHA256 que captura recipe + arquivos listados em manifest_files.
        Se manifest_files vazio, usa lista de nomes + mtimes do tree (fallback).
        """
        m = hashlib.sha256()
        rec_bytes = json.dumps(recipe, sort_keys=True, default=str).encode("utf-8")
        m.update(rec_bytes)

        manifest = recipe.get("manifest_files", [])
        if manifest:
            for rel in sorted(manifest):
                path = os.path.join(source_dir, rel)
                if os.path.exists(path) and os.path.isfile(path):
                    with open(path, "rb") as fh:
                        while True:
                            chunk = fh.read(8192)
                            if not chunk:
                                break
                            m.update(chunk)
                else:
                    # include missing marker so fingerprint changes if file appears/disappears
                    m.update(f"__missing__:{rel}".encode("utf-8"))
        else:
            # fallback: traverse source_dir
            for root, _, files in os.walk(source_dir):
                for fn in sorted(files):
                    fp = os.path.join(root, fn)
                    try:
                        st = os.stat(fp)
                        data = f"{os.path.relpath(fp, source_dir)}:{st.st_mtime}".encode("utf-8")
                    except Exception:
                        data = f"{os.path.relpath(fp, source_dir)}:err".encode("utf-8")
                    m.update(data)

        fp = m.hexdigest()
        self.log.info(f"Fingerprint para {recipe.get('name')} calculado: {fp}")
        return fp

    # -------------------------
    # CLI helpers
    # -------------------------
    def cli_create(self, args):
        path = args.dest
        name = args.name
        version = args.version
        bs = args.build_system or "make"
        summary = args.summary or ""
        description = args.description or ""
        deps = args.depends or []
        manifest = args.manifest or []
        hooks = {}  # hooks podem ser adicionados com add-hook
        md = {"created_by": "RecipeManager CLI"}
        file = self.create(path, name, version, bs, summary, description, deps, manifest, hooks, md)
        print(f"recipe criada em: {file}")

    def cli_validate(self, args):
        recipe = self.load(args.path)
        try:
            self.validate(recipe)
            print("OK: recipe válida")
        except Exception as e:
            print("INVALID:", e)
            raise

    def cli_add_dep(self, args):
        recipe = self.load(args.path)
        self.add_dependency(recipe, args.dep)
        self.save(recipe, os.path.dirname(os.path.join(args.path)) if os.path.isfile(args.path) else args.path)
        print("Dependência adicionada.")

    def cli_add_hook(self, args):
        recipe = self.load(args.path)
        self.add_hook(recipe, args.stage, args.hook)
        self.save(recipe, os.path.dirname(os.path.join(args.path)) if os.path.isfile(args.path) else args.path)
        print("Hook adicionado.")

    def cli_fingerprint(self, args):
        recipe = self.load(args.path)
        srcdir = args.source or os.path.dirname(args.path)
        fp = self.compute_fingerprint(srcdir, recipe)
        print(fp)
        return fp


# -------------------------
# CLI entrypoint
# -------------------------
def main_cli(argv: Optional[List[str]] = None):
    import argparse
    rm = RecipeManager()

    ap = argparse.ArgumentParser(prog="recipe.py", description="Recipe manager")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Create a recipe.yaml")
    p_create.add_argument("dest", help="Destination directory to write recipe.yaml")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--version", required=True)
    p_create.add_argument("--build-system", help="make|cmake|meson|python|rust|node")
    p_create.add_argument("--summary")
    p_create.add_argument("--description")
    p_create.add_argument("--depends", nargs="*", help="Dependencies")
    p_create.add_argument("--manifest", nargs="*", help="Manifest files (relative paths)")

    p_validate = sub.add_parser("validate", help="Validate a recipe")
    p_validate.add_argument("path", help="Path to recipe.yaml or recipe dir")

    p_add_dep = sub.add_parser("add-dep", help="Add dependency to recipe")
    p_add_dep.add_argument("path", help="Path to recipe.yaml or recipe dir")
    p_add_dep.add_argument("dep", help="Dependency name to add")
    p_add_dep.set_defaults(func=rm.cli_add_dep)

    p_add_hook = sub.add_parser("add-hook", help="Add hook to recipe")
    p_add_hook.add_argument("path", help="Path to recipe.yaml or recipe dir")
    p_add_hook.add_argument("stage", help="Stage name, e.g. pre-build, post-install")
    p_add_hook.add_argument("hook", help="Hook command (string)")
    p_add_hook.set_defaults(func=rm.cli_add_hook)

    p_fp = sub.add_parser("fingerprint", help="Compute fingerprint for recipe+source")
    p_fp.add_argument("path", help="Path to recipe.yaml or recipe dir")
    p_fp.add_argument("--source", help="Source directory (defaults to recipe dir)")

    # legacy set defaults
    p_create.set_defaults(func=rm.cli_create)
    p_validate.set_defaults(func=rm.cli_validate)
    p_fp.set_defaults(func=rm.cli_fingerprint)

    args = ap.parse_args(argv)
    # dispatch
    if hasattr(args, "func"):
        return args.func(args)
    else:
        cmd = args.cmd
        if cmd == "create":
            return rm.cli_create(args)
        elif cmd == "validate":
            return rm.cli_validate(args)
        elif cmd == "fingerprint":
            return rm.cli_fingerprint(args)
        else:
            ap.print_help()


if __name__ == "__main__":
    main_cli()
