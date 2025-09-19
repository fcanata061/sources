# modules/upgrade/upgrade.py

class Upgrader:
    """
    Responsável por atualizar pacotes instalados para versões mais novas.
    Pode atuar em um único pacote ou em todo o sistema (--world).
    """

    def __init__(self, repo_path, installed_db):
        self.repo_path = repo_path         # caminho do /usr/source/
        self.installed_db = installed_db   # banco local de pacotes instalados

    def check_updates(self, package=None):
        """
        Verifica se há atualizações disponíveis.
        Se package=None → checa todos os pacotes instalados.
        Retorna lista de pacotes a atualizar.
        """
        pass

    def resolve_dependencies(self, package):
        """
        Resolve dependências novas ou alteradas na versão mais recente.
        Integra com o módulo dependencies.
        """
        pass

    def upgrade_package(self, package):
        """
        Atualiza um único pacote:
        - Baixa/atualiza receita
        - Verifica SHA256
        - Executa build em sandbox
        - Instala via fakeroot
        - Executa hooks
        """
        pass

    def upgrade_world(self):
        """
        Atualiza todos os pacotes instalados para as versões mais recentes.
        """
        pass
