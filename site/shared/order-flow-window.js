// Strike prices arrive in descending order. Keep manual inspection anchored to a price
// so a refreshed universe cannot silently move it to another contract.
function createStrikeWindow() {
  const widths = [5, 9, 17, 33, 65, 129, Infinity];
  let level = 2, follow = true, anchor = null, prices = [], center = 0;
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(n, hi));
  function nearest(value) {
    return prices.reduce((best, p, i) => Math.abs(p - value) < Math.abs(prices[best] - value) ? i : best, 0);
  }
  return {
    update(list, spot) {
      prices = list;
      if (!prices.length) return {prices: [], follow};
      if (follow && Number.isFinite(spot)) anchor = spot;
      const width = Math.min(widths[level], prices.length);
      const start = clamp((anchor == null ? Math.floor(prices.length / 2) : nearest(anchor)) - Math.floor(width / 2), 0, prices.length - width);
      center = start + Math.floor(width / 2);
      return {prices: prices.slice(start, start + width), center, follow, level, lastLevel: widths.length - 1};
    },
    pan(steps) {
      if (!prices.length) return;
      this.goTo(prices[clamp(center + steps, 0, prices.length - 1)]);
    },
    goTo(price) { if (Number.isFinite(price)) { follow = false; anchor = price; } },
    zoom(delta) { level = clamp(level + delta, 0, widths.length - 1); },
    resume() { follow = true; },
    reset() { level = 2; follow = true; anchor = null; prices = []; center = 0; }
  };
}
if (typeof module !== 'undefined') module.exports = createStrikeWindow;
