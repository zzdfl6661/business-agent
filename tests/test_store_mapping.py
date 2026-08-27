from types import SimpleNamespace

from services.store_mapping import build_platform_mapping, match_store


def _store(store_id, name, keyword):
    return SimpleNamespace(
        id=store_id, store_name=name, search_keyword=keyword, budget_keyword=keyword,
    )


def test_exact_and_brand_keyword_mapping():
    stores = [
        _store(1, "鬼十八密室逃脱（长宁来福士店）", "长宁"),
        _store(2, "bb boom运动闯关（月亮湾店）", "月亮湾"),
    ]
    assert match_store("鬼十八密室逃脱·真人NPC\\n（中山公园长宁来福士店）", stores).id == 1
    assert match_store("bb boom运动闯关密室·团建聚会·家庭亲子（月亮湾店）", stores).id == 2


def test_mapping_reports_unmatched_without_guessing():
    stores = [_store(1, "异时刻密室逃脱（杭州店）", "杭州")]
    mapping, unmatched = build_platform_mapping([
        {"store_id": 100, "store_name": "异时刻密室逃脱（杭州店）"},
        {"store_id": 200, "store_name": "异时刻密室逃脱（滨江道店）"},
    ], stores)
    assert mapping == {100: 1}
    assert unmatched == ["异时刻密室逃脱（滨江道店）"]
