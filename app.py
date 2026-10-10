
import os, sys, time, json, requests
import urllib.parse

API_KEY    = os.environ.get("API_KEY") or ""
SERVER_ID  = os.environ.get("SERVER_ID") or ""
COOKIE_RAW = os.environ.get("COOKIE") or ""
EMAIL      = os.environ.get("EMAIL") or ""
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""

BASE_URL = (os.environ.get("BASE_URL") or "https://panel.host-ship.com").rstrip("/")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 20

MAX_RENEWAL_DAYS = 30   # 面板续期上限：续满 30 天后按钮不可点
RENEW_INTERVAL   = int(os.environ.get("RENEW_INTERVAL") or 65)  # 两次续期间隔（面板要求 ≥1 分钟，留 5s 余量）
MAX_RENEW_ROUNDS = 6    # 单次运行最多续期几次（安全阀；正常 3 次即可满 30 天）
POWER_WAIT       = int(os.environ.get("POWER_WAIT") or 15)     # 发出 start 后等待几秒再复查状态

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
    return {"running": "运行中", "starting": "启动中", "stopping": "停止中",
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
    return any(k in low for k in ("limit", "maximum", "max", "cooldown",
                                  "locked", "renewable", "too many"))

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

def main():
    print("#" * 25)
    print("   Host-ship 自动续期 + 状态巡检（纯 HTTP）")
    print("#" * 25)
    if not API_KEY and not COOKIE_RAW:
        print("ℹ️ 未配置 API_KEY 且无 COOKIE，脚本终止。")
        sys.exit(1)
    if not SERVER_ID:
        print("ℹ️ 未配置 SERVER_ID（服务器 UUID），脚本终止。")
        sys.exit(1)
    print(f"🎯 目标服务器: {mask(SERVER_ID, 8, 4)}")
    print(f"🔑 主链 API_KEY: {'已配置' if API_KEY else '未配置'} / "
          f"备链 COOKIE: {'已配置' if COOKIE_RAW else '未配置'}")

    name, old, gate = "（未知）", "（未获取到）", {}
    if API_KEY:
        ok, snap, err = api_get_server()
        if ok:
            name, old, gate = snap
            print(f"📋 主链快照：{name} / {old} / {gate}")
        else:
            print(f"⚠️ 主链 GET 失败: {err}")
    elif COOKIE_RAW:
        s = build_cookie_session()
        if s is not None:
            ok, snap, err = cookie_get_server(s)
            if ok:
                name, old, gate = snap
                print(f"📋 备链快照：{name} / {old} / {gate}")

    old_before = old

    # ---------- 1. 运行状态 ----------
    state_before, _raw_before = fetch_state()
    print(f"🔌 运行状态: {state_before}")

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
                print(("⏭️ 无需续期: " if rounds == 0 else "🛑 停止续期: ") + reason)
                break
            if rounds >= MAX_RENEW_ROUNDS:
                stop_reason = f"已达单次运行续期上限（{MAX_RENEW_ROUNDS} 次）"
                print(f"🛑 {stop_reason}")
                break
            if rounds > 0:
                print(f"⏳ 等待 {RENEW_INTERVAL}s 后继续续期（面板要求间隔 ≥1 分钟）...")
                time.sleep(RENEW_INTERVAL)
            ok, info, via = renew_once()
            if not ok:
                if is_limit_error(info):
                    stop_reason = f"续期被拒（上限/冷却）: {info[:120]}"
                    print(f"🛑 {stop_reason}")
                else:
                    renew_failed = True
                    renew_error = str(info)
                    print(f"❌ 续期失败: {info}")
                break
            rounds += 1
            print(f"✅ 第 {rounds} 次续期成功（{via}）")
            if API_KEY:
                ok2, snap2, err2 = api_get_server()
                if ok2:
                    name, old, gate = snap2
                    print(f"📋 续期后快照：{name} / {old} / {gate}")
                else:
                    print(f"⚠️ 续期后 GET 失败: {err2}")
            else:
                s = build_cookie_session()
                if s is not None:
                    ok2, snap2, _ = cookie_get_server(s)
                    if ok2:
                        name, old, gate = snap2
                        print(f"📋 续期后快照：{name} / {old} / {gate}")

    # ---------- 3. 开机（停机就 start）----------
    boot_note = ""
    state_after = state_before
    raw_after = _raw_before
    if rounds > 0 or _raw_before is not None:
        if raw_after is None:
            state_after, raw_after = fetch_state()
        if (raw_after or "").strip().lower() == "offline":
            print("▶️ 检测到服务器已停机，发送 start ...")
            ok, info = (False, "未配置 API_KEY / COOKIE")
            if API_KEY:
                ok, info = api_power("start")
                if not ok and COOKIE_RAW and ("401" in info or "403" in info):
                    print("🔄 主链开机认证失败，切换备链 Cookie 重试...")
                    ok, info = cookie_power("start")
            elif COOKIE_RAW:
                ok, info = cookie_power("start")
            if ok:
                print(f"⏳ 已发送 start，等待 {POWER_WAIT}s 复查状态 ...")
                time.sleep(POWER_WAIT)
                state_after, raw_after = fetch_state()
                if (raw_after or "").strip().lower() == "running":
                    boot_note = f"已启动（{state_before} → {state_after}）"
                    print(f"✅ 开机完成: {boot_note}")
                else:
                    boot_note = f"已发送 start，但状态仍为「{state_after}」，请人工确认"
                    print(f"⚠️ {boot_note}")
            else:
                boot_note = f"开机失败: {info[:120]}"
                print(f"❌ {boot_note}")
        elif raw_after:
            boot_note = f"无需开机（当前{state_text(raw_after)}）"
            print(f"ℹ️ {boot_note}")
        else:
            boot_note = "状态未知，未执行开机"
            print(f"⚠️ {boot_note}")

    # ---------- 4. 通知 ----------
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
        reason = ""
    send_telegram_message(format_notification(
        title, server_name=name,
        old=old_before,
        new=old if (rounds > 0 and old != old_before) else "",
        state=f"{state_before} → {state_after}" if state_after != state_before else state_before,
        boot=boot_note, reason=reason, error=renew_error))

    print(f"🏁 执行完毕：续期 {rounds} 次 / 状态 {state_before} → {state_after} / {boot_note}")
    if renew_failed:
        sys.exit(1)

if __name__ == "__main__":
    main()
