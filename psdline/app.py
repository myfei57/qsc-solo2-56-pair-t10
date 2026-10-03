"""平台装配与控制循环。

``Platform`` 把命名空间、站台、列车、站台门、信号、联锁、门控、门区映射、间隙
探测、配额与审计按真实调用链装到一起，并暴露四件事：到发编排（到站、发车、
门区联动、批量开门、清扫）、控制循环、状态快照与手动检修动作。

控制链固定为「信号 → 停稳对位 → 开门 → 间隙探测 → 关门 → 发车 → 审计」，
任何一步错位都会被对应组件拒绝，并把拒绝原因落进审计。
"""

from __future__ import annotations

from .audit import AuditLog, build_timeline
from .clock import Clock, SystemClock
from .config import PlatformConfig, default_config, with_storage_root
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
from .errors import RailError
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
from .link import (
    AUTO,
    BLOCKED,
    MANUAL,
    EffectOutbox,
    LinkEventLog,
    LinkSample,
    LinkService,
    RecordingSink,
    ReplayCursor,
    explain_decision,
    replay_train,
    state_as_of,
    verify_chain,
)
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
        # 链路事件独立成一条只追加的哈希链（link-*.jsonl），与审计流水互不覆盖。
        self.link_journal = JsonlJournal(
            store.root, "link", segment_limit=self.config.link.journal_segment
        )
        self.link_log = LinkEventLog(self.link_journal)
        self.link_sink = RecordingSink()
        self.link_outbox = EffectOutbox(self.link_log)
        for kind in (
            "doors_arm",
            "doors_lock",
            "signal_hold",
            "signal_green",
            "link_alarm",
        ):
            self.link_outbox.attach(kind, self.link_sink)
        self.links = LinkService(
            self.config.link, self.clock, self.link_log, self.bus, self.link_outbox
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
        self._pending_link_release: tuple[str, str] | None = None
        self._event_counts: dict[str, int] = {}
        self.bus.subscribe("*", self._on_event)
        self._register_namespaces()
        self._seed(store)
        self._restore()
        # 部分失败恢复：把事件流里"已登记但未确认投递"的副作用幂等重投。
        self._link_redriven = self.links.redrive_pending()

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
            ("line", "link", "train-ground-radio"),
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
        """从落盘状态恢复操作计数与锁定状态。"""

        state = self.store.read("platform/state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))
        restore_lockouts(self.faults, self.gates, self.lockouts, not self.gaps.blocked_ids())

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

    def arrive(self, train_id: str, station_id: str, position_m: int) -> dict:
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
            self.doors_open(train_id, station_id)
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

    def doors_open(self, train_id: str, station_id: str) -> dict:
        """开门并确认车门：对位必须先落盘，且链路放行闸门允许开门。

        链路未纳入管控的车闸门不介入；降级车默认锁闭，只有当次人工确认
        （:meth:`confirm_departure`）后才放行开门；彻底失联直接拒绝。
        """

        allowed, rule, adopted = self.links.can_open_doors(train_id, self.clock.now())
        if not allowed:
            self.audit.record(
                "link_doors_blocked", train_id, station_id, rule=rule
            )
            raise RailError(
                f"链路降级，开门联锁默认锁闭（{rule}）；须值班员当次确认",
                component="link",
            )
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

    def depart(self, train_id: str, station_id: str) -> dict:
        """关门到发车：关门指令 → 间隙探测 → 关门落盘 → 发车信号。"""

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

        # 链路放行闸门：未纳入管控的车不介入；否则必须自动档或已人工确认/紧急覆盖。
        link_block = self._link_depart_gate(train_id, station_id, steps)
        if link_block is not None:
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": [link_block],
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}

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
        # 发车完成：回收本次链路放行授权（开门联锁只对当次有效）。
        if self._pending_link_release is not None:
            released_train, request_id = self._pending_link_release
            if released_train == train_id:
                self.links.close_release(train_id, request_id, self.clock.now())
            self._pending_link_release = None
        report = {
            "ok": True,
            "train_id": train_id,
            "station_id": station_id,
            "close_durable": bool(closed["close_durable"]),
            "aspect": verdict["aspect"],
            "cleared_commands": departure["cleared_commands"],
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

    def refresh_position(self, train_id: str, position_m: int) -> dict:
        """按信号刷新列车位置。"""

        outcome = refresh_position(self.positions, train_id, position_m, self.clock.now())
        if outcome["ok"]:
            self.trend.append("train_position_m", self.clock.now(), position_m)
        else:
            self.audit.record("position_rejected", train_id, "", reason=outcome["reason"])
        return {**outcome, "violations": [] if outcome["ok"] else [outcome["reason"]]}

    # ---- 车地无线链路质量 ---------------------------------------------

    def link_sample(
        self,
        train_id: str,
        seq: int,
        epoch: str,
        rssi_dbm: float = -55.0,
        snr_db: float = 25.0,
        rtt_ms: float = 120.0,
        packet_loss: float = 0.0,
        position_m: int | None = None,
        sent_at: float | None = None,
    ) -> dict:
        """接收一帧车地无线采样；链路可信时才顺带刷新位置，否则位置冻结。

        这就是"车地无线一断、位置不能再假装停在最后一个值"的入口：位置是否落盘
        由链路可信度决定，返回里同时给出 ``position_written`` 与可信度。
        """

        moment = self.clock.now()
        if self.trains.get(train_id) is None:
            self.trains.upsert(Train(id=train_id, line=self.namespace.line_id()))
        sample = LinkSample(
            train_id=train_id,
            seq=int(seq),
            epoch=str(epoch),
            sent_at=moment if sent_at is None else float(sent_at),
            at=moment,
            rssi_dbm=float(rssi_dbm),
            snr_db=float(snr_db),
            rtt_ms=float(rtt_ms),
            packet_loss=float(packet_loss),
            position_m=None if position_m is None else int(position_m),
        )
        outcome = self.links.ingest(sample)
        if outcome.get("accepted"):
            if outcome.get("position_written") and position_m is not None:
                refresh_position(self.positions, train_id, int(position_m), moment)
                self.trend.append("train_position_m", moment, int(position_m))
                self.audit.record(
                    "link_position_applied", train_id,
                    self._station_of(train_id), confidence=outcome["confidence"],
                    position_m=int(position_m), epoch=outcome["epoch"], seq=outcome["seq"],
                )
            elif position_m is not None:
                self.audit.record(
                    "link_position_held", train_id, self._station_of(train_id),
                    confidence=outcome["confidence"], contact=outcome["contact"],
                    position_m=int(position_m),
                )
        else:
            self.audit.record(
                "link_sample_rejected", train_id, self._station_of(train_id),
                reason=outcome.get("reason", "rejected"),
            )
        view = self.links.view(train_id, moment)
        return {
            "ok": bool(outcome.get("accepted")),
            "train_id": train_id,
            "accepted": outcome.get("accepted", False),
            "reason": outcome.get("reason"),
            "score": outcome.get("score"),
            "confidence": view["confidence"],
            "contact": view["contact"],
            "mode": view["mode"],
            "position_written": bool(outcome.get("position_written")),
            "position_m": self.positions.get(train_id),
            "frozen": not outcome.get("position_written", False),
            "violations": [] if outcome.get("accepted") else [outcome.get("reason", "rejected")],
        }

    def link_view(self, train_id: str) -> dict:
        """返回一列车的链路/可信度/位置视图。"""

        return self.links.view(train_id, self.clock.now())

    def link_tick(self, train_id: str | None = None) -> dict:
        """推进链路控制周期（失联退避升级、探测调度、恢复再判定）。"""

        moment = self.clock.now()
        if train_id:
            result = {train_id: self.links.tick(train_id, moment)}
        else:
            result = self.links.tick_all(moment)
        return {"ok": True, "trains": result, "violations": []}

    def link_redrive(self) -> dict:
        """重投未确认的链路副作用（部分失败恢复）。"""

        return {"ok": True, **self.links.redrive_pending(self.clock.now())}

    def confirm_departure(self, train_id: str, request_id: str, operator: str) -> dict:
        """降级期间值班员对当次发车的人工确认（默认锁闭，确认后才开门放行）。"""

        result = self.links.confirm_release(train_id, request_id, operator, self.clock.now())
        self.audit.record(
            "link_manual_confirm", train_id, self._station_of(train_id),
            request_id=request_id, operator=operator, ok=bool(result.get("ok")),
            violations=list(result.get("violations", [])),
        )
        return result

    def emergency_departure(
        self, train_id: str, station_id: str, supervisor: str, reason: str
    ) -> dict:
        """彻底失联时高权限紧急覆盖旁路（强审计、双人责任留痕）。"""

        result = self.links.emergency_override(
            train_id, station_id, supervisor, reason, self.clock.now()
        )
        self.audit.record(
            "link_emergency_override", train_id, station_id,
            request_id=result.get("request_id"), supervisor=supervisor, reason=reason,
            ok=bool(result.get("ok")),
        )
        return result

    def link_release(self, train_id: str, station_id: str) -> dict:
        """只做链路放行判定（不联动既有门控流程），供控制台/巡检单独观察。"""

        return self.links.request_release(train_id, station_id, self.clock.now())

    def close_link_release(self, train_id: str, request_id: str) -> dict:
        """发车完成后关闭一次链路放行请求，回收当次开门授权。"""

        return self.links.close_release(train_id, request_id, self.clock.now())

    def link_replay(self, train_id: str) -> dict:
        """重放一列车的完整链路事件链。"""

        return {"train_id": train_id, "timeline": replay_train(self.link_log, train_id, self.config.link)}

    def link_state_as_of(self, train_id: str, event_seq: int) -> dict:
        """把一列车的链路状态重放到指定全局事件序号（含），还原"那一刻"。

        用游标逐事件走到该序号并取当时视图，可与 :meth:`link_explain` 交叉核对，
        回答"在第 N 条事件发生后，系统眼里这列车处于什么接触/可信度/放行档"。
        """

        target = self.link_log.find(int(event_seq))
        if target is None:
            return {"ok": False, "reason": "event_not_found", "event_seq": event_seq}
        state = state_as_of(self.link_log, train_id, int(event_seq), self.config.link)
        cursor = ReplayCursor.begin(train_id, self.config.link)
        for event in self.link_log.for_train(train_id):
            if event.seq > int(event_seq):
                break
            cursor.step(event)
        return {
            "ok": True,
            "train_id": train_id,
            "as_of_event_seq": int(event_seq),
            "at": target.at,
            "view": state.view(target.at, self.links.policy),
            "cursor_seq": cursor.seq,
        }

    def link_explain(self, event_seq: int) -> dict:
        """解释一次放行判定当时凭什么这么判。"""

        event = self.link_log.find(int(event_seq))
        if event is None:
            return {"ok": False, "reason": "event_not_found", "event_seq": event_seq}
        return {"ok": True, **explain_decision(event, self.link_log)}

    def link_verify(self) -> dict:
        """校验链路事件哈希链完整（历史不可变）。"""

        return self.link_log.verify()

    def _station_of(self, train_id: str) -> str:
        train = self.trains.get(train_id)
        return train.station if train else ""

    def _link_depart_gate(self, train_id: str, station_id: str, steps: list[dict]) -> str | None:
        """发车前的链路闸门核对。

        * 未纳入链路管控（从未上报采样）：返回 ``None``，闸门不介入，兼容存量车；
        * 自动档：放行，并在事件流上登记本次自动放行；
        * 降级档：必须已有当次人工确认/紧急覆盖产生的开放授权，否则拒绝；
        * 彻底失联：拒绝（紧急覆盖授权同样可放行）。

        返回 ``None`` 表示通过，否则返回拒绝原因。
        """

        if not self.links.is_adopted(train_id):
            return None
        view = self.links.view(train_id, self.clock.now())
        open_request = view.get("open_request")
        if open_request is None:
            decision = self.links.request_release(train_id, station_id, self.clock.now())
            verdict_decision = decision.get("decision")
            request_id = decision.get("request_id")
        else:
            verdict_decision = open_request.get("decision")
            request_id = open_request.get("request_id")

        if verdict_decision in ("auto_granted", "manual_armed", "emergency_override"):
            steps.append(
                {
                    "component": "link",
                    "action": "release",
                    "detail": {
                        "train_id": train_id,
                        "decision": verdict_decision,
                        "confidence": view["confidence"],
                    },
                }
            )
            self._pending_link_release = (train_id, request_id)
            self.audit.record(
                "link_release_passed", train_id, station_id,
                request_id=request_id, decision=str(verdict_decision),
                confidence=view["confidence"],
            )
            return None

        reason = {
            "manual_pending": "链路降级，发车按降级处理：默认锁闭，须值班员当次人工确认",
            "inhibited": "车地无线彻底失联，禁止发车；仅高权限紧急覆盖可旁路",
        }.get(str(verdict_decision), f"链路放行未通过（{view.get('mode')}/{verdict_decision}）")
        steps.append(
            {"component": "link", "action": "block",
             "detail": {"train_id": train_id, "decision": str(verdict_decision),
                        "confidence": view["confidence"]}}
        )
        self.audit.record(
            "link_release_blocked", train_id, station_id,
            request_id=request_id, decision=str(verdict_decision),
            confidence=view["confidence"],
        )
        return reason

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
        link_tick = self.links.tick_all(moment)
        stale = [
            train.id for train in self.trains.list() if self.positions.stale(train.id, moment)
        ]
        link_degraded = [
            train_id
            for train_id, view in self.links.views(moment).items()
            if view["mode"] != "auto"
        ]
        link_lost = [
            train_id
            for train_id, view in self.links.views(moment).items()
            if view["contact"] == "lost"
        ]
        # 位置陈旧但链路仍显示自动可信是矛盾的，周期里显式暴露，防止"假在动"。
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
            "link_tick": link_tick,
            "link_degraded": link_degraded,
            "link_lost": link_lost,
            "locked_count": interlock["locked_count"],
            "can_release_depart": interlock["can_release_depart"],
            "ready_trains": holding,
            "gaps": gap_summary(self.gaps, [train.id for train in self.trains.list()]),
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
            "link": {
                train_id: self.links.view(train_id, self.clock.now())
                for train_id in self.links.adopted_trains()
            },
            "link_effects": self.link_sink.effects(),
            "link_events": len(self.link_log),
            "link_pending_on_boot": self._link_redriven,
            "link_chain": self.link_log.verify(),
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
            "link": self.links.view(train_id, self.clock.now()),
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
