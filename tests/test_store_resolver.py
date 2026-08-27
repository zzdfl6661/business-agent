from tools import store_resolver


def test_resolves_unique_abbreviated_mall_name(monkeypatch):
    monkeypatch.setattr(
        store_resolver,
        "_store_cache",
        [
            {"store_id": 6, "name": "异时刻密室(上海长风大悦城店)", "search_keyword": "长风大悦城", "city": ""},
            {"store_id": 20, "name": "Xcape密室(静安大悦城店)", "search_keyword": "静安大悦城", "city": ""},
        ],
    )
    assert store_resolver.resolve_store_id("分析上海大悦城门店数据，并通知店长") == 6


def test_does_not_guess_ambiguous_mall_name(monkeypatch):
    monkeypatch.setattr(
        store_resolver,
        "_store_cache",
        [
            {"store_id": 6, "name": "异时刻密室(上海长风大悦城店)", "search_keyword": "长风大悦城", "city": ""},
            {"store_id": 20, "name": "Xcape密室(静安大悦城店)", "search_keyword": "静安大悦城", "city": "上海"},
        ],
    )
    assert store_resolver.resolve_store_id("分析上海大悦城门店数据") is None
