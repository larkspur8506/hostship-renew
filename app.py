
import os, sys, time, json, requests
import urllib.parse

API_KEY    = os.environ.get("API_KEY") or ""
SERVER_ID_RAW = (os.environ.get("SERVER_ID")
                 or os.environ.get("SERVER_IDS") or "")
SERVER_ID  = ""   # 当前正在处理的服务器 UUID，由 set_current_server() 设置
COOKIE_RAW = os.environ.get("COOKIE") or ""
EMAIL      = os.environ.get("EMAIL") or ""
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""

BASE_URL = (os.environ.get("BASE_URL") or "https://panel.host-ship.com").rstrip("/")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 20

MAX_RENEWAL_DAYS = 14   # 面板续期上限（2026-10-21 实测由 30 收紧到 14；超了 POST 返 400「cannot add more than 14 days」，只能靠 POST 判，renewable 标志不再可信）
RENEW_INTERVAL   = int(os.environ.get("RENEW_INTERVAL") or 65)  # 两次续期间隔（面板要求 ≥1 分钟，留 5s 余量）
MAX_RENEW_ROUNDS = 6    # 单台服务器单次运行最多续期几次（安全阀；上限 14 天后 1~2 次即可打满）
POWER_WAIT       = int(os.environ.get("POWER_WAIT") or 10)      # 发出 start 后首次等待秒数
POWER_POLL_TOTAL = int(os.environ.get("POWER_POLL_TOTAL") or 120)  # 确认开机的最长轮询时间
POWER_POLL_EVERY = int(os.environ.get("POWER_POLL_EVERY") or 10)   # 轮询间隔

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def parse_server_ids(raw: str):
    """SERVER_ID 支持多台：逗号 / 分号 / 竖线 / 空格 / 换行分隔，去重且保持顺序。"""
    if not raw:
        return []
    norm = raw.replace(",", "\n").replace(";", "\n").replace("|", "\n")
    out = []
    for item in norm.split():
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


SERVER_IDS = parse_server_ids(SERVER_ID_RAW)


def set_current_server(sid: str):
    """所有请求函数都读全局 SERVER_ID，这里切换当前目标。"""
    global SERVER_ID
    SERVER_ID = sid


def mask(s: str, head: int = 6, tail: int = 4) -> str:
    s = s or ""
    return f"{s[:head]}...{s[-tail:]}" if len(s) > head + tail else "***"

def mask_email(addr: str) -> str:
    addr = (addr or "").strip()
    if not addr:
        return "（未填）"
    if "@" in addr:
        name, domain = addr.split("@", 1)
        if len(name) <= 4:
            return f"{name}@{domain}"
        return f"{name[:2]}****{name[-2:]}@{domain}"
    return addr[:2] + "****" if len(addr) > 2 else "***"

def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": message,
                            "parse_mode": "HTML"}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")

def state_text(state, suspended=False) -> str:
    """把面板运行态翻译成中文"""
    if suspended:
        return "已暂停（suspended）"
    key = (state or "").strip().lower()
    # 面板会把刚开机的服务器报成 starting；对用户来说这就是"运行中"
    return {"running": "运行中", "starting": "运行中", "stopping": "停止中",
            "offline": "已停机"}.get(key, state or "（未知）")

def format_notification(status: str, server_name: str = "", old: str = "",
                        new: str = "", error: str = "", reason: str = "",
                        state: str = "", boot: str = "") -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))
    lines = ["🎰 Host-ship 续期报告", "", f"{status}",
             f"📧 账号: {mask_email(EMAIL)}"]
    if server_name:
        lines.append(f"🖥️ 服务器: {server_name}")
    if state:
        lines.append(f"🔌 运行状态: {state}")
    if new:
        lines.append(f"⏱ 续期前到期时间: {old or '（未获取到）'}")
        lines.append(f"⏱ 续期后到期时间: {new}")
    elif old:
        lines.append(f"⏱ 续期前到期时间: {old}")
    if boot:
        lines.append(f"▶️ 开机: {boot}")
    if reason:
        lines.append(f"📝 原因: {reason}")
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"🕒 执行时间: {now}(北京时间)")
    return "\n".join(lines)

