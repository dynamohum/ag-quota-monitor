/**
 * Antigravity Quota Monitor — frontend logic
 */

const POLL_INTERVAL = 60_000;
let pollTimer = null;
let quotaData = null;
let resetTimers = [];
let currentView = 'pools'; // 'pools' or 'models'

// ─── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('refreshBtn').addEventListener('click', fetchQuota);
    fetchQuota();
    pollTimer = setInterval(fetchQuota, POLL_INTERVAL);
});

// ─── Fetch quota data ──────────────────────────────────────────────────────────

async function fetchQuota() {
    const btn = document.getElementById('refreshBtn');
    btn.classList.add('refresh-btn--spinning');

    try {
        const res = await fetch('/api/quota');
        const data = await res.json();

        if (data.error) {
            showError(data.error);
            setStatus('error', 'Error');
            return;
        }

        quotaData = data;
        render(data);
        setStatus('ok', 'Connected');
        document.getElementById('lastUpdated').textContent =
            '🕐 ' + new Date().toLocaleTimeString();

    } catch (err) {
        showError('Failed to connect to the server. Is it running?');
        setStatus('error', 'Disconnected');
    } finally {
        btn.classList.remove('refresh-btn--spinning');
    }
}

// ─── Status indicator ──────────────────────────────────────────────────────────

function setStatus(type, label) {
    document.getElementById('statusDot').className = 'status-dot status-dot--' + type;
    document.getElementById('statusText').textContent = label;
}

// ─── Error state ───────────────────────────────────────────────────────────────

function showError(message) {
    document.getElementById('mainContent').innerHTML = `
    <div class="error-container fade-in">
      <span class="error-container__icon">⚠️</span>
      <div class="error-container__title">Connection Issue</div>
      <div class="error-container__msg">${escapeHTML(message)}</div>
      <button class="error-container__retry" id="retryBtn">⟳ Retry</button>
    </div>
  `;
    document.getElementById('retryBtn').addEventListener('click', fetchQuota);
}

// ─── View toggle ───────────────────────────────────────────────────────────────

function setView(view) {
    currentView = view;
    if (quotaData) render(quotaData);
}

// ─── Main render ───────────────────────────────────────────────────────────────

function render(data) {
    clearResetTimers();

    let html = '';

    // User info
    const userInfo = document.getElementById('userInfo');
    if (data.user_email || data.plan_name) {
        document.getElementById('userName').textContent = data.user_email || '';
        document.getElementById('planBadge').textContent = data.plan_name || 'Free';
        userInfo.style.display = 'flex';
    }

    // Credits section
    const hasCredits = data.prompt_credits || data.flow_credits;
    if (hasCredits) {
        html += '<div class="credits-row">';
        if (data.prompt_credits) html += renderCreditCard('Prompt Credits', data.prompt_credits, '💬');
        if (data.flow_credits) html += renderCreditCard('Flow Credits', data.flow_credits, '🔄');
        html += '</div>';
    }

    // Section header with view toggle
    const totalModels = (data.models || []).length;
    const totalPools = (data.pools || []).length;
    const poolsActive = currentView === 'pools' ? 'view-toggle__btn--active' : '';
    const modelsActive = currentView === 'models' ? 'view-toggle__btn--active' : '';

    html += `
    <div class="section-header">
      <div class="section-title">
        🤖 Model Quotas
        <span class="section-title__count">${totalModels} models · ${totalPools} pools</span>
      </div>
      <div class="view-toggle">
        <button class="view-toggle__btn ${poolsActive}"  id="viewPoolsBtn">📦 Pools</button>
        <button class="view-toggle__btn ${modelsActive}" id="viewModelsBtn">📋 Models</button>
      </div>
    </div>
  `;

    html += currentView === 'pools' ? renderPoolsView(data) : renderModelsView(data);

    document.getElementById('mainContent').innerHTML = html;

    // Attach view-toggle listeners (elements just created above)
    document.getElementById('viewPoolsBtn').addEventListener('click', () => setView('pools'));
    document.getElementById('viewModelsBtn').addEventListener('click', () => setView('models'));

    // Start countdown timers
    if (currentView === 'pools') {
        startCountdownTimers(data.pools || [], 'pool-reset-');
    } else {
        startCountdownTimers(data.models || [], 'reset-');
    }
}

