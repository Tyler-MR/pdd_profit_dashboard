<template>
  <section class="panel chart-panel">
    <div class="panel-heading">
      <div>
        <h2>{{ title }}</h2>
        <p v-if="subtitle">{{ subtitle }}</p>
      </div>
      <slot name="actions"></slot>
      <span v-if="badge" class="panel-badge">{{ badge }}</span>
    </div>
    <div v-if="empty" class="chart-empty">暂无可用数据</div>
    <div v-else ref="chartEl" class="chart" :style="{ height: `${height}px` }"></div>
  </section>
</template>

<script setup>
import * as echarts from 'echarts';
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

const props = defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, default: '' },
  badge: { type: String, default: '' },
  options: { type: Object, required: true },
  height: { type: Number, default: 300 },
  empty: { type: Boolean, default: false },
});
const emit = defineEmits(['chart-click']);

const chartEl = ref(null);
let chart;
let resizeObserver;
let chartClickHandler;

function draw() {
  if (!chart || props.empty) return;
  chart.setOption(props.options, true);
  chart.resize();
}

onMounted(async () => {
  await nextTick();
  if (!chartEl.value) return;
  chart = echarts.init(chartEl.value);
  chartClickHandler = (event) => {
    const x = Number(event?.offsetX ?? event?.zrX);
    const y = Number(event?.offsetY ?? event?.zrY);
    if (!Number.isFinite(x) || !Number.isFinite(y) || !chart.containPixel('grid', [x, y])) return;

    const point = chart.convertFromPixel({ gridIndex: 0 }, [x, y]);
    const dataIndex = Math.round(Number(point?.[0]));
    const value = Number(point?.[1]);
    if (!Number.isFinite(dataIndex) || !Number.isFinite(value)) return;

    const candidates = (chart.getOption().series || [])
      .map((series) => {
        const raw = series.data?.[dataIndex];
        const candidateValue = Number(raw && typeof raw === 'object' ? raw.value : raw);
        return { series, candidateValue };
      })
      .filter(({ series, candidateValue }) => series.type === 'line' && Number.isFinite(candidateValue));
    if (!candidates.length) return;

    const nearest = candidates.reduce((best, candidate) => (
      Math.abs(candidate.candidateValue - value) < Math.abs(best.candidateValue - value) ? candidate : best
    ));
    emit('chart-click', {
      componentType: 'series',
      seriesType: nearest.series.type,
      seriesName: nearest.series.name,
      dataIndex,
      value: nearest.candidateValue,
    });
  };
  chart.getZr().on('click', chartClickHandler);
  resizeObserver = new ResizeObserver(() => chart?.resize());
  resizeObserver.observe(chartEl.value);
  draw();
});

watch(() => [props.options, props.empty], draw, { deep: true });

onBeforeUnmount(() => {
  resizeObserver?.disconnect();
  chart?.getZr().off('click', chartClickHandler);
  chart?.dispose();
});
</script>
