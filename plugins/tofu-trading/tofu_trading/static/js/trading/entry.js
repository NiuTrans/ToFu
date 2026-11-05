/** Public plugin entry. Host ports bind before any feature starts requests. */
import './state.js';

export async function mount(host) {
  window.TradingApp.bindHost(host);
  await Promise.all([
    import('./dashboard.js'), import('./simulator.js'), import('./tasks.js'),
    import('./overview.js'), import('./brain.js'), import('./reconcile.js'),
  ]);
  await import('./init.js');
}
