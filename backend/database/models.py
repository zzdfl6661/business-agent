"""
SQLAlchemy 2.x ORM 模型：stores / products / orders / campaigns
=============================================================
设计约定：
- 所有表含 id 主键 + created_at/updated_at（TimestampMixin）
- 金额一律 DECIMAL（精确小数）
- 查询主路径 orders(store_id, order_time) 建复合索引
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    DECIMAL,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Store(TimestampMixin, Base):
    """门店信息"""

    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    store_name: Mapped[str] = mapped_column(String(128), nullable=False)
    region: Mapped[str | None] = mapped_column(String(32), index=True)
    city: Mapped[str | None] = mapped_column(String(32), index=True)
    address: Mapped[str | None] = mapped_column(String(255))
    manager_name: Mapped[str | None] = mapped_column(String(32))
    open_date: Mapped[date | None] = mapped_column(Date)
    search_keyword: Mapped[str | None] = mapped_column(String(64))   # 美团后台下拉搜索关键字
    budget_keyword: Mapped[str | None] = mapped_column(String(64))   # 定位共享预算行关键字（自动化执行用）
    status: Mapped[str] = mapped_column(String(16), default="active")


class Product(TimestampMixin, Base):
    """商品信息（按门店维度）"""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("stores.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    product_name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str | None] = mapped_column(String(32), index=True)
    price: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False)
    cost: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    status: Mapped[str] = mapped_column(String(16), default="active")


class Order(TimestampMixin, Base):
    """订单明细（一行一个商品）"""

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_store_time", "store_id", "order_time"),  # 查询主路径
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    store_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("stores.id"), index=True, nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id"), index=True, nullable=False
    )
    quantity: Mapped[int] = mapped_column(nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    total_amount: Mapped[Decimal] = mapped_column(DECIMAL(12, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(DECIMAL(12, 2), default=0)
    payment_method: Mapped[str | None] = mapped_column(String(16))
    order_time: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    order_status: Mapped[str] = mapped_column(String(16), default="completed")


class Campaign(TimestampMixin, Base):
    """推广计划"""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("stores.id"), index=True, nullable=False
    )
    campaign_name: Mapped[str] = mapped_column(String(128), nullable=False)
    campaign_type: Mapped[str | None] = mapped_column(String(32))  # 满减/折扣券/新客立减
    budget: Mapped[Decimal] = mapped_column(DECIMAL(12, 2), nullable=False)
    spent_amount: Mapped[Decimal] = mapped_column(DECIMAL(12, 2), default=0)
    clicks: Mapped[int] = mapped_column(default=0)          # 点击量（ROI 计算）
    conversions: Mapped[int] = mapped_column(default=0)     # 转化数（ROI 计算）
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), index=True, default="planned")
    channel: Mapped[str | None] = mapped_column(String(32))  # 门店/小程序/美团/抖音


class PromotionReport(TimestampMixin, Base):
    """
    推广数据报告（美团经营宝智选展位下载的真实数据）
    dimension: adv=分推广维度(按推广名聚合) / time=分时段维度(日粒度)
    用于 Agent 基于真实投放数据做 ROI / 花费 / 转化分析。
    """

    __tablename__ = "promotion_reports"
    __table_args__ = (Index("ix_promo_dim_name", "dimension", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dimension: Mapped[str] = mapped_column(String(16), nullable=False)   # adv / time
    name: Mapped[str] = mapped_column(String(64), nullable=False)        # 推广名称 或 日期(08-03)
    target: Mapped[str | None] = mapped_column(String(64))               # 人群定向（adv 维度）
    report_date: Mapped[date | None] = mapped_column(Date)               # time 维度对应日期
    spent: Mapped[Decimal] = mapped_column(DECIMAL(12, 2), default=0)
    impressions: Mapped[int] = mapped_column(default=0)
    clicks: Mapped[int] = mapped_column(default=0)
    cpm: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), default=0)      # 千次曝光均价
    ctr: Mapped[Decimal] = mapped_column(DECIMAL(6, 3), default=0)       # 点击率(%)
    interested: Mapped[int] = mapped_column(default=0)
    reservations: Mapped[int] = mapped_column(default=0)                 # 预约及意向
    favorites: Mapped[int] = mapped_column(default=0)
    view_group: Mapped[int] = mapped_column(default=0)                   # 查看团购
    view_coupon: Mapped[int] = mapped_column(default=0)                  # 查看优惠促销
    orders_paid: Mapped[int] = mapped_column(default=0)                  # 支付订单量
    orders_group: Mapped[int] = mapped_column(default=0)                 # 团购订单量
    ind_reservations: Mapped[int] = mapped_column(default=0)             # 间接预约及意向
    ind_orders_paid: Mapped[int] = mapped_column(default=0)
    ind_orders_group: Mapped[int] = mapped_column(default=0)
    ind_consult: Mapped[int] = mapped_column(default=0)                  # 间接在线咨询沟通量
    new_customer: Mapped[int] = mapped_column(default=0)                 # 新客感兴趣量
    shares: Mapped[int] = mapped_column(default=0)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)


class DataSnapshot(TimestampMixin, Base):
    """
    数据导入快照元信息：记录每个数据集的时间范围（7 天区间 / 对比区间 / 单日粒度）。
    每次导入前先删除同 dataset 旧快照，保证幂等。
    """

    __tablename__ = "data_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    # 本数据集覆盖的时间区间（近 7 天）
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    # 对比区间（智选展位报告有 07-27~08-02 对比，其他数据集可空）
    compare_start: Mapped[date | None] = mapped_column(Date)
    compare_end: Mapped[date | None] = mapped_column(Date)
    source_files: Mapped[str | None] = mapped_column(String(255))
    rows: Mapped[int] = mapped_column(default=0)
    note: Mapped[str | None] = mapped_column(String(255))


class TrafficReport(TimestampMixin, Base):
    """
    客流分析-变化趋势（日×门店粒度，近 7 天）
    来源：经营参谋 → 客流分析 → 变化趋势 下载明细表格
    """

    __tablename__ = "traffic_reports"
    __table_args__ = (Index("ix_traffic_store_date", "store_id", "report_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    store_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)   # 点评门店ID
    store_name: Mapped[str | None] = mapped_column(String(128))
    province: Mapped[str | None] = mapped_column(String(32))
    city: Mapped[str | None] = mapped_column(String(32))
    exposure_users: Mapped[int] = mapped_column(default=0)    # 曝光人数
    exposure_views: Mapped[int] = mapped_column(default=0)    # 曝光次数
    visit_users: Mapped[int] = mapped_column(default=0)       # 访问人数
    visit_views: Mapped[int] = mapped_column(default=0)       # 访问次数
    exp_visit_rate: Mapped[Decimal | None] = mapped_column(DECIMAL(6, 3))  # 曝光访问转化率(%)
    intention_users: Mapped[int] = mapped_column(default=0)   # 意向转化人数
    intention_rate: Mapped[Decimal | None] = mapped_column(DECIMAL(6, 3))   # 意向转化率(%)
    order_users: Mapped[int] = mapped_column(default=0)       # 下单人数
    lead_users: Mapped[int] = mapped_column(default=0)        # 留资人数
    collect_total: Mapped[int] = mapped_column(default=0)     # 累计收藏人数
    collect_new: Mapped[int] = mapped_column(default=0)       # 新增收藏人数
    checkin_new: Mapped[int] = mapped_column(default=0)       # 新增打卡人数
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)


class TrafficLead(TimestampMixin, Base):
    """
    客流分析-引流用户数分析（日×门店×平台粒度）
    来源：经营参谋 → 客流分析 → 引流用户数分析 下载明细表格
    """

    __tablename__ = "traffic_leads"
    __table_args__ = (Index("ix_traffic_lead_store_date", "store_id", "report_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    store_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    store_name: Mapped[str | None] = mapped_column(String(128))
    platform: Mapped[str | None] = mapped_column(String(16))   # ALL / 美团 / 点评
    meituan_leads: Mapped[int] = mapped_column(default=0)      # 美团引流顾客
    no_preaction: Mapped[int] = mapped_column(default=0)       # 没有提前电话/咨询/预约/留资或购买团购
    pre_contact: Mapped[int] = mapped_column(default=0)        # 提前电话/咨询/预约/留资了
    pre_purchase: Mapped[int] = mapped_column(default=0)       # 提前购买了团购
    natural_customers: Mapped[int] = mapped_column(default=0)  # 自然到店顾客
    purchase_after_arrival: Mapped[int] = mapped_column(default=0)  # 到店后购买了团购
    browse_after_arrival: Mapped[int] = mapped_column(default=0)    # 没买团购但到店后线上浏览了
    potential_customers: Mapped[int] = mapped_column(default=0)     # 潜在顾客
    seen_no_action: Mapped[int] = mapped_column(default=0)          # 看过门店没进一步动作也没去其他店
    went_other: Mapped[int] = mapped_column(default=0)              # 去了其他门店
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)


class TransactionReport(TimestampMixin, Base):
    """
    交易分析-商品明细（日×商品×门店粒度）
    来源：经营参谋 → 交易分析 → 商品交易数据（明细版 1434 行）
    """

    __tablename__ = "transaction_reports"
    __table_args__ = (Index("ix_trans_store_date", "store_id", "report_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    product_type: Mapped[str | None] = mapped_column(String(16))   # 团购/预订
    product_id: Mapped[int] = mapped_column(BigInteger, index=True, default=0)
    product_name: Mapped[str | None] = mapped_column(String(128))
    province: Mapped[str | None] = mapped_column(String(32))
    city: Mapped[str | None] = mapped_column(String(32))
    store_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    store_name: Mapped[str | None] = mapped_column(String(128))
    order_users: Mapped[int] = mapped_column(default=0)
    order_coupons: Mapped[int] = mapped_column(default=0)
    order_amount_orig: Mapped[Decimal | None] = mapped_column(DECIMAL(14, 2))
    order_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(14, 2))
    verify_users: Mapped[int] = mapped_column(default=0)
    verify_coupons: Mapped[int] = mapped_column(default=0)
    verify_amount_orig: Mapped[Decimal | None] = mapped_column(DECIMAL(14, 2))
    verify_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(14, 2))
    refund_coupons: Mapped[int] = mapped_column(default=0)
    refund_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(14, 2))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)


class ConsultReport(TimestampMixin, Base):
    """
    在线咨询分析-总览（日×门店粒度）
    来源：经营参谋 → 在线咨询分析 → 在线咨询数据
    """

    __tablename__ = "consult_reports"
    __table_args__ = (Index("ix_consult_store_date", "store_id", "report_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    store_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    store_name: Mapped[str | None] = mapped_column(String(128))
    consult_users: Mapped[int] = mapped_column(default=0)     # 在线咨询人数
    consult_leads: Mapped[int] = mapped_column(default=0)     # 在线咨询留咨数
    lead_rate: Mapped[Decimal | None] = mapped_column(DECIMAL(6, 3))   # 咨询留资转化率(%)
    avg_response_sec: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))  # 平均响应时长(秒)
    reply5_rate: Mapped[Decimal | None] = mapped_column(DECIMAL(6, 3))  # 5分钟内回复率(%)
    reply30_rate: Mapped[Decimal | None] = mapped_column(DECIMAL(6, 3))  # 30秒内回复率(%)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)


class ConsultHourly(TimestampMixin, Base):
    """
    在线咨询分析-分时段（日×小时×门店粒度）
    来源：经营参谋 → 在线咨询分析 → 分时段咨询数据（已 JOIN 总览补门店名）
    """

    __tablename__ = "consult_hourly"
    __table_args__ = (Index("ix_consult_hour_store_date", "store_id", "report_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    store_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    store_name: Mapped[str | None] = mapped_column(String(128))
    hour: Mapped[int] = mapped_column(default=0)
    consult_users: Mapped[int] = mapped_column(default=0)
    reply30_rate: Mapped[Decimal | None] = mapped_column(DECIMAL(6, 3))
    reply5_rate: Mapped[Decimal | None] = mapped_column(DECIMAL(6, 3))
    avg_response_sec: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)


class ChatSession(TimestampMixin, Base):
    """
    对话会话持久化（轻量记忆，非 LangGraph checkpoint）：
    只持久化 messages（JSON 数组），供跨会话/刷新恢复上下文。
    - 不依赖 checkpointer（那是图级中断恢复，我们一次性执行不需要）
    - 长期记忆（经验层）由向量库承担，本表只负责多轮对话上下文
    """

    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    history: Mapped[str] = mapped_column(Text)          # JSON: [{"role","content"}, ...]
    last_question: Mapped[str | None] = mapped_column(String(500))
    message_count: Mapped[int] = mapped_column(default=0)


class AuditLog(TimestampMixin, Base):
    """
    操作审计日志（重要）：记录每次关键操作——对话请求/工具调用/自动执行。
    谁(session) · 何时(created_at) · 做了什么(event_type+detail JSON)
    """

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_time", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)   # chat / tool_call / execute / upload
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(Text)                          # JSON 详情
