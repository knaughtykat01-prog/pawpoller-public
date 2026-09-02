/* ── Chart.js factory functions ────────────────────────────── */
/*
 * Chart.js wrapper singleton managing chart lifecycle and rendering.
 * Uses Chart.js with the date-fns time adapter for temporal X axes and
 * the annotation plugin for milestone marker overlays.
 *
 * All public methods accept a canvasId string, fetch the <canvas> element,
 * and store the resulting Chart instance in _instances for later cleanup.
 */

function _toDate(ts) {
    if (!ts) return new Date(NaN);
    const s = String(ts);
    return new Date(s.endsWith('Z') || s.includes('+') ? s : s + 'Z');
}

/**
 * Compute a simple linear regression trendline from {x: Date, y: number} points.
 * Returns just the first and last projected points (a straight line).
 */
function _trendline(points) {
    const valid = points.filter(p => p.x instanceof Date && !isNaN(p.x) && p.y != null);
    if (valid.length < 2) return null;
    const n = valid.length;
    const t0 = valid[0].x.getTime();
    let sumX = 0, sumY = 0, sumXY = 0, sumX2 = 0;
    for (const p of valid) {
        const x = (p.x.getTime() - t0) / 3600000; // hours from first point
        sumX += x;
        sumY += p.y;
        sumXY += x * p.y;
        sumX2 += x * x;
    }
    const denom = n * sumX2 - sumX * sumX;
    if (denom === 0) return null;
    const slope = (n * sumXY - sumX * sumY) / denom;
    const intercept = (sumY - slope * sumX) / n;
    const xFirst = (valid[0].x.getTime() - t0) / 3600000;
    const xLast = (valid[valid.length - 1].x.getTime() - t0) / 3600000;
    return [
        { x: valid[0].x, y: intercept + slope * xFirst },
        { x: valid[valid.length - 1].x, y: intercept + slope * xLast },
    ];
}

