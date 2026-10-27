#!/usr/bin/env python3
"""Generate the scripted eval scenarios in tests/evals/scenarios and predict their metrics.

The scenarios below are the source of truth for their YAML files: change a scenario here, run

    uv run scripts/generate_eval_scenarios.py

and commit the regenerated YAML (tests/test_generate_eval_scenarios.py fails when they drift).
The engine itself decides the real metrics: refresh the baseline afterwards with
``scripts/run_evals.py --write-baseline``. ``simulate`` is only a quick model of the fake
model and browser that flags a scenario whose script cannot work (``PROBLEMS`` in the
table); ``--predicted PATH`` writes its predictions as JSON.

Every scenario is ``strict``: a model call past the scripted responses fails it instead of
replaying an earlier response.
"""
import argparse
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "evals" / "scenarios"
UNCERTAIN = {}  # name -> why metrics/status need verification against the engine


class Flow(dict):
    pass


def _flow_rep(d, data):
    return d.represent_mapping("tag:yaml.org,2002:map", data.items(), flow_style=True)


def _str_rep(d, data):
    if "\n" in data:
        return d.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return d.represent_scalar("tag:yaml.org,2002:str", data)


class _Dumper(yaml.SafeDumper):
    """Scenario YAML style, without touching the global PyYAML representers."""


_Dumper.add_representer(Flow, _flow_rep)
_Dumper.add_representer(str, _str_rep)


# ---------- builders ----------
def plan(*descs):
    return {"steps": [Flow(id=i + 1, description=d, status="pending") for i, d in enumerate(descs)]}


def call(name, reason, outcome="ok", **args):
    r = {"decision": "tool_call", "tool_request": Flow(name=name, args=Flow(args), reason=reason)}
    r["_outcome"] = outcome  # simulator-only annotation, stripped before dump
    return r


nav = lambda url, reason="Open the page.", **kw: call("browser_navigate", reason, url=url, **kw)
snapc = lambda reason="Inspect the page.", **kw: call("browser_snapshot", reason, **kw)
click = lambda ref, reason, outcome="ok": call("browser_click", reason, outcome, ref=ref)
type_ = lambda ref, text, reason, outcome="ok": call("browser_type", reason, outcome, ref=ref, text=text)
hover = lambda ref, reason, outcome="ok": call("browser_hover", reason, outcome, ref=ref)
done = lambda ans: {"decision": "done", "final_answer": ans}
stop = lambda status, msg: {"decision": "stop", "status": status, "message": msg}


def page(url, *lines):
    return "\n".join([f"- Page URL: {url}"] + [f"- {l}" for l in lines])


BLANK = "- Page URL: about:blank"
ALLOW_REST = {"id": "allow_rest", "decision": "allow", "tool": ".*"}

SCENARIOS = []


def add(name, task, responses, snapshots, status, contains=None, max_tools=None, max_rep=0,
        max_blocks=0, permissions=None, human=None, turn_cap=None, uncertain=None, max_errors=None):
    s = {"name": name, "task": task, "strict": True}
    if turn_cap:
        s["turn_cap"] = turn_cap
    if permissions:
        s["permissions"] = permissions
    if human is not None:
        s["human"] = {"answers": human}
    s["model"] = {"responses": responses}
    s["browser"] = {"snapshots": snapshots}
    a = {"expected_terminal_status": status}
    if contains:
        a["final_answer_contains"] = contains
    a["max_tool_calls"] = max_tools
    a["max_repeated_actions"] = max_rep
    a["max_policy_blocks"] = max_blocks
    if max_errors is not None:
        a["max_errors"] = max_errors
    s["assertions"] = a
    if uncertain:
        UNCERTAIN[name] = uncertain
    SCENARIOS.append(s)


# =====================================================================
# 1. Happy path
# =====================================================================
NEWS = page("https://news.example/subscribe", 'heading "Newsletter" [ref=e2]', 'textbox "Email" [ref=e3]', 'button "Subscribe" [ref=e4]')
add("form_fill_submit",
    "Subscribe test@example.com to the newsletter on https://news.example/subscribe.",
    [plan("Open the subscription page", "Fill in the email and submit", "Confirm the subscription"),
     nav("https://news.example/subscribe", "Open the subscription page."),
     snapc("Find the email field."),
     type_("e3", "test@example.com", "Enter the email."),
     click("e4", "Submit the form."),
     snapc("Check the result."),
     done("Subscribed test@example.com; the page confirms the subscription.")],
    [BLANK, NEWS,
     page("https://news.example/subscribe", 'textbox "Email" [ref=e3]: test@example.com', 'button "Subscribe" [ref=e4]'),
     page("https://news.example/subscribe/done", 'heading "You are subscribed" [ref=e7]')],
    "completed", ["Subscribed"], 5)

add("no_tool_unit_conversion",
    "How many meters are in 5.2 kilometers? No browsing needed.",
    [plan("Convert kilometers to meters"),
     done("5.2 kilometers is 5200 meters.")],
    [BLANK], "completed", ["5200"], 0)

add("navigate_read_heading",
    "Open https://docs.example/changelog and tell me the latest version number.",
    [plan("Open the changelog", "Read the latest version"),
     nav("https://docs.example/changelog", "Open the changelog."),
     snapc("Read the version headings."),
     done("The latest version is 3.4.1.")],
    [BLANK, page("https://docs.example/changelog", 'heading "Changelog" [ref=e1]', 'heading "3.4.1 (2026-10-01)" [ref=e2]',
                 'heading "3.4.0 (2026-09-12)" [ref=e3]')],
    "completed", ["3.4.1"], 2)

add("already_on_page_snapshot_only",
    "What is the price of the item on the current page?",
    [plan("Read the current page"),
     snapc("Read the price on the current page."),
     done("The item costs $24.99.")],
    [page("https://shop.example/item/42", 'heading "Ceramic Mug" [ref=e1]', 'text "$24.99" [ref=e2]', 'button "Add to cart" [ref=e3]')],
    "completed", ["$24.99"], 1)