def format_server_block(index: int, res: dict) -> str:
    """多台模式下的单台报告块"""
    tag = CIRCLED[index] if index < len(CIRCLED) else f"({index + 1})"
    lines = [f"{tag} {res['name']}"]
    st = res["state_before"]
    if res["state_after"] and res["state_after"] != st:
        st = f"{st} → {res['state_after']}"
    if st:
        lines.append(f"🔌 运行状态: {st}")
    if res["old_after"]:
        lines.append(f"⏱ 续期前到期时间: {res['old_before']}")
        lines.append(f"⏱ 续期后到期时间: {res['old_after']}")
    elif res["old_before"]:
        lines.append(f"⏱ 续期前到期时间: {res['old_before']}")
    if res["boot"]:
        lines.append(f"▶️ 开机: {res['boot']}")
    if res["reason"]:
        lines.append(f"📝 原因: {res['reason']}")
    if res["error"]:
        lines.append(f"⚠️ 错误信息: {res['error']}")
    return "\n".join(lines)

def format_multi_notification(results) -> str:
    """多台汇总报告：先总览再逐台"""
    ok = sum(1 for r in results if r["rounds"] > 0 and not r["renew_failed"])
    bad = sum(1 for r in results if r["renew_failed"])
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))
    if bad:
        head = f"⚠️ 共 {len(results)} 台：续期成功 {ok} 台 / 失败 {bad} 台"
    elif ok:
        head = f"✅ 共 {len(results)} 台：续期成功 {ok} 台"
    else:
        head = f"⏭️ 共 {len(results)} 台：续期时间未到"
    lines = ["🎰 Host-ship 续期报告", "", head,
             f"📧 账号: {mask_email(EMAIL)}", ""]
    for i, r in enumerate(results):
        lines.append(format_server_block(i, r))
        lines.append("")
    lines.append(f"🕒 执行时间: {now}(北京时间)")
    return "\n".join(lines).strip()

