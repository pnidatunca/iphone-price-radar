from datetime import date

import pytest

from price_radar.config import load_settings
from price_radar.ingest.normalize import canonical_model
from price_radar.storage.devices import COLUMNS, SEED, annotations, compare, read_seed, report_lines

HEADER = ",".join(COLUMNS)
ROW = "iPhone 13,13,standart,2021-09-24,128,799,,https://www.apple.com/newsroom/2021/09/example/,"
# Decision K21: iPhone X and newer, with SE 2020 and SE 2022. Later models are added as they are released.
SCOPE = (
    "iPhone X",
    "iPhone XS",
    "iPhone XS Max",
    "iPhone XR",
    "iPhone 11",
    "iPhone 11 Pro",
    "iPhone 11 Pro Max",
    "iPhone SE (2020)",
    "iPhone 12 mini",
    "iPhone 12",
    "iPhone 12 Pro",
    "iPhone 12 Pro Max",
    "iPhone 13 mini",
    "iPhone 13",
    "iPhone 13 Pro",
    "iPhone 13 Pro Max",
    "iPhone SE (2022)",
    "iPhone 14",
    "iPhone 14 Plus",
    "iPhone 14 Pro",
    "iPhone 14 Pro Max",
    "iPhone 15",
    "iPhone 15 Plus",
    "iPhone 15 Pro",
    "iPhone 15 Pro Max",
    "iPhone 16",
    "iPhone 16 Plus",
    "iPhone 16 Pro",
    "iPhone 16 Pro Max",
    "iPhone 16e",
    "iPhone 17",
    "iPhone Air",
    "iPhone 17 Pro",
    "iPhone 17 Pro Max",
    "iPhone 17e",  # March 2026
    "iPhone 18 Pro",  # September 2026
    "iPhone 18 Pro Max",
    "iPhone Duo",
)


def seed(tmp_path, *rows, header=HEADER):
    path = tmp_path / "devices.csv"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def test_the_seed_covers_the_scope_in_the_normalisers_spelling():
    devices = read_seed(SEED)
    models = {d.model for d in devices}
    assert set(SCOPE) <= models
    scope = load_settings().scope
    for device in devices:
        assert canonical_model(device.model) == device.model and scope.includes(device.model), device.model
        assert device.release_date >= date(2017, 11, 3), device.model


def test_the_seed_follows_the_column_rules():
    devices = {(d.model, d.storage_gb): d for d in read_seed(SEED)}
    by_model = {d.model: d for d in devices.values()}
    assert {m: (by_model[m].generation, by_model[m].series) for m in ("iPhone X", "iPhone XS Max", "iPhone XR")} == {
        "iPhone X": (10, "Pro"),
        "iPhone XS Max": (10, "Pro Max"),
        "iPhone XR": (10, "standart"),
    }
    assert (by_model["iPhone SE (2020)"].generation, by_model["iPhone SE (2020)"].series) == (11, "SE")
    assert (by_model["iPhone SE (2022)"].generation, by_model["iPhone Air"].generation) == (13, 17)
    assert (by_model["iPhone 16e"].generation, by_model["iPhone 16e"].series) == (16, "e")
    assert (by_model["iPhone 12 mini"].series, by_model["iPhone 15 Plus"].series) == ("mini", "Plus")
    for device in devices.values():  # one release date and generation per model, prices rising with storage
        assert (device.release_date, device.generation) == (
            by_model[device.model].release_date,
            by_model[device.model].generation,
        )
    for (model, size), device in devices.items():
        larger = [d for (m, s), d in devices.items() if m == model and s > size]
        assert all(d.us_launch_price_usd > device.us_launch_price_usd for d in larger), model


def test_a_valid_row_is_read_with_an_empty_turkish_price(tmp_path):
    (device,) = read_seed(seed(tmp_path, ROW))
    assert (device.model, device.storage_gb, device.us_launch_price_usd) == ("iPhone 13", 128, 799)
    assert (device.tr_launch_price_tl, device.tr_source_url) == (None, None)


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        (ROW.replace("iPhone 13,", "iPhone 13 Mini,"), "not spelled as normalize.canonical_model writes it"),
        (ROW.replace("standart", "Standard"), "series"),
        (ROW.replace(",128,", ",100,"), "100 GB is not an iPhone storage size"),
        (ROW.replace(",799,,", ",799,32999,"), "tr_launch_price_tl and tr_source_url go together"),
        (ROW.replace("https://", "http://"), "a source must be an https:// URL"),
    ],
)
def test_a_bad_row_is_refused_with_its_line(tmp_path, row, problem):
    with pytest.raises(ValueError, match=r"devices\.csv:3:") as err:
        read_seed(seed(tmp_path, ROW.replace(",128,", ",256,"), row))
    assert problem in str(err.value)


def test_a_model_and_size_may_appear_once(tmp_path):
    with pytest.raises(ValueError, match="devices.csv:3: iPhone 13 128 GB appears twice"):
        read_seed(seed(tmp_path, ROW, ROW))
    with pytest.raises(ValueError, match="the header must be"):
        read_seed(seed(tmp_path, ROW, header=HEADER.replace("storage_gb", "storage")))


def test_unknown_models_and_sizes_are_reported_apart():
    seen = [
        ("iPhone 13", 128, 900, date(2026, 11, 2)),
        ("iPhone 13", 64, 3, date(2026, 10, 30)),
        ("iPhone 17e", 256, 40, date(2026, 11, 2)),
    ]
    report = compare(seen, {("iPhone 13", 128), ("iPhone 13", 256)})
    assert (report.models_seen, report.pairs_seen) == (2, 3)
    assert [(u.model, u.storage_gb) for u in report.unknown_models] == [("iPhone 17e", 256)]
    assert [(u.model, u.storage_gb) for u in report.unknown_sizes] == [("iPhone 13", 64)]
    lines = report_lines(report)
    assert lines[0].endswith("1 unknown models, 1 unknown sizes in ref.devices")
    assert lines[1] == "unknown model: iPhone 17e 256 GB (40 offer rows, last seen 02.11.2026)"
    assert [line.split("::")[1] for line in annotations(report)] == [
        "notice title=ref.devices",
        "error title=Unknown model",
        "warning title=Unknown size",
    ]
    assert compare(seen[:1], {("iPhone 13", 128)}).unknown_models == []
