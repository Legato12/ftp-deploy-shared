# ftp-deploy-shared

**为「难搞的共享主机」上的 PHP/MySQL 网站设计的一行命令 FTP 部署工具。** 专门处理流行 FTP-deploy 工具（`SamKirkland/ftp-deploy`、`mar10/pyftpsync`、`saierd/ftp-sync`）在真实共享主机上拒绝处理的失败场景：

- 旧文件归属 `root`（`STOR` 返回 `553 Could not create file`）
- 旧目录归属 `root`（根本无法 `STOR` 进去）
- 上传后文件权限是 `600`，Apache（不同用户）读不了 → 403/500
- Cloudflare WAF 拦截 HTTP POST 体里的 SQL/Markdown 字符
- 没有 SSH，没法 `mysql < migration.sql`
- MySQL 解析器对带 emoji 的多行 `INSERT` 有版本差异 bug
- Cloudflare 把 Python 默认 User-Agent 当机器人封了
- 主机 `AllowOverride` 不允许 `.htaccess` 写 `Options` / `RemoveHandler` / `php_flag`

如果你的部署脚本一直撞这些坑——你来对地方了。

[English](README.md) · [详细对比 pyftpsync / saierd/ftp-sync / SamKirkland/ftp-deploy](docs/COMPARISON.md)

---

## 一行运行

```bash
git clone https://github.com/Legato12/ftp-deploy-shared.git
cd ftp-deploy-shared
cp .env.example .env                     # 填 FTP + DB 凭据（不需要 SSH）
python deploy.py 1.2.3                   # 上传文件 + 跑 SQL 迁移 + 推二进制资源
python deploy.py 1.2.3 --dry-run         # 看计划，不发送任何东西
```

只需 Python 3.8+ 标准库 + 服务器上一个 PHP 文件。无需 `pip install`，无需 Node，无需 Docker。

---

## 为什么有这个项目

我们在一台真实的西部数码主机（pure-ftpd + Cloudflare）上评估了三个流行的 FTP-deploy 工具。**全部在共享主机的两个强制要求上失败：**

| 要求 | pyftpsync | saierd/ftp-sync | SamKirkland/ftp-deploy | **本项目** |
|---|:---:|:---:|:---:|:---:|
| `STOR` 后 `SITE CHMOD`（Apache 用不同用户读文件） | ❌ | ❌ | ❌ 2022 起 issue 未合 | ✅ |
| `DELETE`-后-`STOR` 覆盖 root 文件（控制面板留下的） | ❌ | ❌ | ❌ 2022 起 issue 未合 | ✅ |
| 重命名 root 目录后重建（无法 `STOR` 进去） | ❌ | ❌ | ❌ | ✅ |
| 自带 SQL 迁移（无 SSH，没法 `mysql <`） | — | — | — | ✅ 通过 PHP 助手 |
| Cloudflare WAF 友好（base64 绕过内容审查） | — | — | — | ✅ |
| 不依赖 MLSD（国内主机经常剥离 MLSD） | ❌ 必须 | ✅ 本地索引 | ✅ basic-ftp 兜底 | ✅ 不需要列表 |
| 一行命令（不用 GitHub Actions / 不用 Node） | ✅ | ✅ | ❌ Node | ✅ 纯 Python 标准库 |

完整评估见 [`docs/COMPARISON.md`](docs/COMPARISON.md)。

---

## 它做了什么（每次 `python deploy.py <version>` 运行）

1. **本地解压**你构建好的 `web-update.zip` 到临时目录。
2. **通过 FTP** 把文件树上传到 `WEB_ROOT`（被动模式）：
   - 遇到 `553`（文件归 root）→ 先 `DELE`（因为父目录是我们的，删除权来自父目录），再 `STOR`。
   - 遇到 `553`（连父目录本身都归 root）→ 把父目录改名为 `_oldver_<name>_<时间戳>`（我们拥有它的上级），`MKD` 新的（现在归我们），再 `STOR`。
   - 每次 `STOR` 后：`SITE CHMOD 644`（让 Apache 能读）。
   - 每次 `MKD` 后：`SITE CHMOD 755`（让 Apache 能进入）。
3. **可选**：按版本号匹配上传一个二进制资源（地图、安装包、构建产物）。
4. **删除**项目里已经移除但服务器上还在的旧文件（通过 `EXTRA_DELETE` 配置）。
5. **跑 SQL 迁移**——这是整个流程里唯一走 HTTP 的步骤：
   - Python 本地解析 SQL 成结构化 JSON（避开服务器 `mysqli::multi_query` 在带 emoji 多行 `INSERT` 上的 bug）。
   - JSON **base64 编码**后通过 FTP 传上去（`db-data.b64`）——Cloudflare WAF 看不到原始 markdown 内容。
   - `import.php`（每次随机 token）通过 FTP 上传。
   - **触发** = HTTPS `GET` + URL 里的 token，**没有 POST body 给 WAF 检查**。
   - 服务端：先逐条跑 DDL（容忍 `Duplicate column` / `already exists`），然后用 `PDO::prepare()` + `execute([?, ?, ?])` 插入每一行——MySQL 永远不解析我们的 body 文本作为 SQL。
   - 成功后 `import.php` 和 `db-data.b64` **自毁**。
