# source/modules/update.py
"""
update.py - verificador de novas versões (HTML scraping + Git) integrado ao sources.

Recursos principais:
 - Lê configuração em source.conf (fallbacks se não existir).
 - Usa installed_db.json para saber pacotes instalados.
 - Para cada pacote:
     • se source é Git (git+, .git, git://) -> usa `git ls-remote` para descobrir tags/heads
     • se homepage/URL for HTTP -> faz fetch e aplica regex para extrair versões
 - Compara versões (algoritmo heurístico) e detecta novidades.
 - Gera relatório JSON e TXT em report_dir.
 - Envia notificações via notify-send quando --execute.
 - Hooks: pre_update_check, post_update_check (se HookManager disponível).
 - Opções CLI: --execute, --dry-run, --only, --exclude, --concurrency, --timeout
 - NÃO instala nada — apenas notifica e registra.
"""

from __future__ import annotations
import os
import re
import sys
import json
import time
import shutil
import subprocess
import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse

# Prefer requests if available
try:
    import requests
except Exception:
    requests = None

# Project modules (optional - module names used if present)
try:
    from modules import hooks as _hooks
except Exception:
    _hooks = None
try:
    from modules import recipe as _recipe
except Exception:
    _recipe = None
try:
    from modules import search as _search
except Exception:
    _search = None
try:
    from modules import logger as _logger
except Exception:
    _logger = None

# fallback logger
class _SimpleLogger:
    def info(self, *a, **k): print("[INFO]", *a)
    def error(self, *a, **k): print("[ERROR]", *a, file=sys.stderr)
    def debug(self, *a, **k): print("[DEBUG]", *a)

LOG = _logger.Logger("update.log") if (_logger and hasattr(_logger, "Logger")) else _SimpleLogger()

# ---------- helpers ----------

DEFAULT_CONF_PATHS = [
    "/etc/sources/source.conf",
    os.path.expanduser("~/.config/sources/source.conf"),
    os.path.join(os.getcwd(), "source.conf"),
]

def load_config(conf_path: Optional[str] = None) -> Dict[str, Any]:
    cfg = configparser.ConfigParser()
    found = None
    if conf_path:
        if os.path.exists(conf_path):
            cfg.read(conf_path); found = conf_path
    else:
        for p in DEFAULT_CONF_PATHS:
            if os.path.exists(p):
                cfg.read(p); found = p; break
    conf = {}
    conf_section = "update"
    if found and cfg.has_section(conf_section):
        conf = {k: v for k, v in cfg.items(conf_section)}
    # defaults
    defaults = {
        "installed_db": "/var/lib/sources/installed_db.json",
        "report_dir": "/var/log/sources",
        "git_prefer": "tags",   # tags | head
        "update_regex_default": r"\d+(?:\.\d+)+",
        "concurrency": "6",
        "timeout": "15"
    }
    for k, v in defaults.items():
        conf.setdefault(k, v)
    # parse some numeric
    conf["concurrency"] = int(conf.get("concurrency"))
    conf["timeout"] = int(conf.get("timeout"))
    return conf

def has_notify_send() -> bool:
    return shutil.which("notify-send") is not None

def notify(title: str, message: str, dry_run: bool):
    if dry_run:
        LOG.info(f"[DRY-RUN] notify: {title} - {message}")
        return
    if has_notify_send():
        try:
            subprocess.run(["notify-send", title, message], check=False)
        except Exception as e:
            LOG.error("notify-send failed: " + str(e))
            LOG.info(f"{title}: {message}")
    else:
        LOG.info(f"{title}: {message}")

def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

