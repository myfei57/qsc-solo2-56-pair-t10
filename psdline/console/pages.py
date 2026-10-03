"""控制台页面。

四个页面各自只读一份状态：站台页看门区与布局、列车页看到发与对位、门控页看开闭
与锁定、告警页看故障与审计。页面不做任何写操作，动作一律走 ``/api/...`` 接口，
避免浏览器刷新就把现场设备带动一次。
"""

from __future__ import annotations

from html import escape

from ..app import Platform

PAGE_PATHS: tuple[str, ...] = ("/", "/trains", "/doors", "/link", "/alarms")

_STYLE = """
body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 0; background: #101820; color: #e8eef5; }
header { padding: 14px 20px; background: #18222e; border-bottom: 1px solid #243447; }
nav a { color: #8ecdf7; margin-right: 14px; text-decoration: none; }
main { padding: 18px 20px 40px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 18px; }
th, td { border: 1px solid #243447; padding: 6px 10px; text-align: left; font-size: 14px; }
th { background: #18232f; }
.alarm { color: #ff8d8d; font-weight: 600; }
.ok { color: #7ee787; }
.warn { color: #f0c674; }
"""

_NAV: tuple[tuple[str, str], ...] = (
    ("/", "站台"),
    ("/trains", "列车"),
    ("/doors", "门控"),
    ("/link", "链路"),
    ("/alarms", "告警"),
)


def _table(title: str, rows: list[tuple[str, object]]) -> str:
    """渲染一张两列数据表。"""

    body = "".join(
        f"<tr><th>{escape(str(key))}</th><td>{escape(str(value))}</td></tr>" for key, value in rows
    )
    return f"<h2>{escape(title)}</h2><table>{body}</table>"


def _shell(title: str, body: str) -> str:
    """套上页头与导航。"""

    nav = "".join(f'<a href="{path}">{label}</a>' for path, label in _NAV)
    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f'<body><header><strong>{escape(title)}</strong><nav style="margin-top:8px">{nav}</nav>'
        f"</header><main>{body}</main></body></html>"
    )


def _station_page(platform: Platform) -> str:
    """站台页：门区、布局与映射修订。"""

    status = platform.status()
    rows: list[tuple[str, object]] = [
        ("线路", status["line"]),
        ("站台数", len(status["stations"])),
        ("门区总数", sum(len(item["door_groups"]) for item in status["stations"])),
        ("映射重排次数", platform.mappings.rewrites),
    ]
    body = _table("线路概览", rows)
    body += _table(
        "站台与门区",
        [
            (item["id"], f"{item['name']} 门区 {len(item['door_groups'])} 个")
            for item in status["stations"]
        ]
        or [("暂无", "-")],
    )
    views = [platform.station_view(item["id"]) for item in status["stations"]]
    body += _table(
        "布局与映射",
        [(item["station"]["id"], f"修订 {item['revision']} 跨度 {item['span_m']} m") for item in views]
        or [("暂无", "-")],
    )
    return _shell("站台控制台", body)


def _train_page(platform: Platform) -> str:
    """列车页：到发、对位与灯位。"""

    status = platform.status()
    views = [platform.train_view(train_id) for train_id in status["trains"]]
    body = _table(
        "在线列车",
        [
            (
                item["train"]["id"],
                f"{item['train']['station']} 位置 {item['state']['position_m']} m 灯位 {item['aspect']}",
            )
            for item in views
            if item["train"]
        ]
        or [("暂无", "-")],
    )
    body += _table(
        "对位与车门",
        [
            (
                item["state"]["train_id"],
                f"对位 {'已落盘' if item['state']['aligned'] else '未落盘'} 车门 {'开' if item['state']['doors_open'] else '关'}",
            )
            for item in views
        ]
        or [("暂无", "-")],
    )
    body += _table(
        "门控队列",
        [
            (
                item["state"]["train_id"],
                f"排队 {item['doors']['queued']} 条 发车 {'是' if item['doors']['departed'] else '否'}",
            )
            for item in views
        ]
        or [("暂无", "-")],
    )
    return _shell("列车控制台", body)


