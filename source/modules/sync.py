import os
import logging
from pathlib import Path
from git import Repo, GitCommandError


class SyncError(Exception):
    pass


class SyncManager:
    """Gerencia a sincronização do repositório principal (/usr/source)."""

    def __init__(self, config_file="/etc/source/source.conf", repo_path="/usr/source"):
        self.config_file = config_file
        self.repo_path = Path(repo_path)
        self.repo_url, self.branch = self._load_config()

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )

    def _load_config(self):
        """Carrega configurações do /etc/source/source.conf."""
        repo_url, branch = None, "main"
        if not os.path.exists(self.config_file):
            raise SyncError(f"Arquivo de configuração não encontrado: {self.config_file}")

        with open(self.config_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("REPO_URL="):
                    repo_url = line.split("=", 1)[1].strip()
                elif line.startswith("BRANCH="):
                    branch = line.split("=", 1)[1].strip()

        if not repo_url:
            raise SyncError("Configuração inválida: REPO_URL não definido.")

        return repo_url, branch

    def is_cloned(self):
        """Verifica se o repositório já foi clonado em /usr/source."""
        return (self.repo_path / ".git").exists()

    def clone_repo(self):
        """Faz o clone inicial do repositório."""
        try:
            logging.info(f"Clonando {self.repo_url} em {self.repo_path} (branch {self.branch})...")
            Repo.clone_from(self.repo_url, self.repo_path, branch=self.branch)
            logging.info("Clone concluído.")
        except GitCommandError as e:
            raise SyncError(f"Erro ao clonar repositório: {e}")

    def update_repo(self):
        """Atualiza o repositório existente via pull."""
        try:
            logging.info(f"Atualizando repositório em {self.repo_path}...")
            repo = Repo(self.repo_path)
            repo.git.checkout(self.branch)
            repo.remotes.origin.pull()
            logging.info("Repositório atualizado com sucesso.")
        except GitCommandError as e:
            raise SyncError(f"Erro ao atualizar repositório: {e}")

    def sync(self):
        """Sincroniza o repositório (clone ou pull)."""
        if not self.is_cloned():
            self.clone_repo()
        else:
            self.update_repo()
