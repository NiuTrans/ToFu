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
}
impl DispatchedResponse {
    pub(crate) fn successful(request: &Request, payload: IrResponse) -> Self {
        let canonical = payload.json_proven().then_some(CanonicalPayloadCertificate);
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
        }
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
        Self { response, canonical: None }
    }
}
impl std::ops::Deref for DispatchedResponse {
    type Target = Response;
    fn deref(&self) -> &Self::Target { &self.response }
}
