"""高并发压测脚本：向风控异步API服务发起并发申请，统计吞吐与耗时分位。

用法（先启动服务: python server.py）：
    python scripts/load_test.py --concurrency 50 --requests 200
    python scripts/load_test.py --concurrency 100 --requests 500 --url http://127.0.0.1:8000

原理：以演示案例为模板批量生成独立申请单（唯一申请单号/申请人/设备），
httpx.AsyncClient 并发提交后轮询状态直至终态，汇总端到端耗时分布。
"""
import argparse
import asyncio
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from main import build_demo_cases

TERMINAL = {"COMPLETED", "FAILED", "TIMEOUT"}
CASE_TEMPLATES = ("normal", "fraud", "review", "supplement", "compliance")


def _variant(template_name: str, index: int):
    """基于演示案例模板生成独立申请单。

    保留模板的三要素（姓名/身份证/手机号，与Mock核验注册表一致，保证
    进件核验通过、决策分布真实），仅申请单号/设备/IP唯一化。
    同一模板的申请共享手机号，高并发下会真实触发图谱SHARED_PHONE
    关联检测——顺带压测团伙识别路径的并发安全。
    """
    from credit_risk_system.models.loan import LoanApplication

    app: LoanApplication = build_demo_cases()[template_name].model_copy(deep=True)
    app.application_id = f"LOAD2026{index:06d}"
    app.applicant.applicant_id = f"LOAD-CUST-{index:06d}"
    app.device.device_id = f"LOAD-DEV-{index:06d}"
    app.device.fingerprint = f"load-fp-{index}"
    app.device.ip = f"10.{index % 250 + 1}.{index % 256}.{index % 256}"
    return app


async def _submit_one(client: httpx.AsyncClient, app) -> str:
    resp = await client.post("/api/v1/applications", json=app.model_dump(mode="json"))
    resp.raise_for_status()
    return resp.json()["application_id"]


async def _poll_until_done(client: httpx.AsyncClient, app_id: str,
                           poll_interval: float = 0.05) -> dict:
    while True:
        resp = await client.get(f"/api/v1/applications/{app_id}")
        resp.raise_for_status()
        status = resp.json()
        if status["status"] in TERMINAL:
            return status
        await asyncio.sleep(poll_interval)


async def run_load(base_url: str, total: int, concurrency: int) -> None:
    sem = asyncio.Semaphore(concurrency)
    results: list = []
    decisions: dict = {}
    counter = 0
    counter_lock = asyncio.Lock()
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)

    async def one(index: int, client: httpx.AsyncClient):
        nonlocal counter
        app = _variant(CASE_TEMPLATES[index % len(CASE_TEMPLATES)], index)
        async with sem:
            t0 = time.perf_counter()
            app_id = await _submit_one(client, app)
            status = await _poll_until_done(client, app_id)
            elapsed = (time.perf_counter() - t0) * 1000
            results.append(elapsed)
            decisions[status["status"]] = decisions.get(status["status"], 0) + 1
            decision = (status.get("final_decision") or {}).get("decision", "-")
            async with counter_lock:
                counter += 1
                print(f"  [{counter}/{total}] {app_id} -> {decision} "
                      f"({elapsed:.0f}ms)", flush=True)

    t_start = time.perf_counter()
    # trust_env=False: 压测本机服务时不走系统代理（避免代理回502）
    async with httpx.AsyncClient(base_url=base_url, timeout=120.0, limits=limits,
                                 trust_env=False) as client:
        await asyncio.gather(*(one(i, client) for i in range(total)))
    wall = time.perf_counter() - t_start

    print()
    print("=" * 72)
    print(f"  压测完成 | 总数={total} | 并发={concurrency} | "
          f"总耗时={wall:.1f}s | 吞吐={total / wall:.1f} 笔/秒")
    print("-" * 72)
    if results:
        sorted_ms = sorted(results)
        pct = lambda p: sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * p))]
        print(f"  端到端耗时: avg={statistics.mean(results):.0f}ms "
              f"| p50={pct(0.50)}ms | p90={pct(0.90)}ms | p99={pct(0.99)}ms "
              f"| max={max(results):.0f}ms")
    print(f"  终态分布: {decisions}")
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0,
                                 trust_env=False) as client:
        stats = (await client.get("/api/v1/stats")).json()
    print(f"  服务端统计: {stats}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description="信贷风控异步服务压测")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="服务地址")
    parser.add_argument("--concurrency", type=int, default=50, help="并发提交数")
    parser.add_argument("--requests", type=int, default=200, help="总申请数")
    args = parser.parse_args()

    # 压测案例随机化：以5个演示案例为模板轮转
    random.seed(2026)
    print(f"目标: {args.url} | 总数={args.requests} | 并发={args.concurrency}")
    asyncio.run(run_load(args.url, args.requests, args.concurrency))
    return 0


if __name__ == "__main__":
    sys.exit(main())
