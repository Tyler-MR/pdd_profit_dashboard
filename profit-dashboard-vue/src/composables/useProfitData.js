import { computed, ref } from 'vue';

const apiBase = import.meta.env.VITE_API_BASE || '';
const demoMode = import.meta.env.VITE_DEMO_MODE === 'true';
const demoBase = `${import.meta.env.BASE_URL || './'}demo-data`;
const demoPeriod = '2026-07-01_2026-07-14';
const demoCache = new Map();

const demoKeys = {
  linkId: '\u94fe\u63a5id',
  productCode: '\u5546\u54c1\u7f16\u7801',
  title: '\u5546\u54c1\u6807\u9898',
  storeName: '\u5e97\u94fa\u540d\u79f0',
  person: '\u8d1f\u8d23\u4eba',
  date: '\u6570\u636e\u65e5\u671f',
};

async function loadDemoJson(filename) {
  if (!demoCache.has(filename)) {
    demoCache.set(filename, fetch(`${demoBase}/${filename}`).then(async (response) => {
      if (!response.ok) throw new Error(`Demo fixture request failed (${response.status})`);
      return response.json();
    }));
  }
  return demoCache.get(filename);
}

function splitDemoFilter(value) {
  return String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
}

function inDemoDateRange(value, start, end) {
  const date = String(value || '').slice(0, 10);
  return (!start || date >= start) && (!end || date <= end);
}

function demoRowMatches(row, params = {}) {
  const linkIds = splitDemoFilter(params.link_ids);
  if (linkIds.length && !linkIds.includes(String(row[demoKeys.linkId] ?? ''))) return false;
  if (params.product_code && !String(row[demoKeys.productCode] ?? '').toLowerCase().includes(String(params.product_code).toLowerCase())) return false;
  if (params.store_name && !String(row[demoKeys.storeName] ?? '').toLowerCase().includes(String(params.store_name).toLowerCase())) return false;
  if (params.store_person && String(row[demoKeys.person] ?? '') !== String(params.store_person)) return false;
  if (params.search) {
    const search = String(params.search).toLowerCase();
    const haystack = [demoKeys.linkId, demoKeys.productCode, demoKeys.title, demoKeys.storeName].map((key) => String(row[key] ?? '').toLowerCase());
    if (!haystack.some((value) => value.includes(search))) return false;
  }
  if (row[demoKeys.date] !== undefined && !inDemoDateRange(row[demoKeys.date], params.start, params.end)) return false;

  if (params.filter_json) {
    let filters = [];
    try { filters = JSON.parse(params.filter_json) || []; } catch { filters = []; }
    for (const filter of filters) {
      const value = row[filter.field];
      const text = String(value ?? '').toLowerCase();
      const v1 = String(filter.v1 ?? '').toLowerCase();
      const v2 = String(filter.v2 ?? '').toLowerCase();
      if (filter.op === 'equals' || filter.op === 'eq') {
        if (text !== v1) return false;
      } else if (filter.op === 'gte' || filter.op === 'lte' || filter.op === 'between') {
        const left = Number(value);
        const lower = Number(filter.v1);
        const upper = Number(filter.v2);
        if (Number.isNaN(left)) return false;
        if (filter.op === 'gte' && left < lower) return false;
        if (filter.op === 'lte' && left > lower) return false;
        if (filter.op === 'between' && ((filter.v1 && left < lower) || (filter.v2 && left > upper))) return false;
      } else if (!text.includes(v1)) {
        return false;
      }
    }
  }
  return true;
}

function demoPage(rows, page = 1, size = 20) {
  const safeSize = Math.max(1, Number(size) || 20);
  const total = rows.length;
  const pages = total ? Math.ceil(total / safeSize) : 0;
  const safePage = pages ? Math.min(Math.max(1, Number(page) || 1), pages) : 1;
  return { data: rows.slice((safePage - 1) * safeSize, safePage * safeSize), total, page: safePage, size: safeSize, pages };
}

function emptyData() {
  return {
    grand: {},
    peopleSummary: [],
    products: [],
    allStores: [],
    dailyOverall: [],
    dailyByPerson: {},
    dailyByProduct: {},
    dailyByStore: {},
  };
}

async function request(path, options) {
  const response = await fetch(`${apiBase}${path}`, options);
  const contentType = response.headers.get('content-type') || '';
  const body = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof body === 'object' && body?.error ? body.error : `请求失败（${response.status}）`;
    throw new Error(message);
  }
  return body;
}

