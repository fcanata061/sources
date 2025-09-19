# modules/install/sandbox.py

class Sandbox:
    """
    Gerencia o ambiente isolado de build
    """
    def __init__(self, sandbox_path):
        self.sandbox_path = sandbox_path

    def create(self):
        """Cria diretórios de sandbox"""
        pass

    def clean(self):
        """Remove sandbox após build/install"""
        pass
