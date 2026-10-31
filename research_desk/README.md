# Research Desk · E1002 数据连接

这个目录里的页面维持 800×480 横屏 V6 排版。`data.json` 由本机采集程序写入真实数据；原来的模拟数值已单独移到 `demo-data.json`，只有 URL 加 `?demo=1` 才会加载。页面按真实标题高度动态分页，每 5 分钟切换一页，依次轮播全部 Zotero 页和 AI 页。

## 启动

```bash
cd /path/to/research_desk
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py
```

本机预览地址：`http://127.0.0.1:8000/research_desk.html`。服务默认只监听本机。

## E1002 屏幕写入与轮播

屏幕通过 OpenDisplay 写入，当前页记录在 `.state/screen_cycle.json`。屏幕服务优先通过 Wi-Fi/LAN 发现并写入 E1002，未发现时回退到 BLE；未使用 USB 传图。彩色屏使用 800×480 原尺寸和整屏刷新。

网页控制栏可手动刷新数据并重写当前页，也可手动前进或后退。命令行手动刷新当前页：

```bash
curl -X POST http://127.0.0.1:8000/api/refresh
```

按键输入已映射为：输入 0 上一页、输入 1 下一页、输入 2 刷新数据并重写当前页。另可通过 `screen_cycle.py --once` 前进一页，或 `screen_cycle.py --refresh-current` 刷新当前页。

设备导出的配置目前没有 Wi-Fi 包。要把 E1002 加入 2.4 GHz 网络，运行下面的本地交互配置；密码输入时不回显，凭据只写入权限为 `0600` 的 `.state/` 配置文件和设备。写入后会重启设备并尝试 LAN 发现：

```bash
.state/opendisplay-venv/bin/python configure_e1002_wifi.py
```

E1002 仅支持 2.4 GHz Wi-Fi。写入成功后，屏幕程序会自动使用 OpenDisplay LAN/TCP；本机发现不到设备时才回退 BLE。

在 macOS 登录时自动运行本地数据服务、每 5 分钟更新数据并轮播 RSS 各页和 AI 页面：

```bash
python3 install_screen_agent.py
```

注意：macOS 曾因 Xcode 自带的 Python 缺少 `NSBluetoothAlwaysUsageDescription`，以 `OS_REASON_TCC` 终止 LaunchAgent。当前会话已从前台授权环境以 `server.py --screen` 启动本地服务，轮播循环每 5 分钟尝试上传下一页，同时监听机身按键。若设备休眠，写屏程序会重试 4 次，仍不可达则保留当前屏幕并记录错误。这个前台服务不保证在退出 Codex 或注销后继续运行。安装脚本会先检查 Python 的蓝牙用途描述，避免再装一个启动即崩溃的 LaunchAgent。

E1002 的电源配置已通过 OpenDisplay 核验并改为 USB 模式：空闲睡眠计时为 `Never`，深度睡眠已关闭。配置修改前的私有备份在 `.state/e1002-config-before.json`；配置 JSON 含设备凭据，仅保存在权限为 `0600` 的 `.state/` 中。可用 `configure_e1002_usb.py` 再次读取、检查并应用该配置。

OpenDisplay 使用 Python 3.13。安装写屏环境并把当前 OpenDisplay 密钥放入权限为 `0600` 的 `.state/opendisplay.key`；`.state/` 整个目录为 `0700` 且已加入 `.gitignore`。密钥不会写入 HTML、JSON 或日志。停止后台服务：

```bash
launchctl bootout gui/501/com.researchdesk.screen
```

轮播状态和服务日志保存在 `.state/`。页面顺序是所有 Zotero 页，然后 AI 页；一次屏幕整刷大约需要数秒到十几秒。

后台数据服务每 5 分钟重新读取数据并轮播屏幕；启用 `--screen` 后，每 5 分钟被动采样 E1002 温湿度传感器，并错开屏幕 BLE 写入时段。

也可先单次刷新并检查汇总：

```bash
.venv/bin/python refresh_data.py
```

写屏依赖单独放在 `.state/opendisplay-venv`：当前 E1002 OpenDisplay 密钥保存在本机私有状态目录，不随源码同步。

## 已接入的数据源

