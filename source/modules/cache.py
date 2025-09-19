# source/modules/cache.py

"""
CacheManager avançado para diferentes tipos de arquivos de cache:
 - distfiles (originais/arqs fontes baixados)
 - binpkgs (pacotes binários)
 - metadata

Funcionalidades:
 - armazenar/recuperar arquivos do cache
 - verificação de checksum
 - políticas de limpeza: LRU, TTL (tempo de vida), tamanho máximo
 - integração com hooks (pre_store, post_store, pre_fetch, post_fetch)
 - suporte a cache remoto stub
 - modo dry-run
"""

import os
import shutil
import time
import hashlib
from typing import Optional, Callable, Dict, Any
from datetime import datetime, timedelta

from modules import logger as _logger
from modules import hooks as _hooks

# configuração padrão — pode ser substituída via source.conf ou parâmetros
DEFAULT_CACHE_CONFIG = {
    "distfiles": {
        "path": "/var/cache/source/distfiles",
        "max_size_mb": 1024,    # tamanho limite
        "ttl_days": 7           # tempo de vida em dias
    },
    "binpkgs": {
        "path": "/var/cache/source/binpkgs",
        "max_size_mb": 512,
        "ttl_days": 30
    },
    "metadata": {
        "path": "/var/cache/source/metadata",
        "max_size_mb": 100,
        "ttl_days": 30
    }
}

