"""平台装配与控制循环。

``Platform`` 把命名空间、站台、列车、站台门、信号、联锁、门控、门区映射、间隙
探测、配额与审计按真实调用链装到一起，并暴露四件事：到发编排（到站、发车、
门区联动、批量开门、清扫）、控制循环、状态快照与手动检修动作。

控制链固定为「信号 → 停稳对位 → 开门 → 间隙探测 → 关门 → 发车 → 审计」，
任何一步错位都会被对应组件拒绝，并把拒绝原因落进审计。
"""

from __future__ import annotations

import threading

from .audit import AuditLog, build_timeline
from .clock import Clock, SystemClock
from .config import LinkSection, PlatformConfig, default_config, with_storage_root
from .door import (
    DoorController,
    active_group,
    batch_open,
    close_and_audit,
    depart,
    depart_policy,
    door_report,
    door_state,
    enqueue,
    open_pressure,
    open_doors,
    open_group,
    process_queue,
    queued_total,
)
from .errors import InterlockError, RailError
from .eventbus import Event, EventBus
from .gap import GapRegistry, gap_summary
from .ids import IdFactory
from .interlock import (
    FaultRegistry,
    InterlockArmer,
    interlock_status,
    next_sequence,
    releasable,
    restore_lockouts,
    verdict_of,
)
from .link import REDUCED, UNTRUSTED, LinkMonitor, LinkTuning, replay_train
from .ns import Namespace, NamespaceRegistry
from .platform import MappingRegistry, adjacent, group_keys, physical_groups, resolve
from .psd import LockoutRegistry, PSD, PSDRegistry, sweep_station
from .quota import QuotaRegistry
from .sig import SignalController, aspect_of, depart_checked, refresh_position, signal_summary
from .station import (
    DoorZone,
    Layout,
    LayoutRegistry,
    Schedule,
    ScheduleRegistry,
    Station,
    StationRegistry,
    nearest_zone,
    spread_m,
)
from .store import CommandLog, JsonStore, JsonlJournal, StoreMetrics
from .telemetry import TrendBuffer
from .train import (
    AlignRegistry,
    DoorStateRegistry,
    Dwell,
    DwellRegistry,
    PositionTracker,
    Route,
    RouteRegistry,
    Train,
    TrainRegistry,
    train_state,
)

ARRIVAL = "arrival"
DEPART = "depart"


