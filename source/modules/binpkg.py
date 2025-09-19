# source/modules/binpkg.py
"""
binpkg.py - gerenciamento de pacotes binários (criar, verificar, instalar, inspecionar)

Formato de binpkg (tar.gz):
 - top-level directory: <pkgname>-<version>/
 - dentro: arquivos do pacote (bin, lib, etc.) preservando caminhos absolutos relativos ao root
 - metadata.json (na raiz do tarball) com estrutura:
   {
     "name": "<pkgname>",
     "version": "<version>",
     "arch": "<arch|any>",
     "created_at": "<ISO timestamp>",
     "files": ["usr/bin/foo", "usr/lib/libfoo.so", ...],  # caminhos relativos
     "sha256": "<sha256 of tar.gz>",
     "recipe": { ... optional recipe snapshot ... },
     "signature": { "gpg": "<armored-signature-file-name>" }  # optional
   }

Requisitos:
 - usa modules.logger.Logger, modules.sandbox.Sandbox, modules.fakeroot.Fakeroot, modules.hooks.HookManager
 - usa tarfile, hashlib, tempfile, shutil, subprocess
"""

from __future__ import annotations
import os
import io
import sys
import tarfile
import tempfile
import shutil
import hashlib
import json
import time
import urllib.request
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, List

# Modules from project (assumed present)
from modules import logger as _logger
from modules import sandbox as _sandbox
from modules import fakeroot as _fakeroot
from modules import hooks as _hooks
from modules import recipe as _recipe


class BinpkgError(Exception):
    pass