// ─── Shared card helpers ───────────────────────────────────────────────────────

function getColorSuffix(remaining) {
    if (remaining < 20) return 'red';
    if (remaining < 50) return 'amber';
    return 'green';
}

function getBadge(remaining, exhausted) {
    if (exhausted) return { cls: 'model-card__badge--exhausted', text: 'EXHAUSTED' };
    if (remaining < 20) return { cls: 'model-card__badge--warning', text: remaining.toFixed(0) + '%' };
    return { cls: 'model-card__badge--ok', text: remaining.toFixed(0) + '%' };
}

// ─── Pool view ─────────────────────────────────────────────────────────────────

function renderPoolsView(data) {
    if (!data.pools || data.pools.length === 0) return '';
    let html = '<div class="pool-grid">';
    data.pools.forEach((pool, i) => { html += renderPoolCard(pool, i); });
    html += '</div>';
    return html;
}

function renderPoolCard(pool, index) {
    const remaining = pool.remaining_percentage ?? 0;
    const used = pool.used_percentage ?? 0;
    const exhausted = pool.is_exhausted;

    const colorSuffix = getColorSuffix(remaining);
    const badge = getBadge(remaining, exhausted);
    const cardMod = exhausted ? 'pool-card--exhausted' : remaining > 80 ? 'pool-card--healthy' : '';

    const chips = pool.models.map(m =>
        `<span class="pool-chip" title="${escapeHTML(m.model_id)}">${escapeHTML(m.label)}</span>`
    ).join('');

    return `
    <div class="pool-card ${cardMod} fade-in">
      <div class="pool-card__header">
        <div>
          <span class="pool-card__name">${escapeHTML(pool.name)}</span>
          <span class="pool-card__count">${pool.model_count} model${pool.model_count > 1 ? 's' : ''}</span>
        </div>
        <span class="model-card__badge ${badge.cls}">${badge.text}</span>
      </div>
      <div class="pool-card__stats">
        <span class="pool-card__pct pool-card__pct--${colorSuffix}">${remaining.toFixed(1)}%</span>
        <span class="pool-card__pct-label">remaining · ${used.toFixed(1)}% used</span>
      </div>
      <div class="pool-card__bar">
        <div class="pool-card__bar-fill pool-card__bar-fill--${colorSuffix}" style="width: ${remaining}%"></div>
      </div>
      <div class="pool-card__meta">
        <span>⏱ Resets in: <strong id="pool-reset-${index}">${formatCountdown(pool.time_until_reset_ms)}</strong></span>
      </div>
      <div class="pool-card__models">${chips}</div>
    </div>
  `;
}

// ─── Models view ───────────────────────────────────────────────────────────────

function renderModelsView(data) {
    if (!data.models || data.models.length === 0) return '';
    let html = '<div class="model-grid">';
    data.models.forEach((model, i) => { html += renderModelCard(model, i); });
    html += '</div>';
    return html;
}

function renderModelCard(model, index) {
    const remaining = model.remaining_percentage ?? 0;
    const used = model.used_percentage ?? 0;
    const exhausted = model.is_exhausted;

    const colorSuffix = getColorSuffix(remaining);
    const badge = getBadge(remaining, exhausted);
    const cardMod = exhausted ? 'model-card--exhausted' : remaining > 80 ? 'model-card--healthy' : '';

    return `
    <div class="model-card ${cardMod} fade-in">
      <div class="model-card__top">
        <div>
          <div class="model-card__name">${escapeHTML(model.label)}</div>
          <div class="model-card__id">${escapeHTML(model.model_id)}</div>
        </div>
        <span class="model-card__badge ${badge.cls}">${badge.text}</span>
      </div>
      <div class="model-card__bar-container">
        <div class="model-card__bar-labels">
          <span class="model-card__bar-used">Used: ${used.toFixed(1)}%</span>
          <span class="model-card__bar-remaining model-card__bar-remaining--${colorSuffix}">
            ${remaining.toFixed(1)}% remaining
          </span>
        </div>
        <div class="model-bar">
          <div class="model-bar__fill model-bar__fill--${colorSuffix}" style="width: ${remaining}%"></div>
        </div>
      </div>
      <div class="model-card__reset">
        <span class="model-card__reset-icon">⏱</span>
        <span>Resets in: <strong id="reset-${index}">${formatCountdown(model.time_until_reset_ms)}</strong></span>
      </div>
    </div>
  `;
}

