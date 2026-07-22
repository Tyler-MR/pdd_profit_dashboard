<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ collapsed: sidebarCollapsed }">
      <div class="brand-row">
        <div class="brand-mark">率</div>
        <div v-if="!sidebarCollapsed" class="brand-copy">
          <strong>利润率看板</strong>
          <span>Profit workspace</span>
        </div>
        <button class="icon-button sidebar-toggle" aria-label="收起导航" @click="sidebarCollapsed = !sidebarCollapsed">{{ sidebarCollapsed ? '›' : '‹' }}</button>
      </div>

      <nav class="nav-list" aria-label="看板模块">
        <a v-for="item in navItems" :key="item.label" class="nav-item" :class="{ active: item.active }" :href="item.href">
          <span class="nav-icon">{{ item.icon }}</span>
          <span v-if="!sidebarCollapsed">{{ item.label }}</span>
        </a>
      </nav>

      <div v-if="!sidebarCollapsed" class="sidebar-footer">
        <div class="status-dot" :class="{ offline: !!apiMessage }"></div>
        <div><strong>{{ apiMessage ? 'API 异常' : 'API 已连接' }}</strong><span>{{ statusText }}</span></div>
      </div>
    </aside>

    <main class="main-area">
      <header class="topbar">
        <div>
          <p class="breadcrumb">经营分析 / 链接群控</p>
          <h1>链接群控</h1>
        </div>
        <div class="topbar-actions">
          <div class="sync-copy"><span class="status-dot" :class="{ offline: !!apiMessage }"></span>{{ lastUpdated ? `同步于 ${lastUpdated}` : '等待数据同步' }}</div>
          <button class="button secondary" :disabled="loading || linkDashboardLoading" @click="refreshPage">{{ loading || linkDashboardLoading ? '同步中…' : '↻ 刷新数据' }}</button>
        </div>
      </header>

      <div v-if="apiMessage" class="error-banner"><strong>数据加载失败</strong><span>{{ apiMessage }}</span><button class="text-button" @click="initialize">重试</button></div>
      <div v-if="initialLoading" class="loading-state"><div class="loading-spinner"></div><strong>正在从 API 加载链接群控…</strong><span>不会使用本地嵌入数据</span></div>

      <template v-else>
        <section class="toolbar panel-lite link-control-toolbar">
          <div class="toolbar-title"><span class="toolbar-icon">⌁</span><div><strong>数据范围</strong><span>{{ rangeHint }}</span></div></div>
          <div class="toolbar-body">
            <div class="range-controls">
              <label>开始 <input v-model="dateStart" type="date" :min="availableDates[0]" :max="availableDates.at(-1)" /></label>
              <span>至</span>
              <label>结束 <input v-model="dateEnd" type="date" :min="availableDates[0]" :max="availableDates.at(-1)" /></label>
              <button class="range-chip" :class="{ active: rangePreset === 'all' }" @click="setRange('all')">全部</button>
              <button class="range-chip" :class="{ active: rangePreset === 'month' }" @click="setRange('month')">本月</button>
              <button class="range-chip" :class="{ active: rangePreset === '7d' }" @click="setRange('7d')">近 7 天</button>
            </div>
            <div class="filter-controls" aria-label="维度筛选">
              <label>链接 ID <input v-model="globalFilters.link_ids" placeholder="支持逗号分隔" /></label>
              <label>商品编码 <input v-model="globalFilters.product_code" placeholder="如 FG2" /></label>
              <label>店铺名称 <input v-model="globalFilters.store_name" placeholder="输入店铺名称" /></label>
              <label>负责人 <select v-model="globalFilters.store_person"><option value="">全部负责人</option><option v-for="person in peopleNames" :key="person" :value="person">{{ person }}</option></select></label>
              <button class="button primary compact" :disabled="linkDashboardLoading" @click="applyFilters">{{ linkDashboardLoading ? '加载中…' : '应用' }}</button>
              <button class="button secondary compact" :disabled="linkDashboardLoading" @click="clearFilters">清除筛选</button>
            </div>
          </div>
        </section>

        <section class="panel link-detail-panel link-control-panel">
          <div class="link-detail-header">
            <button type="button" class="link-detail-title" :aria-expanded="dashboardExpanded" title="点击收起/展开" @click="dashboardExpanded = !dashboardExpanded">
              <span class="toggle-icon">{{ dashboardExpanded ? '▼' : '▶' }}</span>
              <span>📊 链接群控</span>
              <small>({{ linkDashboardMeta.total.toLocaleString() }}条)</small>
            </button>
            <div class="link-detail-controls">
              <input v-model="linkSearch" class="link-search-input" placeholder="🔍 搜索链接ID/编码/标题..." @keyup.enter="fetchDashboard(1)" />
              <span>每页</span>
              <select v-model.number="pageSize" @change="fetchDashboard(1)"><option :value="20">20条</option><option :value="50">50条</option><option :value="100">100条</option></select>
              <div class="link-pager link-pager-top">
                <button type="button" :disabled="linkDashboardMeta.page <= 1 || linkDashboardLoading" aria-label="第一页" @click="fetchDashboard(1)">«</button>
                <button type="button" :disabled="linkDashboardMeta.page <= 1 || linkDashboardLoading" aria-label="上一页" @click="fetchDashboard(linkDashboardMeta.page - 1)">‹</button>
                <span>{{ linkDashboardMeta.page }} / {{ linkDashboardMeta.pages || 1 }}</span>
                <button type="button" :disabled="linkDashboardMeta.page >= linkDashboardMeta.pages || linkDashboardLoading" aria-label="下一页" @click="fetchDashboard(linkDashboardMeta.page + 1)">›</button>
                <button type="button" :disabled="linkDashboardMeta.page >= linkDashboardMeta.pages || linkDashboardLoading" aria-label="最后一页" @click="fetchDashboard(linkDashboardMeta.pages || 1)">»</button>
              </div>
            </div>
          </div>

          <div v-if="dashboardExpanded" class="link-detail-content">
            <div class="link-alerts">
              <template v-for="group in linkAlertGroups" :key="group.key">
                <section v-if="group.items.length" class="link-alert-group" :class="group.tone">
                  <button type="button" class="link-alert-header" :aria-expanded="alertOpen[group.key]" @click="toggleAlert(group.key)"><span>{{ group.icon }} {{ group.label }} <small>({{ group.count }}条)</small></span><span>{{ alertOpen[group.key] ? '▼' : '▶' }}</span></button>
                  <div v-show="alertOpen[group.key]" class="link-alert-list">
                    <button v-for="item in group.items" :key="item.id" type="button" class="link-alert-item" @click="selectAlert(item)"><span class="alert-days">{{ item.days }}天</span><code>{{ item.id }}</code><span>{{ item.code }}</span><em>{{ item.store }}</em></button>
                    <div v-if="group.count > group.items.length" class="link-alert-more">...还有{{ group.count - group.items.length }}条</div>
                  </div>
                </section>
              </template>
              <div v-if="!linkDashboardLoading && !linkAlertGroups.some((group) => group.items.length)" class="link-alert-empty">当前日期范围内没有连续亏损预警</div>
            </div>

            <div class="link-detail-table-scroll">
              <table class="link-detail-table">
                <thead><tr><th v-for="column in linkDashboardFixedColumns" :key="column.key" :class="`link-fixed-${column.key}`">{{ column.label }}</th><th v-for="date in linkDashboardDates" :key="date" class="link-date-column">{{ date.slice(5) }}</th></tr></thead>
                <tbody>
                  <tr v-for="row in linkDashboardRows" :key="row.linkId"><td v-for="column in linkDashboardFixedColumns" :key="column.key" :class="`link-fixed-${column.key}`" :title="row[column.key]">{{ row[column.key] || '—' }}</td><td v-for="date in linkDashboardDates" :key="`${row.linkId}-${date}`" class="link-rate-cell" :class="linkRateTone(row.rates?.[date])">{{ formatLinkRate(row.rates?.[date]) }}</td></tr>
                  <tr v-if="!linkDashboardLoading && !linkDashboardRows.length"><td :colspan="linkDashboardFixedColumns.length + linkDashboardDates.length" class="empty-cell">暂无链接数据</td></tr>
                </tbody>
              </table>
            </div>
            <div class="link-pager link-pager-bottom">
              <button type="button" :disabled="linkDashboardMeta.page <= 1 || linkDashboardLoading" @click="fetchDashboard(1)">«</button>
              <button type="button" :disabled="linkDashboardMeta.page <= 1 || linkDashboardLoading" @click="fetchDashboard(linkDashboardMeta.page - 1)">‹</button>
              <span>第 {{ linkDashboardMeta.page }} / {{ linkDashboardMeta.pages || 1 }} 页</span>
              <button type="button" :disabled="linkDashboardMeta.page >= linkDashboardMeta.pages || linkDashboardLoading" @click="fetchDashboard(linkDashboardMeta.page + 1)">›</button>
              <button type="button" :disabled="linkDashboardMeta.page >= linkDashboardMeta.pages || linkDashboardLoading" @click="fetchDashboard(linkDashboardMeta.pages || 1)">»</button>
            </div>
          </div>
        </section>
      </template>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import { useProfitData } from './composables/useProfitData';

