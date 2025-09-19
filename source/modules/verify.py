# modules/verify/verify.py

import hashlib
import os

class Verifier:
    """
    Verifica integridade de pacotes usando SHA256SUM.
    """

    def __init__(self, cache_dir="/var/cache/source/distfiles"):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    def sha256sum(self, file_path):
        """
        Calcula o SHA256 de um arquivo.
        """
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def verify_file(self, file_path, expected_hash):
        """
        Verifica se o arquivo corresponde ao hash esperado.
        Retorna True/False.
        """
        if not os.path.exists(file_path):
            return False

        file_hash = self.sha256sum(file_path)
        return file_hash == expected_hash
