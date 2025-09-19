# modules/cache/binpkg.py

import os
import tarfile

class BinPackageManager:
    """
    Gerencia pacotes binários (quickpkg).
    Permite criar pacotes binários a partir de instalações
    e instalar diretamente deles.
    """

    def __init__(self, binpkg_dir="/var/cache/source/binpkgs"):
        self.binpkg_dir = binpkg_dir
        os.makedirs(self.binpkg_dir, exist_ok=True)

    def create_binpkg(self, package_name, version, install_path):
        """
        Cria pacote binário (tar.gz) a partir de arquivos instalados.
        """
        filename = f"{package_name}-{version}.tar.gz"
        filepath = os.path.join(self.binpkg_dir, filename)

        with tarfile.open(filepath, "w:gz") as tar:
            tar.add(install_path, arcname=os.path.basename(install_path))

        return filepath

    def install_binpkg(self, package_name, version, dest_path):
        """
        Instala pacote binário diretamente no sistema.
        """
        filename = f"{package_name}-{version}.tar.gz"
        filepath = os.path.join(self.binpkg_dir, filename)

        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Binário {filename} não encontrado.")

        with tarfile.open(filepath, "r:gz") as tar:
            tar.extractall(path=dest_path)

        return dest_path
