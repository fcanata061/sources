# modules/verify/generator.py

from .verify import Verifier
import os

class HashGenerator:
    """
    Gera SHA256SUM de tarballs para inclusão nas receitas.
    """

    def __init__(self, repo_path="/usr/source"):
        self.repo_path = repo_path
        self.verifier = Verifier()

    def generate_for_tarball(self, tarball_path):
        """
        Gera o SHA256 de um tarball.
        """
        return self.verifier.sha256sum(tarball_path)

    def write_to_recipe(self, package, sha256):
        """
        Escreve o SHA256 na receita do pacote.
        (Formato a ser definido: recipe.yaml, .py ou outro)
        """
        pass
