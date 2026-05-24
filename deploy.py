#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ftp-deploy-shared — single-command FTP-only deploy for PHP/MySQL sites
on stubborn shared hosting (pure-ftpd, 西部数码-style, Cloudflare-fronted).

Handles cases popular FTP-deploy tools refuse to handle:
  - root-owned existing files (DELETE-then-STOR)
  - root-owned existing directories (rename-aside + recreate)
  - server-side file permissions (SITE CHMOD 644/755 after each STOR/MKD)
  - SQL migration via PHP helper (no SSH needed)
  - Cloudflare WAF-safe (base64 payload over FTP + GET-trigger, never POSTs body)
  - browser-fingerprint User-Agent (avoids Cloudflare 1010 bot block)

For each `python deploy.py <version>` run:
  1. Unzip web bundle locally to a temp dir
  2. FTP upload tree to WEB_ROOT (with all the fixups above)
  3. Optional: upload a versioned binary asset to a downloads folder
  4. Delete obsolete paths (EXTRA_DELETE)
  5. Run SQL migration via one-shot PHP helper (prepared statements + base64)

Stdlib-only. Python 3.8+. See README.md and .env.example for full docs.
"""

import argparse
import base64
import ftplib
import io
import json
import os
import re
import secrets
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config from .env
# ---------------------------------------------------------------------------

def load_env(env_path: Path) -> dict:
    cfg = {}
    if not env_path.is_file():
        sys.exit(f"[!] Config not found: {env_path}\n    Copy .env.example to .env and fill it in.")
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        cfg[key.strip()] = val
    return cfg


def require(cfg: dict, key: str) -> str:
    val = cfg.get(key, "")
    if not val:
        sys.exit(f"[!] Missing required .env value: {key}")
    return val


# ---------------------------------------------------------------------------
# FTP helpers — with all the stubborn-shared-hosting workarounds
# ---------------------------------------------------------------------------

def ftp_connect(cfg: dict) -> ftplib.FTP:
    host = require(cfg, "FTP_HOST")
    user = require(cfg, "FTP_USER")
    password = require(cfg, "FTP_PASS")
    port = int(cfg.get("FTP_PORT", "21"))
    use_tls = cfg.get("FTP_TLS", "0") in ("1", "true", "True", "yes")

    ftp = ftplib.FTP_TLS() if use_tls else ftplib.FTP()
    ftp.connect(host, port, timeout=30)
    ftp.login(user, password)
    if use_tls:
        ftp.prot_p()
    ftp.set_pasv(True)  # passive mode is required on most shared hosts
    print(f"[ok] FTP connected: {user}@{host}:{port} (passive, tls={use_tls})")
    return ftp


def ftp_ensure_dir(ftp: ftplib.FTP, remote_dir: str, dir_chmod: str = "") -> None:
    """Recursively create remote dir. Optionally SITE CHMOD each freshly-created segment."""
    parts = [p for p in remote_dir.strip("/").split("/") if p]
    ftp.cwd("/")
    for part in parts:
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            ftp.mkd(part)
            if dir_chmod:
                try:
                    ftp.voidcmd(f"SITE CHMOD {dir_chmod} {part}")
                except ftplib.error_perm:
                    pass
            ftp.cwd(part)
    ftp.cwd("/")


def _rename_aside(ftp: ftplib.FTP, remote_dir: str, dir_chmod: str = "") -> None:
    """Move a root-owned dir aside and recreate it under our FTP user.

    Works because `rename` only needs write perm on the PARENT directory
    (which we own), even when the dir itself is owned by root. The fresh
    mkd'd dir is then ours, so subsequent STOR succeeds.

    Stale _oldver_* dirs should be blocked from web access via .htaccess
    (RewriteRule ^_oldver_ - [F,L]) since their old root-owned content
    can't be FTP-deleted by us.
    """
    parent = "/".join(remote_dir.rstrip("/").split("/")[:-1]) or "/"
    name = remote_dir.rstrip("/").split("/")[-1]
    aside = f"_oldver_{name}_{int(time.time())}"
    ftp.cwd(parent)
    ftp.rename(name, aside)
    ftp.mkd(name)
    if dir_chmod:
        try: ftp.voidcmd(f"SITE CHMOD {dir_chmod} {name}")
        except ftplib.error_perm: pass
    print(f"      (root-owned {remote_dir} -> {parent.rstrip('/')}/{aside}, mkd fresh {name})")


# Blocksizes used by _stor_with_size_verify, in order tried per retry.
# Empirically on pure-ftpd behind Cloudflare on shared hosting:
#   8192 / 4096 / 2048 → fail on ~30% of files >5 KB (silent truncation)
#   1024 → succeeds almost always, slightly slower
#    512 → essentially bulletproof, twice as slow as 1024
# We start fast and degrade to bulletproof so 99% of uploads are fast
# but the rare stubborn file still eventually lands intact.
_STOR_BLOCKSIZES = [8192, 4096, 2048, 1024, 1024, 512, 512, 512]


def _stor_with_size_verify(ftp: ftplib.FTP, filename: str, data: bytes,
                            expected: int) -> bool:
    """STOR `data` under `filename` (already cwd'd into parent dir).
    Verifies remote SIZE matches `expected`. Retries on mismatch.

    Some shared-hosting FTP servers (pure-ftpd behind Cloudflare / OVH /
    西部数码 / DreamHost shared) silently truncate streaming uploads —
    `STOR` returns 226 OK, control connection is happy, but the destination
    file is short. The truncation point is roughly aligned with the TCP /
    proxy buffer size, so we cycle through *decreasing* blocksizes on
    retry — large/fast first for healthy hosts, small/reliable last for
    the stubborn ones. Each STOR is re-sent from a fresh BytesIO buffer.
    We then call `SIZE` to verify against the expected length.

    Returns True once `ftp.size(filename) == expected`. False after
    all blocksize tiers exhausted.
    """
    for attempt, blocksize in enumerate(_STOR_BLOCKSIZES, start=1):
        try: ftp.delete(filename)
        except ftplib.error_perm: pass
        # Brief pause between retries — pure-ftpd sometimes needs time after
        # a partial transfer before accepting a fresh STOR cleanly.
        if attempt > 1:
            time.sleep(0.8 + 0.3 * attempt)
        try:
            ftp.voidcmd("TYPE I")
            ftp.storbinary(f"STOR {filename}", io.BytesIO(data), blocksize=blocksize)
        except (ftplib.error_temp, OSError):
            # Connection hiccup — let next iteration try again with smaller blocks
            continue
        try:
            actual = ftp.size(filename)
        except ftplib.error_perm:
            actual = -1
        if actual == expected:
            return True
    return False


def ftp_upload_file(ftp: ftplib.FTP, local_path: Path, remote_path: str,
                    file_chmod: str = "644", dir_chmod: str = "755") -> None:
    """Upload one file. Auto-handles:
       - root-owned existing FILE: DELETE-then-STOR
       - root-owned existing PARENT DIR: rename-aside + recreate
       - silent FTP truncation: SIZE-verify after STOR + retry from BytesIO
       - applies SITE CHMOD after STOR if file_chmod is set
    """
    remote_dir = "/".join(remote_path.split("/")[:-1]) or "/"
    filename = remote_path.rsplit("/", 1)[-1]
    ftp_ensure_dir(ftp, remote_dir, dir_chmod=dir_chmod)
    ftp.cwd(remote_dir)
    # Read once into memory so retries don't re-read the disk file.
    data = local_path.read_bytes()
    expected = len(data)
    # Pre-delete: works because we own the parent dir, even if the file itself is root-owned.
    try: ftp.delete(filename)
    except ftplib.error_perm: pass
    ok = False
    try:
        ok = _stor_with_size_verify(ftp, filename, data, expected)
    except ftplib.error_perm as e:
        if "553" in str(e):
            # Parent dir itself is root-owned; we can't write inside.
            # Rename aside + recreate under our user, retry.
            _rename_aside(ftp, remote_dir, dir_chmod=dir_chmod)
            ftp.cwd(remote_dir)
            ok = _stor_with_size_verify(ftp, filename, data, expected)
        else:
            raise
    if not ok:
        raise RuntimeError(
            f"upload size mismatch (silent FTP truncation): {remote_path} "
            f"(expected {expected} bytes, server kept returning a shorter file "
            f"after {len(_STOR_BLOCKSIZES)} retries with decreasing blocksizes — "
            f"check host's FTP buffer / proxy settings)"
        )
    if file_chmod:
        try: ftp.voidcmd(f"SITE CHMOD {file_chmod} {filename}")
        except ftplib.error_perm: pass
    print(f"      ↑ {remote_path}")


def ftp_upload_bytes(ftp: ftplib.FTP, data: bytes, remote_path: str,
                     file_chmod: str = "644", dir_chmod: str = "755") -> None:
    """Same as ftp_upload_file but from an in-memory bytes payload."""
    remote_dir = "/".join(remote_path.split("/")[:-1]) or "/"
    filename = remote_path.rsplit("/", 1)[-1]
    ftp_ensure_dir(ftp, remote_dir, dir_chmod=dir_chmod)
    ftp.cwd(remote_dir)
    expected = len(data)
    try: ftp.delete(filename)
    except ftplib.error_perm: pass
    ok = False
    try:
        ok = _stor_with_size_verify(ftp, filename, data, expected)
    except ftplib.error_perm as e:
        if "553" in str(e):
            _rename_aside(ftp, remote_dir, dir_chmod=dir_chmod)
            ftp.cwd(remote_dir)
            ok = _stor_with_size_verify(ftp, filename, data, expected)
        else:
            raise
    if not ok:
        raise RuntimeError(
            f"upload size mismatch (silent FTP truncation): {remote_path} "
            f"(expected {expected} bytes after retries)"
        )
    if file_chmod:
        try: ftp.voidcmd(f"SITE CHMOD {file_chmod} {filename}")
        except ftplib.error_perm: pass
    print(f"      ↑ {remote_path}")


def ftp_delete_quiet(ftp: ftplib.FTP, remote_path: str) -> None:
    try:
        remote_dir = "/".join(remote_path.split("/")[:-1]) or "/"
        filename = remote_path.rsplit("/", 1)[-1]
        ftp.cwd(remote_dir)
        ftp.delete(filename)
        print(f"      ✕ deleted {remote_path}")
    except ftplib.error_perm as exc:
        print(f"      (could not delete {remote_path}: {exc})")


def upload_tree(ftp, local_root: Path, remote_root: str, file_chmod: str, dir_chmod: str, dry: bool) -> int:
    count = 0
    for path in sorted(local_root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(local_root).as_posix()
        remote_path = f"{remote_root.rstrip('/')}/{rel}"
        if dry:
            print(f"      [dry] {remote_path}")
        else:
            ftp_upload_file(ftp, path, remote_path, file_chmod=file_chmod, dir_chmod=dir_chmod)
        count += 1
    return count


# ---------------------------------------------------------------------------
# SQL migration: parse locally, send as base64 JSON, trigger via GET
# ---------------------------------------------------------------------------

def sql_parse_row(sql: str, p: int):
    """Parse one (v1, v2, ...) VALUES tuple of string literals. Returns (values, end_pos)."""
    assert sql[p] == "(", f"expected '(' at {p}"
    p += 1
    vals = []
    while True:
        while p < len(sql) and sql[p] in " \t\n":
            p += 1
        if sql[p] == ")":
            return vals, p + 1
        assert sql[p] == "'", f"expected ' at {p}, got {sql[p]!r}"
        p += 1
        s = ""
        while p < len(sql):
            if sql[p] == "'":
                if p + 1 < len(sql) and sql[p + 1] == "'":  # doubled = literal apostrophe
                    s += "'"; p += 2
                else:
                    p += 1; break
            else:
                s += sql[p]; p += 1
        vals.append(s)
        while p < len(sql) and sql[p] in " \t\n":
            p += 1
        if sql[p] == ",":
            p += 1
        elif sql[p] == ")":
            return vals, p + 1


def sql_extract_insert(sql: str, table: str):
    """Find `INSERT INTO <table> (col1, col2, ...) VALUES (...), (...);`.
       Returns (columns, rows) — columns is a list of names, rows is list of lists.
    """
    pat = re.compile(
        rf"INSERT INTO\s+{re.escape(table)}\s*\(([^)]+)\)\s*VALUES\s*",
        re.IGNORECASE,
    )
    m = pat.search(sql)
    if not m:
        return [], []
    cols = [c.strip().strip("`") for c in m.group(1).split(",")]
    p = m.end()
    rows = []
    while p < len(sql):
        while p < len(sql) and sql[p] in " \t\n,;":
            p += 1
        if p >= len(sql) or sql[p] != "(":
            break
        vals, p = sql_parse_row(sql, p)
        rows.append(vals)
    return cols, rows


def sql_parse_file(sql_path: Path) -> dict:
    """Split a migration SQL into:
       - 'schema': DDL text (everything before the first DELETE/INSERT for a table)
       - 'tables': list of {name, columns, rows} for each INSERT INTO found

    The structure is intentionally simple — extend if you need more complex parsing.
    """
    sql = sql_path.read_text(encoding="utf-8")
    m = re.search(r"\b(DELETE FROM|INSERT INTO)\b", sql, re.IGNORECASE)
    schema = (sql[: m.start()] if m else sql).rstrip()
    tables = []
    seen = set()
    for ins in re.finditer(r"INSERT INTO\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.IGNORECASE):
        name = ins.group(1)
        if name in seen:
            continue
        seen.add(name)
        cols, rows = sql_extract_insert(sql, name)
        if rows:
            tables.append({"name": name, "columns": cols, "rows": rows})
    return {"schema": schema, "tables": tables}


# import.php template: token-protected, reads base64 JSON, uses PDO prepared statements
def render_import_php(template_path: Path, token: str, db_host: str, db_name: str,
                      db_user: str, db_pass: str) -> bytes:
    t = template_path.read_text(encoding="utf-8")
    t = t.replace("__TOKEN__", token)
    t = t.replace("__DB_HOST__", db_host)
    t = t.replace("__DB_NAME__", db_name)
    t = t.replace("__DB_USER__", db_user)
    t = t.replace("__DB_PASS__", db_pass)
    return t.encode("utf-8")


def run_sql_migration(ftp: ftplib.FTP, cfg: dict, sql_path: Path, web_root: str,
                      deploy_dir: str, file_chmod: str, dir_chmod: str) -> bool:
    """Upload db-data.b64 + import.php via FTP, trigger via HTTPS GET, return True on success."""
    print(f"[..] Parsing {sql_path.name} locally for prepared-statement upload...")
    data = sql_parse_file(sql_path)
    print(f"      schema: {len(data['schema'])} chars; "
          f"tables with seed rows: {[t['name'] for t in data['tables']]}")

    token = secrets.token_urlsafe(24)
    tpl_path = Path(__file__).resolve().parent / "import_template.php"
    if not tpl_path.is_file():
        sys.exit(f"[!] import_template.php not found next to deploy.py: {tpl_path}")
    php = render_import_php(
        tpl_path, token,
        cfg.get("DB_HOST", "localhost"),
        require(cfg, "DB_NAME"),
        require(cfg, "DB_USER"),
        require(cfg, "DB_PASS"),
    )

    # JSON -> base64 (innocent chars only — bypasses WAF content scanning at FTP layer too,
    # in case some hosts inspect uploaded file content).
    js_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
    b64 = base64.b64encode(js_bytes)

    remote_deploy = f"{web_root.rstrip('/')}/{deploy_dir.strip('/')}"
    print(f"[..] Uploading data + helper to {remote_deploy}/ ...")
    ftp_upload_bytes(ftp, b64, f"{remote_deploy}/db-data.b64", file_chmod=file_chmod, dir_chmod=dir_chmod)
    ftp_upload_bytes(ftp, php, f"{remote_deploy}/import.php", file_chmod=file_chmod, dir_chmod=dir_chmod)

    site_url = require(cfg, "SITE_URL")
    url = f"{site_url.rstrip('/')}/{deploy_dir.strip('/')}/import.php?token={token}"
    print(f"[..] Triggering {url}")

    ua = cfg.get("HTTP_USER_AGENT", "").strip() or \
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

    ctx = ssl.create_default_context()
    if cfg.get("INSECURE_TLS", "0") in ("1", "true", "yes"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url)
    req.add_header("User-Agent", ua)
    req.add_header("Accept", "application/json")
    basic = cfg.get("SITE_BASIC_AUTH", "").strip()
    if basic:
        req.add_header("Authorization", "Basic " + base64.b64encode(basic.encode()).decode())

    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print(f"[!] HTTP {e.code}: {e.read().decode('utf-8','replace')[:500]}")
        return False
    except Exception as e:
        print(f"[!] {type(e).__name__}: {e}")
        return False

    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        print(f"[!] non-JSON response: {body[:500]}")
        return False

    if result.get("ok"):
        print(f"[ok] SQL migration: {result}")
        return True
    else:
        print(f"[!] SQL migration failed: {json.dumps(result, ensure_ascii=False, indent=2)}")
        return False


# ---------------------------------------------------------------------------
# Optional binary asset (e.g. game map, installer, build artifact)
# ---------------------------------------------------------------------------

def find_asset(cfg: dict, version: str, explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        return Path(explicit)
    asset_dir = cfg.get("ASSET_DIR", "").strip()
    if not asset_dir:
        return None
    pattern = cfg.get("ASSET_PATTERN", "*{version}*").replace("{version}", version)
    d = Path(asset_dir)
    if not d.is_dir():
        sys.exit(f"[!] ASSET_DIR does not exist: {d}")
    matches = list(d.glob(pattern))
    if not matches:
        sys.exit(f"[!] No asset found in {d} matching {pattern}")
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Single-command FTP deploy for stubborn shared hosting.")
    ap.add_argument("version", help="Release version (used to find the binary asset, e.g. '1.2.3')")
    ap.add_argument("--zip", default=None, help="Path to web bundle .zip (default: ZIP_PATH from .env)")
    ap.add_argument("--sql", default=None, help="Path to migration .sql (default: SQL_PATH from .env)")
    ap.add_argument("--asset", default=None, help="Path to binary asset (default: auto-find via ASSET_DIR/ASSET_PATTERN)")
    ap.add_argument("--dry-run", action="store_true", help="Print plan, ship nothing")
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    cfg = load_env(script_dir / ".env")

    web_root = cfg.get("WEB_ROOT", "/wwwroot")
    deploy_dir = cfg.get("DEPLOY_DIR", "_deploy")
    file_chmod = cfg.get("FILE_CHMOD", "644").strip()
    dir_chmod = cfg.get("DIR_CHMOD", "755").strip()
    asset_remote_dir = cfg.get("ASSET_REMOTE_DIR", "").strip()
    extra_delete = [p.strip() for p in cfg.get("EXTRA_DELETE", "").split(",") if p.strip()]

    zip_path = Path(args.zip) if args.zip else Path(cfg.get("ZIP_PATH", "./web-update.zip"))
    sql_path_str = args.sql if args.sql else cfg.get("SQL_PATH", "").strip()
    sql_path = Path(sql_path_str) if sql_path_str else None
    asset_path = find_asset(cfg, args.version, args.asset) if (asset_remote_dir or args.asset) else None

    if not zip_path.is_file():
        sys.exit(f"[!] Web zip not found: {zip_path}")
    if sql_path and not sql_path.is_file():
        sys.exit(f"[!] SQL file not found: {sql_path}")
    if asset_path and not asset_path.is_file():
        sys.exit(f"[!] Asset file not found: {asset_path}")

    print(f"=== Deploy version {args.version} ===")
    print(f"  web zip : {zip_path}  ({zip_path.stat().st_size // 1024} KB)")
    if sql_path:   print(f"  sql     : {sql_path}  ({sql_path.stat().st_size // 1024} KB)")
    if asset_path: print(f"  asset   : {asset_path}  ({asset_path.stat().st_size // 1024 // 1024} MB)")
    if extra_delete: print(f"  delete  : {extra_delete}")

    with tempfile.TemporaryDirectory(prefix="ftpdeploy_") as tmp:
        tmp_dir = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_dir)
        n_files = sum(1 for _ in tmp_dir.rglob("*") if _.is_file())
        print(f"[ok] zip extracted locally: {n_files} files")

        if args.dry_run:
            print(f"[dry] Would upload to {web_root}:")
            upload_tree(None, tmp_dir, web_root, file_chmod, dir_chmod, dry=True)
            if asset_path: print(f"[dry] Would upload asset -> {asset_remote_dir}/{asset_path.name}")
            for rel in extra_delete: print(f"[dry] Would delete from server: {web_root.rstrip('/')}/{rel}")
            if sql_path: print(f"[dry] Would run SQL migration via {deploy_dir}/import.php (token-protected GET)")
            return 0

        ftp = ftp_connect(cfg)
        try:
            # 1. site files
            print(f"[..] Uploading site files to {web_root} ...")
            n = upload_tree(ftp, tmp_dir, web_root, file_chmod, dir_chmod, dry=False)
            print(f"[ok] Uploaded {n} files")

            # 2. binary asset
            if asset_path:
                print(f"[..] Uploading asset ({asset_path.stat().st_size // 1024 // 1024} MB) ...")
                ftp_upload_file(ftp, asset_path,
                                f"{asset_remote_dir.rstrip('/')}/{asset_path.name}",
                                file_chmod=file_chmod, dir_chmod=dir_chmod)

            # 3. delete obsolete files
            for rel in extra_delete:
                ftp_delete_quiet(ftp, f"{web_root.rstrip('/')}/{rel}")

            # 4. SQL migration
            if sql_path:
                ok = run_sql_migration(ftp, cfg, sql_path, web_root, deploy_dir, file_chmod, dir_chmod)
                if not ok:
                    print("[!] SQL migration failed — your files are deployed but DB is not migrated.")
                    return 2
        finally:
            try: ftp.quit()
            except Exception:
                try: ftp.close()
                except Exception: pass

    print("=== Deploy complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
