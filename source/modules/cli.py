# source/modules/cli.py
"""
CLI principal do gerenciador de pacotes.
Integra módulos: build, recipe, hooks, sandbox, fakeroot, logger.
"""

import argparse
import sys
import os
from modules import build, recipe, hooks, sandbox, fakeroot, logger


def main(argv=None):
    argv = argv or sys.argv[1:]
    log = logger.Logger("cli.log")

    # Inicializa gerenciadores
    builder = build.Builder(dry_run=False)
    recipeman = recipe.RecipeManager()
    hookman = hooks.HookManager()
    fk = fakeroot.FakeRoot()
    log.info("CLI inicializado")

    parser = argparse.ArgumentParser(
        prog="sources",
        description="Gerenciador de pacotes Linux (source-based)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -------------------------------
    # Recipes
    # -------------------------------
    p_new = sub.add_parser("new", help="Criar nova receita (alias: n)")
    p_new.add_argument("dest", help="Diretório destino")
    p_new.add_argument("--name", required=True)
    p_new.add_argument("--version", required=True)
    p_new.add_argument("--build-system", default="make")
    p_new.add_argument("--summary", default="")
    p_new.add_argument("--description", default="")

    p_validate = sub.add_parser("validate", help="Validar receita (alias: val)")
    p_validate.add_argument("path", help="Caminho da receita")

    p_fp = sub.add_parser("fingerprint", help="Calcular fingerprint (alias: fp)")
    p_fp.add_argument("path")
    p_fp.add_argument("--source")

    p_info = sub.add_parser("info", help="Exibir informações da receita (alias: in)")
    p_info.add_argument("path")

    p_search = sub.add_parser("search", help="Procurar receitas (alias: s)")
    p_search.add_argument("term", help="Termo de busca")
    p_search.add_argument("--dir", default="recipes", help="Diretório base de receitas")

    # -------------------------------
    # Build / install
    # -------------------------------
    p_build = sub.add_parser("build", help="Compilar pacote (alias: b)")
    p_build.add_argument("path")
    p_build.add_argument("--sandbox", default="sandbox")
    p_build.add_argument("--dry-run", action="store_true")

    p_install = sub.add_parser("install", help="Instalar pacote (alias: i)")
    p_install.add_argument("archive")
    p_install.add_argument("--prefix", default="/usr/local")

    p_remove = sub.add_parser("remove", help="Remover pacote (alias: rm)")
    p_remove.add_argument("name")

    p_upgrade = sub.add_parser("upgrade", help="Atualizar pacote (alias: up)")
    p_upgrade.add_argument("path")

    # -------------------------------
    # Sistema / status
    # -------------------------------
    p_hooks = sub.add_parser("hooks", help="Listar hooks globais")
    p_graph = sub.add_parser("graph", help="Exportar grafo de dependências")
    p_graph.add_argument("--dir", default="recipes", help="Diretório de receitas")
    p_graph.add_argument("--out", default="deps.dot")

    p_status = sub.add_parser("status", help="Listar pacotes instalados e cacheados")

    # -------------------------------
    # Dispatch
    # -------------------------------
    args = parser.parse_args(argv)

    # Aliases → comando
    aliases = {
        "n": "new", "val": "validate", "fp": "fingerprint",
        "in": "info", "s": "search", "b": "build",
        "i": "install", "rm": "remove", "up": "upgrade"
    }
    if args.command in aliases:
        args.command = aliases[args.command]

    # Recipes
    if args.command == "new":
        recipeman.create(args.dest, args.name, args.version,
                         args.build_system, args.summary, args.description)
        return 0

    elif args.command == "validate":
        r = recipeman.load(args.path)
        recipeman.validate(r)
        print("Recipe válida ✅")
        return 0

    elif args.command == "fingerprint":
        r = recipeman.load(args.path)
        fp = recipeman.compute_fingerprint(args.source or args.path, r)
        print(fp)
        return 0

    elif args.command == "info":
        r = recipeman.load(args.path)
        print("--- Info da Receita ---")
        for k, v in r.items():
            print(f"{k}: {v}")
        return 0

    elif args.command == "search":
        results = []
        for root, _, files in os.walk(args.dir):
            if "recipe.yaml" in files:
                path = os.path.join(root, "recipe.yaml")
                rec = recipeman.load(path)
                if args.term.lower() in rec.get("name", "").lower() or \
                   args.term.lower() in rec.get("summary", "").lower():
                    results.append((rec["name"], rec.get("version"), path))
        if not results:
            print("Nenhuma receita encontrada")
        else:
            for name, version, path in results:
                print(f"{name}-{version} -> {path}")
        return 0

    # Build
    elif args.command == "build":
        b = build.Builder(dry_run=args.dry_run)
        recipe_data = recipeman.load(args.path)
        sb = sandbox.Sandbox(recipe_data["name"], base_dir=args.sandbox, dry_run=args.dry_run)
        sb.prepare()
        archive = b.build_package(recipe_data, sb)
        print(f"Pacote gerado: {archive}")
        return 0

    elif args.command == "install":
        fk.install(args.archive, prefix=args.prefix)
        return 0

    elif args.command == "remove":
        fk.remove(args.name)
        return 0

    elif args.command == "upgrade":
        recipe_data = recipeman.load(args.path)
        sb = sandbox.Sandbox(recipe_data["name"])
        sb.prepare()
        archive = builder.build_package(recipe_data, sb)
        fk.install(archive)
        print(f"Pacote {recipe_data['name']} atualizado ✅")
        return 0

    # Hooks
    elif args.command == "hooks":
        h = hookman.list_hooks()
        print("Hooks globais:")
        for stage, funcs in h.items():
            print(f" - {stage}: {funcs}")
        return 0

    # Grafo
    elif args.command == "graph":
        deps = {}
        for root, _, files in os.walk(args.dir):
            if "recipe.yaml" in files:
                rec = recipeman.load(os.path.join(root, "recipe.yaml"))
                deps[rec["name"]] = rec.get("depends", [])
        builder.export_graph(deps, output=args.out)
        print(f"Grafo exportado em {args.out}")
        return 0

    # Status
    elif args.command == "status":
        print("Pacotes instalados:")
        for pkg in fk.list_installed():
            print(f" - {pkg}")
        print("Pacotes em cache:")
        for pkg in builder.list_cache():
            print(f" - {pkg}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