def parse_cookie_str(raw: str):
    pairs = []
    for item in (raw or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name, value = name.strip(), value.strip()
        if name and value:
            pairs.append((name, value))
    return pairs

def pick_snapshot(attrs: dict):
    """返回 (name, old, gate)"""
    if not isinstance(attrs, dict):
        return "（未知）", "（未获取到）", {}
    name = attrs.get("name") or attrs.get("server_name") or "（未知）"
    renewal = attrs.get("renewal")
    try:
        renewal = int(renewal) if renewal is not None else None
    except (TypeError, ValueError):
        renewal = None
    try:
        cooldown = int(attrs.get("renewal_cooldown") or 0)
    except (TypeError, ValueError):
        cooldown = 0
    gate = {"renewable": attrs.get("renewable"),
            "renewal": renewal,
            "renewal_cooldown": cooldown,
            "renewal_locked": attrs.get("renewal_locked")}
    if renewal is not None:
        old = f"剩余{renewal}天"
    else:
        old = "（未获取到）"
    return str(name), old, gate

def should_skip_renew(gate: dict):
    if not isinstance(gate, dict) or not gate:
        return False, ""
    renewal = gate.get("renewal")
    if isinstance(renewal, int) and renewal >= MAX_RENEWAL_DAYS:
        return True, f"Renew Limit Reached（renewal={renewal}，已达{MAX_RENEWAL_DAYS}天上限，无需续期）"
    if gate.get("renewable") is False:
        return True, "后端标记 renewable=false，无需续期"
    if gate.get("renewal_locked") is True:
        return True, "renewal_locked=true，已锁定，无需续期"
    cd = gate.get("renewal_cooldown") or 0
    if isinstance(cd, int) and cd > 0:
        return True, f"冷却中（renewal_cooldown={cd}），暂无需续期"
    return False, ""

LIMIT_TEXT = "renew limit reached"

def contains_limit_text(text: str) -> bool:
    low = (text or "").lower()
    nospace = " ".join(low.split())
    return LIMIT_TEXT in nospace or "renew limit" in nospace

def is_limit_error(info: str) -> bool:
    if contains_limit_text(info):
        return True
    low = (info or "").lower()
    # 2026-10-21：面板把总上限收紧到 14 天，拒绝文案是
    # "You cannot add more than 14 days of renewal time."——不含 limit/max 等旧关键词，
    # 漏判会让「太满」被记成续期失败（退出码 1、workflow 变红）。
    if ("cannot add more than" in low or "days of renewal" in low
            or "renewal time" in low):
        return True
    return any(k in low for k in ("limit", "maximum", "max", "cooldown",
                                  "locked", "renewable", "too many"))

def panel_detail(info: str, limit: int = 110) -> str:
    """把面板错误响应提炼成一句话：'HTTP 400: {"errors":[{"detail":"..."}]}' → detail。"""
    try:
        body = info.split(":", 1)[1].strip()
        j = json.loads(body)
        errs = j.get("errors") or []
        if errs and isinstance(errs, list):
            return str(errs[0].get("detail") or "").strip()[:limit]
    except Exception:
        pass
    return (info or "")[:limit]

def api_headers():
    return {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest", "User-Agent": UA}

def api_get_server():
    try:
        r = requests.get(f"{BASE_URL}/api/client/servers/{SERVER_ID}",
                         headers=api_headers(), timeout=TIMEOUT)
        if r.status_code == 200:
            if contains_limit_text(r.text):
                print("⏭️ GET 响应里检测到 Renew Limit Reached")
            try:
                attrs = r.json().get("attributes", {})
            except Exception:
                attrs = {}
            return True, pick_snapshot(attrs if isinstance(attrs, dict) else {}), ""
        return False, ("（未知）", "（未获取到）", {}), f"GET {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, ("（未知）", "（未获取到）", {}), f"GET 异常: {e}"

def api_get_state():
    """运行状态：GET /resources → attributes.current_state / is_suspended
    返回 (state, suspended) 或 None"""
    try:
        r = requests.get(f"{BASE_URL}/api/client/servers/{SERVER_ID}/resources",
                         headers=api_headers(), timeout=TIMEOUT)
        if r.status_code == 200:
            attrs = r.json().get("attributes", {}) or {}
            return attrs.get("current_state"), bool(attrs.get("is_suspended"))
        print(f"⚠️ 状态查询 HTTP {r.status_code}: {r.text[:150]}")
    except Exception as e:
        print(f"⚠️ 状态查询异常: {e}")
    return None

def api_power(signal: str = "start"):
    try:
        r = requests.post(f"{BASE_URL}/api/client/servers/{SERVER_ID}/power",
                          headers=api_headers(), json={"signal": signal},
                          timeout=TIMEOUT)
        print(f"📡 POST power({signal}) → HTTP {r.status_code}")
        if r.status_code in (200, 201, 202, 204):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"POST 异常: {e}"

def build_cookie_session():
    pairs = parse_cookie_str(COOKIE_RAW)
    if not pairs:
        return None
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Referer": f"{BASE_URL}/"})
    for name, value in pairs:
        try:
            s.cookies.set(name, value, domain="panel.host-ship.com", path="/")
        except Exception as e:
            print(f"⚠️ 装配 Cookie {name} 失败: {e}")
    return s

def cookie_power(signal: str = "start"):
    s = build_cookie_session()
    if s is None:
        return False, "COOKIE 为空或格式错误"
    try:
        xsrf = s.cookies.get("XSRF-TOKEN", domain="panel.host-ship.com") or ""
        try:
            xsrf = urllib.parse.unquote(xsrf)
        except Exception:
            pass
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                   "Content-Type": "application/json"}
        if xsrf:
            headers["X-XSRF-TOKEN"] = xsrf
        r = s.post(f"{BASE_URL}/api/client/servers/{SERVER_ID}/power",
                   headers=headers, json={"signal": signal}, timeout=TIMEOUT)
        print(f"📡 备链 POST power({signal}) → HTTP {r.status_code}")
        if r.status_code in (200, 201, 202, 204):
            return True, ""
        if r.status_code in (401, 403, 419):
            return False, f"备链认证失败（HTTP {r.status_code}）"
        return False, f"备链 HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"备链异常: {e}"

def cookie_get_server(s: requests.Session):
    try:
        g = s.get(f"{BASE_URL}/api/client/servers/{SERVER_ID}", timeout=TIMEOUT)
        print(f"📡 备链 GET → HTTP {g.status_code}")
        if g.status_code == 200:
            try:
                attrs = g.json().get("attributes", {})
            except Exception:
                attrs = {}
            return True, pick_snapshot(attrs if isinstance(attrs, dict) else {}), ""
        if g.status_code in (401, 403):
            return False, ("（未知）", "（未获取到）", {}), f"备链 Cookie 已失效（GET {g.status_code}）"
        return False, ("（未知）", "（未获取到）", {}), f"备链 GET {g.status_code}: {g.text[:200]}"
    except Exception as e:
        return False, ("（未知）", "（未获取到）", {}), f"备链 GET 异常: {e}"

def page_has_limit_text(session: requests.Session = None) -> bool:
    if not COOKIE_RAW:
        return False
    try:
        short_id = (SERVER_ID or "").split("-")[0]
        if not short_id:
            return False
        s = session or requests.Session()
        if session is None:
            for name, value in parse_cookie_str(COOKIE_RAW):
                try:
                    s.cookies.set(name, value, domain="panel.host-ship.com", path="/")
                except Exception:
                    pass
        h = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
             "Referer": f"{BASE_URL}/"}
        r = s.get(f"{BASE_URL}/server/{short_id}", headers=h, timeout=TIMEOUT)
        if r.status_code == 200 and contains_limit_text(r.text):
            print("⏭️ 服务器页面检测到 Renew Limit Reached，本次跳过续期")
            return True
    except Exception as e:
        print(f"⚠️ 页面上限探测失败（忽略，走 API 门控）: {e}")
    return False

