import os
import json
import yaml
import logging
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from git import Repo, GitCommandError
import typer


class OverlayError(Exception):
    pass


class OverlayManager:
    """Gerencia múltiplos repositórios (overlays)."""

    def __init__(
        self,
        overlays_config="/etc/source/overlays.yaml",
        overlays_base_dir="/var/lib/overlays",
        max_workers=4,
    ):
        self.overlays_config = overlays_config
        self.overlays_base_dir = Path(overlays_base_dir)
        self.max_workers = max_workers
        self.overlays = {}  # name -> {url, branch, tag, commit, path, hooks, status}
        self._ensure_base_dir()
        self.load_overlays()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )

    def _ensure_base_dir(self):
        self.overlays_base_dir.mkdir(parents=True, exist_ok=True)

    def load_overlays(self):
        """Carrega lista de overlays do arquivo YAML/JSON."""
        if not os.path.exists(self.overlays_config):
            self.overlays = {}
            return
        try:
            with open(self.overlays_config, "r", encoding="utf-8") as f:
                if self.overlays_config.endswith(".json"):
                    data = json.load(f)
                else:
                    data = yaml.safe_load(f)
            for ov in data.get("overlays", []):
                name = ov["name"]
                self.overlays[name] = {
                    "url": ov["url"],
                    "branch": ov.get("branch", "main"),
                    "tag": ov.get("tag"),
                    "commit": ov.get("commit"),
                    "hooks": ov.get("hooks", {}),
                    "path": str(Path(ov.get("path", self.overlays_base_dir / name))),
                    "status": "unknown",
                }
        except Exception as e:
            raise OverlayError(f"Erro ao ler config {self.overlays_config}: {e}")

    def save_overlays(self):
        """Salva overlays em YAML/JSON."""
        data = {
            "overlays": [
                {
                    "name": n,
                    "url": o["url"],
                    "branch": o["branch"],
                    "tag": o.get("tag"),
                    "commit": o.get("commit"),
                    "hooks": o.get("hooks", {}),
                    "path": o["path"],
                }
                for n, o in self.overlays.items()
            ]
        }
        try:
            os.makedirs(os.path.dirname(self.overlays_config), exist_ok=True)
            with open(self.overlays_config, "w", encoding="utf-8") as f:
                if self.overlays_config.endswith(".json"):
                    json.dump(data, f, indent=2, ensure_ascii=False)
                else:
                    yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        except Exception as e:
            raise OverlayError(f"Erro ao salvar config: {e}")

    def add_overlay(self, name, url, branch="main", tag=None, commit=None, hooks=None):
        if name in self.overlays:
            raise OverlayError(f"Overlay '{name}' já existe.")
        path = self.overlays_base_dir / name
        self.overlays[name] = {
            "url": url,
            "branch": branch,
            "tag": tag,
            "commit": commit,
            "hooks": hooks or {},
            "path": str(path),
            "status": "new",
        }
        self.save_overlays()
        logging.info(f"Overlay '{name}' adicionado ({url}).")

    def remove_overlay(self, name, remove_local=False):
        if name not in self.overlays:
            raise OverlayError(f"Overlay '{name}' não encontrado.")
        ov = self.overlays.pop(name)
        self.save_overlays()
        if remove_local and Path(ov["path"]).exists():
            subprocess.run(["rm", "-rf", ov["path"]], check=True)
            logging.info(f"Overlay '{name}' removido com diretório local.")
        else:
            logging.info(f"Overlay '{name}' removido (config apenas).")

    def sync_overlays(self, parallel=True):
        """Sincroniza todos os overlays."""
        if parallel:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {executor.submit(self._sync_one, n): n for n in self.overlays}
                for fut in as_completed(futures):
                    name = futures[fut]
                    try:
                        fut.result()
                        logging.info(f"[{name}] sincronizado com sucesso.")
                    except Exception as e:
                        logging.error(f"[{name}] erro: {e}")
        else:
            for name in self.overlays:
                self._sync_one(name)

    def _sync_one(self, name):
        ov = self.overlays[name]
        path = Path(ov["path"])
        url, branch, tag, commit = ov["url"], ov["branch"], ov.get("tag"), ov.get("commit")

        try:
            if not path.exists():
                repo = Repo.clone_from(url, path, branch=branch)
            else:
                repo = Repo(path)
                repo.remotes.origin.fetch()
                repo.git.checkout(branch)
                repo.remotes.origin.pull()

            if tag:
                repo.git.checkout(f"tags/{tag}")
            if commit:
                repo.git.checkout(commit)

            ov["status"] = "up-to-date"
            self._run_hooks(ov)

        except GitCommandError as e:
            ov["status"] = f"error: {e}"
            raise OverlayError(f"Falha em overlay '{name}': {e}")

    def _run_hooks(self, overlay):
        """Executa hooks pós-sync, se existirem."""
        hooks = overlay.get("hooks", {})
        if "post_sync" in hooks:
            try:
                subprocess.run(hooks["post_sync"], shell=True, cwd=overlay["path"], check=True)
                logging.info(f"[{overlay}] hook pós-sync executado.")
            except subprocess.CalledProcessError as e:
                logging.error(f"Hook pós-sync falhou em {overlay}: {e}")

    def list_overlays(self):
        return list(self.overlays.keys())

    def status(self):
        """Mostra status resumido de cada overlay (último commit)."""
        result = {}
        for name, ov in self.overlays.items():
            path = Path(ov["path"])
            if path.exists():
                try:
                    repo = Repo(path)
                    commit = repo.head.commit.hexsha[:8]
                    result[name] = {
                        "url": ov["url"],
                        "branch": ov["branch"],
                        "commit": commit,
                        "status": ov.get("status", "unknown"),
                    }
                except Exception as e:
                    result[name] = {"url": ov["url"], "status": f"erro: {e}"}
            else:
                result[name] = {"url": ov["url"], "status": "não clonado"}
        return result


# ----------------- CLI -----------------

app = typer.Typer(help="Gerenciador de Overlays")

def get_manager():
    return OverlayManager(
        overlays_config=os.environ.get("OVERLAY_CONFIG", "./overlays.yaml"),
        overlays_base_dir=os.environ.get("OVERLAY_DIR", "./overlays"),
    )


@app.command()
def list():
    om = get_manager()
    typer.echo("\n".join(om.list_overlays()))


@app.command()
def add(name: str, url: str, branch: str = "main", tag: str = None, commit: str = None):
    om = get_manager()
    om.add_overlay(name, url, branch=branch, tag=tag, commit=commit)


@app.command()
def remove(name: str, remove_local: bool = typer.Option(False, help="Remover diretório local também")):
    om = get_manager()
    om.remove_overlay(name, remove_local=remove_local)


@app.command()
def sync(parallel: bool = typer.Option(True, help="Sincronizar em paralelo")):
    om = get_manager()
    om.sync_overlays(parallel=parallel)


@app.command()
def status():
    om = get_manager()
    for name, st in om.status().items():
        typer.echo(f"{name}: {st}")


if __name__ == "__main__":
    app()
