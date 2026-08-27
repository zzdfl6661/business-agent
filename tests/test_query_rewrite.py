from agent.nodes import _fuse_query_routes, _is_precise_lookup


def test_precise_lookup_disables_hyde():
    assert _is_precise_lookup("公司有多少门店")
    assert _is_precise_lookup("店长姓名是什么")
    assert not _is_precise_lookup("顾客投诉怎么安抚")


def test_multi_query_rrf_fuses_same_document():
    a = {"content": "制度A正文", "metadata": {"source": "员工手册.docx"}}
    b = {"content": "制度B正文", "metadata": {"source": "晋升制度.docx"}}
    fused = _fuse_query_routes([[a, b], [a]], 5)
    assert fused[0]["content"] == "制度A正文"
    assert fused[0]["multi_query_rrf"] > fused[1]["multi_query_rrf"]