add("hover_reveal_menu_then_click",
    "On https://corp.example open the Careers page from the Company menu.",
    [plan("Open the site", "Open the Company menu", "Go to Careers"),
     nav("https://corp.example", "Open the site."),
     hover("e3", "Reveal the Company menu."),
     click("e6", "Open Careers."),
     done("Opened the Careers page; it lists 4 open positions.")],
    [BLANK,
     page("https://corp.example/", 'link "Home" [ref=e2]', 'button "Company" [ref=e3]', 'link "Contact" [ref=e4]'),
     page("https://corp.example/", 'button "Company" [ref=e3]', 'menuitem "About" [ref=e5]', 'menuitem "Careers" [ref=e6]'),
     page("https://corp.example/careers", 'heading "Careers" [ref=e8]', 'text "4 open positions" [ref=e9]')],
    "completed", ["Careers"], 3)

add("multi_page_link_chain",
    "Starting from https://wiki.example, go to Rivers and then to the Volga article and report its length.",
    [plan("Open the wiki", "Go to Rivers", "Open Volga", "Report the length"),
     nav("https://wiki.example", "Open the wiki."),
     click("e3", "Open the Rivers category."),
     click("e12", "Open the Volga article."),
     snapc("Find the length in the article."),
     done("The Volga is 3530 km long.")],
    [BLANK,
     page("https://wiki.example/", 'link "Mountains" [ref=e2]', 'link "Rivers" [ref=e3]', 'link "Lakes" [ref=e4]'),
     page("https://wiki.example/rivers", 'heading "Rivers" [ref=e10]', 'link "Danube" [ref=e11]', 'link "Volga" [ref=e12]'),
     page("https://wiki.example/rivers/volga", 'heading "Volga" [ref=e20]', 'text "Length: 3530 km" [ref=e21]')],
    "completed", ["3530"], 4)

add("search_and_read_results",
    "Search https://books.example for 'distributed systems' and name the first result.",
    [plan("Open the site", "Search for the query", "Read the first result"),
     nav("https://books.example", "Open the site."),
     type_("e2", "distributed systems", "Enter the query."),
     click("e3", "Run the search."),
     snapc("Read the results."),
     done("The first result is 'Designing Data-Intensive Applications'.")],
    [BLANK,
     page("https://books.example/", 'searchbox "Search books" [ref=e2]', 'button "Search" [ref=e3]'),
     page("https://books.example/", 'searchbox "Search books" [ref=e2]: distributed systems', 'button "Search" [ref=e3]'),
     page("https://books.example/search?q=distributed+systems", 'heading "Results" [ref=e5]',
          'link "Designing Data-Intensive Applications" [ref=e6]', 'link "Distributed Systems, 4th ed." [ref=e7]')],
    "completed", ["Designing Data-Intensive Applications"], 4)

add("contact_form_two_fields",
    "Send the message 'Is the store open on Sunday?' as Anna via the contact form at https://bakery.example/contact.",
    [plan("Open the contact form", "Fill name and message", "Send"),
     nav("https://bakery.example/contact", "Open the contact form."),
     type_("e2", "Anna", "Enter the name."),
     type_("e3", "Is the store open on Sunday?", "Enter the message."),
     click("e4", "Send the message."),
     done("Message sent; the page says 'Thanks, we will reply soon'.")],
    [BLANK,
     page("https://bakery.example/contact", 'textbox "Name" [ref=e2]', 'textbox "Message" [ref=e3]', 'button "Send" [ref=e4]'),
     page("https://bakery.example/contact", 'textbox "Name" [ref=e2]: Anna', 'textbox "Message" [ref=e3]', 'button "Send" [ref=e4]'),
     page("https://bakery.example/contact", 'textbox "Name" [ref=e2]: Anna', 'textbox "Message" [ref=e3]: Is the store open on Sunday?', 'button "Send" [ref=e4]'),
     page("https://bakery.example/contact/sent", 'heading "Thanks, we will reply soon" [ref=e6]')],
    "completed", ["Message sent"], 4)

add("snapshot_with_depth_arg",
    "List the top-level sections of https://portal.example.",
    [plan("Open the portal", "List the sections"),
     nav("https://portal.example", "Open the portal."),
     snapc("Read only the top-level structure.", depth=2),
     done("Sections: News, Projects, Team.")],
    [BLANK, page("https://portal.example/", 'navigation "Main" [ref=e1]', 'link "News" [ref=e2]', 'link "Projects" [ref=e3]', 'link "Team" [ref=e4]')],
    "completed", ["News", "Projects", "Team"], 2)

add("extra_args_dropped",
    "Open https://status.example and report whether all systems are operational.",
    [plan("Open the status page", "Report the status"),
     nav("https://status.example", "Open the status page.", wait_until="networkidle", timeout_ms=5000),
     done("All systems are operational.")],
    [BLANK, page("https://status.example/", 'heading "All systems operational" [ref=e1]')],
    "completed", ["operational"], 1, max_errors=0)

# =====================================================================
# 2. Error recovery
# =====================================================================
add("stale_ref_after_navigation",
    "On https://forum.example open the 'Announcements' board and then the pinned thread.",
    [plan("Open the forum", "Open Announcements", "Open the pinned thread"),
     nav("https://forum.example", "Open the forum."),
     click("e3", "Open Announcements."),
     click("e3", "Open the pinned thread (using the ref from the previous page)."),
     snapc("The ref was stale; refresh the page structure."),
     click("e14", "Open the pinned thread."),
     done("Opened the pinned thread 'Forum rules (read first)'.")],
    [BLANK,
     page("https://forum.example/", 'link "General" [ref=e2]', 'link "Announcements" [ref=e3]'),
     page("https://forum.example/announcements", 'heading "Announcements" [ref=e13]', 'link "Forum rules (read first)" [ref=e14]'),
     page("https://forum.example/announcements/1", 'heading "Forum rules (read first)" [ref=e20]')],
    "completed", ["Forum rules"], 5, max_rep=1)

