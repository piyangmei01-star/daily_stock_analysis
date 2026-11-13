# -*- coding: utf-8 -*-
"""归档完整性守卫（GitHub Actions 归档步骤调用）。

用途：修复「Actions 显示 success 但业务数据丢失」这一根因。
在提交归档之前校验：
  1. 本次运行产生的不可覆盖真相源 run_<run_id>.json 存在、结构完整、hash 正确
  2. 每个 run_id 全局唯一，且不存在 .tmp 半成品残留
  3. 若发生微信推送，push_receipt_<run_id>.json 的推送文本 hash 必须等于真相源
  4. 未被 Git 跟踪的二进制 db / 缓存 CSV 不得进入归档（只提交 data/history/）

任何一项不满足 → 打印 ARCHIVE_GUARD_FAIL 并 exit 1，让工作流明确判红，绝不假绿。
用法: python archive_guard.py [task] [YYYY-MM-DD]
"""
import glob
import json
import os
import sys
from pathlib import Path

import history_store as hs


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else ""
    run_date = sys.argv[2] if len(sys.argv) > 2 else hs.today()
    day = os.path.join(hs.ARCHIVE_ROOT, run_date.replace("-", ""))
    errs = []

    # 1. 清理中断遗留的 .tmp（只删未完成的临时文件，绝不删已完成的归档）
    stale = glob.glob(os.path.join(day, "*.tmp"))
    for fp in stale:
        os.remove(fp)
    if stale:
        print(f"GUARD_INFO 清理中断遗留 .tmp {len(stale)} 个（未删任何已完成的归档）")

    # 2. 真相源完整性
    runs = sorted(glob.glob(os.path.join(day, "run_*.json")))
    if task:
        runs = [f for f in runs if f"run_{task}_" in os.path.basename(f)]
    ids = []
    for fp in runs:
        d, e = hs.verify_run_file(fp)
        if e:
            errs.append(f"{os.path.basename(fp)}: {';'.join(e)}")
        else:
            ids.append(d["run_id"])
            print(f"GUARD_RUN_OK {d['run_id']} picks={d['picks_count']} "
                  f"data_date={d.get('data_date')} source={d.get('data_source')} "
                  f"codes_hash={d['codes_hash'][:16]}")
    if len(ids) != len(set(ids)):
        errs.append("存在重复 run_id（违反「每次选股唯一 Run ID」）")
    # 只有显式指定选股任务时才强制要求「本次必须产出真相源」；
    # 盘前/异动/AI 等非选股任务只做通用完整性校验，不因当日无 run 文件误判失败。
    if task and not runs:
        errs.append(f"任务 {task} 当日无任何不可覆盖真相源 run_*.json（名单可能未持久化）")
    if not task and not runs:
        print("GUARD_INFO 非选股任务，当日无 run 真相源，跳过强制要求")

    # 3. 推送凭证与真相源一致性
    receipts = sorted(glob.glob(os.path.join(day, "push_receipt_*.json")))
    for fp in receipts:
        try:
            r = json.loads(Path(fp).read_text(encoding="utf-8"))
            cand = os.path.join(day, f"run_{r.get('run_id')}.json")
            if not os.path.exists(cand):
                errs.append(f"{os.path.basename(fp)}: 找不到对应真相源")
                continue
            d, e = hs.verify_run_file(cand)
            if e:
                errs.append(f"{os.path.basename(fp)}: 对应真相源校验失败")
                continue
            if r.get("push_ok") is False:
                errs.append(f"{os.path.basename(fp)}: 推送凭证 push_ok=false（推送实际失败，不得判绿）")
            if r.get("push_text_hash") != d.get("push_text_hash"):
                errs.append(f"{os.path.basename(fp)}: 推送文本hash≠归档hash（名单可能不一致）")
            elif r.get("push_ok") is not False:
                print(f"GUARD_PUSH_OK {r['run_id']} push_hash 一致")
        except Exception as e:
            errs.append(f"{os.path.basename(fp)}: {type(e).__name__}: {e}")

    # 4. 归档目录内不得混入二进制 db / 缓存 CSV（rebase 冲突根因）
    bad = glob.glob(os.path.join(day, "*.db")) + glob.glob(os.path.join(day, "*.csv"))
    if bad:
        errs.append(f"归档目录混入二进制/CSV 文件（rebase 冲突根因）: {[os.path.basename(x) for x in bad]}")

    if errs:
        for e in errs:
            print(f"GUARD_ERROR {e}")
        print(f"ARCHIVE_GUARD_FAIL 校验失败 {len(errs)} 项，归档步骤判定失败")
        sys.exit(1)
    print(f"ARCHIVE_GUARD_OK 真相源 {len(runs)} 份 / 推送凭证 {len(receipts)} 份，"
          f"run_id 全唯一，hash 全一致，归档目录无二进制")


if __name__ == "__main__":
    main()
