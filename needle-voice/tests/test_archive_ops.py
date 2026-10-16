"""Run inside AI2's existing Needle test suite."""
from needle_voice.archive_ops import plan_search

def test_document_route():
    assert plan_search('find my tuition spreadsheet')['category_filter']=='documents'
    assert plan_search('find my tuition spreadsheet')['query']=='tuition'
def test_media_route():
    assert plan_search('find pictures taken in 2018')['category_filter']=='pictures'
    assert plan_search('find pictures taken in 2018')['query']=='2018'
    assert plan_search('find music by Miles Davis')['query']=='Miles Davis'
def test_no_home_or_web_hijack():
    assert plan_search('turn on the kitchen lights') is None
    assert plan_search('search for weather in New York') is None
    assert plan_search('find restaurants near me') is None
def test_no_mutations_or_deep_reasoning():
    assert plan_search('delete my old documents') is None
    assert plan_search('find and compare the contracts') is None
def test_broad_search():
    assert plan_search('find files about kitchen renovation')['category_filter']=='all'
    assert plan_search('find pictures and documents about kitchen renovation')['category_filter']=='all'
