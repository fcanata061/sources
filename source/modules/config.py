import configparser
import os

DEFAULT_LOCATIONS = [
    "/etc/source/source.conf",
    os.path.expanduser("~/.config/source/source.conf"),
    "/run/source/source.conf",
]

class SourceConfig:
    def __init__(self, locations=None):
        self.locations = locations or DEFAULT_LOCATIONS
        self.config = configparser.ConfigParser()
        self.loaded_from = None
        self.reload()

    def reload(self):
        """(Re)carrega a configuração do primeiro arquivo disponível."""
        for path in self.locations:
            if os.path.isfile(path):
                self.config.read(path)
                self.loaded_from = path
                return
        raise FileNotFoundError(f"Nenhum arquivo de configuração encontrado em: {self.locations}")

    def get(self, section, option, fallback=None):
        try:
            return self.config.get(section, option, fallback=fallback)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return fallback

    def getboolean(self, section, option, fallback=False):
        try:
            return self.config.getboolean(section, option, fallback=fallback)
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return fallback

    def getint(self, section, option, fallback=0):
        try:
            return self.config.getint(section, option, fallback=fallback)
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return fallback

    def getlist(self, section, option, fallback=None, delimiter=","):
        raw = self.get(section, option, fallback="")
        if raw:
            return [item.strip() for item in raw.split(delimiter) if item.strip()]
        return fallback or []

    def __getitem__(self, section):
        if section in self.config:
            return dict(self.config[section])
        raise KeyError(f"Seção '{section}' não encontrada.")

    def __contains__(self, section):
        return section in self.config

# Instância global padrão para uso em outros módulos
config = SourceConfig()
