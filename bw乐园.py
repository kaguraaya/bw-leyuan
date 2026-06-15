#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili BW 预约抢票脚本（优化版）
作者：Kaguraaya
Python ≥3.8 仅依赖 requests
可选依赖：
orjson（超快 JSON 解析）
psutil（CPU 亲和/系统信息）
二者若未安装会自动回落到标准库实现，不影响功能，但对速度有一定影响。
"""
#装上所有的依赖:pip install requests orjson psutil
#792
#873,854,872,874
import sys, time, json, threading, requests, statistics, re, atexit, os
from datetime import datetime
from requests.adapters import HTTPAdapter
import importlib
import importlib.util

# Optional accel libs
def _fast_json_loads(data):
    return json.loads(data)

# 动态检测 orjson，避免静态导入报错
_spec = importlib.util.find_spec("orjson")
if _spec is not None:
    orjson = importlib.import_module("orjson")  # type: ignore[var-annotated]
    _fast_json_loads = orjson.loads  # type: ignore[assignment]

try:
    import psutil  # CPU 亲和与优先级
except ImportError:
    psutil = None

# Windows 1 ms 定时器 & 进程优先级
if sys.platform == "win32":
    try:
        import ctypes

        _winmm = ctypes.WinDLL("winmm")

        if _winmm.timeBeginPeriod(1) == 0:  # 返回 0 表示成功
            atexit.register(lambda: _winmm.timeEndPeriod(1))

        # 升高进程优先级
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080)  # HIGH_PRIORITY_CLASS
    except Exception:
        pass

# 绑定首核减少上下文切换（可按需修改）
if psutil is not None:
    try:
        p = psutil.Process()
        cpus = p.cpu_affinity()
        if cpus and len(cpus) > 1:
            p.cpu_affinity([cpus[0]])
    except Exception:
        pass

# ────────────────────  性能相关常量  ─────────────────────
# perf_counter 与 time.time 的纳秒级基准差，用于高精度忙等
_PERF_OFFSET_NS = time.perf_counter_ns() - time.time_ns()

# ────────────────────  0. 账号 Cookie & 购票日期  ──────────────────────
def _load_cookie() -> str:
    env_cookie = os.environ.get("BW_COOKIE", "").strip()
    if env_cookie:
        return env_cookie

    cookie_file = os.environ.get("BW_COOKIE_FILE", "cookie.txt")
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        print(f"❌ 读取 Cookie 文件失败: {exc}")
        return ""

RAW_COOKIE = _load_cookie()

# 购票日期配置：1=11号, 2=12号, 3=13号，可多选
# 例如：[2] 只看12号，[1,2,3] 看全部三天
TICKET_DAYS = [1,2]  # 默认只看12号

COOKIE_DICT = {kv.split("=", 1)[0].strip(): kv.split("=", 1)[1]
               for kv in RAW_COOKIE.split(";") if "=" in kv}

SESSDATA = COOKIE_DICT.get("SESSDATA")
BILI_JCT = COOKIE_DICT.get("bili_jct")
if not (SESSDATA and BILI_JCT):
    print("❌ Cookie 中缺少 SESSDATA / bili_jct，脚本无法工作")
    sys.exit(1)

# ────────────────────  1. 可调参数  ─────────────────────────
CFG = {
    "ahead_sec":     0.8,        # 提前秒数
    "threads":       32,           # 并发线程（降低以减少 GIL 竞争）
    "requests_per_thread": 2,     # 每个线程发送的请求次数
    "time_jitter_ms": 15,         # 请求时间抖动范围（毫秒）
    "preheat_rounds": 8,          # 每个目标在抢前预热的请求次数，减小卡顿
    "dry_run":       False,
    "debug":         True        # True 输出详细调试信息
}

# 日期映射
DAY_MAP = {1: 20250711, 2: 20250712, 3: 20250713}

# ────────────────────  2. Session 初始化  ───────────────────
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "origin":  "https://www.bilibili.com",
    "referer": "https://www.bilibili.com/blackboard/era/bws2025-event.html",
    "accept": "application/json, text/plain, */*",
    "accept-encoding": "gzip, deflate",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8"
}

sess = requests.Session()
sess.headers.update(HEADERS)
# 连接池大小根据实际并发调整
pool_size = CFG["threads"] * CFG.get("requests_per_thread", 2) * 2
sess.mount("https://", HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, pool_block=True))
for k, v in COOKIE_DICT.items():
    sess.cookies.set(k, v, domain=".bilibili.com")

# 动态检测 httpx 以启用 HTTP/2 低延迟
_spec_httpx = importlib.util.find_spec("httpx")
if _spec_httpx is not None:
    httpx = importlib.import_module("httpx")
    _httpx_cli = httpx.Client(http2=True, headers=HEADERS, timeout=2.0)
else:
    _httpx_cli = None

# 统一发 POST 的辅助函数：优先用 httpx HTTP/2
def _http_post(url: str, data: bytes, headers: dict[str, str]):
    if _httpx_cli is not None:
        return _httpx_cli.post(url, content=data, headers=headers)
    return sess.post(url, data=data, headers=headers, timeout=(1, 2))

def log(*msg):
    print(time.strftime("[%H:%M:%S]"), *msg, flush=True)

# 仅在 CFG["debug"] 为 True 时输出
def dbg(*msg):
    if CFG.get("debug"):
        log("DEBUG:", *msg)

# ────────────────────  3. 服务器时间同步  ──────────────────
# 主校时接口（通用，无需登录）
_TIME_SOURCES = [
    ("https://api.bilibili.com/x/report/click/now", "now"),                # 通用接口
    ("https://api.bilibili.com/x/activity/bws/online/park/nav", "server_time")  # 旧 BW 活动接口（兼容）
]
_TIME_OFFSET = 0.0        # server_time ≈ local + _TIME_OFFSET
_OFFSET_TS   = 0.0        # 上次校时本地时间

def _calibrate_offset(samples: int = 20):
    global _TIME_OFFSET, _OFFSET_TS
    log("🔄 正在与 B 站服务器校时…")
    offsets = []
    last_err = ""
    for _ in range(samples):
        t0 = time.time()
        try:
            # 依次尝试若干接口，只要有一个拿到时间就 break
            server = None
            for url, key in _TIME_SOURCES:
                r = sess.get(url, timeout=2)

                # 调试打印（状态码 + content-type + 前 120 字节）
                body_preview = r.text[:120].replace("\n", " ") if r.text else ""
                print("SRC", url.split("/x/")[-1][:25], "HTTP", r.status_code,
                      "CT", r.headers.get("content-type"), "BODY", body_preview)

                # 只处理 JSON 响应
                if not r.headers.get("content-type", "").startswith("application/json"):
                    continue

                try:
                    j = r.json()
                    # 兼容两种结构：{"now":xxx} 或 {"data": {"now":xxx}}
                    if key in j:
                        server = j.get(key)
                    else:
                        server = j.get("data", {}).get(key)

                    # 若返回毫秒时间戳，则转换为秒
                    if isinstance(server, (int, float)) and server > 1e12:
                        server /= 1000.0
                except Exception:
                    server = None

                if server:
                    break  # 本轮获取成功

            t1 = time.time()

            if server:
                offsets.append(server - (t0 + t1) / 2)
            else:
                last_err = "no server time"
        except Exception as e:
            last_err = str(e)

        # 防风控：每次请求之间稍微停 300 ms
        time.sleep(0.3)

    if offsets:
        _TIME_OFFSET = statistics.mean(offsets)
        _OFFSET_TS   = time.time()
        log(f"⏱️  时差校准成功: {_TIME_OFFSET*1000:.1f} ms (样本数={len(offsets)})")
    else:
        log(f"⚠️  时差校准失败，未能获取服务器时间，最后一次错误: {last_err}")

def now_server() -> float:
    """
    返回 float 秒级服务器时间，保证极低开销。1
    每 5 分钟重新校时一次。
    """
    if time.time() - _OFFSET_TS > 300:
        threading.Thread(target=_calibrate_offset, daemon=True).start()
    return time.time() + _TIME_OFFSET

# 首次同步
_calibrate_offset()

# ────────────────────  4. 场次相关 API  ────────────────────
INFO_URL = "https://api.bilibili.com/x/activity/bws/online/park/reserve/info"
GOODS_URL = "https://api.bilibili.com/x/activity/bws/online/park/goods/list"  # 商品接口

def fetch_info(reserve_type=0):
    """获取场次信息，支持多日期查询"""
    # 构建日期字符串：20250711,20250712,20250713
    dates = [str(DAY_MAP[d]) for d in TICKET_DAYS if d in DAY_MAP]
    date_str = ",".join(dates) if dates else "20250712"
    
    params = {"csrf": BILI_JCT,
              "reserve_date": date_str,
              "reserve_type": reserve_type}
    r = sess.get(INFO_URL, params=params, timeout=5).json()
    if r["code"] != 0:
        raise RuntimeError(f"接口错误 code={r['code']} msg={r.get('message')}")

    # ---------- DEBUG：保存原始 JSON 便于分析 ----------
    if CFG.get("debug"):
        filename = "_bw_goods.json" if reserve_type == 1 else "_bw_info.json"
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=2)
            dbg(f"JSON saved to {filename}", list(r.keys()))
        except Exception as e:
            dbg("save json err", e)

    return r["data"]

def fetch_goods():
    """获取商品列表 - 使用 reserve_type=1 获取 VIP/商品场次"""
    try:
        return fetch_info(reserve_type=1)
    except Exception as e:
        dbg("fetch_goods error:", e)
        return None

def _norm_status(start_ts: int, remain: int, now: float) -> int:
    if now < start_ts:
        return 0
    return 2 if remain <= 0 else 1

def parse_goods(data) -> list:
    """解析商品列表 - 实际上商品也在 reserve_list 里，只是 reserve_type=1"""
    if not data:
        return []
    
    # 商品也在 reserve_list 里
    raw = data.get("reserve_list", {})
    
    # 处理多日期格式
    if isinstance(raw, dict):
        items = []
        for date_key, v in raw.items():
            lst_v = v if isinstance(v, list) else [v]
            for itm in lst_v:
                if isinstance(itm, dict):
                    itm = itm.copy()
                    itm["_date"] = str(date_key)
                    items.append(itm)
        raw = items
    elif not isinstance(raw, list):
        raw = []
    
    # 获取票券信息
    ticket_map = {str(k): v.get("ticket", "") for k, v in data.get("user_ticket_info", {}).items()}
    
    now = int(now_server())
    lst = []
    
    for itm in raw:
        if not isinstance(itm, dict):
            continue
        
        start_ts = int(itm.get("reserve_begin_time") or itm.get("reserve_time") or 0)
        title_raw = (itm.get("title") or itm.get("act_title") or itm.get("sku_name") or "")
        loc = itm.get("reserve_location", "")
        title = f"{title_raw}｜{loc}" if loc else title_raw
        remain = int(itm.get("standard_stock", itm.get("surplus", 0)))
        
        next_open_ts = int(itm.get("next_reserve", {}).get("reserve_begin_time", 0))
        if next_open_ts > start_ts:
            start_ts = next_open_ts
        
        dt = datetime.fromtimestamp(start_ts) if start_ts else None
        start_str = f"{dt.month}月{dt.day}日 {dt.strftime('%H:%M:%S')}" if dt else "??:??:??"
        
        date_key = itm.get("_date") or str(itm.get("screen_date", ""))
        ticket_no = ticket_map.get(date_key, "")
        
        action_url = (itm.get("reserve_action_url") or itm.get("button_link") or
                      itm.get("url") or (DO_URL if ticket_no else RESV_URL))
        
        lst.append({
            "id": itm.get("reserve_id"),
            "title": f"[商品] {title}",
            "start": start_ts,
            "start_s": start_str,
            "remain": remain,
            "total": int(itm.get("standard_ticket_num", itm.get("total", 0))),
            "status": _norm_status(start_ts, remain, now),
            "next_open": next_open_ts,
            "url": action_url,
            "ticket": ticket_no,
            "is_goods": True
        })
    
    lst.sort(key=lambda x: x["start"])
    return lst

def parse_sessions(data) -> list:
    raw = data.get("reserve_list", [])
    # JSON 兼容处理
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    if isinstance(raw, dict):
        tmp = []
        for date_key, v in raw.items():
            lst_v = v if isinstance(v, list) else [v]
            for itm0 in lst_v:
                if isinstance(itm0, dict):
                    itm0 = itm0.copy()
                    itm0["_date"] = str(date_key)  # 记录所属日期，便于 ticket 匹配
                tmp.append(itm0)
        raw = tmp
    if not isinstance(raw, list):
        raw = []

    # 提取用户票券，key 为日期字符串，值为 ticket_no
    ticket_map = {str(k): v.get("ticket", "") for k, v in data.get("user_ticket_info", {}).items()}

    now = int(now_server())
    lst = []
    for itm in raw:
        if not isinstance(itm, dict):
            continue

        # ---------- DEBUG：输出可能的 URL 字段 ----------
        if CFG.get("debug"):
            rid_dbg = itm.get("reserve_id")

            # 1) 首层包含 url/link 的字段
            url_candidates = {k: v for k, v in itm.items()
                               if isinstance(v, str) and ("reserve" in v and "http" in v)}

            # 2) 递归查找嵌套结构中的 URL
            def _scan(obj):
                if isinstance(obj, dict):
                    for vv in obj.values():
                        _scan(vv)
                elif isinstance(obj, list):
                    for vv in obj:
                        _scan(vv)
                elif isinstance(obj, str):
                    if "/reserve" in obj and obj.startswith("http"):
                        dbg("URL-NEST", rid_dbg, obj)

            _scan(itm)

            if url_candidates:
                dbg("URL-CANDS", rid_dbg, url_candidates)

        start_ts = int(itm.get("reserve_begin_time") or itm.get("reserve_time") or 0)
        title_raw = (itm.get("title") or itm.get("act_title") or
                     itm.get("sku_name") or "")
        loc   = itm.get("reserve_location", "")
        title = f"{title_raw}｜{loc}" if loc else title_raw
        remain = int(itm.get("standard_stock", itm.get("surplus", 0)))

        # 修正显示时间：若存在 next_open 且更晚，则使用 next_open 作为真正开抢时间
        next_open_ts = int(itm.get("next_reserve", {}).get("reserve_begin_time", 0))
        if next_open_ts > start_ts:
            start_ts = next_open_ts  # ↓ 后续 status/等待 统一用修正后的时间

        display_ts = start_ts
        dt = datetime.fromtimestamp(display_ts) if display_ts else None
        start_str = f"{dt.month}月{dt.day}日 {dt.strftime('%H:%M:%S')}" if dt else "??:??:??"

        # 关联票券：部分 VIP/签售场次需要 ticket_no
        date_key = itm.get("_date") or str(itm.get("screen_date", ""))
        ticket_no = ticket_map.get(date_key, "")

        action_url = (itm.get("reserve_action_url") or itm.get("button_link") or
                       itm.get("url") or (DO_URL if ticket_no else RESV_URL))

        lst.append({
            "id":       itm.get("reserve_id"),
            "title":    title,
            "start":    start_ts,
            "start_s":  start_str,
            "remain":   remain,
            "total":    int(itm.get("standard_ticket_num", itm.get("total", 0))),
            "status":   _norm_status(start_ts, remain, now),
            "next_open": int(itm.get("next_reserve", {}).get("reserve_begin_time", 0)),
            "url":      action_url,
            "ticket":   ticket_no,
            "is_goods": False
        })
    lst.sort(key=lambda x: x["start"])
    return lst

def group_by_start(sessions: list):
    """把同一开抢时间的若干场次放在一起，返回 [(start_ts, [ses...]), ...]"""
    mp = {}
    for s in sessions:
        mp.setdefault(s["start"], []).append(s)
    return [(k, mp[k]) for k in sorted(mp)]

def print_sessions(lst):
    if not lst:
        return
    mark_map = {0: "未开", 1: "未开", 2: "售完"}
    for idx, it in enumerate(lst, 1):
        mark = mark_map.get(it["status"], f"状态{it['status']}")
        tag = "🛒" if it.get("is_goods") else "🎫"
        log(f" {idx:02d}  {tag} id={it['id']}  {it['start_s']}  "
            f"余{it['remain']:>4}/{it['total']:<4}  {mark}  {it['title']}")
    print()

# ────────────────────  5. 预约接口  ─────────────────────────
RESV_URL = "https://api.bilibili.com/x/activity/bws/online/park/reserve/add"
# VIP / 签售预约接口，需要附带 ticket_no
DO_URL   = "https://api.bilibili.com/x/activity/bws/online/park/reserve/do"

# 预编译正则 + bytes 直接匹配，加速接口响应解析
_CODE_RE = re.compile(rb'"code":\s*(-?\d+)')
_SUCCESS_BYTES = b'"code":0'

def reserve_once(reserve_id: int, url_use: str | None = None, ticket: str = ""):
    """单次预约请求，尽可能减少序列化/反序列化开销"""
    if CFG["dry_run"]:
        return {"code": 0, "message": "dry-run"}

    # 构造请求体：若带 ticket_no 说明走 /reserve/do 接口
    # 添加时间戳参数以避免请求被识别为重复
    import random
    ts = int(time.time() * 1000)
    nonce = random.randint(10000, 99999)
    
    if ticket:
        payload = (f"inter_reserve_id={reserve_id}&ticket_no={ticket}&csrf={BILI_JCT}&ts={ts}&_={nonce}").encode()
        if url_use is None:
            url_use = DO_URL
    else:
        payload = (f"csrf={BILI_JCT}&reserve_id={reserve_id}&reserve_type={CFG['reserve_type']}&ts={ts}&_={nonce}").encode()
        if url_use is None:
            url_use = RESV_URL

    try:
        # 使用完整头信息并补充 content-type，避免被服务器拦截
        _hdr = HEADERS.copy()
        _hdr["content-type"] = "application/x-www-form-urlencoded"
        # 轻微随机化 User-Agent 版本号
        _hdr["user-agent"] = _hdr["user-agent"].replace("125.0.0.0", f"125.0.{random.randint(0,9)}.{random.randint(0,99)}")

        dbg("POST", url_use)
        resp = _http_post(url_use, payload, _hdr)

        # ---------- 自动路径探测：若 404，则试一系列候选路径 ----------
        if resp.status_code == 404 and not (ticket and url_use.endswith("/reserve/do")):
            base = url_use.rsplit("/reserve", 1)[0]  # 去掉 /reserve... 部分
            alt_paths = [
                "/reserve/apply",               # 老脚本 fallback
                "/reserve/v2/add",
                "/reserve/v3/add",
                "/v2/reserve/add",
                "/v3/reserve/add",
                "/ticket/apply",
                "/ticket/reserve/add",
                "/reserve/add"                # 最后再试一次原路径避免漏
            ]
            for ap in alt_paths:
                alt_url = base + ap
                try:
                    dbg("probe", alt_url)
                    resp = _http_post(alt_url, payload, _hdr)
                    if resp.status_code != 404:
                        dbg("hit", alt_url, resp.status_code)
                        break
                except Exception as _e:
                    dbg("probe exc", alt_url, _e)
            content = resp.content  # bytes
        else:
            content = resp.content  # bytes

        # 若返回非 JSON（如 403/404 HTML），直接返回 HTTP status
        if not resp.headers.get("content-type", "").startswith("application/json"):
            dbg("HTTP", resp.status_code, "NON-JSON", content[:120])
            return {"code": resp.status_code, "message": "non-json"}

        # fast-path 成功命中
        if _SUCCESS_BYTES in content:
            return {"code": 0, "message": ""}

        # 次级：仅提取 code
        m = _CODE_RE.search(content)
        if m:
            return {"code": int(m.group(1)), "message": ""}

        # fallback：完整解析
        return _fast_json_loads(content)
    except Exception as e:
        dbg("EXC", e)
        return {"code": -1, "message": str(e)}

# ────────────────────  6. 抢票核心  ─────────────────────────
def wait_until(server_ts: int):
    """阻塞到服务器时间 server_ts"""
    while True:
        delta = server_ts - now_server()
        if delta <= 0:
            break
        if delta > 60:
            # 每分钟打一条心跳，避免用户以为假死
            log(f"⌛ 距目标 {int(delta) // 60}m{int(delta) % 60:02d}s")
            time.sleep(60)
        else:
            time.sleep(5 if delta > 10 else max(0.5, delta / 2))

def gun_worker(reserve_id: int, fire_ts_server: float, action_url: str, ticket: str = "", thread_id: int = 0):
    """
    fire_ts_server 服务器时间
    使用单次粗 sleep + perf_counter_ns 纳秒忙等
    改进：
    1. 每个线程发送多次请求
    2. 时间抖动避免所有请求同时到达
    3. 重试间隔调整为 0.35s 以绕过可能的限流窗口
    """
    import random
    
    requests_count = CFG.get("requests_per_thread", 2)
    jitter_ms = CFG.get("time_jitter_ms", 15)
    
    for req_idx in range(requests_count):
        # 为每个请求添加随机时间抖动（-jitter_ms 到 +jitter_ms）
        jitter_sec = random.uniform(-jitter_ms, jitter_ms) / 1000.0
        fire_time = fire_ts_server + jitter_sec + (req_idx * 0.05)  # 每次请求间隔 50ms
        
        fire_local = fire_time - _TIME_OFFSET
        early = fire_local - time.time()
        if early > 0.25:
            time.sleep(early - 0.20)
        
        target_ns = int(fire_time * 1e9 + _PERF_OFFSET_NS)
        while time.perf_counter_ns() < target_ns:
            pass
        
        ret = reserve_once(reserve_id, action_url, ticket)
        dbg(f"gun_worker t{thread_id} req{req_idx}", ret)
        
        if ret.get("code") == 0:
            log(f"\033[92m🔫 {reserve_id} 成功 [线程{thread_id} 请求{req_idx}] code=0 msg={ret.get('message','')}\033[0m")
            return  # 成功后立即返回，不再发送后续请求
        
        # 如果失败且不是最后一次请求，等待 0.35s（绕过可能的 0.3s 限流）
        if req_idx < requests_count - 1:
            time.sleep(0.35)
    
    # 所有请求都失败
    log(f"\033[91m❌ {reserve_id} 失败 [线程{thread_id}] code={ret.get('code')} msg={ret.get('message','')}\033[0m")

def fire_one(ses: dict):
    """
    完成一次场次的等待 + 并发开枪
    """
    # ---------- step-0：处理 next_open ----------
    if ses["remain"] <= 0 and ses["next_open"] > ses["start"]:
        if ses["next_open"] > now_server():
            fmt = datetime.fromtimestamp(ses["next_open"]).strftime("%H:%M:%S")
            log(f"⏳ 库存未上架，等待 next_open {fmt}")
            wait_until(ses["next_open"])

    # ---------- step-1：计算开枪时间 ----------
    fire_at_server = ses["start"] - CFG["ahead_sec"]
    if fire_at_server < now_server():
        fire_at_server = now_server() + 0.05  # 已过点则稍后再抢

    # ---------- step-2：实时倒计时 (>5 s) ----------
    last_sec = -1
    while True:
        delta = fire_at_server - now_server()
        if delta <= 8:
            print("\r", end="", flush=True)
            break
        sec = int(delta)
        if sec != last_sec:
            print(f"\r⌛ 距开抢 {sec:>4d}s", end="", flush=True)
            last_sec = sec
        time.sleep(1)

    # ---------- step-3：并发开枪 ----------
    _preheat_connection(ses["url"])
    log(f"▶️  {ses['id']} {ses['title']}  "
        f"{ses['start_s']}  即将开枪(提前 {CFG['ahead_sec']}s)  URL={ses['url']}")
    ths = [threading.Thread(target=gun_worker,
                            args=(ses["id"], fire_at_server, ses["url"], ses["ticket"], i))
           for i in range(CFG["threads"])]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

def fire_group(sess_list: list):
    """
    同一时间开票的若干场次一起抢。
    sess_list 至少 1 条，且 start 相同
    """
    # ---------- step-0：检查 next_open ----------
    for s in sess_list:
        if s["remain"] <= 0 and s["next_open"] > s["start"]:
            if s["next_open"] > now_server():
                fmt = datetime.fromtimestamp(s["next_open"]).strftime("%H:%M:%S")
                log(f"⏳ id={s['id']} 库存未上架，等待 next_open {fmt}")
                wait_until(s["next_open"])

    # ---------- step-1：统一计算开枪时间 ----------
    fire_at_server = sess_list[0]["start"] - CFG["ahead_sec"]
    if fire_at_server < now_server():
        fire_at_server = now_server() + 0.05

    # ---------- step-2：多行常驻倒计时（仅显示秒） ----------
    last_sec = None  # 上次打印的整秒
    while True:
        delta_f = fire_at_server - now_server()
        sec_left = int(delta_f + 0.999)  # 向上取整，6.1 → 7
        if sec_left <= 5:  # 进入最后 5 s 时停更
            sys.stdout.write("\r" + " " * 120 + "\r")
            sys.stdout.flush()
            break
        if sec_left != last_sec:  # 只有秒变才刷新
            line_lst = [f"[{s['id']}] ⌛ {sec_left:>4d}s"
                        for s in sess_list]
            out = "   ".join(line_lst)[:120].ljust(120)
            sys.stdout.write("\r" + out)
            sys.stdout.flush()
            last_sec = sec_left
        time.sleep(0.2)  # 200 ms 轮询即可

    # ---------- step-3：并发开枪 ----------
    _preheat_connection(sess_list[0]["url"])
    for s in sess_list:
        log(f"▶️  {s['id']} {s['title']}  {s['start_s']}  "
            f"即将开枪(提前 {CFG['ahead_sec']}s)  URL={s['url']}")

    ths = []
    thread_id = 0
    for s in sess_list:               # 每个 reserve_id
        for i in range(CFG["threads"]):
            ths.append(threading.Thread(target=gun_worker,
                                        args=(s["id"], fire_at_server, s["url"], s["ticket"], thread_id)))
            thread_id += 1
    for t in ths:
        t.start()
    for t in ths:
        t.join()

def _preheat_connection(url: str = RESV_URL, rounds=None):
    """提前建立连接，减少首次 TCP/TLS 延迟"""
    try:
        payload = f"csrf={BILI_JCT}&reserve_id=0&reserve_type={CFG['reserve_type']}".encode()
        if rounds is None:
            rounds = min(CFG.get("preheat_rounds", 8), CFG["threads"])
        for _ in range(rounds):
            _http_post(url, payload, {"content-type": "application/x-www-form-urlencoded"})
    except Exception:
        pass

# 扩展版预热：给定一组 id，全部打一次 HEAD（或失败 POST）建立 TLS 复用
def preheat_ids(id_list, url_map, rounds=None):
    if rounds is None:
        rounds = min(CFG.get("preheat_rounds", 8), CFG["threads"])
    for rid in id_list:
        url = url_map.get(rid, RESV_URL)
        try:
            payload = f"csrf={BILI_JCT}&reserve_id={rid}&reserve_type={CFG['reserve_type']}".encode()
            for _ in range(rounds):
                _http_post(url, payload, {"content-type": "application/x-www-form-urlencoded"})
        except Exception:
            pass

# ────────────────────  7. 菜单/业务函数  ───────────────────
def check_cookie():
    nav_api = "https://api.bilibili.com/x/web-interface/nav"
    j = sess.get(nav_api, timeout=5).json()
    ok = j.get("code") == 0
    uname = j.get("data", {}).get("uname", "--")
    log(f"Cookie 检测: {'✅有效' if ok else '❌失效'}  uname={uname}")

def show_today():
    # 显示当前查询的日期
    day_names = {1: "7月11日", 2: "7月12日", 3: "7月13日"}
    selected_days = [day_names.get(d, f"Day{d}") for d in TICKET_DAYS]
    log(f"📅 查询日期: {', '.join(selected_days)}")
    
    # 获取活动场次
    lst = parse_sessions(fetch_info(reserve_type=0))
    
    # 获取商品场次
    goods_data = fetch_goods()
    goods_lst = parse_goods(goods_data) if goods_data else []
    
    if lst:
        log("\n📅 活动场次：")
        print_sessions(lst)
    
    if goods_lst:
        log("\n🛒 商品场次：")
        print_sessions(goods_lst)
    
    if not lst and not goods_lst:
        log("⚠️  没有找到任何场次或商品")
        log("💡 提示：检查 TICKET_DAYS 配置，确保包含有票的日期")

def grab_flow():
    ids_in = input("输入要抢的 id（逗号分隔）、auto 自动挑选、或关键词（如 5070/RTX）: ").strip()
    
    # 获取活动和商品
    sesses = parse_sessions(fetch_info(reserve_type=0))
    goods_data = fetch_goods()
    goods_lst = parse_goods(goods_data) if goods_data else []
    all_items = sesses + goods_lst

    # 选票逻辑：支持 id、auto、关键词
    if ids_in.lower() == "auto":
        now = now_server()
        targets = [s for s in all_items
                   if s["status"] == 0 and s["start"] > now]
    elif ids_in.replace(",", "").replace(" ", "").isdigit():
        # 纯数字：按 id 筛选
        wanted = {int(x) for x in ids_in.replace(" ", "").split(",") if x.strip().isdigit()}
        targets = [s for s in all_items if s["id"] in wanted]
    else:
        # 关键词搜索：匹配 title
        keyword = ids_in.lower()
        targets = [s for s in all_items if keyword in s["title"].lower()]
        if targets:
            log(f"🔍 找到 {len(targets)} 个匹配 '{ids_in}' 的项目：")
            for s in targets:
                tag = "🛒" if s.get("is_goods") else "🎫"
                log(f"   {tag} {s['id']} - {s['title']} ({s['start_s']})")
            confirm = input("确认抢这些项目？(y/n): ").strip().lower()
            if confirm != "y":
                log("已取消")
                return

    if not targets:
        log("⚠️  没有符合条件的项目")
        return

    # ---------- 按 start 分组 ----------
    groups = group_by_start(targets)

    # 预热所有目标 id 的连接
    id_to_url = {s["id"]: s["url"] for _ts, g in groups for s in g}
    preheat_ids(list(id_to_url.keys()), id_to_url)

    for start_ts, ses_lst in groups:      # 按时间先后抢
        fire_group(ses_lst)

    log("🚩 抢票流程结束")


def set_params():
    try:
        global TICKET_DAYS
        
        log(f"当前购票日期: {TICKET_DAYS} (1=11号, 2=12号, 3=13号)")
        days_in = input("修改购票日期（如 1,2,3 或直接回车跳过）: ").strip()
        if days_in:
            TICKET_DAYS = [int(x) for x in days_in.split(",") if x.strip().isdigit() and 1 <= int(x) <= 3]
            log(f"购票日期已更新: {TICKET_DAYS}")
        
        a = float(input(f"提前秒[{CFG['ahead_sec']}]: ") or CFG['ahead_sec'])
        th = int(input(f"并发线程[{CFG['threads']}]: ") or CFG['threads'])
        rpt = int(input(f"每线程请求数[{CFG.get('requests_per_thread', 2)}]: ") or CFG.get('requests_per_thread', 2))
        jit = int(input(f"时间抖动ms[{CFG.get('time_jitter_ms', 15)}]: ") or CFG.get('time_jitter_ms', 15))
        CFG.update(ahead_sec=a, threads=th, requests_per_thread=rpt, time_jitter_ms=jit)
        log("参数已更新:", CFG)
    except Exception as e:
        log("输入有误:", e)

# ────────────────────  8. 菜单循环  ────────────────────────
MENU = """
    ========== BW 抢票助手（优化版）==========
    1) 检查 Cookie 是否有效
    2) 查看全部场次（活动+商品）
    3) 等待并自动抢票（支持 id/auto/关键词）
    4) 设置参数（购票日期 / 提前秒数 / 并发）
    5) 切换 dry-run (当前: {dry})
    6) 并发压测
    0) 退出
    
    当前配置: 线程={threads} 每线程请求={req_per_th} 时间抖动=±{jitter}ms
    请选择："""

# ────────────────────  10. 并发压测  ─────────────────────
def pressure_test():
    """在 dry-run 模式下压测不同线程数，并额外统计 HTTP／业务 code 分布。"""

    levels = [8, 16, 32, 48, 64]
    log("🧪 开始压测… (仅本地统计延迟，不会实际预约)")

    for th in levels:
        lat         = []   # 单次请求耗时 (s)
        http_stats  = []   # HTTP status list
        biz_codes   = []   # "code" 字段统计

        def _w():
            """工作线程：直接发 POST 以观察真实 HTTP 行为"""
            payload = (f"csrf={BILI_JCT}&reserve_id=0&reserve_type={CFG['reserve_type']}").encode()
            t0 = time.perf_counter()
            try:
                resp = _http_post(
                    RESV_URL,
                    payload,
                    {"content-type": "application/x-www-form-urlencoded"}
                )
                http_stats.append(resp.status_code)

                # 尽量简易提取业务 code，失败忽略
                m = _CODE_RE.search(resp.content)
                if m:
                    biz_codes.append(int(m.group(1)))
            except Exception:
                http_stats.append(-1)   # -1 表示请求异常
            finally:
                lat.append(time.perf_counter() - t0)

        threads = [threading.Thread(target=_w) for _ in range(th)]
        t_begin = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        cost = time.perf_counter() - t_begin

        # -------- 汇总统计 --------
        mean_ms = statistics.mean(lat) * 1000
        p90_ms  = sorted(lat)[int(0.9 * len(lat) - 1)] * 1000

        from collections import Counter
        http_cnt = Counter(http_stats)
        biz_cnt  = Counter(biz_codes)

        http_str = ", ".join(f"{k}:{v}" for k, v in sorted(http_cnt.items()))
        biz_str  = ", ".join(f"{k}:{v}" for k, v in sorted(biz_cnt.items())) or "--"

        log(f"线程 {th:>2d}  耗时 {cost:.2f}s  平均 {mean_ms:.1f} ms  P90 {p90_ms:.1f} ms  "
            f"HTTP({http_str})  code({biz_str})")

    log("🧪 压测结束，可根据 HTTP/业务 code 分布判断服务器是否接受高并发请求，并据此挑选合适的 threads")

def main():
    while True:
        try:
            choice = input(MENU.format(
                dry=CFG["dry_run"],
                threads=CFG["threads"],
                req_per_th=CFG.get("requests_per_thread", 2),
                jitter=CFG.get("time_jitter_ms", 15)
            )).strip()
            if choice == "1":
                check_cookie()
            elif choice == "2":
                show_today()
            elif choice == "3":
                grab_flow()
            elif choice == "4":
                set_params()
            elif choice == "5":
                CFG["dry_run"] = not CFG["dry_run"]
                log(f"dry-run 已切换为 {CFG['dry_run']}")
            elif choice == "6":
                pressure_test()
            elif choice == "0":
                log("👋 退出程序，再见！")
                break
            else:
                print("请输入 0-6 之间的选项\n")
        except KeyboardInterrupt:
            print("\n^C 中断，程序退出")
            break
        except Exception as e:
            log("⚠️ 运行时异常:", e)

# ────────────────────  9. 入口  ────────────────────────────
if __name__ == "__main__":
    main()