6. **验证**线上 URL 并报告。

---

## 配置（`.env`）

完整模板见 [`.env.example`](.env.example)。核心几项：

```env
FTP_HOST=ftp.yourhost.example
FTP_USER=yourftpuser
FTP_PASS=yourftppassword
FTP_PORT=21
FTP_TLS=0                          # FTPS 设 1

SITE_URL=https://yourdomain.example

# 注意：DB_HOST 是服务器视角！PHP 助手在服务器上跑，
# 走 localhost。不要填公网 DB IP。
DB_HOST=localhost
DB_NAME=yourdbname
DB_USER=yourdbuser
DB_PASS=yourdbpassword

ZIP_PATH=./web-update.zip
SQL_PATH=./db-deploy.sql          # 不需要 SQL 迁移就留空

WEB_ROOT=/wwwroot                  # 西部数码用 /wwwroot；其他主机可能是 /htdocs

FILE_CHMOD=644
DIR_CHMOD=755
```

---

## 常见错误对照表

| 现象 | 根因 | 本工具如何处理 |
|---|---|---|
| `STOR` 返回 `553 Could not create file` | 文件归别的用户（通常是控制面板上传时的 root） | 自动检测，先 `DELE` 再 `STOR`（父目录是我们的） |
| `553` 仍然不行 | **父目录**就归别的用户 | 把父目录改名 `_oldver_<name>_<时间戳>` + `MKD` 新的（归我们） |
| 部署后 Apache 全站 500 | 文件以 `umask 077` 写入 → 权限 `600`，Apache 不同用户读不了 | 每次 `STOR` 后 `SITE CHMOD 644`；每次 `MKD` 后 `SITE CHMOD 755` |
| 静态文件（比如 `.png`）单独 500 | 你的 `.htaccess` 写了主机 `AllowOverride` 不允许的指令（最常见：`Options -ExecCGI`、`php_flag engine off`、`RemoveHandler`） | 不是脚本的问题。把 `.htaccess` 里这些行去掉，只留 `<FilesMatch>` 块。`php_flag` 只对 mod_php 有效，**在 PHP-FPM 主机上会 500**。 |
| Cloudflare `1010 The owner has banned access based on your browser's signature` | Python 默认 UA 被反爬识别 | 工具发真实浏览器 UA（`HTTP_USER_AGENT` 可配） |
| POST 到 `import.php` 时连接被重置 | WAF 检查 POST body 命中 markdown / SQL 规则 | 工具用 FTP 上传数据 + 纯 GET 触发，**根本不发 POST body** |
| 重跑时 `Duplicate column` | ALTER/CREATE 已经执行过 | PHP 助手把 `Duplicate column` / `already exists` 当无害忽略（重跑是幂等的） |
| 本地 Python 报 `ERR_NAME_NOT_RESOLVED` | 你机器上的 DNS 走代理 / 假 IP（国内常见 Clash/V2Ray fake-IP 模式） | 填 `SITE_URL` 为真实公网域名；Cloudflare 会正确路由 |
| 部署的 PHP 报 `Parse error: unexpected ''' (T_ENCAPSED_AND_WHITESPACE)` | 上传被某些 FTP 服务器 / 代理截断（特别是用 `BytesIO` 当源时） | 工具始终用真实文件句柄 `open(..., 'rb')` 上传，并校验远端文件大小 = 本地大小，截断会立即报错 |

---

## 关键词（搜索用）

`FTP 部署`、`共享主机部署`、`pure-ftpd 部署`、`无 SSH PHP 部署`、`SQL 迁移 FTP`、`SITE CHMOD`、`553 错误 FTP`、`Could not create file`、`西部数码 部署`、`myhostadmin`、`Cloudflare 1010 修复`、`Cloudflare WAF 绕过`、`Apache 500 .htaccess`、`AllowOverride 限制`、`mysqli multi_query emoji bug`、`PDO 预处理 迁移`、`Claude Code 部署`、`AI 编程助手 部署`、`Python FTP 自动化`、`一行命令 部署 PHP`

---

## 许可证

MIT — 见 [LICENSE](LICENSE)。

## 来源

从 **Castle Fight Hub**（一个魔兽争霸 III 自定义地图社区站点，部署在西部数码）的部署过程中提炼。和 **Claude Code**（Anthropic）一起端到端调试出来的——三次失败，三轮 agent 调研，最终一个能跑的工具。文档里每一个症状都是在真实主机上踩过的。欢迎 PR 扩展更多主机的兼容性。

如果这个项目帮你省了一个周末，给个 Star ⭐ 让下一个人找到它。
