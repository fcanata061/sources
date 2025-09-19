# modules/create/hash.py

from ..verify.verify import Verifier
import os

class RecipeHash:
    """
    Gera e injeta SHA256SUM na receita de um pacote.
    """

    def __init__(self, repo_path="/usr/source"):
        self.repo_path = repo_path
        self.verifier = Verifier()

    def generate_for_tarball(self, tarball_path):
        """
        Gera SHA256 de um tarball.
        """
        return self.verifier.sha256sum(tarball_path)

    def inject_into_recipe(self, recipe_file, sha256):
        """
        Substitui o valor de sha256 dentro da receita YAML.
        """
        updated_lines = []
        with open(recipe_file, "r") as f:
            for line in f:
                if line.startswith("sha256:"):
                    updated_lines.append(f"sha256: {sha256}\n")
                else:
                    updated_lines.append(line)

        with open(recipe_file, "w") as f:
            f.writelines(updated_lines)