add("ref_from_next_snapshot",
    "Accept the cookie banner on https://media.example and open the Videos section.",
    [plan("Open the site", "Accept cookies", "Open Videos"),
     nav("https://media.example", "Open the site."),
     click("e9", "Open Videos."),
     snapc("Ref not found; re-read the page."),
     click("e2", "Accept cookies first."),
     click("e9", "Open Videos."),
     done("Accepted cookies and opened the Videos section.")],
    [BLANK,
     page("https://media.example/", 'dialog "Cookies" [ref=e1]', 'button "Accept" [ref=e2]', 'button "Reject" [ref=e3]'),
     page("https://media.example/", 'link "News" [ref=e8]', 'link "Videos" [ref=e9]'),
     page("https://media.example/videos", 'heading "Videos" [ref=e15]')],
    "completed", ["Videos"], 5)

add("type_into_missing_ref",
    "Log the weight 72.5 in the tracker at https://health.example/log.",
    [plan("Open the tracker", "Enter the weight", "Save"),
     nav("https://health.example/log", "Open the tracker."),
     type_("e5", "72.5", "Enter the weight."),
     snapc("Field ref not found; re-read the form."),
     type_("e3", "72.5", "Enter the weight in the correct field."),
     click("e4", "Save the entry."),
     done("Saved the entry 72.5 kg.")],
    [BLANK,
     page("https://health.example/log", 'heading "Log entry" [ref=e2]', 'spinbutton "Weight, kg" [ref=e3]', 'button "Save" [ref=e4]'),
     page("https://health.example/log", 'spinbutton "Weight, kg" [ref=e3]: "72.5"', 'button "Save" [ref=e4]'),
     page("https://health.example/log/saved", 'text "Saved: 72.5 kg" [ref=e7]')],
    "completed", ["72.5"], 5)

add("hover_missing_ref_recover",
    "Read the tooltip on the 'Fees' icon at https://bank-info.example/pricing.",
    [plan("Open pricing", "Hover the Fees icon", "Read the tooltip"),
     nav("https://bank-info.example/pricing", "Open pricing."),
     hover("e7", "Show the Fees tooltip."),
     snapc("Ref not found; re-read the page."),
     hover("e4", "Show the Fees tooltip."),
     done("The tooltip says transfers within the country are free.")],
    [BLANK,
     page("https://bank-info.example/pricing", 'heading "Pricing" [ref=e2]', 'img "Fees info" [ref=e4]'),
     page("https://bank-info.example/pricing", 'img "Fees info" [ref=e4]', 'tooltip "Domestic transfers are free" [ref=e5]')],
    "completed", ["free"], 4)

add("two_wrong_refs_then_success",
    "Click 'Download PDF' on https://reports.example/q3.",
    [plan("Open the report", "Download the PDF"),
     nav("https://reports.example/q3", "Open the report."),
     click("e10", "Click Download PDF."),
     click("e11", "Try the other download ref."),
     snapc("Both refs failed; re-read the page."),
     click("e4", "Click Download PDF."),
     done("Clicked 'Download PDF'; the page shows 'Download started'.")],
    [BLANK,
     page("https://reports.example/q3", 'heading "Q3 report" [ref=e2]', 'link "Download PDF" [ref=e4]'),
     page("https://reports.example/q3", 'status "Download started" [ref=e6]')],
    "completed", ["Download started"], 5)

add("click_before_navigate_recover",
    "Open https://tickets.example and click 'Today'.",
    [plan("Open the site", "Click Today"),
     click("e2", "Click Today."),
     nav("https://tickets.example", "The current page is blank; open the site first."),
     click("e2", "Click Today."),
     done("Opened today's schedule.")],
    [BLANK,
     page("https://tickets.example/", 'link "Today" [ref=e2]', 'link "Tomorrow" [ref=e3]'),
     page("https://tickets.example/today", 'heading "Schedule for today" [ref=e5]')],
    "completed", ["today"], 3)

add("errors_then_stop_failed",
    "Press the 'Export CSV' button on https://crm.example/contacts.",
    [plan("Open contacts", "Export CSV"),
     nav("https://crm.example/contacts", "Open contacts."),
     click("e8", "Click Export CSV."),
     snapc("Ref not found; re-read the page."),
     stop("failed", "The contacts page has no 'Export CSV' button, so the export cannot be done.")],
    [BLANK,
     page("https://crm.example/contacts", 'heading "Contacts" [ref=e2]', 'button "Add contact" [ref=e3]', 'table "Contacts" [ref=e4]')],
    "blocked", ["Export CSV"], 3)

# =====================================================================
# 3. Repeats without progress
# =====================================================================
add("repeat_click_no_page_change",
    "Load more comments on https://blog.example/post/7 until all are shown.",
    [plan("Open the post", "Load all comments"),
     nav("https://blog.example/post/7", "Open the post."),
     click("e5", "Load more comments."),
     click("e5", "Load more comments."),
     click("e5", "Load more comments.", "guard"),
     plan("Report the comments shown"),
     done("The 'Load more' button no longer changes the page; 3 comments are shown.")],
    [BLANK,
     page("https://blog.example/post/7", 'heading "Post 7" [ref=e2]', 'article "Comment 1" [ref=e3]', 'button "Load more" [ref=e5]'),
     page("https://blog.example/post/7", 'article "Comment 1" [ref=e3]', 'article "Comment 2" [ref=e4]',
          'article "Comment 3" [ref=e6]', 'button "Load more" [ref=e5]')],
    "completed", ["Load more"], 3, max_rep=1)

add("repeat_snapshot_loop",
    "Wait until the order status on https://shop.example/orders/55 changes from Processing.",
    [plan("Open the order", "Watch the status"),
     nav("https://shop.example/orders/55", "Open the order."),
     snapc("Check the status."),
     snapc("Check the status again."),
     call("browser_snapshot", "Check the status again.", "guard"),
     plan("Report the status"),
     done("The status is still Processing; it did not change.")],
    [BLANK, page("https://shop.example/orders/55", 'heading "Order 55" [ref=e1]', 'text "Status: Processing" [ref=e2]')],
    "completed", ["Processing"], 3, max_rep=1)

