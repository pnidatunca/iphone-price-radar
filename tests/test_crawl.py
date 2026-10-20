from price_radar.ingest.crawl import CrawlStats


def test_a_rejected_offer_is_kept_with_its_key_and_reason():
    stats = CrawlStats("getmobil")
    stats.reject_offer("getmobil:900101:7001: price_tl: Input should be greater than 0")
    assert stats.rejections == [
        {"offer_key": "getmobil:900101:7001", "reason": "price_tl: Input should be greater than 0"}
    ]
    assert stats.counters["offers_rejected"] == 1
    assert stats.warnings == ["rejected offer getmobil:900101:7001: price_tl: Input should be greater than 0"]
