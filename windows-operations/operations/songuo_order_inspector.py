#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爱绿妈妈（微信小程序）后台 · 每日订单巡检脚本
=================================================

站点：https://songguofood.songguobaobao.com/#/homePage/homePage
后端：https://songguofood.songguobaobao.com/admin   （Vue + Element UI 管理后台）

巡检内容（对应后台功能板块）
    首页概览   POST /admin/homePage/queryOrderCount        待处理订单计数
    订单管理   POST /admin/order/queryPickUpOrderMessage     商城自取订单
               POST /admin/order/queryExpressOrderMessage    商城快递订单
               POST /admin/order/queryPickUpScopeOrderMessage 积分自取订单
               POST /admin/order/queryExpressScopeOrderMessage 积分快递订单
               POST /admin/order/queryReturnOrderMessage     退款/退货订单
    字典       GET  /admin/ad/queryDicList?dicType=ORDER_STATUS  订单状态字典

输出
    控制台报告 + Markdown 巡检报告 + 订单明细 CSV + 运行日志 + 上次快照

运行
    python songuo_order_inspector.py                 # 巡检今天
    python songuo_order_inspector.py --date yesterday
    python songuo_order_inspector.py --date 2026-10-27
    python songuo_order_inspector.py --peek          # 只打印接口真实字段（首次接入用）
    python songuo_order_inspector.py --check         # 只校验账号密码能否登录

退出码
    0 正常    1 有告警    2 巡检失败（登录/网络/配置）    3 首次运行，需要填写 config.ini
"""

from __future__ import annotations

import argparse
import configparser
import csv
import json
import logging
import os
import re
import socket
import ssl
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.ini"

SITE_ORIGIN = "https://songguofood.songguobaobao.com"
DEFAULT_BASE_URL = SITE_ORIGIN + "/admin"
TOKEN_HEADER = "X-Mymall-Admin-Token"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
)

EXIT_OK = 0
EXIT_ALERT = 1
EXIT_FATAL = 2
EXIT_SETUP = 3


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------

CONFIG_TEMPLATE = """\
# ============================================================================
# 爱绿妈妈后台 · 每日订单巡检配置
#   首次运行本脚本会自动生成此文件，请填写 [account] 里的手机号与密码。
#   密码只保存在本机这个文件里，请勿把本文件发给他人。
# ============================================================================

[account]
# 后台登录手机号（登录页“手机号”输入框）
phone =
# 后台登录密码
password =

[site]
# 接口根地址（前端 axios baseURL），一般不需要改
base_url = https://songguofood.songguobaobao.com/admin
# 后台首页，用于 Referer / Origin，一般不需要改
home_page = https://songguofood.songguobaobao.com/
# 请求超时（秒）
timeout = 30
# 证书校验：正常填 true；公司网络有代理劫持导致 SSL 报错时才改成 false
verify_ssl = true

[inspection]
# 巡检日期：today（今天）/ yesterday（昨天）/ 2026-10-27（指定日期）
date = today
# 单页抓取条数（接口 limit）
page_size = 100
# 最多翻页数，防止接口异常时死循环
max_pages = 50
# 判定为“待处理”的状态关键字（逗号分隔，按状态名称模糊匹配）
pending_keywords = 待取货,待发货,待收货,待审核,待退款,退款中,退款申请,退货申请,申请中,待处理
# 需要重点巡检的板块：1 开启 0 跳过，对应订单管理下的五个页签
scope_pickup_mall = 1
scope_express_mall = 1
scope_pickup_scope = 1
scope_express_scope = 1
scope_return = 1

[thresholds]
# 待取货（商城自取）超过多少笔告警
pending_pickup_max = 5
# 待发货（商城快递）超过多少笔告警
pending_express_max = 5
# 待处理（积分订单）超过多少笔告警
pending_scope_max = 5
# 待处理退款/退货超过多少笔告警
refund_pending_max = 0
# 待处理订单停留超过多少小时告警（0 表示不检查）
stale_hours = 24
# 单笔订单金额超过多少元告警（0 表示不检查）
order_amount_max = 1000