def api_renew():
    try:
        r = requests.post(f"{BASE_URL}/api/client/servers/{SERVER_ID}/renew",
                          headers=api_headers(), timeout=TIMEOUT)
        print(f"📡 POST renew → HTTP {r.status_code}")
        if r.status_code in (200, 201, 202, 204):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        return False, f"POST 异常: {e}"

def cookie_renew():
    s = build_cookie_session()
    if s is None:
        return False, "COOKIE 为空或格式错误"
    try:
        g = s.get(f"{BASE_URL}/api/client/servers/{SERVER_ID}", timeout=TIMEOUT)
        print(f"📡 备链 GET → HTTP {g.status_code}")
        if g.status_code in (401, 403):
            return False, f"备链 Cookie 已失效（GET {g.status_code}）"
        if g.status_code == 200:
            if contains_limit_text(g.text):
                return False, "Renew Limit Reached，无需续期（备链 GET 响应里检测到上限文本）"
            try:
                attrs = g.json().get("attributes", {})
            except Exception:
                attrs = {}
            _n, _old, _gate = pick_snapshot(attrs if isinstance(attrs, dict) else {})
            print(f"📋 备链快照：{_n} / {_old} / {_gate}")
            _skip, _reason = should_skip_renew(_gate)
            if _skip:
                return False, f"Renew Limit Reached，无需续期（{_reason}）"
        xsrf = s.cookies.get("XSRF-TOKEN", domain="panel.host-ship.com") or ""
        try:
            xsrf = urllib.parse.unquote(xsrf)
        except Exception:
            pass
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        if xsrf:
            headers["X-XSRF-TOKEN"] = xsrf
        r = s.post(f"{BASE_URL}/api/client/servers/{SERVER_ID}/renew",
                   headers=headers, timeout=TIMEOUT)
        print(f"📡 备链 POST renew → HTTP {r.status_code}")
        if r.status_code in (200, 201, 202, 204):
            return True, ""
        if r.status_code in (401, 403, 419):
            return False, f"备链认证失败（HTTP {r.status_code}），请重拷 COOKIE"
        return False, f"备链 HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        return False, f"备链异常: {e}"

def renew_once():
    """续期一次：主链 API 优先，认证失败自动切备链 Cookie。返回 (ok, info, via)"""
    if API_KEY:
        ok, info = api_renew()
        if ok:
            return True, "", "主链 API"
        if is_limit_error(info):
            return False, info, "主链 API（上限/冷却）"
        if COOKIE_RAW and ("401" in info or "403" in info):
            print("🔄 主链认证失败，切换备链 Cookie 重试...")
            ok2, info2 = cookie_renew()
            return ok2, info2, "备链 Cookie"
        return False, info, "主链 API"
    if COOKIE_RAW:
        ok, info = cookie_renew()
        return ok, info, "备链 Cookie"
    return False, "未配置 API_KEY / COOKIE", "无"

def fetch_state():
    """返回 (中文状态, raw_state)；查不到返回 ('（未获取到）', None)"""
    if not API_KEY:
        return "（未获取到，需 API_KEY）", None
    st = api_get_state()
    if st is None:
        return "（未获取到）", None
    state, suspended = st
    return state_text(state, suspended), state

def new_result(sid: str) -> dict:
    return {"sid": sid, "name": "（未知）", "old_before": "（未获取到）",
            "old_after": "", "state_before": "（未获取到）",
            "state_after": "（未获取到）", "boot": "", "rounds": 0,
            "renew_failed": False, "error": "", "reason": "",
            "title": "⏭️ 续期时间未到"}

