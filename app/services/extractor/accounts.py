"""三大报表标准科目字典（内置兜底，可通过 account_dict 表扩展）。

数据结构约定——每个科目是一个四元组::

    (item_code, item_name_std, synonyms, is_core)
        item_code      英文编码，作为入库的稳定主键（statement_item.item_code）
        item_name_std  标准中文名
        synonyms       同义词列表（PDF 中可能出现的其它写法）
        is_core        是否核心科目；核心科目用于校验是否“抽全了”

is_core 的取舍原则：只保留跨行业普适的合计数（如资产总计、负债合计、
所有者权益合计、营业收入、净利润、经营活动现金流净额等），不把
“流动/非流动合计”这类银行等金融企业没有的科目设为核心，避免误触发兜底。

新增科目/同义词时可修改本文件，或写入 account_dict 表由
``scripts/seed_accounts.py`` 导入后在运行期覆盖。
"""

from app.enums import StatementType

# ============================================================
# 资产负债表科目
# ============================================================
BS_ITEMS = [
    # ---- 流动资产 ----
    ("cash", "货币资金", ["货币资金", "现金及银行存款"], True),
    ("settlement_provisions", "结算备付金", [], False),
    ("trading_assets", "交易性金融资产", ["以公允价值计量且其变动计入当期损益的金融资产"], False),
    ("notes_receivable", "应收票据", [], False),
    ("accounts_receivable", "应收账款", [], False),
    ("receivables_financing", "应收款项融资", [], False),
    ("prepayments", "预付款项", ["预付账款"], False),
    ("other_receivables", "其他应收款", [], False),
    ("inventories", "存货", [], False),
    ("contract_assets", "合同资产", [], False),
    ("current_assets_total", "流动资产合计", ["流动资产总计"], False),
    # ---- 非流动资产 ----
    ("debt_investments", "债权投资", [], False),
    ("long_term_equity_investment", "长期股权投资", [], False),
    ("other_equity_investments", "其他权益工具投资", [], False),
    ("fixed_assets", "固定资产", [], False),
    ("construction_in_progress", "在建工程", [], False),
    ("right_of_use_assets", "使用权资产", [], False),
    ("intangible_assets", "无形资产", [], False),
    ("development_expenditure", "开发支出", [], False),
    ("goodwill", "商誉", [], False),
    ("deferred_tax_assets", "递延所得税资产", [], False),
    ("non_current_assets_total", "非流动资产合计", ["非流动资产总计"], False),
    ("total_assets", "资产总计", ["资产合计"], True),
    # ---- 流动负债 ----
    ("short_term_loans", "短期借款", [], False),
    ("notes_payable", "应付票据", [], False),
    ("accounts_payable", "应付账款", [], False),
    ("contract_liabilities", "合同负债", [], False),
    ("advance_from_customers", "预收款项", ["预收账款"], False),
    ("employee_benefits_payable", "应付职工薪酬", [], False),
    ("taxes_payable", "应交税费", [], False),
    ("other_payables", "其他应付款", [], False),
    ("current_liabilities_total", "流动负债合计", ["流动负债总计"], False),
    # ---- 非流动负债 ----
    ("long_term_loans", "长期借款", [], False),
    ("bonds_payable", "应付债券", [], False),
    ("lease_liabilities", "租赁负债", [], False),
    ("non_current_liabilities_total", "非流动负债合计", ["非流动负债总计"], False),
    ("total_liabilities", "负债合计", ["负债总计"], True),
    # ---- 所有者权益 ----
    ("paid_in_capital", "实收资本（股本）", ["实收资本", "股本"], False),
    ("capital_reserve", "资本公积", [], False),
    ("surplus_reserve", "盈余公积", [], False),
    ("undistributed_profit", "未分配利润", [], False),
    (
        "equity_attributable_to_parent",
        "归属于母公司所有者权益合计",
        [
            "归属于母公司股东权益合计",
            "归属于母公司所有者权益总计",
            "归属于母公司所有者权益（或股东权益）合计",
            "归属于母公司股东权益（或股东权益）合计",
        ],
        True,
    ),
    ("minority_interests", "少数股东权益", [], False),
    (
        "total_equity",
        "所有者权益合计",
        [
            "股东权益合计",
            "所有者权益总计",
            "所有者权益（或股东权益）合计",
            "股东权益（或股东权益）合计",
        ],
        True,
    ),
    (
        "total_liabilities_and_equity",
        "负债和所有者权益总计",
        ["负债和股东权益总计", "负债和所有者权益合计"],
        False,
    ),
]

