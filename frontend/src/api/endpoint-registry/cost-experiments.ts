/** costExperiments endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createCostExperimentsEndpoints(ports: EndpointPorts) {
  const { get } = ports;
  const costExperiments = {
    report: (days: number) => get('/api/v4/cost-experiments/report?days=' +
                          encodeURIComponent(days || 14)),
  };
  return costExperiments;
}
