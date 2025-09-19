# modules/utils/utils.py

import os

class Utils:
    """
    Funções utilitárias gerais usadas por outros módulos.
    """

    @staticmethod
    def ensure_dir(path):
        """
        Cria diretório se não existir.
        """
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def join_path(*args):
        """
        Retorna caminho absoluto concatenando partes.
        """
        return os.path.abspath(os.path.join(*args))

    @staticmethod
    def list_subdirs(path):
        """
        Retorna lista de subdiretórios de um diretório.
        """
        if not os.path.exists(path):
            return []
        return [d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))]

    @staticmethod
    def read_file(path):
        """
        Lê arquivo e retorna conteúdo.
        """
        with open(path, "r") as f:
            return f.read()

    @staticmethod
    def write_file(path, content):
        """
        Escreve conteúdo em arquivo.
        """
        with open(path, "w") as f:
            f.write(content)

    @staticmethod
    def color_text(text, color_code):
        """
        Retorna texto formatado com ANSI color.
        """
        return f"{color_code}{text}\033[0m"

    @staticmethod
    def parse_recipe(recipe_file):
        """
        Função placeholder para parsear recipe (YAML ou outro formato).
        """
        return {}
