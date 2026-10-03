"""控制台接口处理器。

每个接口都是对平台公开方法的一次调用；动作类接口把请求体里的参数透传给平台，
缺少必填字段或取值非法时返回 ``400``，联锁异常统一转成 ``409`` 并在响应里带上
组件名与原因。页面不走这里，见 :mod:`psdline.console.pages`。
"""

from __future__ import annotations

from ..app import Platform
from ..audit import build_timeline
from ..errors import RailError
from .router import Router


def _require(body: dict, key: str) -> str:
    """取出必填的字符串字段。"""

    value = body.get(key)
    if value is None or str(value) == "":
        raise RailError(f"缺少必填字段 {key!r}", component="console")
    return str(value)


def _as_int(body: dict, key: str, default: int) -> int:
    """取出整数字段。"""

    try:
        return int(body.get(key, default))
    except (TypeError, ValueError) as error:
        raise RailError(f"参数 {key!r} 必须是整数", component="console") from error


def _as_float(body: dict, key: str, default: float) -> float:
    """取出浮点字段。"""

    try:
        return float(body.get(key, default))
    except (TypeError, ValueError) as error:
        raise RailError(f"参数 {key!r} 必须是数值", component="console") from error


class Api:
    """把 HTTP 请求翻译成平台调用。"""

    def __init__(self, platform: Platform) -> None:
        self._platform = platform
        self._router = Router()
        self._register()

    @property
    def platform(self) -> Platform:
        return self._platform

    def _register(self) -> None:
        add = self._router.add
        add("GET", "/api/status", lambda params, body: self._platform.status())
        add("GET", "/api/snapshot", lambda params, body: self._platform.snapshot())
        add("GET", "/api/namespace", lambda params, body: self._platform.namespace.as_dict())
        add("GET", "/api/namespaces", lambda params, body: self._platform.namespaces.describe())
        add("GET", "/api/stations", lambda params, body: {"stations": self._platform.status()["stations"]})
        add("GET", "/api/stations/{stationID}", self._station)
        add("GET", "/api/trains", lambda params, body: {"trains": self._platform.status()["trains"]})
        add("GET", "/api/trains/{trainID}", self._train)
        add("GET", "/api/doors", lambda params, body: {"doors": self._platform.status()["doors"]})
        add("GET", "/api/doors/{psdID}", self._gate)
        add("GET", "/api/interlock", lambda params, body: self._platform.status()["interlock"])
        add("GET", "/api/gaps", lambda params, body: self._platform.status()["gaps"])
        add("GET", "/api/link", lambda params, body: self._platform.link_summary())
        add("GET", "/api/link/{trainID}", self._link_train)
        add("GET", "/api/link/{trainID}/replay", self._link_replay)
        add("GET", "/api/quota", lambda params, body: self._platform.status()["quota"])
        add("GET", "/api/audit", self._audit)
        add("GET", "/api/timeline", self._timeline)
        add("GET", "/api/trend", self._trend)
        add("GET", "/api/store", self._store)
        add("GET", "/api/mapping/{stationID}", self._mapping)
        add("GET", "/api/resolve", self._resolve)
        add("GET", "/api/readiness", self._readiness)
        add("POST", "/api/arrive", self._arrive)
        add("POST", "/api/depart", self._depart)
        add("POST", "/api/gap", self._gap)
        add("POST", "/api/enqueue", self._enqueue)
        add("POST", "/api/process", self._process)
        add("POST", "/api/renumber", self._renumber)
        add("POST", "/api/batch-open", self._batch_open)
        add("POST", "/api/fault", self._fault)
        add("POST", "/api/clear-fault", self._clear_fault)
        add("POST", "/api/arm", self._arm)
        add("POST", "/api/position", self._position)
        add("POST", "/api/link/sample", self._link_sample)
        add("POST", "/api/sequence", self._sequence)
        add("POST", "/api/cycle", self._cycle)
        add("POST", "/api/sweep", self._sweep)
        add("POST", "/api/dwell", self._dwell)
        add("POST", "/api/route", self._route)
        add("POST", "/api/reset-state", self._reset_state)

    def routes(self) -> list[dict]:
        """返回接口清单，供自检与文档核对。"""

        return [route.as_dict() for route in self._router.routes()]

    def handle(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        """处理一次请求，返回状态码与响应体。"""

        matched = self._router.match(method, path)
        if matched is None:
            return 404, {"error": "not_found", "path": path}
        handler, params = matched
        merged = {**query, **params}
        try:
            payload = handler(merged, body)
        except RailError as error:
            self._platform.note_error(error)
            status = 409 if error.component != "console" else 400
            return status, {"error": type(error).__name__, **error.as_record()}
        except (TypeError, ValueError) as error:
            return 400, {"error": "bad_request", "message": str(error)}
        return 200, payload

    # ---- 查询 ---------------------------------------------------------

    def _station(self, params: dict, body: dict) -> dict:
        return self._platform.station_view(str(params.get("stationID", "")))

    def _train(self, params: dict, body: dict) -> dict:
        train_id = str(params.get("trainID", ""))
        view = self._platform.train_view(train_id)
        if view["train"] is None:
            raise RailError(f"列车不存在: {train_id}", component="console")
        return view

    def _gate(self, params: dict, body: dict) -> dict:
        psd_id = str(params.get("psdID", ""))
        gate = self._platform.gates.get(psd_id)
        if gate is None:
            raise RailError(f"站台门不存在: {psd_id}", component="console")
        return {"gate": gate.as_dict(), "pressure": self._platform.doors.batch_limit()}

    def _audit(self, params: dict, body: dict) -> dict:
        entries = self._platform.audit.list()
        return {
            "entries": [entry.as_dict() for entry in entries[-int(params.get("limit", 50)) :]],
            "summary": self._platform.audit.summary(),
        }

    def _timeline(self, params: dict, body: dict) -> dict:
        train_id = str(params.get("train_id", ""))
        return {"timeline": build_timeline(self._platform.audit.list(), train_id)}

    def _trend(self, params: dict, body: dict) -> dict:
        signal = str(params.get("signal", "locked_count"))
        samples = self._platform.trend.series(signal, int(params.get("limit", 60)))
        filled = self._platform.trend.fill_gaps(signal, samples)
        return {
            "signal": signal,
            "period_s": self._platform.trend.period_s,
            "signals": self._platform.trend.signals(),
            "samples": [sample.as_dict() for sample in samples],
            "filled": [sample.as_dict() for sample in filled],
        }

    def _store(self, params: dict, body: dict) -> dict:
        return {
            "summary": self._platform.metrics.summary(),
            "documents": self._platform.store.list_ids(),
        }

    def _mapping(self, params: dict, body: dict) -> dict:
        station_id = str(params.get("stationID", ""))
        mapping = self._platform.mappings.current(station_id)
        if mapping is None:
            raise RailError(f"站台 {station_id} 没有门区映射", component="console")
        return mapping.as_dict()

    def _resolve(self, params: dict, body: dict) -> dict:
        return self._platform.resolve_group(
            str(params.get("station_id", "")), str(params.get("group_key", ""))
        )

    def _readiness(self, params: dict, body: dict) -> dict:
        checks = {
            "stations": bool(self._platform.stations.list()),
            "gates": bool(self._platform.gates.list()),
            "quota_arrival": self._platform.quotas.status("arrival")["configured"],
            "quota_depart": self._platform.quotas.status("depart")["configured"],
            "audit_log": self._platform.audit.journal.count() >= 0,
        }
        return {"ready": all(checks.values()), "checks": checks}

    # ---- 动作 ---------------------------------------------------------

    def _arrive(self, params: dict, body: dict) -> dict:
        return self._platform.arrive(
            _require(body, "train_id"),
            _require(body, "station_id"),
            _as_int(body, "position_m", 0),
            operator=str(body.get("operator", "")),
        )

    def _depart(self, params: dict, body: dict) -> dict:
        return self._platform.depart(
            _require(body, "train_id"),
            _require(body, "station_id"),
            operator=str(body.get("operator", "")),
            confirm_degraded=bool(body.get("confirm_degraded", False)),
        )

    def _gap(self, params: dict, body: dict) -> dict:
        return self._platform.check_gap(_require(body, "train_id"), bool(body.get("clear", True)))

    def _enqueue(self, params: dict, body: dict) -> dict:
        return self._platform.enqueue(
            _require(body, "train_id"),
            _require(body, "id"),
            str(body.get("kind", "open")),
            str(body.get("station_id", "")),
        )

    def _process(self, params: dict, body: dict) -> dict:
        return self._platform.process_queue(_require(body, "train_id"))

    def _renumber(self, params: dict, body: dict) -> dict:
        groups = body.get("groups")
        if not isinstance(groups, dict) or not groups:
            raise RailError("映射必须是非空对象", component="console")
        return self._platform.renumber(
            _require(body, "station_id"), {str(k): str(v) for k, v in groups.items()}
        )

    def _batch_open(self, params: dict, body: dict) -> dict:
        return self._platform.batch_open(_require(body, "station_id"))

    def _fault(self, params: dict, body: dict) -> dict:
        return self._platform.set_fault(_require(body, "psd_id"))

    def _clear_fault(self, params: dict, body: dict) -> dict:
        return self._platform.clear_fault(_require(body, "psd_id"))

    def _arm(self, params: dict, body: dict) -> dict:
        return self._platform.arm(_require(body, "train_id"), bool(body.get("armed", True)))

    def _position(self, params: dict, body: dict) -> dict:
        seq = body.get("seq")
        rtt = body.get("rtt_ms")
        sent = body.get("sent_at")
        return self._platform.refresh_position(
            _require(body, "train_id"),
            _as_int(body, "position_m", 0),
            seq=int(seq) if seq is not None else None,
            rtt_ms=float(rtt) if rtt is not None else None,
            sent_at=float(sent) if sent is not None else None,
        )

    def _link_sample(self, params: dict, body: dict) -> dict:
        seq = body.get("seq")
        rtt = body.get("rtt_ms")
        sent = body.get("sent_at")
        return self._platform.link_sample(
            _require(body, "train_id"),
            ok=bool(body.get("ok", True)),
            seq=int(seq) if seq is not None else None,
            rtt_ms=float(rtt) if rtt is not None else None,
            sent_at=float(sent) if sent is not None else None,
        )

    def _link_train(self, params: dict, body: dict) -> dict:
        train_id = str(params.get("trainID", ""))
        view = self._platform.link_view(train_id)
        if view is None:
            raise RailError(f"列车没有链路会话: {train_id}", component="console")
        return view

    def _link_replay(self, params: dict, body: dict) -> dict:
        return self._platform.replay_link(str(params.get("trainID", "")))

    def _sequence(self, params: dict, body: dict) -> dict:
        return self._platform.sequence_verdict(
            _as_int(body, "previous", 0), _as_int(body, "step", 1)
        )

    def _cycle(self, params: dict, body: dict) -> dict:
        return self._platform.cycle(_as_float(body, "dt", 1.0))

    def _sweep(self, params: dict, body: dict) -> dict:
        return self._platform.sweep(_require(body, "station_id"))

    def _dwell(self, params: dict, body: dict) -> dict:
        return self._platform.register_dwell(
            _require(body, "train_id"),
            _as_float(body, "seconds", 0.0),
            str(body.get("station_id", "")),
        )

    def _route(self, params: dict, body: dict) -> dict:
        return self._platform.assign_route(
            _require(body, "train_id"),
            _require(body, "origin"),
            _require(body, "destination"),
            _as_int(body, "distance_m", 0),
        )

    def _reset_state(self, params: dict, body: dict) -> dict:
        return {"removed": self._platform.reset_state()}
