/* ===== migrated source: api.js ===== */
/* Endpoint names and domain projections live in api/endpoint-registry/.
 * This temporary compatibility binding is deleted with the final retained
 * Api caller. HTTP, affinity and ApiError retain one typed transport owner. */
runtimeScope.Api = createEndpointRegistry(requiredApiTransport, () =>
  typeof _i18nLang !== 'undefined' ? _i18nLang : 'zh');
const Api = runtimeScope.Api;
