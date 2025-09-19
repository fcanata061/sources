# modules/create/recipe.py

import os

class RecipeCreator:
    """
    Cria diretório e receita base para um novo pacote.
    """

    def __init__(self, repo_path="/usr/source"):
        self.repo_path = repo_path

    def create_package_dir(self, package_name):
        """
        Cria diretório do pacote dentro de /usr/source/<pacote>/
        """
        package_dir = os.path.join(self.repo_path, package_name)
        os.makedirs(package_dir, exist_ok=True)
        return package_dir

    def create_base_recipe(self, package_name, version="1.0.0"):
        """
        Cria uma receita base (exemplo em YAML) para o pacote.
        """
        package_dir = self.create_package_dir(package_name)
        recipe_file = os.path.join(package_dir, f"{package_name}-{version}.yaml")

        content = f"""# Receita para {package_name}
name: {package_name}
version: {version}
source: URL_DO_TARBALL
sha256: SHA256SUM_AQUI

build:
  system: autotools  # opções: autotools, meson, ninja, cmake, rust, python
  steps: []

dependencies:
  build: []
  runtime: []

use_flags:
  enabled: []
  disabled: []

hooks:
  pre_configure: []
  post_configure: []
  pre_install: []
  post_install: []
  pre_remove: []
  post_remove: []
"""

        with open(recipe_file, "w") as f:
            f.write(content)

        return recipe_file