add("repeat_same_wrong_ref",
    "Open the Settings tab on https://app.example/dashboard.",
    [plan("Open the dashboard", "Open Settings"),
     nav("https://app.example/dashboard", "Open the dashboard."),
     click("e9", "Open Settings."),
     click("e9", "Open Settings."),
     snapc("Re-read the page."),
     click("e4", "Open Settings."),
     done("Opened the Settings tab.")],
    [BLANK,
     page("https://app.example/dashboard", 'tab "Overview" [ref=e3]', 'tab "Settings" [ref=e4]'),
     page("https://app.example/dashboard/settings", 'heading "Settings" [ref=e10]')],
    "completed", ["Settings"], 5, max_rep=1)

add("repeat_type_same_text",
    "Enter the promo code AUTUMN26 at https://shop.example/cart and apply it.",
    [plan("Open the cart", "Enter the code", "Apply it"),
     nav("https://shop.example/cart", "Open the cart."),
     type_("e3", "AUTUMN26", "Enter the code."),
     type_("e3", "AUTUMN26", "Enter the code again."),
     click("e4", "Apply the code."),
     done("Applied AUTUMN26; total is now $45.00.")],
    [BLANK,
     page("https://shop.example/cart", 'textbox "Promo code" [ref=e3]', 'button "Apply" [ref=e4]'),
     page("https://shop.example/cart", 'textbox "Promo code" [ref=e3]: AUTUMN26', 'button "Apply" [ref=e4]'),
     page("https://shop.example/cart", 'textbox "Promo code" [ref=e3]: AUTUMN26', 'button "Apply" [ref=e4]'),
     page("https://shop.example/cart", 'text "Code applied. Total: $45.00" [ref=e6]')],
    "completed", ["AUTUMN26"], 4, max_rep=1)

add("alternating_actions_not_consecutive",
    "Compare the two tabs 'Monthly' and 'Yearly' on https://saas.example/pricing.",
    [plan("Open pricing", "Read both tabs", "Compare"),
     nav("https://saas.example/pricing", "Open pricing."),
     click("e3", "Open Monthly."),
     click("e4", "Open Yearly."),
     click("e3", "Back to Monthly."),
     click("e4", "Back to Yearly."),
     done("Monthly is $10, Yearly is $100 (two months free).")],
    [BLANK,
     page("https://saas.example/pricing", 'tab "Monthly" [ref=e3]', 'tab "Yearly" [ref=e4]', 'text "$10 / month" [ref=e5]'),
     page("https://saas.example/pricing", 'tab "Monthly" [ref=e3]', 'tab "Yearly" [ref=e4]', 'text "$10 / month" [ref=e5]'),
     page("https://saas.example/pricing", 'tab "Monthly" [ref=e3]', 'tab "Yearly" [ref=e4]', 'text "$100 / year" [ref=e5]')],
    "completed", ["$100"], 5, max_rep=0)

# =====================================================================
# 4. Permissions
# =====================================================================
add("read_only_navigation_denied",
    "Open https://shop.example/sale and list the discounted items.",
    [plan("Open the sale page", "List the discounted items"),
     call("browser_navigate", "Open the sale page.", "deny", url="https://shop.example/sale"),
     snapc("Navigation is not allowed; read the current page instead."),
     done("Could not open the sale page: navigation is not allowed in read-only mode. The current page shows no sale items.")],
    [page("https://shop.example/", 'heading "Shop" [ref=e1]', 'link "Sale" [ref=e2]'),
     page("https://shop.example/sale", 'listitem "Jacket A −30%" [ref=e5]')],
    "completed", ["read-only"], 1, max_blocks=1, permissions={"mode": "read_only"})

add("read_only_click_denied_snapshot_ok",
    "Add the mug on the current page to the cart.",
    [plan("Read the page", "Add to cart"),
     snapc("Find the Add to cart button."),
     click("e3", "Add the mug to the cart.", "deny"),
     done("Could not add the mug to the cart: clicking is not allowed in read-only mode.")],
    [page("https://shop.example/item/42", 'heading "Ceramic Mug" [ref=e1]', 'text "$24.99" [ref=e2]', 'button "Add to cart" [ref=e3]'),
     page("https://shop.example/cart", 'text "1 item in cart" [ref=e5]')],
    "completed", ["read-only"], 1, max_blocks=1, permissions={"mode": "read_only"})

add("read_only_type_denied",
    "Leave the review 'Great mug' on the current product page.",
    [plan("Find the review field", "Type the review"),
     type_("e4", "Great mug", "Enter the review.", "deny"),
     done("Could not leave the review: typing is not allowed in read-only mode.")],
    [page("https://shop.example/item/42", 'heading "Ceramic Mug" [ref=e1]', 'textbox "Your review" [ref=e4]', 'button "Post" [ref=e5]'),
     page("https://shop.example/item/42", 'textbox "Your review" [ref=e4]: Great mug')],
    "completed", ["read-only"], 0, max_blocks=1, permissions={"mode": "read_only"})

BUY_DENY = {"mode": "dont_ask", "rules": [
    {"id": "purchases", "decision": "deny", "tool": "browser_click", "target": "(?i)buy|купить",
     "reason": "Purchases are disabled for this agent."}]}
add("deny_rule_buy_button",
    "Buy the cheapest USB cable on https://shop.example/cables.",
    [plan("Open the cables page", "Find the cheapest cable", "Buy it"),
     nav("https://shop.example/cables", "Open the cables page."),
     snapc("Find the cheapest cable."),
     click("e6", "Buy the cheapest cable.", "deny"),
     done("Found the cheapest cable (USB-C 1m, $4.99) but did not buy it: purchases are disabled by policy.")],
    [BLANK,
     page("https://shop.example/cables", 'listitem "USB-C 1m — $4.99" [ref=e5]', 'button "Buy now" [ref=e6]',
          'listitem "USB-C 2m — $7.99" [ref=e7]', 'button "Buy now" [ref=e8]'),
     page("https://shop.example/checkout", 'heading "Checkout" [ref=e10]')],
    "completed", ["did not buy"], 2, max_blocks=1, permissions=BUY_DENY)

