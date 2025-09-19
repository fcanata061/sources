# source/modules/history.py
"""
History manager para sources.

Armazena eventos em JSON-lines em um arquivo (por default /var/log/source_history.log).
Permite consultar, exportar e (best-effort) tentar rollback de ações registradas.

Formato do registro (exemplo):
{
  "id": "8a1f3c6e-....",
  "timestamp": "2025-09-19T12:34:56Z",
  "actor": "cli|auto|user:username",
  "action": "install|remove|upgrade|sync|deepclean|other",
  "package": "nome-do-pacote",
  "details": {"archive":"/tmp/foo.tar.gz", "backup":"/var/backups/..", ...},
  "result": "ok|fail",
  "note": "texto livre"
}
"""

from __future__ import annotations
import os
import sys
import json
import csv
import uuid
import shutil
import tarfile
import tempfile
import subprocess
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

# tentar importar módulos do projeto (opcionais)
try:
    from modules import logger as _logger
except Exception:
    _logger = None
try:
    from modules import remove as _remove
except Exception:
    _remove = None
try:
    from modules import binpkg as _binpkg
except Exception:
    _binpkg = None
try:
    from modules import fakeroot as _fakeroot
except Exception:
    _fakeroot = None
try:
    from modules import hooks as _hooks
except Exception:
    _hooks = None

# fallback simples de logger
class _FallbackLog:
    def info(self, *a, **k): print("[INFO]", *a)
    def error(self, *a, **k): print("[ERROR]", *a, file=sys.stderr)
    def debug(self, *a, **k): print("[DEBUG]", *a)

LOG = _logger.Logger("history.log") if (_logger and hasattr(_logger, "Logger")) else _FallbackLog()


def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


