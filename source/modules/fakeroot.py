# modules/install/fakeroot.py

class FakeRoot:
    """
    Simula root para instalação sem alterar sistema real
    """
    def __init__(self, dest_path):
        self.dest_path = dest_path

    def install_files(self, source_files):
        """Copia arquivos para o DESTDIR do fakeroot"""
        pass