class Platform:
    """站台门区顺序与联锁控制平台。"""

    def __init__(
        self,
        config: PlatformConfig | None = None,
        clock: Clock | None = None,
        unit: str | None = None,
    ) -> None:
        self.config = (config or default_config()).validate()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.unit = unit or self.config.platform.unit
        self.ids = IdFactory("op")

        store = JsonStore(self.config.storage_root)
        self.store = store
        self.metrics = StoreMetrics(store.root)
        self.audit = AuditLog(
            self.clock,
            self.bus,
            JsonlJournal(store.root, "audit", segment_limit=self.config.audit.journal_segment),
        )
        self.namespace = Namespace(
            self.config.namespace.line, list(self.config.namespace.stations)
        )
        self.namespaces = NamespaceRegistry(self.unit)
        self.trend = TrendBuffer(period_s=self.config.signal.refresh_period_s)

        self.stations = StationRegistry(store)
        self.layouts = LayoutRegistry(store)
        self.schedules = ScheduleRegistry(store)
        self.trains = TrainRegistry(store)
        self.align = AlignRegistry(store, tolerance_m=self.config.station.stop_tolerance_m)
        self.train_doors = DoorStateRegistry(store)
        self.dwells = DwellRegistry(store, max_seconds=self.config.train.dwell_max_s)
        self.positions = PositionTracker(
            store,
            max_position_m=self.config.train.max_position_m,
            stale_after_s=self.config.signal.stale_after_s,
        )
        self.routes = RouteRegistry(store)
        self.link = LinkMonitor(
            JsonlJournal(store.root, "link", segment_limit=self.config.audit.journal_segment),
            _link_tuning(self.config.link),
        )
        self._train_locks: dict[str, threading.RLock] = {}
        self._train_locks_guard = threading.RLock()

        self.gates = PSDRegistry(store)
        self.lockouts = LockoutRegistry(self.gates)
        self.signals = SignalController(store)
        self.faults = FaultRegistry(store)
        self.armers = InterlockArmer(store)
        self.mappings = MappingRegistry(store)
        self.gaps = GapRegistry(store)
        self.quotas = QuotaRegistry(store)
        self.commands = CommandLog(store)
        self.doors = DoorController(
            store, self.align, self.commands, self.gates, self.lockouts
        )
        self.doors.set_batch_limit(self.config.door.batch_limit)

        self._cycles = 0
        self._operations = 0
        self._event_counts: dict[str, int] = {}
        self.bus.subscribe("*", self._on_event)
        self._register_namespaces()
        self._seed(store)
        self._restore()

    def _on_event(self, event: Event) -> None:
        """统计总线事件，供状态接口判断动作有没有真的流过组件。"""

        self._event_counts[event.topic] = self._event_counts.get(event.topic, 0) + 1

    # ---- 装配 ---------------------------------------------------------

    def _register_namespaces(self) -> None:
        """登记现场设备点位（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("line", "signal", "signal-controller"),
            ("line", "sequence", "sequence-source"),
            ("station", "platform", "platform-screen"),
            ("station", "gap", "gap-detector"),
            ("station", "door", "platform-door"),
            ("station", "interlock", "door-interlock"),
            ("station", "quota", "quota-counter"),
            ("station", "audit", "audit-sink"),
        ):
            self.namespaces.register(area, device, kind)

    def _seed(self, store: JsonStore) -> None:
        """按线路命名空间布置站台、布局、映射与站台门。"""

        seed_doc = "platform/seed"
        if store.exists(seed_doc):
            return
        groups_per_station = self.config.station.groups_per_station
        doors_per_group = self.config.station.doors_per_group
        span = self.config.station.group_span_m
        for station_index, station_id in enumerate(self.namespace.station_ids(), start=1):
            groups = [f"G{index:02d}" for index in range(1, groups_per_station + 1)]
            self.stations.upsert(
                Station(
                    id=station_id,
                    name=f"站台 {station_index}",
                    line=self.namespace.line_id(),
                    door_groups=groups,
                )
            )
            base = (station_index - 1) * span
            zones = [
                DoorZone(id=group, distance_m=int(base + (index - 1) * span / groups_per_station))
                for index, group in enumerate(groups, start=1)
            ]
            self.layouts.save(Layout(station_id=station_id, zones=zones))
            self.mappings.renumber(station_id, {group: group for group in groups})
            self.schedules.save(
                Schedule(station_id=station_id, headways=[120, 150, 180, 240])
            )
            for group in groups:
                for door_index in range(1, doors_per_group + 1):
                    self.gates.upsert(
                        PSD(
                            id=f"{station_id}-{group}-{door_index:02d}",
                            station=station_id,
                            group=group,
                        )
                    )
        for name, limit in (
            (ARRIVAL, self.config.quota.arrival_limit),
            (DEPART, self.config.quota.depart_limit),
        ):
            self.quotas.set_limit(name, limit)
        store.write(
            seed_doc,
            {"line": self.namespace.line_id(), "stations": self.namespace.station_ids()},
        )

    def _restore(self) -> None:
        """从落盘状态恢复操作计数、锁定状态与链路会话。"""

        state = self.store.read("platform/state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))
        restore_lockouts(self.faults, self.gates, self.lockouts, not self.gaps.blocked_ids())
        self.link.restore()

    def _persist(self) -> None:
        """落盘平台计数，供重启后对照。"""

        self.store.write(
            "platform/state",
            {
                "operations": self._operations,
                "cycles": self._cycles,
                "unit": self.unit,
                "line": self.namespace.line_id(),
            },
        )

    def _record_operation(self, op_id: str, operation: str, report: dict) -> dict:
        """把编排步骤逐条写事件，并记一条汇总审计。"""

        train_id = str(report.get("train_id", ""))
        station_id = str(report.get("station_id", ""))
        reserved = {
            "train_id",
            "station_id",
            "op_id",
            "severity",
            "message",
            "component",
            "action",
            "detail",
        }
        for step in report.get("steps", []):
            detail = {
                key: value
                for key, value in dict(step.get("detail", {})).items()
                if not isinstance(value, (dict, list)) and key not in reserved
            }
            self.bus.publish(
                Event.of(
                    "door",
                    str(step.get("component", "door")),
                    str(step.get("action", "step")),
                    op_id=op_id,
                    train_id=train_id,
                    severity="notice",
                    **detail,
                )
            )
        for violation in report.get("violations", []):
            self.bus.publish(
                Event.of(
                    "audit",
                    "interlock",
                    "interlock_rejected",
                    op_id=op_id,
                    train_id=train_id,
                    station_id=station_id,
                    severity="alarm",
                    message=f"{operation} 被拒绝：{violation}",
                    violation=violation,
                )
            )
        entry = self.audit.record(
            f"{operation}_complete",
            train_id,
            station_id,
            op_id=op_id,
            ok=bool(report.get("ok")),
            violations=list(report.get("violations", [])),
        )
        return {
            "op_id": op_id,
            "operation": operation,
            "ok": bool(report.get("ok")),
            "violations": list(report.get("violations", [])),
            "audit_id": entry.id,
        }

    # ---- 链路质量监督 --------------------------------------------------

    def _lock_for(self, train_id: str) -> threading.RLock:
        """返回一列车的控制锁：采样、位置写入与闸门判定按列车串行。"""

        with self._train_locks_guard:
            return self._train_locks.setdefault(train_id, threading.RLock())

    def _audit_link_decisions(self, train_id: str, decisions: list[dict]) -> None:
        """把链路状态判定逐条落审计（证据随判定一起落盘，供事后重放）。"""

        for decision in decisions:
            kind = "link_backoff" if decision.get("kind") == "backoff" else "link_state"
            detail = {
                key: value
                for key, value in decision.items()
                if key not in ("type", "kind", "train_id", "at")
            }
            self.audit.record(kind, train_id, "", decision_at=decision.get("at"), **detail)

    def _write_trust(self, train_id: str, verdict: dict) -> None:
        """把位置可信度标注落盘；可信度等级变化时补一条审计。"""

        doc_id = f"train/{train_id}/trust"
        previous = self.store.read(doc_id)
        level = verdict["trust"]
        self.store.write(
            doc_id,
            {
                "train_id": train_id,
                "level": level,
                "state": verdict["state"],
                "quality": verdict["quality"],
                "backoff_level": verdict["backoff_level"],
                "at": self.clock.now(),
            },
        )
        if not isinstance(previous, dict) or previous.get("level") != level:
            self.audit.record(
                "position_trust",
                train_id,
                "",
                level=level,
                state=verdict["state"],
                quality=verdict["quality"],
            )

    def _trust_view(self, train_id: str) -> dict:
        """当前位置可信度：以链路监控器为准；监控器没有会话时（例如链路流水
        缺失的部分失败）回退到落盘标注。"""

        trust = self.link.trust(train_id)
        if trust is not None:
            return {"level": trust, "source": "live"}
        raw = self.store.read(f"train/{train_id}/trust")
        if isinstance(raw, dict):
            return {"level": raw.get("level"), "source": "doc", **raw}
        return {"level": None, "source": "none"}

    def _link_gate(
        self,
        train_id: str,
        station_id: str,
        action: str,
        *,
        operator: str = "",
        confirm_degraded: bool = False,
    ) -> dict:
        """发车／开门前的链路闸门。

        返回 ``{"passed": True, "trust": ...}`` 表示放行；否则 ``passed`` 为
        ``False`` 且 ``report`` 带拒绝原因。彻底失联一律拒绝（退避），降级
        需要人工确认，确认人写进审计。无链路会话的列车走既有路径。
        """

        verdict = self.link.evaluate(train_id, self.clock.now())
        if verdict is None:
            return {"passed": True, "trust": None}
        self._audit_link_decisions(train_id, verdict["decisions"])
        trust = verdict["trust"]
        evidence = {
            "quality": verdict["quality"],
            "state": verdict["state"],
            "silence_s": verdict["silence_s"],
            "backoff_level": verdict["backoff_level"],
        }
        if trust == UNTRUSTED:
            reason = "车地链路失联，发车退避" if action == "depart" else "车地链路失联，禁止开门"
            self.audit.record(f"{action}_lost_blocked", train_id, station_id, **evidence)
            return {
                "passed": False,
                "trust": trust,
                "report": {
                    "ok": False,
                    "train_id": train_id,
                    "station_id": station_id,
                    "trust": trust,
                    "steps": [],
                    "violations": [reason],
                    "evidence": evidence,
                },
            }
        if trust == REDUCED:
            confirmed = bool(operator) and (confirm_degraded if action == "depart" else True)
            if not confirmed:
                reason = (
                    "链路降级，发车需人工确认" if action == "depart" else "链路降级，开门需人工确认"
                )
                self.audit.record(f"{action}_degraded_blocked", train_id, station_id, **evidence)
                return {
                    "passed": False,
                    "trust": trust,
                    "report": {
                        "ok": False,
                        "train_id": train_id,
                        "station_id": station_id,
                        "trust": trust,
                        "steps": [],
                        "violations": [reason],
                        "evidence": evidence,
                    },
                }
            self.audit.record(
                f"{action}_degraded", train_id, station_id, operator=operator, **evidence
            )
        return {"passed": True, "trust": trust}

    # ---- 生命周期 -----------------------------------------------------

    def start(self) -> dict:
        """启动平台：核对命名空间、站台、布局与映射是否一致。"""

        missing: dict[str, list[str]] = {}
        for station_id in self.namespace.station_ids():
            if self.stations.get(station_id) is None:
                missing.setdefault("station", []).append(station_id)
            if self.layouts.get(station_id) is None:
                missing.setdefault("layout", []).append(station_id)
            if self.mappings.current(station_id) is None:
                missing.setdefault("mapping", []).append(station_id)
        return {
            "unit": self.unit,
            "line": self.namespace.label(),
            "missing": missing,
            "stations": len(self.namespace.station_ids()),
            "gates": len(self.gates.list()),
            "operations": self._operations,
            "ready": not missing,
        }

    def stop(self) -> dict:
        """停运平台：整站清扫后收起全部灯位。"""

        op_id = self.ids.labeled("stop")
        steps: list[dict] = []
        closed = 0
        for station_id in self.namespace.station_ids():
            report = self.sweep(station_id)
            closed += report["closed"]
            steps.append(
                {
                    "component": "psd",
                    "action": "sweep",
                    "detail": {"station_id": station_id, "closed": report["closed"]},
                }
            )
        for train in self.trains.list():
            self.signals.clear(train.id, ARRIVAL)
            self.signals.clear(train.id, DEPART)
        report = {"ok": True, "train_id": "", "closed": closed, "steps": steps, "violations": []}
        summary = self._record_operation(op_id, "stop", report)
        self._operations += 1
        self._persist()
        return {**report, **summary}

    # ---- 到发编排 -----------------------------------------------------

    def arrive(self, train_id: str, station_id: str, position_m: int, operator: str = "") -> dict:
        """到站到开门：信号到站 → 停稳对位 → 对位落盘 → 开门。"""

        op_id = self.ids.labeled("arrive")
        steps: list[dict] = []
        violations: list[str] = []

        def reject(reason: str) -> dict:
            violations.append(reason)
            self.bus.publish(
                Event.of(
                    "audit",
                    "interlock",
                    "arrive_rejected",
                    op_id=op_id,
                    train_id=train_id,
                    station_id=station_id,
                    severity="alarm",
                    message=reason,
                )
            )
            return {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": violations,
            }

        if not self.quotas.consume(ARRIVAL):
            return reject("到站配额已用完")
        try:
            self.namespace.require(station_id)
        except RailError as error:
            return reject(error.message)
        if self.stations.get(station_id) is None:
            return reject(f"站台未登记: {station_id}")

        self.signals.raise_signal(train_id, ARRIVAL, self.clock.now())
        steps.append(
            {"component": "sig", "action": "arrival_signal", "detail": {"train_id": train_id}}
        )
        if self.trains.get(train_id) is None:
            self.trains.upsert(Train(id=train_id, line=self.namespace.line_id()))
        self.trains.assign_station(train_id, station_id)

        stop_position = self._stop_position(station_id)
        try:
            alignment = self.align.align(train_id, position_m, stop_position, self.clock.now())
        except RailError as error:
            return reject(error.message)
        steps.append(
            {
                "component": "train",
                "action": "align",
                "detail": {"train_id": train_id, "position_m": alignment.position_m},
            }
        )
        try:
            self.doors_open(train_id, station_id, operator=operator)
        except RailError as error:
            return reject(error.message)
        entry = self.audit.record(ARRIVAL, train_id, station_id, position_m=position_m)
        report = {
            "ok": True,
            "train_id": train_id,
            "station_id": station_id,
            "position_m": alignment.position_m,
            "steps": steps,
            "violations": [],
        }
        summary = self._record_operation(op_id, "arrive", report)
        self._operations += 1
        self._persist()
        return {**report, **summary, "arrival_audit": entry.id}

    def doors_open(self, train_id: str, station_id: str, operator: str = "") -> dict:
        """开门并确认车门：对位必须先落盘；链路降级需人工确认，失联禁止开门。"""

        gate = self._link_gate(train_id, station_id, "door_open", operator=operator)
        if not gate["passed"]:
            raise InterlockError(gate["report"]["violations"][0], component="link")
        report = open_doors(self.doors, train_id, self.clock.now())
        group = self._primary_group(station_id)
        if group:
            open_group(self.doors, self.mappings, station_id, group)
        self.train_doors.confirm_open(train_id, self.clock.now())
        self.bus.publish(
            Event.of(
                "door",
                "door",
                "opened",
                train_id=train_id,
                station_id=station_id,
                severity="notice",
                group=group,
            )
        )
        return {**report, "group": group}

    def depart(
        self,
        train_id: str,
        station_id: str,
        operator: str = "",
        confirm_degraded: bool = False,
    ) -> dict:
        """关门到发车：链路闸门 → 关门指令 → 间隙探测 → 关门落盘 → 发车信号。

        链路闸门放在最前面：彻底失联直接退避（不产生任何门控副作用），
        降级必须人工确认后才按降级模式放行，确认人与链路证据落审计。
        """

        op_id = self.ids.labeled("depart")
        steps: list[dict] = []
        if not self.quotas.consume(DEPART):
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": ["发车配额已用完"],
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}

        gate = self._link_gate(
            train_id, station_id, "depart", operator=operator, confirm_degraded=confirm_degraded
        )
        if not gate["passed"]:
            summary = self._record_operation(op_id, "depart", gate["report"])
            return {**gate["report"], **summary}
        trust = gate["trust"]
        if trust == REDUCED:
            steps.append(
                {
                    "component": "link",
                    "action": "degraded_confirmed",
                    "detail": {"train_id": train_id, "operator": operator},
                }
            )

        gate = self._primary_gate(station_id)
        if gate is not None and not self.lockouts.is_locked(gate.id):
            self.lockouts.set(gate.id, True)
            steps.append(
                {"component": "interlock", "action": "lock", "detail": {"psd_id": gate.id}}
            )
        try:
            closed = close_and_audit(
                self.doors, self.audit, train_id, station_id, DEPART, self.clock.now()
            )
        except RailError as error:
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": [error.message],
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}
        steps.append(
            {"component": "door", "action": "close_durable", "detail": {"train_id": train_id}}
        )
        gap_state = self.gaps.state(train_id)
        steps.append(
            {
                "component": "gap",
                "action": "check",
                "detail": {"train_id": train_id, "state": gap_state},
            }
        )
        verdict = depart_checked(self.signals, self.store, self.gaps, train_id, self.clock.now())
        if not verdict["ok"]:
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": list(verdict["violations"]),
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}
        steps.append(
            {"component": "sig", "action": "depart_signal", "detail": {"train_id": train_id}}
        )
        departure = depart(self.doors, train_id, self.clock.now())
        if gate is not None:
            self.lockouts.clear(gate.id)
        report = {
            "ok": True,
            "train_id": train_id,
            "station_id": station_id,
            "close_durable": bool(closed["close_durable"]),
            "aspect": verdict["aspect"],
            "cleared_commands": departure["cleared_commands"],
            "mode": "degraded" if trust == REDUCED else "normal",
            "trust": trust or "unevaluated",
            "steps": steps,
            "violations": [],
        }
        summary = self._record_operation(op_id, "depart", report)
        self._operations += 1
        self._persist()
        return {**report, **summary}

    def check_gap(self, train_id: str, clear: bool = True) -> dict:
        """确认间隙无异常，或登记一次可疑探测结果。"""

        if clear:
            result = self.gaps.check(train_id, self.clock.now())
        else:
            result = self.gaps.probe(train_id, False, self.clock.now())
        self.audit.record("gap_probe", train_id, "", clear=clear)
        return {"ok": True, "train_id": train_id, "clear": result.clear, "violations": []}

    def renumber(self, station_id: str, groups: dict[str, str]) -> dict:
        """按最新布局重排门区映射，并按新映射联动一次。"""

        mapping = self.mappings.renumber(station_id, groups)
        first_key = sorted(groups)[0]
        linked = open_group(self.doors, self.mappings, station_id, first_key)
        self.audit.record("mapping_renumber", "", station_id, revision=mapping.revision)
        return {
            "ok": True,
            "station_id": station_id,
            "revision": mapping.revision,
            "mapping": mapping.as_dict(),
            "active_group": linked["group"],
            "violations": [],
        }

    def enqueue(self, train_id: str, command_id: str, kind: str, station_id: str = "") -> dict:
        """把一条门控指令排进队列。"""

        command = enqueue(self.doors, train_id, command_id, kind, station_id)
        self.audit.record(
            "door_enqueue", train_id, station_id, command_id=command.id, command_kind=kind
        )
        return {"ok": True, "train_id": train_id, "command": command.as_dict(), "violations": []}

    def process_queue(self, train_id: str) -> dict:
        """返回列车队列里待执行的门控指令，并带上发车自检结果。"""

        policy = depart_policy(self.doors, train_id)
        ids = process_queue(self.doors, train_id)
        self.audit.record("door_process", train_id, "", queued=len(ids))
        return {
            "ok": True,
            "train_id": train_id,
            "ids": ids,
            "ready": policy["allowed"],
            "violations": list(policy["failures"]),
        }

    def clear_fault(self, psd_id: str) -> dict:
        """清除门区故障并按条件解锁。"""

        gate = self.gates.require(psd_id)
        allowed, reason = releasable(self.gates, psd_id, True)
        if not allowed:
            return {"ok": False, "psd_id": psd_id, "locked": True, "violations": [reason]}
        self.faults.clear_fault(self.lockouts, psd_id, self.clock.now())
        self.audit.record("interlock_clear", "", gate.station, psd_id=psd_id)
        return {"ok": True, "psd_id": psd_id, "locked": False, "violations": []}

    def set_fault(self, psd_id: str) -> dict:
        """置位门区故障并锁定。"""

        gate = self.gates.require(psd_id)
        self.faults.set_fault(self.lockouts, psd_id, self.clock.now())
        self.audit.record("interlock_fault", "", gate.station, psd_id=psd_id)
        return {"ok": True, "psd_id": psd_id, "locked": True, "violations": []}

    def arm(self, train_id: str, armed: bool) -> dict:
        """启用或退出一列车的联锁。"""

        state = self.armers.arm(train_id, armed)
        self.audit.record("interlock_arm", train_id, "", armed=state)
        return {"ok": True, "train_id": train_id, "armed": state, "violations": []}

    def refresh_position(
        self,
        train_id: str,
        position_m: int,
        seq: int | None = None,
        rtt_ms: float | None = None,
        sent_at: float | None = None,
    ) -> dict:
        """按信号刷新列车位置。

        位置报文同时是链路证据：先过链路监控（写前日志），再按当前可信度
        决定怎么用这个位置——降级时接受但打标，彻底失联时保持最后可信值
        并明确标注（退避），不再静默冻结看着像车没动。
        """

        now = self.clock.now()
        with self._lock_for(train_id):
            verdict = self.link.ingest(
                train_id, seq=seq, rtt_ms=rtt_ms, sent_at=sent_at, now=now
            )
            self._audit_link_decisions(train_id, verdict["decisions"])
            if not verdict["counted"]:
                return {
                    "ok": False,
                    "train_id": train_id,
                    "reason": verdict["note"] or "ignored",
                    "trust": verdict["trust"],
                    "violations": [],
                }
            if verdict["trust"] == UNTRUSTED:
                self.audit.record(
                    "position_held",
                    train_id,
                    "",
                    quality=verdict["quality"],
                    state=verdict["state"],
                    backoff_level=verdict["backoff_level"],
                )
                return {
                    "ok": False,
                    "train_id": train_id,
                    "reason": "link_lost",
                    "trust": verdict["trust"],
                    "violations": ["车地链路失联，位置保持最后可信值"],
                }
            outcome = refresh_position(self.positions, train_id, position_m, now)
            if outcome["ok"]:
                self._write_trust(train_id, verdict)
                self.trend.append("train_position_m", now, position_m)
            else:
                self.audit.record("position_rejected", train_id, "", reason=outcome["reason"])
            return {
                **outcome,
                "trust": verdict["trust"],
                "quality": verdict["quality"],
                "violations": [] if outcome["ok"] else [outcome["reason"]],
            }

    def link_sample(
        self,
        train_id: str,
        ok: bool = True,
        seq: int | None = None,
        rtt_ms: float | None = None,
        sent_at: float | None = None,
    ) -> dict:
        """登记一条纯链路采样（无线前端心跳，不带位置）。"""

        now = self.clock.now()
        with self._lock_for(train_id):
            verdict = self.link.ingest(
                train_id, ok=ok, seq=seq, rtt_ms=rtt_ms, sent_at=sent_at, now=now
            )
            self._audit_link_decisions(train_id, verdict["decisions"])
        return {
            "ok": True,
            "train_id": train_id,
            "counted": verdict["counted"],
            "note": verdict["note"],
            "state": verdict["state"],
            "trust": verdict["trust"],
            "quality": verdict["quality"],
            "violations": [],
        }

    def link_summary(self) -> dict:
        """返回全部链路会话的汇总视图。"""

        return self.link.summary(now=self.clock.now())

    def link_view(self, train_id: str) -> dict | None:
        """返回一列车的链路详情；无会话时返回 ``None``。"""

        view = self.link.describe(train_id, self.clock.now())
        if view is None:
            return None
        view["trust_doc"] = self._trust_view(train_id)
        return view

    def replay_link(self, train_id: str) -> dict:
        """重放一列车的链路判定：凭什么这么判，当场重算给你看。"""

        return replay_train(self.link.journal.read_all(), train_id, self.link.tuning)

    def sequence_verdict(self, previous: int, step: int) -> dict:
        """推进门控事件序号并给出回绕判定。"""

        current = next_sequence(previous, step)
        verdict = verdict_of(previous, current)
        self.audit.record(
            "sequence_verdict",
            "",
            "",
            previous=verdict.previous,
            current=verdict.current,
            advanced=verdict.advanced,
        )
        return {"ok": verdict.advanced, "verdict": verdict.as_dict(), "violations": [] if verdict.advanced else [verdict.reason]}

    def batch_open(self, station_id: str) -> dict:
        """按布局里程顺序批量开门。"""

        layout = self.layouts.get(station_id)
        zones = list(layout.zones) if layout else []
        outcome = batch_open(self.doors, station_id, zones)
        nearest = nearest_zone(zones)
        self.audit.record(
            "batch_open",
            "",
            station_id,
            opened=len(outcome["opened"]),
            skipped=len(outcome["skipped"]),
        )
        return {
            "ok": not outcome["skipped"],
            "station_id": station_id,
            "order": outcome["order"],
            "opened": outcome["opened"],
            "deferred": outcome["deferred"],
            "skipped": outcome["skipped"],
            "nearest": nearest.id if nearest else "",
            "violations": [],
        }

    def sweep(self, station_id: str) -> dict:
        """整站清扫。"""

        return sweep_station(self.gates, station_id)

    def register_dwell(self, train_id: str, seconds: float, station_id: str = "") -> dict:
        """登记停站时长。"""

        dwell = self.dwells.set(Dwell(train_id=train_id, seconds=seconds, station_id=station_id))
        return {"ok": True, "dwell": dwell.as_dict(), "violations": []}

    def assign_route(self, train_id: str, origin: str, destination: str, distance_m: int) -> dict:
        """登记运行交路。"""

        route = self.routes.assign(
            Route(train_id=train_id, origin=origin, destination=destination, distance_m=distance_m)
        )
        return {"ok": True, "route": route.__dict__, "violations": []}

    # ---- 控制循环 -----------------------------------------------------

    def cycle(self, dt: float = 1.0) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        if dt <= 0:
            return {"ok": False, "violations": ["步长必须为正"], "cycle": self._cycles}
        self._cycles += 1
        moment = self.clock.now()
        stale = [
            train.id for train in self.trains.list() if self.positions.stale(train.id, moment)
        ]
        link_tick = self.link.tick(moment)
        for train_id, info in link_tick.items():
            self._audit_link_decisions(train_id, info["decisions"])
        link_summary = self.link.summary(now=moment)
        interlock = interlock_status(self.faults, self.gates, self.config.interlock.max_locked_ratio)
        holding = [
            train.id
            for train in self.trains.list()
            if not self.armers.armed(train.id) and depart_policy(self.doors, train.id)["allowed"]
        ]
        self.trend.append("locked_count", moment, interlock["locked_count"])
        self.trend.append("queued_commands", moment, queued_total(self.doors))
        self.trend.append("audit_entries", moment, len(self.audit.list()))
        self._persist()
        return {
            "ok": True,
            "cycle": self._cycles,
            "dt": dt,
            "stale_positions": stale,
            "locked_count": interlock["locked_count"],
            "can_release_depart": interlock["can_release_depart"],
            "ready_trains": holding,
            "gaps": gap_summary(self.gaps, [train.id for train in self.trains.list()]),
            "link": link_summary,
            "violations": [],
        }

    # ---- 查询 ---------------------------------------------------------

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        trains = self.trains.list()
        ids = [train.id for train in trains]
        return {
            "platform": self.config.name,
            "unit": self.unit,
            "line": self.namespace.label(),
            "operations": self._operations,
            "cycles": self._cycles,
            "utc": self.clock.utc_iso(),
            "namespace": self.namespace.as_dict(),
            "namespaces": self.namespaces.describe(),
            "open_gates": self.gates.count_open(),
            "lockout_changes": self.lockouts.changes,
            "signals_raised": self.signals.raises,
            "signals_cleared": self.signals.clears,
            "interlock_counters": {
                "faults_set": self.faults.sets,
                "faults_cleared": self.faults.clears,
                "armed": self.armers.arms,
                "disarmed": self.armers.disarms,
                "armed_trains": self.armers.armed_trains(),
            },
            "gap_engine": {"checks": self.gaps.checks, "probes": self.gaps.probes},
            "event_counts": dict(sorted(self._event_counts.items())),
            "console_hits": len(self.bus.history("console")),
            "stations": [station.as_dict() for station in self.stations.list()],
            "trains": {train.id: train_state(train.id, self.align, self.train_doors, self.positions) for train in trains},
            "doors": [
                {**gate.as_dict(), "open": gate.open, "locked": gate.locked}
                for gate in self.gates.list()
            ],
            "door_report": door_report(self.doors, self.namespace.station_ids()),
            "signals": signal_summary(self.signals, ids),
            "interlock": interlock_status(
                self.faults, self.gates, self.config.interlock.max_locked_ratio
            ),
            "gaps": gap_summary(self.gaps, ids),
            "link": self.link.summary(now=self.clock.now()),
            "quota": {
                ARRIVAL: self.quotas.status(ARRIVAL),
                DEPART: self.quotas.status(DEPART),
            },
            "store": self.metrics.summary(),
            "audit": self.audit.summary(),
            "trend": self.trend.snapshot(limit=4),
            "ids_issued": self.ids.issued(),
        }

    def snapshot(self) -> dict:
        """返回只读的整体快照（状态 + 命名空间 + 门区映射 + 审计时间线）。"""

        return {
            "status": self.status(),
            "groups": {
                station_id: group_keys(self.mappings, station_id)
                for station_id in self.namespace.station_ids()
            },
            "physical_groups": {
                station_id: physical_groups(self.mappings, station_id)
                for station_id in self.namespace.station_ids()
            },
            "mapping_revisions": {
                station_id: {
                    "latest": self.mappings.history(station_id).latest(),
                    "log": self.mappings.history(station_id).as_dict()["revisions"],
                }
                for station_id in self.namespace.station_ids()
            },
            "adjacent": self._adjacent_sample(),
            "timeline": build_timeline(self.audit.list(), limit=20),
        }

    def train_view(self, train_id: str) -> dict:
        """返回一列车的综合视图。"""

        return {
            "train": self.trains.get(train_id).as_dict() if self.trains.get(train_id) else None,
            "state": train_state(train_id, self.align, self.train_doors, self.positions),
            "doors": door_state(self.doors, train_id),
            "aspect": aspect_of(self.signals, train_id),
            "gap": self.gaps.state(train_id),
            "trust": self._trust_view(train_id),
            "position_trust": self.store.read(f"train/{train_id}/trust"),
            "link": self.link.describe(train_id, self.clock.now()),
        }

    def station_view(self, station_id: str) -> dict:
        """返回一个站台的综合视图。"""

        layout = self.layouts.get(station_id)
        mapping = self.mappings.current(station_id)
        zones = list(layout.zones) if layout else []
        schedule = self.schedules.get(station_id)
        return {
            "station": self.stations.get(station_id).as_dict() if self.stations.get(station_id) else None,
            "group_count": self.stations.group_count(station_id),
            "zones": [zone.as_dict() for zone in zones],
            "order": [zone.id for zone in zones],
            "zone_distances": {
                zone.id: self.layouts.zone_distance(station_id, zone.id) for zone in zones
            },
            "span_m": spread_m(zones),
            "minimum_headway_s": self.schedules.minimum_headway(station_id) if schedule else 0,
            "revision": mapping.revision if mapping else 0,
            "groups": group_keys(self.mappings, station_id),
            "active_group": active_group(self.doors, station_id),
            "pressure": open_pressure(self.doors, station_id),
        }

    def resolve_group(self, station_id: str, group_key: str) -> dict:
        """解析一个逻辑门区。"""

        group = resolve(self.mappings, station_id, group_key)
        return {
            "station_id": station_id,
            "group_key": group_key,
            "group": group,
            "neighbours": adjacent(self.mappings, station_id, group_key),
        }

    def note_error(self, error: RailError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        entry = self.audit.record(
            "error",
            "",
            "",
            component=error.component,
            message=error.message,
            error_type=type(error).__name__,
        )
        return entry.as_dict()

    def reset_state(self) -> bool:
        """清掉落盘的平台计数（交付前重新标定用）。"""

        if not self.store.exists("platform/state"):
            return False
        return self.store.remove("platform/state")

    # ---- 内部 ---------------------------------------------------------

    def _stop_position(self, station_id: str) -> int:
        """返回站台的停稳位置（取最近门区里程）。"""

        layout = self.layouts.get(station_id)
        zones = list(layout.zones) if layout else []
        nearest = nearest_zone(zones)
        return nearest.distance_m if nearest else 0

    def _primary_group(self, station_id: str) -> str:
        """返回站台的首个门区。"""

        keys = group_keys(self.mappings, station_id)
        return keys[0] if keys else ""

    def _primary_gate(self, station_id: str) -> PSD | None:
        """返回站台的首樘站台门。"""

        gates = self.gates.by_station(station_id)
        return gates[0] if gates else None

    def _adjacent_sample(self) -> dict:
        """给快照提供一个相邻关系样例。"""

        station_ids = self.namespace.station_ids()
        if not station_ids:
            return {}
        station_id = station_ids[0]
        keys = group_keys(self.mappings, station_id)
        if not keys:
            return {}
        return {keys[0]: adjacent(self.mappings, station_id, keys[0])}


def _link_tuning(section: LinkSection) -> LinkTuning:
    """把配置小节映射成链路监控器的限值。"""

    return LinkTuning(
        expected_period_s=section.expected_period_s,
        window_s=section.window_s,
        good_threshold=section.good_threshold,
        degraded_threshold=section.degraded_threshold,
        lost_after_s=section.lost_after_s,
        recover_samples=section.recover_samples,
        backoff_base_s=section.backoff_base_s,
        backoff_max_s=section.backoff_max_s,
        max_clock_skew_s=section.max_clock_skew_s,
    )


def build_platform(
    config: PlatformConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str | None = None,
) -> Platform:
    """构造一个平台；``storage_root`` 可覆盖配置里的存储目录。"""

    base = config or default_config()
    if storage_root is not None:
        base = with_storage_root(base, storage_root)
    return Platform(base, clock=clock, unit=unit)
