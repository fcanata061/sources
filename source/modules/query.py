# modules/use/query.py

class UseQuery:
    """
    Consulta e exibe informações sobre USE flags.
    """

    def __init__(self, repo_path, use_flags):
        self.repo_path = repo_path
        self.use_flags = use_flags

    def list_all_flags(self):
        """
        Lista todas as USE flags globais disponíveis.
        """
        pass

    def list_package_flags(self, package):
        """
        Mostra as USE flags disponíveis para um pacote e quais estão ativas.
        """
        pass

    def check_flag_status(self, flag):
        """
        Verifica se uma flag está ativa ou desativada globalmente.
        """
        pass