const navItems = [
  { label: '目标进度', icon: '↗', href: '/' },
  { label: '负责人总览', icon: '◎', href: '/?tab=overview' },
  { label: '店铺明细', icon: '▦', href: '/?tab=stores' },
  { label: '商品分析', icon: '◇', href: '/?tab=products' },
  { label: '链接明细', icon: '⌁', href: '/?tab=links' },
  { label: '链接群控', icon: '▤', href: '/link-control.html', active: true },
  { label: '成本结构', icon: '◒', href: '/?tab=cost' },
  { label: '管理中台', icon: '⚙', href: '/?tab=admin' },
];

const { data, status, loading, error, lastUpdated, availableDates, loadAll, refresh: refreshData, linkDashboard, linkDashboardLoading, loadLinkDashboard } = useProfitData();
const sidebarCollapsed = ref(false);
const dateStart = ref('');
const dateEnd = ref('');
const rangePreset = ref('');
const linkSearch = ref('');
const pageSize = ref(20);
const dashboardExpanded = ref(true);
const pageError = ref('');
const alertOpen = reactive({ a15: true, a10: false, a5: false });
const globalFilters = reactive({ link_ids: '', product_code: '', store_name: '', store_person: '' });

const apiMessage = computed(() => pageError.value || error.value || '');
const initialLoading = computed(() => (loading.value || linkDashboardLoading.value) && !availableDates.value.length);
const peopleNames = computed(() => (data.value.peopleSummary || []).map((item) => item.name).filter(Boolean));
const linkDashboardRows = computed(() => linkDashboard.value.data || []);
const linkDashboardDates = computed(() => linkDashboard.value.dates || []);
const hasDashboard = computed(() => linkDashboardDates.value.length > 0 || linkDashboardRows.value.length > 0);
const linkDashboardFixedColumns = [
  { key: 'linkId', label: '链接ID' },
  { key: 'productCode', label: '商品编码' },
  { key: 'title', label: '商品标题' },
  { key: 'storeName', label: '店铺名称' },
  { key: 'brand', label: '品牌' },
];
const linkDashboardMeta = computed(() => ({
  total: linkDashboard.value.total || 0,
  page: linkDashboard.value.page || 1,
  pages: linkDashboard.value.pages || 0,
  size: linkDashboard.value.size || pageSize.value,
}));
const linkAlertGroups = computed(() => {
  const alerts = linkDashboard.value.alerts || {};
  const counts = linkDashboard.value.alertCounts || {};
  return [
    { key: 'a15', label: '近15天以上利润率<0', icon: '⚠️', tone: 'danger', items: alerts.a15 || [], count: counts.a15 ?? (alerts.a15 || []).length },
    { key: 'a10', label: '近10-14天利润率<0', icon: '⚠️', tone: 'warning', items: alerts.a10 || [], count: counts.a10 ?? (alerts.a10 || []).length },
    { key: 'a5', label: '近5-9天利润率<0', icon: '⚠️', tone: 'info', items: alerts.a5 || [], count: counts.a5 ?? (alerts.a5 || []).length },
  ];
});
const rangeHint = computed(() => dateStart.value && dateEnd.value ? `${dateStart.value} 至 ${dateEnd.value} · ${linkDashboardDates.value.length} 天` : '等待 API 返回日期');
const statusText = computed(() => status.value?.database ? `${status.value.database.rows?.toLocaleString?.() || 0} 行数据` : '等待状态');

