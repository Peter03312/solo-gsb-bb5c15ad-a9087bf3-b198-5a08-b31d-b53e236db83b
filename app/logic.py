"""核心业务逻辑：参数校验、线性漂移映射、声段投影排序、版本差异计算。

时间约定：所有时间均为整数毫秒。
- local_ms：设备本地时钟毫秒（如设备开机以来的毫秒数）；
- utc_ms ：Unix 纪元毫秒（UTC）。

对齐算法：每台设备在相邻锚点间按线性漂移把本地时间映射为 UTC：
    utc(t) = u0 + (t - l0) * (u1 - u0) / (l1 - l0)
其中 (l0, u0)、(l1, u1) 为夹住 t 的相邻锚点。结果四舍五入（半数向上）到毫秒整数。
t 恰好等于某锚点本地时间时取该锚点 UTC。落在锚点覆盖范围 [首锚点, 末锚点]
之外的时刻无法映射。
"""
from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left

MAX_INT64 = 2 ** 63 - 1
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

STATUS_LOCATED = "located"
STATUS_UNLOCATABLE = "unlocatable"

# 默认周期：未显式指定 cycle_id 的锚点/声段一律归入此周期。
DEFAULT_CYCLE_ID = "default"


class ApiError(Exception):
    """业务错误，直接映射为 HTTP 错误响应。"""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


# ---------------------------------------------------------------- 参数校验

def require_payload(body) -> dict:
    if body is None:
        raise ApiError(400, "validation_error", "请求体不能为空，必须是 JSON 对象")
    if not isinstance(body, dict):
        raise ApiError(400, "validation_error", "请求体必须是 JSON 对象",
                       {"received_type": type(body).__name__})
    return body


def require_fields(payload: dict, fields: list[str]) -> None:
    missing = [f for f in fields if f not in payload]
    if missing:
        raise ApiError(400, "validation_error",
                       "缺少必填字段: " + ", ".join(missing), {"missing": missing})


def validate_ms(value, field: str) -> int:
    """毫秒时间必须是 [0, 2^63-1] 内的 JSON 整数（布尔、浮点、字符串一律拒绝）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "invalid_time",
                       f"时间字段 {field} 必须是毫秒整数（int64）",
                       {"field": field, "value": value})
    if value < 0 or value > MAX_INT64:
        raise ApiError(400, "invalid_time",
                       f"时间字段 {field} 超出有效范围 [0, {MAX_INT64}]",
                       {"field": field, "value": value})
    return value


def validate_id(value, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ApiError(400, "validation_error",
                       f"{field} 必须以字母或数字开头，仅含字母、数字、_ . - ，长度 1-128",
                       {"field": field, "value": value})
    return value


def validate_cycle_id(value) -> str:
    """周期编号：省略/缺省 -> 默认周期；否则按与设备/采集编号相同的命名规则校验。"""
    if value is None:
        return DEFAULT_CYCLE_ID
    return validate_id(value, "cycle_id")


def validate_note(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 500:
        raise ApiError(400, "validation_error", "note 必须是不超过 500 字符的字符串",
                       {"value": value})
    return value


def validate_fingerprint(value) -> str:
    """封存请求携带的周期内容指纹：64 位小写十六进制（sha256）。"""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ApiError(400, "validation_error",
                       "fingerprint 必须是 64 位小写十六进制字符串（sha256），"
                       "请从 GET /devices/{id}/cycles/{cycle_id} 周期详情中获取",
                       {"value": value})
    return value


def validate_min_devices(value) -> int:
    """覆盖审计门槛 min_devices：正整数（布尔、浮点、字符串、零、负数一律拒绝）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "invalid_threshold",
                       "min_devices 必须是正整数（最少设备数）",
                       {"value": value})
    if value < 1:
        raise ApiError(400, "invalid_threshold",
                       f"min_devices 必须是正整数，收到 {value}",
                       {"value": value})
    return value


# ---------------------------------------------------------------- 漂移映射

def _interpolate(l0: int, u0: int, l1: int, u1: int, t: int) -> int:
    """整数线性插值，四舍五入（半数向上）。分子分母恒非负。"""
    num = (t - l0) * (u1 - u0)
    den = l1 - l0
    q, r = divmod(num, den)
    if 2 * r >= den:
        q += 1
    return u0 + q