class BinPkgManager:
    def __init__(self,
                 cache_dir: str = "binpkg_cache",
                 installed_db: str = "installed_db.json",
                 dry_run: bool = False):
        self.cache_dir = os.path.abspath(cache_dir)
        os.makedirs(self.cache_dir, exist_ok=True)

        self.installed_db_path = os.path.abspath(installed_db)
        self.dry_run = dry_run

        self.log = _logger.Logger("binpkg.log")
        self.fakeroot = _fakeroot.Fakeroot(dry_run=dry_run)
        # Hook manager (for pre/post install hooks)
        try:
            self.hooks = _hooks.HookManager(dry_run=dry_run)
        except Exception:
            # fallback minimal
            self.hooks = None

        # load installed DB
        if os.path.exists(self.installed_db_path):
            try:
                with open(self.installed_db_path, "r", encoding="utf-8") as fh:
                    self.installed_db = json.load(fh)
            except Exception:
                self.installed_db = {}
        else:
            self.installed_db = {}

    # ------------------------
    # Utility helpers
    # ------------------------
    def _save_installed_db(self):
        if self.dry_run:
            self.log.info(f"[DRY-RUN] Would save installed DB to {self.installed_db_path}")
            return
        with open(self.installed_db_path, "w", encoding="utf-8") as fh:
            json.dump(self.installed_db, fh, indent=2)
        self.log.debug(f"Installed DB updated: {self.installed_db_path}")

    @staticmethod
    def _sha256_of_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _read_metadata_from_archive(archive_path: str) -> Dict[str, Any]:
        with tarfile.open(archive_path, "r:gz") as tar:
            # metadata expected at <pkgdir>/metadata.json or metadata.json at root
            meta_file = None
            for member in tar.getmembers():
                nm = os.path.basename(member.name)
                if nm == "metadata.json":
                    meta_file = member
                    break
            if not meta_file:
                raise BinpkgError("metadata.json not found inside archive")
            f = tar.extractfile(meta_file)
            if f is None:
                raise BinpkgError("Failed to read metadata.json from archive")
            return json.load(io.TextIOWrapper(f, encoding="utf-8"))

    # ------------------------
    # Create binpkg from sandbox
    # ------------------------
    def create_binpkg(self,
                      sandbox_path: str,
                      name: Optional[str] = None,
                      version: Optional[str] = None,
                      recipe_snapshot: Optional[Dict[str, Any]] = None,
                      output_dir: str = "binpkgs",
                      compress: bool = True) -> str:
        """
        Empacota o conteúdo do sandbox em um tar.gz com metadata.json.
        sandbox_path: path to prepared sandbox (directory containing bin, usr, etc)
        Returns path to created archive.
        """
        sandbox_path = os.path.abspath(sandbox_path)
        if not os.path.isdir(sandbox_path):
            raise BinpkgError(f"Sandbox not found: {sandbox_path}")

        # attempt to infer name/version if not provided (from metadata in sandbox)
        guessed_name = name
        guessed_version = version
        meta_file = os.path.join(sandbox_path, ".metadata.json")
        if os.path.exists(meta_file):
            try:
                with open(meta_file, "r", encoding="utf-8") as fh:
                    sd_meta = json.load(fh)
                guessed_name = guessed_name or sd_meta.get("package") or sd_meta.get("name")
                guessed_version = guessed_version or sd_meta.get("version")
            except Exception:
                pass

        if not guessed_name or not guessed_version:
            raise BinpkgError("Package name and version required (pass name/version or include in sandbox metadata)")

        outdir = os.path.abspath(output_dir)
        os.makedirs(outdir, exist_ok=True)

        # list files relative to sandbox root
        files_list = []
        for root, _, files in os.walk(sandbox_path):
            for fn in files:
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, sandbox_path)
                # skip internal metadata we may not want packaged (optional)
                if rel == ".metadata.json":
                    continue
                files_list.append(rel)

        # prepare metadata
        metadata = {
            "name": guessed_name,
            "version": guessed_version,
            "arch": "any",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "files": sorted(files_list),
            "recipe": recipe_snapshot or {}
        }

        pkg_dirname = f"{guessed_name}-{guessed_version}"
        archive_name = os.path.join(outdir, f"{pkg_dirname}.tar.gz")

        # create tarball with top-level dir
        if self.dry_run:
            self.log.info(f"[DRY-RUN] Would create binpkg {archive_name} from {sandbox_path} containing {len(files_list)} files")
            return archive_name

        tmpfd, tmpname = tempfile.mkstemp(suffix=".tar.gz")
        os.close(tmpfd)
        try:
            with tarfile.open(tmpname, "w:gz") as tar:
                # add files with arcname prefixed by pkg_dirname/
                for rel in files_list:
                    fullp = os.path.join(sandbox_path, rel)
                    arcname = os.path.join(pkg_dirname, rel)
                    tar.add(fullp, arcname=arcname)
                # add metadata.json
                meta_bytes = json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8")
                meta_info = tarfile.TarInfo(name=os.path.join(pkg_dirname, "metadata.json"))
                meta_info.size = len(meta_bytes)
                meta_info.mtime = int(time.time())
                tar.addfile(meta_info, io.BytesIO(meta_bytes))
            # compute sha256
            sha = self._sha256_of_file(tmpname)
            metadata["sha256"] = sha
            # we need to rewrite metadata in archive to contain sha256
            # simplest: create final archive by copying tmp and replacing metadata
            final_tmpfd, final_tmp = tempfile.mkstemp(suffix=".tar.gz")
            os.close(final_tmpfd)
            with tarfile.open(tmpname, "r:gz") as src, tarfile.open(final_tmp, "w:gz") as dst:
                # copy all members except metadata, then append updated metadata
                for member in src.getmembers():
                    if os.path.basename(member.name) == "metadata.json":
                        continue
                    f = src.extractfile(member) if member.isfile() else None
                    dst.addfile(member, f)
                # add updated metadata
                meta_bytes2 = json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8")
                meta_info2 = tarfile.TarInfo(name=os.path.join(pkg_dirname, "metadata.json"))
                meta_info2.size = len(meta_bytes2)
                meta_info2.mtime = int(time.time())
                dst.addfile(meta_info2, io.BytesIO(meta_bytes2))
            # move final_tmp to archive_name
            shutil.move(final_tmp, archive_name)
            os.remove(tmpname)
            self.log.info(f"Binpkg criado: {archive_name} (sha256: {sha})")
            return archive_name
        finally:
            if os.path.exists(tmpname):
                try: os.remove(tmpname)
                except: pass

    # ------------------------
    # Verification / signature
    # ------------------------
    def verify_binpkg(self, archive_path: str, check_signature: bool = False, gpg_keyring: Optional[str] = None) -> bool:
        """
        Verifica integridade do binpkg comparando sha256 e (opcional) verificando assinatura GPG.
        """
        archive_path = os.path.abspath(archive_path)
        if not os.path.exists(archive_path):
            raise BinpkgError("Archive not found: " + archive_path)

        meta = self._read_metadata_from_archive(archive_path)
        # check sha256
        computed = self._sha256_of_file(archive_path)
        declared = meta.get("sha256")
        if not declared:
            self.log.error("No sha256 declared in metadata")
            return False
        if computed != declared:
            self.log.error(f"SHA256 mismatch: computed={computed} declared={declared}")
            return False
        self.log.info("SHA256 OK")

        if check_signature:
            sig_info = meta.get("signature", {}) or {}
            sig_file = sig_info.get("gpg")
            if not sig_file:
                self.log.error("Signature requested but not present in metadata")
                return False
            # extract signature file to temp and call gpg --verify
            with tarfile.open(archive_path, "r:gz") as tar:
                sig_member = None
                for m in tar.getmembers():
                    if os.path.basename(m.name) == sig_file:
                        sig_member = m
                        break
                if not sig_member:
                    self.log.error("Declared signature file not found inside archive")
                    return False
                f = tar.extractfile(sig_member)
                if not f:
                    self.log.error("Failed to extract signature")
                    return False
                tmp_sig = tempfile.NamedTemporaryFile(delete=False)
                tmp_sig.write(f.read())
                tmp_sig.flush()
                tmp_sig.close()
            try:
                gpg_cmd = ["gpg", "--verify", tmp_sig.name]
                if gpg_keyring:
                    gpg_cmd = ["gpg", "--no-default-keyring", "--keyring", gpg_keyring, "--verify", tmp_sig.name]
                self.log.info(f"Running: {' '.join(gpg_cmd)}")
                res = subprocess.run(gpg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if res.returncode != 0:
                    self.log.error(f"GPG verify failed: {res.stderr.strip()}")
                    return False
                self.log.info("GPG signature OK")
            finally:
                try: os.remove(tmp_sig.name)
                except: pass

        return True

    # ------------------------
    # Install flow
    # ------------------------
    def _download_if_url(self, src: str) -> str:
        if os.path.exists(src):
            return os.path.abspath(src)
        if src.startswith("http://") or src.startswith("https://"):
            self.log.info(f"Downloading binpkg from {src} ...")
            tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
            tmpf.close()
            if self.dry_run:
                self.log.info(f"[DRY-RUN] Would download {src} -> {tmpf.name}")
                return tmpf.name
            with urllib.request.urlopen(src) as resp, open(tmpf.name, "wb") as out:
                shutil.copyfileobj(resp, out)
            self.log.info(f"Downloaded to {tmpf.name}")
            return tmpf.name
        raise BinpkgError("Source not found and not a URL: " + src)

    def install_binpkg(self,
                       src: str,
                       force: bool = False,
                       check_signature: bool = False,
                       backup: bool = True,
                       preserve_backup_dir: str = "binpkg_backups",
                       allow_downgrade: bool = False) -> Dict[str, Any]:
        """
        Instala um binpkg de arquivo local ou URL:
         - baixa se necessário
         - verifica sha256 (e opcionalmente assinatura)
         - extrai para temp dir
         - executa pre-install hooks (recipe + global)
         - faz backup dos arquivos que serão sobrescritos
         - move arquivos para FS usando fakeroot (atomic via move)
         - atualiza installed_db
        Retorna dict com details.
        """
        tmp_src = None
        try:
            archive = self._download_if_url(src)
            tmp_src = archive if os.path.exists(archive) else None

            # verify integrity
            ok = self.verify_binpkg(archive, check_signature=check_signature)
            if not ok:
                raise BinpkgError("Integrity/signature check failed")

            # read metadata
            meta = self._read_metadata_from_archive(archive)
            name = meta.get("name")
            version = meta.get("version")
            if not name or not version:
                raise BinpkgError("metadata missing name/version")

            # check installed DB for existing package
            existing = self.installed_db.get(name)
            if existing:
                installed_version = existing.get("version")
                if installed_version == version and not force:
                    self.log.info(f"Package {name} version {version} already installed (use force=True to reinstall)")
                    return {"installed": False, "reason": "already-installed", "package": name}
                # version compare logic can be expanded
                if not allow_downgrade:
                    # naive semantic compare: string compare; better to use packaging.version if available
                    if installed_version and installed_version > version and not force:
                        raise BinpkgError(f"Installed version {installed_version} is newer than {version}. Use allow_downgrade=True to override.")

            # extract to tempdir
            tmpdir = tempfile.mkdtemp(prefix="binpkg-install-")
            self.log.debug(f"Extracting package to {tmpdir}")
            with tarfile.open(archive, "r:gz") as tar:
                # extract only package directory contents into tmpdir root
                members = tar.getmembers()
                # find top-level prefix (pkgdir)
                prefix = None
                for m in members:
                    parts = m.name.split("/", 1)
                    if len(parts) > 1:
                        prefix = parts[0]
                        break
                if prefix is None:
                    raise BinpkgError("Archive structure unexpected (no top-level dir)")
                # extract members into tmpdir while stripping prefix/
                for m in members:
                    if not m.name.startswith(prefix + "/"):
                        continue
                    m.name = m.name[len(prefix)+1:]
                    if m.name == "":
                        continue
                    tar.extract(m, path=tmpdir)

            # run pre-install hooks: recipe-level hooks if recipe snapshot present in metadata
            try:
                if self.hooks:
                    # metadata may include "recipe" snapshot; pass it to hook executor
                    recipe_snapshot = meta.get("recipe") or {}
                    # some HookManager expects run_hooks(stage, recipe, sandbox_path)
                    try:
                        self.hooks.run_hooks("pre_install", recipe_snapshot, tmpdir)
                    except TypeError:
                        # fallback to previous interfaces
                        try:
                            self.hooks.run("pre-install")
                        except Exception:
                            pass
                # also run global hooks from source/hooks/global/pre-install if present
                # handled by HookManager.run_hooks above or separately omitted
            except Exception as e:
                raise BinpkgError(f"pre-install hooks failed: {e}")

            # compute list of files to install (relative)
            files_to_install = meta.get("files", [])
            # make backup list of absolute paths that would be overwritten/removed
            overwrite_targets = []
            for rel in files_to_install:
                abs_path = os.path.join("/", rel) if not rel.startswith("/") else rel
                overwrite_targets.append(abs_path)

            # perform backups if requested
            backup_path = None
            if backup and overwrite_targets:
                os.makedirs(preserve_backup_dir, exist_ok=True)
                ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                backup_name = f"{name}-{version}-{ts}.tar.gz"
                backup_path = os.path.join(preserve_backup_dir, backup_name)
                # create tar of existing files that exist
                if not self.dry_run:
                    with tarfile.open(backup_path, "w:gz") as t:
                        for p in overwrite_targets:
                            if os.path.exists(p):
                                try:
                                    t.add(p, arcname=os.path.relpath(p, "/"))
                                except Exception as e:
                                    self.log.debug(f"Skipping backup of {p}: {e}")
                    self.log.info(f"Backup of overwritten files saved to {backup_path}")
                else:
                    self.log.info(f"[DRY-RUN] Would create backup at {backup_path}")

            # move files into final locations: iterate files_to_install and move from tmpdir/<rel> to /<rel>
            installed_files_abs = []
            try:
                for rel in files_to_install:
                    src_path = os.path.join(tmpdir, rel.lstrip("/"))
                    dest_path = rel if rel.startswith("/") else os.path.join("/", rel)
                    dest_dir = os.path.dirname(dest_path)
                    # ensure dest_dir exists (use fakeroot to mkdir -p)
                    mkdir_cmd = f"mkdir -p {shutil.quote(dest_dir)}"
                    if self.dry_run:
                        self.log.info(f"[DRY-RUN] Would run: {mkdir_cmd}")
                    else:
                        self.fakeroot.run(mkdir_cmd, shell=True, check=True)
                    # copy file from tmpdir to dest using fakeroot with tar piping to preserve metadata
                    if not os.path.exists(src_path):
                        self.log.warning(f"Package missing expected file in archive: {rel} (skipping)")
                        continue
                    # perform atomic move: create temp file in dest dir then move
                    basename = os.path.basename(dest_path)
                    tmp_dest = os.path.join(dest_dir, f".{basename}.tmp")
                    # use tar to preserve modes and symlinks: tar -C <tmpdir> -cf - '<rel>' | fakeroot tar -C / -xpf - -O > tmp_dest (complex)
                    # Simpler approach: use fakeroot to copy file
                    if self.dry_run:
                        self.log.info(f"[DRY-RUN] Would copy {src_path} -> {dest_path}")
                        installed_files_abs.append(dest_path)
                    else:
                        # prefer "cp --preserve=mode,timestamps,links" if available
                        cp_cmd = f"cp -a {shutil.quote(src_path)} {shutil.quote(dest_path)}"
                        try:
                            self.fakeroot.run(cp_cmd, shell=True, check=True)
                        except Exception:
                            # fallback to tar streaming to preserve symlinks and perms
                            # tar -C tmpdir -cf - <rel> | fakeroot tar -C / -xpf -
                            rel_dir = os.path.dirname(rel.lstrip("/")) or "."
                            rel_name = os.path.basename(rel)
                            tcmd = f"tar -C {shutil.quote(tmpdir)} -cf - {shutil.quote(rel.lstrip('/'))} | fakeroot tar -C / -xpf -"
                            self.fakeroot.run(tcmd, shell=True, check=True)
                        installed_files_abs.append(dest_path)
                # installation successful, update installed_db
                self.installed_db[name] = {
                    "name": name,
                    "version": version,
                    "installed_at": datetime.utcnow().isoformat() + "Z",
                    "files": installed_files_abs,
                    "metadata": meta
                }
                self._save_installed_db()
            except Exception as e:
                self.log.error(f"Installation failed: {e}")
                # attempt rollback from backup if available
                if backup_path and os.path.exists(backup_path):
                    try:
                        self.log.info("Attempting rollback from backup...")
                        self._restore_backup_to_root(backup_path)
                        self.log.info("Rollback applied")
                    except Exception as re:
                        self.log.error(f"Rollback failed: {re}")
                raise BinpkgError(f"Installation failed: {e}")
            finally:
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass
                # remove downloaded tmp copy if we downloaded from URL
                if tmp_src and tmp_src.startswith(tempfile.gettempdir()):
                    try:
                        os.remove(tmp_src)
                    except Exception:
                        pass

            # post-install hooks
            try:
                if self.hooks:
                    recipe_snapshot = meta.get("recipe") or {}
                    try:
                        self.hooks.run_hooks("post_install", recipe_snapshot, None)
                    except TypeError:
                        try:
                            self.hooks.run("post-install")
                        except Exception:
                            pass
            except Exception as e:
                self.log.error(f"post-install hooks failed: {e}")

            return {"installed": True, "package": name, "version": version, "backup": backup_path, "files": installed_files_abs}

        finally:
            # cleanup any temporary downloaded file if not wanted
            pass

    def _restore_backup_to_root(self, backup_path: str):
        """
        Extracts a backup tar.gz created earlier to '/' using fakeroot.
        Used for rollback only.
        """
        if self.dry_run:
            self.log.info(f"[DRY-RUN] Would restore backup {backup_path} to /")
            return
        # Use fakeroot to extract
        cmd = f"tar -xzf {shutil.quote(backup_path)} -C /"
        self.fakeroot.run(cmd, shell=True, check=True)

    # ------------------------
    # Helpers: info, list, unpack
    # ------------------------
    def info_binpkg(self, archive_path: str) -> Dict[str, Any]:
        archive_path = os.path.abspath(archive_path)
        if not os.path.exists(archive_path):
            raise BinpkgError("Archive not found: " + archive_path)
        meta = self._read_metadata_from_archive(archive_path)
        # add computed sha256
        try:
            meta["_computed_sha256"] = self._sha256_of_file(archive_path)
        except Exception:
            meta["_computed_sha256"] = None
        return meta

    def list_installed(self) -> List[str]:
        return sorted(self.installed_db.keys())

    def unpack_binpkg(self, archive_path: str, dest: str):
        dest = os.path.abspath(dest)
        if self.dry_run:
            self.log.info(f"[DRY-RUN] Would unpack {archive_path} -> {dest}")
            return dest
        with tarfile.open(archive_path, "r:gz") as tar:
            # similar to install: strip top-level component
            members = tar.getmembers()
            prefix = None
            for m in members:
                parts = m.name.split("/", 1)
                if len(parts) > 1:
                    prefix = parts[0]
                    break
            if prefix is None:
                raise BinpkgError("Unexpected archive layout")
            for m in members:
                if not m.name.startswith(prefix + "/"):
                    continue
                m.name = m.name[len(prefix)+1:]
                if m.name == "":
                    continue
                tar.extract(m, path=dest)
        self.log.info(f"Unpacked {archive_path} -> {dest}")
        return dest

    # ------------------------
    # Cache remote stubs
    # ------------------------
    def push_to_cache(self, archive_path: str) -> bool:
        """
        Stub to push archive to remote cache (S3/HTTP/rsync).
        Implement per-infra.
        """
        self.log.info(f"[remote-cache] stub push {archive_path}")
        return False

    def fetch_from_cache(self, name: str, fingerprint: str) -> Optional[str]:
        """
        Stub to fetch archive from remote cache. Should return local path if found.
        """
        self.log.info(f"[remote-cache] stub fetch {name}-{fingerprint}")
        return None

    # ------------------------
    # Uninstall (integrates with remove module optionally)
    # ------------------------
    def uninstall(self, name: str, remove_files: bool = True, backup_before: bool = True):
        """
        Uninstall a package by name using installed_db. Optionally remove files from FS (via fakeroot).
        Note: more advanced dependency checks should be done by caller.
        """
        if name not in self.installed_db:
            raise BinpkgError("Package not installed: " + name)
        entry = self.installed_db[name]
        files = entry.get("files", [])

        # run pre-remove hooks if any (we don't have recipe here necessarily)
        try:
            if self.hooks:
                self.hooks.run_hooks("pre_remove", entry.get("metadata", {}).get("recipe", {}), None)
        except Exception as e:
            self.log.error(f"pre_remove hooks failed: {e}")

        # backup
        backup_path = None
        if backup_before and files:
            os.makedirs(self.cache_dir, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            backup_path = os.path.join(self.cache_dir, f"{name}-uninstall-{ts}.tar.gz")
            if not self.dry_run:
                with tarfile.open(backup_path, "w:gz") as t:
                    for p in files:
                        if os.path.exists(p):
                            try:
                                t.add(p, arcname=os.path.relpath(p, "/"))
                            except Exception:
                                self.log.debug(f"Skipping backup of {p}")
                self.log.info(f"Backup before uninstall saved to {backup_path}")
            else:
                self.log.info(f"[DRY-RUN] Would create backup {backup_path}")

        # remove files
        if remove_files:
            for p in files:
                cmd = f"rm -rf {shutil.quote(p)}"
                if self.dry_run:
                    self.log.info(f"[DRY-RUN] Would run: {cmd}")
                else:
                    self.fakeroot.run(cmd, shell=True, check=True)
                    self.log.info(f"Removed {p}")

        # update DB
        del self.installed_db[name]
        self._save_installed_db()

        # post remove hooks
        try:
            if self.hooks:
                self.hooks.run_hooks("post_remove", entry.get("metadata", {}).get("recipe", {}), None)
        except Exception as e:
            self.log.error(f"post_remove hooks failed: {e}")

        return {"uninstalled": name, "backup": backup_path}

# ------------------------
# CLI
# ------------------------
def main_cli(argv=None):
    import argparse
    argv = argv or sys.argv[1:]
    mgr = BinPkgManager()
    ap = argparse.ArgumentParser(prog="binpkg", description="Binary package manager helpers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Create binpkg from sandbox")
    p_create.add_argument("sandbox", help="Path to prepared sandbox directory")
    p_create.add_argument("--name")
    p_create.add_argument("--version")
    p_create.add_argument("--out", default="binpkgs")

    p_info = sub.add_parser("info", help="Show metadata from binpkg")
    p_info.add_argument("archive")

    p_verify = sub.add_parser("verify", help="Verify binpkg integrity/signature")
    p_verify.add_argument("archive")
    p_verify.add_argument("--gpg", help="Keyring for gpg verification", default=None)

    p_install = sub.add_parser("install", help="Install binpkg (file or URL)")
    p_install.add_argument("src")
    p_install.add_argument("--force", action="store_true")
    p_install.add_argument("--no-backup", action="store_true")
    p_install.add_argument("--check-sig", action="store_true")

    p_list = sub.add_parser("list", help="List installed binary packages")

    p_unpack = sub.add_parser("unpack", help="Unpack a binpkg to directory")
    p_unpack.add_argument("archive")
    p_unpack.add_argument("dest")

    p_push = sub.add_parser("push", help="Push binpkg to remote cache (stub)")
    p_push.add_argument("archive")

    p_fetch = sub.add_parser("fetch", help="Fetch binpkg from remote cache (stub)")
    p_fetch.add_argument("name")
    p_fetch.add_argument("fingerprint")

    p_uninstall = sub.add_parser("uninstall", help="Uninstall by package name")
    p_uninstall.add_argument("name")
    p_uninstall.add_argument("--no-backup", action="store_true")
    p_uninstall.add_argument("--remove-files", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "create":
        out = mgr.create_binpkg(args.sandbox, name=args.name, version=args.version, output_dir=args.out)
        print("Created:", out)
        return 0

    if args.cmd == "info":
        meta = mgr.info_binpkg(args.archive)
        print(json.dumps(meta, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "verify":
        ok = mgr.verify_binpkg(args.archive, check_signature=bool(args.gpg), gpg_keyring=args.gpg)
        print("OK" if ok else "FAIL")
        return 0

    if args.cmd == "install":
        res = mgr.install_binpkg(args.src, force=args.force, backup=not args.no_backup, check_signature=args.check_sig)
        print(json.dumps(res, indent=2))
        return 0

    if args.cmd == "list":
        for p in mgr.list_installed():
            print(p)
        return 0

    if args.cmd == "unpack":
        dst = mgr.unpack_binpkg(args.archive, args.dest)
        print("Unpacked to", dst)
        return 0

    if args.cmd == "push":
        mgr.push_to_cache(args.archive)
        return 0

    if args.cmd == "fetch":
        path = mgr.fetch_from_cache(args.name, args.fingerprint)
        print("Fetched:", path)
        return 0

    if args.cmd == "uninstall":
        res = mgr.uninstall(args.name, remove_files=args.remove_files, backup_before=not args.no_backup)
        print(json.dumps(res, indent=2))
        return 0

if __name__ == "__main__":
    main_cli()