function normalizeDateRange() {
  if (dateStart.value && dateEnd.value && dateStart.value > dateEnd.value) [dateStart.value, dateEnd.value] = [dateEnd.value, dateStart.value];
}
function setInitialRange() {
  const dates = availableDates.value;
  if (!dates.length) return;
  const month = dates.at(-1).slice(0, 7);
  const inMonth = dates.filter((date) => date.startsWith(month));
  dateStart.value = inMonth[0] || dates[0];
  dateEnd.value = inMonth.at(-1) || dates.at(-1);
  rangePreset.value = 'month';
}
async function setRange(preset) {
  const dates = availableDates.value;
  if (!dates.length) return;
  rangePreset.value = preset;
  if (preset === 'all') {
    dateStart.value = dates[0];
    dateEnd.value = dates.at(-1);
  } else if (preset === 'month') {
    const month = dates.at(-1).slice(0, 7);
    const inMonth = dates.filter((date) => date.startsWith(month));
    dateStart.value = inMonth[0];
    dateEnd.value = inMonth.at(-1);
  } else {
    dateStart.value = dates[Math.max(0, dates.length - 7)];
    dateEnd.value = dates.at(-1);
  }
  await fetchDashboard(1);
}
function dashboardParams(page = 1) {
  return {
    page,
    size: pageSize.value,
    start: dateStart.value,
    end: dateEnd.value,
    search: linkSearch.value.trim(),
    link_ids: globalFilters.link_ids.trim(),
    product_code: globalFilters.product_code.trim(),
    store_name: globalFilters.store_name.trim(),
    store_person: globalFilters.store_person,
  };
}
async function fetchDashboard(page = 1) {
  normalizeDateRange();
  pageError.value = '';
  try {
    await loadLinkDashboard(dashboardParams(page));
  } catch (err) {
    pageError.value = err.message || '链接群控数据加载失败';
  }
}
async function initialize() {
  pageError.value = '';
  await loadAll();
  if (error.value) return;
  setInitialRange();
  await fetchDashboard(1);
}
async function refreshPage() {
  pageError.value = '';
  try {
    await refreshData();
    if (!error.value) await fetchDashboard(1);
  } catch (err) {
    pageError.value = err.message || '刷新数据失败';
  }
}
async function applyFilters() { await fetchDashboard(1); }
async function clearFilters() {
  Object.assign(globalFilters, { link_ids: '', product_code: '', store_name: '', store_person: '' });
  linkSearch.value = '';
  await fetchDashboard(1);
}
function toggleAlert(key) { alertOpen[key] = !alertOpen[key]; }
async function selectAlert(item) { linkSearch.value = item.id; dashboardExpanded.value = true; await fetchDashboard(1); }
function formatLinkRate(value) { return value === null || value === undefined || value === '' || Number.isNaN(Number(value)) ? '-' : `${(Number(value) * 100).toFixed(1)}%`; }
function linkRateTone(value) { if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return 'missing'; return Number(value) < 0 ? 'negative' : 'positive'; }

onMounted(initialize);
</script>
