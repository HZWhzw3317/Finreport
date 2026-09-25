"""财报抽取模块。

    accounts      标准科目字典（内置）
    numbers       金额解析与单位识别
    normalizer    科目名归一化
    locator       报表页面定位
    table_parser  有框线表格解析
    text_parser   无框线文本行解析
    rule_extract  规则抽取编排（表格+文本+合并+补齐）
    validator     校验与置信度
    llm_fallback  视觉大模型兜底
    result        解析过程数据载体
"""