# Version comparison: heuristics that compare numeric groups; fallback to lexicographic
def version_key(v: str) -> List:
    """
    Convert version string into comparable key: list of ints/strings.
    Examples:
      "1.2.3" -> [1,2,3]
      "1.2.3-rc1" -> [1,2,3,"rc",1]
    Non-numeric tokens are kept as strings to compare lexicographically after numeric parts.
    """
    if not isinstance(v, str):
        return [v]
    # normalize common separators
    s = v.strip()
    # remove leading 'v'
    if s.startswith("v") and re.match(r"v\d", s):
        s = s[1:]
    parts = re.split(r'[.\-_\+]', s)
    key = []
    for p in parts:
        if p.isdigit():
            key.append(int(p))
        else:
            # try to split numeric prefix like rc1 -> rc,1
            m = re.match(r'([a-zA-Z]+)(\d+)$', p)
            if m:
                key.append(m.group(1))
                key.append(int(m.group(2)))
            else:
                # keep as lowercase string
                key.append(p.lower())
    return key

def compare_versions(a: str, b: str) -> int:
    """
    Compare versions a and b using version_key.
    Returns: -1 if a<b, 0 if equal, 1 if a>b
    """
    ka = version_key(a)
    kb = version_key(b)
    for x, y in zip(ka, kb):
        if type(x) == type(y):
            if x < y: return -1
            if x > y: return 1
        else:
            # int vs str: int is considered greater than str (so 1 > "rc")
            if isinstance(x, int) and isinstance(y, str):
                return 1
            if isinstance(x, str) and isinstance(y, int):
                return -1
            # fallback
            if str(x) < str(y): return -1
            if str(x) > str(y): return 1
    # equal up to min len; longer one wins if remaining not all zeros/empties
    if len(ka) < len(kb):
        rest = kb[len(ka):]
        # if rest all zeros or empty strings => equal
        for r in rest:
            if r == 0 or r == "":
                continue
            return -1
        return 0
    elif len(ka) > len(kb):
        rest = ka[len(kb):]
        for r in rest:
            if r == 0 or r == "":
                continue
            return 1
        return 0
    return 0

# ---------- fetching logic ----------

def fetch_url_text(url: str, timeout: int = 15) -> Optional[str]:
    """
    Fetch URL and return text. Uses requests if available, else urllib.
    """
    try:
        if requests:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent":"sources-update/1.0"})
            if resp.status_code == 200:
                return resp.text
            LOG.error(f"HTTP {resp.status_code} for {url}")
            return None
        else:
            # fallback to urllib
            from urllib.request import Request, urlopen
            req = Request(url, headers={"User-Agent":"sources-update/1.0"})
            with urlopen(req, timeout=timeout) as fh:
                return fh.read().decode("utf-8", errors="ignore")
    except Exception as e:
        LOG.error(f"fetch_url_text error for {url}: {e}")
        return None

def git_lsremote_tags(repo: str, timeout: int = 15) -> Tuple[List[str], List[str]]:
    """
    Run `git ls-remote --tags` and --heads and return (tags, heads) lists.
    repo may be git+https://... or https://...git
    """
    # normalize repo: strip git+ prefix
    repo_url = repo
    if repo_url.startswith("git+"):
        repo_url = repo_url[len("git+"):]
    tags = []
    heads = []
    try:
        # tags
        cmd = ["git", "ls-remote", "--tags", repo_url]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                # format: <sha>\trefs/tags/<tagname>
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("refs/tags/"):
                    tagref = parts[1][len("refs/tags/"):]
                    # ignore annotated tags that have ^{}
                    if tagref.endswith("^{}"):
                        tagref = tagref[:-3]
                    tags.append(tagref)
        # heads
        cmd2 = ["git", "ls-remote", "--heads", repo_url]
        res2 = subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if res2.returncode == 0:
            for line in res2.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                    head = parts[1][len("refs/heads/"):]
                    heads.append(head)
    except Exception as e:
        LOG.error(f"git ls-remote failed for {repo}: {e}")
    return tags, heads

def detect_latest_tag_version(tags: List[str]) -> Optional[str]:
    """
    From a list of tag names, find the best candidate version by parsing numeric sequences.
    """
    candidates = []
    ver_re = re.compile(r'\d+(?:\.\d+)+')
    for t in tags:
        m = ver_re.search(t)
        if m:
            candidates.append(m.group(0))
    if not candidates:
        return None
    # choose max by compare_versions
    best = candidates[0]
    for c in candidates[1:]:
        if compare_versions(c, best) == 1:
            best = c
    return best

