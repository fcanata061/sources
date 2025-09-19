# modules/cli/cli.py

import argparse
from ..install.build import Builder
from ..upgrade.upgrade import Upgrader
from ..remove.remove import Remover
from ..use.flags import UseFlags
from ..use.query import UseQuery
from ..sync.sync import SyncManager
from ..logging.logger import Logger

class SourceCLI:
    """
    Interface de linha de comando (CLI) para o gerenciador de pacotes source.
    """

    def __init__(self):
        self.logger = Logger()
        self.parser = argparse.ArgumentParser(
            prog="source",
            description="Gerenciador de pacotes - Source"
        )
        self.subparsers = self.parser.add_subparsers(dest="command")

        # comandos principais
        self._add_install()
        self._add_remove()
        self._add_upgrade()
        self._add_flags()
        self._add_sync()
        self._add_create()
        self._add_history()

    # ----------------------
    # Definições de comandos
    # ----------------------

    def _add_install(self):
        sp = self.subparsers.add_parser("install", aliases=["i"], help="Instalar pacotes")
        sp.add_argument("package", help="Nome do pacote a instalar")

    def _add_remove(self):
        sp = self.subparsers.add_parser("remove", aliases=["rm"], help="Remover pacotes")
        sp.add_argument("package", help="Nome do pacote a remover")
        sp.add_argument("--force", action="store_true", help="Forçar remoção ignorando dependências")

    def _add_upgrade(self):
        sp = self.subparsers.add_parser("upgrade", aliases=["up"], help="Atualizar pacotes")
        sp.add_argument("package", nargs="?", help="Nome do pacote (vazio = todo o sistema)")

    def _add_flags(self):
        sp = self.subparsers.add_parser("flags", aliases=["fl"], help="Consultar USE flags")
        sp.add_argument("package", nargs="?", help="Nome do pacote para exibir flags")
        sp.add_argument("--list", action="store_true", help="Listar todas as flags globais")
        sp.add_argument("--enable", help="Ativar flag global")
        sp.add_argument("--disable", help="Desativar flag global")

    def _add_sync(self):
        sp = self.subparsers.add_parser("sync", aliases=["s"], help="Sincronizar repositório")

    def _add_create(self):
        sp = self.subparsers.add_parser("create", aliases=["c"], help="Criar nova receita")
        sp.add_argument("package", help="Nome do pacote a criar")

    def _add_history(self):
        sp = self.subparsers.add_parser("history", aliases=["h"], help="Exibir histórico de operações")
        sp.add_argument("--limit", type=int, default=50, help="Número de registros a exibir")

    # ----------------------
    # Execução de comandos
    # ----------------------

    def run(self, args=None):
        args = self.parser.parse_args(args)

        if args.command in ("install", "i"):
            self.logger.info(f"Instalando {args.package}...")
            # chamar módulo install futuramente

        elif args.command in ("remove", "rm"):
            self.logger.info(f"Removendo {args.package} (force={args.force})...")
            # chamar módulo remove futuramente

        elif args.command in ("upgrade", "up"):
            if args.package:
                self.logger.info(f"Atualizando pacote {args.package}...")
            else:
                self.logger.info("Atualizando todo o sistema...")
            # chamar módulo upgrade futuramente

        elif args.command in ("flags", "fl"):
            if args.list:
                self.logger.info("Listando todas as USE flags globais...")
            elif args.package:
                self.logger.info(f"Exibindo flags do pacote {args.package}...")
            elif args.enable:
                self.logger.success(f"Ativando flag global {args.enable}")
            elif args.disable:
                self.logger.warning(f"Desativando flag global {args.disable}")
            # integrar com módulo use futuramente

        elif args.command in ("sync", "s"):
            self.logger.info("Sincronizando repositório...")
            # chamar módulo sync futuramente

        elif args.command in ("create", "c"):
            self.logger.info(f"Criando nova receita para {args.package}...")
            # chamar módulo create futuramente

        elif args.command in ("history", "h"):
            self.logger.info(f"Exibindo histórico (limite={args.limit})...")
            # chamar módulo history futuramente

        else:
            self.parser.print_help()