const Charts = {
    /**
     * Map of canvasId -> Chart instance.
     * Tracked so we can call .destroy() before re-rendering into the same
     * <canvas>, preventing "Canvas is already in use" errors and memory leaks
     * when the SPA navigates between pages without full page reloads.
     */
    _instances: {},
    /** Stored chart configs for re-rendering in the expand modal. */
    _configs: {},

    /**
     * Configurable thresholds for milestone marker detection, keyed by metric
     * name. Can be updated at runtime via setMilestones() after loading user
     * preferences from the backend.
     */
    _milestones: {
        views: [100, 250, 500, 1000, 2500, 5000, 10000],
        favorites_count: [10, 25, 50, 100, 250],
        comments_count: [10, 25, 50, 100],
    },

    /**
     * Update milestone thresholds from user preferences.
     * @param {Object} m - { views: [...], faves: [...], comments: [...] }
     */
    setMilestones(m) {
        if (m.views) this._milestones.views = m.views;
        if (m.faves) this._milestones.favorites_count = m.faves;
        if (m.comments) this._milestones.comments_count = m.comments;
    },

    /**
     * Read current theme colors from CSS custom properties so charts
     * adapt to dark/light theme without hard-coded hex values.
     */
    _getThemeColors() {
        const s = getComputedStyle(document.documentElement);
        return {
            text: s.getPropertyValue('--text-secondary').trim() || '#9198a8',
            muted: s.getPropertyValue('--text-muted').trim() || '#5f6578',
            border: s.getPropertyValue('--border').trim() || '#2e3140',
            card: s.getPropertyValue('--bg-card').trim() || '#22252f',
            primary: s.getPropertyValue('--text-primary').trim() || '#e4e6ed',
        };
    },

    /**
     * Themed brand accent (the primary series / "views" line colour), read
     * live from the --accent token so charts follow the active theme — sienna
     * under Quill, violet under the dark themes. Charts are destroyed + rebuilt
     * on theme change (App calls Charts.destroyAll), so reading at build time
     * is sufficient. Fallback is the Quill sienna (kept free of the old violet
     * literal so the reskin's literal-swap can't recurse into this getter).
     */
    _accent() {
        return getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#9a5b34';
    },

    /**
     * Scans sequential snapshots to find the first moment a metric crossed
     * each configured threshold. Compares adjacent snapshot pairs (prev, curr)
     * and records a milestone when prev < threshold <= curr.
     *
     * @param {Array}  snapshots - Chronologically ordered snapshot objects,
     *                             each containing polled_at and metric values.
     * @param {string} metric    - Metric key to check (e.g. 'views').
     * @returns {Array} Array of { threshold, timestamp, metric } objects
     *                  representing each crossed milestone.
     */
    _detectMilestones(snapshots, metric) {
        const thresholds = this._milestones[metric];
        if (!thresholds || snapshots.length < 2) return [];
        const found = [];
        for (let i = 1; i < snapshots.length; i++) {
            const prev = snapshots[i - 1][metric];
            const curr = snapshots[i][metric];
            for (const t of thresholds) {
                if (prev < t && curr >= t) {
                    found.push({ threshold: t, timestamp: snapshots[i].polled_at, metric });
                }
            }
        }
        return found;
    },

    /**
     * Converts detected milestones into Chart.js annotation plugin format.
     * Each milestone becomes a vertical dashed line at the timestamp where the
     * threshold was crossed, with a small colored label at the top showing a
     * compact descriptor like "V:1K" (1000 views) or "F:100" (100 favourites).
     *
     * Colour-coded per metric to match the corresponding dataset line colour.
     * Labels use shortened metric prefixes: V = Views, F = Favourites, C = Comments.
     * Thresholds >= 1000 are displayed in K notation (e.g. 5000 -> "5K").
     *
     * @param {Array}  snapshots - Chronologically ordered snapshot objects.
     * @param {Array}  metrics   - Array of metric keys to scan for milestones.
     * @returns {Object} Keyed annotation config object for Chart.js annotation plugin.
     */
    _milestoneAnnotations(snapshots, metrics) {
        const prefixes = { views: 'V', favorites_count: 'F', comments_count: 'C', reads: 'R', votes: 'Vo', num_lists: 'L', likes: 'Lk', reshares: 'Rs', kudos_count: 'K', hits: 'H', bookmarks_count: 'B' };
        const colors = { views: Charts._accent(), favorites_count: '#f0a050', comments_count: '#5ae0a0', reads: Charts._accent(), votes: '#f0a050', num_lists: '#fbc050', likes: Charts._accent(), reshares: '#fbc050', kudos_count: '#f0a050', hits: Charts._accent(), bookmarks_count: '#fbc050' };
        const annotations = {};
        let idx = 0;
        for (const metric of metrics) {
            const milestones = this._detectMilestones(snapshots, metric);
            for (const m of milestones) {
                // Each annotation is a vertical line (xMin === xMax) with a label pill
                annotations[`ms_${idx++}`] = {
                    type: 'line',
                    xMin: _toDate(m.timestamp),
                    xMax: _toDate(m.timestamp),
                    borderColor: colors[metric] || Charts._accent(),
                    borderWidth: 1,
                    borderDash: [6, 4],
                    label: {
                        display: true,
                        content: `${prefixes[metric]}:${m.threshold >= 1000 ? (m.threshold / 1000) + 'K' : m.threshold}`,
                        position: 'start',
                        backgroundColor: colors[metric] || Charts._accent(),
                        color: '#fff',
                        font: { size: 9, weight: 'bold' },
                        padding: { top: 2, bottom: 2, left: 4, right: 4 },
                        borderRadius: 3,
                    },
                };
            }
        }
        return annotations;
    },

    /**
     * Shared base chart configuration providing a consistent dark-theme
     * appearance across all chart types. Colours are chosen to match the
     * application's CSS custom properties (--surface, --muted, --border, etc.).
     *
     * Includes:
     *  - Responsive sizing with no forced aspect ratio (fills container)
     *  - Muted legend text for low visual weight
     *  - Dark tooltip styling with subtle border
     *  - Dimmed axis ticks and grid lines for a clean dark-mode look
     *  - Y axis always starts at zero for honest visual comparisons
     *
     * Callers override or extend the returned object (e.g. adding time X axis,
     * dual Y axes, or annotation config) before passing to new Chart().
     */
    _baseOptions() {
        const c = this._getThemeColors();
        return {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    labels: { color: c.text, font: { size: 11 } }
                },
                tooltip: {
                    backgroundColor: c.card,
                    titleColor: c.primary,
                    bodyColor: c.text,
                    borderColor: c.border,
                    borderWidth: 1,
                }
            },
            scales: {
                x: {
                    ticks: { color: c.muted, font: { size: 10 } },
                    grid: { color: c.border },
                },
                y: {
                    ticks: { color: c.muted, font: { size: 10 } },
                    grid: { color: c.border },
                    beginAtZero: true,
                }
            }
        };
    },

    /**
     * Time-scale X axis configuration using the date-fns Chart.js adapter.
     *
     * - tooltipFormat: full date+time shown on hover ("25 Jan 2026 14:30")
     * - displayFormats: controls the axis tick labels at each auto-detected
     *   zoom level -- hours show "HH:mm", days/weeks show "dd MMM", months
     *   show "MMM yyyy". Chart.js picks the appropriate unit automatically
     *   based on the data's time span.
     * - maxTicksLimit: caps axis labels at 12 to prevent overcrowding.
     *
     * @returns {Object} Chart.js scale configuration object for a time X axis.
     */
    _timeXAxis() {
        const c = this._getThemeColors();
        return {
            type: 'time',
            ticks: { color: c.muted, font: { size: 10 }, maxTicksLimit: 8, maxRotation: 45, minRotation: 25 },
            grid: { color: c.border },
            time: {
                tooltipFormat: 'dd MMM yyyy HH:mm',
                displayFormats: {
                    hour: 'dd MMM HH:mm',
                    day: 'dd MMM',
                    week: 'dd MMM',
                    month: 'MMM yyyy',
                }
            }
        };
    },

    /**
     * Destroys a single chart instance by canvas ID to free resources and
     * release the canvas element. Prevents the "Canvas is already in use"
     * error that occurs when Chart.js tries to binds to a canvas that still
     * has an active chart attached. Called automatically at the start of
     * every chart-creation method.
     *
     * @param {string} id - The canvas element ID whose chart should be destroyed.
     */
    destroy(id) {
        if (this._instances[id]) {
            this._instances[id].destroy();
            delete this._instances[id];
        }
    },

    /**
     * Destroys all tracked chart instances. Called during SPA page transitions
     * (e.g. by the router) to ensure a clean slate when navigating away from
     * a page that rendered charts.
     */
    destroyAll() {
        Object.keys(this._instances).forEach(id => this.destroy(id));
    },

    /**
     * Time-series multi-metric line chart for the dashboard aggregate view.
     * Overlays views, favourites, and comments as separate coloured lines on
     * a shared Y axis with a time-based X axis.
     *
     * Point radius adapts to data density: when there are more than 50
     * snapshots, individual data points are hidden (radius 0) to keep the
     * chart readable; with fewer points, dots are shown at radius 3.
     *
     * @param {string} canvasId  - ID of the target <canvas> element.
     * @param {Array}  snapshots - Chronologically ordered snapshot objects
     *                             with polled_at timestamps and metric values.
     * @param {Array}  metrics   - Metric keys to plot (defaults to all three).
     */
    aggregateLine(canvasId, snapshots, metrics = ['views', 'favorites_count', 'comments_count'], type = 'line') {
        this.destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;
        const isBar = type === 'bar';

        // Metric-to-colour mapping (consistent across all chart types)
        const colors = {
            views: Charts._accent(),            // blue
            favorites_count: '#f0a050',  // red
            comments_count: '#5ae0a0',   // green
            followers: '#e0679a',        // magenta (follower growth)
        };
        // Human-readable legend labels
        const labels = {
            views: 'Views',
            favorites_count: 'Favorites',
            comments_count: 'Comments',
            followers: 'Followers',
        };

        // Build one dataset per requested metric, plus a trendline for each
        const datasets = [];
        metrics.forEach(m => {
            const data = snapshots.map(s => ({ x: _toDate(s.polled_at), y: s[m] }));
            const color = colors[m] || Charts._accent();
            datasets.push({
                label: labels[m] || m,
                data,
                borderColor: color,
                backgroundColor: isBar ? color + 'cc' : color + '20',
                borderWidth: isBar ? 0 : 2,
                pointRadius: isBar ? 0 : (snapshots.length > 50 ? 0 : 3),
                tension: 0.3,
                fill: !isBar ? false : true,
            });
            // Trendlines only make sense on the line view.
            if (!isBar) {
                const trend = _trendline(data);
                if (trend) {
                    datasets.push({
                        label: (labels[m] || m) + ' Trend',
                        data: trend,
                        borderColor: color + '80',  // 50% opacity
                        borderWidth: 1.5,
                        borderDash: [6, 4],
                        pointRadius: 0,
                        tension: 0,
                        fill: false,
                    });
                }
            }
        });

        const opts = this._baseOptions();
        opts.scales.x = this._timeXAxis();
        opts.scales.y.beginAtZero = false;

        const chartType = isBar ? 'bar' : 'line';
        const chartCfg = { type: chartType, data: { datasets }, datasets, options: opts };
        this._configs[canvasId] = chartCfg;
        this._instances[canvasId] = new Chart(ctx, {
            type: chartType,
            data: { datasets },
            options: opts,
        });
    },

    /**
     * Dual Y-axis time-series chart for individual submission detail pages.
     *
     * Uses two Y axes because views typically dwarf faves/comments in magnitude:
     *  - Left axis  (y):  Views (blue) -- own scale so large view counts
     *                      don't compress the faves/comments lines to zero.
     *  - Right axis (y1): Favourites (red) + Comments (green) -- shares a
     *                      scale since they are usually similar in magnitude.
     *                      Grid lines are hidden on this axis to avoid clutter.
     *
     * Milestone annotations (vertical dashed lines) are overlaid for all three
     * metrics, marking when configured thresholds were crossed (e.g. "V:1K").
     *
     * @param {string} canvasId  - ID of the target <canvas> element.
     * @param {Array}  snapshots - Chronologically ordered snapshot objects.
     */
    submissionLine(canvasId, snapshots, metrics = ['views', 'favorites_count', 'comments_count']) {
        this.destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;

        // Metric configuration: first metric goes on left Y axis, rest on right
        const metricConfig = {
            views:           { label: 'Views',      color: Charts._accent() },
            favorites_count: { label: 'Favorites',  color: '#f0a050' },
            comments_count:  { label: 'Comments',   color: '#5ae0a0' },
            reads:           { label: 'Reads',      color: Charts._accent() },
            votes:           { label: 'Votes',      color: '#f0a050' },
            num_lists:       { label: 'Lists',      color: '#fbc050' },
            likes:           { label: 'Likes',      color: Charts._accent() },
            reshares:        { label: 'Reshares',   color: '#fbc050' },
            kudos_count:     { label: 'Kudos',      color: '#f0a050' },
            hits:            { label: 'Hits',       color: Charts._accent() },
            bookmarks_count: { label: 'Bookmarks',  color: '#fbc050' },
        };

        const leftMetric = metrics[0];
        const rightMetrics = metrics.slice(1);
        const leftCfg = metricConfig[leftMetric] || { label: leftMetric, color: Charts._accent() };
        const rightLabel = rightMetrics.map(m => (metricConfig[m] || { label: m }).label).join(' / ');
        const rightColor = (metricConfig[rightMetrics[0]] || { color: '#f0a050' }).color;

        const opts = this._baseOptions();
        opts.scales.x = this._timeXAxis();

        // Left Y axis -- primary metric
        opts.scales.y = {
            type: 'linear',
            position: 'left',
            ticks: { color: leftCfg.color, font: { size: 10 } },
            grid: { color: '#2e3140' },
            beginAtZero: false,
            title: { display: true, text: leftCfg.label, color: leftCfg.color },
        };
        // Right Y axis -- secondary metrics
        opts.scales.y1 = {
            type: 'linear',
            position: 'right',
            ticks: { color: rightColor, font: { size: 10 } },
            grid: { drawOnChartArea: false },
            beginAtZero: false,
            title: { display: true, text: rightLabel, color: rightColor },
        };

        // Generate milestone annotation lines for all metrics
        const annotations = this._milestoneAnnotations(snapshots, metrics);
        opts.plugins.annotation = { annotations };

        // Build datasets: first metric on left axis, rest on right, plus trendlines
        const datasets = [];
        metrics.forEach((m, i) => {
            const cfg = metricConfig[m] || { label: m, color: Charts._accent() };
            const data = snapshots.map(s => ({ x: _toDate(s.polled_at), y: s[m] }));
            const axisID = i === 0 ? 'y' : 'y1';
            datasets.push({
                label: cfg.label,
                data,
                borderColor: cfg.color,
                borderWidth: 2,
                pointRadius: snapshots.length > 50 ? 0 : 3,
                tension: 0.3,
                yAxisID: axisID,
            });
            const trend = _trendline(data);
            if (trend) {
                datasets.push({
                    label: cfg.label + ' Trend',
                    data: trend,
                    borderColor: cfg.color + '80',
                    borderWidth: 1.5,
                    borderDash: [6, 4],
                    pointRadius: 0,
                    tension: 0,
                    fill: false,
                    yAxisID: axisID,
                });
            }
        });

        this._configs[canvasId] = { type: 'line', data: { datasets }, datasets, options: opts };
        this._instances[canvasId] = new Chart(ctx, {
            type: 'line',
            data: { datasets },
            options: opts,
        });
    },

    /**
     * Horizontal bar chart for "Top Viewed" / "Top Faved" rankings on the
     * dashboard. Bars are oriented horizontally (indexAxis: 'y') so that
     * submission titles are readable as Y-axis labels.
     *
     * Legend is hidden since there is only one dataset. Titles are truncated
     * to 25 characters via Utils.truncate() to prevent label overflow.
     *
     * @param {string} canvasId - ID of the target <canvas> element.
     * @param {Array}  items    - Array of submission objects to rank.
     * @param {string} valueKey - Property name for the bar value (e.g. 'views').
     * @param {string} labelKey - Property name for the bar label (default: 'title').
     */
    topBar(canvasId, items, valueKey, labelKey = 'title') {
        this.destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;

        const labels = items.map(i => Utils.truncate(i[labelKey], 25));
        const values = items.map(i => i[valueKey]);

        const opts = this._baseOptions();
        opts.indexAxis = 'y';                       // horizontal bars
        opts.plugins.legend = { display: false };   // single dataset, no legend needed
        opts.scales.x.beginAtZero = true;
        opts.scales.y.ticks = { color: this._getThemeColors().text, font: { size: 11 } };

        this._instances[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    data: values,
                    backgroundColor: (Charts._accent() + '40'), // purple at 25% opacity for bar fill
                    borderColor: Charts._accent(),       // solid purple border
                    borderWidth: 1,
                }]
            },
            options: opts,
        });
    },

    /**
     * Multi-submission overlay line chart for the Compare page. Each selected
     * submission gets its own line, coloured from a 10-colour palette that
     * wraps around if more than 10 submissions are compared.
     *
     * Unlike submissionLine(), this uses a single Y axis (the chosen metric)
     * so that all submissions are directly comparable on the same scale.
     *
     * Per-series milestone annotations are generated individually so each
     * annotation line inherits the colour of its parent submission's line,
     * making it visually clear which submission hit which milestone.
     *
     * @param {string} canvasId   - ID of the target <canvas> element.
     * @param {Object} seriesData - Map of submissionId -> snapshot arrays.
     * @param {Object} titles     - Map of submissionId -> submission title string.
     * @param {string} metric     - Which metric to plot (default: 'views').
     */
    comparisonLine(canvasId, seriesData, titles, metric = 'views') {
        this.destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;

        // 10-colour palette -- wraps via modulo for >10 submissions
        const palette = [Charts._accent(), '#f0a050', '#5ae0a0', '#fbc050', '#a78bfa', '#e879f9', '#70d4c0', '#d08030', '#70a0ff', '#c07dff'];

        // Build one dataset per submission, each assigned a palette colour
        const datasets = Object.entries(seriesData).map(([id, snaps], i) => ({
            label: Utils.truncate(titles[id] || `#${id}`, 30),
            data: snaps.map(s => ({ x: _toDate(s.polled_at), y: s[metric] })),
            borderColor: palette[i % palette.length],
            borderWidth: 2,
            pointRadius: snaps.length > 50 ? 0 : 3,
            tension: 0.3,
            fill: false,
        }));

        // Per-series milestone annotations: each submission's milestones are
        // coloured to match that submission's line, so the user can tell which
        // series hit which threshold at a glance.
        const annotations = {};
        let idx = 0;
        Object.entries(seriesData).forEach(([id, snaps], i) => {
            const milestones = this._detectMilestones(snaps, metric);
            const color = palette[i % palette.length];
            const prefix = { views: 'V', favorites_count: 'F', comments_count: 'C' }[metric] || '?';
            for (const m of milestones) {
                annotations[`ms_${idx++}`] = {
                    type: 'line',
                    xMin: _toDate(m.timestamp),
                    xMax: _toDate(m.timestamp),
                    borderColor: color,
                    borderWidth: 1,
                    borderDash: [6, 4],
                    label: {
                        display: true,
                        content: `${prefix}:${m.threshold >= 1000 ? (m.threshold / 1000) + 'K' : m.threshold}`,
                        position: 'start',
                        backgroundColor: color,
                        color: '#fff',
                        font: { size: 9, weight: 'bold' },
                        padding: { top: 2, bottom: 2, left: 4, right: 4 },
                        borderRadius: 3,
                    },
                };
            }
        });

        const opts = this._baseOptions();
        opts.scales.x = this._timeXAxis();
        opts.scales.y.beginAtZero = false;
        // Only attach annotation plugin config if there are milestones to show
        if (Object.keys(annotations).length > 0) {
            opts.plugins.annotation = { annotations };
        }

        this._configs[canvasId] = { type: 'line', data: { datasets }, datasets, options: opts };
        this._instances[canvasId] = new Chart(ctx, {
            type: 'line',
            data: { datasets },
            options: opts,
        });
    },

    /**
     * Grouped bar chart for weekly growth on the Analytics page.
     * Shows views/faves/comments gained per week.
     */
    weeklyGrowthBar(canvasId, weeklyData) {
        this.destroy(canvasId);
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;

        const labels = weeklyData.map(w => w.week_label);
        const opts = this._baseOptions();
        opts.plugins.legend = { labels: { color: this._getThemeColors().text, font: { size: 11 } } };

        this._instances[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [
                    { label: 'Views', data: weeklyData.map(w => w.views_delta), backgroundColor: (Charts._accent() + '80'), borderColor: Charts._accent(), borderWidth: 1 },
                    { label: 'Faves', data: weeklyData.map(w => w.faves_delta), backgroundColor: '#f0a05080', borderColor: '#f0a050', borderWidth: 1 },
                    { label: 'Comments', data: weeklyData.map(w => w.comments_delta), backgroundColor: '#5ae0a080', borderColor: '#5ae0a0', borderWidth: 1 },
                ],
            },
            options: opts,
        });
        return this._instances[canvasId];
    },

    /**
     * Open a chart in the expand modal, re-rendered at full size with more detail.
     * @param {string} canvasId - The original canvas ID to expand.
     * @param {string} title    - Chart title for the modal header.
     */
    expand(canvasId, title) {
        const cfg = this._configs[canvasId];
        if (!cfg) return;

        const overlay = document.getElementById('chart-modal-overlay');
        const modalTitle = document.getElementById('chart-modal-title');
        const modalCanvas = document.getElementById('chart-modal-canvas');
        if (!overlay || !modalCanvas) return;

        modalTitle.textContent = title || 'Chart';
        overlay.classList.add('open');
        document.body.style.overflow = 'hidden';

        // Destroy any previous modal chart
        this.destroy('chart-modal-canvas');

        // Build fresh options for expanded view (more ticks, relaxed rotation)
        const opts = this._baseOptions();
        opts.scales.x = this._timeXAxis();
        opts.scales.x.ticks.maxTicksLimit = 20;
        opts.scales.x.ticks.maxRotation = 35;
        opts.scales.x.ticks.minRotation = 0;
        opts.scales.y.beginAtZero = false;

        // Copy over dual-axis config if the original had it
        if (cfg.options.scales?.y1) {
            opts.scales.y = { ...cfg.options.scales.y };
            opts.scales.y1 = { ...cfg.options.scales.y1 };
        }

        // Copy annotation config if present
        if (cfg.options.plugins?.annotation) {
            opts.plugins.annotation = cfg.options.plugins.annotation;
        }

        // Show data points on all datasets at expanded size
        const datasets = cfg.datasets.map(ds => ({
            ...ds,
            pointRadius: ds.borderDash ? 0 : 3,
            pointHoverRadius: ds.borderDash ? 0 : 6,
        }));

        this._instances['chart-modal-canvas'] = new Chart(modalCanvas, {
            type: cfg.type,
            data: { datasets },
            options: opts,
        });
    },

    /** Close the expand modal. */
    closeModal() {
        const overlay = document.getElementById('chart-modal-overlay');
        if (overlay) overlay.classList.remove('open');
        document.body.style.overflow = '';
        this.destroy('chart-modal-canvas');
    },

    /** Wire click handlers on all .chart-container elements to open the expand modal. */
    bindExpandHandlers() {
        // Use event delegation on the main content area
        document.addEventListener('click', (e) => {
            const container = e.target.closest('.chart-container');
            if (!container) return;
            const canvas = container.querySelector('canvas');
            if (!canvas || !this._configs[canvas.id]) return;
            const title = container.querySelector('h3')?.textContent || 'Chart';
            this.expand(canvas.id, title);
        });

        // Close modal handlers
        const overlay = document.getElementById('chart-modal-overlay');
        const closeBtn = document.getElementById('chart-modal-close');
        if (closeBtn) closeBtn.addEventListener('click', () => this.closeModal());
        if (overlay) overlay.addEventListener('click', (e) => {
            if (e.target === overlay) this.closeModal();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') this.closeModal();
        });
    },
};
