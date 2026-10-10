/** text endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions } from './contracts';
export function createTextEndpoints(ports: EndpointPorts) {
  const { post } = ports;
  const text = {
    // opts.forceFasttext: force the statistical detector (skip-gate callers
    // that must distinguish kanji-heavy Japanese from Chinese pass true).
    detectLanguage: (textBody: string, opts: EndpointOptions = {}) => post('/api/v4/text/detect-language',
      { text: textBody, forceFasttext: !!opts.forceFasttext }, { onError: 'null' }),
  };
  return text;
}
