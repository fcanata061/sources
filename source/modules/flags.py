# modules/use/flags.py

class UseFlags:
    """
    Gerencia USE flags globais e específicas por pacote.
    """

    def __init__(self, config_path="/etc/source/use.conf"):
        self.config_path = config_path
        self.global_flags = {}
        self.package_flags = {}

    def load(self):
        """
        Carrega flags globais e por pacote do arquivo de configuração.
        """
        pass

    def save(self):
        """
        Salva alterações no arquivo de configuração.
        """
        pass

    def get_global_flags(self):
        """
        Retorna todas as USE flags globais.
        """
        pass

    def get_package_flags(self, package):
        """
        Retorna as USE flags específicas para um pacote.
        """
        pass

    def enable_global(self, flag):
        """
        Ativa uma USE flag globalmente.
        """
        pass

    def disable_global(self, flag):
        """
        Desativa uma USE flag globalmente.
        """
        pass

    def set_package_flags(self, package, flags):
        """
        Define flags específicas para um pacote.
        """
        pass
