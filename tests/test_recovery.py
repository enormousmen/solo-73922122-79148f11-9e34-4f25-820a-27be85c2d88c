"""单卷恢复服务关键操作自测。

运行: python3 -m pytest tests/ -v
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from app import create_app  # noqa: E402

CFG = dict(BLOCK_SIZE=4096, BLOCK_COUNT=32, BACKFILL_DELAY=0.002)


def app_volume(c):
    """从 test_client 拿到 Volume 实例(便于断言持久层/注入故障)。"""
    return c.application.vol


def wait_for(pred, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def status(c):
    return c.get("/api/status").get_json()


@pytest.fixture()
def client(tmp_path):
    cfg = dict(CFG, DATA_DIR=str(tmp_path / "data"), BACKUP_IMAGE=str(tmp_path / "backup.img"))
    app = create_app(cfg)
    app.testing = True
    with app.test_client() as c:
        yield c
    app.vol.close()


def test_read_through_backup_and_not_persisted(client):
    """本地缺块 → 从备份读透;读透不落盘(由后台回填负责补齐)。"""
    r = client.get("/api/block/5")
    assert r.status_code == 200
    j = r.get_json()
    assert j["source"] == "backup"
    assert j["text"].startswith("BACKUP-BLOCK-000005|")
    assert j["size"] == 4096
    st = status(client)
    assert st["missing"] == 32, "读透不应落盘"
    assert st["stats"]["backup_reads"] == 1


def test_backfill_completes_and_reads_become_local(client):
    """启动回填 → 全部补齐 → 读取变为本地命中。"""
    r = client.post("/api/backfill/start")
    assert r.status_code == 200
    assert wait_for(lambda: status(client)["missing"] == 0)
    st = status(client)
    assert st["backfill_complete"] and not st["backfill_running"]
    assert st["stats"]["backfilled"] == 32
    j = client.get("/api/block/5").get_json()
    assert j["source"] == "local" and j["origin"] == "backfill"
    assert j["text"].startswith("BACKUP-BLOCK-000005|")


def test_suspend_write_resume_and_late_backup_never_overwrites(client):
    """挂起期间:读被拒、整块写入可提交;放行补齐后业务数据不被覆盖。"""
    assert client.post("/api/block/7/suspend").status_code == 200
    client.post("/api/backfill/start")
    # 其余块都补齐,只剩被挂起的 7
    assert wait_for(lambda: status(client)["missing"] == 1)
    assert 7 in status(client)["suspended"]

    # 挂起期间:缺块读取被拒(不读备份)
    r = client.get("/api/block/7")
    assert r.status_code == 409

    # 挂起期间:同一块的整块写入仍可提交
    r = client.post("/api/block/7/write", json={"data": "BUSINESS-DATA-7"})
    assert r.status_code == 200
    j = client.get("/api/block/7").get_json()
    assert j["source"] == "local" and j["origin"] == "business"
    assert j["text"] == "BUSINESS-DATA-7"

    # 还有缺块时拒绝断开备份(此时其实已补齐 7 以外的块,先验证拒绝逻辑见下)
    client.post("/api/block/7/resume")
    assert wait_for(lambda: status(client)["missing"] == 0)

    # 迟到的备份数据不能覆盖业务写入
    j = client.get("/api/block/7").get_json()
    assert j["origin"] == "business" and j["text"] == "BUSINESS-DATA-7"


def test_disconnect_refused_until_full_then_detached_volume_rw(client):
    """缺块时拒绝断开;补齐后可断开;断开后数据盘仍可读写。"""
    client.post("/api/backfill/start")
    r = client.post("/api/disconnect")
    assert r.status_code == 409
    assert "拒绝" in r.get_json()["message"]

    assert wait_for(lambda: status(client)["missing"] == 0)
    r = client.post("/api/disconnect")
    assert r.status_code == 200
    assert status(client)["backup_disconnected"]

    # 断开后:读本地、写本地都正常
    j = client.get("/api/block/3").get_json()
    assert j["source"] == "local"
    assert client.post("/api/block/3/write", json={"data": "AFTER-DISCONNECT"}).status_code == 200
    j = client.get("/api/block/3").get_json()
    assert j["text"] == "AFTER-DISCONNECT"

    # 断开后不能再回填、不能重复断开
    assert client.post("/api/backfill/start").status_code == 409
    assert client.post("/api/disconnect").status_code == 409


def test_persistence_across_restart(tmp_path):
    """服务重启后保留已落盘的块,并自动继续回填。"""
    cfg = dict(CFG, DATA_DIR=str(tmp_path / "data"), BACKUP_IMAGE=str(tmp_path / "backup.img"))

    app1 = create_app(cfg)
    c1 = app1.test_client()
    c1.post("/api/block/3/write", json={"data": "PERSIST-ME"})
    c1.post("/api/backfill/start")
    assert wait_for(lambda: c1.get("/api/status").get_json()["present"] >= 8)
    app1.vol.stop()  # 模拟进程停止(保留持久状态)

    app2 = create_app(cfg)
    c2 = app2.test_client()
    try:
        j = c2.get("/api/block/3").get_json()
        assert j["origin"] == "business" and j["text"] == "PERSIST-ME", "已落盘的块应保留"
        st = c2.get("/api/status").get_json()
        assert st["backfill_running"], "重启后应自动继续回填"
        assert wait_for(lambda: c2.get("/api/status").get_json()["missing"] == 0)
    finally:
        app2.vol.close()


def test_write_validation(client):
    """整块写入校验:超 4 KiB 拒绝;块号越界 404。"""
    r = client.post("/api/block/0/write", json={"data": "x" * 5000})
    assert r.status_code == 400
    r = client.post("/api/block/0/write", json={})
    assert r.status_code == 400
    assert client.post("/api/block/999/write", json={"data": "x"}).status_code == 404
    assert client.get("/api/block/999").status_code == 404
    # 恰好 4096 字节可以
    r = client.post("/api/block/0/write", json={"data": "y" * 4096})
    assert r.status_code == 200


def test_concurrent_business_writes_survive_backfill(client):
    """回填进行中并发业务写入:所有业务数据都不被回填覆盖。"""
    client.post("/api/backfill/start")
    written = {}
    for i in range(0, 32, 3):
        assert client.post(f"/api/block/{i}/write", json={"data": f"BIZ-{i}"}).status_code == 200
        written[i] = f"BIZ-{i}"
    assert wait_for(lambda: status(client)["missing"] == 0)
    for i, text in written.items():
        j = client.get(f"/api/block/{i}").get_json()
        assert j["origin"] == "business" and j["text"] == text, f"块 {i} 被回填覆盖"


# ---------- 试写 ----------

def test_trial_only_one_round_at_a_time(client):
    """一次只开一轮:重复开始被拒;结束后可再开新一轮。"""
    assert client.post("/api/trial/start").status_code == 200
    r = client.post("/api/trial/start")
    assert r.status_code == 409 and "进行中" in r.get_json()["message"]
    assert client.post("/api/trial/abort").status_code == 200
    assert client.post("/api/trial/start").status_code == 200
    assert status(client)["trial"]["open"]


def test_trial_writes_are_isolated_and_read_back_trial(client):
    """试写期间整块写入记为试写:读优先返回试写,原卷不落盘,试写块仍算缺块。"""
    client.post("/api/trial/start")
    r = client.post("/api/block/5/write", json={"data": "TRIAL-5"})
    assert r.status_code == 200 and r.get_json()["written"] == "trial"

    j = client.get("/api/block/5").get_json()
    assert j["source"] == "trial" and j["text"] == "TRIAL-5"
    st = status(client)
    assert st["present"] == 0 and st["missing"] == 32, "试写不落原卷"
    assert st["trial"]["blocks"] == [5] and st["trial"]["count"] == 1
    assert client.get("/api/blocks").get_json()["present"] == {}

    # 同一块再次试写只覆盖,不额外保留(每块最多一份额外副本)
    client.post("/api/block/5/write", json={"data": "TRIAL-5-AGAIN"})
    assert client.get("/api/block/5").get_json()["text"] == "TRIAL-5-AGAIN"
    assert status(client)["trial"]["count"] == 1
    (rows,) = app_volume(client).db.execute(
        "SELECT COUNT(*) FROM trial_blocks").fetchone()
    assert rows == 1


def test_trial_block_still_missing_until_original_backfilled(client):
    """后台继续补原卷;只有试写数据的块仍算缺块,原卷补齐后才允许断开备份。"""
    client.post("/api/trial/start")
    client.post("/api/block/7/write", json={"data": "TRIAL-7"})
    client.post("/api/backfill/start")
    # 原卷全部补齐,试写块 7 也已有原卷数据,但读取仍优先试写
    assert wait_for(lambda: status(client)["missing"] == 0)
    assert status(client)["trial"]["blocks"] == [7]
    j = client.get("/api/block/7").get_json()
    assert j["source"] == "trial" and j["text"] == "TRIAL-7"
    r = client.post("/api/disconnect")
    assert r.status_code == 200, "原卷补齐即可断开,试写数据不阻塞"


def test_trial_alone_does_not_allow_disconnect(client):
    """只有试写数据、原卷仍缺块时拒绝断开备份。"""
    client.post("/api/trial/start")
    client.post("/api/block/7/write", json={"data": "TRIAL-7"})
    r = client.post("/api/disconnect")
    assert r.status_code == 409 and "缺块" in r.get_json()["message"]
    assert status(client)["missing"] == 32


def test_trial_commit_merges_into_volume(client):
    """提交:一次性把试写并入原卷,来源记为 trial,试写区清空。"""
    client.post("/api/block/3/write", json={"data": "ORIGINAL-3"})
    client.post("/api/trial/start")
    client.post("/api/block/3/write", json={"data": "TRIAL-3"})
    client.post("/api/block/4/write", json={"data": "TRIAL-4"})
    assert client.get("/api/block/3").get_json()["text"] == "TRIAL-3"

    r = client.post("/api/trial/commit")
    assert r.status_code == 200
    st = status(client)
    assert not st["trial"]["open"] and st["trial"]["count"] == 0
    assert st["present"] == 2
    for i, text in ((3, "TRIAL-3"), (4, "TRIAL-4")):
        j = client.get(f"/api/block/{i}").get_json()
        assert j["source"] == "local" and j["origin"] == "trial" and j["text"] == text
    # 提交后不再处于试写:新写入直接落原卷
    r = client.post("/api/block/3/write", json={"data": "AFTER-COMMIT"})
    assert r.get_json()["written"] == "business"
    assert client.get("/api/block/3").get_json()["origin"] == "business"


def test_trial_abort_discards_trial_only_keeps_backfill(client):
    """放弃:只丢试写改动,期间回填到原卷的数据保留;读回该块原数据。"""
    client.post("/api/trial/start")
    client.post("/api/block/7/write", json={"data": "TRIAL-7"})
    client.post("/api/backfill/start")
    assert wait_for(lambda: status(client)["missing"] == 0)

    r = client.post("/api/trial/abort")
    assert r.status_code == 200
    st = status(client)
    assert not st["trial"]["open"] and st["missing"] == 0, "回填结果必须保留"
    j = client.get("/api/block/7").get_json()
    assert j["source"] == "local" and j["origin"] == "backfill"
    assert j["text"].startswith("BACKUP-BLOCK-000007|")


def test_trial_commit_failure_rolls_back_both(client):
    """提交中途失败:已并入的部分也回滚,原卷与试写都保持原状。"""
    client.post("/api/block/3/write", json={"data": "ORIGINAL-3"})
    client.post("/api/trial/start")
    client.post("/api/block/3/write", json={"data": "TRIAL-3"})
    client.post("/api/block/4/write", json={"data": "TRIAL-4"})

    vol = app_volume(client)
    # 第二块并入原卷时触发数据库错误(块 3 的写入已在同一事务中)
    vol.db.execute(
        "CREATE TEMP TRIGGER fail_trial_commit BEFORE INSERT ON blocks "
        "WHEN NEW.idx=4 BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
    )
    vol.db.commit()
    try:
        r = client.post("/api/trial/commit")
    finally:
        vol.db.execute("DROP TRIGGER fail_trial_commit")
        vol.db.commit()
    assert r.status_code == 409 and "未改变" in r.get_json()["message"]

    st = status(client)
    assert st["trial"]["open"] and st["trial"]["blocks"] == [3, 4]
    assert st["present"] == 1, "原卷不应被部分写入"
    assert client.get("/api/block/3").get_json()["text"] == "TRIAL-3"
    assert client.get("/api/block/4").get_json()["text"] == "TRIAL-4"
    # 回滚后仍可正常提交
    assert client.post("/api/trial/commit").status_code == 200
    assert client.get("/api/block/3").get_json()["origin"] == "trial"


def test_trial_abort_failure_keeps_state(client):
    """放弃失败:原卷与本轮试写都保持原状。"""
    client.post("/api/block/3/write", json={"data": "ORIGINAL-3"})
    client.post("/api/trial/start")
    client.post("/api/block/3/write", json={"data": "TRIAL-3"})

    vol = app_volume(client)
    vol.db.execute(
        "CREATE TEMP TRIGGER fail_trial_abort BEFORE DELETE ON trial_blocks "
        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
    )
    vol.db.commit()
    try:
        r = client.post("/api/trial/abort")
    finally:
        vol.db.execute("DROP TRIGGER fail_trial_abort")
        vol.db.commit()
    assert r.status_code == 409 and "未改变" in r.get_json()["message"]
    st = status(client)
    assert st["trial"]["open"] and st["trial"]["blocks"] == [3]
    assert client.get("/api/block/3").get_json()["text"] == "TRIAL-3"
    assert client.get("/api/block/3").get_json()["source"] == "trial"


def test_trial_refuses_reset_while_open(client):
    """试写期间拒绝重置卷。"""
    client.post("/api/trial/start")
    client.post("/api/block/7/write", json={"data": "TRIAL-7"})
    r = client.post("/api/reset")
    assert r.status_code == 409 and "拒绝重置" in r.get_json()["message"]
    assert status(client)["trial"]["open"]
    assert client.get("/api/block/7").get_json()["text"] == "TRIAL-7"
    client.post("/api/trial/abort")
    assert client.post("/api/reset").status_code == 200
    assert status(client)["trial"]["count"] == 0


def test_trial_open_round_guarded_at_schema_level(client):
    """持久层兜底:数据库不允许同时存在两个 open 轮次。"""
    import sqlite3

    client.post("/api/trial/start")
    db = app_volume(client).db
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO trial_round(state, started_at) VALUES('open', 0)")
    db.rollback()
    assert client.post("/api/trial/start").status_code == 409


def test_trial_survives_restart(tmp_path):
    """重启后可继续未结束的试写。"""
    cfg = dict(CFG, DATA_DIR=str(tmp_path / "data"), BACKUP_IMAGE=str(tmp_path / "backup.img"))
    app1 = create_app(cfg)
    c1 = app1.test_client()
    c1.post("/api/trial/start")
    c1.post("/api/block/9/write", json={"data": "TRIAL-9"})
    c1.post("/api/backfill/start")
    assert wait_for(lambda: c1.get("/api/status").get_json()["missing"] == 0)
    app1.vol.stop()

    app2 = create_app(cfg)
    c2 = app2.test_client()
    try:
        st = c2.get("/api/status").get_json()
        assert st["trial"]["open"] and st["trial"]["blocks"] == [9]
        assert c2.get("/api/block/9").get_json()["text"] == "TRIAL-9"
        assert c2.post("/api/trial/start").status_code == 409, "重启后仍只允许一轮"
        assert c2.post("/api/trial/commit").status_code == 200
        j = c2.get("/api/block/9").get_json()
        assert j["origin"] == "trial" and j["text"] == "TRIAL-9"
    finally:
        app2.vol.close()

