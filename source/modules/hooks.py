# source/modules/hooks.py
import os
import subprocess
from typing import List, Dict, Callable, Optional
from modules import logger


class HookManager:
    """
    Gerencia hooks de build.
    - Hooks podem ser:
        • Globais (configuração geral)
        • Por receita (definidos dentro da receita)
    - Hooks suportam:
        • Funções Python
        • Scripts shell (executados no sandbox ou sistema)
        • Comandos inline
    """

    def __init__(self, dry_run: bool = False):
        self.global_hooks: Dict[str, List[Callable]] = {}
        self.log = logger.Logger("hooks.log")
        self.dry_run = dry_run

    # ---------------------------------------------------
    # Registro de hooks
    # ---------------------------------------------------
    def register_global(self, stage: str, func: Callable):
        """Registra um hook global (executado em todos os pacotes)"""
        self.global_hooks.setdefault(stage, []).append(func)
        self.log.debug(f"Hook global registrado para stage={stage}: {func}")

    def load_recipe_hooks(self, recipe: Dict) -> Dict[str, List]:
        """Carrega hooks definidos dentro de uma receita"""
        return recipe.get("hooks", {})

    # ---------------------------------------------------
    # Execução
    # ---------------------------------------------------
    def run_hooks(self, stage: str, recipe: Dict, sandbox_path: Optional[str] = None):
        """
        Executa hooks globais e locais de uma etapa.
        Hooks podem ser funções Python, comandos shell ou scripts.
        """
        self.log.info(f"Executando hooks para stage={stage} ({recipe.get('name')})")

        # Hooks globais
        for func in self.global_hooks.get(stage, []):
            self._execute_func(func, recipe, sandbox_path)

        # Hooks da receita
        recipe_hooks = self.load_recipe_hooks(recipe).get(stage, [])
        for hook in recipe_hooks:
            self._execute_hook(hook, recipe, sandbox_path)

    # ---------------------------------------------------
    # Execução de tipos de hook
    # ---------------------------------------------------
    def _execute_func(self, func: Callable, recipe: Dict, sandbox_path: Optional[str]):
        """Executa uma função Python registrada como hook"""
        try:
            if self.dry_run:
                self.log.info(f"[DRY-RUN] Executaria hook Python: {func.__name__}")
                return
            func(recipe, sandbox_path)
        except Exception as e:
            self.log.error(f"Erro no hook Python {func}: {e}")
            raise

    def _execute_hook(self, hook, recipe: Dict, sandbox_path: Optional[str]):
        """Executa hook que pode ser string (comando/script) ou função"""
        if callable(hook):
            return self._execute_func(hook, recipe, sandbox_path)

        if isinstance(hook, str):
            return self._execute_command(hook, sandbox_path)

        self.log.warning(f"Hook inválido ignorado: {hook}")

    def _execute_command(self, command: str, sandbox_path: Optional[str]):
        """Executa um comando shell ou script"""
        self.log.info(f"Executando comando hook: {command}")

        if self.dry_run:
            self.log.info(f"[DRY-RUN] Não executado: {command}")
            return

        env = os.environ.copy()
        if sandbox_path:
            env["SANDBOX_PATH"] = sandbox_path

        try:
            subprocess.run(command, shell=True, check=True, env=env)
        except subprocess.CalledProcessError as e:
            self.log.error(f"Erro ao executar hook '{command}': {e}")
            raise

    # ---------------------------------------------------
    # Helpers
    # ---------------------------------------------------
    def list_hooks(self) -> Dict[str, List[str]]:
        """Lista hooks globais registrados"""
        return {stage: [f.__name__ for f in funcs] for stage, funcs in self.global_hooks.items()}