// ─── Credit card ───────────────────────────────────────────────────────────────

function renderCreditCard(title, credits, icon) {
    const pct = credits.remaining_percentage;
    const barClass = pct > 50 ? '' : pct > 20 ? 'credit-bar__fill--warning' : 'credit-bar__fill--critical';

    return `
    <div class="credit-card fade-in">
      <div class="credit-card__header">
        <span class="credit-card__title">${icon} ${title}</span>
      </div>
      <div class="credit-card__value">
        ${credits.available.toLocaleString()} <small>/ ${credits.monthly.toLocaleString()}</small>
      </div>
      <div class="credit-bar">
        <div class="credit-bar__fill ${barClass}" style="width: ${pct}%"></div>
      </div>
    </div>
  `;
}

// ─── Countdown timers ──────────────────────────────────────────────────────────

/**
 * Start live countdown timers for an array of items (pools or models).
 * @param {Array}  items    - Array with `time_until_reset_ms` and `reset_time_iso`
 * @param {string} idPrefix - DOM element ID prefix, e.g. 'pool-reset-' or 'reset-'
 */
function startCountdownTimers(items, idPrefix) {
    items.forEach((item, i) => {
        if (item.time_until_reset_ms <= 0) return;

        const resetStr = getRelativeDateString(new Date(item.reset_time_iso));
        const endTime = Date.now() + item.time_until_reset_ms;
        const elId = idPrefix + i;

        // Set initial value immediately
        const elInit = document.getElementById(elId);
        if (elInit) elInit.textContent = `${formatCountdown(item.time_until_reset_ms)} (${resetStr})`;

        const timer = setInterval(() => {
            const remaining = endTime - Date.now();
            const el = document.getElementById(elId);
            if (remaining <= 0) {
                clearInterval(timer);
                if (el) el.textContent = 'Ready!';
            } else if (el) {
                el.textContent = `${formatCountdown(remaining)} (${resetStr})`;
            }
        }, 1000);

        resetTimers.push(timer);
    });
}

function clearResetTimers() {
    resetTimers.forEach(t => clearInterval(t));
    resetTimers = [];
}

function formatCountdown(ms) {
    if (ms <= 0) return 'Ready!';
    const totalMins = Math.ceil(ms / 60000);
    if (totalMins < 60) return totalMins + 'm';
    const hours = Math.floor(totalMins / 60);
    const mins = totalMins % 60;
    if (hours < 24) return hours + 'h ' + mins + 'm';
    const days = Math.floor(hours / 24);
    const remHours = hours % 24;
    return days + 'd ' + remHours + 'h';
}

// ─── Utilities ─────────────────────────────────────────────────────────────────

function getRelativeDateString(date) {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const target = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    const diffDays = Math.ceil((target - today) / (1000 * 60 * 60 * 24));
    const timeStr = date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

    if (diffDays === 0) return `Today at ${timeStr}`;
    if (diffDays === 1) return `Tomorrow at ${timeStr}`;
    if (diffDays > 1 && diffDays < 7) {
        return `${date.toLocaleDateString('en-GB', { weekday: 'long' })} at ${timeStr}`;
    }
    return `${date.toLocaleDateString('en-GB', { day: '2-digit', month: '2-digit', year: 'numeric' })} at ${timeStr}`;
}

function escapeHTML(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