def map_local_to_utc(locals_list: list[int], utcs_list: list[int], t: int) -> int | None:
    """把设备本地毫秒映射为 UTC 毫秒；超出锚点覆盖范围返回 None。"""
    n = len(locals_list)
    if n == 0:
        return None
    i = bisect_left(locals_list, t)
    if i < n and locals_list[i] == t:
        return utcs_list[i]
    if i == 0 or i == n:
        return None
    return _interpolate(locals_list[i - 1], utcs_list[i - 1],
                        locals_list[i], utcs_list[i], t)


# ---------------------------------------------------------------- 声段投影

def project_segment(seg: dict, anchors_pair) -> dict:
    """根据设备当前锚点计算声段的 UTC 投影。

    起止任一时刻落在锚点覆盖范围之外 -> status = unlocatable，UTC 字段为 null。
    """
    locals_list, utcs_list = anchors_pair
    utc_start = map_local_to_utc(locals_list, utcs_list, seg["local_start_ms"])
    utc_end = map_local_to_utc(locals_list, utcs_list, seg["local_end_ms"])
    located = utc_start is not None and utc_end is not None
    out = dict(seg)
    out["status"] = STATUS_LOCATED if located else STATUS_UNLOCATABLE
    out["utc_start_ms"] = utc_start if located else None
    out["utc_end_ms"] = utc_end if located else None
    return out


def sort_projections(projs: list[dict]) -> list[dict]:
    """稳定排序：可定位声段按 (UTC 起点, 设备编号, 采集编号) 升序；
    无法定位的声段排在最后，按 (设备编号, 采集编号) 升序。"""

    def key(p: dict):
        if p["status"] == STATUS_LOCATED:
            return (0, p["utc_start_ms"], p["device_id"], p["capture_id"])
        return (1, 0, p["device_id"], p["capture_id"])

    return sorted(projs, key=key)


