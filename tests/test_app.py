"""应用装配冒烟测试。

验证 app.main 可正常导入，且关键路由已注册（通过 OpenAPI schema 检查）。
"""


def test_app_import_and_routes():
    """导入应用并确认核心接口路径存在。"""
    from app.main import app

    # 通过生成 OpenAPI schema 间接校验路由注册（避免暴露内部路由对象）
    paths = set(app.openapi()["paths"].keys())
    assert "/api/health" in paths
    assert "/api/companies" in paths
    assert "/api/reports" in paths
    assert "/api/jobs/download" in paths
    assert "/api/jobs/parse" in paths