- **Zotero Feeds**：只读本机 `~/Zotero/zotero.sqlite` 的 13 个 Feeds 订阅和缓存条目，然后抓取订阅的 RSS/Atom。按 DOI、GUID、规范化 URL 去重；保留原始发布时间与首次发现时间。默认按 `first_seen` 选“今天”，也可设置 `RESEARCH_DESK_RSS_TODAY_BASIS=published`。网络失败时保留本地 SQLite 缓存的上次数据。订阅 URL 不会写进页面 JSON 或日志。
- **Codex 账号 Token**：通过 Codex App Server 的官方 `account/usage/read` 读取 ChatGPT 账号累计 Token、每日 Token 桶、峰值日与连续天数。`today` 使用 API 返回的当日桶，`total` 为 `lifetimeTokens`；该接口不提供 Input/Output/Cache 拆分，因此明细显示为峰值/连续天数。额度仍通过 `account/rateLimits/read` 读取。
- **OpenCode 账号 Token**：通过 OpenCode Console 的账号级 Usage API 汇总，`today` 使用本地零点（`Asia/Shanghai`）到当前时刻，`total` 使用全部可用日汇总。总量包含 Input、Output、Cache Read、Cache Write；这与 OpenCode 网页端“账号用量”一致，不是本机 CLI 的记录。需要具有 `usage:read` 权限的 Service API Key，放在 `.state/opencode_api_key`（`0600`，已 gitignore）或 `RESEARCH_DESK_OPENCODE_API_KEY`。
- **OpenCode Go 额度**：优先用同一个 Service API Key 请求 `https://opencode.ai/zen/go/v1/usage`，读取 `usage.rolling/weekly/monthly` 的百分比与重置时间；失败时回退到 `opencode api get /api/experimental/usage/limits` 的本地 Console 登录态。OpenCode CLI 通过绝对路径和可用的 PATH 解析，避免后台服务因 PATH 缺少 `/opt/homebrew/bin` 而无法读取额度。
- **E1002 温湿度**：通过 BLE 被动读取 E1002 的 SHT40 广播，不建立 BLE 会话，也不走 USB。需要系统蓝牙权限与 `bleak`。读数会本地保留；扫描暂不可用时显示上次成功读数，没有读数则显示 `N/A`。

## 当前数据缺口

OpenCode 账号用量与 Go 额度都需要本机保存的 Service API Key；如果 Key 失效或没有 `usage:read` 权限，页面会显示 `N/A`，不会用本机 CLI 的数字替代账号数字。Go 配额条按滚动 5 小时、每周、每月窗口显示，模型标签显示最近一次本地使用的 `opencode-go` 模型。

Codex 账号 Token 通过官方 `account/usage/read` 获取；`today` 使用 API 返回的当日桶，因此可能随 API 更新节奏变化，`total` 为账号累计 `lifetimeTokens`。该接口不提供 Input/Output/Cache 拆分。

当天 Zotero 没有文章时，RSS 页不生成、也不参与轮换，屏幕只显示 AI 页。

当前 BLE 传感器是否可读取决于 E1002 正在广播且 macOS 允许 Python 使用蓝牙。可以运行 `refresh_data.py` 查看只包含读取状态的结果；扫描器不会打印 BLE 地址或原始广播内容。

## 本地文件与安全

- `.state/research_desk.sqlite3`：首次发现记录、Codex 去重游标、传感器最近读数；目录权限为 `0700`，数据库为 `0600`。
- `.state/opencode_api_key`：OpenCode Console Service API Key，权限 `0600`，已被 gitignore；不会写入 `data.json` 或提交到仓库。
- `data.json`：供页面读取的聚合显示数据，不包含账户凭据、邮箱或 RSS URL。
- `demo-data.json`：仅供视觉预览的示例数据。默认生产页面不会加载。
- `server.py` 只提供页面、JSON 与演示 JSON，不会暴露状态数据库或配置文件；默认绑定 `127.0.0.1`。

可设置以下变量覆盖本机路径或筛选规则：

```text
RESEARCH_DESK_ZOTERO_DB
RESEARCH_DESK_OPENCODE_DB
RESEARCH_DESK_OPENCODE_API_KEY  (OpenCode Console Service API Key；也可写入 .state/opencode_api_key)
RESEARCH_DESK_TIMEZONE=Asia/Shanghai
RESEARCH_DESK_RSS_TODAY_BASIS=first_seen|published
RESEARCH_DESK_DISPLAY_NAME  (E1002 设备名；也可写入 .state/display_name；缺失时 BLE 设备相关功能自动禁用)
```

原型截图由 `render_v6.py` 生成，尺寸固定为 800×480。`?demo=1` 只用于确认长标题动态分页和排版，不代表实际数据。
