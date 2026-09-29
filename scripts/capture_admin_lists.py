"""Capture admin lists against a running preview without changing its data.

Usage: .venv/bin/python scripts/capture_admin_lists.py --url http://127.0.0.1:8088 \
    --key-file /path/to/admin-key.txt
"""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


VIEWS = ("cross-checks", "annotators", "corpus", "quality", "activity", "overview")
MEASURE = """root => {
  const selectors = {
    container: '.admin-list',
    header: '.ant-table-thead th:not(.ant-table-selection-column)',
    cell: '.ant-table-row td:not(.admin-table-meta):not(.ant-table-selection-column)',
    button: '.ant-table-row .ant-btn:not(.admin-person-button)',
    tag: '.ant-table-row .ant-tag',
    footer: '.admin-list-footer',
  };
  return Object.fromEntries(Object.entries(selectors).map(([name, selector]) => {
    const element = root.querySelector(selector);
    if (!element) return [name, null];
    const style = getComputedStyle(element);
    return [name, Object.fromEntries(
      ['fontSize', 'lineHeight', 'fontWeight', 'padding']
        .map(key => [key, style[key]])
        .concat(['container', 'button', 'tag'].includes(name) ? [['borderRadius', style.borderRadius]] : [])
        .concat(name === 'button' ? [['height', style.height]] : [])
    )];
  }));
}"""

MEASURE_ACTIONS = """root => [...root.querySelectorAll('.admin-table')].flatMap(table => {
  const header = [...table.querySelectorAll('thead th')].find(cell => cell.textContent.trim() === 'Actions');
  if (!header || header.querySelector('.cc-sr-only')) return [];
  const index = [...header.parentElement.children].indexOf(header);
  const range = document.createRange();
  range.selectNodeContents(header);
  const left = range.getBoundingClientRect().left;
  return [...table.querySelectorAll('tbody .ant-table-row')].flatMap(row => {
    const buttons = row.children[index].querySelectorAll('button');
    return buttons.length ? [{
      buttons: [...buttons].map(button => button.textContent),
      offset: buttons[0].getBoundingClientRect().left - left,
    }] : [];
  });
})"""


def capture(page, output, name, scope="body", full_page=False):
    expect(page.locator(".admin-list").first).to_be_visible()
    page.wait_for_load_state("networkidle")
    page.screenshot(path=str(output / f"{name}.png"), full_page=full_page)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
    result = page.locator(scope).evaluate(MEASURE)
    result["actions"] = page.locator(scope).evaluate(MEASURE_ACTIONS)
    assert all(abs(row["offset"]) < 1 for row in result["actions"]), (name, result["actions"])
    return result


def compare(reference, actual, name):
    for kind in ("container", "header", "cell", "button", "tag"):
        if reference[kind] is not None and actual[kind] is not None:
            assert actual[kind] == reference[kind], (name, kind, actual[kind], reference[kind])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/admin-lists"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    base = args.url.rstrip("/") + "/admin"
    report = {}
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(reduced_motion="reduce")
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base)
        page.locator("#adminKey").fill(args.key_file.read_text().strip())
        page.locator("#loginButton").click()
        expect(page.locator("#adminApp")).to_be_visible()
        for width, height in ((1440, 1000), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            for mode in ("light", "dark"):
                page.emulate_media(color_scheme=mode)
                group = f"{width}-{mode}"
                snapshots = {}
                for view in VIEWS:
                    page.goto(f"{base}?view={view}&range=all")
                    snapshots[view] = capture(page, args.output, f"{view}-{group}")
                    if view != "overview":
                        compare(snapshots["cross-checks"], snapshots[view], f"{view}-{group}")
                        page.locator(".admin-list").first.evaluate("el => el.scrollIntoView({block: 'start'})")
                        page.screenshot(path=str(args.output / f"{view}-list-{group}.png"))
                        scroller = page.locator(".admin-table .ant-table-content").first
                        if width < 600 and scroller.count():
                            scroller.evaluate("el => el.scrollLeft = el.scrollWidth")
                            page.screenshot(path=str(args.output / f"{view}-scrolled-{group}.png"))
                    else:
                        page.locator(".admin-grid-two").scroll_into_view_if_needed()
                        page.screenshot(path=str(args.output / f"overview-lists-{group}.png"))
                page.goto(f"{base}?view=annotators")
                expect(page.get_by_test_id("annotator-table")).to_be_visible()
                page.wait_for_load_state("networkidle")
                person = page.locator("[data-annotator-id].ant-btn").first
                if person.count():
                    person.click()
                    drawer = page.locator(".admin-annotator-drawer")
                    expect(drawer).to_be_visible()
                    drawer.get_by_role("region", name="Current annotations").scroll_into_view_if_needed()
                    snapshots["annotator-drawer"] = capture(
                        page, args.output, f"annotator-drawer-{group}", ".admin-annotator-drawer"
                    )
                    compare(snapshots["cross-checks"], snapshots["annotator-drawer"], f"drawer-{group}")
                page.goto(f"{base}?view=corpus&range=all&status=annotated")
                page.wait_for_load_state("networkidle")
                view_button = page.get_by_test_id("corpus-tbody").get_by_role("button", name="View", exact=True).first
                if view_button.count():
                    view_button.click()
                    drawer = page.locator(".admin-task-drawer")
                    expect(drawer).to_be_visible()
                    drawer.get_by_role("region", name="Version history").scroll_into_view_if_needed()
                    capture(page, args.output, f"task-history-{group}", ".admin-task-drawer")
                report[group] = snapshots
        browser.close()
    report["page_errors"] = errors
    (args.output / "styles.json").write_text(json.dumps(report, indent=2) + "\n")
    assert not errors, errors
    print(f"Captured all six pages and available detail lists: {args.output}")


if __name__ == "__main__":
    main()
