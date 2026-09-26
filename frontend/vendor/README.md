# Vendored frontend libraries

Third-party libraries bundled into PawPoller. Kept local rather than
loaded from a CDN so the desktop build works offline and the dashboard
isn't tied to external uptime.

| File | Source | Version | License |
|------|--------|---------|---------|
| `epub.min.js` | https://github.com/futurepress/epub.js | 0.3.93 | BSD-2-Clause |
| `jszip.min.js` | https://github.com/Stuk/jszip | 3.10.1 | MIT |
| `../js/vendor/Sortable.min.js` | https://github.com/SortableJS/Sortable | 1.15.6 | MIT |

`Sortable.min.js` lives under `frontend/js/vendor/` with the dashboard's other
scripts; it is the mouse + touch drag for the Trello board (`trello_board.js`).

`epub.js` depends on `jszip` at runtime — load `jszip.min.js` first.

Used by `frontend/epub-viewer.html` to render in-app EPUB previews
linked from the editor's Downloads dropdown.
