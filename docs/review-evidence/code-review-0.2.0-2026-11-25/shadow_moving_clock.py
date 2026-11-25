"""影子账户在“真实时钟”下的行为检查。

仓库自带的影子账户测试把 now_utc 固定成一个常量，因此 shadow._freeze 里 `if when != clock:`
这条只在真实运行时才会走到的分支（在写事务里按实际时刻重新核验条款和首簿）没有被任何测试执行过。
本脚本复用 tests/test_shadow_settlement.py 的合成夹具，只把时钟换成每次读取前进一小步的时钟，
对比固定时钟与前进时钟下同一场景的结果。不修改任何仓库文件，数据写在临时目录。

用法：PYTHONDONTWRITEBYTECODE=1 python3 shadow_moving_clock.py [仓库根目录] [每次读取前进的毫秒数=1]
"""

import itertools
import json
import sys
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
STEP_MS = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import test_opportunity as f  # noqa: E402
import test_shadow_settlement as S  # noqa: E402
from polymarket861 import shadow, store  # noqa: E402

MODULES = (store, f.pipeline, f.datasets, f.input_manifest, f.evidence, f.evidence_sources, f.archive)


@contextmanager
def moving(start, step_ms):
    """每读一次时钟前进 step_ms 毫秒，模拟真实运行里两次读钟之间必然存在的间隔。"""
    ticks = itertools.count()

    def now():
        return store.iso(start + timedelta(milliseconds=step_ms) * next(ticks))

    with ExitStack() as stack:
        for module in MODULES:
            stack.enter_context(patch.object(module, "now_utc", side_effect=now))
        yield


def scenario(clock):
    case = S.ShadowAndSettlement("test_first_run_report_has_no_fictitious_balance")
    case.setUp()
    try:
        seal = case.setup_model(technical=True)
        case.native_state(f.CUT - timedelta(minutes=4))
        price = (Decimal(str(seal["probabilities"]["total_over_2_5"])) - Decimal(".05")).quantize(
            Decimal(".01"), rounding=ROUND_FLOOR
        )
        case.case.book(f.CUT - timedelta(seconds=6), price)
        with clock(f.CUT - timedelta(seconds=4)):
            first = shadow.run(case.root, f.SLUG)
        case.case.book(f.CUT + timedelta(seconds=6), price)
        with clock(f.CUT + timedelta(seconds=7)):
            second = shadow.run(case.root, f.SLUG)
        with f.at(f.CUT + timedelta(seconds=30)):
            report = shadow.report(case.root)
        return {
            "freeze_actions": [(a["state"], a.get("reason")) for a in first["actions"]],
            "freeze_gaps": first["gaps"],
            "execute_actions": [(a["state"], a.get("reason")) for a in second["actions"]],
            "execute_gaps": second["gaps"],
            "counts": report["counts"],
        }
    finally:
        case.doCleanups()


if __name__ == "__main__":
    fixed = scenario(f.at)
    stepped = scenario(lambda when: moving(when, STEP_MS))
    print(json.dumps({"fixed_clock": fixed, "moving_clock_ms": STEP_MS, "moving_clock": stepped}, ensure_ascii=False, indent=1, default=str))
