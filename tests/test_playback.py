"""端到端测试：回放片段裁剪（playback clips，v1.5）。

覆盖：
- 基本裁剪：只计可定位声段并与 UTC 半开窗口相交；恒等/线性漂移映射与
  四舍五入取整；窗口起终点分别把本地边界裁到"映射 UTC 首次不小于对应
  窗口边界"的整数毫秒；终点在声段范围内不存在时截在声段末端；
- 半开语义：仅首尾相接（窗口起点=声段终点 / 窗口终点=声段起点）不返回；
  不可定位、零长度声段不返回；
- 同设备重叠声段分别返回（不去重）；按 (UTC 起点, 设备编号, 采集编号) 排序；
- 整数毫秒分辨率：映射存在平台或陡坡时，裁剪后起止相同的声段不返回；
  裁剪后的 UTC 起止可能略超出窗口边界（如实返回）；
- 各周期独立映射（多周期锚点不串用）；窗口越过末锚点覆盖范围的部分按
  声段末端裁；
- 指定 version_no 的历史裁剪只使用发布时冻结的锚点与声段：后续锚点更正、
  声段增删、周期归属纠正与新发布都不改动历史结果；旧版快照（format_rev=1）
  缺失的周期归属按 default 处理；省略 version_no 查询当前时间轴；
- 输入校验：时间须为 [0, 2^63-1] 整数毫秒、起点必须小于终点、版本不存在
  -> 明确拒绝且不改动数据；查询本身只读；
- 并发：裁剪查询与锚点更正/发布并发时，每次响应只引用存在的锚点与声段。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, server  # noqa: E402

MAX_INT64 = 2 ** 63 - 1

# v1（无周期）建表脚本，仅用于构造带 format_rev=1 旧版本快照的库
LEGACY_SCHEMA = """
CREATE TABLE devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE anchors (
    device_id   TEXT NOT NULL,
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, local_ms)
);
CREATE TABLE segments (
    capture_id      TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE versions (
    version_no  INTEGER PRIMARY KEY,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE version_anchors (
    version_no  INTEGER NOT NULL,
    device_id   TEXT NOT NULL,
    local_ms    INTEGER NOT NULL,
    utc_ms      INTEGER NOT NULL,
    PRIMARY KEY (version_no, device_id, local_ms)
);
CREATE TABLE version_segments (
    version_no      INTEGER NOT NULL,
    capture_id      TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    local_start_ms  INTEGER NOT NULL,
    local_end_ms    INTEGER NOT NULL,
    note            TEXT,
    status          TEXT NOT NULL,
    utc_start_ms    INTEGER,
    utc_end_ms      INTEGER,
    created_at      TEXT,
    PRIMARY KEY (version_no, capture_id)
);
"""


def request(base, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class PlaybackClipsTestCase(unittest.TestCase):
    server = None
    base = None
    tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.server = server.make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.db_path = os.path.join(self.tmpdir.name, f"{self.id().split('.')[-1]}.db")
        db.init_db(self.db_path)
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    # ------------------------------------------------------------ 工具

    def make_device(self, device_id="D1", name=None):
        payload = {"device_id": device_id}
        if name is not None:
            payload["name"] = name
        return self.req("POST", "/devices", payload)

    def add_anchor(self, device_id, local_ms, utc_ms, cycle_id=None):
        payload = {"local_ms": local_ms, "utc_ms": utc_ms}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        return self.req("POST", f"/devices/{device_id}/anchors", payload)

    def submit(self, capture_id, device_id, start, end, cycle_id=None, note=None):
        payload = {"capture_id": capture_id, "device_id": device_id,
                   "local_start_ms": start, "local_end_ms": end}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        if note is not None:
            payload["note"] = note
        return self.req("POST", "/segments", payload)

    def clips(self, start_ms, end_ms, version_no=None):
        payload = {"start_ms": start_ms, "end_ms": end_ms}
        if version_no is not None:
            payload["version_no"] = version_no
        return self.req("POST", "/playback/clips", payload)

    def make_identity_device(self, device_id):
        """建一台恒等映射设备：锚点 (0,0)-(100000,100000)，本地毫秒即 UTC 毫秒。"""
        self.make_device(device_id)
        self.add_anchor(device_id, 0, 0)
        self.add_anchor(device_id, 100000, 100000)

    # ------------------------------------------------------------ 基本裁剪

    def test_identity_full_and_partial_overlap(self):
        """恒等映射：声段完全覆盖窗口 -> 两端裁到窗口；窗口越过声段末端
        -> 终点截在声段末端。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 1000, 9000)

        # 窗口在声段内部：本地/UTC 裁剪起止即窗口边界
        status, body = self.clips(2000, 8000)
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "current")
        self.assertIsNone(body["version_no"])
        self.assertEqual(body["clips"], [
            {"capture_id": "CAP-1", "device_id": "D1", "cycle_id": "default",
             "clip_local_start_ms": 2000, "clip_local_end_ms": 8000,
             "clip_utc_start_ms": 2000, "clip_utc_end_ms": 8000}])
        self.assertEqual(body["clip_count"], 1)

        # 声段落在窗口内部：裁剪范围即声段本身（终点在范围内不存在
        # 映射 >= 100000 的本地毫秒 -> 截在声段末端 9000）
        status, body = self.clips(0, 100000)
        self.assertEqual(status, 200)
        self.assertEqual(body["clips"][0]["clip_local_start_ms"], 1000)
        self.assertEqual(body["clips"][0]["clip_local_end_ms"], 9000)
        self.assertEqual(body["clips"][0]["clip_utc_start_ms"], 1000)
        self.assertEqual(body["clips"][0]["clip_utc_end_ms"], 9000)

        # 部分重叠：窗口起点早于声段
        status, body = self.clips(0, 5000)
        self.assertEqual(status, 200)
        self.assertEqual(body["clips"][0]["clip_local_start_ms"], 1000)
        self.assertEqual(body["clips"][0]["clip_local_end_ms"], 5000)

        # 部分重叠：窗口终点晚于声段
        status, body = self.clips(5000, 20000)
        self.assertEqual(status, 200)
        self.assertEqual(body["clips"][0]["clip_local_start_ms"], 5000)
        self.assertEqual(body["clips"][0]["clip_local_end_ms"], 9000)
        self.assertEqual(body["clips"][0]["clip_utc_end_ms"], 9000)

    def test_linear_drift_and_rounding(self):
        """线性漂移：锚点 (1000,1000)-(61000,61500)，斜率 1.01。
        边界本地毫秒取映射 UTC 首次不小于窗口边界的整数（四舍五入）。"""
        self.make_device("D1")
        self.add_anchor("D1", 1000, 1000)
        self.add_anchor("D1", 61000, 61500)
        # 声段本地 [0, 100000) 不允许（起点超首锚点不可定位），用 [1000,61000]
        self.submit("CAP-1", "D1", 1000, 61000)

        # utc(t) = 1000 + (t-1000)*60500/60000 = 1000 + (t-1000)*121/120
        # t=2000 -> 1000+1008.333=2008（四舍五入），t=1999 -> 2007.32=2007
        # 窗口起点 UTC 2008：首个映射 >= 2008 的本地毫秒是 2000
        # 窗口终点 UTC 49500：解 1000+(t-1000)*121/120 >= 49500
        #   -> t-1000 >= 48500*120/121 = 48099.17 -> t=49100 映射 49500.83=49501，
        #      t=49099 映射 49499.83=49500（半数向上）-> 终点裁到本地 49099
        status, body = self.clips(2008, 49500)
        self.assertEqual(status, 200)
        clip = body["clips"][0]
        self.assertEqual(clip["clip_local_start_ms"], 2000)
        self.assertEqual(clip["clip_utc_start_ms"], 2008)
        # 首个映射 UTC >= 49500 的本地毫秒；上一个本地毫秒严格小于窗口终点
        self.assertEqual(clip["clip_local_end_ms"], 49099)
        self.assertEqual(clip["clip_utc_end_ms"], 49500)
        prev = clip["clip_local_end_ms"] - 1
        num = (prev - 1000) * 60500
        q, r = divmod(num, 60000)
        prev_utc = 1000 + q + (1 if 2 * r >= 60000 else 0)
        self.assertLess(prev_utc, 49500)  # 上一个本地毫秒严格小于窗口终点

    def test_window_extends_past_anchor_coverage(self):
        """窗口越过末锚点：声段末端可映射，终点仍按声段末端裁；
        超覆盖的不可定位声段不返回。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 1000, 50000)
        self.submit("CAP-OUT", "D1", 150000, 200000)  # 超出锚点覆盖 [0,100000]

        status, body = self.clips(40000, 200000)
        self.assertEqual(status, 200)
        self.assertEqual([c["capture_id"] for c in body["clips"]], ["CAP-1"])
        clip = body["clips"][0]
        self.assertEqual(clip["clip_local_start_ms"], 40000)
        self.assertEqual(clip["clip_local_end_ms"], 50000)
        self.assertEqual(clip["clip_utc_end_ms"], 50000)

    def test_half_open_touching_not_returned(self):
        """半开语义：仅边界相接不算相交，不返回片段。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 1000, 5000)

        # 窗口起点 = 声段终点（声段不覆盖 5000）
        status, body = self.clips(5000, 9000)
        self.assertEqual(status, 200)
        self.assertEqual(body["clips"], [])
        self.assertEqual(body["clip_count"], 0)

        # 窗口终点 = 声段起点（窗口不覆盖 1000）
        status, body = self.clips(0, 1000)
        self.assertEqual(status, 200)
        self.assertEqual(body["clips"], [])

        # 真正重叠一毫秒则返回
        status, body = self.clips(4999, 6000)
        self.assertEqual(status, 200)
        self.assertEqual([c["capture_id"] for c in body["clips"]], ["CAP-1"])

    def test_unlocatable_and_zero_length_ignored(self):
        """不可定位声段与零长度声段（本地起=止）都不产生回放片段。"""
        self.make_identity_device("D1")
        self.submit("CAP-OK", "D1", 1000, 4000)
        self.submit("CAP-OUT", "D1", 200000, 300000)
        self.submit("CAP-ZERO", "D1", 2000, 2000)

        status, body = self.clips(0, 100000)
        self.assertEqual(status, 200)
        ids = [c["capture_id"] for c in body["clips"]]
        self.assertEqual(ids, ["CAP-OK"])

    def test_same_device_overlap_returned_separately(self):
        """同设备重叠声段分别返回（不做并集/去重）。"""
        self.make_identity_device("D1")
        self.submit("CAP-A", "D1", 1000, 6000)
        self.submit("CAP-B", "D1", 3000, 9000)

        status, body = self.clips(0, 10000)
        self.assertEqual(status, 200)
        self.assertEqual(
            [(c["capture_id"], c["clip_local_start_ms"], c["clip_local_end_ms"])
             for c in body["clips"]],
            [("CAP-A", 1000, 6000), ("CAP-B", 3000, 9000)])

    def test_sorting_by_utc_device_capture(self):
        """按 (映射后 UTC 起点, 设备编号, 采集编号) 升序；注册顺序无关。"""
        self.make_identity_device("D3")
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("CAP-9", "D3", 8000, 9000)
        self.submit("CAP-1", "D1", 1000, 2000)
        self.submit("CAP-3", "D1", 1000, 2000)  # 同设备同时刻，按采集号排
        self.submit("CAP-2", "D2", 1000, 2000)

        status, body = self.clips(0, 10000)
        self.assertEqual(status, 200)
        self.assertEqual(
            [(c["clip_utc_start_ms"], c["device_id"], c["capture_id"])
             for c in body["clips"]],
            [(1000, "D1", "CAP-1"), (1000, "D1", "CAP-3"),
             (1000, "D2", "CAP-2"), (8000, "D3", "CAP-9")])

    def test_integer_resolution_plateau_dropped_and_overshoot(self):
        """整数毫秒分辨率：映射平台导致窗口内无任何整数本地毫秒时，
        裁剪后起止相同 -> 不返回；可裁时 UTC 终点可能越过窗口边界（如实返回）。"""
        # 陡坡：本地 [0,10] 映射到 UTC [0,1000]（每本地毫秒跨 100 UTC 毫秒）
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 10, 1000)
        # 再补一个末锚点，声段可以延伸
        self.add_anchor("D1", 100, 10000)
        # 声段 [0,100]：utc(0)=0, utc(1)=100, ..., utc(10)=1000
        self.submit("CAP-A", "D1", 0, 100)

        # 窗口 [150,160)：首个映射 >=150 的本地毫秒是 2（utc=200），
        # 首个 >=160 的也是 2 -> 裁剪起止同为 2 -> 不返回
        status, body = self.clips(150, 160)
        self.assertEqual(status, 200)
        self.assertEqual(body["clips"], [])

        # 窗口 [50,250)：起点裁到本地 1（utc=100 >=50），终点裁到本地 3（utc=300），
        # UTC 终点 300 越过窗口终点 250（设备整数分辨率所限，如实返回）
        status, body = self.clips(50, 250)
        self.assertEqual(status, 200)
        clip = body["clips"][0]
        self.assertEqual(clip["clip_local_start_ms"], 1)
        self.assertEqual(clip["clip_local_end_ms"], 3)
        self.assertEqual(clip["clip_utc_start_ms"], 100)
        self.assertEqual(clip["clip_utc_end_ms"], 300)

    def test_flat_plateau_at_segment_start_dropped(self):
        """浅斜率平台：多个本地毫秒映射同一 UTC 整数。窗口终点落在该平台上
        且与起点落入同一平台时，起止相同 -> 不返回；跨平台则正常裁剪。"""
        # 浅坡：本地 [0,4] 映射到 UTC [0,2]，即 utc: 0,1,1,2,2（四舍五入）
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 4, 2)
        self.submit("CAP-A", "D1", 0, 4)

        # 窗口 [0,2)：首个映射 >=0 的本地是 0；首个映射 >=2 的本地是 3（utc(3)=2）
        # -> 片段 [0,3)，UTC 映射 [0,2)，正常返回
        status, body = self.clips(0, 2)
        self.assertEqual(status, 200)
        clip = body["clips"][0]
        self.assertEqual((clip["clip_local_start_ms"], clip["clip_local_end_ms"]),
                         (0, 3))
        self.assertEqual((clip["clip_utc_start_ms"], clip["clip_utc_end_ms"]),
                         (0, 2))

        # 窗口 [1,2)：首个映射 >=1 的本地是 1（utc(1)=1，半数向上），
        # 首个 >=2 仍是 3 -> [1,3) 返回
        status, body = self.clips(1, 2)
        self.assertEqual(status, 200)
        clip = body["clips"][0]
        self.assertEqual((clip["clip_local_start_ms"], clip["clip_local_end_ms"]),
                         (1, 3))

        # 窗口 [2,3)：首个映射 >=2 的本地是 3（utc(3)=2），首个 >=3 在声段
        # 范围内不存在 -> 终点截在声段末端 4 -> 片段 [3,4)（半开相接：
        # 窗口起点 2 恰好是声段覆盖到的整数时刻，仅该整数毫秒粒度下
        # 本地 3 这一帧映射到 2）
        status, body = self.clips(2, 3)
        self.assertEqual(status, 200)
        clip = body["clips"][0]
        self.assertEqual((clip["clip_local_start_ms"], clip["clip_local_end_ms"]),
                         (3, 4))

    def test_per_cycle_mapping_independent(self):
        """声段只按所属周期冻结锚点映射，不串用其他周期锚点。"""
        self.make_device("D1")
        # default：恒等
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 100000, 100000)
        # boot-2：整体平移 +1000000
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.add_anchor("D1", 0, 1000000, cycle_id="boot-2")
        self.add_anchor("D1", 100000, 1100000, cycle_id="boot-2")
        self.submit("CAP-DEF", "D1", 1000, 9000)
        self.submit("CAP-B2", "D1", 1000, 9000, cycle_id="boot-2")

        status, body = self.clips(0, 50000)
        self.assertEqual(status, 200)
        by_id = {c["capture_id"]: c for c in body["clips"]}
        self.assertEqual(set(by_id), {"CAP-DEF"})  # boot-2 声段在 1000001 起，不相交

        status, body = self.clips(1000000, 1005000)
        by_id = {c["capture_id"]: c for c in body["clips"]}
        self.assertEqual(set(by_id), {"CAP-B2"})
        clip = by_id["CAP-B2"]
        self.assertEqual(clip["cycle_id"], "boot-2")
        self.assertEqual((clip["clip_local_start_ms"], clip["clip_local_end_ms"]),
                         (1000, 5000))
        self.assertEqual((clip["clip_utc_start_ms"], clip["clip_utc_end_ms"]),
                         (1001000, 1005000))

    # ------------------------------------------------------------ 版本裁剪

    def test_version_clips_frozen_history(self):
        """历史裁剪只使用发布时冻结的锚点与声段：发布后的锚点更正、声段
        增删、周期归属纠正与新发布都不改动历史结果；当前视图反映最新状态。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("CAP-1", "D1", 0, 10000)
        self.submit("CAP-2", "D2", 5000, 15000)
        status, _ = self.req("POST", "/versions", {"note": "v1"})
        self.assertEqual(status, 201)

        status, before = self.clips(2000, 12000, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(before["source"], "version")
        self.assertEqual(before["version_no"], 1)
        self.assertEqual([(c["capture_id"], c["cycle_id"],
                          c["clip_local_start_ms"], c["clip_local_end_ms"],
                          c["clip_utc_start_ms"], c["clip_utc_end_ms"])
                         for c in before["clips"]],
                        [("CAP-1", "default", 2000, 10000, 2000, 10000),
                         ("CAP-2", "default", 5000, 12000, 5000, 12000)])

        # 发布后：锚点更正（D1 整体 +5000）、删除声段、新增声段、周期纠正
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 0, "utc_ms": 5000},
                              {"local_ms": 100000, "utc_ms": 105000}]})
        self.req("DELETE", "/segments/CAP-2")
        self.submit("CAP-3", "D2", 20000, 30000)
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.add_anchor("D1", 0, 900000, cycle_id="boot-2")
        self.add_anchor("D1", 100000, 1000000, cycle_id="boot-2")
        status, _ = self.req("POST", "/segments/reassign-cycle",
                             {"items": [{"capture_id": "CAP-1",
                                         "from_cycle_id": "default",
                                         "to_cycle_id": "boot-2"}]})
        self.assertEqual(status, 200)
        self.req("POST", "/versions", {"note": "v2"})

        # 历史版本裁剪结果逐字段不变
        status, after = self.clips(2000, 12000, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(after, before)

        # 当前视图：CAP-1 在 boot-2（900000 起，窗口内不存在），
        # CAP-2 已删除，CAP-3 在 [20000,30000) 不相交 -> 无片段
        status, current = self.clips(2000, 12000)
        self.assertEqual(status, 200)
        self.assertEqual(current["source"], "current")
        self.assertEqual(current["clips"], [])

        # v2 裁剪与当前时间轴一致（v2 即当前状态的冻结）
        status, v2 = self.clips(2000, 12000, version_no=2)
        self.assertEqual(status, 200)
        self.assertEqual(v2["clips"], current["clips"])

    def test_version_clips_uses_frozen_anchors_for_boundaries(self):
        """版本裁剪的本地边界按发布时冻结的锚点线性映射，而非当前锚点。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 100000, 100000)
        self.submit("CAP-1", "D1", 0, 100000)
        self.req("POST", "/versions")
        # 更正为斜率 2（本地 1ms 对 UTC 2ms）
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 0, "utc_ms": 0},
                              {"local_ms": 100000, "utc_ms": 200000}]})

        # 冻结锚点（斜率 1）：窗口 [4000,8000) -> 本地 [4000,8000]
        status, v1 = self.clips(4000, 8000, version_no=1)
        self.assertEqual(status, 200)
        clip = v1["clips"][0]
        self.assertEqual((clip["clip_local_start_ms"], clip["clip_local_end_ms"]),
                         (4000, 8000))
        self.assertEqual((clip["clip_utc_start_ms"], clip["clip_utc_end_ms"]),
                         (4000, 8000))

        # 当前锚点（斜率 2）：同一 UTC 窗口 -> 本地 [2000,4000]
        status, cur = self.clips(4000, 8000)
        self.assertEqual(status, 200)
        clip = cur["clips"][0]
        self.assertEqual((clip["clip_local_start_ms"], clip["clip_local_end_ms"]),
                         (2000, 4000))
        self.assertEqual((clip["clip_utc_start_ms"], clip["clip_utc_end_ms"]),
                         (4000, 8000))

    def test_legacy_version_missing_cycle_treated_as_default(self):
        """旧版快照（format_rev=1）缺失周期归属：回放裁剪按 default 处理，
        仍可使用发布时冻结的锚点映射出本地边界。"""
        legacy_path = os.path.join(
            self.tmpdir.name, f"{self.id().split('.')[-1]}.legacy.db")
        self._seed_legacy(legacy_path)
        db.init_db(legacy_path)  # 在线迁移
        self.server.ctx.db_path = legacy_path

        # 旧版冻结锚点 (1000,100000)-(2000,101000)：斜率 1；
        # 旧声段 OLD1 本地 [1100,1200] -> UTC [100100,100200]（自洽快照）
        status, body = self.clips(100050, 100150, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["clips"]), 1)
        clip = body["clips"][0]
        self.assertEqual(clip["capture_id"], "OLD1")
        self.assertEqual(clip["cycle_id"], "default")  # 缺失归属按 default
        # 首个映射 >=100050 的本地是 1050，但声段从 1100 起 -> 起点裁到 1100；
        # 首个映射 >=100150 的本地是 1150 -> 终点裁到 1150
        self.assertEqual((clip["clip_local_start_ms"], clip["clip_local_end_ms"]),
                         (1100, 1150))
        self.assertEqual((clip["clip_utc_start_ms"], clip["clip_utc_end_ms"]),
                         (100100, 100150))

        # 已发布旧版本查询格式保持不变（无 cycle_id / format_rev 字段）
        status, v1 = self.req("GET", "/versions/1")
        self.assertEqual(status, 200)
        self.assertNotIn("format_rev", v1)
        self.assertNotIn("cycle_id", v1["segments"][0])

    def _seed_legacy(self, path):
        conn = sqlite3.connect(path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute("INSERT INTO devices VALUES ('D1','录音笔','t')")
            conn.execute("INSERT INTO anchors VALUES ('D1',1000,100000,'t')")
            conn.execute("INSERT INTO anchors VALUES ('D1',2000,101000,'t')")
            import hashlib
            import json as _json
            canon = _json.dumps(
                {"capture_id": "OLD1", "device_id": "D1",
                 "local_start_ms": 1100, "local_end_ms": 1200, "note": None},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO segments VALUES ('OLD1','D1',1100,1200,NULL,?,'t')",
                (digest,))
            conn.execute("INSERT INTO versions VALUES (1,'旧版','t')")
            conn.execute("INSERT INTO version_anchors VALUES (1,'D1',1000,100000)")
            conn.execute("INSERT INTO version_anchors VALUES (1,'D1',2000,101000)")
            conn.execute(
                "INSERT INTO version_segments VALUES (1,'OLD1','D1',1100,1200,NULL,"
                "'located',100100,100200,'t')")
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ 输入校验

    def test_invalid_inputs_rejected_and_data_unchanged(self):
        """无效输入或版本不存在：明确拒绝且不改动任何数据。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        self.req("POST", "/versions")
        _, before_timeline = self.req("GET", "/timeline/current")
        _, before_versions = self.req("GET", "/versions")

        cases = [
            # 起点必须小于终点
            ({"start_ms": 5000, "end_ms": 5000}, 400, "invalid_range"),
            ({"start_ms": 9000, "end_ms": 1000}, 400, "invalid_range"),
            # 时间须为 [0, 2^63-1] 的整数毫秒
            ({"start_ms": -1, "end_ms": 10000}, 400, "invalid_time"),
            ({"start_ms": 0, "end_ms": MAX_INT64 + 1}, 400, "invalid_time"),
            ({"start_ms": 0.5, "end_ms": 10000}, 400, "invalid_time"),
            ({"start_ms": "0", "end_ms": 10000}, 400, "invalid_time"),
            ({"start_ms": True, "end_ms": 10000}, 400, "invalid_time"),
            ({"start_ms": None, "end_ms": 10000}, 400, "invalid_time"),
            # 缺字段
            ({"end_ms": 10000}, 400, "validation_error"),
            ({"start_ms": 0}, 400, "validation_error"),
            # 版本号非法 / 不存在
            ({"start_ms": 0, "end_ms": 10000, "version_no": 0},
             400, "validation_error"),
            ({"start_ms": 0, "end_ms": 10000, "version_no": -1},
             400, "validation_error"),
            ({"start_ms": 0, "end_ms": 10000, "version_no": "1"},
             400, "validation_error"),
            ({"start_ms": 0, "end_ms": 10000, "version_no": 1.0},
             400, "validation_error"),
            ({"start_ms": 0, "end_ms": 10000, "version_no": True},
             400, "validation_error"),
            ({"start_ms": 0, "end_ms": 10000, "version_no": 999},
             404, "version_not_found"),
        ]
        for payload, want_status, want_code in cases:
            with self.subTest(payload=payload):
                status, body = self.req("POST", "/playback/clips", payload)
                self.assertEqual(status, want_status)
                self.assertEqual(body["error"]["code"], want_code)

        # null version_no 合法（=当前时间轴）
        status, body = self.req("POST", "/playback/clips",
                                {"start_ms": 0, "end_ms": 10000,
                                 "version_no": None})
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "current")

        # 请求体非对象 / 空请求体
        status, body = self.req("POST", "/playback/clips", [1, 2])
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))
        status, body = self.req("POST", "/playback/clips")
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))

        # 全部拒绝后数据不变
        _, after_timeline = self.req("GET", "/timeline/current")
        _, after_versions = self.req("GET", "/versions")
        self.assertEqual(after_timeline["segments"], before_timeline["segments"])
        self.assertEqual(after_versions, before_versions)

    def test_query_is_read_only(self):
        """合法裁剪查询是只读的：不改动时间轴与版本数据。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        self.req("POST", "/versions")
        _, before_timeline = self.req("GET", "/timeline/current")
        _, before_versions = self.req("GET", "/versions")

        status, _ = self.clips(0, 10000)
        self.assertEqual(status, 200)
        status, _ = self.clips(0, 10000, version_no=1)
        self.assertEqual(status, 200)

        _, after_timeline = self.req("GET", "/timeline/current")
        _, after_versions = self.req("GET", "/versions")
        self.assertEqual(after_timeline["segments"], before_timeline["segments"])
        self.assertEqual(after_versions, before_versions)

    def test_int64_boundary(self):
        """时间上限 2^63-1：大数值窗口可正常裁剪。"""
        self.make_device("D1")
        self.add_anchor("D1", MAX_INT64 - 2, MAX_INT64 - 2)
        self.add_anchor("D1", MAX_INT64 - 1, MAX_INT64 - 1)
        self.submit("CAP-EDGE", "D1", MAX_INT64 - 2, MAX_INT64 - 1)

        status, body = self.clips(MAX_INT64 - 2, MAX_INT64)
        self.assertEqual(status, 200)
        self.assertEqual(body["clips"], [
            {"capture_id": "CAP-EDGE", "device_id": "D1", "cycle_id": "default",
             "clip_local_start_ms": MAX_INT64 - 2,
             "clip_local_end_ms": MAX_INT64 - 1,
             "clip_utc_start_ms": MAX_INT64 - 2,
             "clip_utc_end_ms": MAX_INT64 - 1}])

    # ------------------------------------------------------------ 并发一致性

    def test_concurrent_clips_with_writes(self):
        """裁剪查询与锚点更正/发布并发：每次响应中的每条片段都必须自洽——
        本地边界在声段本地范围内，UTC 起止与同周期锚点映射一致，
        且起点 < 终点。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        for i in range(5):
            self.submit(f"CAP-{i}", "D1", i * 2000, i * 2000 + 3000)
            self.submit(f"CAP-B{i}", "D2", i * 2000 + 500, i * 2000 + 2500)

        errors, seen = [], []

        def correct(k):
            try:
                self.req("PUT", "/devices/D1/anchors",
                         {"anchors": [{"local_ms": 0, "utc_ms": k},
                                      {"local_ms": 100000, "utc_ms": 100000 + k}]})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                self.req("POST", "/versions")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def query():
            try:
                status, body = self.clips(0, 12000)
                if status != 200:
                    errors.append(("clips_status", status, body))
                else:
                    seen.append(body)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for k in range(1, 6):
            threads.append(threading.Thread(target=correct, args=(k,)))
            threads.append(threading.Thread(target=publish))
            threads.append(threading.Thread(target=query))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(seen)
        for body in seen:
            prev_key = None
            for c in body["clips"]:
                self.assertLess(c["clip_local_start_ms"], c["clip_local_end_ms"])
                self.assertLessEqual(c["clip_utc_start_ms"], c["clip_utc_end_ms"])
                key = (c["clip_utc_start_ms"], c["device_id"], c["capture_id"])
                if prev_key is not None:
                    self.assertLessEqual(prev_key, key)
                prev_key = key


if __name__ == "__main__":
    unittest.main()