add("deny_rule_by_domain",
    "Click 'Confirm transfer' on the current page.",
    [plan("Find the button", "Confirm the transfer"),
     snapc("Find the button."),
     click("e3", "Confirm the transfer.", "deny"),
     done("Did not confirm the transfer: actions on bank.example are blocked by policy.")],
    [page("https://bank.example/transfer/confirm", 'heading "Confirm transfer of €200" [ref=e2]', 'button "Confirm transfer" [ref=e3]'),
     page("https://bank.example/transfer/done", 'heading "Transfer sent" [ref=e5]')],
    "completed", ["blocked by policy"], 1, max_blocks=1,
    permissions={"mode": "dont_ask", "rules": [
        {"id": "no_bank", "decision": "deny", "tool": "browser_(click|type)", "domains": ["bank.example"]}]})

ND_RULES = {"mode": "dont_ask", "rules": [
    {"id": "only_intranet", "decision": "deny", "tool": "browser_click", "not_domains": ["intranet.example"]}]}
add("not_domains_outside_denied",
    "Click 'Sign up' on the current page.",
    [plan("Click Sign up"),
     snapc("Find the button."),
     click("e2", "Click Sign up.", "deny"),
     done("Did not click: clicks are only allowed on intranet.example, and this page is on promo.example.")],
    [page("https://promo.example/landing", 'heading "Join now" [ref=e1]', 'button "Sign up" [ref=e2]'),
     page("https://promo.example/signup", 'heading "Create account" [ref=e4]')],
    "completed", ["intranet.example"], 1, max_blocks=1, permissions=ND_RULES)

add("not_domains_inside_allowed",
    "Open the 'Vacation request' form from the current page.",
    [plan("Open the form"),
     snapc("Find the form link."),
     click("e3", "Open the Vacation request form."),
     done("Opened the Vacation request form.")],
    [page("https://intranet.example/hr", 'heading "HR" [ref=e1]', 'link "Payslips" [ref=e2]', 'link "Vacation request" [ref=e3]'),
     page("https://intranet.example/hr/vacation", 'heading "Vacation request" [ref=e6]')],
    "completed", ["Vacation request"], 2, permissions=ND_RULES)

ASK_BUY = {"mode": "default", "rules": [
    {"id": "purchases", "decision": "ask", "tool": "browser_click", "target": "(?i)buy|place order|купить"}, ALLOW_REST]}
add("ask_once_approved",
    "Buy one pack of AA batteries on https://shop.example/batteries.",
    [plan("Open the page", "Buy the batteries"),
     nav("https://shop.example/batteries", "Open the page."),
     snapc("Find the batteries."),
     click("e4", "Buy one pack.", "ask_yes"),
     done("Bought one pack of AA batteries; order #7781 confirmed.")],
    [BLANK,
     page("https://shop.example/batteries", 'listitem "AA batteries, 8 pack — $6.50" [ref=e3]', 'button "Buy now" [ref=e4]'),
     page("https://shop.example/orders/7781", 'heading "Order #7781 confirmed" [ref=e6]')],
    "completed", ["#7781"], 3, permissions=ASK_BUY, human=["once"])

add("ask_denied_by_human_blocked",
    "Buy the 4K monitor on https://shop.example/monitors/4k.",
    [plan("Open the page", "Buy the monitor"),
     nav("https://shop.example/monitors/4k", "Open the page."),
     snapc("Find the buy button."),
     click("e3", "Buy the monitor.", "ask_no")],
    [BLANK,
     page("https://shop.example/monitors/4k", 'heading "27\\" 4K Monitor — $399" [ref=e2]', 'button "Buy now" [ref=e3]'),
     page("https://shop.example/orders/9001", 'heading "Order confirmed" [ref=e5]')],
    "blocked", None, 2, permissions=ASK_BUY, human=["deny"])

add("ask_session_reused",
    "Buy two different notebooks on https://stationery.example: the A5 and the A4.",
    [plan("Open the shop", "Buy the A5 notebook", "Buy the A4 notebook"),
     nav("https://stationery.example", "Open the shop."),
     snapc("Find the notebooks."),
     click("e3", "Buy the A5 notebook.", "ask_yes"),
     snapc("Find the A4 notebook."),
     click("e5", "Buy the A4 notebook.", "ask_session_skip"),
     done("Bought both notebooks: A5 and A4.")],
    [BLANK,
     page("https://stationery.example/", 'listitem "Notebook A5 — $3" [ref=e2]', 'button "Buy A5" [ref=e3]',
          'listitem "Notebook A4 — $4" [ref=e4]', 'button "Buy A4" [ref=e5]'),
     page("https://stationery.example/", 'status "A5 added to order" [ref=e7]', 'button "Buy A4" [ref=e5]'),
     page("https://stationery.example/", 'status "A4 added to order" [ref=e8]')],
    "completed", ["both notebooks"], 5, permissions=ASK_BUY, human=["session"])

add("ask_once_twice",
    "Buy the A5 and the A4 notebooks on https://stationery.example.",
    [plan("Open the shop", "Buy A5", "Buy A4"),
     nav("https://stationery.example", "Open the shop."),
     snapc("Find the notebooks."),
     click("e3", "Buy the A5 notebook.", "ask_yes"),
     snapc("Find the A4 notebook."),
     click("e5", "Buy the A4 notebook.", "ask_yes"),
     done("Bought both notebooks: A5 and A4.")],
    [BLANK,
     page("https://stationery.example/", 'button "Buy A5" [ref=e3]', 'button "Buy A4" [ref=e5]'),
     page("https://stationery.example/", 'status "A5 added to order" [ref=e7]', 'button "Buy A4" [ref=e5]'),
     page("https://stationery.example/", 'status "A4 added to order" [ref=e8]')],
    "completed", ["both notebooks"], 5, permissions=ASK_BUY, human=["once", "once"])

