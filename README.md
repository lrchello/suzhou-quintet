# SUZHOU QUINTET

苏州爵士五重奏的官网：演出、成员、往期现场，以及人工核对的订单与电子票。

Python 3.13 + Flask + Jinja + SQLite，服务端渲染，核心表单不依赖 JavaScript。当前已完成本机维护与上线准备，服务器、真实域名和公网 HTTPS 尚待配置。

## 项目预览

![桌面首页](docs/maintenance-home-desktop.png)

截图来自本机运行的官网。仓库分享应用代码、测试和公开页面截图，不包含真实数据库、管理员凭据、订单、上传照片或备份。首次初始化会创建演示内容，不能恢复截图中的全部真实资料。

项目用于整合乐队的演出信息、成员介绍和现场记录，也用于学习内容管理、订单状态、访问控制及维护流程。支付由工作人员人工核对；目前未进行公网发布，也未验证真实大流量承载能力。

功能和架构可参考 [项目说明](docs/项目说明.md)，检查依据见 [验证记录](docs/验证记录.md)。代码包含辅助工具参与维护的改动；说明以实际代码和验证结果为准。

## 本机运行

在能看到 app.py 的目录打开 PowerShell。如果已有可用虚拟环境，不必重新创建。

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe setup.py
.\.venv\Scripts\python.exe serve.py --preview
~~~

打开 <http://127.0.0.1:8000>，后台在 /admin。预览只监听本机，按 Ctrl+C 停止。以后只需运行最后一条命令；端口冲突时加 --port 8765。

首次初始化询问管理员账号和至少 12 位的密码，没有内置密码。已有账号与内容保留，不会清空数据库。开发可以使用 Flask 调试服务器，正式运行使用 serve.py。

## 功能与边界

| 模块 | 功能 |
| --- | --- |
| 官网 | 演出、成员、往期回顾、图片、视频链接、地图与 ICS 日历 |
| 内容管理 | 发布和草稿、阵容、照片批量上传、预览和保留选择 |
| 订单 | 整数分计价、库存事务、20 分钟占位、防重复下单、密钥找回 |
| 电子票 | 人工确认到账后出票、独立随机票码、二维码、核销与退款记录 |
| 后台 | 订单筛选与分页、场次统计、操作审计 |
| 运维 | 生产配置检查、健康检查、完整备份与校验、代码规范与 CI 模板 |

支付采用人工核对，工作人员须在收款应用确认真实到账，再出票。网站不会自动收款、退款或查询支付平台。截图不能替代到账核验。

适合小规模、单实例运行。尚未实现自动支付、细分工作人员角色、两步验证、自动通知、部分退款或离线验票。缺少联系渠道、收款配置或须知时，正式收费演出不能下单。

## 安全机制

- 密码哈希；管理员会话在数据库保存摘要，退出与改密码会撤销，闲置 30 分钟或登录 8 小时后失效。
- 修改操作校验 CSRF；权限、数量、价格、库存与状态由后端验证。
- 登录按网络地址及账号限流，订单查询与下单也有限流，返回 Retry-After。
- Jinja 转义，SQL 参数绑定，CSP、禁止嵌入、nosniff、无来源泄露及敏感页面禁缓存。
- 图片限制格式、大小与像素，重新编码并清除元数据；随机文件名，草稿与收款图片按权限访问。
- 生产启动要求 HTTPS Cookie、明确的随机密钥、公开地址及可信域名，默认不信任转发头。

本次将 Werkzeug 升级为 3.1.9，修复 Windows 文件路径漏洞，见[官方公告](https://github.com/pallets/werkzeug/security/advisories/GHSA-g6x2-hccm-hh4m)。扫描仅反映当时已知漏洞，详情见 [验证记录](docs/验证记录.md)。

## 目录

| 文件 | 职责 |
| --- | --- |
| app.py | 页面路由、表单验证、后台操作 |
| config.py / security.py | 生产配置、CSRF、会话、限流 |
| ticketing.py | 库存、订单状态、出票、退款、核销 |
| db.py / schema.sql | 数据库连接、数据结构、索引 |
| gallery_uploads.py | 图片验证、重新编码、相册选择 |
| maintenance.py | 部署检查、备份、健康检查 |
| public_pages.py | 隐私说明、站点地图与搜索元数据 |
| templates/ / static/ | 页面、CSS 与渐进增强脚本 |
| serve.py / deploy/ | Waitress 与代理、服务配置示例 |
| tests/ | 功能、安全、并发与备份测试 |
| docs/项目说明.md | 功能、架构与技术取舍 |
| docs/上线与验收.md | 配置、验收、维护与恢复 |

**instance/ 含真实数据库、图片和密钥，不能公开发布或提交到 Git。** maintenance-backups/ 及本机内容审核笔记也可能含私人信息，已忽略。Git 仓库以本目录为根目录；GitHub Actions 的实际结果以仓库 Actions 页面为准。

## 测试和格式

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m djlint templates --check
.\.venv\Scripts\python.exe tools/format_assets.py --check
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt --no-deps --disable-pip
~~~

测试使用独立临时数据库。漏洞扫描需要联网。编辑后统一格式：ruff format .、djlint templates --reformat --quiet、python tools/format_assets.py，然后运行 ruff check .。

## 备份和正式上线

~~~powershell
.\.venv\Scripts\python.exe -m flask --app app backup --full backups/band-20261006.zip
.\.venv\Scripts\python.exe -m flask --app app verify-backup backups/band-20261006.zip
~~~

目标已存在时拒绝覆盖。完整备份包含数据库、图片、本机密钥文件及校验清单；生产环境变量中的密钥须单独安全保存。旧版仅数据库命令 backup backups/band.sqlite3 仍可用。

首次部署到全新 Debian 13 服务器，请按 [一步步上线教程](docs/一步步上线.md) 操作，包含本机打包、数据迁移、生产配置、自动启动和 HTTPS。

生产变量参考 .env.example，程序不会自动读取该文件。配置后执行 check-deployment，再运行 python serve.py。服务只监听 127.0.0.1:8000，前面须配置可信 HTTPS 代理。请完成 [上线与验收](docs/上线与验收.md)，不要用 --preview 对外上线。
