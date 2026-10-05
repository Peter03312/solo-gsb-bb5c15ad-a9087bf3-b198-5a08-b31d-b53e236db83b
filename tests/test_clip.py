"""端到端测试：回放裁剪（playback clip，v1.5）。

覆盖：
- 基本裁剪：与 UTC 半开窗口相交且可定位的声段返回本地裁剪起止与映射 UTC 起止；
- 边界规则：两个本地边界分别取该声段本地范围内"映射 UTC 首次不小于窗口
  对应边界"的整数毫秒，不存在则截在声段末端；线性漂移与毫秒四舍五入沿用
  现有映射规则（含取整平台）；
- 裁后起止相同的声段不返回：零长度声段、与窗口边界相接的声段、窗口内无
  整数本地毫秒可映射（陡峭漂移）的声段；不可定位声段不返回；
- 同设备重叠声段分别返回；结果按 (UTC 起点, 设备编号, 采集编号) 排序；
- 周期感知：声段只使用所属周期锚点做裁剪映射；
- 指定 version_no 只使用发布时冻结的锚点与声段（后续锚点更正、声段增删、
  周期归属纠正与新发布都不改动历史结果）；旧版（format_rev=1）快照缺失的
  周期归属按 default 处理；
- 输入校验：非法时间 / 起点不小于终点 / 版本号非法 / 版本不存在 ->
  明确拒绝且不改动数据；接口只读；
- 并发：裁剪与更正/发布并发时，每次响应都是一致快照（结果良构、排序正确）。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
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

# v1（无周期）建表脚本，仅用于构造待迁移的旧库
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


class ClipTestCase(unittest.TestCase):
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

    def clip(self, start_ms, end_ms, version_no=None):
        payload = {"start_ms": start_ms, "end_ms": end_ms}
        if version_no is not None:
            payload["version_no"] = version_no
        return self.req("POST", "/segments/clip", payload)

    def make_identity_device(self, device_id):
        """建一台恒等映射设备：锚点 (0,0)-(100000,100000)，本地毫秒即 UTC 毫秒。"""
        self.make_device(device_id)
        self.add_anchor(device_id, 0, 0)
        self.add_anchor(device_id, 100000, 100000)

    def assert_well_formed(self, body, start_ms, end_ms):
        """响应良构：回显窗口、排序正确、每条裁段本地起止非零长度且 UTC 单调。"""
        self.assertEqual(body["start_ms"], start_ms)
        self.assertEqual(body["end_ms"], end_ms)
        segs = body["segments"]
        self.assertEqual(body["segment_count"], len(segs))
        keys = [(s["utc_start_ms"], s["device_id"], s["capture_id"]) for s in segs]
        self.assertEqual(keys, sorted(keys))
        for s in segs:
            self.assertLess(s["local_start_ms"], s["local_end_ms"])
            self.assertLessEqual(s["utc_start_ms"], s["utc_end_ms"])
            self.assertIn("cycle_id", s)

    # ------------------------------------------------------------ 基本裁剪

    def test_basic_clip_identity_device(self):
        """恒等映射设备：窗口内裁剪、窗口覆盖整段、声段被窗口包含。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)

        # 窗口落在声段内部：两端都裁
        status, body = self.clip(3000, 6000)
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "current")
        self.assertIsNone(body["version_no"])
        self.assertEqual(body["segments"], [
            {"capture_id": "CAP-1", "device_id": "D1", "cycle_id": "default",
             "local_start_ms": 3000, "local_end_ms": 6000,
             "utc_start_ms": 3000, "utc_end_ms": 6000},
        ])
        self.assertEqual(body["segment_count"], 1)

        # 窗口覆盖整段：两端都截在声段自身边界
        status, body = self.clip(0, 20000)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"][0]["local_start_ms"], 0)
        self.assertEqual(body["segments"][0]["local_end_ms"], 10000)

        # 声段只越出窗口右端：起点在窗口内保持，终点裁到窗口边界
        self.submit("CAP-2", "D1", 20000, 40000)
        status, body = self.clip(25000, 30000)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [
            {"capture_id": "CAP-2", "device_id": "D1", "cycle_id": "default",
             "local_start_ms": 25000, "local_end_ms": 30000,
             "utc_start_ms": 25000, "utc_end_ms": 30000},
        ])

    def test_clip_end_falls_back_to_segment_end(self):
        """声段末端映射仍小于窗口终点：本地终点截在声段末端。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 2000, 5000)

        status, body = self.clip(0, 10000)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"][0]["local_end_ms"], 5000)
        self.assertEqual(body["segments"][0]["utc_end_ms"], 5000)

    def test_first_local_ms_not_less_than_boundary_with_rounding(self):
        """线性漂移 + 四舍五入：本地边界取"映射 UTC 首次不小于窗口边界"的整数毫秒。

        锚点 (0,0)-(3,10)：utc(0)=0, utc(1)=3, utc(2)=7, utc(3)=10。
        """
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)
        self.add_anchor("D1", 3, 10)
        self.submit("CAP-1", "D1", 0, 3)

        # 窗口 [4,8)：首个 utc>=4 的本地毫秒是 2（utc=7），首个 utc>=8 的是 3（utc=10）
        status, body = self.clip(4, 8)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [
            {"capture_id": "CAP-1", "device_id": "D1", "cycle_id": "default",
             "local_start_ms": 2, "local_end_ms": 3,
             "utc_start_ms": 7, "utc_end_ms": 10},
        ])

        # 窗口 [3,4)：首个 utc>=3 的是 1（utc=3），首个 utc>=4 的是 2（utc=7）
        status, body = self.clip(3, 4)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"][0]["local_start_ms"], 1)
        self.assertEqual(body["segments"][0]["local_end_ms"], 2)
        self.assertEqual(body["segments"][0]["utc_start_ms"], 3)
        self.assertEqual(body["segments"][0]["utc_end_ms"], 7)

    def test_rounding_plateau_first_ms_of_plateau(self):
        """取整平台：多个本地毫秒映射到同一 UTC 时，边界取平台上首个达标毫秒。

        锚点 (0,100)-(10000,600)：utc(t)=100+round(t/20)，斜率 0.05。
        """
        self.make_device("D1")
        self.add_anchor("D1", 0, 100)
        self.add_anchor("D1", 10000, 600)
        self.submit("CAP-1", "D1", 0, 10000)

        # 窗口 [599,700)：首个 utc>=599 的本地毫秒是 9970（utc=599）；
        # 没有任何本地毫秒映射 >=700（声段末端 utc=600）-> 终点截在声段末端
        status, body = self.clip(599, 700)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [
            {"capture_id": "CAP-1", "device_id": "D1", "cycle_id": "default",
             "local_start_ms": 9970, "local_end_ms": 10000,
             "utc_start_ms": 599, "utc_end_ms": 600},
        ])

        # 窗口 [600,700)：声段 UTC 终点 600 不大于窗口起点 600（半开），不相交
        status, body = self.clip(600, 700)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [])
        self.assertEqual(body["segment_count"], 0)

    # ------------------------------------------------------------ 退化与排除

    def test_degenerate_clips_not_returned(self):
        """裁后起止相同的声段不返回：零长度、边界相接、窗口内无整数本地毫秒。"""
        self.make_identity_device("D1")
        self.submit("CAP-ZERO", "D1", 5000, 5000)      # 零长度
        self.submit("CAP-BEFORE", "D1", 0, 3000)       # 终点=窗口起点
        self.submit("CAP-AFTER", "D1", 6000, 9000)     # 起点=窗口终点
        self.submit("CAP-OUT", "D1", 20000, 30000)     # 完全在窗口外

        status, body = self.clip(3000, 6000)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [])

        # 陡峭漂移：锚点 (0,0)-(1,1000)，窗口 [500,600) 内无整数本地毫秒可映射
        # -> CAP-STEEP 裁后退化不返回；CAP-BEFORE [0,3000] 与窗口相交正常裁剪
        self.make_device("D2")
        self.add_anchor("D2", 0, 0)
        self.add_anchor("D2", 1, 1000)
        self.submit("CAP-STEEP", "D2", 0, 1)
        status, body = self.clip(500, 600)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [
            {"capture_id": "CAP-BEFORE", "device_id": "D1", "cycle_id": "default",
             "local_start_ms": 500, "local_end_ms": 600,
             "utc_start_ms": 500, "utc_end_ms": 600},
        ])

    def test_unlocatable_segments_not_returned(self):
        """不可定位声段（超出锚点覆盖）不返回，即使窗口极大。"""
        self.make_identity_device("D1")
        self.submit("CAP-OUT", "D1", 200000, 300000)   # 超出锚点覆盖
        self.submit("CAP-IN", "D1", 1000, 2000)

        status, body = self.clip(0, MAX_INT64)
        self.assertEqual(status, 200)
        self.assertEqual([s["capture_id"] for s in body["segments"]], ["CAP-IN"])

    def test_no_segments_empty_result(self):
        """无任何声段：返回空列表，窗口与来源字段正常回显。"""
        status, body = self.clip(1000, 2000)
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "current")
        self.assertIsNone(body["version_no"])
        self.assertEqual(body["segments"], [])
        self.assertEqual(body["segment_count"], 0)

    # ------------------------------------------------------------ 重叠与排序

    def test_same_device_overlapping_segments_returned_separately(self):
        """同设备重叠声段分别返回（不合并），各自独立裁剪。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        self.submit("CAP-2", "D1", 5000, 15000)

        status, body = self.clip(4000, 12000)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [
            {"capture_id": "CAP-1", "device_id": "D1", "cycle_id": "default",
             "local_start_ms": 4000, "local_end_ms": 10000,
             "utc_start_ms": 4000, "utc_end_ms": 10000},
            {"capture_id": "CAP-2", "device_id": "D1", "cycle_id": "default",
             "local_start_ms": 5000, "local_end_ms": 12000,
             "utc_start_ms": 5000, "utc_end_ms": 12000},
        ])

    def test_sorting_by_utc_device_capture(self):
        """排序：UTC 起点升序；相同则设备编号升序；再相同则采集编号升序。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        self.submit("CAP-B", "D2", 100, 200)
        self.submit("CAP-C", "D1", 100, 150)
        self.submit("CAP-A", "D1", 100, 160)
        self.submit("CAP-D", "D1", 50, 80)

        status, body = self.clip(0, 1000)
        self.assertEqual(status, 200)
        self.assertEqual(
            [s["capture_id"] for s in body["segments"]],
            ["CAP-D", "CAP-A", "CAP-C", "CAP-B"])
        self.assert_well_formed(body, 0, 1000)

    # ------------------------------------------------------------ 周期感知

    def test_clip_uses_owning_cycle_anchors(self):
        """声段只按所属周期锚点裁剪映射；响应带周期归属。"""
        self.make_device("D1")
        self.add_anchor("D1", 0, 0)              # default：恒等
        self.add_anchor("D1", 100000, 100000)
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.add_anchor("D1", 0, 500000, "boot-2")   # boot-2：utc = 500000 + t
        self.add_anchor("D1", 100000, 600000, "boot-2")
        self.submit("CAP-1", "D1", 100, 200)                      # default
        self.submit("CAP-2", "D1", 100, 200, cycle_id="boot-2")   # boot-2

        status, body = self.clip(150, 500150)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [
            {"capture_id": "CAP-1", "device_id": "D1", "cycle_id": "default",
             "local_start_ms": 150, "local_end_ms": 200,
             "utc_start_ms": 150, "utc_end_ms": 200},
            {"capture_id": "CAP-2", "device_id": "D1", "cycle_id": "boot-2",
             "local_start_ms": 100, "local_end_ms": 150,
             "utc_start_ms": 500100, "utc_end_ms": 500150},
        ])

    # ------------------------------------------------------------ 版本快照

    def test_version_clip_uses_frozen_anchors_and_segments(self):
        """指定版本只使用发布时冻结的锚点与声段：后续更正/增删/纠正不改历史。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        status, _ = self.req("POST", "/versions", {"note": "v1"})
        self.assertEqual(status, 201)

        status, before = self.clip(3000, 6000, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(before["source"], "version")
        self.assertEqual(before["version_no"], 1)
        self.assertEqual(before["segments"], [
            {"capture_id": "CAP-1", "device_id": "D1", "cycle_id": "default",
             "local_start_ms": 3000, "local_end_ms": 6000,
             "utc_start_ms": 3000, "utc_end_ms": 6000},
        ])

        # 发布后：锚点更正（+250 位移）、删除旧声段、新声段、周期归属纠正、再发布
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 0, "utc_ms": 250},
                              {"local_ms": 100000, "utc_ms": 100250}]})
        self.req("DELETE", "/segments/CAP-1")
        self.submit("CAP-2", "D1", 0, 10000)   # 当前映射为 [250, 10250]
        self.req("POST", "/devices/D1/cycles", {"cycle_id": "boot-2"})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-2", "local_ms": 0, "utc_ms": 500000})
        self.req("POST", "/devices/D1/anchors",
                 {"cycle_id": "boot-2", "local_ms": 100000, "utc_ms": 600000})
        status, _ = self.req("POST", "/segments/reassign-cycle",
                             {"items": [{"capture_id": "CAP-2",
                                         "from_cycle_id": "default",
                                         "to_cycle_id": "boot-2"}]})
        self.assertEqual(status, 200)
        self.req("POST", "/versions", {"note": "v2"})

        # 历史版本裁剪结果逐字段不变
        status, after = self.clip(3000, 6000, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(after, before)

        # 当前时间轴反映最新状态：CAP-2 在 boot-2（utc=500000+t），
        # 窗口 [3000,6000) 内无任何可定位声段
        status, current = self.clip(3000, 6000)
        self.assertEqual(status, 200)
        self.assertEqual(current["segments"], [])

        # v2 的裁剪与当前时间轴一致（v2 即当前状态的冻结）
        status, v2 = self.clip(3000, 6000, version_no=2)
        self.assertEqual(status, 200)
        self.assertEqual(v2["segments"], current["segments"])

    def test_version_clip_current_mapping_applied_to_frozen(self):
        """版本裁剪用冻结锚点重算本地边界：更正前后同一窗口裁出不同本地片段。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        self.req("POST", "/versions")
        # 锚点更正：utc = 250 + t（投影整体平移 +250）
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 0, "utc_ms": 250},
                              {"local_ms": 100000, "utc_ms": 100250}]})

        # 当前视图：首个 utc>=3000 的本地毫秒是 2750，首个 utc>=6000 的是 5750
        status, cur = self.clip(3000, 6000)
        self.assertEqual(status, 200)
        self.assertEqual(cur["segments"][0]["local_start_ms"], 2750)
        self.assertEqual(cur["segments"][0]["local_end_ms"], 5750)
        self.assertEqual(cur["segments"][0]["utc_start_ms"], 3000)
        self.assertEqual(cur["segments"][0]["utc_end_ms"], 6000)

        # 历史视图：仍按发布时锚点（恒等）裁剪
        status, v1 = self.clip(3000, 6000, version_no=1)
        self.assertEqual(status, 200)
        self.assertEqual(v1["segments"][0]["local_start_ms"], 3000)
        self.assertEqual(v1["segments"][0]["local_end_ms"], 6000)

    # ------------------------------------------------------------ 输入校验与只读

    def test_invalid_inputs_rejected_and_data_unchanged(self):
        """无效输入或版本不存在：明确拒绝且不改动任何数据。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        self.req("POST", "/versions")
        _, before_timeline = self.req("GET", "/timeline/current")
        _, before_versions = self.req("GET", "/versions")

        cases = [
            # 起点必须小于终点
            ({"start_ms": 10000, "end_ms": 10000}, 400, "invalid_range"),
            ({"start_ms": 20000, "end_ms": 10000}, 400, "invalid_range"),
            # 时间须为 [0, 2^63-1] 的整数毫秒
            ({"start_ms": -1, "end_ms": 10000}, 400, "invalid_time"),
            ({"start_ms": 0, "end_ms": MAX_INT64 + 1}, 400, "invalid_time"),
            ({"start_ms": 0.5, "end_ms": 10000}, 400, "invalid_time"),
            ({"start_ms": "0", "end_ms": 10000}, 400, "invalid_time"),
            ({"start_ms": True, "end_ms": 10000}, 400, "invalid_time"),
            ({"start_ms": None, "end_ms": 10000}, 400, "invalid_time"),
            # 缺字段 / 版本号非法 / 版本不存在
            ({"start_ms": 0}, 400, "validation_error"),
            ({"end_ms": 10000}, 400, "validation_error"),
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
                status, body = self.req("POST", "/segments/clip", payload)
                self.assertEqual(status, want_status)
                self.assertEqual(body["error"]["code"], want_code)

        # 请求体非对象 / 空请求体
        status, body = self.req("POST", "/segments/clip", [1, 2, 3])
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))
        status, body = self.req("POST", "/segments/clip")
        self.assertEqual((status, body["error"]["code"]), (400, "validation_error"))

        # 全部拒绝后数据不变
        _, after_timeline = self.req("GET", "/timeline/current")
        _, after_versions = self.req("GET", "/versions")
        self.assertEqual(after_timeline["segments"], before_timeline["segments"])
        self.assertEqual(after_timeline["devices"], before_timeline["devices"])
        self.assertEqual(after_versions, before_versions)

    def test_clip_is_read_only(self):
        """合法裁剪同样是只读的：不改动时间轴与版本数据。"""
        self.make_identity_device("D1")
        self.submit("CAP-1", "D1", 0, 10000)
        self.req("POST", "/versions")
        _, before_timeline = self.req("GET", "/timeline/current")
        _, before_versions = self.req("GET", "/versions")

        status, _ = self.clip(0, 10000)
        self.assertEqual(status, 200)
        status, _ = self.clip(0, 10000, version_no=1)
        self.assertEqual(status, 200)

        _, after_timeline = self.req("GET", "/timeline/current")
        _, after_versions = self.req("GET", "/versions")
        self.assertEqual(after_timeline["segments"], before_timeline["segments"])
        self.assertEqual(after_versions, before_versions)

    def test_int64_boundary(self):
        """时间上限 2^63-1：大数值窗口与边界声段可正常裁剪。"""
        status, body = self.clip(0, MAX_INT64)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [])

        self.make_device("D1")
        self.add_anchor("D1", MAX_INT64 - 2, MAX_INT64 - 2)
        self.add_anchor("D1", MAX_INT64 - 1, MAX_INT64 - 1)
        self.submit("CAP-EDGE", "D1", MAX_INT64 - 2, MAX_INT64 - 1)
        status, body = self.clip(MAX_INT64 - 2, MAX_INT64 - 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["segments"], [
            {"capture_id": "CAP-EDGE", "device_id": "D1", "cycle_id": "default",
             "local_start_ms": MAX_INT64 - 2, "local_end_ms": MAX_INT64 - 1,
             "utc_start_ms": MAX_INT64 - 2, "utc_end_ms": MAX_INT64 - 1},
        ])

    # ------------------------------------------------------------ 并发一致性

    def test_concurrent_clip_with_writes(self):
        """裁剪与锚点更正/发布并发：每次裁剪响应都必须是一致快照——
        结果良构（排序正确、裁段非零长度、UTC 单调）。"""
        self.make_identity_device("D1")
        self.make_identity_device("D2")
        for i in range(5):
            self.submit(f"CAP-{i}", "D1", i * 2000, i * 2000 + 1500)
            self.submit(f"CAP-B{i}", "D2", i * 2000 + 500, i * 2000 + 2000)

        errors, clips = [], []

        def correct(k):
            try:
                self.req("PUT", "/devices/D1/anchors",
                         {"anchors": [{"local_ms": 0, "utc_ms": k * 10},
                                      {"local_ms": 100000, "utc_ms": 100000 + k * 10}]})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def publish():
            try:
                self.req("POST", "/versions")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def clip():
            try:
                status, body = self.clip(0, 12000)
                if status != 200:
                    errors.append(("clip_status", status, body))
                else:
                    clips.append(body)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for k in range(1, 6):
            threads.append(threading.Thread(target=correct, args=(k,)))
            threads.append(threading.Thread(target=publish))
            threads.append(threading.Thread(target=clip))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(clips)
        for body in clips:
            self.assert_well_formed(body, 0, 12000)


