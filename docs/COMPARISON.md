# Comparison with existing FTP-deploy tools

Three popular FTP-deploy tools were evaluated against a real Chinese shared host (西部数码 / pure-ftpd behind Cloudflare). This document is the per-tool deep-dive; the top-level README has the summary table.

The evaluation used four hard requirements that this hosting class enforces:

1. **`SITE CHMOD`** after each `STOR` (Apache runs as a different user from FTP; files default to mode `600` → unreadable → Apache 403/500).
2. **`DELETE`-then-`STOR`** for files owned by another user (panel-uploaded files are typically owned by `root`; direct `STOR` returns `553 Could not create file`, even though we own the parent dir and *can* delete them).
3. **Rename-aside for root-owned directories** (we can't `STOR` inside them at all; the only path is to rename them via the parent we own, then `MKD` a fresh one).
4. **WAF-safe SQL migration** (Cloudflare WAF blocks POST bodies containing markdown / SQL patterns; the migration must be uploaded out-of-band).

---

## 1. `mar10/pyftpsync`

Repo: <https://github.com/mar10/pyftpsync>
Last meaningful release: 4.1.0 (Apr 2024)
GitHub stars: ~120

**Incremental upload**: Yes — uses local-folder `.pyftpsync-meta.json` files storing last-sync `(mtime, size)` for conflict detection. No hashes.

**MLSD dependency**: **Hard requirement.** `FTPTarget.get_dir()` calls MLSD exclusively; on `500` (server doesn't support MLSD) it raises *"The FTP server does not support the 'MLSD' command."* with no LIST fallback. Recurring issues: [#51](https://github.com/mar10/pyftpsync/issues/51), [#55](https://github.com/mar10/pyftpsync/issues/55). For Chinese pure-ftpd hosts this is a **blocker risk** — many strip MLSD. Verify with `FEAT` before adopting.

**`SITE CHMOD` / post-upload hook**: **Not supported.** Zero `chmod` / `SITE` references in `ftp_target.py`. The only post-write callback is per-chunk progress, not post-STOR. No CLI flag, no config knob. You'd have to subclass `FTPTarget.write_file()`.

**Overwriting root-owned files**: **Direct STOR only.** `write_file()` calls `STOR` without a preceding `DELE`. `--force` bypasses the "remote-is-newer" conflict check but does **not** issue `DELE` first. On 553 it errors out. No "force-delete-on-overwrite" mode.

**Verdict**: Three of four hard requirements fail out of the box. Adopting it means subclassing `FTPTarget` to add chmod + delete-on-conflict, monkey-patching MLSD → LIST, *and* still bolting on the SQL migration externally. That's more code than maintaining a 300-line custom script.

Sources:
- [README](https://github.com/mar10/pyftpsync)
- [ftp_target.py source](https://pyftpsync.readthedocs.io/en/latest/_modules/ftpsync/ftp_target.html)
- [CLI reference](https://pyftpsync.readthedocs.io/en/latest/ug_cli.html)
- [Issue #51 — MLSD unsupported](https://github.com/mar10/pyftpsync/issues/51)

---

## 2. `saierd/ftp-sync`

Repo: <https://github.com/saierd/ftp-sync>
Last commit: June 2022
GitHub stars: 1
Status: effectively abandoned.

**Incremental upload**: Index file is **uploaded to the server** as `ftp_sync.json.gz` (gzipped JSON of SHA-256 hashes), then re-downloaded next run. Compares hashes, not mtime. README explicitly avoids mtime as "problematic for CI-generated files."

**First run on existing site**: tries to GET the index, falls back to empty dict on `error_perm` / `FileNotFoundError` → re-uploads everything. Pre-existing remote files are left untouched unless `--delete-files`.

**Server independence**: **No.** Imports only `ftplib.FTP_TLS` and `paramiko` — there is **no plain-FTP code path**. If your host is plain FTP (no FTPS), you must monkey-patch `FtpsConnection._connect_ftp`. Doesn't need MLSD (walks via the local index, not server listing).

**`SITE CHMOD` / post-upload hook**: **Neither exists.** Zero `SITE` / `chmod` / hook references in the source.

**Overwriting root-owned files**: **Direct STOR only** — `storbinary("STOR " + remote_filename, f)` with no preceding `DELE`. Would return `553` and abort on our hosting. `connection.delete()` exists but is only invoked for orphan cleanup under `--delete-files`, never as a pre-STOR step.

**Verdict**: Effectively dead (4 commits total, last June 2022, 1 star, single 319-line file, no PyPI release). Even monkey-patched for plain FTP, you'd still need to add `DELE`-before-`STOR`, `SITE CHMOD` per file, plus the WAF-safe SQL pipeline.

One idea worth stealing: the gzipped JSON SHA-256 index pattern for skip-unchanged without server-side MLSD.

Sources:
- [README + repo](https://github.com/saierd/ftp-sync)
- [ftp_sync.py source (319 lines)](https://raw.githubusercontent.com/saierd/ftp-sync/master/ftp_sync.py)
- [Commit history (4 commits total)](https://github.com/saierd/ftp-sync/commits/master)

---

## 3. `SamKirkland/ftp-deploy` (npm CLI) and `SamKirkland/FTP-Deploy-Action` (GitHub Action)

Repos:
- npm CLI: <https://github.com/SamKirkland/ftp-deploy>
- GitHub Action: <https://github.com/SamKirkland/FTP-Deploy-Action>

Stars: Action 4.9k, CLI ~113. Most popular FTP-deploy tool overall.
Last release: v4.4.0 (Apr 2026).
Stack: Node.js, built on `basic-ftp`.

**Incremental upload**: Uses a server-stored JSON state file (`.ftp-deploy-sync-state.json`) tracking what was deployed; on next run it diffs local vs state and uploads only changes. The diff is **content-based** (per maintainer comment in [#209](https://github.com/SamKirkland/FTP-Deploy-Action/issues/209): "File content is the same, doing nothing"), so effectively hash/size. Incremental is fast (seconds for small diffs); first run is full upload.

**MLSD dependency**: Built on `basic-ftp`, which supports **MLSD, Unix-LIST, and DOS-LIST** formats with automatic fallback. Pure-ftpd answers MLSD natively. Compatible with most servers.

**`SITE CHMOD` / post-upload hook**: **Not supported.** Issue [#209 — "support file permissions"](https://github.com/SamKirkland/FTP-Deploy-Action/issues/209) open since 2022. Referenced PR #269 unmerged. No post-upload hook API, no custom-command escape hatch. **Cannot satisfy our 644/755 requirement without forking.**

**Overwriting root-owned files**: Pure `STOR` via `basic-ftp`'s `uploadFrom`, **no `DELETE` first**. Issue [#255 — "NEW FEATURE: force"](https://github.com/SamKirkland/FTP-Deploy-Action/issues/255) confirms the gap. Would return `553` and fail or skip files on our hosting.

**Node/Windows**: Pure Node.js (Node ≥10). Works on Windows. `npx @samkirkland/ftp-deploy` runs without install.

**GitHub Action variant**: Clean YAML workflow with `uses: SamKirkland/FTP-Deploy-Action@v4.4.0`, secrets in repo Settings. Triggers on `push`. Excellent push-to-deploy ergonomics — *if* you can solve the chmod/553 issues another way.

**Maturity**: Action 4.9k stars, 143 open issues. NPM CLI is a separate younger package (`@samkirkland/ftp-deploy` v1.2.5, Nov 2025, 113 stars). The older NPM package `ftp-deploy` (different author, ~8.5k weekly downloads) is unrelated.

**Verdict**: Two hard requirements fail (no `SITE CHMOD`, no `DELETE`-then-`STOR`), both with multi-year open issues. The Action's push-to-deploy ergonomics are attractive but you'd need to wrap something else around it to handle perms / root files. We recommend wrapping `python deploy.py` in a tiny GitHub Action instead.

Sources:
- [SamKirkland/ftp-deploy](https://github.com/SamKirkland/ftp-deploy)
- [SamKirkland/FTP-Deploy-Action](https://github.com/SamKirkland/FTP-Deploy-Action)
- [Issue #209 — file permissions / CHMOD (open since 2022)](https://github.com/SamKirkland/FTP-Deploy-Action/issues/209)
- [Issue #255 — force / DELETE-then-STOR](https://github.com/SamKirkland/FTP-Deploy-Action/issues/255)
- [basic-ftp library](https://github.com/patrickjuchli/basic-ftp)

---

## Why this repo (`ftp-deploy-shared`) exists

The three tools above cover ~95% of typical PaaS / managed-server FTP deploys. They fail on the long-tail of cheap shared hosting where:

- Existing files are owned by another user (panel uploads).
- Files written via FTP are mode 600 by default.
- The host's `AllowOverride` forbids the `.htaccess` directives that would normally compensate.
- The migration must go through an in-place PHP helper (no SSH).
- A WAF in front of the origin inspects HTTP bodies aggressively.

This repo encodes the hard-won workarounds — `DELETE`-then-`STOR`, rename-aside, `SITE CHMOD` after every upload, prepared-statement SQL migration over base64 over FTP, browser User-Agent for the HTTPS trigger — in 300 lines of standard-library Python and one PHP file.

If your hosting doesn't need any of these, you can and should use one of the above tools instead.