def process_server(sid: str, index: int = 1, total: int = 1) -> dict:
    """处理单台服务器：快照 → 循环续期 → 必要时开机。返回结果字典。"""
    set_current_server(sid)
    res = new_result(sid)
    tag = f"[{index}/{total}] " if total > 1 else ""
    print("=" * 46)
    print(f"{tag}🎯 目标服务器: {mask(sid, 8, 4)}")

    name, old, gate = "（未知）", "（未获取到）", {}
    if API_KEY:
        ok, snap, err = api_get_server()
        if ok:
            name, old, gate = snap
            print(f"{tag}📋 主链快照：{name} / {old} / {gate}")
        else:
            print(f"{tag}⚠️ 主链 GET 失败: {err}")
    elif COOKIE_RAW:
        s = build_cookie_session()
        if s is not None:
            ok, snap, err = cookie_get_server(s)
            if ok:
                name, old, gate = snap
                print(f"{tag}📋 备链快照：{name} / {old} / {gate}")

    old_before = old
    res["name"] = name
    res["old_before"] = old_before

    # ---------- 1. 运行状态 ----------
    state_before, _raw_before = fetch_state()
    print(f"{tag}🔌 运行状态: {state_before}")

    # ---------- 2. 续期（循环直到满 30 天 / 不可续 / 达安全上限）----------
    rounds = 0
    renew_failed = False
    renew_error = ""
    stop_reason = ""

    if page_has_limit_text():
        stop_reason = "页面显示 Renew Limit Reached"
    else:
        while True:
            skip, reason = should_skip_renew(gate)
            if skip:
                stop_reason = reason if rounds == 0 else f"已停止：{reason}"
                print(tag + ("⏭️ 无需续期: " if rounds == 0 else "🛑 停止续期: ") + reason)
                break
            if rounds >= MAX_RENEW_ROUNDS:
                stop_reason = f"已达单次运行续期上限（{MAX_RENEW_ROUNDS} 次）"
                print(f"{tag}🛑 {stop_reason}")
                break
            if rounds > 0:
                print(f"{tag}⏳ 等待 {RENEW_INTERVAL}s 后继续续期（面板要求间隔 ≥1 分钟）...")
                time.sleep(RENEW_INTERVAL)
            ok, info, via = renew_once()
            if not ok:
                if is_limit_error(info):
                    stop_reason = f"{old}，面板暂不接受加时：{panel_detail(info)}"
                    print(f"{tag}🛑 {stop_reason}")
                else:
                    renew_failed = True
                    renew_error = str(info)
                    print(f"{tag}❌ 续期失败: {info}")
                break
            rounds += 1
            print(f"{tag}✅ 第 {rounds} 次续期成功（{via}）")
            if API_KEY:
                ok2, snap2, err2 = api_get_server()
                if ok2:
                    name, old, gate = snap2
                    print(f"{tag}📋 续期后快照：{name} / {old} / {gate}")
                else:
                    print(f"{tag}⚠️ 续期后 GET 失败: {err2}")
            else:
                s = build_cookie_session()
                if s is not None:
                    ok2, snap2, _ = cookie_get_server(s)
                    if ok2:
                        name, old, gate = snap2
                        print(f"{tag}📋 续期后快照：{name} / {old} / {gate}")

    # ---------- 3. 开机（停机就 start）----------
    boot_note = ""
    state_after = state_before
    raw_after = _raw_before
    if rounds > 0 or _raw_before is not None:
        if raw_after is None:
            state_after, raw_after = fetch_state()
        if (raw_after or "").strip().lower() == "offline":
            print(f"{tag}▶️ 检测到服务器已停机，发送 start ...")
            ok, info = (False, "未配置 API_KEY / COOKIE")
            if API_KEY:
                ok, info = api_power("start")
                if not ok and COOKIE_RAW and ("401" in info or "403" in info):
                    print("🔄 主链开机认证失败，切换备链 Cookie 重试...")
                    ok, info = cookie_power("start")
            elif COOKIE_RAW:
                ok, info = cookie_power("start")
            if ok:
                print(f"{tag}⏳ 已发送 start，开始轮询状态（每 {POWER_POLL_EVERY}s 一次，"
                      f"最多 {POWER_POLL_TOTAL}s）...")
                waited = 0
                while waited < POWER_POLL_TOTAL:
                    step = POWER_WAIT if waited == 0 else POWER_POLL_EVERY
                    time.sleep(step)
                    waited += step
                    state_after, raw_after = fetch_state()
                    if (raw_after or "").strip().lower() in ("running", "starting"):
                        boot_note = f"已启动（{state_before} → {state_after}，{waited}s 后确认）"
                        print(f"{tag}✅ 开机完成: {boot_note}")
                        break
                    print(f"   …已等 {waited}s：{state_after}")
                else:
                    boot_note = (f"已发送 start（HTTP 204），但 {POWER_POLL_TOTAL}s 内面板状态仍为"
                                 f"「{state_after}」；可能是状态上报延迟，请稍后自行确认")
                    print(f"{tag}⚠️ {boot_note}")
            else:
                boot_note = f"开机失败: {info[:120]}"
                print(f"{tag}❌ {boot_note}")
        elif raw_after:
            boot_note = f"无需开机（当前{state_text(raw_after)}）"
            print(f"{tag}ℹ️ {boot_note}")
        else:
            boot_note = "状态未知，未执行开机"
            print(f"{tag}⚠️ {boot_note}")

    # ---------- 4. 结果 ----------
    if renew_failed:
        title = "❌ 续期失败"
    elif rounds > 0:
        title = "✅ 续期成功"
    else:
        title = "⏭️ 续期时间未到"

    if rounds > 0 and stop_reason:
        reason = f"续期 {rounds} 次后停止：{stop_reason}"
    elif renew_failed:
        reason = stop_reason
    else:
        # 未续期也要把原因带上（例：剩余 20 天、面板 14 天上限 → 未到可续窗口）
        reason = stop_reason

    res.update({
        "name": name,
        "old_before": old_before,
        "old_after": old if (rounds > 0 and old != old_before) else "",
        "state_before": state_before,
        "state_after": state_after,
        "boot": boot_note,
        "rounds": rounds,
        "renew_failed": renew_failed,
        "error": renew_error,
        "reason": reason,
        "title": title,
    })
    print(f"{tag}🏁 执行完毕：续期 {rounds} 次 / 状态 {state_before} → {state_after} / {boot_note}")
    return res