class CacheManager:
    def __init__(self,
                 cache_types: Optional[Dict[str, Dict[str, Any]]] = None,
                 config_override: Optional[Dict[str, Dict[str, Any]]] = None,
                 dry_run: bool = False):
        """
        cache_types: dicionário definindo tipos de cache e suas configurações
        config_override: valores para substituir DEFAULT_CACHE_CONFIG parcialmente
        dry_run: modo simulação
        """
        self.dry_run = dry_run
        self.log = _logger.Logger("cache.log")
        self.hooks = _hooks.HookManager(dry_run=dry_run) if _hooks else None

        # configurar caches
        self.cache_types = cache_types or {}
        # iniciar com defaults
        for ctype, cfg in DEFAULT_CACHE_CONFIG.items():
            if ctype not in self.cache_types:
                self.cache_types[ctype] = cfg.copy()
        # aplicar overrides
        if config_override:
            for ctype, conf in config_override.items():
                self.cache_types.setdefault(ctype, {}).update(conf)

        # garantir existência dos diretórios
        for ctype, conf in self.cache_types.items():
            p = os.path.abspath(conf.get("path"))
            os.makedirs(p, exist_ok=True)
            conf["path"] = p

    def _hash_file(self, file_path: str, algorithm: str = "sha256") -> str:
        h = getattr(hashlib, algorithm, hashlib.sha256)()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    # ------------------------
    # Store / Fetch
    # ------------------------
    def store(self, ctype: str, file_path: str, checksum: Optional[str] = None) -> Optional[str]:
        """
        Armazena um arquivo no cache do tipo ctype.
        Se checksum fornecido, verifica se bate; se não, calcula.
        Retorna caminho no cache ou None (se dry_run).
        """
        if ctype not in self.cache_types:
            raise ValueError(f"Tipo de cache desconhecido: {ctype}")

        conf = self.cache_types[ctype]
        dest_dir = conf["path"]
        base = os.path.basename(file_path)
        dest = os.path.join(dest_dir, base)

        if self.hooks:
            try:
                self.hooks.run_hooks("pre_store", {"ctype": ctype, "file": file_path}, None)
            except Exception as e:
                self.log.error(f"Hook pre_store falhou: {e}")

        if checksum:
            calc = self._hash_file(file_path)
            if calc != checksum:
                self.log.error(f"Checksum mismatch ao armazenar {file_path}: esperado {checksum}, obtido {calc}")
                # continuar ou abortar? Vamos abortar
                raise IOError("Checksum mismatch in store")

        if self.dry_run:
            self.log.info(f"[DRY-RUN] Armazenaria {file_path} -> {dest}")
            if self.hooks:
                try:
                    self.hooks.run_hooks("post_store", {"ctype": ctype, "file": dest}, None)
                except Exception as e:
                    self.log.error(f"Hook post_store falhou: {e}")
            return None

        shutil.copy2(file_path, dest)
        self.log.info(f"Armazenado no cache {ctype}: {dest}")

        if self.hooks:
            try:
                self.hooks.run_hooks("post_store", {"ctype": ctype, "file": dest}, None)
            except Exception as e:
                self.log.error(f"Hook post_store falhou: {e}")

        return dest

    def fetch(self, ctype: str, filename: str, expected_checksum: Optional[str] = None) -> Optional[str]:
        """
        Busca um arquivo de cache. Se expected_checksum dado, verifica.
        Retorna caminho ou None se não encontrado.
        """
        if ctype not in self.cache_types:
            raise ValueError(f"Tipo de cache desconhecido: {ctype}")

        conf = self.cache_types[ctype]
        path = os.path.join(conf["path"], filename)

        if self.hooks:
            try:
                self.hooks.run_hooks("pre_fetch", {"ctype": ctype, "file": filename}, None)
            except Exception as e:
                self.log.error(f"Hook pre_fetch falhou: {e}")

        if not os.path.exists(path):
            self.log.debug(f"Arquivo não encontrado no cache {ctype}: {filename}")
            return None

        if expected_checksum:
            calc = self._hash_file(path)
            if calc != expected_checksum:
                self.log.error(f"Checksum mismatch ao buscar {filename}: esperado {expected_checksum}, obtido {calc}")
                # considerar remover arquivo corrompido?
                return None

        if self.dry_run:
            self.log.info(f"[DRY-RUN] Recuperaria {filename} de cache {ctype} em {path}")
            if self.hooks:
                try:
                    self.hooks.run_hooks("post_fetch", {"ctype": ctype, "file": path}, None)
                except Exception as e:
                    self.log.error(f"Hook post_fetch falhou: {e}")
            return None

        self.log.info(f"Cache hit {ctype}: {path}")

        if self.hooks:
            try:
                self.hooks.run_hooks("post_fetch", {"ctype": ctype, "file": path}, None)
            except Exception as e:
                self.log.error(f"Hook post_fetch falhou: {e}")

        return path

    # ------------------------
    # Cleanup / policy
    # ------------------------
    def clean_type(self, ctype: str, keep_recent: bool = True) -> None:
        """
        Limpa cache de um tipo específico com base em política de TTL ou tamanho.

        Se keep_recent: mantém os arquivos mais recentes; caso contrário
        remove tudo exceto o que estiver em uso.
        """
        if ctype not in self.cache_types:
            raise ValueError(f"Tipo de cache desconhecido: {ctype}")
        conf = self.cache_types[ctype]
        path = conf["path"]
        ttl_days = conf.get("ttl_days")
        max_size_mb = conf.get("max_size_mb")

        now = time.time()
        entries = []
        # coletar arquivos com suas mtimes e tamanhos
        for fn in os.listdir(path):
            fp = os.path.join(path, fn)
            try:
                st = os.stat(fp)
            except Exception:
                continue
            entries.append((fp, st.st_mtime, st.st_size))

        # política TTL
        if ttl_days is not None:
            cutoff = now - (ttl_days * 86400)
            for fp, mtime, _ in entries:
                if mtime < cutoff:
                    if self.dry_run:
                        self.log.info(f"[DRY-RUN] TTL: Remover {fp}")
                    else:
                        try:
                            os.remove(fp)
                            self.log.info(f"Removido por TTL {fp}")
                        except Exception as e:
                            self.log.error(f"Falha remoção TTL {fp}: {e}")

        # política de tamanho
        if max_size_mb is not None:
            total = sum(size for _, _, size in entries)
            limit = max_size_mb * 1024 * 1024
            if total > limit:
                # ordenar por mtime asc (mais antigos primeiro)
                entries.sort(key=lambda x: x[1])
                to_remove = []
                accumulated = total
                for fp, mtime, size in entries:
                    if accumulated <= limit:
                        break
                    to_remove.append((fp, size))
                    accumulated -= size
                for fp, size in to_remove:
                    if self.dry_run:
                        self.log.info(f"[DRY-RUN] Limpeza de tamanho: Remover {fp}")
                    else:
                        try:
                            os.remove(fp)
                            self.log.info(f"Removido por tamanho {fp}")
                        except Exception as e:
                            self.log.error(f"Falha remoção tamanho {fp}: {e}")

    def clean_all(self, keep_recent: bool = True) -> None:
        """
        Limpa todos os tipos de cache existentes.
        """
        for ctype in self.cache_types.keys():
            self.clean_type(ctype, keep_recent=keep_recent)

    # ------------------------
    # Remote cache stub
    # ------------------------
    def fetch_remote(self, url: str, dest_filename: Optional[str] = None) -> Optional[str]:
        """
        Tenta baixar um arquivo remoto para cache (download + store).
        Se dest_filename dado, nome no cache; senão usa basename.
        """
        # stub: implementar mirrors ou servidor HTTP
        self.log.info(f"Fetch remoto (stub) {url}")
        # poderia checar se já no cache

        try:
            import urllib.request
            tmpfd, tmpname = tempfile.mkstemp()
            os.close(tmpfd)
            if self.dry_run:
                self.log.info(f"[DRY-RUN] Baixaria {url} para {tmpname}")
                return None
            with urllib.request.urlopen(url) as r, open(tmpname, "wb") as out:
                shutil.copyfileobj(r, out)
            basename = dest_filename or os.path.basename(url)
            return self.store("distfiles", tmpname)
        except Exception as e:
            self.log.error(f"Falha fetch remoto: {e}")
            return None

    # ------------------------
    # CLI
    # ------------------------
    def cli_store(self, args):
        path = args.file
        ctype = args.type
        checksum = getattr(args, "checksum", None)
        res = self.store(ctype, path, checksum=checksum)
        print("Stored:", res)
        return 0

    def cli_fetch(self, args):
        ctype = args.type
        fname = args.filename
        checksum = getattr(args, "checksum", None)
        res = self.fetch(ctype, fname, expected_checksum=checksum)
        print("Found:", res)
        return 0

    def cli_clean(self, args):
        ctype = getattr(args, "type", None)
        keep = not args.remove_all
        if ctype:
            self.clean_type(ctype, keep_recent=keep)
        else:
            self.clean_all(keep_recent=keep)
        print("Cache cleaned.")
        return 0