[output]
# 报告目录（相对本脚本所在目录；也可写绝对路径）
dir = 巡检报告
# 是否导出订单明细 CSV
csv = true
# 报告与日志保留天数（0 表示不清理）
keep_days = 180
# 日志级别：DEBUG / INFO / WARNING
log_level = INFO
"""


@dataclass
class Config:
    phone: str = ""
    password: str = ""
    base_url: str = DEFAULT_BASE_URL
    home_page: str = SITE_ORIGIN + "/"
    timeout: int = 30
    verify_ssl: bool = True
    date_spec: str = "today"
    page_size: int = 100
    max_pages: int = 50
    pending_keywords: List[str] = field(default_factory=list)
    enabled_scopes: Dict[str, bool] = field(default_factory=dict)
    pending_pickup_max: int = 5
    pending_express_max: int = 5
    pending_scope_max: int = 5
    refund_pending_max: int = 0
    stale_hours: int = 24
    order_amount_max: float = 1000.0
    output_dir: Path = SCRIPT_DIR / "巡检报告"
    export_csv: bool = True
    keep_days: int = 180
    log_level: str = "INFO"


def _bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def ensure_config() -> bool:
    """配置文件不存在时写出模板，返回 True 表示需要用户先填写。"""
    if CONFIG_PATH.exists():
        return False
    CONFIG_PATH.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return True


def load_config() -> Config:
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    parser.optionxform = str  # 保留键名大小写
    parser.read(CONFIG_PATH, encoding="utf-8")

    def get(section: str, option: str, default: str = "") -> str:
        return parser.get(section, option, fallback=default).strip()

    cfg = Config()
    # 环境变量优先，便于以后接到任务计划里而不把密码写进文件
    cfg.phone = os.environ.get("SONGUO_PHONE") or get("account", "phone")
    cfg.password = os.environ.get("SONGUO_PASSWORD") or get("account", "password")

    cfg.base_url = (get("site", "base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
    cfg.home_page = get("site", "home_page", SITE_ORIGIN + "/") or (SITE_ORIGIN + "/")
    cfg.timeout = _int(get("site", "timeout", "30"), 30)
    cfg.verify_ssl = _bool(get("site", "verify_ssl", "true"), True)

    cfg.date_spec = get("inspection", "date", "today") or "today"
    cfg.page_size = max(1, _int(get("inspection", "page_size", "100"), 100))
    cfg.max_pages = max(1, _int(get("inspection", "max_pages", "50"), 50))
    keywords = get(
        "inspection",
        "pending_keywords",
        "待取货,待发货,待收货,待审核,待退款,退款中,申请中",
    )
    cfg.pending_keywords = [k.strip() for k in re.split(r"[,，;；]", keywords) if k.strip()]
    cfg.enabled_scopes = {
        s.key: _bool(get("inspection", "scope_" + s.key, "1"), True) for s in SCOPES
    }

    cfg.pending_pickup_max = _int(get("thresholds", "pending_pickup_max", "5"), 5)
    cfg.pending_express_max = _int(get("thresholds", "pending_express_max", "5"), 5)
    cfg.pending_scope_max = _int(get("thresholds", "pending_scope_max", "5"), 5)
    cfg.refund_pending_max = _int(get("thresholds", "refund_pending_max", "0"), 0)
    cfg.stale_hours = _int(get("thresholds", "stale_hours", "24"), 24)
    cfg.order_amount_max = _float(get("thresholds", "order_amount_max", "1000"), 1000.0)

    out = get("output", "dir", "巡检报告") or "巡检报告"
    out_path = Path(out)
    if not out_path.is_absolute():
        out_path = SCRIPT_DIR / out_path
    cfg.output_dir = out_path
    cfg.export_csv = _bool(get("output", "csv", "true"), True)
    cfg.keep_days = _int(get("output", "keep_days", "180"), 180)
    cfg.log_level = get("output", "log_level", "INFO").upper() or "INFO"
    return cfg


# ----------------------------------------------------------------------------
# 巡检板块定义（对应后台“订单管理”下的五个页签）
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    key: str
    title: str
    path: str
    is_convert: str
    order_type: int
    kind: str  # pickup / express / refund
    pending_threshold_key: str = ""

    @property
    def amount_label(self) -> str:
        return "退款金额" if self.kind == "refund" else "订单金额"


SCOPES: Tuple[Scope, ...] = (
    Scope("pickup_mall", "商城自取订单", "/order/queryPickUpOrderMessage", "0", 0, "pickup",
          "pending_pickup_max"),
    Scope("express_mall", "商城快递订单", "/order/queryExpressOrderMessage", "0", 1, "express",
          "pending_express_max"),
    Scope("pickup_scope", "积分自取订单", "/order/queryPickUpScopeOrderMessage", "1", 0, "pickup",
          "pending_scope_max"),
    Scope("express_scope", "积分快递订单", "/order/queryExpressScopeOrderMessage", "1", 1, "express",
          "pending_scope_max"),
    Scope("return", "退款/退货订单", "/order/queryReturnOrderMessage", "1", 0, "refund",
          "refund_pending_max"),
)

SCOPE_BY_KEY = {s.key: s for s in SCOPES}

# 这些状态不计入“有效金额”
CANCELLED_KEYWORDS = ("已取消", "取消", "已关闭", "关闭", "已退款", "退款完成", "退款成功")


# ----------------------------------------------------------------------------
# HTTP 客户端
# ----------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, message: str, errno: Optional[int] = None):
        super().__init__(message)
        self.errno = errno


class AuthError(ApiError):
    """errno=501：登录态失效或未登录。"""


def _pick(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """按顺序取第一个存在且非空的字段（兼容后端字段命名差异）。"""
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data:
            value = data[key]
            if value is not None and value != "":
                return value
    return default


def _as_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").replace("¥", "").strip())
    except ValueError:
        return 0.0


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class Client:
    """后台接口客户端：urllib 实现，只用标准库，免安装依赖。"""

    def __init__(self, cfg: Config, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger
        self.token: Optional[str] = None
        self._ssl_ctx = ssl.create_default_context()
        if not cfg.verify_ssl:
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE
        origin = self._origin()
        self._headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": USER_AGENT,
            "Origin": origin,
            "Referer": cfg.home_page or (origin + "/"),
        }

    def _origin(self) -> str:
        parsed = urlparse.urlsplit(self.cfg.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    # -- 底层请求 ---------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        retries: int = 2,
    ) -> Any:
        url = self.cfg.base_url + path
        if params:
            query = urlparse.urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                url += ("&" if "?" in url else "?") + query

        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")

        headers = dict(self._headers)
        if self.token:
            headers[TOKEN_HEADER] = self.token

        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            req = urlrequest.Request(url, data=body, headers=headers, method=method.upper())
            started = time.time()
            try:
                with urlrequest.urlopen(req, timeout=self.cfg.timeout, context=self._ssl_ctx) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                break
            except urlerror.HTTPError as exc:  # 4xx/5xx 且带了响应体
                raw = exc.read().decode("utf-8", errors="replace")
                self.log.warning("HTTP %s %s %s", exc.code, method.upper(), path)
                break
            except (urlerror.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
                last_error = exc
                self.log.warning(
                    "请求失败（第 %d 次）：%s %s -> %s", attempt + 1, method.upper(), path, exc
                )
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                continue
        else:
            raise ApiError(f"网络请求失败：{last_error}")

        elapsed = (time.time() - started) * 1000
        self.log.debug("%s %s %.0fms %d bytes", method.upper(), path, elapsed, len(raw))

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            snippet = raw.strip().replace("\n", " ")[:180]
            raise ApiError(f"接口返回的不是 JSON（{path}）：{snippet}")

        if not isinstance(payload, dict):
            raise ApiError(f"接口返回结构异常（{path}）：{str(payload)[:180]}")

        errno = _as_int(payload.get("errno"), 0)
        errmsg = str(payload.get("errmsg") or payload.get("message") or "").strip()
        if errno == 501:
            raise AuthError(errmsg or "登录态失效（errno=501）", errno)
        if errno != 0:
            raise ApiError(f"接口报错（errno={errno}）：{errmsg or '未知错误'}", errno)
        return payload.get("data")

    # -- 登录 -------------------------------------------------------------
    def login(self, phone: str, password: str) -> str:
        data = self.request("POST", "/auth/login", data={"username": phone, "password": password})
        token = ""
        if isinstance(data, str):
            token = data.strip()
        elif isinstance(data, dict):
            token = str(_pick(data, "token", "accessToken", "access_token", default="")).strip()
        if not token:
            raise ApiError("登录成功但未取到 token，请用 --peek 查看 /auth/login 返回结构")
        self.token = token
        self.log.info("登录成功：%s", mask_phone(phone))
        return token

    # -- 业务接口 ---------------------------------------------------------
    def homepage_stats(self) -> Dict[str, Any]:
        data = self.request("POST", "/homePage/queryOrderCount", data={})
        return data if isinstance(data, dict) else {}

    def order_status_dict(self) -> Dict[str, str]:
        """订单状态字典，用于把状态码翻译成中文。失败不影响巡检。"""
        try:
            data = self.request("GET", "/ad/queryDicList", params={"dicType": "ORDER_STATUS"})
        except ApiError as exc:
            self.log.warning("订单状态字典获取失败：%s", exc)
            return {}
        rows = _first_list(data) if not isinstance(data, list) else data
        mapping: Dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = _pick(row, "code", "value", "dicValue", "dicCode", "key", "id")
            name = _pick(row, "name", "label", "dicName", "dicLabel", "desc", "valueName")
            if code is not None and name:
                mapping[str(code)] = str(name)
        self.log.info("订单状态字典 %d 项：%s", len(mapping),
                      "、".join(sorted(set(mapping.values()))[:12]))
        return mapping


def _first_list(data: Any) -> List[Any]:
    """从返回体里找出订单数组（兼容 items/list/records/rows 等命名）。"""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("items", "list", "records", "rows", "data", "content", "result"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    # 兜底：取第一个“元素都是字典”的数组字段
    for value in data.values():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            return value
    return []


def mask_phone(phone: str) -> str:
    if len(phone) >= 7:
        return phone[:3] + "****" + phone[-4:]
    return phone or "(未配置)"


# ----------------------------------------------------------------------------
# 时间与文本工具
# ----------------------------------------------------------------------------

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M",
)


def parse_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # 毫秒
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return parse_datetime(int(text))
    text = text.replace("T", " ").split(".")[0]
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def display_width(text: str) -> int:
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    text = "" if text is None else str(text)
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def money(value: Any) -> str:
    return f"{_as_float(value):,.2f}"


# ----------------------------------------------------------------------------
# 巡检逻辑
# ----------------------------------------------------------------------------


@dataclass
class OrderRecord:
    scope_key: str
    scope_title: str
    raw: Dict[str, Any]
    status_name: str
    created_at: Optional[datetime]
    amount: float

    @property
    def order_sn(self) -> str:
        return str(_pick(self.raw, "orderSn", "orderNo", "orderNumber", "sn", default="") or "")

    @property
    def org_name(self) -> str:
        return str(_pick(self.raw, "orgName", "companyName", "shopName", "storeName",
                         "organizationName", default="") or "")


@dataclass
class ScopeResult:
    scope: Scope
    summary: Dict[str, Any] = field(default_factory=dict)
    orders: List[OrderRecord] = field(default_factory=list)
    total: int = 0
    raw_count: int = 0
    error: str = ""
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped

    @property
    def count(self) -> int:
        return len(self.orders)

    @property
    def amount(self) -> float:
        return round(sum(o.amount for o in self.orders), 2)

    @property
    def effective_amount(self) -> float:
        """金额合计（剔除已取消/已关闭订单，避免虚高）。"""
        return round(
            sum(o.amount for o in self.orders
                if not any(k in (o.status_name or "") for k in CANCELLED_KEYWORDS)),
            2,
        )

    def status_counts(self) -> List[Tuple[str, int]]:
        bucket: Dict[str, int] = {}
        for order in self.orders:
            bucket[order.status_name or "未知状态"] = bucket.get(order.status_name or "未知状态", 0) + 1
        return sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))

    def pending_orders(self, keywords: List[str]) -> List[OrderRecord]:
        result = []
        for order in self.orders:
            name = order.status_name or ""
            if any(kw and kw in name for kw in keywords):
                result.append(order)
        return result


@dataclass
class Alert:
    level: str  # 严重 / 提醒 / 提示
    message: str


def fetch_scope(client: Client, cfg: Config, scope: Scope, log: logging.Logger) -> ScopeResult:
    result = ScopeResult(scope=scope)
    page = 1
    per_page = cfg.page_size
    seen: set = set()
    while page <= cfg.max_pages:
        payload = {
            "page": page,
            "limit": per_page,
            "isConvert": scope.is_convert,
            "orderType": scope.order_type,
            "orderSn": "",
            "orderDate": [],
            "orderStatus": "",
        }
        data = client.request("POST", scope.path, data=payload)
        if not isinstance(data, dict):
            raise ApiError(f"{scope.title} 返回结构异常：{str(data)[:120]}")
        if page == 1:
            result.summary = {
                k: v for k, v in data.items() if not isinstance(v, (list, dict))
            }
            result.total = _as_int(_pick(data, "total", "count", "totalCount", default=0))
        rows = _first_list(data)
        if not rows:
            break

        new_rows = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(_pick(row, "id", "orderSn", "orderNo", "orderId", default="")) or ""
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            result.orders.append(
                OrderRecord(
                    scope_key=scope.key,
                    scope_title=scope.title,
                    raw=row,
                    status_name="",
                    created_at=None,
                    amount=0.0,
                )
            )
            new_rows += 1

        if new_rows == 0:  # 服务端忽略了 page 参数，继续翻页只会拿到重复数据
            log.warning("%s：第 %d 页没有新数据，停止翻页", scope.title, page)
            break
        if result.total and len(result.orders) >= result.total:
            break
        if len(rows) < per_page:
            # 服务端单页上限低于配置值：按实际返回条数继续，避免漏数据
            if result.total and len(result.orders) < result.total and len(rows) > 0:
                per_page = len(rows)
                page = len(result.orders) // per_page + 1
                log.info("%s：服务端单页上限 %d，已自动按该值翻页", scope.title, per_page)
                continue
            break
        page += 1
    result.raw_count = len(result.orders)
    if not result.total:
        result.total = result.raw_count
    log.info("%s：%d 笔（接口 total=%s）", scope.title, result.raw_count, result.total)
    return result


def enrich_orders(
    result: ScopeResult,
    status_map: Dict[str, str],
    keywords: List[str],
    target_date: Optional[datetime],
    date_label: str,
    log: logging.Logger,
) -> None:
    """补全状态名/时间/金额，并按巡检日期过滤。"""
    kept: List[OrderRecord] = []
    unknown_date = 0
    for order in result.orders:
        raw = order.raw
        status_name = str(_pick(raw, "orderStatusName", "orderStatusText", "statusName",
                                "statusText", "orderStateName", "orderStatusStr", default="") or "")
        if not status_name:
            code = _pick(raw, "orderStatus", "status", "orderState", "orderStatusCode")
            if code is not None and str(code) in status_map:
                status_name = status_map[str(code)]
            elif code is not None:
                status_name = f"状态{code}"
        order.status_name = status_name

        order.created_at = parse_datetime(
            _pick(raw, "addTime", "createTime", "orderTime", "createDate", "addDate", "orderAddTime")
        )
        order.amount = _as_float(
            _pick(raw, "totalPrice", "orderTotalPrice", "payPrice", "actualPrice",
                  "orderAmount", "amount", "price")
        )

        if target_date is None or order.created_at is None:
            kept.append(order)
            if order.created_at is None:
                unknown_date += 1
            continue
        if order.created_at.date() == target_date.date():
            kept.append(order)

    if unknown_date:
        log.debug("%s：%d 笔订单没有可解析的下单时间，已全部保留", result.scope.title, unknown_date)
    result.orders = kept
    if result.total < len(kept):
        result.total = len(kept)


def resolve_date(spec: str) -> Tuple[Optional[datetime], str]:
    spec = (spec or "today").strip().lower()
    today = datetime.now()
    if spec in ("today", "今天", ""):
        return today, "今天"
    if spec in ("yesterday", "昨天"):
        return today - timedelta(days=1), "昨天"
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(spec, fmt), spec
        except ValueError:
            continue
    raise SystemExit(f"无法识别的巡检日期：{spec}（可用 today / yesterday / 2026-10-27）")


def build_alerts(
    results: List[ScopeResult],
    cfg: Config,
    stats: Dict[str, Any],
    now: datetime,
) -> List[Alert]:
    alerts: List[Alert] = []
    thresholds = {
        "pending_pickup_max": cfg.pending_pickup_max,
        "pending_express_max": cfg.pending_express_max,
        "pending_scope_max": cfg.pending_scope_max,
        "refund_pending_max": cfg.refund_pending_max,
    }

    for result in results:
        scope = result.scope
        if result.error:
            alerts.append(Alert("严重", f"{scope.title}：接口访问失败 —— {result.error}"))
            continue
        if result.skipped:
            continue

        pending = result.pending_orders(cfg.pending_keywords)
        limit = thresholds.get(scope.pending_threshold_key, 999)

        # 字段自检：接口有数据但解析不出时间/状态时，提示用 --peek 校准字段名
        if result.raw_count > 0 and result.count == 0:
            alerts.append(
                Alert("严重",
                      f"{scope.title}：接口返回 {result.raw_count} 笔订单，但按 {cfg.date_spec} 过滤后为 0，"
                      f"请运行 python songuo_order_inspector.py --peek 核对下单时间字段")
            )
        if result.count > 0:
            unrecognized = sum(
                1 for o in result.orders
                if not o.status_name or re.fullmatch(r"状态\d+", o.status_name or "")
            )
            if unrecognized == len(result.orders):
                alerts.append(
                    Alert("提醒",
                          f"{scope.title}：{unrecognized} 笔订单都无法识别状态字段，"
                          f"待处理统计可能不准，请运行 --peek 核对状态字段")
                )

        if len(pending) > limit:
            detail = "、".join(f"{name} {count} 笔" for name, count in pending_status_breakdown(pending))
            alerts.append(
                Alert("严重" if scope.kind == "refund" else "提醒",
                      f"{scope.title}：待处理 {len(pending)} 笔，超过阈值 {limit}（{detail}）")
            )

        if cfg.stale_hours > 0 and pending:
            stamps = [o.created_at for o in pending if o.created_at]
            if stamps:
                oldest = min(stamps)
                age_hours = (now - oldest).total_seconds() / 3600
                if age_hours >= cfg.stale_hours:
                    alerts.append(
                        Alert("提醒",
                              f"{scope.title}：最早一笔待处理订单已停留 {age_hours:.1f} 小时"
                              f"（下单时间 {oldest:%Y-%m-%d %H:%M}，单号 {pending_order_sn(pending, oldest)}）")
                    )

        if cfg.order_amount_max > 0:
            big = [o for o in result.orders if o.amount >= cfg.order_amount_max]
            for order in sorted(big, key=lambda o: -o.amount)[:3]:
                alerts.append(
                    Alert("提示",
                          f"{scope.title}：大额订单 ¥{order.amount:,.2f}"
                          f"（{order.order_sn or '无单号'}，{order.status_name or '状态未知'}）")
                )

    # 首页待处理计数与列表交叉核对
    item_stats = stats.get("itemStatis") if isinstance(stats.get("itemStatis"), dict) else {}
    if item_stats:
        pairs = [
            ("pickup_mall", "pickOrderCount", "商城自取待处理"),
            ("express_mall", "expressOrderCount", "商城快递待发货"),
            ("pickup_scope", "pickScopeCount", "积分自取待处理"),
            ("express_scope", "expressScopeCount", "积分快递待处理"),
        ]
        for scope_key, stat_key, label in pairs:
            value = item_stats.get(stat_key)
            if value is None:
                continue
            count = _as_int(value)
            result = next((r for r in results if r.scope.key == scope_key and r.ok), None)
            if result is not None and count > result.count:
                alerts.append(
                    Alert("提示",
                          f"首页概览「{label}」显示 {count} 笔，本日订单明细抓到 {result.count} 笔，"
                          f"差额多为历史未处理订单，请到后台核对")
                )
    return alerts


def pending_status_breakdown(orders: List[OrderRecord]) -> List[Tuple[str, int]]:
    bucket: Dict[str, int] = {}
    for order in orders:
        name = order.status_name or "未知状态"
        bucket[name] = bucket.get(name, 0) + 1
    return sorted(bucket.items(), key=lambda kv: -kv[1])


def pending_order_sn(orders: List[OrderRecord], stamp: datetime) -> str:
    for order in orders:
        if order.created_at == stamp and order.order_sn:
            return order.order_sn
    return "无"


# ----------------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------------


def print_overview(results: List[ScopeResult], stats: Dict[str, Any], cfg: Config,
                   date_label: str, target: Optional[datetime]) -> None:
    print()
    print("=" * 78)
    print(f"  爱绿妈妈后台 · 订单巡检   {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  巡检对象：{target:%Y-%m-%d}（{date_label}）   "
          f"站点：{urlparse.urlsplit(SITE_ORIGIN).netloc}")
    print("=" * 78)

    columns = [
        ("板块", 16, "left"), ("订单数", 7, "right"), ("待处理", 7, "right"),
        ("有效金额", 12, "right"), ("退款中", 7, "right"), ("结果", 10, "left"),
    ]
    line = "  ".join(pad(t, w, a) for t, w, a in columns)
    print(line)
    print("-" * display_width(line))
    for result in results:
        if result.skipped:
            row = [result.scope.title, "-", "-", "-", "-", "配置中关闭"]
        elif result.error:
            row = [result.scope.title, "-", "-", "-", "-", "接口失败"]
        else:
            pending = result.pending_orders(cfg.pending_keywords)
            refunding = sum(
                1 for o in result.orders
                if any(k in (o.status_name or "") for k in ("退款", "退货"))
                and not any(k in (o.status_name or "") for k in ("完成", "成功", "关闭"))
            )
            row = [
                result.scope.title,
                str(result.count),
                str(len(pending)),
                money(result.effective_amount),
                str(refunding) if result.scope.kind != "refund" else "-",
                "正常",
            ]
        print("  ".join(pad(value, w, a) for (_, w, a), value in zip(columns, row)))

    for result in results:
        if result.error:
            print(f"  [!] {result.scope.title} 接口失败：{result.error}")

    total_orders = sum(r.count for r in results if r.ok)
    total_amount = sum(r.effective_amount for r in results if r.ok)
    print("-" * display_width(line))
    print(f"  合计：{total_orders} 笔订单，有效金额 ¥{total_amount:,.2f}（已剔除取消/关闭订单）")

    item_stats = stats.get("itemStatis") if isinstance(stats.get("itemStatis"), dict) else {}
    if item_stats:
        print()
        print("  首页概览待处理计数：")
        for label, key in (
            ("商城自取待取货", "pickOrderCount"),
            ("商城快递待发货", "expressOrderCount"),
            ("积分自取待处理", "pickScopeCount"),
            ("积分快递待处理", "expressScopeCount"),
        ):
            if key in item_stats:
                print(f"    - {label}：{item_stats.get(key)}")


def print_status_breakdown(results: List[ScopeResult], cfg: Config) -> None:
    for result in results:
        if not result.ok or not result.orders:
            continue
        print()
        print(f"  [{result.scope.title}] 状态分布")
        for name, count in result.status_counts():
            flag = "  ← 待处理" if any(kw and kw in name for kw in cfg.pending_keywords) else ""
            print(f"    - {pad(name, 20)} {count:>3} 笔{flag}")


def print_alerts(alerts: List[Alert]) -> None:
    print()
    if not alerts:
        print("  [OK] 未发现异常，所有待处理订单均在阈值内。")
        return
    print(f"  [!] 发现 {len(alerts)} 条需要关注的事项：")
    order = {"严重": 0, "提醒": 1, "提示": 2}
    for alert in sorted(alerts, key=lambda a: order.get(a.level, 9)):
        print(f"    [{alert.level}] {alert.message}")


def write_markdown(
    path: Path,
    results: List[ScopeResult],
    alerts: List[Alert],
    stats: Dict[str, Any],
    cfg: Config,
    target: Optional[datetime],
    date_label: str,
    elapsed: float,
    diff: Dict[str, Any],
) -> None:
    lines: List[str] = []
    now = datetime.now()
    section = {"n": 0}
    numerals = "一二三四五六七八九十"

    def heading(title: str) -> None:
        idx = section["n"]
        section["n"] += 1
        num = numerals[idx] if idx < len(numerals) else str(idx + 1)
        lines.append(f"## {num}、{title}")
        lines.append("")

    lines.append(f"# 订单每日巡检报告 · {now:%Y-%m-%d}")
    lines.append("")
    lines.append(f"- 站点：爱绿妈妈后台（{urlparse.urlsplit(SITE_ORIGIN).netloc}）")
    lines.append(f"- 巡检时间：{now:%Y-%m-%d %H:%M:%S}（耗时 {elapsed:.1f}s）")
    lines.append(f"- 巡检日期：{target:%Y-%m-%d}（{date_label}）" if target else f"- 巡检日期：{date_label}")
    total_orders = sum(r.count for r in results if r.ok)
    total_amount = sum(r.effective_amount for r in results if r.ok)
    lines.append(f"- 当日订单：{total_orders} 笔，有效金额 ¥{total_amount:,.2f}（已剔除取消/关闭订单）")
    lines.append("")

    heading("巡检结论")
    if not alerts:
        lines.append("未发现异常，各板块待处理订单均在阈值内。")
    else:
        lines.append(f"共 {len(alerts)} 条需要关注：")
        lines.append("")
        for alert in alerts:
            lines.append(f"- **[{alert.level}]** {alert.message}")
    lines.append("")

    heading("板块概览")
    lines.append("| 板块 | 订单数 | 待处理 | 有效金额 | 接口 total | 状态 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | --- |")
    for result in results:
        if result.skipped:
            lines.append(f"| {result.scope.title} | - | - | - | - | 跳过 |")
        elif result.error:
            lines.append(f"| {result.scope.title} | - | - | - | - | 接口失败：{result.error} |")
        else:
            pending = len(result.pending_orders(cfg.pending_keywords))
            lines.append(
                f"| {result.scope.title} | {result.count} | {pending} | "
                f"¥{result.effective_amount:,.2f} | {result.total} | 正常 |"
            )
    lines.append("")

    item_stats = stats.get("itemStatis") if isinstance(stats.get("itemStatis"), dict) else {}
    if item_stats:
        heading("首页概览待处理计数")
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | ---: |")
        for label, key in (
            ("商城自取待取货", "pickOrderCount"),
            ("商城快递待发货", "expressOrderCount"),
            ("积分自取待处理", "pickScopeCount"),
            ("积分快递待处理", "expressScopeCount"),
        ):
            if key in item_stats:
                lines.append(f"| {label} | {item_stats.get(key)} |")
        lines.append("")

    if diff and diff.get("compared_date"):
        heading("与上次巡检对比")
        lines.append(f"- 对比基准：{diff.get('compared_date')}")
        lines.append(f"- 新增订单：{diff.get('new_orders', 0)} 笔")
        if diff.get("new_order_sns"):
            lines.append(f"- 新单号：{'、'.join(diff['new_order_sns'][:20])}")
        lines.append("")

    heading("状态分布")
    lines.append("")
    for result in results:
        if not result.ok or not result.orders:
            continue
        lines.append(f"### {result.scope.title}")
        lines.append("")
        lines.append("| 状态 | 笔数 |")
        lines.append("| --- | ---: |")
        for name, count in result.status_counts():
            lines.append(f"| {name} | {count} |")
        lines.append("")

    org_rows: Dict[str, Dict[str, float]] = {}
    for result in results:
        if not result.ok:
            continue
        for order in result.orders:
            key = order.org_name or "（未标注门店）"
            bucket = org_rows.setdefault(key, {"count": 0.0, "amount": 0.0})
            bucket["count"] += 1
            bucket["amount"] += order.amount
    if org_rows:
        heading("门店分布")
        lines.append("| 门店 | 订单数 | 金额合计 |")
        lines.append("| --- | ---: | ---: |")
        for name, bucket in sorted(org_rows.items(), key=lambda kv: -kv[1]["count"]):
            lines.append(f"| {name} | {int(bucket['count'])} | ¥{bucket['amount']:,.2f} |")
        lines.append("")

    pending_all: List[OrderRecord] = []
    for result in results:
        if result.ok:
            pending_all.extend(result.pending_orders(cfg.pending_keywords))
    if pending_all:
        heading("待处理订单明细")
        lines.append("")
        lines.append("| 板块 | 单号 | 下单时间 | 状态 | 金额 | 门店 |")
        lines.append("| --- | --- | --- | --- | ---: | --- |")
        for order in sorted(pending_all, key=lambda o: o.created_at or datetime.max)[:80]:
            created = f"{order.created_at:%Y-%m-%d %H:%M}" if order.created_at else "-"
            lines.append(
                f"| {order.scope_title} | {order.order_sn or '-'} | {created} | "
                f"{order.status_name or '-'} | ¥{order.amount:,.2f} | {order.org_name or '-'} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*本报告由 songuo_order_inspector.py 自动生成，数据来自后台订单接口实时查询。*")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(path: Path, results: List[ScopeResult]) -> int:
    rows = 0
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "板块", "订单号", "下单时间", "订单状态", "金额", "门店",
            "下单人", "手机号", "取货码", "收货人", "收货电话", "收货地址", "商品",
        ])
        for result in results:
            if not result.ok:
                continue
            for order in result.orders:
                raw = order.raw
                goods = raw.get("goodsList") if isinstance(raw.get("goodsList"), list) else []
                goods_text = " / ".join(
                    f"{_pick(g, 'goodsName', 'name', default='商品')}×{_pick(g, 'number', 'num', 'count', default=1)}"
                    for g in goods if isinstance(g, dict)
                )
                writer.writerow([
                    order.scope_title,
                    order.order_sn,
                    f"{order.created_at:%Y-%m-%d %H:%M:%S}" if order.created_at else "",
                    order.status_name,
                    f"{order.amount:.2f}",
                    order.org_name,
                    _pick(raw, "nickname", "userName", "memberName", default=""),
                    _pick(raw, "mobile", "phone", "userMobile", default=""),
                    _pick(raw, "pickupCode", "pickUpCode", default=""),
                    _pick(raw, "linkmanName", "receiverName", "consignee", default=""),
                    _pick(raw, "linkmanMobile", "receiverMobile", default=""),
                    _pick(raw, "linkmanAddress", "address", "receiverAddress", default=""),
                    goods_text,
                ])
                rows += 1
    return rows


def build_diff(results: List[ScopeResult], snapshot_path: Path, today_label: str) -> Dict[str, Any]:
    current: Dict[str, Dict[str, str]] = {}
    for result in results:
        if not result.ok:
            continue
        for order in result.orders:
            if order.order_sn:
                current[order.order_sn] = {
                    "scope": result.scope.title,
                    "status": order.status_name,
                    "amount": f"{order.amount:.2f}",
                }
    diff: Dict[str, Any] = {}
    if not current:
        # 一片订单都没抓到（网络/接口全失败），不覆盖快照，避免下次误报“新增”
        return diff
    if snapshot_path.exists():
        try:
            previous = json.loads(snapshot_path.read_text(encoding="utf-8"))
            prev_orders = previous.get("orders") or {}
            new_sns = [sn for sn in current if sn not in prev_orders]
            diff = {
                "compared_date": previous.get("date") or previous.get("generated_at", "")[:10],
                "new_orders": len(new_sns),
                "new_order_sns": new_sns,
            }
        except (json.JSONDecodeError, OSError):
            diff = {}
    snapshot_path.write_text(
        json.dumps(
            {
                "date": today_label,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "orders": current,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return diff


def cleanup_old_files(folder: Path, keep_days: int, log: logging.Logger) -> None:
    if keep_days <= 0 or not folder.exists():
        return
    cutoff = datetime.now() - timedelta(days=keep_days)
    for item in folder.glob("*"):
        try:
            if item.is_file() and datetime.fromtimestamp(item.stat().st_mtime) < cutoff:
                item.unlink()
                log.info("清理过期文件：%s", item.name)
        except OSError:
            continue


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------


def setup_logging(log_path: Path, level: str) -> logging.Logger:
    log = logging.getLogger("songuo")
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.handlers.clear()
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    stream.setLevel(logging.WARNING)  # 控制台只显示警告，正常信息进文件
    log.addHandler(stream)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        log.addHandler(file_handler)
    except OSError:
        pass
    return log


def cmd_peek(client: Client, cfg: Config) -> int:
    """打印接口真实字段，便于第一次接入时校准字段名。"""
    client.login(cfg.phone, cfg.password)
    print("\n== 首页概览 /homePage/queryOrderCount ==")
    print(json.dumps(client.homepage_stats(), ensure_ascii=False, indent=2)[:2500])

    print("\n== 订单状态字典 /ad/queryDicList?dicType=ORDER_STATUS ==")
    print(json.dumps(client.order_status_dict(), ensure_ascii=False, indent=2)[:2000])

    for scope in SCOPES:
        try:
            data = client.request("POST", scope.path, data={
                "page": 1, "limit": 3, "isConvert": scope.is_convert,
                "orderType": scope.order_type, "orderSn": "", "orderDate": [], "orderStatus": "",
            })
        except ApiError as exc:
            print(f"\n== {scope.title} {scope.path} 失败：{exc}")
            continue
        print(f"\n== {scope.title} {scope.path} ==")
        summary = {k: v for k, v in data.items() if not isinstance(v, (list, dict))} if isinstance(data, dict) else {}
        print("汇总字段：", json.dumps(summary, ensure_ascii=False))
        rows = _first_list(data)
        if rows:
            print("首条订单字段：", json.dumps(rows[0], ensure_ascii=False, indent=2)[:2500])
        else:
            print("本页无订单数据")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    # 中文 Windows 控制台默认 GBK，个别字符（如 ←）无法编码时报错会中断巡检，这里降级为替换
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description="爱绿妈妈后台 · 每日订单巡检",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--date", help="巡检日期：today / yesterday / 2026-10-27")
    parser.add_argument("--config", help="配置文件路径（默认同目录 config.ini）")
    parser.add_argument("--peek", action="store_true", help="只打印接口真实字段，用于校准")
    parser.add_argument("--check", action="store_true", help="只校验账号密码能否登录")
    parser.add_argument("--no-report", action="store_true", help="只输出到控制台，不写报告文件")
    parser.add_argument("--verbose", action="store_true", help="控制台输出详细日志")
    args = parser.parse_args(argv)

    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = Path(args.config).expanduser().resolve()

    if ensure_config():
        print("=" * 78)
        print("  首次运行：已生成配置文件")
        print(f"  {CONFIG_PATH}")
        print("  请填入后台登录手机号、密码后重新运行本脚本。")
        print("=" * 78)
        return EXIT_SETUP

    cfg = load_config()
    if args.date:
        cfg.date_spec = args.date

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = cfg.output_dir / "logs"
    log = setup_logging(log_dir / f"inspection_{datetime.now():%Y%m}.log", cfg.log_level)
    if args.verbose:
        for handler in log.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setLevel(logging.INFO)

    if not cfg.phone or not cfg.password:
        print(f"请先在配置文件里填写后台登录手机号和密码：\n  {CONFIG_PATH}")
        return EXIT_SETUP

    target, date_label = resolve_date(cfg.date_spec)
    client = Client(cfg, log)

    try:
        client.login(cfg.phone, cfg.password)
    except AuthError as exc:
        print(f"登录失败：账号或密码不正确（{exc}）")
        return EXIT_FATAL
    except ApiError as exc:
        print(f"登录失败：{exc}")
        return EXIT_FATAL

    if args.check:
        print(f"登录成功：{mask_phone(cfg.phone)} @ {cfg.base_url}")
        return EXIT_OK

    if args.peek:
        try:
            return cmd_peek(client, cfg)
        except ApiError as exc:
            print(f"接口调试失败：{exc}")
            return EXIT_FATAL

    started = time.time()
    results: List[ScopeResult] = []
    stats: Dict[str, Any] = {}
    status_map: Dict[str, str] = {}

    try:
        stats = client.homepage_stats()
    except ApiError as exc:
        log.warning("首页概览获取失败：%s", exc)
        print(f"[提示] 首页概览接口失败，跳过该部分：{exc}")

    try:
        status_map = client.order_status_dict()
    except ApiError as exc:
        log.warning("状态字典获取失败：%s", exc)

    for scope in SCOPES:
        if not cfg.enabled_scopes.get(scope.key, True):
            results.append(ScopeResult(scope=scope, skipped=True))
            continue
        result = ScopeResult(scope=scope)
        try:
            result = fetch_scope(client, cfg, scope, log)
            enrich_orders(result, status_map, cfg.pending_keywords, target, date_label, log)
        except AuthError:
            log.warning("登录态失效，尝试重新登录后重试 %s", scope.title)
            try:
                client.login(cfg.phone, cfg.password)
                result = fetch_scope(client, cfg, scope, log)
                enrich_orders(result, status_map, cfg.pending_keywords, target, date_label, log)
            except ApiError as exc:
                result = ScopeResult(scope=scope, error=str(exc))
        except ApiError as exc:
            log.error("%s 巡检失败：%s", scope.title, exc)
            result = ScopeResult(scope=scope, error=str(exc))
        results.append(result)

    elapsed = time.time() - started
    alerts = build_alerts(results, cfg, stats, datetime.now())

    print_overview(results, stats, cfg, date_label, target)
    print_status_breakdown(results, cfg)
    print_alerts(alerts)

    diff: Dict[str, Any] = {}
    if not args.no_report:
        snapshot_path = cfg.output_dir / "last_run.json"
        diff = build_diff(results, snapshot_path, f"{target:%Y-%m-%d}" if target else date_label)
        if diff and diff.get("new_orders"):
            print(f"\n  较上次巡检（{diff.get('compared_date')}）新增 {diff['new_orders']} 笔订单")

        stamp = f"{target:%Y%m%d}" if target else datetime.now().strftime("%Y%m%d")
        md_path = cfg.output_dir / f"巡检报告_{stamp}.md"
        write_markdown(md_path, results, alerts, stats, cfg, target, date_label,
                       elapsed, diff)
        print(f"\n  报告：{md_path}")
        if cfg.export_csv:
            csv_path = cfg.output_dir / f"订单明细_{stamp}.csv"
            rows = write_csv(csv_path, results)
            print(f"  明细：{csv_path}（{rows} 行）")
        cleanup_old_files(cfg.output_dir, cfg.keep_days, log)

    failed = [r for r in results if r.error]
    print(f"\n  耗时 {elapsed:.1f}s，退出码 {EXIT_ALERT if alerts else EXIT_OK}"
          f"{'（存在接口失败）' if failed else ''}")
    print()
    return EXIT_ALERT if (alerts or failed) else EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - 兜底，避免窗口一闪而过看不到错误
        import traceback
        traceback.print_exc()
        print(f"\n脚本异常退出：{exc}")
        sys.exit(EXIT_FATAL)