def main():
    print("#" * 25)
    print("   Host-ship 自动续期 + 状态巡检（纯 HTTP / 支持多台）")
    print("#" * 25)
    if not API_KEY and not COOKIE_RAW:
        print("ℹ️ 未配置 API_KEY 且无 COOKIE，脚本终止。")
        sys.exit(1)
    if not SERVER_IDS:
        print("ℹ️ 未配置 SERVER_ID（服务器 UUID），脚本终止。")
        sys.exit(1)
    print(f"🎯 共 {len(SERVER_IDS)} 台服务器: "
          + ", ".join(mask(s, 8, 4) for s in SERVER_IDS))
    print(f"🔑 主链 API_KEY: {'已配置' if API_KEY else '未配置'} / "
          f"备链 COOKIE: {'已配置' if COOKIE_RAW else '未配置'}")

    results = []
    for i, sid in enumerate(SERVER_IDS):
        try:
            results.append(process_server(sid, i + 1, len(SERVER_IDS)))
        except Exception as e:
            print(f"❌ 服务器 {mask(sid, 8, 4)} 处理异常: {e}")
            bad = new_result(sid)
            bad.update({"renew_failed": True, "error": str(e), "title": "❌ 续期失败"})
            results.append(bad)

    # ---------- 通知：单台沿用原格式，多台用汇总格式 ----------
    if len(results) == 1:
        r = results[0]
        send_telegram_message(format_notification(
            r["title"], server_name=r["name"],
            old=r["old_before"], new=r["old_after"],
            state=(f"{r['state_before']} → {r['state_after']}"
                   if r["state_after"] != r["state_before"] else r["state_before"]),
            boot=r["boot"], reason=r["reason"], error=r["error"]))
    else:
        send_telegram_message(format_multi_notification(results))

    failed = [r for r in results if r["renew_failed"]]
    total_rounds = sum(r["rounds"] for r in results)
    print(f"🏁 全部完成：{len(results)} 台 / 共续期 {total_rounds} 次 / 失败 {len(failed)} 台")
    if failed:
        sys.exit(1)

if __name__ == "__main__":
    main()
