# modules/cache/cache.py

import os
import shutil

class CacheManager:
    """
    Gerencia cache de downloads e arquivos de origem (distfiles).
    """

    def __init__(self, cache_dir="/var/cache/source/distfiles"):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    def store_file(self, file_path):
        """
        Armazena um arquivo baixado no cache.
        """
        dest = os.path.join(self.cache_dir, os.path.basename(file_path))
        shutil.copy(file_path, dest)
        return dest

    def get_file(self, filename):
        """
        Retorna o caminho do arquivo no cache, se existir.
        """
        cached_file = os.path.join(self.cache_dir, filename)
        if os.path.exists(cached_file):
            return cached_file
        return None

    def clean_cache(self):
        """
        Remove todos os arquivos do cache.
        """
        for f in os.listdir(self.cache_dir):
            os.remove(os.path.join(self.cache_dir, f))
