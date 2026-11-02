"""``BrowserPlaybackAdapter`` on real Playwright MCP snapshots (``tests/fixtures/snapshots``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.browser.playback import BrowserPlaybackAdapter
from src.config import ReplaySettings
from src.contracts import Binding
from src.harness.playback import TextPlaybackAdapter

SNAPSHOTS = Path(__file__).resolve().parent / "fixtures" / "snapshots"


def snapshot(name: str) -> str:
    return (SNAPSHOTS / f"{name}.txt").read_text(encoding="utf-8")


@pytest.fixture
def adapter() -> BrowserPlaybackAdapter:
    return BrowserPlaybackAdapter(ReplaySettings())


# -- describe -----------------------------------------------------------------------------


def test_a_reference_value_is_described_by_role_name_and_position(adapter: BrowserPlaybackAdapter) -> None:
    (binding,) = adapter.describe({"element": "Submit order", "target": "f3e44"}, [snapshot("httpbin_form")])

    assert binding.path == ("target",)
    assert binding.value == "f3e44"
    assert binding.descriptor["role"] == "button"
    assert binding.descriptor["name"] == "Submit order"
    assert (binding.descriptor["nth"], binding.descriptor["count"]) == (1, 1)
    assert binding.descriptor["frame"] == "f3"


def test_values_that_are_not_references_are_left_alone(adapter: BrowserPlaybackAdapter) -> None:
    args = {"url": "https://books.toscrape.com/", "text": "e44", "element": "Submit order"}

    assert adapter.describe(args, [snapshot("httpbin_form")]) == []


def test_references_are_found_by_value_at_any_depth(adapter: BrowserPlaybackAdapter) -> None:
    args = {"fields": [{"name": "Customer", "target": "f3e5"}, {"name": "Phone", "target": "f3e8"}]}

    bindings = adapter.describe(args, [snapshot("httpbin_form")])

    assert [binding.path for binding in bindings] == [("fields", 0, "target"), ("fields", 1, "target")]
    assert [binding.descriptor["name"] for binding in bindings] == ["Customer name:", "Telephone:"]


def test_the_context_keeps_the_nearest_named_ancestors(adapter: BrowserPlaybackAdapter) -> None:
    (binding,) = adapter.describe({"target": "f3e30"}, [snapshot("httpbin_form")])

    assert binding.descriptor["role"] == "checkbox"
    # Anonymous generic wrappers are skipped; the named group and the paragraph remain.
    assert binding.descriptor["context"] == ['group "Pizza Toppings"', "paragraph"]


def test_inline_text_stands_in_for_a_missing_quoted_name(adapter: BrowserPlaybackAdapter) -> None:
    (binding,) = adapter.describe({"target": "f4e29"}, [snapshot("books_product")])

    assert (binding.descriptor["role"], binding.descriptor["name"]) == ("paragraph", "£51.77")


def test_equal_lines_get_their_position(adapter: BrowserPlaybackAdapter) -> None:
    (binding,) = adapter.describe({"target": "f3e32"}, [snapshot("quotes_js")])

    assert binding.descriptor["name"] == "by Albert Einstein"
    assert (binding.descriptor["nth"], binding.descriptor["count"]) == (3, 3)


# -- rebind -------------------------------------------------------------------------------


def recorded(adapter: BrowserPlaybackAdapter, args: dict, page: str) -> list[Binding]:
    return adapter.describe(args, [snapshot(page)])


def test_a_reference_is_rebound_after_a_reload(adapter: BrowserPlaybackAdapter) -> None:
    args = {"element": "Books breadcrumb", "target": "f4e13"}
    bindings = recorded(adapter, args, "books_product")

    result = adapter.rebind(args, bindings, [snapshot("books_product_reloaded")])

    assert result.args == {"element": "Books breadcrumb", "target": "f9e13"}
    assert args["target"] == "f4e13"  # the recorded arguments are never mutated


def test_a_changed_number_in_the_name_still_rebinds(adapter: BrowserPlaybackAdapter) -> None:
    args = {"target": "f4e29"}  # the price paragraph: £51.77 -> £47.20
    bindings = recorded(adapter, args, "books_product")

    assert adapter.rebind(args, bindings, [snapshot("books_product_reloaded")]).args == {"target": "f9e29"}


def test_a_missing_element_is_a_divergence_before_the_call(adapter: BrowserPlaybackAdapter) -> None:
    args = {"target": "f3e44"}
    bindings = recorded(adapter, args, "httpbin_form")

    result = adapter.rebind(args, bindings, [snapshot("books_product")])

    assert result.args is None
    assert 'no button "Submit order"' in result.detail


def test_equal_lines_rebind_by_position_while_their_count_holds(adapter: BrowserPlaybackAdapter) -> None:
    page = snapshot("quotes_js")
    args = {"target": "f3e20"}  # the second "by Albert Einstein"
    bindings = recorded(adapter, args, "quotes_js")

    reloaded = page.replace("[ref=f3e", "[ref=f5e")
    assert adapter.rebind(args, bindings, [reloaded]).args == {"target": "f5e20"}


def test_equal_lines_whose_count_changed_are_ambiguous(adapter: BrowserPlaybackAdapter) -> None:
    page = snapshot("quotes_js")
    args = {"target": "f3e20"}
    bindings = recorded(adapter, args, "quotes_js")

    # One of the three quotes is gone: positions no longer say which one is meant.
    fewer = page.replace("by Albert Einstein [ref=f3e12]", "by Someone Else [ref=f3e12]")
    fewer = fewer.replace("- generic [ref=f3e12]: by Albert Einstein", "- generic [ref=f3e12]: by Someone Else")
    result = adapter.rebind(args, bindings, [fewer])

    assert result.args is None
    assert "2 of 3 recorded" in result.detail


def test_verify_accepts_only_live_references(adapter: BrowserPlaybackAdapter) -> None:
    bindings = [Binding(path=("target",), value="f3e44")]
    page = [snapshot("httpbin_form")]

    assert adapter.rebind({"target": "f3e39"}, bindings, page, verify=True).args == {"target": "f3e39"}
    invented = adapter.rebind({"target": "f3e999"}, bindings, page, verify=True)
    assert invented.args is None and "not a reference" in invented.detail


def test_the_first_text_wins_for_a_reference_listed_twice(adapter: BrowserPlaybackAdapter) -> None:
    form = snapshot("httpbin_form")
    args = {"target": "f3e44"}
    bindings = recorded(adapter, args, "httpbin_form")

    assert adapter.rebind(args, bindings, [form, form]).args == {"target": "f3e44"}


# -- checkpoint and similarity -------------------------------------------------------------


def test_a_reload_with_new_numbers_keeps_the_checkpoint_similar(adapter: BrowserPlaybackAdapter) -> None:
    before = adapter.checkpoint(snapshot("books_product"))
    after = adapter.checkpoint(snapshot("books_product_reloaded"))

    assert before["url"] == "books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
    assert adapter.similarity(before, after) == pytest.approx(1.0)


def test_another_path_is_another_page(adapter: BrowserPlaybackAdapter) -> None:
    product = adapter.checkpoint(snapshot("books_product"))
    catalog = adapter.checkpoint(snapshot("books_catalog"))

    assert adapter.similarity(product, catalog) == 0.0
    host_only = BrowserPlaybackAdapter(ReplaySettings(checkpoint_url="host"))
    assert 0.0 < host_only.similarity(product, catalog) < 0.6


def test_a_checkpoint_keeps_no_page_text(adapter: BrowserPlaybackAdapter) -> None:
    fingerprint = adapter.checkpoint(snapshot("books_product"))

    assert "Attic" not in repr(fingerprint["structure"])


def test_without_an_url_check_only_the_structure_counts() -> None:
    adapter = BrowserPlaybackAdapter(ReplaySettings(checkpoint_url="off"))
    form = adapter.checkpoint(snapshot("httpbin_form"))

    assert adapter.similarity(form, adapter.checkpoint(snapshot("httpbin_form"))) == 1.0
    assert adapter.similarity(form, adapter.checkpoint(snapshot("quotes_js"))) < 0.2


# -- region -------------------------------------------------------------------------------


def test_the_region_drops_references_and_ignored_text(adapter: BrowserPlaybackAdapter) -> None:
    region = adapter.region(
        snapshot("books_product"),
        r'(?s)paragraph \[ref=f4e29\].*?Tax',
        [r"\(\d+ available\)"],
    )

    assert "[ref=" not in region
    assert region.startswith("paragraph: £51.77")
    assert "Tax" in region


def test_an_unmatched_select_is_an_empty_region(adapter: BrowserPlaybackAdapter) -> None:
    assert adapter.region(snapshot("books_product"), "never-there", []) == ""


# -- the plain-text adapter -----------------------------------------------------------------


def test_the_text_adapter_replays_arguments_literally() -> None:
    adapter = TextPlaybackAdapter(ReplaySettings())

    assert adapter.describe({"q": "x"}, ["anything [ref=x]"]) == []
    assert adapter.rebind({"q": "x"}, [], []).args == {"q": "x"}
    assert adapter.rebind({"q": "x"}, [Binding(path=("q",), value="x")], []).args is None


def test_the_text_adapter_fingerprint_is_number_blind() -> None:
    adapter = TextPlaybackAdapter(ReplaySettings())
    before = adapter.checkpoint("Order 1043 shipped to the warehouse today")
    after = adapter.checkpoint("Order 2210 shipped to the warehouse today")
    other = adapter.checkpoint("Payment failed: card declined by the bank")

    assert adapter.similarity(before, after) == 1.0
    assert adapter.similarity(before, other) == 0.0
    assert adapter.region("  a   b \n\n c ", None, []) == "a b\nc"
