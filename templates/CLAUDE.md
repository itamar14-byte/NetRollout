# Frontend (templates/)

Loaded only when working under `templates/`. Claude writes the templates/HTML directly (see the root CLAUDE.md, Working style).

## Frontend

Always-dark enterprise aesthetic — permanently dark, no toggle. Key design elements:
- **Fonts:** Inter (body) + JetBrains Mono (monospace/badges)
- **Accent color:** `#00bcd4` cyan
- **Custom classes:** `.nr-card`, `.nr-card-accent`, `.nr-card-body`, `.nr-badge`, `.nr-label`, `.nr-back-btn`
- **Dot-grid background:** `body::before` at `z-index: -1` (NOT 0 — traps modals)
- **`.container` must NOT have z-index** — breaks Bootstrap modal stacking
- All Bootstrap components overridden in `base.html` to match dark theme
- All operator pages extend `operator_base.html`. Topbar and footer are automatic.
- **Admin pages extend `admin.html`** — standalone template (does not extend `base.html` or `operator_base.html`). Own topbar with ← Home button, own collapsible sidebar (Access / Observability / System sections), own footer, restart button. Sub-pages: `admin_users.html`, `admin_audit.html`, `admin_analytics.html`, `server_management.html`.
- **nginx** sits in front of Waitress. Config at `docs/nginx/nginx.conf`, bind-mounted to `/etc/nginx/nginx.conf`. Flask uses `ProxyFix(x_for=1, x_proto=1, x_host=1)`. SSE (`/rollout/stream/<job_id>`) streams through nginx because the app sends `X-Accel-Buffering: no`; the `location /rollout_stream` block in `nginx.conf` is dead config, to be cleaned up with the 4.1 compose rewrite.
- **Vendor logos**: `VENDOR_LOGOS` dict in `src/webapp/setup.py` maps Netmiko device_type → Simple Icons CDN URL. Registered as Jinja2 global — available in all templates as `VENDOR_LOGOS`.
- **NrSelect widget**: custom FortiGate-style dropdown in `inventory.html` — search box, scrollable list, shield icon, cyan checkmark. Init with `initNrSelect(containerId)`, returns `{getValue, setValue, reset}`.
- **Inventory cards**: thin horizontal rectangles — vendor badge (CDN SVG + BI router fallback) + label + IP. Hover tooltip (FortiGate-style fixed panel). Click → edit modal.
- **Assign board** (Security Profiles + Variable Mappings devices modals): shared `nrAssignBoard()` in `operator_base.html` <head>. Two columns, Assigned | Available/Eligible; drag either way or click/Enter a card to move it. Moves are staged; Save shows `(+N / −M)`. Security saves via `/inventory/bulk_assign` (`profile_id: null` unassigns) and warns when devices lose their profile; mappings save via `/mappings/bulk_assign` with `device_ids` + `remove_ids`.

## Frontend asset structure
Per-page CSS and JS live inline in `{% block extra_style %}` and `{% block extra_script %}` blocks — no build pipeline, one file per page. Shared widgets (reachability helpers, the assign board) live in `operator_base.html` `<head>` so page scripts can call them. Extracting to `static/css/` and `static/js/` is deferred post-v1.0.