def detect_latest_version_from_head(repo: str, prefer_branch: Optional[str] = None, timeout: int = 15) -> Optional[str]:
    """
    If tags not present, optionally use HEAD info (not ideal to get version).
    We can try to infer version from release branches like release-<ver> or main commit date.
    """
    tags, heads = git_lsremote_tags(repo, timeout=timeout)
    # try branch names indicating versions
    ver = detect_latest_tag_version(tags)
    if ver:
        return ver
    # search heads for 'release' or 'gcc*' patterns with numbers
    ver_re = re.compile(r'\d+(?:\.\d+)+')
    for h in heads:
        m = ver_re.search(h)
        if m:
            return m.group(0)
    # fallback: no version found
    return None

def extract_version_from_html(html: str, regex: str) -> Optional[str]:
    """
    Search html with regex and return best candidate version.
    regex should have a capturing group or match the version directly.
    """
    if not html:
        return None
    try:
        pattern = re.compile(regex)
    except re.error:
        pattern = re.compile(r'\d+(?:\.\d+)+')
    matches = pattern.findall(html)
    if not matches:
        # try fallback numeric regex
        fallback = re.findall(r'\d+(?:\.\d+)+', html)
        if not fallback:
            return None
        matches = fallback
    # pattern.findall might return tuples if groups used - normalize
    norm = []
    for m in matches:
        if isinstance(m, tuple):
            # pick first non-empty group
            v = next((x for x in m if x), None)
            if v:
                norm.append(v)
        else:
            norm.append(m)
    # pick best (max by compare)
    best = norm[0]
    for c in norm[1:]:
        if compare_versions(c, best) == 1:
            best = c
    return best

# ---------- main Updater class ----------

