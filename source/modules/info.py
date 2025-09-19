# source/modules/info.py
"""
info.py - exibe informações sobre pacotes e recipes.

- Lê receitas disponíveis em /usr/sources (ou configurado).
- Verifica estado de instalação no banco local installed_db.json.
- Mostra resumo, descrição, dependências, arquivos etc.
- Integra com logger e suporta modo detalhado (--verbose).
"""

import os
import sys
import json
import argparse
from typing import Dict, Any

from modules import logger as _logger


DB_PATH = "/var/lib/sources/installed_db.json"
DEFAULT_RECIPE_DIR = "/usr/sources"


class InfoError(Exception):
    pass


class PackageInfo:
    def __init__(self, recipe_dir: str = DEFAULT_RECIPE_DIR):
        self.recipe_dir = recipe_dir
        self.log = _logger.Logger("info.log")
        self.installed_db = self._load_db()

    def _load_db(self) -> Dict[str, Any]:
        if not os.path.exists(DB_PATH):
            return {}
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.log.error(f"Falha ao carregar DB instalado: {e}")
            return {}

    def _find_recipe(self, pkg: str) -> Dict[str, Any]:
        """
        Procura recipe.json ou recipe.yaml em subpastas correspondentes ao pacote.
        """
        for root, dirs, files in os.walk(self.recipe_dir):
            for fn in files:
                if fn in ("recipe.json", "recipe.yaml"):
                    if os.path.basename(root) == pkg:
                        path = os.path.join(root, fn)
                        try:
                            import yaml
                            if fn.endswith(".yaml"):
                                with open(path, "r", encoding="utf-8") as f:
                                    return yaml.safe_load(f)
                            else:
                                with open(path, "r", encoding="utf-8") as f:
                                    return json.load(f)
                        except Exception as e:
                            raise InfoError(f"Falha ao ler recipe de {pkg}: {e}")
        raise InfoError(f"Recipe para pacote '{pkg}' não encontrada em {self.recipe_dir}")

    def status(self, pkg: str) -> str:
        """
        Retorna estado de instalação: ✅ instalado, ❌ não instalado.
        """
        if pkg in self.installed_db:
            return f"✅ Instalado (versão {self.installed_db[pkg].get('version', '?')})"
        return "❌ Não instalado"

    def details(self, pkg: str, verbose: bool = False) -> Dict[str, Any]:
        """
        Retorna detalhes de um pacote.
        """
        recipe = self._find_recipe(pkg)
        info = {
            "name": recipe.get("name", pkg),
            "version": recipe.get("version", "?"),
            "summary": recipe.get("summary", ""),
            "description": recipe.get("description", ""),
            "build_system": recipe.get("build_system", ""),
            "dependencies": recipe.get("dependencies", []),
            "hooks": recipe.get("hooks", {}),
            "installed": pkg in self.installed_db
        }

        if verbose:
            inst = self.installed_db.get(pkg, {})
            if inst:
                info["install_time"] = inst.get("install_time", "?")
                info["files"] = inst.get("files", [])
        return info


def main_cli(argv=None):
    argv = argv or sys.argv[1:]
    ap = argparse.ArgumentParser(prog="info", description="Exibe informações sobre pacotes")
    ap.add_argument("package", help="Nome do pacote")
    ap.add_argument("-v", "--verbose", action="store_true", help="Exibir informações detalhadas")
    args = ap.parse_args(argv)

    mgr = PackageInfo()
    try:
        print("Estado:", mgr.status(args.package))
        details = mgr.details(args.package, verbose=args.verbose)
        print("Nome:", details["name"])
        print("Versão:", details["version"])
        print("Resumo:", details["summary"])
        print("Descrição:", details["description"])
        print("Build system:", details["build_system"])
        print("Dependências:", ", ".join(details["dependencies"]) if details["dependencies"] else "nenhuma")
        if args.verbose:
            print("Hooks:", details["hooks"])
            if "files" in details:
                print("Arquivos instalados:")
                for f in details["files"]:
                    print("  -", f)
    except InfoError as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