class History:
    def __init__(self,
                 history_file: str = "/var/log/source_history.log",
                 max_entries: Optional[int] = None,
                 ttl_days: Optional[int] = None,
                 dry_run: bool = False):
        """
        history_file: path to append JSON-line events
        max_entries: if set, prune to this number when calling prune()
        ttl_days: if set, prune entries older than this
        dry_run: when True, destructive ops (rollback) are simulated
        """
        self.history_file = os.path.abspath(history_file)
        self.max_entries = max_entries
        self.ttl_days = ttl_days
        self.dry_run = dry_run
        self._ensure_dir()
        # optional helpers
        try:
            self.hooks = _hooks.HookManager(dry_run=dry_run) if _hooks else None
        except Exception:
            self.hooks = None
        # helpers for rollback
        self.remover = None
        if _remove:
            # tentar instanciar remover com assinatura comum
            try:
                self.remover = _remove.Remover(installed_db="/var/lib/sources/installed_db.json", dry_run=dry_run)
            except Exception:
                try:
                    self.remover = _remove.Remover(installed_db="/var/lib/sources/installed_db.json")
                    self.remover.dry_run = dry_run
                except Exception:
                    self.remover = None
        self.binpkg_mgr = None
        if _binpkg:
            try:
                self.binpkg_mgr = _binpkg.BinPkgManager(dry_run=dry_run)
            except Exception:
                try:
                    self.binpkg_mgr = _binpkg.BinPkgManager()
                    self.binpkg_mgr.dry_run = dry_run
                except Exception:
                    self.binpkg_mgr = None

    def _ensure_dir(self):
        d = os.path.dirname(self.history_file)
        if d and not os.path.exists(d):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass

    # -------------------------
    # gravação de eventos
    # -------------------------
    def record(self,
               action: str,
               package: Optional[str] = None,
               details: Optional[Dict[str, Any]] = None,
               result: str = "ok",
               actor: Optional[str] = None,
               note: Optional[str] = None) -> Dict[str, Any]:
        """
        Registra um evento no arquivo.
        Retorna o registro gravado.
        """
        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": now_iso(),
            "actor": actor or "cli",
            "action": action,
            "package": package,
            "details": details or {},
            "result": result,
            "note": note or ""
        }
        # pre hook
        if self.hooks:
            try:
                self.hooks.run_hooks("pre_history_record", entry, None)
            except Exception as e:
                LOG.error("pre_history_record hook failed: " + str(e))
        # escrever como JSON-line append
        try:
            if self.dry_run:
                LOG.info(f"[DRY-RUN] Would append history entry: {entry}")
            else:
                with open(self.history_file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            LOG.info(f"History.record: action={action} package={package} id={entry['id']}")
        except Exception as e:
            LOG.error("Failed to write history entry: " + str(e))
            raise
        # post hook
        if self.hooks:
            try:
                self.hooks.run_hooks("post_history_record", entry, None)
            except Exception as e:
                LOG.error("post_history_record hook failed: " + str(e))
        return entry

    # -------------------------
    # leitura / listagem
    # -------------------------
    def _iter_entries(self):
        """Generator que retorna entries do arquivo, mais recente primeiro."""
        if not os.path.exists(self.history_file):
            return
            yield  # pragma: no cover
        try:
            with open(self.history_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except Exception:
                        # ignorar linhas inválidas
                        continue
        except Exception as e:
            LOG.error("Failed to read history file: " + str(e))
            return

    def list_history(self,
                     limit: int = 50,
                     action: Optional[str] = None,
                     package: Optional[str] = None,
                     since: Optional[str] = None,
                     text: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Retorna as últimas entradas que casam com filtros.
        - since: ISO timestamp string; retorna entradas >= since
        - text: pesquisa texto em package/note/details JSON dumped
        """
        out = []
        # carregar tudo em memória (arquivo normalmente não gigante)
        entries = list(self._iter_entries())
        # entries are in file order (oldest -> newest); we want newest first
        for e in reversed(entries):
            if action and e.get("action") != action:
                continue
            if package and e.get("package") != package:
                continue
            if since:
                try:
                    ts = datetime.fromisoformat(since.replace("Z", "+00:00"))
                    ent_ts = datetime.fromisoformat(e.get("timestamp").replace("Z", "+00:00"))
                    if ent_ts < ts:
                        continue
                except Exception:
                    pass
            if text:
                hay = " ".join([str(e.get("package") or ""), str(e.get("note") or ""), json.dumps(e.get("details") or {})])
                if text.lower() not in hay.lower():
                    continue
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def show(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """Retorna o registro completo por id."""
        for e in self._iter_entries():
            if e.get("id") == entry_id:
                return e
        return None

    # -------------------------
    # export
    # -------------------------
    def export(self, out_path: str, fmt: str = "json") -> str:
        """
        Exporta todo o histórico para out_path (file or dir if csv).
        fmt: 'json' or 'csv'
        Retorna caminho do arquivo gerado.
        """
        entries = list(self._iter_entries())
        entries = [e for e in entries]  # oldest->newest
        if fmt == "json":
            if os.path.isdir(out_path):
                out_file = os.path.join(out_path, f"history-export-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json")
            else:
                out_file = out_path
            if self.dry_run:
                LOG.info(f"[DRY-RUN] Would write JSON export to {out_file}")
                return out_file
            with open(out_file, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, indent=2, ensure_ascii=False)
            LOG.info(f"Wrote history JSON export to {out_file}")
            return out_file
        elif fmt == "csv":
            if os.path.isdir(out_path):
                out_file = os.path.join(out_path, f"history-export-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.csv")
            else:
                out_file = out_path
            if self.dry_run:
                LOG.info(f"[DRY-RUN] Would write CSV export to {out_file}")
                return out_file
            with open(out_file, "w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["id", "timestamp", "actor", "action", "package", "result", "note", "details_json"])
                for e in entries:
                    writer.writerow([e.get("id"), e.get("timestamp"), e.get("actor"), e.get("action"),
                                     e.get("package"), e.get("result"), e.get("note"),
                                     json.dumps(e.get("details") or {}, ensure_ascii=False)])
            LOG.info(f"Wrote history CSV export to {out_file}")
            return out_file
        else:
            raise ValueError("fmt must be 'json' or 'csv'")

    # -------------------------
    # prune
    # -------------------------
    def prune(self):
        """
        Aplica políticas simples: ttl_days e max_entries.
        Reescreve arquivo filtrando entradas antigas/excedentes.
        """
        entries = list(self._iter_entries())
        if not entries:
            return {"pruned": 0}
        # oldest->newest in entries
        now = datetime.utcnow()
        keep = []
        for e in entries:
            keep_flag = True
            if self.ttl_days:
                try:
                    ent_ts = datetime.fromisoformat(e.get("timestamp").replace("Z", "+00:00"))
                    if now - ent_ts > timedelta(days=self.ttl_days):
                        keep_flag = False
                except Exception:
                    pass
            if keep_flag:
                keep.append(e)
        # apply max_entries
        if self.max_entries and len(keep) > self.max_entries:
            # keep last max_entries
            keep = keep[-self.max_entries:]
        # write back
        if self.dry_run:
            LOG.info(f"[DRY-RUN] Would prune history to {len(keep)} entries (from {len(entries)})")
            return {"pruned": len(entries) - len(keep)}
        try:
            with open(self.history_file + ".tmp", "w", encoding="utf-8") as fh:
                for e in keep:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            shutil.move(self.history_file + ".tmp", self.history_file)
            LOG.info(f"Pruned history: kept {len(keep)} entries")
            return {"pruned": len(entries) - len(keep)}
        except Exception as e:
            LOG.error("Prune failed: " + str(e))
            return {"error": str(e)}

    # -------------------------
    # rollback (best-effort)
    # -------------------------
    def rollback(self, entry_id: str, assume_yes: bool = False) -> Dict[str, Any]:
        """
        Tentativa de rollback 'best-effort' para um registro específico.
        Estratégia:
         - busca entry pelo id
         - se details contém 'snapshot' ou 'backup' (tar.gz) -> extrai para '/' usando fakeroot/binpkg manager
         - se details contém 'archive' (binpkg) -> instala via BinPkgManager.install_binpkg
         - se action == 'install' -> tenta remover via Remover
         - se action == 'remove' -> tenta reinstalar via archive/snapshot (se presente)
         - sempre opera em dry_run por padrão; respeita self.dry_run
        Retorna dict com resultado.
        """
        e = self.show(entry_id)
        if not e:
            return {"status": "not-found", "id": entry_id}
        res = {"id": entry_id, "action": e.get("action"), "package": e.get("package"), "attempted": [], "status": "pending"}
        # confirmation (unless assume_yes)
        if not assume_yes and not self.dry_run:
            try:
                yn = input(f"About to attempt rollback of {e.get('action')} {e.get('package')} (id={entry_id}). Continue? [y/N] ").strip().lower()
                if yn not in ("y", "yes"):
                    return {"status": "aborted"}
            except Exception:
                pass
        details = e.get("details") or {}
        # helper to extract tar.gz to / using fakeroot if available
        def _extract_to_root(tar_path: str):
            if self.dry_run:
                LOG.info(f"[DRY-RUN] Would extract {tar_path} -> /")
                res["attempted"].append(f"extract-dryrun:{tar_path}")
                return {"ok": True}
            # prefer binpkg_mgr.fakeroot if available
            try:
                if self.binpkg_mgr and hasattr(self.binpkg_mgr, "fakeroot") and self.binpkg_mgr.fakeroot:
                    cmd = f"tar -xzf {shutil.quote(tar_path)} -C /"
                    self.binpkg_mgr.fakeroot.run(cmd, shell=True, check=True)
                else:
                    # fallback to system tar (may require root)
                    subprocess.run(["tar", "-xzf", tar_path, "-C", "/"], check=True)
                res["attempted"].append(f"extract:{tar_path}")
                return {"ok": True}
            except Exception as ex:
                LOG.error("Extract failed: " + str(ex))
                return {"ok": False, "error": str(ex)}
        # 1) if archive/binpkg exists in details -> try install
        archive = details.get("archive") or details.get("binpkg") or details.get("package_archive")
        if archive:
            if self.binpkg_mgr:
                if self.dry_run:
                    LOG.info(f"[DRY-RUN] Would install binpkg {archive}")
                    res["attempted"].append(f"install-dryrun:{archive}")
                    res["status"] = "dry-run"
                    return res
                try:
                    inst = self.binpkg_mgr.install_binpkg(archive, force=True, backup=True)
                    res["attempted"].append({"install_binpkg": inst})
                    res["status"] = "ok" if inst.get("installed", False) else "failed"
                    return res
                except Exception as ex:
                    res["attempted"].append({"install_error": str(ex)})
                    # continue to other strategies
            else:
                LOG.info("Binpkg manager not available for rollback install archive.")
        # 2) if snapshot/backup tar available -> extract to root
        snapshot = details.get("snapshot") or details.get("backup")
        if snapshot:
            out = _extract_to_root(snapshot)
            if out.get("ok"):
                res["status"] = "ok"
            else:
                res["status"] = "failed"
                res["error"] = out.get("error")
            return res
        # 3) if action==install -> remove package using remover (undo install)
        if e.get("action") == "install":
            pkg = e.get("package")
            if self.remover:
                if self.dry_run:
                    LOG.info(f"[DRY-RUN] Would call remover.remove_package({pkg})")
                    res["attempted"].append(f"remove-dryrun:{pkg}")
                    res["status"] = "dry-run"
                    return res
                try:
                    rr = self.remover.remove_package(pkg, force=True, backup=True)
                    res["attempted"].append({"remove": rr})
                    res["status"] = "ok"
                    return res
                except Exception as ex:
                    res["attempted"].append({"remove_error": str(ex)})
                    res["status"] = "failed"
                    res["error"] = str(ex)
                    return res
            else:
                res["status"] = "no-remover"
                return res
        # 4) if action==remove -> try to restore using details.files (list)
        if e.get("action") == "remove":
            files = details.get("files", []) or []
            if not files:
                res["status"] = "no-files-to-restore"
                return res
            # if snapshot not present, cannot restore reliably; but we can warn
            if self.dry_run:
                LOG.info(f"[DRY-RUN] Would attempt to restore {len(files)} files (but no snapshot) - manual action required")
                res["attempted"].append(f"restore-files-dryrun:{len(files)}")
                res["status"] = "dry-run"
                return res
            # without a snapshot we can't reconstruct file contents; fail-safe:
            res["status"] = "cannot-restore-without-snapshot"
            return res
        # default: nothing to do
        res["status"] = "no-rollback-strategy"
        return res

# -------------------------
# CLI
# -------------------------
def main(argv=None):
    import argparse
    argv = argv or sys.argv[1:]
    ap = argparse.ArgumentParser(prog="history", description="History manager for sources")
    ap.add_argument("--file", help="History file path", default="/var/log/source_history.log")
    ap.add_argument("--dry-run", action="store_true", help="Simulate destructive actions")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_record = sub.add_parser("record", help="Append an event to history")
    p_record.add_argument("action")
    p_record.add_argument("--package")
    p_record.add_argument("--result", default="ok")
    p_record.add_argument("--note")
    p_record.add_argument("--details", help="JSON string for details")

    p_list = sub.add_parser("list", help="List recent history")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--action")
    p_list.add_argument("--package")
    p_list.add_argument("--since", help="ISO timestamp filter")
    p_list.add_argument("--text", help="Text search in package/note/details")

    p_show = sub.add_parser("show", help="Show entry by id")
    p_show.add_argument("id")

    p_export = sub.add_parser("export", help="Export history to JSON or CSV")
    p_export.add_argument("out")
    p_export.add_argument("--fmt", choices=("json", "csv"), default="json")

    p_prune = sub.add_parser("prune", help="Apply prune policy (ttl/max_entries)")

    p_rollback = sub.add_parser("rollback", help="Attempt rollback of entry")
    p_rollback.add_argument("id")
    p_rollback.add_argument("--yes", action="store_true", help="Assume yes")
    args = ap.parse_args(argv)

    h = History(history_file=args.file, dry_run=args.dry_run)
    if args.cmd == "record":
        details = {}
        if args.details:
            try:
                details = json.loads(args.details)
            except Exception as e:
                print("Invalid JSON for --details:", e, file=sys.stderr)
                return 2
        ent = h.record(action=args.action, package=args.package, details=details, result=args.result, note=args.note)
        print(json.dumps(ent, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "list":
        items = h.list_history(limit=args.limit, action=args.action, package=args.package, since=args.since, text=args.text)
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "show":
        e = h.show(args.id)
        if not e:
            print("Not found", file=sys.stderr)
            return 2
        print(json.dumps(e, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "export":
        path = h.export(args.out, fmt=args.fmt)
        print("Exported to", path)
        return 0

    if args.cmd == "prune":
        r = h.prune()
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "rollback":
        r = h.rollback(args.id, assume_yes=args.yes)
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0

if __name__ == "__main__":
    sys.exit(main())
