import os
import configparser
from pathlib import Path

class SourceConfig:
    def __init__(self, config_file=None):
        # Local padrão
        default_path = "/etc/source/source.conf"

        # Permite também arquivo local no repositório
        local_path = Path(__file__).resolve().parent.parent / "source.conf"

        if config_file:
            self.config_file = config_file
        elif os.path.exists(local_path):
            self.config_file = str(local_path)
        else:
            self.config_file = default_path

        self.parser = configparser.ConfigParser()
        self.load()

    def load(self):
        if not os.path.exists(self.config_file):
            raise FileNotFoundError(f"Arquivo de configuração não encontrado: {self.config_file}")
        self.parser.read(self.config_file)

    def get(self, section, option, fallback=None, type=str):
        if not self.parser.has_section(section):
            return fallback
        if not self.parser.has_option(section, option):
            return fallback

        value = self.parser.get(section, option, fallback=fallback)

        # Conversão de tipos
        if type == bool:
            return self.parser.getboolean(section, option, fallback=fallback)
        elif type == int:
            return self.parser.getint(section, option, fallback=fallback)
        elif type == float:
            return self.parser.getfloat(section, option, fallback=fallback)
        return value

    # ===== Acessos rápidos =====
    @property
    def recipes_dir(self):
        return self.get("core", "recipes_dir", "/usr/sources/recipes")

    @property
    def binpkg_dir(self):
        return self.get("core", "binpkg_dir", "/usr/sources/binpkgs")

    @property
    def cache_dir(self):
        return self.get("core", "cache_dir", "/var/cache/source")

    @property
    def log_dir(self):
        return self.get("core", "log_dir", "/var/log/source")

    @property
    def use_colors(self):
        return self.get("core", "use_colors", True, bool)

    @property
    def use_animations(self):
        return self.get("core", "use_animations", True, bool)

    @property
    def dry_run(self):
        return self.get("core", "dry_run", False, bool)

    @property
    def repo_url(self):
        return self.get("sync", "repo_url")

    @property
    def branch(self):
        return self.get("sync", "branch", "main")

    @property
    def force_update(self):
        return self.get("sync", "force_update", True, bool)

    @property
    def make_jobs(self):
        return self.get("build", "make_jobs", 1, int)

    @property
    def fakeroot(self):
        return self.get("build", "fakeroot", True, bool)

    @property
    def sandbox(self):
        return self.get("build", "sandbox", True, bool)

    @property
    def pre_hooks(self):
        return self.get("hooks", "pre_hooks")

    @property
    def post_hooks(self):
        return self.get("hooks", "post_hooks")

    @property
    def notify_enabled(self):
        return self.get("notifications", "enabled", True, bool)

    @property
    def notify_title(self):
        return self.get("notifications", "title", "Source Package Manager")

    @property
    def updates_file(self):
        return self.get("update", "updates_file", "/var/log/source/updates.log")

    @property
    def check_interval_days(self):
        return self.get("update", "check_interval_days", 1, int)


# Instância global para todo o sistema
config = SourceConfig()
