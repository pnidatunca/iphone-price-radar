import base64
import json

import pytest
from helpers import FIXTURES

from price_radar.ingest.rsc import Flight, FlightError, find_dicts, flight_bytes, parse_rows


def push(text: str) -> str:
    """One inline script the way Next.js writes it: a JSON string literal with "<" escaped."""
    return f"<script>self.__next_f.push([1,{json.dumps(text).replace('<', '\\u003c')}])</script>"


def page(*pieces: str, bootstrap: bool = True) -> str:
    scripts = "".join(push(piece) for piece in pieces)
    head = "<script>(self.__next_f=self.__next_f||[]).push([0])</script>" if bootstrap else ""
    return f"<html><body>{head}{scripts}</body></html>"


def text_row(row_id: str, text: str) -> str:
    return f"{row_id}:T{len(text.encode('utf-8')):x},{text}"


def test_pushes_are_joined_in_order_even_when_a_row_is_split():
    html = page('0:{"a":', '1}\n1:["x"', ',"y"]\n')
    flight = Flight.from_html(html)
    assert [(row.id, row.value) for row in flight.rows] == [("0", {"a": 1}), ("1", ["x", "y"])]


def test_escaped_string_literals_are_decoded():
    value = {"html": '<b>"quoted"</b>', "path": "C:\\dir", "lines": "a\nb", "tr": "Yıldız Işığı"}
    flight = Flight.from_html(page(f"5:{json.dumps(value, ensure_ascii=False)}\n"))
    assert flight.row("5").value == value


def test_text_rows_are_cut_by_utf8_byte_length():
    # The JSON-LD of both sites lives in text rows; Turkish letters are two bytes, and the text may hold newlines.
    text = '{"name":"Yenilenmiş iPhone","note":"satır\nsatır"}'
    html = page(text_row("23", text) + '24:{"after":true}\n')
    flight = Flight.from_html(html)
    assert flight.row("23").tag == "T"
    assert flight.row("23").value == text
    assert flight.row("24").value == {"after": True}
    assert flight.texts() == [text]


def test_tagged_rows_hint_rows_without_id_and_stream_markers():
    html = page(':HL["/_next/static/css/a.css","style"]\n3:I[68172,[],""]\na:X\n0:{"ok":1}\na:C\n4:E{"digest":"1"}\n')
    rows = Flight.from_html(html).rows
    assert [(row.id, row.tag) for row in rows] == [
        ("", "HL"),
        ("3", "I"),
        ("a", "X"),
        ("0", ""),
        ("a", "C"),
        ("4", "E"),
    ]
    assert rows[0].value == ["/_next/static/css/a.css", "style"]
    assert rows[2].value is None


def test_bootstrap_and_form_state_pushes_carry_no_data_and_binary_pushes_do():
    binary = base64.b64encode(b'7:{"from":"binary"}\n').decode()
    html = (
        "<script>(self.__next_f=self.__next_f||[]).push([0])</script>"
        "<script>self.__next_f.push([2,null])</script>"
        + push('6:{"from":"text"}\n')
        + f'<script>self.__next_f.push([3,"{binary}"])</script>'
    )
    flight = Flight.from_html(html)
    assert flight.row("6").value == {"from": "text"}
    assert flight.row("7").value == {"from": "binary"}


def test_reference_paths_walk_through_react_element_props():
    rows = (
        '4d:["$","$L10",null,{"children":[["$","div",null,{"priceInfo":{"main":24990}}]]}]\n'
        '53:["$","$L11",null,{"view":{"priceInfo":"$4d:props:children:0:props:priceInfo"}}]\n'
    )
    flight = Flight.from_html(page(rows))
    view = next(flight.find_dicts("view"))["view"]
    assert view["priceInfo"] == {"main": 24990}
    assert flight.resolve("$4d:props:children:0:type") == "div"


