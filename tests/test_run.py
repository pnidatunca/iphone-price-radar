import json

import httpx
import pytest
from helpers import NOW, FakeSite, page

from price_radar.ingest import run
from price_radar.ingest.schemas import RawOffer

CATALOG = "/satin-al/cep-telefonu/iphone-ios-telefonlar/apple"
UNIT_1 = "/apple-iphone-13-128-gb-mavi-cep-telefonu-700001"
UNIT_2 = "/apple-iphone-14-128-gb-yildiz-isigi-cep-telefonu-700002"


def getmobil_site(**overrides) -> FakeSite:
    routes = {
        f"{CATALOG}/": page("getmobil/brand.html"),
        f"{CATALOG}/iphone-13/?pageNumber=1": page("getmobil/listing_iphone-13_p1.html"),
        f"{CATALOG}/iphone-13/?pageNumber=2": page("getmobil/listing_iphone-13_p2.html"),
        f"{CATALOG}/iphone-14-pro/?pageNumber=1": page("getmobil/listing_iphone-14-pro_p1.html"),
        f"{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900101/": page("getmobil/variant_alternatives.html"),
        f"{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900102/": page("getmobil/variant_out_of_stock.html"),
        f"{CATALOG}/iphone-14-pro/apple-iphone-14-pro-256-gb-gumus-900103/": page("getmobil/variant_esim.html"),
    }
    return FakeSite("https://getmobil.com", routes | overrides)


def easycep_site() -> FakeSite:
    routes = {
        "/kategori/apple-2?page=1": page("easycep/listing_p1.html"),
        "/kategori/apple-2?page=2": page("easycep/listing_p2.html"),
        UNIT_1: page("easycep/unit_flags.html"),
        UNIT_2: page("easycep/unit_esim.html"),
        "/apple-iphone-12-mini-64-gb-siyah-cep-telefonu-700003": page("easycep/unit_unknown_token.html"),
    }
    return FakeSite("https://easycep.com", routes)


def trendyol_site() -> FakeSite:
    category = "/apple-yenilenmis-cep-telefonu-x-b101470-c109461"
    routes = {
        category: page("trendyol/listing_p1.html"),
        f"{category}?pi=2": page("trendyol/listing_p2.html"),
        "/apple/yenilenmis-iphone-13-128-gb-mavi-ornek-urun-a-kalite-p-900000101": page(
            "trendyol/product_buybox_others.html"
        ),
        "/apple/iphone-12-mini-siyah-64gb-xyz1tu-a-ornek-outlet-c-kalite-p-900000105": page(
            "trendyol/product_misfiled.html"
        ),
    }
    return FakeSite("https://www.trendyol.com", routes)


def cli(args: list[str], site: FakeSite) -> int:
    return run.main(args, transport=site.transport(), sleep=lambda seconds: None, now=lambda: NOW)


def test_cli_writes_offers_and_a_summary(tmp_path):
    assert cli(["--source", "getmobil", "--out", str(tmp_path)], getmobil_site()) == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == ["offers.jsonl", "run_summary.json"]  # never raw HTML
    offers = [
        RawOffer.model_validate_json(line) for line in (tmp_path / "offers.jsonl").read_text("utf-8").splitlines()
    ]
    assert len(offers) == 7 and len({o.offer_key for o in offers}) == 7
    summary = json.loads((tmp_path / "run_summary.json").read_text("utf-8"))
    assert summary["stop_reason"] == "completed"
    assert (summary["offers"], summary["in_stock_offers"]) == (7, 7)
    assert summary["sellers_per_variant"] == {"1": 1, "6": 1}
    assert summary["pages_fetched"] == {"brand": 1, "listing": 3, "variant": 4}
    assert summary["status_codes"] == {"200": 7, "404": 1}
    assert summary["requests"] == {"made": 9, "budget": 1500}
    assert summary["response_seconds"]["count"] == 9
    assert summary["snapshot_date"] == "2026-10-12"
    assert summary["counters"]["variants_removed"] == 1


