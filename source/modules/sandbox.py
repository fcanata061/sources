# source/modules/sandbox.py
import os
import shutil
import tarfile
import hashlib
import json
import subprocess
import time
from datetime import datetime
from modules import logger


class SandboxError(Exception):
    pass


class Sandbox:
    """
    Sandbox avançado para builds isolados.
    Recursos:
      - Snapshots e rollback
      - Metadados detalhados
      - Camadas (overlays)
      - Controle de integridade (hashes)
      - Execução de comandos isolados
      - Histórico de operações
      - Modo dry-run
    """

    def __init__(self, package_name: str, base_dir: str = "sandbox", dry_run: bool = False, quota_mb: int = None):
        self.package_name = package_name
        self.base_dir = os.path.abspath(base_dir)
        self.path = os.path.join(self.base_dir, package_name)
        self.meta_file = os.path.join(self.path, ".metadata.json")
        self.log = logger.Logger(f"{self.package_name}.log")
        self.dry_run = dry_run
        self.quota_mb = quota_mb
        self.history = []

    # -------------------------------
    # Core
    # -------------------------------
    def prepare(self, clean: bool = True, std_dirs=True, metadata=None):
        """Cria sandbox, opcionalmente limpando se já existir"""
        if clean and os.path.exists(self.path):
            self.log.info(f"Limpando sandbox antigo em {self.path}")
            if not self.dry_run:
                shutil.rmtree(self.path)

        self.log.info(f"Preparando sandbox em {self.path}")
        if not self.dry_run:
            os.makedirs(self.path, exist_ok=True)
            if std_dirs:
                self._create_standard_dirs()
            if metadata:
                self._write_metadata(metadata)

        self._record("prepare", {"clean": clean})

    def _create_standard_dirs(self):
        """Cria diretórios padrão"""
        std_dirs = [
            "bin", "lib", "include", "share", "etc",
            "usr/bin", "usr/lib", "usr/include", "usr/share",
            "var", "tmp"
        ]
        for d in std_dirs:
            path = os.path.join(self.path, d)
            os.makedirs(path, exist_ok=True)
        self.log.debug(f"Diretórios padrão criados em {self.path}")

    def clean(self):
        """Remove completamente o sandbox"""
        if os.path.exists(self.path):
            self.log.info(f"Removendo sandbox {self.path}")
            if not self.dry_run:
                shutil.rmtree(self.path)
        self._record("clean")

    # -------------------------------
    # Execução dentro do sandbox
    # -------------------------------
    def run(self, command: list, env=None, cwd=None, use_fakeroot=True, check=True):
        """Executa comando dentro do sandbox"""
        self.log.info(f"Executando no sandbox: {' '.join(command)}")

        if self.dry_run:
            self.log.info("[DRY-RUN] Comando não executado")
            return 0, "", ""

        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        sandbox_cwd = cwd or self.path
        os.makedirs(sandbox_cwd, exist_ok=True)

        prefix = ["fakeroot"] if use_fakeroot else []
        proc = subprocess.Popen(
            prefix + command,
            cwd=sandbox_cwd,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate()

        if proc.returncode != 0 and check:
            raise SandboxError(f"Erro ao executar {' '.join(command)}: {err}")

        self._record("run", {"command": command, "rc": proc.returncode})
        return proc.returncode, out, err

    # -------------------------------
    # Snapshots & rollback
    # -------------------------------
    def snapshot(self, name=None):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        snap_name = name or f"{self.package_name}-snap-{ts}.tar.gz"
        snap_path = os.path.join(self.base_dir, snap_name)

        self.log.info(f"Criando snapshot {snap_path}")
        if self.dry_run:
            return snap_path

        with tarfile.open(snap_path, "w:gz") as tar:
            tar.add(self.path, arcname=os.path.basename(self.path))

        self._record("snapshot", {"file": snap_path})
        return snap_path

    def restore(self, archive_file: str):
        self.log.info(f"Restaurando snapshot {archive_file}")
        if self.dry_run:
            return

        if os.path.exists(self.path):
            shutil.rmtree(self.path)
        with tarfile.open(archive_file, "r:gz") as tar:
            tar.extractall(path=self.base_dir)

        self._record("restore", {"file": archive_file})

    # -------------------------------
    # Metadados
    # -------------------------------
    def _write_metadata(self, metadata: dict):
        data = {
            "package": self.package_name,
            "created_at": datetime.now().isoformat(),
            "history": self.history,
            **metadata,
        }
        if not self.dry_run:
            with open(self.meta_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        self.log.debug(f"Metadados gravados em {self.meta_file}")

    def read_metadata(self):
        if os.path.exists(self.meta_file):
            with open(self.meta_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    # -------------------------------
    # Integridade
    # -------------------------------
    def checksums(self, algorithm="sha256"):
        sums = {}
        if not os.path.exists(self.path):
            return sums

        algo = getattr(hashlib, algorithm, hashlib.sha256)
        for root, _, files in os.walk(self.path):
            for f in files:
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, self.path)
                with open(fp, "rb") as fh:
                    h = algo(fh.read()).hexdigest()
                sums[rel] = h
        self._record("checksums", {"files": len(sums)})
        return sums

    # -------------------------------
    # Cotas e monitoramento
    # -------------------------------
    def size(self) -> int:
        total = 0
        for root, _, files in os.walk(self.path):
            for f in files:
                fp = os.path.join(root, f)
                total += os.path.getsize(fp)
        return total

    def check_quota(self):
        if self.quota_mb:
            used_mb = self.size() / (1024 * 1024)
            if used_mb > self.quota_mb:
                raise SandboxError(
                    f"Quota excedida: {used_mb:.2f}MB usados, limite {self.quota_mb}MB"
                )
            self.log.debug(f"Uso atual: {used_mb:.2f}MB (limite {self.quota_mb}MB)")

    # -------------------------------
    # Histórico de operações
    # -------------------------------
    def _record(self, action, details=None):
        entry = {
            "action": action,
            "timestamp": datetime.now().isoformat(),
            "details": details or {},
        }
        self.history.append(entry)

    def history_log(self):
        return self.history