add("ask_answers_exhausted_default_deny",
    "Buy the A5 and the A4 notebooks on https://stationery.example.",
    [plan("Open the shop", "Buy A5", "Buy A4"),
     nav("https://stationery.example", "Open the shop."),
     snapc("Find the notebooks."),
     click("e3", "Buy the A5 notebook.", "ask_yes"),
     snapc("Find the A4 notebook."),
     click("e5", "Buy the A4 notebook.", "ask_no")],
    [BLANK,
     page("https://stationery.example/", 'button "Buy A5" [ref=e3]', 'button "Buy A4" [ref=e5]'),
     page("https://stationery.example/", 'status "A5 added to order" [ref=e7]', 'button "Buy A4" [ref=e5]'),
     page("https://stationery.example/", 'status "A4 added to order" [ref=e8]')],
    "blocked", None, 4, permissions=ASK_BUY, human=["once"])

add("deny_rule_then_alternative_path",
    "I want to stop using https://social.example. Prefer deactivating over deleting if deletion is not possible.",
    [plan("Open account settings", "Delete or deactivate the account"),
     nav("https://social.example/settings/account", "Open account settings."),
     snapc("Find the account controls."),
     click("e3", "Delete the account.", "deny"),
     click("e4", "Deletion is blocked by policy; deactivate instead."),
     done("Account deletion is blocked by policy, so I deactivated the account instead.")],
    [BLANK,
     page("https://social.example/settings/account", 'heading "Account" [ref=e2]', 'button "Delete account" [ref=e3]', 'button "Deactivate account" [ref=e4]'),
     page("https://social.example/settings/account/deactivated", 'heading "Your account is deactivated" [ref=e6]')],
    "completed", ["deactivated"], 3, max_blocks=1,
    permissions={"mode": "dont_ask", "rules": [
        {"id": "no_delete", "decision": "deny", "tool": "browser_click", "target": "(?i)delete",
         "reason": "Irreversible deletions are not allowed."}]})

add("bypass_mode_skips_ask",
    "Place the order in the current cart.",
    [plan("Place the order"),
     snapc("Find the order button."),
     click("e3", "Place the order."),
     done("Order placed; confirmation number A-1029.")],
    [page("https://shop.example/cart", 'text "Total: $18.00" [ref=e2]', 'button "Place order" [ref=e3]'),
     page("https://shop.example/orders/A-1029", 'heading "Order A-1029 placed" [ref=e5]')],
    "completed", ["A-1029"], 2,
    permissions={"mode": "bypass", "rules": [
        {"id": "purchases", "decision": "ask", "tool": "browser_click", "target": "(?i)buy|place order"}]},
    human=[])

# An ask rule whose target does not match the click must not ask: no answer is scripted, so
# an ask would be denied and block the task.
add("default_mode_unmatched_ask_rule_no_ask",
    "Open the 'Pricing' page from https://saas.example.",
    [plan("Open the site", "Open Pricing"),
     nav("https://saas.example", "Open the site."),
     snapc("Find the Pricing link."),
     click("e3", "Open Pricing."),
     done("Opened the Pricing page.")],
    [BLANK,
     page("https://saas.example/", 'link "Features" [ref=e2]', 'link "Pricing" [ref=e3]'),
     page("https://saas.example/pricing", 'heading "Pricing" [ref=e5]')],
    "completed", ["Pricing"], 3,
    permissions={"mode": "default", "rules": [
        {"id": "purchases", "decision": "ask", "tool": "browser_click", "target": "(?i)buy|checkout"},
        ALLOW_REST]}, human=[])

# =====================================================================
# 5. Explicit model stops
# =====================================================================
add("login_wall_stop_blocked",
    "Download the March invoice from https://billing.example/invoices.",
    [plan("Open the invoices page", "Download the March invoice"),
     nav("https://billing.example/invoices", "Open the invoices page."),
     snapc("Inspect the page."),
     stop("blocked", "The invoices page requires a login and no credentials were provided.")],
    [BLANK,
     page("https://billing.example/login", 'heading "Sign in" [ref=e1]', 'textbox "Email" [ref=e2]',
          'textbox "Password" [ref=e3]', 'button "Sign in" [ref=e4]')],
    "blocked", ["requires a login"], 2)

add("captcha_stop_blocked",
    "Get the tracking status of parcel RX123 on https://post.example/track.",
    [plan("Open tracking", "Enter the number", "Read the status"),
     nav("https://post.example/track", "Open tracking."),
     snapc("Inspect the page."),
     stop("blocked", "The tracking page shows a CAPTCHA that I must not solve; please complete it manually.")],
    [BLANK,
     page("https://post.example/track", 'heading "Are you a robot?" [ref=e1]', 'checkbox "I am not a robot" [ref=e2]', 'button "Continue" [ref=e3]')],
    "blocked", ["CAPTCHA"], 2)

add("server_error_stop_failed",
    "Check today's exchange rate on https://rates.example.",
    [plan("Open the rates page", "Read the rate"),
     nav("https://rates.example", "Open the rates page."),
     stop("failed", "The site returns 500 Internal Server Error, so the rate is unavailable.")],
    [BLANK, page("https://rates.example/", 'heading "500 Internal Server Error" [ref=e1]')],
    "blocked", ["500"], 1)

add("not_found_stop_failed",
    "Find the product 'Zeta-9 drone' on https://gadgets.example and report its price.",
    [plan("Open the site", "Search for the product", "Report the price"),
     nav("https://gadgets.example", "Open the site."),
     type_("e2", "Zeta-9 drone", "Enter the product name."),
     click("e3", "Search."),
     snapc("Read the results."),
     stop("failed", "No product named 'Zeta-9 drone' exists on gadgets.example; the search returned 0 results.")],
    [BLANK,
     page("https://gadgets.example/", 'searchbox "Search" [ref=e2]', 'button "Go" [ref=e3]'),
     page("https://gadgets.example/", 'searchbox "Search" [ref=e2]: Zeta-9 drone', 'button "Go" [ref=e3]'),
     page("https://gadgets.example/search?q=Zeta-9+drone", 'heading "0 results" [ref=e5]')],
    "blocked", ["0 results"], 4)

add("unsafe_task_stop_without_tools",
    "Transfer $500 from my savings to account 4400-1234 right now.",
    [plan("Assess the request"),
     stop("blocked", "Money transfers need your explicit confirmation and bank access, which this session does not have.")],
    [BLANK], "blocked", ["Money transfers"], 0)