# ============================================================
# 利润表科目
# ============================================================
IS_ITEMS = [
    ("operating_revenue", "营业收入", [], True),
    # 营业总收入（含利息收入等其他收入）单独成项，避免与“营业收入”混淆
    ("total_operating_revenue", "营业总收入", [], False),
    # 银行等金融企业用“营业支出”表述，作为“营业成本”的同义词
    ("operating_cost", "营业成本", ["营业支出"], False),
    ("taxes_and_surcharges", "税金及附加", ["营业税金及附加"], False),
    ("selling_expenses", "销售费用", [], False),
    ("admin_expenses", "管理费用", [], False),
    ("rd_expenses", "研发费用", [], False),
    ("finance_expenses", "财务费用", [], False),
    ("interest_expense", "利息费用", [], False),
    ("interest_income", "利息收入", [], False),
    ("other_income", "其他收益", [], False),
    ("investment_income", "投资收益", [], False),
    ("fair_value_change", "公允价值变动收益", [], False),
    ("credit_impairment_loss", "信用减值损失", [], False),
    ("asset_impairment_loss", "资产减值损失", [], False),
    ("asset_disposal_income", "资产处置收益", [], False),
    ("operating_profit", "营业利润", [], True),
    ("non_operating_income", "营业外收入", [], False),
    ("non_operating_expense", "营业外支出", [], False),
    ("total_profit", "利润总额", [], True),
    ("income_tax_expense", "所得税费用", [], False),
    ("net_profit", "净利润", [], True),
    (
        "net_profit_attributable_to_parent",
        "归属于母公司所有者的净利润",
        ["归属于母公司股东的净利润", "归属于上市公司股东的净利润"],
        True,
    ),
    ("minority_profit", "少数股东损益", [], False),
    ("total_comprehensive_income", "综合收益总额", [], False),
    ("basic_eps", "基本每股收益", [], False),
    ("diluted_eps", "稀释每股收益", [], False),
]

# ============================================================
# 现金流量表科目
# ============================================================
CF_ITEMS = [
    # ---- 经营活动 ----
    ("cash_from_sales", "销售商品、提供劳务收到的现金", [], False),
    ("cash_received_tax_refund", "收到的税费返还", [], False),
    ("operating_cash_inflow", "经营活动现金流入小计", [], False),
    ("cash_paid_goods_services", "购买商品、接受劳务支付的现金", [], False),
    ("cash_paid_employees", "支付给职工以及为职工支付的现金", [], False),
    ("cash_paid_taxes", "支付的各项税费", [], False),
    ("operating_cash_outflow", "经营活动现金流出小计", [], False),
    ("net_cash_flow_operating", "经营活动产生的现金流量净额", [], True),
    # ---- 投资活动 ----
    ("cash_from_investments", "收回投资收到的现金", [], False),
    ("investment_income_cash", "取得投资收益收到的现金", [], False),
    (
        "fixed_asset_disposal_cash",
        "处置固定资产、无形资产和其他长期资产收回的现金净额",
        [],
        False,
    ),
    ("investing_cash_inflow", "投资活动现金流入小计", [], False),
    ("cash_paid_investments", "投资支付的现金", [], False),
    ("capex_cash", "购建固定资产、无形资产和其他长期资产支付的现金", [], False),
    ("investing_cash_outflow", "投资活动现金流出小计", [], False),
    ("net_cash_flow_investing", "投资活动产生的现金流量净额", [], True),
    # ---- 筹资活动 ----
    ("cash_from_borrowings", "取得借款收到的现金", [], False),
    ("financing_cash_inflow", "筹资活动现金流入小计", [], False),
    ("cash_repay_debts", "偿还债务支付的现金", [], False),
    ("dividends_interest_paid", "分配股利、利润或偿付利息支付的现金", [], False),
    ("financing_cash_outflow", "筹资活动现金流出小计", [], False),
    ("net_cash_flow_financing", "筹资活动产生的现金流量净额", [], True),
    # ---- 现金净增加与余额 ----
    ("exchange_rate_effect", "汇率变动对现金及现金等价物的影响", [], False),
    ("net_increase_in_cash", "现金及现金等价物净增加额", [], True),
    ("cash_beginning", "期初现金及现金等价物余额", [], False),
    ("cash_end", "期末现金及现金等价物余额", [], True),
]

# 报表类型 -> 科目列表
DEFAULT_ACCOUNTS = {
    StatementType.bs: BS_ITEMS,
    StatementType.is_: IS_ITEMS,
    StatementType.cf: CF_ITEMS,
}

# 报表定位关键词（(报表类型, 口径) -> 标题关键词）。
# 口径命名多样：母公司/公司/银行/本行，故列多个候选。
STATEMENT_PAGE_KEYWORDS = {
    (StatementType.bs, "consolidated"): ["合并资产负债表"],
    (StatementType.bs, "parent"): ["母公司资产负债表", "公司资产负债表", "银行资产负债表"],
    (StatementType.is_, "consolidated"): ["合并利润表"],
    (StatementType.is_, "parent"): ["母公司利润表", "公司利润表", "银行利润表"],
    (StatementType.cf, "consolidated"): ["合并现金流量表"],
    (StatementType.cf, "parent"): ["母公司现金流量表", "公司现金流量表", "银行现金流量表"],
}

# 报表名简写，供定位器按“合并+报表名 / 报表名”规则匹配（见 locator.py）
STATEMENT_WORDS = {
    StatementType.bs: "资产负债表",
    StatementType.is_: "利润表",
    StatementType.cf: "现金流量表",
}

# 遇到这些关键词说明报表正文已结束（进入附注），用于限定报表页范围
STOP_KEYWORDS = ["财务报表附注", "合并财务报表项目注释", "母公司财务报表主要项目注释"]


def iter_accounts(stype: StatementType):
    """遍历某报表的全部内置科目。

    :return: 生成器，每项为 (item_code, item_name_std, synonyms, is_core, sort_order)
    """
    for idx, (code, name, syn, core) in enumerate(DEFAULT_ACCOUNTS[stype]):
        yield code, name, syn, core, idx
