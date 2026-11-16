# -*- coding: utf-8 -*-
"""风控模块接入主程序的确定性测试（不联网、不推送、不写业务库）。"""
import sys
sys.path.insert(0, ".")
import risk_management as rm

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name)


# ===== 1. market_position_md_lines 防御性 =====
lines = rm.market_position_md_lines(breadth={"adv": 3000, "dec": 1000},
                                    turnover={"ratio": 1.2}, indices={"上证指数": {"bias_pct": 2.0}})
check("仓位段有标题", any("仓位" in x for x in lines))
check("仓位段含进攻档", any("进攻" in x for x in lines))
check("仓位段有评分行", any("评分" in x for x in lines))

lines = rm.market_position_md_lines(breadth=None, turnover=None, indices=None)
check("空数据仓位段不抛且有降级", any("数据不足" in x for x in lines))

# 脏数据不抛
for bad in [{"adv": "x", "dec": None}, "not-a-dict", 123, None]:
    try:
        out = rm.market_position_md_lines(breadth=bad, turnover=bad, indices=bad)
        check(f"脏入参不抛 {type(bad).__name__}", isinstance(out, list) and len(out) > 0)
    except Exception as e:
        check(f"脏入参不抛 {type(bad).__name__}", False)
        print("   EXC:", e)

# ===== 2. stops_md_table 防御性（全部 monkeypatch 为纯函数，不联网）=====
orig = rm.analyze_stock_risk
try:
    def fake_ok(code, entry, source_order=None):
        return {"ok": True, "code": code,
                "stops": {"atr14": 1.5, "suggested_stop": 9.2,
                          "max_loss_pct": 8.0, "struct_stop": 9.0}, "error": None}
    def fake_fail(code, entry, source_order=None):
        return {"ok": False, "code": code, "error": "kline_unavailable",
                "stops": rm.compute_atr_stops([], entry)}
    def fake_raise(code, entry, source_order=None):
        raise RuntimeError("boom")

    rm.analyze_stock_risk = fake_ok
    picks = [{"code": "600000", "name": "浦发银行", "entry": 10.0},
             {"code": "000001", "name": "平安银行", "entry": 12.0}]
    tbl = rm.stops_md_table(picks)
    joined = "\n".join(tbl)
    check("止损表有标题", any("止损" in x for x in tbl))
    check("止损表有表头", any("建议止损" in x for x in tbl))
    check("止损表含2只股", joined.count("| 600000 |") == 1 and joined.count("| 000001 |") == 1)
    check("止损表标注真实", "真实" in joined)
    check("止损表有免责声明", any("非买卖建议" in x for x in tbl))

    rm.analyze_stock_risk = fake_fail
    tbl = rm.stops_md_table([{"code": "600000", "name": "浦发银行", "entry": 10.0}])
    check("单股失败降级该行", "降级" in "\n".join(tbl))

    rm.analyze_stock_risk = fake_raise
    tbl = rm.stops_md_table([{"code": "600000", "name": "浦发银行", "entry": 10.0}])
    check("单股抛异常不中断", isinstance(tbl, list) and any("降级" in x for x in tbl))

    rm.analyze_stock_risk = orig
    tbl = rm.stops_md_table([])
    check("空列表不抛", isinstance(tbl, list) and len(tbl) > 0)
    tbl = rm.stops_md_table(None)
    check("None 入参不抛", isinstance(tbl, list) and len(tbl) > 0)
    tbl = rm.stops_md_table([None, {}, {"code": ""}])
    check("脏 picks 不抛", isinstance(tbl, list))
finally:
    rm.analyze_stock_risk = orig

# ===== 3. 主程序 import 风控降级路径（文本级验证）=====
src = open("daily_ai_top10.py", encoding="utf-8").read()
check("主程序 import 风控", "import risk_management as rm" in src)
check("import 有 try 降级", "RISK_IMPORT_DEGRADED" in src)
check("归档 json 含 risk_management 字段", '"risk_management": risk_payload' in src)
check("md 段调用风控", "lines.extend(risk_md_lines)" in src)
check("风控段不进 push_text（push_text 定义处无风控变量）",
      "push_text = \"\\n\".join(lines)" in src)
check("归档 risk_management.json", '"risk_management.json"' in src)

print(f"\n结果: PASS={PASS} FAIL={FAIL}  (共 {PASS+FAIL} 项)")
sys.exit(1 if FAIL else 0)