def _door_page(platform: Platform) -> str:
    """门控页：开闭、锁定与批量上限。"""

    status = platform.status()
    report = status["door_report"]
    body = _table(
        "门控总览",
        [
            ("累计开门", report["opens"]),
            ("累计关门", report["closes"]),
            ("批量上限", report["batch_limit"]),
            ("排队指令", report["queued"]),
            ("锁定门数", report["locked"]),
        ],
    )
    body += _table(
        "门区状态",
        [
            (
                item["id"],
                f"{item['station']} {item['group']} {'开' if item['open'] else '关'}{' 已锁' if item['locked'] else ''}",
            )
            for item in status["doors"][:24]
        ]
        or [("暂无", "-")],
    )
    body += _table(
        "站台开门压力",
        [
            (item["station_id"], f"开 {item['open_count']} / 上限 {item['limit']}")
            for item in report["stations"]
        ]
        or [("暂无", "-")],
    )
    return _shell("门控控制台", body)


def _alarm_page(platform: Platform) -> str:
    """告警页：故障、锁定比例与审计时间线。"""

    status = platform.status()
    interlock = status["interlock"]
    audit = status["audit"]
    body = _table(
        "联锁与故障",
        [
            ("活动故障", ", ".join(interlock["faults_active"]) or "-"),
            ("锁定门数", interlock["locked_count"]),
            ("锁定比例", interlock["locked_ratio"]),
            ("可发车", "是" if interlock["can_release_depart"] else "否"),
            ("审计条数", audit["entries"]),
            ("审计分段", audit["segments"]),
        ],
    )
    body += _table(
        "间隙探测",
        [
            (item["train_id"], f"{item['state'] or '未探测'} {'已确认' if item['clear'] else ''}")
            for item in status["gaps"]["trains"]
        ]
        or [("暂无", "-")],
    )
    timeline = platform.snapshot()["timeline"]
    body += _table(
        "最近审计",
        [(f"{item['step']}. {item['kind']}", f"{item['train_id']} {item['station_id']}") for item in timeline]
        or [("暂无", "-")],
    )
    return _shell("告警控制台", body)


def _link_page(platform: Platform) -> str:
    """链路页：每列车的接触状态、位置可信度、放行档位与退避。"""

    status = platform.status()
    links = status.get("link", {})
    mode_label = {"auto": "自动放行", "manual": "降级·人工确认", "blocked": "禁止"}
    body = _table(
        "链路总览",
        [
            ("纳入管控列车", len(links)),
            ("链路事件数", status.get("link_events", 0)),
            ("哈希链", "完整" if status.get("link_chain", {}).get("ok") else "断裂"),
            ("启动时补偿副作用", status.get("link_pending_on_boot", {}).get("retried", 0)),
        ],
    )

    def cell(train_id: str, item: dict) -> str:
        mode = item.get("mode", "")
        cls = "ok" if mode == "auto" else ("warn" if mode == "manual" else "alarm")
        return (
            f"{escape(train_id)} "
            f'接触 <b>{escape(str(item.get("contact")))}</b> '
            f'档位 <span class="{cls}">{escape(mode_label.get(mode, mode))}</span> '
            f"可信度 {item.get('confidence')} 窗口 {item.get('window_score')} "
            f"位置 {item.get('position_m')}（龄 {item.get('position_age_s')} s）"
            f" 退避档 {item.get('backoff_level')}"
        )

    body += _table(
        "车地无线链路与位置可信度",
        [(train_id, cell(train_id, item)) for train_id, item in sorted(links.items())]
        or [("暂无", "尚无列车上报链路采样")],
    )
    effects = status.get("link_effects", [])
    body += _table(
        "最近下发的现场副作用（信号/门控/告警）",
        [(item.get("effect_id", ""), item.get("kind", "")) for item in effects[-12:]]
        or [("暂无", "-")],
    )
    return _shell("车地无线链路控制台", body)


_RENDERERS = {
    "/": _station_page,
    "/trains": _train_page,
    "/doors": _door_page,
    "/link": _link_page,
    "/alarms": _alarm_page,
}


def render_page(path: str, platform: Platform) -> str | None:
    """渲染一个页面；路径不是页面时返回 ``None``。"""

    renderer = _RENDERERS.get(path)
    if renderer is None:
        return None
    return renderer(platform)
