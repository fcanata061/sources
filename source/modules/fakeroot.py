# source/modules/fakeroot.py
import os
import subprocess
import threading
import queue
import shlex
import time
import json
import asyncio
from datetime import datetime
from modules import logger


class FakerootError(Exception):
    pass


class CommandResult:
    """Estrutura de resultado de um comando executado"""

    def __init__(self, command, returncode, stdout, stderr, duration):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.duration = duration
        self.timestamp = datetime.now().isoformat()

    def ok(self):
        return self.returncode == 0

    def to_dict(self):
        return {
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": self.duration,
            "timestamp": self.timestamp,
        }


class Fakeroot:
    """
    Executor avançado de comandos dentro de fakeroot.
    Recursos:
      - Timeout, retry, background jobs
      - Async/await (streaming de saída em tempo real)
      - Pipelines (cmd1 | cmd2)
      - Hooks pré/pós
      - Perfis de execução
      - Controle de recursos (CPU/mem, futuro via cgroups)
      - Histórico e relatórios avançados
      - Plugins para integração externa
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.log = logger.Logger("fakeroot.log")
        self.history = []
        self.pre_hooks = []
        self.post_hooks = []
        self.profiles = {
            "default": {"timeout": None, "retries": 1},
            "build": {"timeout": 600, "retries": 2},
            "test": {"timeout": 300, "retries": 1},
            "package": {"timeout": 120, "retries": 1},
        }
        self.plugins = []

    # -------------------------------
    # Hooks & Plugins
    # -------------------------------
    def add_pre_hook(self, func):
        self.pre_hooks.append(func)

    def add_post_hook(self, func):
        self.post_hooks.append(func)

    def add_plugin(self, plugin):
        """Plugin deve ter método process(result: CommandResult)"""
        self.plugins.append(plugin)

    # -------------------------------
    # Execução síncrona
    # -------------------------------
    def run(self, command, cwd=None, env=None, timeout=None, retries=1, check=True, profile="default"):
        """Executa comando dentro do fakeroot (bloqueante)"""
        if isinstance(command, str):
            command = shlex.split(command)

        # aplicar perfil
        profile_conf = self.profiles.get(profile, {})
        timeout = timeout or profile_conf.get("timeout")
        retries = retries or profile_conf.get("retries", 1)

        for hook in self.pre_hooks:
            hook(command)

        self.log.info(f"[fakeroot] Executando: {' '.join(command)}")

        if self.dry_run:
            return CommandResult(command, 0, "[dry-run]", "", 0)

        attempt = 0
        start = time.time()
        while attempt < retries:
            attempt += 1
            try:
                proc = subprocess.Popen(
                    ["fakeroot"] + command,
                    cwd=cwd,
                    env=env or os.environ.copy(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                stdout, stderr = proc.communicate(timeout=timeout)
                duration = time.time() - start
                result = CommandResult(command, proc.returncode, stdout, stderr, duration)
                self._process_result(result, check)
                return result
            except subprocess.TimeoutExpired:
                proc.kill()
                self.log.error(f"Timeout: {' '.join(command)}")
                if attempt >= retries:
                    raise

    # -------------------------------
    # Execução assíncrona
    # -------------------------------
    async def run_async(self, command, cwd=None, env=None, profile="default"):
        """Executa comando de forma assíncrona (streaming de saída)"""
        if isinstance(command, str):
            command = shlex.split(command)

        profile_conf = self.profiles.get(profile, {})
        timeout = profile_conf.get("timeout")

        self.log.info(f"[fakeroot-async] {' '.join(command)}")

        if self.dry_run:
            return CommandResult(command, 0, "[dry-run]", "", 0)

        start = time.time()
        proc = await asyncio.create_subprocess_exec(
            "fakeroot", *command,
            cwd=cwd,
            env=env or os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        duration = time.time() - start
        result = CommandResult(command, proc.returncode, stdout.decode(), stderr.decode(), duration)
        self._process_result(result, check=True)
        return result

    # -------------------------------
    # Pipelines
    # -------------------------------
    def run_pipeline(self, commands, cwd=None, env=None):
        """
        Executa comandos encadeados tipo pipeline: [cmd1, cmd2, cmd3]
        """
        self.log.info(f"[fakeroot-pipeline] {' | '.join(' '.join(c) if isinstance(c, list) else c for c in commands)}")

        if self.dry_run:
            return CommandResult(commands, 0, "[dry-run pipeline]", "", 0)

        procs = []
        prev_stdout = None
        for cmd in commands:
            if isinstance(cmd, str):
                cmd = shlex.split(cmd)
            p = subprocess.Popen(
                ["fakeroot"] + cmd,
                cwd=cwd,
                env=env or os.environ.copy(),
                stdin=prev_stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if prev_stdout:
                prev_stdout.close()
            prev_stdout = p.stdout
            procs.append(p)

        stdout, stderr = procs[-1].communicate()
        duration = sum([0.1 for _ in procs])  # simplificação
        result = CommandResult(commands, procs[-1].returncode, stdout, stderr, duration)
        self._process_result(result, check=True)
        return result

    # -------------------------------
    # Execução paralela
    # -------------------------------
    def run_parallel(self, commands, max_workers=4):
        results = []
        q = queue.Queue()

        def worker():
            while True:
                try:
                    cmd = q.get_nowait()
                except queue.Empty:
                    break
                try:
                    result = self.run(cmd, check=False)
                    results.append(result)
                finally:
                    q.task_done()

        for cmd in commands:
            q.put(cmd)

        threads = [threading.Thread(target=worker) for _ in range(min(max_workers, len(commands)))]
        for t in threads: t.start()
        q.join()
        for t in threads: t.join()

        return results

    # -------------------------------
    # Histórico e relatórios
    # -------------------------------
    def _process_result(self, result: CommandResult, check: bool):
        self.history.append(result.to_dict())
        for hook in self.post_hooks:
            hook(result)
        for plugin in self.plugins:
            plugin.process(result)
        if result.returncode != 0 and check:
            raise FakerootError(f"Erro no comando {result.command}\n{result.stderr}")

    def save_history(self, path="fakeroot-history.json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.history, f, indent=2)
        self.log.info(f"Histórico salvo em {path}")

    def stats(self):
        """Retorna estatísticas simples dos comandos executados"""
        total = len(self.history)
        success = sum(1 for h in self.history if h["returncode"] == 0)
        fail = total - success
        avg_time = sum(h["duration"] for h in self.history) / total if total else 0
        return {"total": total, "success": success, "fail": fail, "avg_time": avg_time}