class LegacyVersionClipTestCase(unittest.TestCase):
    """旧版（format_rev=1）快照：缺失的周期归属按 default 处理。"""

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
        self._seed_legacy()
        db.init_db(self.db_path)  # 在线迁移
        self.server.ctx.db_path = self.db_path

    def req(self, method, path, body=None):
        return request(self.base, method, path, body)

    def _seed_legacy(self):
        """构造 v1 旧库：恒等锚点 (0,0)-(100000,100000)，声段 OLD1 [1000,9000]，
        已发布 v1（无周期列，version_segments 投影 utc 与本地一致）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(LEGACY_SCHEMA)
            conn.execute("INSERT INTO devices VALUES ('D1','录音笔','2026-09-01T00:00:00.000Z')")
            conn.execute("INSERT INTO anchors VALUES ('D1',0,0,'t')")
            conn.execute("INSERT INTO anchors VALUES ('D1',100000,100000,'t')")
            canon = json.dumps(
                {"capture_id": "OLD1", "device_id": "D1",
                 "local_start_ms": 1000, "local_end_ms": 9000, "note": None},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO segments VALUES ('OLD1','D1',1000,9000,NULL,?,'t')",
                (digest,))
            conn.execute("INSERT INTO versions VALUES (1,'旧版','2026-09-02T00:00:00.000Z')")
            conn.execute("INSERT INTO version_anchors VALUES (1,'D1',0,0)")
            conn.execute("INSERT INTO version_anchors VALUES (1,'D1',100000,100000)")
            conn.execute(
                "INSERT INTO version_segments VALUES (1,'OLD1','D1',1000,9000,NULL,"
                "'located',1000,9000,'t')")
            conn.commit()
        finally:
            conn.close()

    def test_legacy_version_clip_cycle_defaults_to_default(self):
        """旧版快照裁剪：冻结锚点可用，缺失的周期归属按 default 返回。"""
        status, body = self.req("POST", "/segments/clip",
                                {"start_ms": 3000, "end_ms": 6000, "version_no": 1})
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "version")
        self.assertEqual(body["version_no"], 1)
        self.assertEqual(body["segments"], [
            {"capture_id": "OLD1", "device_id": "D1", "cycle_id": "default",
             "local_start_ms": 3000, "local_end_ms": 6000,
             "utc_start_ms": 3000, "utc_end_ms": 6000},
        ])

        # 旧版本查询格式不变（无 cycle_id 字段），裁剪接口不受影响
        status, v1 = self.req("GET", "/versions/1")
        self.assertEqual(status, 200)
        self.assertNotIn("cycle_id", v1["segments"][0])

    def test_legacy_version_clip_frozen_after_changes(self):
        """迁移后继续变更数据，旧版快照裁剪结果不变。"""
        status, before = self.req("POST", "/segments/clip",
                                  {"start_ms": 3000, "end_ms": 6000, "version_no": 1})
        self.assertEqual(status, 200)
        # 迁移后更正锚点（+500 位移）并删除声段
        self.req("PUT", "/devices/D1/anchors",
                 {"anchors": [{"local_ms": 0, "utc_ms": 500},
                              {"local_ms": 100000, "utc_ms": 100500}]})
        self.req("DELETE", "/segments/OLD1")
        status, after = self.req("POST", "/segments/clip",
                                 {"start_ms": 3000, "end_ms": 6000, "version_no": 1})
        self.assertEqual(status, 200)
        self.assertEqual(after, before)
        # 当前时间轴已无声段
        status, cur = self.req("POST", "/segments/clip",
                               {"start_ms": 3000, "end_ms": 6000})
        self.assertEqual(status, 200)
        self.assertEqual(cur["segments"], [])


if __name__ == "__main__":
    unittest.main()