export function useProfitData() {
  const data = ref(emptyData());
  const status = ref(null);
  const targets = ref({});
  const loading = ref(false);
  const error = ref('');
  const lastUpdated = ref('');
  const links = ref([]);
  const linkFields = ref([]);
  const linksMeta = ref({ total: 0, page: 1, pages: 0, size: 20 });
  const linksLoading = ref(false);
  const linkDashboard = ref({
    data: [],
    dates: [],
    alerts: { a15: [], a10: [], a5: [] },
    alertCounts: { a15: 0, a10: 0, a5: 0 },
    total: 0,
    page: 1,
    pages: 0,
    size: 20,
  });
  const linkDashboardLoading = ref(false);
  const lastDataParams = ref({});

  const availableDates = computed(() => (data.value.dailyOverall || []).map((item) => String(item.date).slice(0, 10)).sort());

  async function loadAll(params = {}) {
    loading.value = true;
    error.value = '';
    lastDataParams.value = { ...params };
    try {
      if (demoMode) {
        const fixture = await loadDemoJson(`dashboard-${demoPeriod}.json`);
        if (!fixture?.success || !fixture?.data) throw new Error('Demo fixture is empty');
        data.value = fixture.data;
        status.value = fixture.status || null;
        targets.value = fixture.targets || {};
        linkFields.value = fixture.linkFields || [];
        lastUpdated.value = new Date().toLocaleString('zh-CN', { hour12: false });
        return;
      }
      const query = new URLSearchParams();
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') query.set(key, value);
      });
      const [dashboard, system, targetResponse, linkFieldsResponse] = await Promise.all([
        request(`/api/v3/data?${query.toString()}`),
        request('/api/v3/status'),
        request('/api/v3/admin/targets'),
        request('/api/v3/link-fields'),
      ]);
      if (!dashboard?.success || !dashboard?.data) throw new Error(dashboard?.error || '看板数据为空');
      data.value = dashboard.data;
      status.value = system;
      targets.value = targetResponse?.data || {};
      linkFields.value = linkFieldsResponse?.fields || [];
      lastUpdated.value = new Date().toLocaleString('zh-CN', { hour12: false });
    } catch (err) {
      error.value = err.message || '数据加载失败';
    } finally {
      loading.value = false;
    }
  }

  async function refresh() {
    loading.value = true;
    try {
      if (demoMode) {
        await loadAll(lastDataParams.value);
        return;
      }
      await request('/api/v3/refresh', { method: 'POST' });
      await loadAll(lastDataParams.value);
    } finally {
      loading.value = false;
    }
  }

  async function loadLinks(params = {}) {
    linksLoading.value = true;
    try {
      if (demoMode) {
        const fixture = await loadDemoJson(`links-${demoPeriod}.json`);
        const rows = (fixture?.data || []).filter((row) => demoRowMatches(row, params));
        const page = demoPage(rows, params.page, params.size);
        links.value = page.data;
        linksMeta.value = page;
        return;
      }
      const query = new URLSearchParams();
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') query.set(key, value);
      });
      const response = await request(`/api/v3/links?${query.toString()}`);
      if (!response?.success) throw new Error(response?.error || '链接数据加载失败');
      links.value = response.data || [];
      linksMeta.value = {
        total: response.total || 0,
        page: response.page || 1,
        pages: response.pages || 0,
        size: response.size || 20,
      };
    } finally {
      linksLoading.value = false;
    }
  }

  async function loadLinkDashboard(params = {}) {
    linkDashboardLoading.value = true;
    try {
      if (demoMode) {
        const fixture = await loadDemoJson(`link-dashboard-${demoPeriod}.json`);
        const dates = (fixture?.dates || []).filter((date) => inDemoDateRange(date, params.start, params.end));
        const rows = (fixture?.data || []).filter((row) => demoRowMatches({
          [demoKeys.linkId]: row.linkId,
          [demoKeys.productCode]: row.productCode,
          [demoKeys.title]: row.title,
          [demoKeys.storeName]: row.storeName,
          [demoKeys.person]: row.person,
        }, params)).map((row) => ({
          ...row,
          rates: Object.fromEntries(Object.entries(row.rates || {}).filter(([date]) => dates.includes(date))),
        }));
        const page = demoPage(rows, params.page, params.size);
        linkDashboard.value = {
          ...fixture,
          data: page.data,
          dates,
          total: page.total,
          page: page.page,
          pages: page.pages,
          size: page.size,
        };
        return;
      }
      const query = new URLSearchParams();
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') query.set(key, value);
      });
      const response = await request(`/api/v3/link-dashboard?${query.toString()}`);
      if (!response?.success) throw new Error(response?.error || '链接看板加载失败');
      linkDashboard.value = {
        data: response.data || [],
        dates: response.dates || [],
        alerts: response.alerts || { a15: [], a10: [], a5: [] },
        alertCounts: response.alertCounts || { a15: 0, a10: 0, a5: 0 },
        total: response.total || 0,
        page: response.page || 1,
        pages: response.pages || 0,
        size: response.size || 20,
      };
    } finally {
      linkDashboardLoading.value = false;
    }
  }

  async function saveTargets(month, config) {
    if (demoMode) {
      targets.value = { ...targets.value, [month]: config };
      return { success: true, demo: true };
    }
    const response = await request('/api/v3/admin/targets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ month, config }),
    });
    if (!response?.success) throw new Error(response?.error || '目标保存失败');
    targets.value = { ...targets.value, [month]: config };
  }

  async function submitDelist(payload) {
    if (demoMode) return { success: true, demo: true, message: `Demo mode accepted ${payload?.link_ids?.length || 0} links` };
    return request('/api/delist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }

  return {
    data,
    status,
    targets,
    loading,
    error,
    lastUpdated,
    links,
    linkFields,
    linksMeta,
    linksLoading,
    linkDashboard,
    linkDashboardLoading,
    availableDates,
    loadAll,
    refresh,
    loadLinks,
    loadLinkDashboard,
    saveTargets,
    submitDelist,
  };
}