class UpdateChecker:
    def __init__(self, conf_path: Optional[str] = None, dry_run: bool = True):
        self.conf = load_config(conf_path)
        self.dry_run = dry_run
        self.installed_db_path = self.conf.get("installed_db")
        self.report_dir = self.conf.get("report_dir")
        self.git_prefer = self.conf.get("git_prefer", "tags")
        self.default_regex = self.conf.get("update_regex_default")
        self.concurrency = int(self.conf.get("concurrency", 6))
        self.timeout = int(self.conf.get("timeout", 15))
        self.hooks = None
        if _hooks:
            try:
                self.hooks = _hooks.HookManager(dry_run=self.dry_run)
            except Exception:
                try:
                    self.hooks = _hooks.HookManager()
                    self.hooks.dry_run = self.dry_run
                except Exception:
                    self.hooks = None
        # load installed DB
        self.installed = {}
        if os.path.exists(self.installed_db_path):
            try:
                with open(self.installed_db_path, "r", encoding="utf-8") as fh:
                    self.installed = json.load(fh)
            except Exception as e:
                LOG.error("Failed to load installed_db: " + str(e))
                self.installed = {}
        else:
            LOG.info("installed_db not found at {}, proceeding with empty list".format(self.installed_db_path))
        # recipe/search helper if available (to find homepage/source)
        self.search = _search.PackageSearch(repo_path=os.path.abspath("/usr/sources"), installed_db=self.installed_db_path) if _search else None

    def _pkg_source_info(self, pkg_name: str, entry: Dict[str, Any]) -> Dict[str, Any]:
        """
        Determine authoritative source URL and optional per-package regex:
         - prefer entry fields: 'source', 'homepage', 'url', 'update_regex'
         - fallback to recipe info via search
        Returns dict with keys: type ('git'|'http'|'unknown'), url, regex_override
        """
        url = entry.get("homepage") or entry.get("url") or entry.get("source") or entry.get("homepage_url")
        regex_override = entry.get("update_regex")
        # try recipe if missing
        if not url and self.search:
            rec = self.search.info(pkg_name)
            if rec:
                url = url or rec.get("homepage") or rec.get("source") or rec.get("url")
                regex_override = regex_override or rec.get("update_regex")
        if not url:
            return {"type": "unknown", "url": None, "regex": regex_override}
        u = url.strip()
        # detect git
        if u.startswith("git+") or u.endswith(".git") or u.startswith("git://") or u.startswith("ssh://") or u.startswith("git@"):
            return {"type": "git", "url": u, "regex": regex_override}
        # http(s)
        parsed = urlparse(u)
        if parsed.scheme in ("http", "https"):
            return {"type": "http", "url": u, "regex": regex_override}
        # maybe a plain git repo without scheme
        if "github.com" in u or "gitlab.com" in u:
            # treat as git
            return {"type": "git", "url": u, "regex": regex_override}
        # otherwise unknown
        return {"type": "unknown", "url": u, "regex": regex_override}

    def _check_one(self, pkg_name: str, entry: Dict[str, Any], git_prefer: Optional[str] = None) -> Dict[str, Any]:
        """
        Check single package. Returns dict with result fields:
        { package, installed, latest (or None), source_type, source_url, status, error? }
        """
        res = {"package": pkg_name, "installed": entry.get("version"), "latest": None,
               "source_type": None, "source_url": None, "status": "unknown", "checked_at": now_iso()}
        try:
            info = self._pkg_source_info(pkg_name, entry)
            s_type = info["type"]
            s_url = info["url"]
            res["source_type"] = s_type
            res["source_url"] = s_url
            regex = info.get("regex") or self.default_regex
            if s_type == "git" and s_url:
                # attempt tags first if preferred
                tags, heads = git_lsremote_tags(s_url, timeout=self.timeout)
                latest_ver = None
                if tags and (git_prefer or self.git_prefer) == "tags":
                    latest_ver = detect_latest_tag_version(tags)
                if not latest_ver:
                    # fallback: try to find version in heads or branch names
                    latest_ver = detect_latest_version_from_head(s_url, timeout=self.timeout)
                res["latest"] = latest_ver
                if latest_ver:
                    cmp = compare_versions(entry.get("version") or "0", latest_ver)
                    if cmp < 0:
                        res["status"] = "outdated"
                    else:
                        res["status"] = "up-to-date"
                else:
                    res["status"] = "no-version-found"
                return res
            elif s_type == "http" and s_url:
                # try homepage or specific URL
                html = fetch_url_text(s_url, timeout=self.timeout)
                if not html:
                    res["status"] = "fetch-failed"
                    return res
                latest = extract_version_from_html(html, regex)
                res["latest"] = latest
                if latest:
                    if entry.get("version") is None:
                        res["status"] = "unknown-installed-version"
                    else:
                        cmp = compare_versions(entry.get("version"), latest)
                        if cmp < 0:
                            res["status"] = "outdated"
                        else:
                            res["status"] = "up-to-date"
                else:
                    res["status"] = "no-version-found"
                return res
            else:
                res["status"] = "no-source"
                return res
        except Exception as e:
            res["error"] = str(e)
            res["status"] = "error"
            LOG.error(f"Error checking {pkg_name}: {e}")
            return res

    def check_all(self, only: Optional[str] = None, exclude: Optional[str] = None, concurrency: Optional[int] = None) -> Dict[str, Any]:
        """
        Check all installed packages for updates.
        Returns aggregated report dict.
        """
        if self.hooks:
            try:
                self.hooks.run_hooks("pre_update_check", {}, None)
            except Exception as e:
                LOG.error("pre_update_check hook error: " + str(e))
        pkgs = list(self.installed.keys())
        # apply filters
        if only:
            pkgs = [p for p in pkgs if re.search(only, p)]
        if exclude:
            pkgs = [p for p in pkgs if not re.search(exclude, p)]
        concurrency = concurrency or self.concurrency
        report = {"generated_at": now_iso(), "counts": {"checked": 0, "outdated": 0, "errors": 0}, "results": []}

        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = {}
            for p in pkgs:
                futures[ex.submit(self._check_one, p, self.installed[p])]=p
            for fut in as_completed(futures):
                p = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    LOG.error(f"Exception for {p}: {e}")
                    r = {"package": p, "status": "error", "error": str(e)}
                report["results"].append(r)
                report["counts"]["checked"] += 1
                if r.get("status") == "outdated":
                    report["counts"]["outdated"] += 1
                if r.get("status") in ("error","fetch-failed"):
                    report["counts"]["errors"] += 1

        if self.hooks:
            try:
                self.hooks.run_hooks("post_update_check", report, None)
            except Exception as e:
                LOG.error("post_update_check hook error: " + str(e))

        return report

    def _write_reports(self, report: Dict[str, Any], execute: bool = False, basename: Optional[str] = None) -> Tuple[str, str]:
        """
        Writes JSON and TXT reports. Returns (json_path, txt_path).
        If dry_run or execute=False, files are NOT written unless execute True.
        """
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        basename = basename or f"update-report-{ts}"
        json_path = os.path.join(self.report_dir, f"{basename}.json")
        txt_path = os.path.join(self.report_dir, f"{basename}.txt")
        if self.dry_run or (not execute):
            LOG.info(f"[DRY-RUN] Would write reports: {json_path}, {txt_path}")
            return json_path, txt_path
        # ensure dir
        os.makedirs(self.report_dir, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        # TXT summary
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(f"Update report generated at {report.get('generated_at')}\n")
            fh.write(f"Checked: {report['counts']['checked']}, Outdated: {report['counts']['outdated']}, Errors: {report['counts']['errors']}\n\n")
            for r in report.get("results", []):
                status = r.get("status")
                pkg = r.get("package")
                installed = r.get("installed")
                latest = r.get("latest")
                url = r.get("source_url")
                if status == "outdated":
                    fh.write(f"[OUTDATED] {pkg}: {installed} -> {latest} (source: {url})\n")
                else:
                    fh.write(f"[{status}] {pkg}: installed={installed} latest={latest} source={url}\n")
        LOG.info(f"Wrote reports to {json_path} and {txt_path}")
        return json_path, txt_path

    def notify_summary(self, report: Dict[str, Any], execute: bool = False):
        """
        Send desktop notification summarizing updates (only when execute True and not dry_run).
        """
        count = report["counts"]["outdated"]
        if count <= 0:
            notify("Update check", "Nenhuma versão nova detectada", self.dry_run or (not execute))
            return
        # build short message (limit to 6 packages shown)
        outdated = [r for r in report["results"] if r.get("status") == "outdated"]
        shown = outdated[:6]
        lines = [f"{r['package']}: {r.get('installed')} → {r.get('latest')}" for r in shown]
        if len(outdated) > len(shown):
            lines.append(f"... +{len(outdated)-len(shown)} outros")
        message = "\n".join(lines)
        title = f"{count} pacotes com novas versões"
        notify(title, message, self.dry_run or (not execute))

# ---------- CLI ----------

def main(argv: Optional[List[str]] = None):
    import argparse, json
    argv = argv or sys.argv[1:]
    ap = argparse.ArgumentParser(prog="update", description="Check for newer upstream versions (notify only)")
    ap.add_argument("--conf", help="Path to source.conf (optional)")
    ap.add_argument("--execute", action="store_true", help="Write reports and send notifications (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="Explicit dry-run (default)")
    ap.add_argument("--only", help="Regex filter to include only package names matching this")
    ap.add_argument("--exclude", help="Regex to exclude package names")
    ap.add_argument("--concurrency", type=int, help="Override concurrency")
    ap.add_argument("--timeout", type=int, help="Override per-request timeout (seconds)")
    ap.add_argument("--basename", help="Base name for report files")
    args = ap.parse_args(argv)

    # effective dry-run logic: default True unless --execute provided
    execute = bool(args.execute)
    dry_run = (not execute) or bool(args.dry_run)

    checker = UpdateChecker(conf_path=args.conf, dry_run=dry_run)
    if args.concurrency:
        checker.concurrency = args.concurrency
    if args.timeout:
        checker.timeout = args.timeout

    LOG.info("Starting update check (dry_run=%s, execute=%s)" % (dry_run, execute))
    report = checker.check_all(only=args.only, exclude=args.exclude, concurrency=checker.concurrency)

    json_path, txt_path = checker._write_reports(report, execute=execute, basename=args.basename)
    checker.notify_summary(report, execute=execute)

    # print concise JSON to stdout
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
