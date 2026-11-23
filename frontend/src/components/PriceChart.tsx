import { useEffect, useRef, useState } from "react";
import { createChart, IChartApi, ISeriesApi, SeriesMarker, Time } from "lightweight-charts";
import { api, Evaluation } from "../api";

interface Bar { time: string; open: number; high: number; low: number; close: number; volume: number }
interface Overlay { name: string; values: { time: string; value: number }[] }

function css(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function PriceChart({ symbol, tick, evaluation, journal, live }: { symbol: string; tick: number; evaluation: Evaluation | null; journal: any[]; live: boolean }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<{ c: ISeriesApi<"Candlestick">; lines: ISeriesApi<"Line">[]; v: ISeriesApi<"Histogram"> } | null>(null);
  const [meta, setMeta] = useState<{ names: string[]; strategy: string; source: string } | null>(null);

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: css("--muted"), fontFamily: "IBM Plex Mono, ui-monospace, monospace", fontSize: 11 },
      grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
      rightPriceScale: { borderColor: css("--line") },
      timeScale: { borderColor: css("--line") },
      crosshair: { mode: 0 },
    });
    const cs = c.addCandlestickSeries({ upColor: css("--up"), downColor: css("--down"), borderVisible: false, wickUpColor: css("--up"), wickDownColor: css("--down") });
    const lines = [css("--accent"), css("--warn")].map((color) =>
      c.addLineSeries({ color, lineWidth: 2, priceLineVisible: false, lastValueVisible: true }));   // up to two strategy overlays
    const vs = c.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, color: css("--vol") });
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    cs.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.22 } });
    chart.current = c;
    series.current = { c: cs, lines, v: vs };
    return () => {
      series.current = null; chart.current = null;
      // lightweight-charts may have a paint queued for this frame; dispose after it runs to avoid "Object is disposed".
      requestAnimationFrame(() => c.remove());
    };
  }, []);

  useEffect(() => {
    let cancelled = false;   // a response arriving after unmount or a symbol switch must not touch a disposed chart
    api.get<{ bars: Bar[]; overlays: Overlay[]; strategy_name: string; source: string }>(`/api/symbols/${symbol}/chart?sessions=120`).then((d) => {
      const s = series.current;
      if (cancelled || !s) return;
      s.c.setData(d.bars.map((b) => ({ time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close })));
      s.lines.forEach((ls, k) => {
        const o = d.overlays[k];
        ls.setData(o ? o.values.map((p) => ({ time: p.time as Time, value: p.value })) : []);
        ls.applyOptions({ title: o?.name ?? "", visible: !!o });
      });
      s.v.setData(d.bars.map((b, i) => ({ time: b.time as Time, value: b.volume,
        color: i > 0 && b.close >= d.bars[i - 1].close ? css("--vol-up") : css("--vol") })));
      setMeta({ names: d.overlays.map((o) => o.name), strategy: d.strategy_name, source: d.source });

      const markers: SeriesMarker<Time>[] = [];
      if (evaluation && evaluation.symbol === symbol) {
        const ok = evaluation.status.startsWith("QUALIFIES");
        markers.push({ time: evaluation.date as Time, position: "belowBar", shape: "arrowUp",
          color: ok ? css("--up") : css("--warn"), text: ok ? "signal" : evaluation.status.toLowerCase().replace("_", " ") });
      }
      for (const j of journal.filter((x) => x.symbol === symbol)) {
        if (j.evidence.date !== evaluation?.date)
          markers.push({ time: j.evidence.date as Time, position: "belowBar", shape: "arrowUp", color: css("--up"), text: `signal (rules v${j.ruleset_version})` });
        if (j.entry_date) markers.push({ time: j.entry_date as Time, position: "belowBar", shape: "circle", color: css("--accent"), text: `buy ${j.qty}` });
        if (j.exit_date) markers.push({ time: j.exit_date as Time, position: "aboveBar", shape: "arrowDown", color: css("--text"), text: `exit (${j.exit_reason})` });
      }
      const inRange = new Set(d.bars.map((b) => b.time));
      s.c.setMarkers(markers.filter((m) => inRange.has(m.time as string)).sort((a, b) => (a.time as string).localeCompare(b.time as string)));
      chart.current?.timeScale().setVisibleLogicalRange({ from: d.bars.length - 75, to: d.bars.length + 3 });
    }).catch(() => { /* chart fetch failed (e.g. no bars yet); leave the previous view */ });
    return () => { cancelled = true; };
  }, [symbol, tick, evaluation, journal]);

  // Stop level as a price line.
  useEffect(() => {
    const s = series.current;
    if (!s || !evaluation?.suggested_stop || !evaluation.status.startsWith("QUALIFIES")) return;
    const line = s.c.createPriceLine({ price: evaluation.suggested_stop, color: css("--down"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "planned stop" });
    return () => { if (series.current === s) s.c.removePriceLine(line); };
  }, [evaluation]);

  return (
    <div className="chart-wrap">
      <div className="chart-head">
        <b>{symbol}</b>
        <span className="muted small">Daily · completed bars · {meta?.strategy}{meta?.names.length ? ` · ${meta.names.join(", ")}` : ""}</span>
        {live ? <span className="livetag" title={meta?.source}>real prices · {meta?.source?.startsWith("Alpaca") ? "Alpaca SIP (licensed)" : "Yahoo Finance"}</span> : <span className="simtag" title={meta?.source}>simulated prices</span>}
      </div>
      <div ref={el} className="chart" />
    </div>
  );
}
