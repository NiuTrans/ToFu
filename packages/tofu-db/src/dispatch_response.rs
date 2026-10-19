//! Immutable dispatch result carrying canonical serializer/depth evidence.
//! Entry: successful consumes the actual selected IrResponse. Public Response
//! extraction discards the certificate before exposing any mutable byte vector.
//! Unknown/control/search/notice/replay bytes retain the transport parser.

use crate::protocol::{Request, Response};
use crate::transaction_ir::IrResponse;

struct CanonicalPayloadCertificate;
pub(crate) struct DispatchedResponse {
    response: Response,
    canonical: Option<CanonicalPayloadCertificate>,
    snapshot_stable: Option<crate::query_result_cache::Admission>,
}
impl DispatchedResponse {
    pub(crate) fn successful(request: &Request, payload: IrResponse) -> Self {
        let canonical = payload.json_proven().then_some(CanonicalPayloadCertificate);
        let snapshot_stable = payload.cache_admission();
        Self {
            response: Response {
                correlation_id: request.correlation_id,
                schema_id: request.schema_id,
                payload: payload.into_bytes(),
                status: 0,
                error_code: None,
                error_message: None,
                retryable: false,
            },
            canonical,
            snapshot_stable,
        }
    }
    pub(crate) fn cache_admission(&self) -> Option<crate::query_result_cache::Admission> {
        if self.canonical.is_some() { self.snapshot_stable } else { None }
    }
    #[cfg(test)]
    pub(crate) fn snapshot_cacheable(&self) -> bool { self.cache_admission().is_some() }
    pub(crate) fn snapshot_cache_copy(&self) -> Option<IrResponse> {
        self.cache_admission().map(|admission|
            IrResponse::from_snapshot_cache(self.response.payload.clone(), admission))
    }
    pub(crate) fn canonical_payload(&self) -> Option<&[u8]> {
        self.canonical.as_ref().map(|_| self.response.payload.as_slice())
    }
    pub(crate) fn into_response(self) -> Response {
        self.response
    }
}
impl From<Response> for DispatchedResponse {
    fn from(response: Response) -> Self {
        Self { response, canonical: None, snapshot_stable: None }
    }
}
impl std::ops::Deref for DispatchedResponse {
    type Target = Response;
    fn deref(&self) -> &Self::Target { &self.response }
}
