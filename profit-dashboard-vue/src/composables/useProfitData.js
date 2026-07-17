import { computed, ref } from 'vue';

const apiBase = import.meta.env.VITE_API_BASE || '';

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
      await request('/api/v3/refresh', { method: 'POST' });
      await loadAll(lastDataParams.value);
    } finally {
      loading.value = false;
    }
  }

  async function loadLinks(params = {}) {
    linksLoading.value = true;
    try {
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
    const response = await request('/api/v3/admin/targets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ month, config }),
    });
    if (!response?.success) throw new Error(response?.error || '目标保存失败');
    targets.value = { ...targets.value, [month]: config };
  }

  async function submitDelist(payload) {
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
