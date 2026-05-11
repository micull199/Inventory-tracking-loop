/* UC Inventory — leaf-only searchable category picker.
 *
 * Plumbs the items form's vanilla combobox: opens / closes the results
 * dropdown, handles click + keyboard selection, and dispatches the change
 * event on the hidden #taxonomy_node_id input so the existing _custom-fields
 * HTMX chain runs (custom fields + SKU preview OOB swap).
 *
 * HTMX handles the actual fetch on input — see items_form.html. This file
 * only owns selection / a11y / show-hide.
 *
 * Dependency-free vanilla JS to match the rest of the codebase voice. */

(function () {
    "use strict";

    function setup() {
        const search = document.getElementById("taxonomy_node_search");
        const hidden = document.getElementById("taxonomy_node_id");
        const results = document.getElementById("taxonomy_node_results");
        if (!search || !hidden || !results) {
            return;
        }

        function showResults() {
            results.hidden = false;
            search.setAttribute("aria-expanded", "true");
        }

        function hideResults() {
            results.hidden = true;
            search.setAttribute("aria-expanded", "false");
            search.removeAttribute("aria-activedescendant");
            clearHighlight();
        }

        function options() {
            return Array.from(results.querySelectorAll("li[role='option'][data-id]"));
        }

        function clearHighlight() {
            options().forEach((o) => o.removeAttribute("aria-selected"));
        }

        function highlight(opt) {
            if (!opt) return;
            clearHighlight();
            opt.setAttribute("aria-selected", "true");
            if (opt.id) {
                search.setAttribute("aria-activedescendant", opt.id);
            }
            // Keep the highlighted item in view without animation jitter.
            if (typeof opt.scrollIntoView === "function") {
                opt.scrollIntoView({ block: "nearest" });
            }
        }

        function selectOption(opt) {
            if (!opt || !opt.dataset || !opt.dataset.id) return;
            hidden.value = opt.dataset.id;
            search.value = opt.dataset.breadcrumb || "";
            hideResults();
            // Fire change on the hidden input so HTMX picks it up (custom
            // fields + SKU preview swap). Bubbles so the form-level listeners
            // (if any) catch it.
            hidden.dispatchEvent(new Event("change", { bubbles: true }));
        }

        function currentHighlight() {
            return results.querySelector("li[aria-selected='true']");
        }

        // ---- Wiring ---------------------------------------------------------

        search.addEventListener("focus", showResults);

        search.addEventListener("input", function () {
            // A fresh keystroke means the previously-chosen id is stale until
            // the user picks again. Clear so a partial query can't accidentally
            // submit with the prior selection's id.
            if (hidden.value && search.value !== (hidden.dataset.currentBreadcrumb || "")) {
                hidden.value = "";
            }
            showResults();
        });

        // Hide when focus genuinely leaves the picker. Delay so a click on an
        // option fires before we hide (click target's blur happens first).
        document.addEventListener("click", function (ev) {
            if (!results.contains(ev.target) && ev.target !== search) {
                hideResults();
            }
        });

        results.addEventListener("click", function (ev) {
            const opt = ev.target.closest("li[role='option'][data-id]");
            if (opt) {
                selectOption(opt);
            }
        });

        search.addEventListener("keydown", function (ev) {
            const opts = options();
            if (ev.key === "ArrowDown") {
                ev.preventDefault();
                showResults();
                if (!opts.length) return;
                const cur = currentHighlight();
                const idx = cur ? opts.indexOf(cur) : -1;
                highlight(opts[Math.min(idx + 1, opts.length - 1)]);
            } else if (ev.key === "ArrowUp") {
                ev.preventDefault();
                if (!opts.length) return;
                const cur = currentHighlight();
                const idx = cur ? opts.indexOf(cur) : opts.length;
                highlight(opts[Math.max(idx - 1, 0)]);
            } else if (ev.key === "Enter") {
                const cur = currentHighlight();
                if (cur) {
                    ev.preventDefault();
                    selectOption(cur);
                }
            } else if (ev.key === "Escape") {
                hideResults();
            }
        });

        // After HTMX swaps in a new option list, the previous highlight is
        // gone — repaint nothing and reopen the dropdown.
        results.addEventListener("htmx:afterSwap", function () {
            showResults();
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", setup);
    } else {
        setup();
    }
})();