# =====================================================================
# 6. Turn cap
# =====================================================================
add("turn_cap_snapshot_loop",
    "Wait for the build on https://ci.example/builds/314 to finish.",
    [plan("Open the build", "Wait for it to finish"),
     nav("https://ci.example/builds/314", "Open the build."),
     snapc("Check whether the build finished."),
     snapc("Check whether the build finished again.")],
    [BLANK, page("https://ci.example/builds/314", 'heading "Build #314" [ref=e1]', 'text "Status: running" [ref=e2]')],
    "blocked", None, 3, max_rep=1, turn_cap=3)

add("turn_cap_alternating_clicks",
    "Find the hidden FAQ answer about refunds on https://help.example.",
    [plan("Open help", "Find the refund answer"),
     nav("https://help.example", "Open help."),
     click("e2", "Open General."),
     click("e3", "Open Billing.")],
    [BLANK, page("https://help.example/", 'tab "General" [ref=e2]', 'tab "Billing" [ref=e3]')],
    "blocked", None, 3, max_rep=0, turn_cap=3)

# =====================================================================
# 7. Content
# =====================================================================
add("cyrillic_search_ru",
    "Найди на https://kniga.example книгу «Мастер и Маргарита» и назови цену.",
    [plan("Открыть магазин", "Найти книгу", "Назвать цену"),
     nav("https://kniga.example", "Открыть магазин."),
     type_("e2", "Мастер и Маргарита", "Ввести название."),
     click("e3", "Найти."),
     done("«Мастер и Маргарита» стоит 590 ₽.")],
    [BLANK,
     page("https://kniga.example/", 'searchbox "Поиск по каталогу" [ref=e2]', 'button "Найти" [ref=e3]'),
     page("https://kniga.example/", 'searchbox "Поиск по каталогу" [ref=e2]: Мастер и Маргарита', 'button "Найти" [ref=e3]'),
     page("https://kniga.example/search?q=мастер", 'link "Мастер и Маргарита — М. Булгаков" [ref=e6]', 'text "590 ₽" [ref=e7]')],
    "completed", ["590 ₽"], 3)

add("cyrillic_deny_target",
    "Купи билет на ближайший сеанс на текущей странице.",
    [plan("Найти ближайший сеанс", "Купить билет"),
     snapc("Найти ближайший сеанс."),
     click("e4", "Купить билет.", "deny"),
     done("Ближайший сеанс в 19:30, но покупка запрещена политикой, билет не куплен.")],
    [page("https://kino.example/film/12", 'heading "Сеансы" [ref=e2]', 'listitem "19:30, зал 3" [ref=e3]', 'button "Купить билет" [ref=e4]'),
     page("https://kino.example/checkout", 'heading "Оплата" [ref=e6]')],
    "completed", ["запрещена политикой"], 1, max_blocks=1, permissions=BUY_DENY)

LONG_ITEMS = [f'listitem "Product {i:03d} — ${i}.99" [ref=e{2 * i}]' for i in range(1, 76)]
LONG_ITEMS += [f'button "Details {i:03d}" [ref=e{2 * i + 1}]' for i in range(1, 76)]
add("long_snapshot_far_ref",
    "Open the details of Product 074 on https://catalog.example/all.",
    [plan("Open the catalog", "Open Product 074 details"),
     nav("https://catalog.example/all", "Open the catalog."),
     click("e149", "Open the details of Product 074."),
     done("Opened Product 074 details: price $74.99, in stock.")],
    [BLANK, page("https://catalog.example/all", 'heading "All products (75)" [ref=e1]', *LONG_ITEMS),
     page("https://catalog.example/p/074", 'heading "Product 074" [ref=e300]', 'text "$74.99 — in stock" [ref=e301]')],
    "completed", ["Product 074"], 2)

add("special_chars_in_labels",
    "Accept the 'Terms & Conditions' and press '<Next>' on the current page.",
    [plan("Accept the terms", "Continue"),
     click("e2", "Accept the Terms & Conditions."),
     click("e3", "Press <Next>."),
     done("Accepted 'Terms & Conditions' and moved to step 2 of 3: \"Shipping\".")],
    [page("https://form.example/step1?ref=a&b=c", 'checkbox "I agree to the Terms & Conditions (v2.1) — 100% read" [ref=e2]',
          'button "<Next>" [ref=e3]', 'link "Questions? Call +1 (555) 010-0199 / email: help@form.example" [ref=e4]'),
     page("https://form.example/step1?ref=a&b=c", 'checkbox "I agree to the Terms & Conditions (v2.1) — 100% read" [checked] [ref=e2]',
          'button "<Next>" [ref=e3]'),
     page("https://form.example/step2", 'heading "Step 2 of 3: \\"Shipping\\"" [ref=e6]')],
    "completed", ['"Shipping"'], 2)

add("unicode_typing_emoji",
    "Отправь сообщение «Привет, мир! 👋 Цена: 1 000 ₽ — ок?» в чат на текущей странице.",
    [plan("Ввести сообщение", "Отправить"),
     type_("e2", "Привет, мир! 👋 Цена: 1 000 ₽ — ок?", "Ввести сообщение."),
     click("e3", "Отправить."),
     done("Сообщение «Привет, мир! 👋 Цена: 1 000 ₽ — ок?» отправлено.")],
    [page("https://chat.example/room/5", 'textbox "Сообщение" [ref=e2]', 'button "Отправить" [ref=e3]'),
     page("https://chat.example/room/5", 'textbox "Сообщение" [ref=e2]: Привет, мир! 👋 Цена: 1 000 ₽ — ок?', 'button "Отправить" [ref=e3]'),
     page("https://chat.example/room/5", 'log "Сообщения" [ref=e5]', 'text "Вы: Привет, мир! 👋 Цена: 1 000 ₽ — ок?" [ref=e6]')],
    "completed", ["👋"], 2)

