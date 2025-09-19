# modules/logging/logger.py

import os
import datetime

class Logger:
    """
    Sistema de logging do source.
    - Saída colorida no terminal
    - Registro em arquivo
    - Histórico de ações (para auditoria/reversão futura)
    """

    LOG_COLORS = {
        "INFO": "\033[94m",    # Azul
        "SUCCESS": "\033[92m", # Verde
        "WARNING": "\033[93m", # Amarelo
        "ERROR": "\033[91m",   # Vermelho
        "RESET": "\033[0m"
    }

    def __init__(self, log_file="/var/log/source.log"):
        self.log_file = log_file
        os.makedirs(os.path.dirname(log_file), exist_ok=True)

    def _write_file(self, message):
        """Escreve mensagem no arquivo de log"""
        with open(self.log_file, "a") as f:
            f.write(message + "\n")

    def _format_message(self, level, message):
        """Formata mensagem com timestamp e nível"""
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"[{timestamp}] [{level}] {message}"

    def log(self, level, message):
        """Log genérico"""
        formatted = self._format_message(level, message)
        color = self.LOG_COLORS.get(level, "")
        reset = self.LOG_COLORS["RESET"]
        # terminal
        print(f"{color}{formatted}{reset}")
        # arquivo
        self._write_file(formatted)

    def info(self, message):
        self.log("INFO", message)

    def success(self, message):
        self.log("SUCCESS", message)

    def warning(self, message):
        self.log("WARNING", message)

    def error(self, message):
        self.log("ERROR", message)
