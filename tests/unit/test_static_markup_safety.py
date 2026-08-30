from __future__ import annotations

from tools.check_static_markup import check_tree, markup_violations


def test_static_dashboard_markup_passes_shared_escape_check() -> None:
    assert check_tree() == []


def test_markup_check_rejects_raw_values_and_unsafe_attribute_contexts() -> None:
    assert markup_violations("target.innerHTML = `<p>${peer.name}</p>`;")
    assert markup_violations(
        'import {escapeHtml} from "/ui-primitives.js"; target.innerHTML = '
        "`<p data-name=${escapeHtml(peer.name)}>ok</p>`;"
    )
    assert markup_violations(
        'import {escapeHtml} from "/ui-primitives.js"; target.innerHTML = '
        '`<a href="${escapeHtml(peer.url)}">open</a>`;'
    )


def test_markup_check_accepts_shared_escape_and_constrained_local_urls() -> None:
    source = (
        'import {escapeHtml, safeLocalHref} from "/ui-primitives.js"; '
        'target.innerHTML = `<a href="${escapeHtml(safeLocalHref(link))}">'
        "${escapeHtml(peer.name)}</a>`;"
    )
    assert markup_violations(source) == []
