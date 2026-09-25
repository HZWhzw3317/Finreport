"""finreport 应用包。

A 股上市公司财报 PDF 批量提取入库系统。

模块概览：
    config    全局配置
    db        Tortoise ORM 初始化
    enums     统一枚举
    models    数据库模型
    schemas   接口出入参
    main      FastAPI 入口
    routers   接口路由
    services  业务逻辑（采集/抽取/编排）
"""
