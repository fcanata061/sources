# modules/search/info.py

class PackageInfo:
    """
    Exibe informações detalhadas de um pacote.
    """

    def __init__(self, installed_db, repo_path="/usr/source"):
        self.installed_db = installed_db
        self.repo_path = repo_path

    def status(self, package_name):
        """
        Retorna se o pacote está instalado e sua versão.
        """
        pass

    def details(self, package_name):
        """
        Retorna informações detalhadas de uma receita:
        - Versão
        - USE flags
        - Dependências
        - Hooks
        - Build system
        """
        pass
