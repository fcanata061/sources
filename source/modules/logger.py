import os
import datetime
import threading
import json

from source.modules.config import config

class Logger:
    LEVELS = {
        "debug": 10,
        "info": 20,
        "success": 25,
        "warning": 30,
        "error": 40,
    }

    LOG_COLORS = {
        "DEBUG": "\033[90m",    # Cinza
        "INFO": "\033[94m",     # Azul
        "SUCCESS": "\033[92m",  # Verde
        "WARNING": "\033[93m",  # Amarelo
        "ERROR": "\033[91m",    # Vermelho
        "RESET": "\033[0m"
    }

    def __init__(self, name="source"):
        self.name = name
        self.log_file = config.get("logging", "log_file", fallback="/var/log/source.log")
        self.history_file = config.get("logging", "history_file", fallback="/var/log/source_history.log")
        self.color_output = config.getboolean("logging", "color_output", fallback=True)
        self.log_to_file = config.getboolean("logging", "log_to_file", fallback=True)
        self.log_to_console = config.getboolean("logging", "log_to_console", fallback=True)
        self.use_utc = config.getboolean("logging", "timestamp_utc", fallback=False)
        self.log_format = config.get("logging", "log_format", fallback="text").lower()
        self.max_log_size_kb = config.getint("logging", "max_log_size_kb", fallback=0)

        level_str = config.get("logging", "level", fallback="info").lower()
        self.min_level = self.LEVELS.get(level_str, 20)

        self._ensure_dir(self.log_file)
        self._ensure_dir(self.history_file)

        self._lock = threading.Lock()

    def _ensure_dir(self, filepath):
        dirpath = os.path.dirname(filepath)
        try:
            os.makedirs(dirpath, exist_ok=True)
        except Exception as e:
            print(f"Logger: falha ao criar diretório de log {dirpath}: {e}")

    def _get_timestamp(self):
        now = datetime.datetime.utcnow() if self.use_utc else datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")

    def _rotate_if_needed(self, filepath):
        if self.max_log_size_kb <= 0:
            return
        if os.path.exists(filepath) and os.path.getsize(filepath) > self.max_log_size_kb * 1024:
            rotated = filepath + ".1"
            try:
                if os.path.exists(rotated):
                    os.remove(rotated)
                os.rename(filepath, rotated)
            except Exception as e:
                print(f"Logger: erro ao rotacionar log {filepath}: {e}")

    def _write_file(self, filepath, message):
        if not self.log_to_file:
            return
        self._rotate_if_needed(filepath)
        try:
            with open(filepath, "a") as f:
                f.write(message + "\n")
        except Exception as e:
            print(f"Logger: falha ao escrever no arquivo de log {filepath}: {e}")

    def _format_text(self, level, message):
        timestamp = self._get_timestamp()
        return f"[{timestamp}] [{self.name}] [{level}] {message}"

    def _format_json(self, level, message):
        return json.dumps({
            "timestamp": self._get_timestamp(),
            "logger": self.name,
            "level": level,
            "message": message
        })

    def _format_message(self, level, message):
        if self.log_format == "json":
            return self._format_json(level, message)
        return self._format_text(level, message)

    def _log_to_console(self, formatted, level):
        if not self.log_to_console:
            return
        if self.color_output and self.log_format == "text":
            color = self.LOG_COLORS.get(level.upper(), "")
            reset = self.LOG_COLORS.get("RESET", "")
            print(f"{color}{formatted}{reset}")
        else:
            print(formatted)

    def _should_log(self, level):
        return self.LEVELS.get(level.lower(), 0) >= self.min_level

    def _record_history(self, formatted):
        self._write_file(self.history_file, formatted)

    def log(self, level, message, *, to_history=False):
        level = level.upper()
        if not self._should_log(level):
            return

        formatted = self._format_message(level, message)
        with self._lock:
            self._log_to_console(formatted, level)
            self._write_file(self.log_file, formatted)
            if to_history:
                self._record_history(formatted)

    def debug(self, message):
        self.log("DEBUG", message)

    def info(self, message, *, to_history=False):
        self.log("INFO", message, to_history=to_history)

    def success(self, message, *, to_history=False):
        self.log("SUCCESS", message, to_history=to_history)

    def warning(self, message, *, to_history=False):
        self.log("WARNING", message, to_history=to_history)

    def error(self, message, *, to_history=False):
        self.log("ERROR", message, to_history=to_history)