def test_list_items_that_are_references_are_resolved_recursively():
    rows = (
        '44:["$","div",null,{"seller":{"vendorId":7002}}]\n'
        '45:{"sellers":[{"vendorId":7004}]}\n'
        '46:{"sellers":["$44:props:seller","$45:sellers:0"]}\n'
        '47:{"data":{"sellers":"$46:sellers"}}\n'
    )
    flight = Flight.from_html(page(rows))
    data = flight.first_value("data")
    assert [seller["vendorId"] for seller in data["sellers"]] == [7002, 7004]


def test_lazy_and_promise_references_name_rows_and_module_references_stay():
    rows = '3:I[1,[],"default"]\n5:{"n":1}\n6:{"lazy":"$L5","promise":"$@5","component":"$L3"}\n'
    resolved = Flight.from_html(page(rows)).resolve_deep({"x": "$6"})
    assert resolved == {"x": {"lazy": {"n": 1}, "promise": {"n": 1}, "component": "$L3"}}


def test_cycles_are_left_as_references():
    rows = '1:{"name":"a","next":"$2"}\n2:{"name":"b","next":"$1"}\n3:{"self":"$3:self"}\n4:{"a":{"up":"$4"}}\n'
    flight = Flight.from_html(page(rows))
    assert flight.resolve_deep("$1") == {"name": "a", "next": {"name": "b", "next": "$1"}}
    assert flight.resolve("$3:self") == "$3:self"
    assert flight.resolve_deep("$4") == {"a": {"up": "$4"}}


def test_references_to_other_parts_of_the_same_row_are_not_cycles():
    # A row may hold a seller list and the sellers it points to; resolving one must not stop at the other.
    rows = (
        '49:["$","div",null,{"alt":{"sellers":["$49:props:seller"]},"seller":{"vendorId":7002}}]\n'
        '50:{"view":{"alt":"$49:props:alt"}}\n'
    )
    flight = Flight.from_html(page(rows))
    assert flight.resolve_deep("$50") == {"view": {"alt": {"sellers": [{"vendorId": 7002}]}}}


def test_special_values_are_decoded_and_missing_targets_stay_strings():
    rows = '1:{"gone":"$undefined","dollar":"$$5","date":"$D2026-10-12T17:00:00Z","missing":"$99","bad":"$1:nope"}\n'
    value = Flight.from_html(page(rows)).resolve_deep("$1")
    assert value == {"gone": None, "dollar": "$5", "date": "2026-10-12T17:00:00Z", "missing": "$99", "bad": "$1:nope"}


def test_find_dicts_and_first_value_search_every_row():
    rows = '1:{"a":{"priceInfo":"$2:priceInfo"}}\n2:{"priceInfo":{"main":10}}\n3:{"priceInfo":"$undefined"}\n'
    flight = Flight.from_html(page(rows))
    assert [d["priceInfo"] for d in flight.find_dicts("priceInfo")] == [{"main": 10}, {"main": 10}, None]
    assert flight.first_value("priceInfo") == {"main": 10}
    assert flight.first_value("priceInfo", kind=str) is None
    assert list(find_dicts({"x": [{"k": 1}, {"y": {"k": 2}}]}, "k")) == [{"k": 1}, {"k": 2}]


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>no flight data</body></html>",
        page("0:{not json}\n"),
        page("0:T10,short"),
        page("zz:{}\n"),
        "<script>self.__next_f.push([1,'single quotes'])</script>",
    ],
)
def test_malformed_flight_data_raises(html):
    with pytest.raises(FlightError):
        Flight.from_html(html)


def test_rows_parse_from_raw_bytes():
    rows = parse_rows(b'0:{"a":1}\n\n' + text_row("1", "ç").encode() + b"2:null\n")
    assert [(row.id, row.tag, row.value) for row in rows] == [("0", "", {"a": 1}), ("1", "T", "ç"), ("2", "", None)]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*/*.html")), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_fixture_page_decodes(path):
    html = path.read_text(encoding="utf-8")
    assert flight_bytes(html)
    assert Flight.from_html(html).rows
