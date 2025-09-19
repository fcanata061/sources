# modules/dependencies/resolver.py

class DependencyResolver:
    """
    Resolve dependências de pacotes (build, runtime e opcionais).
    Integra com USE flags para dependências condicionais.
    """

    def __init__(self, repo_path, installed_db):
        self.repo_path = repo_path         # diretório com receitas (/usr/source)
        self.installed_db = installed_db   # banco local de pacotes instalados

    def parse_dependencies(self, recipe):
        """
        Lê a receita e retorna:
        - build_deps → dependências de compilação
        - run_deps   → dependências de execução
        - opt_deps   → dependências opcionais (USE flags)
        """
        pass

    def resolve(self, package, use_flags=None):
        """
        Dado um pacote, retorna a árvore de dependências resolvida.
        Considera USE flags ativadas.
        """
        pass

    def find_missing(self, package, use_flags=None):
        """
        Retorna lista de dependências que ainda não estão instaladas.
        """
        pass

    def find_reverse_dependencies(self, package):
        """
        Localiza pacotes que dependem do pacote informado.
        Usado pelo módulo remove.
        """
        pass
