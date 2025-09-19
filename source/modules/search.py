# modules/search/search.py

import os

class PackageSearch:
    """
    Ferramentas para buscar pacotes, dependências e arquivos no repositório.
    """

    def __init__(self, repo_path="/usr/source"):
        self.repo_path = repo_path

    def list_all_packages(self):
        """
        Lista todos os pacotes disponíveis no repositório.
        """
        packages = [d for d in os.listdir(self.repo_path) if os.path.isdir(os.path.join(self.repo_path, d))]
        return packages

    def find_package(self, package_name):
        """
        Verifica se o pacote existe no repositório e retorna seu path.
        """
        package_path = os.path.join(self.repo_path, package_name)
        if os.path.exists(package_path):
            return package_path
        return None

    def list_files(self, package_name):
        """
        Lista arquivos instalados por um pacote (via banco de pacotes instalados ou recipe).
        """
        pass

    def list_dependencies(self, package_name):
        """
        Retorna lista de dependências (build e runtime) do pacote.
        """
        pass
