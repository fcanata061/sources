# modules/dependencies/graph.py

class DependencyGraph:
    """
    Representa o grafo de dependências entre pacotes.
    Usado para ordenar builds e detectar conflitos.
    """

    def __init__(self):
        self.graph = {}  # {package: [dependencies]}

    def add_package(self, package, dependencies):
        """Adiciona um pacote e suas dependências ao grafo"""
        pass

    def topo_sort(self):
        """
        Retorna ordem correta para instalar/atualizar pacotes
        respeitando dependências.
        """
        pass

    def detect_cycles(self):
        """Detecta ciclos de dependências no grafo"""
        pass
