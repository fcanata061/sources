# modules/remove/remove.py

class Remover:
    """
    Responsável por remover pacotes instalados do sistema.
    Executa hooks e atualiza banco de pacotes instalados.
    """

    def __init__(self, installed_db):
        self.installed_db = installed_db

    def check_reverse_dependencies(self, package):
        """
        Verifica se outros pacotes dependem deste.
        Retorna lista de pacotes que seriam quebrados.
        """
        pass

    def pre_remove_hooks(self, package):
        """
        Executa hooks 'pre_remove' definidos na receita.
        """
        pass

    def remove_files(self, package):
        """
        Remove todos os arquivos instalados pelo pacote.
        """
        pass

    def post_remove_hooks(self, package):
        """
        Executa hooks 'post_remove' definidos na receita.
        """
        pass

    def remove_package(self, package, force=False):
        """
        Remove um pacote específico.
        Se force=True → ignora dependências reversas.
        """
        pass