# ------------------------
# CLI entrypoint
# ------------------------
def main_cli():
    import argparse
    parser = argparse.ArgumentParser(prog="cache", description="Gerenciar cache de distfiles/binpkgs/metadata")
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_store = sub.add_parser("store", help="Armazenar arquivo no cache")
    p_store.add_argument("type", help="Tipo de cache (distfiles/binpkgs/metadata)")
    p_store.add_argument("file", help="Caminho do arquivo à armazenar")
    p_store.add_argument("--checksum")

    p_fetch = sub.add_parser("fetch", help="Buscar arquivo no cache")
    p_fetch.add_argument("type", help="Tipo de cache")
    p_fetch.add_argument("filename", help="Nome do arquivo no cache")
    p_fetch.add_argument("--checksum")

    p_clean = sub.add_parser("clean", help="Limpar cache")
    p_clean.add_argument("--type", help="Tipo de cache específico")
    p_clean.add_argument("--remove-all", action="store_true", help="Remover tudo do cache")

    args = parser.parse_args()
    mgr = CacheManager(dry_run=args.dry_run)
    if args.cmd == "store":
        return mgr.cli_store(args)
    elif args.cmd == "fetch":
        return mgr.cli_fetch(args)
    elif args.cmd == "clean":
        return mgr.cli_clean(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main_cli())
