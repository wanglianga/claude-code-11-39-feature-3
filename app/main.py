"""Cross-border export declaration & remittance facts service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html

from .config import settings
from .db import init_db
from .routers import admin, audit, intake, reconciliation


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_create_schema:
        init_db()
    yield


DESCRIPTION = """
跨境出口申报与到账事实对账数据服务。

系统只交付可审计的**出口申报与到账对账事实**，不代办报关、不提交退税申报，
不操作库存、税务余额或银行资金，也不生成付款、授信或税务处理指令。

核心规则
- 来源受理账 append-only 保存发票、装箱单、报关档案及更正、税务回执、银行流水、
  主体目录和合同附件的原文位置、事件时间、接收时间、外部编号、币种、金额、数量、
  哈希及来源版本。
- 申报状态账（draft/submitted/verified/rejected/correction_pending）与到账状态账
  （unmatched/pending_allocation/partial/matched/conflict）分离，不共享已结清字段。
- 行级身份解析器生成带证据等级（A/B/C）的候选案卷。
- 余额控制器同时维护银行流水与原出口行的可占用金额；先确认唯一关联，再对合并流水
  生成人工确认候选。未确认金额始终留在待分摊余额中。
- 合并流水缺少唯一批次键时只生成候选，绝不按金额比例或行数自动分摊。
- 并发确认时原子校验并占用两侧余额，累计确认额不得超过任一侧实际金额。
- 海关更正建立修订链，已提交行转为更正待处理，旧审核记录仍可回查。
- 税务回执乱序时按签发时间与可验证序号转换；无法证明顺序时进入回执冲突。
- 已确认关联进入复审时，新编译不把它计入已证实到账量，占用金额冻结。

跨主体出口证据链一致性与授权时效审计
- 主体档案册、授权凭证册与证据关系图在**出口日**判定跨主体关系；接收时间不替代出口日状态。
- 出口后接收但出口日前已生效的授权可支持历史关系（复核核准后可引用）。
- 出口后签发且无可验证追溯范围的授权只能标为待补证，不能回填出口日资格。
- 部分货权覆盖拆为受限确认与货权断链两段；同一货权份额多路径只算一次；
  搜索截断只给待补证，不断言不存在有效授权。
- 互斥撤销冻结授权链；共享凭证变动先定位依赖行，再逐行重判，仅暂停失去有效见证的行。
"""

app = FastAPI(
    title="跨境出口申报与到账事实对账数据服务",
    version="1.0.0",
    description=DESCRIPTION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

app.include_router(admin.router)
app.include_router(intake.router)
app.include_router(reconciliation.router)
app.include_router(audit.router)


@app.get("/health", tags=["meta"])
def health():
    """存活探针：返回服务名与状态，不触碰业务数据。"""
    return {"status": "ok", "service": "cross-border-export-reconciliation"}


@app.get("/docs", include_in_schema=False)
def swagger_ui():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " — Swagger UI",
    )


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": app.title,
        "version": app.version,
        "openapi": "/openapi.json",
        "swagger_ui": "/docs",
        "scope_note": "facts only: no customs filing, refund, inventory or fund operations",
    }
