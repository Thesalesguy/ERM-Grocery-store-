"""The application must import and construct without raising."""

from app.main import app


def test_app_is_created() -> None:
    assert app is not None
    assert app.title == "Grocery ERP/POS"