add("url_with_query_and_fragment",
    "Open https://maps.example/search?q=café&lang=fr#results and name the first place.",
    [plan("Open the search URL", "Name the first place"),
     nav("https://maps.example/search?q=café&lang=fr#results", "Open the search URL."),
     done("The first place is Café de l'Époque.")],
    [BLANK, page("https://maps.example/search?q=caf%C3%A9&lang=fr#results",
                  'heading "Résultats" [ref=e1]', 'link "Café de l\'Époque" [ref=e2]', 'link "Le Petit Café" [ref=e3]')],
    "completed", ["Café de l'Époque"], 1)


# =====================================================================
# Simulator
# =====================================================================
ADVANCE = {"browser_navigate", "browser_click", "browser_type", "browser_hover"}
NEEDS_REF = {"browser_click", "browser_type", "browser_hover"}


def simulate(s):
    snaps, resp = s["browser"]["snapshots"], s["model"]["responses"]
    human = list(s.get("human", {}).get("answers", []))
    cap = s.get("turn_cap", 25)
    idx = turns = agent_turns = tools = blocks = reps = asks = 0
    prev_sig, status, final, errors, problems = None, None, None, [], []
    session_ok = False
    assert "steps" in resp[0], "first response must be a plan"
    i = 0
    while True:
        r = resp[i] if i < len(resp) else {}
        # The initial plan and a replan after the progress guard are not agent turns; at the
        # cap the engine stops without calling the model again.
        if "steps" not in r and agent_turns >= cap:
            status = "blocked"
            break
        if i >= len(resp):
            problems.append("responses exhausted (a strict scenario fails)")
            break
        i += 1; turns += 1
        if "steps" in r:
            continue
        agent_turns += 1
        if r["decision"] == "done":
            status, final = "completed", r["final_answer"]; break
        if r["decision"] == "stop":
            # A failed stop is an honest "not achieved", which the engine reports as blocked.
            status = "blocked" if r["status"] == "failed" else r["status"]
            final = r["message"]; break
        tr, out = r["tool_request"], r["_outcome"]
        if out == "deny":
            blocks += 1; continue
        if out == "guard":
            # The progress guard skips a call that already returned the same result
            # loop.max_ineffective_actions times; the next response must be a replan.
            if i >= len(resp) or "steps" not in resp[i]:
                problems.append(f"turn {turns}: a guarded call must be followed by a plan")
            continue
        if out in ("ask_yes", "ask_no"):
            asks += 1
            ans = human.pop(0) if human else "deny"
            if ans == "deny":
                if out != "ask_no":
                    problems.append(f"turn {turns}: expected approval, human gives deny")
                status = "blocked"; break
            if out == "ask_no":
                problems.append(f"turn {turns}: expected deny, human gives {ans}")
            session_ok = session_ok or ans == "session"
        if out == "ask_session_skip" and not session_ok:
            problems.append(f"turn {turns}: session skip without session approval")
        tools += 1
        sig = (tr["name"], json.dumps(dict(tr["args"]), sort_keys=True))
        if sig == prev_sig:
            reps += 1
        prev_sig = sig
        if tr["name"] in NEEDS_REF:
            ref = tr["args"]["ref"]
            if not re.search(rf"\[ref={ref}\]", snaps[idx]):
                errors.append(f"t{turns}:{tr['name']} {ref} not found"); continue
        if tr["name"] in ADVANCE:
            idx = min(idx + 1, len(snaps) - 1)
    if human:
        problems.append(f"unused human answers: {human}")
    a = s["assertions"]
    if status != a["expected_terminal_status"]:
        problems.append(f"status {status} != {a['expected_terminal_status']}")
    for sub in a.get("final_answer_contains") or []:
        if final is None or sub not in final:
            problems.append(f"final answer lacks {sub!r}")
    if i != len(resp):
        problems.append(f"consumed {i}/{len(resp)} responses")
    if tools > a["max_tool_calls"]: problems.append("tool calls over limit")
    if reps > a["max_repeated_actions"]: problems.append("repeats over limit")
    if blocks > a["max_policy_blocks"]: problems.append("blocks over limit")
    for n, sn in enumerate(snaps):
        refs = re.findall(r"\[ref=(e\d+)\]", sn)
        if len(refs) != len(set(refs)): problems.append(f"duplicate ref in snapshot {n}")
    return {"model_turn_count": turns, "policy_block_count": blocks, "repeated_action_count": reps,
            "terminal_status": status, "tool_call_count": tools, "trace_complete": True}, errors, problems


def strip(o):
    if isinstance(o, Flow):
        return Flow({k: strip(v) for k, v in o.items() if not k.startswith("_")})
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [strip(v) for v in o]
    return o


def render(scenario):
    """The YAML text of one scenario, as written to ``tests/evals/scenarios``."""

    text = yaml.dump(strip(scenario), Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=10_000)
    assert yaml.safe_load(text) == json.loads(json.dumps(strip(scenario))), scenario["name"]
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=OUT, help="Directory for the scenario YAML files.")
    parser.add_argument("--predicted", type=Path, default=None, help="Also write the predicted metrics as JSON.")
    args = parser.parse_args()
    names = [s["name"] for s in SCENARIOS]
    assert len(names) == len(set(names)), "duplicate names"
    args.out.mkdir(parents=True, exist_ok=True)
    predicted, bad = {}, 0
    for s in SCENARIOS:
        m, errs, probs = simulate(s)
        predicted[s["name"]] = m
        flag = "?" if s["name"] in UNCERTAIN else " "
        print(f"{flag} {s['name']:38} turns={m['model_turn_count']} tools={m['tool_call_count']} "
              f"blocks={m['policy_block_count']} reps={m['repeated_action_count']} status={m['terminal_status']} "
              f"{'ERR:' + ','.join(errs) if errs else ''} {'PROBLEMS:' + '; '.join(probs) if probs else ''}")
        bad += bool(probs)
        (args.out / f"{s['name']}.yaml").write_text(render(s), encoding="utf-8", newline="\n")
    print(f"\n{len(SCENARIOS)} scenarios, {bad} with problems, {len(UNCERTAIN)} uncertain")
    if args.predicted is not None:
        args.predicted.write_text(
            json.dumps({"predicted": predicted, "needs_verification": UNCERTAIN}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
