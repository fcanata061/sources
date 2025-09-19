# modules/sync/overlay.py

class OverlayManager:
    """
    Suporte a múltiplos repositórios (overlays).
    Permite adicionar repositórios extras além do principal.
    """

    def __init__(self, overlays_config="/etc/source/overlays.conf"):
        self.overlays_config = overlays_config
        self.overlays = []

    def load_overlays(self):
        """
        Carrega lista de repositórios extras do arquivo de configuração.
        """
        pass

    def add_overlay(self, name, url):
        """
        Adiciona um overlay (nome + URL do git).
        """
        pass

    def remove_overlay(self, name):
        """
        Remove um overlay pelo nome.
        """
        pass

    def sync_overlays(self):
        """
        Sincroniza todos os overlays registrados.
        """
        pass
