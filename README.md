## 🚢 Host-ship 自动续期 + 状态巡检（GitHub Actions，纯 HTTP 单账号版）

### 🔐 Secrets 配置说明

| Secret 名称   | 是否必填 | 说明 |
|---------------|----------|------|
| API_KEY       | ✅ 必填* | 面板 Client API key（`ptlc_` 开头）。*无则走备链 |
| SERVER_ID     | ✅ 必填  | 服务器 UUID（面板服务器页面 URL 里那串 `xxxxxxxx-xxxx-...`） |
| COOKIE        | ❌ 可选  | 备链用，格式 `remember_web_...=xxx; jexactyl_session=yyy`（F12 → Application → Cookies → panel.host-ship.com） |
| EMAIL         | ❌ 可选  | 通知备注名 |
| TG_BOT_TOKEN  | ❌ 可选  | Telegram Bot Token |
| TG_CHAT_ID    | ❌ 可选  | Telegram Chat ID |

### 🔁 每次运行做什么

1. **读快照**：`GET /api/client/servers/{uuid}` → `name` / `renewal`（剩余天数）/ `renewable` / `renewal_cooldown` / `renewal_locked`
2. **查运行状态**：`GET /api/client/servers/{uuid}/resources` → `current_state`（`running` / `offline` …）+ `is_suspended`
   - 面板详情接口的 `status` 字段是空的，**运行态只能从这里拿**，日志里打印的就是它
3. **续期循环**：只要没到 30 天上限、后端允许（`renewable` / `renewal_locked` / `renewal_cooldown` 门控通过），就反复 `POST /renew`，
   每次之间等待 `RENEW_INTERVAL`（默认 65s，面板要求间隔 ≥1 分钟），直到**续满 30 天**或不可续；单次运行最多 `MAX_RENEW_ROUNDS`（默认 6）次
4. **开机**：若 `current_state == offline`，发 `POST /api/client/servers/{uuid}/power` `{"signal":"start"}`，
   然后**轮询状态**（首次等 `POWER_WAIT`，之后每 `POWER_POLL_EVERY` 秒一次，最长 `POWER_POLL_TOTAL`），
   直到状态变成 `running`/`starting`（都算开机成功）或超时
   - 面板状态上报有延迟：实测 start 返回 204 后，`current_state` 可能十几秒到一分钟还是 `offline`，
     所以不能只看一次就下结论
5. **通知**：Telegram 一条汇总（续期次数、续期前后到期时间、运行状态变化、开机结果）

> 续期与开机是两件事：面板不会因为续期成功就自动开机，所以脚本在续期后补了一次 start。
> 免费机规则：每次续期最多 +10 天、累计上限 30 天（满了按钮不可点）；只要剩余天数 > 0 服务器就能开机。

### ⚙️ 可调环境变量（一般不用设）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BASE_URL` | `https://panel.host-ship.com` | 面板地址（本地联调时可指向 mock） |
| `RENEW_INTERVAL` | `65` | 两次续期之间的等待秒数 |
| `POWER_WAIT` | `10` | 发出 start 后首次复查前的等待秒数 |
| `POWER_POLL_TOTAL` | `120` | 确认开机的最长轮询时间（秒） |
| `POWER_POLL_EVERY` | `10` | 轮询间隔（秒） |

### 🧪 退出码

- `0`：续期成功 / 无需续期（开机失败也会打印明确提示）
- `1`：续期失败（Actions 会标红）

## ⚠️ 免责声明

仅供学习了解；遵守相关法律法规，作者不对不当行为负责。