def test_summary_only_prints_markdown_and_writes_no_offers(tmp_path, capsys):
    args = ["--source", "easycep", "--out", str(tmp_path), "--max-requests", "40", "--summary-only"]
    assert cli(args, easycep_site()) == 0
    assert [path.name for path in tmp_path.iterdir()] == ["run_summary.json"]
    markdown = capsys.readouterr().out
    assert markdown.startswith("### Access test: easycep")
    assert "| Offers | 3 (3 in stock) |" in markdown
    assert "| Requests | 6 of 40 |" in markdown
    assert "Example Store" not in markdown and "M-PIP" not in markdown


def test_cli_collects_trendyol_offers_with_list_prices(tmp_path, capsys):
    assert cli(["--source", "trendyol", "--out", str(tmp_path), "--markdown"], trendyol_site()) == 0
    lines = (tmp_path / "offers.jsonl").read_text("utf-8").splitlines()
    offers = [RawOffer.model_validate_json(line) for line in lines]
    assert [o.offer_key for o in offers] == [
        "trendyol:900000101:7001",
        "trendyol:900000101:7002",
        "trendyol:900000101:7003",
        "trendyol:900000105:7011",
    ]
    assert all(o.list_price_tl >= o.price_tl and o.promo_pct is not None for o in offers)
    assert all('"list_price_tl":' in line and '"promo_pct":' in line for line in lines)
    summary = json.loads((tmp_path / "run_summary.json").read_text("utf-8"))
    assert (summary["source"], summary["parser_version"], summary["stop_reason"]) == (
        "trendyol",
        "trendyol-1",
        "completed",
    )
    assert summary["pages_fetched"] == {"listing": 2, "product": 7}  # five of them answer 404 here
    assert summary["counters"]["products_removed"] == 5
    assert summary["discovery"]["products_seen"] == 36
    markdown = capsys.readouterr().out
    assert markdown.startswith("### Collection run: trendyol")
    assert "Decoy Seller" not in markdown and "@example.com" not in markdown


def test_known_ids_skip_their_unit_pages(tmp_path):
    known = tmp_path / "known.txt"
    known.write_text("# unit pages read before\neasycep:700001\n700002  # second one\n\n", encoding="utf-8")
    site = easycep_site()
    assert cli(["--source", "easycep", "--out", str(tmp_path / "out"), "--known-ids", str(known)], site) == 0
    assert UNIT_1 not in site.requested and UNIT_2 not in site.requested


def test_known_ids_are_easycep_only(tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        run.parse_args(["--source", "getmobil", "--out", str(tmp_path), "--known-ids", "ids.txt"])
    assert exit_info.value.code == 2


def test_a_malformed_known_ids_file_is_a_usage_error(tmp_path):
    known = tmp_path / "known.txt"
    known.write_text("700001\nnot-an-id\n", encoding="utf-8")
    assert cli(["--source", "easycep", "--out", str(tmp_path), "--known-ids", str(known)], easycep_site()) == 2


def test_a_spent_budget_ends_the_run_normally(tmp_path):
    assert cli(["--source", "getmobil", "--out", str(tmp_path), "--max-requests", "3"], getmobil_site()) == 0
    summary = json.loads((tmp_path / "run_summary.json").read_text("utf-8"))
    assert summary["stop_reason"] == "request budget of 3 spent"
    assert summary["requests"] == {"made": 3, "budget": 3}


def test_a_bot_challenge_fails_the_run_but_keeps_the_summary(tmp_path):
    site = getmobil_site(**{f"{CATALOG}/": httpx.Response(403)})
    assert cli(["--source", "getmobil", "--out", str(tmp_path)], site) == 1
    summary = json.loads((tmp_path / "run_summary.json").read_text("utf-8"))
    assert summary["stop_reason"].startswith("bot challenge or HTTP 403")
    assert (tmp_path / "offers.jsonl").read_text("utf-8") == ""


def test_percentiles_use_the_nearest_rank():
    assert run.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert run.percentile([1.0, 2.0, 3.0, 4.0], 0.9) == 4.0
    assert run.percentile([], 0.5) is None
