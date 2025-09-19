# source/modules/build.py
import os
from modules import logger, sandbox, fakeroot, hooks


class Builder:
    def __init__(self, package_name: str, source_dir: str,
                 build_dir: str = "build", dry_run: bool = False):
        self.package_name = package_name
        self.source_dir = os.path.abspath(source_dir)
        self.build_dir = os.path.abspath(build_dir)
        self.dry_run = dry_run

        # Logger central
        self.log = logger.Logger(f"{self.package_name}.log")

        # Instâncias de módulos auxiliares
        self.sandbox = sandbox.Sandbox(package_name, dry_run=dry_run)
        self.fakeroot = fakeroot.Fakeroot(dry_run=dry_run)
        self.hooks = hooks.HookManager(source_dir, dry_run=dry_run)

    # -------------------------------
    # Utilitários
    # -------------------------------
    def _run_hooks(self, stage: str):
        """Executa hooks de receita, locais e globais"""
        self.log.debug(f"Executando hooks para {stage}")

        # Hooks em recipe.yaml
        recipe_hooks = self.hooks.load_from_recipe(stage)
        for cmd in recipe_hooks:
            self.fakeroot.run(cmd, shell=True)

        # Hooks locais
        self.hooks.run(stage)

        # Hooks globais
        self.hooks.run_global(stage)

    # -------------------------------
    # Fluxo principal
    # -------------------------------
    def prepare(self):
        self._run_hooks("pre-prepare")
        self.sandbox.prepare()
        self._run_hooks("post-prepare")
        self.log.info(f"Sandbox preparado em {self.sandbox.path}")

    def build(self, build_system: str = "make"):
        self._run_hooks("pre-build")
        self.log.info(f"Compilando {self.package_name} com {build_system}")

        if build_system == "cmake":
            self.fakeroot.run(["cmake", self.source_dir], cwd=self.build_dir)
            self.fakeroot.run(["make", "-j"], cwd=self.build_dir)
        elif build_system == "meson":
            self.fakeroot.run(["meson", self.build_dir, self.source_dir])
            self.fakeroot.run(["ninja", "-C", self.build_dir])
        else:
            self.fakeroot.run(["./configure"], cwd=self.source_dir)
            self.fakeroot.run(["make", "-j"], cwd=self.source_dir)

        self._run_hooks("post-build")
        self.log.info("Build concluído")

    def install(self, build_system: str = "make"):
        self._run_hooks("pre-install")
        self.log.info(f"Instalando {self.package_name} no sandbox {self.sandbox.path}")

        env = os.environ.copy()
        env["DESTDIR"] = self.sandbox.path

        if build_system == "cmake":
            self.fakeroot.run(["make", "install"], cwd=self.build_dir, env=env)
        elif build_system == "meson":
            self.fakeroot.run(["ninja", "-C", self.build_dir, "install"], env=env)
        else:
            self.fakeroot.run(["make", "install"], cwd=self.source_dir, env=env)

        self._run_hooks("post-install")
        self.log.info(f"{self.package_name} instalado em {self.sandbox.path}")

    def package(self, output_dir="packages"):
        self._run_hooks("pre-package")
        os.makedirs(output_dir, exist_ok=True)

        archive_name = os.path.join(output_dir, f"{self.package_name}.tar.gz")
        self.sandbox.archive(archive_name)

        self._run_hooks("post-package")
        self.log.info(f"Pacote gerado: {archive_name}")