def segment_content_hash(capture_id: str, device_id: str, cycle_id: str,
                         local_start_ms: int, local_end_ms: int,
                         note: str | None) -> str:
    """声段内容指纹：用于"同编号同内容幂等、异内容冲突"的判定。

    周期计入内容：同采集编号仅当（设备、周期、本地起止、note）全一致才算同内容。
    """
    canonical = json.dumps(
        {"capture_id": capture_id, "device_id": device_id, "cycle_id": cycle_id,
         "local_start_ms": local_start_ms, "local_end_ms": local_end_ms,
         "note": note},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 周期封存

def cycle_fingerprint(device_id: str, cycle_id: str,
                      anchors: list[dict], segments: list[dict]) -> str:
    """周期内容稳定指纹：由该周期全部锚点与声段内容生成（sha256）。

    - 锚点须已按 local_ms 升序、声段须已按 capture_id 升序（调用方保证），
      因此同样的内容无论读取多少次都得到同一个指纹；
    - 声段只计入录入内容（编号、本地起止、note）；UTC 投影由锚点推导，
      锚点已在指纹中，不重复计入；
    - 锚点或声段任何一处增删改都会改变指纹，封存请求据此核对"当前内容"。
    """
    canonical = json.dumps(
        {"device_id": device_id, "cycle_id": cycle_id,
         "anchors": [{"local_ms": a["local_ms"], "utc_ms": a["utc_ms"]}
                     for a in anchors],
         "segments": [{"capture_id": s["capture_id"],
                       "local_start_ms": s["local_start_ms"],
                       "local_end_ms": s["local_end_ms"],
                       "note": s["note"]} for s in segments]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 版本差异

def compute_changes(prev_list: list[dict], curr_list: list[dict]) -> dict:
    """比较相邻两版声段投影，给出新增、移除与位移。

    位移：两版都存在的声段，其定位状态或 UTC 起止发生变化。
    start_shift_ms / end_shift_ms 仅在两版均可定位时给出（毫秒差值），否则为 null。
    """
    prev = {p["capture_id"]: p for p in prev_list}
    curr = {p["capture_id"]: p for p in curr_list}

    added = sorted((curr[c] for c in curr.keys() - prev.keys()),
                   key=lambda p: (p["device_id"], p["capture_id"]))
    removed = sorted((prev[c] for c in prev.keys() - curr.keys()),
                     key=lambda p: (p["device_id"], p["capture_id"]))

    displaced = []
    for c in sorted(curr.keys() & prev.keys()):
        p, n = prev[c], curr[c]
        if (p["status"], p["utc_start_ms"], p["utc_end_ms"]) != \
           (n["status"], n["utc_start_ms"], n["utc_end_ms"]):
            both = p["status"] == STATUS_LOCATED and n["status"] == STATUS_LOCATED
            displaced.append({
                "capture_id": c,
                "device_id": n["device_id"],
                "previous": {"status": p["status"],
                             "utc_start_ms": p["utc_start_ms"],
                             "utc_end_ms": p["utc_end_ms"]},
                "current": {"status": n["status"],
                            "utc_start_ms": n["utc_start_ms"],
                            "utc_end_ms": n["utc_end_ms"]},
                "start_shift_ms": (n["utc_start_ms"] - p["utc_start_ms"]) if both else None,
                "end_shift_ms": (n["utc_end_ms"] - p["utc_end_ms"]) if both else None,
            })
    displaced.sort(key=lambda d: (d["device_id"], d["capture_id"]))
    return {"added": added, "removed": removed, "displaced": displaced}


# ---------------------------------------------------------------- 覆盖审计

def _merge_device_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """同一设备的区间并集：重叠或边界相接（半开语义下首尾相等）的区间合并。"""
    merged: list[list[int]] = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            if b > merged[-1][1]:
                merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def compute_coverage(located: list[dict], start_ms: int, end_ms: int,
                     min_devices: int) -> dict:
    """覆盖审计纯逻辑：判断半开区间 [start_ms, end_ms) 内是否始终有足够
    不同设备录到声音。

    - located：可定位声段投影（至少含 device_id / utc_start_ms / utc_end_ms）；
      只取与查询区间相交的非零长度部分，零长度声段不贡献覆盖；
    - 同设备的重叠（含相接）声段只算一台：先按设备求区间并集；
    - 事件扫描把查询区间切成设备集合恒定的最大半开片段，每片段给出排序后的
      设备编号、数量与是否达标；不足门槛的相邻片段合并为连续缺口；
    - 边界相接不重叠：结束于 t 的声段不覆盖 t 之后，开始于 t 的恰好接上；
    - 无相交声段时整段为一个缺口。
    """
    per_device: dict[str, list[tuple[int, int]]] = {}
    contributing = 0
    for s in located:
        a = max(s["utc_start_ms"], start_ms)
        b = min(s["utc_end_ms"], end_ms)
        if a < b:  # 空交集与零长度片段不贡献覆盖
            per_device.setdefault(s["device_id"], []).append((a, b))
            contributing += 1

    # 事件点：t 时刻先移除结束于 t 的设备、再加入开始于 t 的设备，
    # 得到 [t, 下一事件点) 上恒定的设备集合（半开语义：边界相接不重叠）。
    starts: dict[int, set[str]] = {}
    ends: dict[int, set[str]] = {}
    points = {start_ms, end_ms}
    for device_id, intervals in per_device.items():
        for a, b in _merge_device_intervals(intervals):
            starts.setdefault(a, set()).add(device_id)
            ends.setdefault(b, set()).add(device_id)
            points.add(a)
            points.add(b)
    timeline = sorted(points)

    fragments: list[dict] = []
    active: set[str] = set()
    for i, t in enumerate(timeline):
        for d in ends.get(t, ()):
            active.discard(d)
        for d in starts.get(t, ()):
            active.add(d)
        if i + 1 < len(timeline) and timeline[i + 1] > t:
            fragments.append({"start_ms": t, "end_ms": timeline[i + 1],
                              "device_ids": sorted(active),
                              "device_count": len(active)})

    # 相邻且设备集合相同的片段合并为最大半开片段
    maximal: list[dict] = []
    for f in fragments:
        if maximal and maximal[-1]["device_ids"] == f["device_ids"]:
            maximal[-1]["end_ms"] = f["end_ms"]
        else:
            maximal.append(dict(f))
    for f in maximal:
        f["meets_threshold"] = f["device_count"] >= min_devices

    # 缺口：连续不达标片段合并；记录段内出现过的设备（并集）与峰值设备数
    gaps: list[dict] = []
    for f in maximal:
        if f["meets_threshold"]:
            continue
        if gaps and gaps[-1]["end_ms"] == f["start_ms"]:
            g = gaps[-1]
            g["end_ms"] = f["end_ms"]
            g["device_ids"] = sorted(set(g["device_ids"]) | set(f["device_ids"]))
            g["max_device_count"] = max(g["max_device_count"], f["device_count"])
        else:
            gaps.append({"start_ms": f["start_ms"], "end_ms": f["end_ms"],
                         "device_ids": list(f["device_ids"]),
                         "max_device_count": f["device_count"]})

    covered_ms = sum(f["end_ms"] - f["start_ms"] for f in maximal
                     if f["meets_threshold"])
    return {"ok": not gaps,
            "covered_ms": covered_ms,
            "gap_ms": (end_ms - start_ms) - covered_ms,
            "located_segment_count": contributing,
            "fragments": maximal,
            "gaps": gaps}


# ---------------------------------------------------------------- 回放裁剪

def _first_local_ms_with_utc_geq(locals_list: list[int], utcs_list: list[int],
                                 lo: int, hi: int, threshold: int) -> int | None:
    """在整数本地毫秒 [lo, hi] 上找第一个映射 UTC 不小于 threshold 的时刻。

    可定位声段的本地范围落在锚点覆盖内，映射 UTC 在该范围上单调不减
    （线性漂移 + 毫秒四舍五入保持单调），因此可二分；范围内全部小于
    threshold 时返回 None。
    """
    found = None
    while lo <= hi:
        mid = (lo + hi) // 2
        u = map_local_to_utc(locals_list, utcs_list, mid)
        if u is not None and u >= threshold:
            found = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return found


def clip_segment_to_window(anchors_pair, local_start_ms: int, local_end_ms: int,
                           window_start_ms: int, window_end_ms: int):
    """计算单条声段在 UTC 半开窗口 [window_start_ms, window_end_ms) 内的本地裁剪。

    - 声段须可定位（起止都在所属周期锚点覆盖内）且与窗口相交
      （utc_start < window_end 且 utc_end > window_start，边界相接不算相交），
      否则返回 None；
    - 两个本地边界分别取该声段本地范围内"映射 UTC 首次不小于窗口对应边界"
      的整数毫秒；不存在（声段末端映射仍小于窗口边界）则截在声段末端；
    - 裁后起止相同（无可回放内容）返回 None；
    - 返回 (local_clip_start, local_clip_end, utc_clip_start, utc_clip_end)，
      其中 UTC 起止为裁剪后本地边界按同一映射（线性漂移 + 毫秒四舍五入）的结果。
    """
    locals_list, utcs_list = anchors_pair
    utc_start = map_local_to_utc(locals_list, utcs_list, local_start_ms)
    utc_end = map_local_to_utc(locals_list, utcs_list, local_end_ms)
    if utc_start is None or utc_end is None:
        return None  # 不可定位：起止任一时刻超出锚点覆盖
    if utc_start >= window_end_ms or utc_end <= window_start_ms:
        return None  # 与窗口不相交（半开语义）
    clip_start = _first_local_ms_with_utc_geq(
        locals_list, utcs_list, local_start_ms, local_end_ms, window_start_ms)
    if clip_start is None:
        clip_start = local_end_ms  # 不存在 -> 截在声段末端
    clip_end = _first_local_ms_with_utc_geq(
        locals_list, utcs_list, local_start_ms, local_end_ms, window_end_ms)
    if clip_end is None:
        clip_end = local_end_ms  # 不存在 -> 截在声段末端
    if clip_start >= clip_end:
        return None  # 裁后起止相同，不返回
    return (clip_start, clip_end,
            map_local_to_utc(locals_list, utcs_list, clip_start),
            map_local_to_utc(locals_list, utcs_list, clip_end))


def compute_clips(segments: list[dict], anchors_map: dict,
                  window_start_ms: int, window_end_ms: int) -> list[dict]:
    """回放裁剪纯逻辑：裁出每条与窗口相交且可定位声段的可回放片段。

    - segments：至少含 capture_id / device_id / cycle_id / local_start_ms /
      local_end_ms（当前时间轴的实时投影或版本冻结快照均可）；
    - anchors_map：(device_id, cycle_id) -> (locals_list, utcs_list)，
      声段只使用所属周期的锚点做线性漂移映射（含现有毫秒四舍五入规则）；
    - 同设备重叠声段分别返回（不做任何合并）；
    - 结果按 (UTC 起点, 设备编号, 采集编号) 升序。
    """
    clips = []
    for seg in segments:
        pair = anchors_map.get((seg["device_id"], seg["cycle_id"]), ([], []))
        clip = clip_segment_to_window(pair, seg["local_start_ms"],
                                      seg["local_end_ms"],
                                      window_start_ms, window_end_ms)
        if clip is None:
            continue
        local_start, local_end, utc_start, utc_end = clip
        clips.append({"capture_id": seg["capture_id"],
                      "device_id": seg["device_id"],
                      "cycle_id": seg["cycle_id"],
                      "local_start_ms": local_start,
                      "local_end_ms": local_end,
                      "utc_start_ms": utc_start,
                      "utc_end_ms": utc_end})
    clips.sort(key=lambda c: (c["utc_start_ms"], c["device_id"], c["capture_id"]))
    return clips
